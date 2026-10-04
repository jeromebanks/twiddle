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
recreated from `logs/interventions.jsonl` (`deleted`, `recreate`).

Each call has a pure parser beside it, so tests feed recorded SOAP responses
and never reach a speaker.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import play
from ..devices import soap
from .model import Alarm, key, parse_alarms

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


class AlarmWriteError(RuntimeError):
    """A write that stopped short. `landed` is whether the list read back
    shows the speaker did it (None when that can't be told); `alarm_id` and
    `alarm` are the alarm written, as read back; `current` is the list as
    last read, None if it couldn't be."""

    def __init__(self, message: str, *, landed: bool | None = False,
                 alarm_id: str | None = None, alarm: Alarm | None = None,
                 current: AlarmList | None = None):
        super().__init__(message)
        self.landed, self.alarm_id, self.alarm, self.current = landed, alarm_id, alarm, current


class VersionChanged(AlarmWriteError):
    """The alarm list moved by someone else's hand. Either before the write,
    and nothing was written; or between the write and its read-back, when
    the write landed and `others` names the alarms someone else changed."""

    def __init__(self, expected: str, current: AlarmList, others: list[str] = (),
                 **landed):
        self.expected, self.others = expected, list(others)
        if landed.get("landed"):
            msg = (f"alarm {landed.get('alarm_id')} was written, but alarms "
                   f"{', '.join(self.others)} changed meanwhile; stopped")
        else:
            msg = (f"the alarm list changed since it was read "
                   f"(version {expected}, now {current.version}); nothing written")
        super().__init__(msg, current=current, **landed)


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


def from_record(rec: dict) -> Alarm:
    """An alarm as the journal kept it (`_record`'s inverse), through the same
    parser as ListAlarms: unknown attributes, children and escaping included."""
    el = ET.Element("Alarm", rec["attributes"])
    for child in rec.get("children", ()):
        el.append(ET.fromstring(child))
    return Alarm.from_element(el)


def deleted(alarm_id: str, log: Path | None = None) -> Alarm | None:
    """Alarm `alarm_id` as it was when twiddle deleted it: the `before` of the
    journal's last `alarm_destroy` of it that landed. None if there is none."""
    log = log or play.INTERVENTION_LOG
    found = None
    try:
        lines = log.read_text().splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (rec.get("action") == "alarm_destroy" and rec.get("alarm_id") == alarm_id
                and rec.get("written") is True and rec.get("before")):
            found = rec["before"]
    return from_record(found) if found else None


def _checked(ip: str, expected_version: str) -> AlarmList:
    current = list_alarms(ip)
    if current.version != expected_version:
        raise VersionChanged(expected_version, current)
    return current


def _landed(action: str, read: AlarmList, now: AlarmList, alarm_id: str | None,
            want: Alarm | None) -> tuple[bool | None, str | None]:
    """Did a write whose answer was lost happen? Judged from the list read
    back: (True/False, or None when it can't be told; the alarm's ID)."""
    if action == "DestroyAlarm":
        return now.get(alarm_id) is None, alarm_id
    if action == "UpdateAlarm":
        got = now.get(alarm_id)
        if got is not None and key(got) == key(want):
            return True, alarm_id
        return (False if got == read.get(alarm_id) else None), alarm_id
    new = [a for a in now.alarms if read.get(a.id) is None]
    same = [a for a in new if key(a) == key(want)]
    if len(same) == 1:
        return True, same[0].id
    return (False if not new else None), None


def _others(read: AlarmList, now: AlarmList, alarm_id: str | None) -> list[str]:
    """The alarms that changed between two reads, apart from `alarm_id`."""
    ids = {a.id for a in read.alarms} | {a.id for a in now.alarms}
    return sorted((i for i in ids - {alarm_id} if read.get(i) != now.get(i)),
                  key=lambda i: (len(i), i))


