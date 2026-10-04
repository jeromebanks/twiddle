"""The speaker's `AlarmClock` service: reads, and the three writes.

Alarms are household-wide, so any speaker answers for all of them. The reads
are `ListAlarms`, `GetTimeNow` and `GetFormat`. `GetTimeZone` isn't read:
`GetTimeNow`'s `CurrentLocalTime` is already the household's wall-clock time,
and the zone is only an opaque index and a DST flag.

The writes are `CreateAlarm`, `UpdateAlarm` and `DestroyAlarm`. AlarmClock has
no compare-and-swap, so each write re-reads the list first and refuses, sending
nothing, if `CurrentAlarmListVersion` has moved from the version the caller
read (someone edited an alarm in the Sonos app meanwhile). The window between
that re-read and the write is unavoidable, and short. Every write is journalled
with the alarm before and after, so a deleted or clobbered alarm can be
recreated from `logs/interventions.jsonl`.

Each call has a pure parser beside it, so tests feed recorded SOAP responses
and never reach a speaker.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

from .. import play
from ..devices import soap
from .model import Alarm, parse_alarms

SERVICE = "AlarmClock"
READS = frozenset({"ListAlarms", "GetTimeNow", "GetFormat"})
WRITES = frozenset({"CreateAlarm", "UpdateAlarm", "DestroyAlarm"})


@dataclass
class AlarmList:
    version: str            # CurrentAlarmListVersion, "<uuid>:<n>"; changes on every edit
    alarms: list[Alarm]
    xml: str = ""           # the CurrentAlarmList document exactly as it came

    def get(self, alarm_id: str | None) -> Alarm | None:
        return next((a for a in self.alarms if a.id == alarm_id), None)


class VersionChanged(RuntimeError):
    """The alarm list moved since it was read. Nothing was written; `current`
    is the list as it is now."""

    def __init__(self, expected: str, current: AlarmList):
        self.expected, self.current = expected, current
        super().__init__(f"the alarm list changed since it was read "
                         f"(version {expected}, now {current.version}); nothing written")


@dataclass
class HouseholdTime:
    """The household's own clock and display format, as the speaker reports it."""
    local: datetime         # naive: the household's local wall-clock time
    utc: datetime
    time_format: str = ""   # "INV" (unset) observed; "12H"/"24H" assumed
    date_format: str = ""

    def to_dict(self) -> dict:
        return {"local": self.local.isoformat(), "utc": self.utc.isoformat(),
                "time_format": self.time_format, "date_format": self.date_format}


def _response(xml: str, action: str) -> dict[str, str]:
    """The out-arguments of a SOAP `<action>Response`, by name."""
    body = ET.fromstring(xml)
    for el in body.iter():
        if el.tag.endswith(f"}}{action}Response") or el.tag == f"{action}Response":
            return {c.tag.rsplit("}", 1)[-1]: c.text or "" for c in el}
    raise ValueError(f"no {action}Response in the speaker's answer")


def parse_list_alarms(xml: str) -> AlarmList:
    r = _response(xml, "ListAlarms")
    doc = r.get("CurrentAlarmList") or "<Alarms/>"
    return AlarmList(r.get("CurrentAlarmListVersion", ""), parse_alarms(doc), doc)


def parse_time_now(xml: str) -> tuple[datetime, datetime]:
    r = _response(xml, "GetTimeNow")
    fmt = "%Y-%m-%d %H:%M:%S"
    return (datetime.strptime(r["CurrentLocalTime"], fmt),
            datetime.strptime(r["CurrentUTCTime"], fmt))


def parse_format(xml: str) -> tuple[str, str]:
    r = _response(xml, "GetFormat")
    return r.get("CurrentTimeFormat", ""), r.get("CurrentDateFormat", "")


def _read(ip: str, action: str) -> str:
    if action not in READS:
        raise ValueError(f"{action} is not an AlarmClock read")
    return soap(ip, SERVICE, action)


def list_alarms(ip: str) -> AlarmList:
    """Every alarm in the household (read-only)."""
    return parse_list_alarms(_read(ip, "ListAlarms"))


def household_time(ip: str) -> HouseholdTime:
    """The household's time now, and how it likes times written (read-only)."""
    local, utc = parse_time_now(_read(ip, "GetTimeNow"))
    time_format, date_format = parse_format(_read(ip, "GetFormat"))
    return HouseholdTime(local, utc, time_format, date_format)


# ---- writes -----------------------------------------------------------------

def _write(ip: str, action: str, args: list[tuple[str, str]]) -> dict[str, str]:
    """Send one AlarmClock write. Each argument is escaped exactly once: the
    model keeps values decoded, and `soap` sends its body as given."""
    if action not in WRITES:
        raise ValueError(f"{action} is not an AlarmClock write")
    body = "".join(f"<{k}>{play._esc(v)}</{k}>" for k, v in args)
    return _response(soap(ip, SERVICE, action, body), action)


def _record(alarm: Alarm | None) -> dict | None:
    """An alarm as the journal keeps it: enough to recreate it."""
    if alarm is None:
        return None
    return {"attributes": alarm.to_attributes(), "children": list(alarm.children)}


def _checked(ip: str, expected_version: str) -> AlarmList:
    current = list_alarms(ip)
    if current.version != expected_version:
        raise VersionChanged(expected_version, current)
    return current


def _journalled(ip: str, action: str, alarm_id: str | None, before: Alarm | None,
                args: list[tuple[str, str]]) -> tuple[Alarm | None, AlarmList]:
    """Send a write, then read the list back; journal both whatever happens.

    A write that raises may still have landed (a timeout after the speaker
    acted), so the journal line is written in any case, with `error` when it
    failed. Returns the alarm as the speaker now has it (None once destroyed)
    and the list it came from, whose version the next write expects.
    """
    entry: dict = {"alarm_id": alarm_id, "before": _record(before), "sent": dict(args)}
    try:
        reply = _write(ip, action, args)
        alarm_id = reply.get("AssignedID") or alarm_id
        entry["alarm_id"] = alarm_id
        after_list = list_alarms(ip)
        after = after_list.get(alarm_id)
        entry["after"] = _record(after)
        entry["version"] = after_list.version
        return after, after_list
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        play._journal(f"alarm_{action.removesuffix('Alarm').lower()}", ip, **entry)


def create_alarm(ip: str, alarm: Alarm, expected_version: str) -> tuple[Alarm, AlarmList]:
    """Create `alarm` (its `id` is ignored; the speaker assigns one)."""
    _checked(ip, expected_version)
    after, now = _journalled(ip, "CreateAlarm", None, None, alarm.create_args())
    if after is None:
        raise RuntimeError("CreateAlarm answered but the new alarm isn't in the list")
    return after, now


def update_alarm(ip: str, alarm: Alarm, expected_version: str) -> tuple[Alarm, AlarmList]:
    """Overwrite the alarm with `alarm.id` with every field of `alarm`."""
    before = _checked(ip, expected_version).get(alarm.id)
    if before is None:
        raise KeyError(f"no alarm {alarm.id} to update")
    after, now = _journalled(ip, "UpdateAlarm", alarm.id, before, alarm.update_args())
    if after is None:
        raise RuntimeError(f"alarm {alarm.id} vanished after UpdateAlarm")
    return after, now


def destroy_alarm(ip: str, alarm_id: str, expected_version: str) -> AlarmList:
    """Delete one alarm; the journal keeps all of it."""
    before = _checked(ip, expected_version).get(alarm_id)
    if before is None:
        raise KeyError(f"no alarm {alarm_id} to destroy")
    _, now = _journalled(ip, "DestroyAlarm", alarm_id, before, [("ID", alarm_id)])
    return now