def _journalled(ip: str, action: str, read: AlarmList, alarm_id: str | None,
                args: list[tuple[str, str]], want: Alarm | None = None
                ) -> tuple[Alarm | None, AlarmList]:
    """Send a write, read the list back, and journal both whatever happens.

    The read-back must differ from `read` (the list the write was checked
    against) only in the alarm written. If anything else moved, someone
    edited an alarm in that window: VersionChanged, and the caller stops
    rather than overwrite it. A write that raises may still have landed (a
    timeout after the speaker acted), so the list is read back then too and
    `written` says whether it shows the write: true, false, or null when it
    can't be told. Returns the alarm as the speaker now has it (None once
    destroyed) and the list it came from, whose version the next write expects.
    """
    entry: dict = {"alarm_id": alarm_id, "before": _record(read.get(alarm_id)),
                   "sent": dict(args), "written": False}
    landed, failed, now, after = None, None, None, None
    try:
        try:
            reply = _write(ip, action, args)
            alarm_id, landed = reply.get("AssignedID") or alarm_id, True
        except Exception as exc:
            failed = exc
        try:
            now = list_alarms(ip)
        except Exception as exc:
            failed = failed or exc
        if now is not None and landed is None:
            landed, alarm_id = _landed(action, read, now, alarm_id, want)
        after = now.get(alarm_id) if now is not None else None
        others = _others(read, now, alarm_id) if now is not None else []
        entry |= {"alarm_id": alarm_id, "written": landed, "after": _record(after)}
        if now is not None:
            entry["version"] = now.version
        if others:
            entry["others_changed"] = others
        if failed is not None:
            entry["error"] = f"{type(failed).__name__}: {failed}"
    finally:
        play._journal(f"alarm_{action.removesuffix('Alarm').lower()}", ip, **entry)
    found = {"landed": landed, "alarm_id": alarm_id, "alarm": after}
    if failed is not None:
        raise AlarmWriteError(f"{action}: {type(failed).__name__}: {failed}",
                              current=now, **found) from failed
    if others:
        raise VersionChanged(read.version, now, others, **found)
    if action != "DestroyAlarm" and after is None:
        raise AlarmWriteError(f"{action} answered, but alarm {alarm_id} isn't in "
                              "the list read back", current=now, **found)
    return after, now


def create_alarm(ip: str, alarm: Alarm, expected_version: str) -> tuple[Alarm, AlarmList]:
    """Create `alarm` (its `id` is ignored; the speaker assigns one)."""
    read = _checked(ip, expected_version)
    return _journalled(ip, "CreateAlarm", read, None, alarm.create_args(), alarm)


def update_alarm(ip: str, alarm: Alarm, expected_version: str) -> tuple[Alarm, AlarmList]:
    """Overwrite the alarm with `alarm.id` with every field of `alarm`."""
    read = _checked(ip, expected_version)
    if read.get(alarm.id) is None:
        raise KeyError(f"no alarm {alarm.id} to update")
    return _journalled(ip, "UpdateAlarm", read, alarm.id, alarm.update_args(), alarm)


def destroy_alarm(ip: str, alarm_id: str, expected_version: str) -> AlarmList:
    """Delete one alarm; the journal keeps all of it."""
    read = _checked(ip, expected_version)
    if read.get(alarm_id) is None:
        raise KeyError(f"no alarm {alarm_id} to destroy")
    return _journalled(ip, "DestroyAlarm", read, alarm_id, [("ID", alarm_id)])[1]


def recreate(ip: str, alarm_id: str, expected_version: str) -> tuple[Alarm, AlarmList]:
    """Create again the alarm twiddle deleted as `alarm_id`, from the journal.
    It gets a new ID. Its child elements (a Spotify alarm's `<Content>`) can't
    be recreated: CreateAlarm has no argument for them."""
    alarm = deleted(alarm_id)
    if alarm is None:
        raise KeyError(f"no deleted alarm {alarm_id} in {play.INTERVENTION_LOG}")
    return create_alarm(ip, alarm, expected_version)
