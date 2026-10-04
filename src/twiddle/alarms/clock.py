"""The speaker's `AlarmClock` service: reads, and the three writes; and the
`AVTransport` side of alarms: is one ringing, try it now, stop, snooze.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .. import play
from ..devices import PORT, soap
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


def unrecreatable(alarm: Alarm) -> list[str]:
    """What of `alarm` CreateAlarm can't set, so a recreated copy lacks: its
    child elements (a Spotify alarm's `<Content>`) and attributes the model
    doesn't know. Every field CreateAlarm takes comes back exactly."""
    lost = []
    if alarm.children:
        lost.append(f"its child elements ({len(alarm.children)})")
    if alarm.extra:
        lost.append(f"attributes {', '.join(sorted(alarm.extra))}")
    return lost


def recreate(ip: str, alarm_id: str, expected_version: str
             ) -> tuple[Alarm, AlarmList, list[str]]:
    """Create again the alarm twiddle deleted as `alarm_id`, from the journal:
    every CreateAlarm field exactly, under a new ID, and `unrecreatable`'s
    list of what couldn't come back. Refused, sending nothing, if an alarm
    equal to it is already there (recreated before, say)."""
    alarm = deleted(alarm_id)
    if alarm is None:
        raise KeyError(f"no deleted alarm {alarm_id} in {play.INTERVENTION_LOG}")
    twin = next((a for a in list_alarms(ip).alarms if key(a) == key(alarm)), None)
    if twin is not None:
        raise ValueError(f"alarm {twin.id} is already the same as deleted alarm {alarm_id}")
    return (*create_alarm(ip, alarm, expected_version), unrecreatable(alarm))


# ---- AVTransport: is one ringing; try it now, stop it, snooze it -------------
#
# Ringing belongs to a group's transport, not to the household, so these go to
# a group coordinator's AVTransport, never to any speaker that will answer.
# The argument names are the speaker's own (/xml/AVTransport1.xml, recorded in
# tests/fixtures/avtransport_alarm_scpd.xml).

AV_READS = frozenset({"GetRunningAlarmProperties"})
AV_WRITES = frozenset({"RunAlarm", "SnoozeAlarm", "Stop"})
# GetRunningAlarmProperties answers UPnPError 800 when no alarm is running
# (observed on a Beam and a Roam, 2026-10-04, nothing ringing).
NOT_RUNNING = "800"
SNOOZE_MINUTES = (5, 10, 15, 30)
DEFAULT_SNOOZE = 10
FIRE_MARGIN_S = 180     # how loosely the speaker keeps a duration or snooze


@dataclass
class Running:
    """An alarm going off (or snoozed) on one group's transport.

    `alarm_id`, `group_id` and `logged_start` are GetRunningAlarmProperties';
    `alarm_running`/`snooze_running` are LastChange's `AlarmRunning` and
    `SnoozeRunning`, None when no event could be had. A snoozed alarm may not
    answer GetRunningAlarmProperties at all (not observed yet), so one known
    only from LastChange has an empty `alarm_id`.
    """
    alarm_id: str = ""
    group_id: str = ""
    logged_start: str = ""
    alarm_running: bool | None = None
    snooze_running: bool | None = None

    @property
    def snoozed(self) -> bool:
        return bool(self.snooze_running) and not self.alarm_running

    def to_dict(self) -> dict:
        return {"alarm_id": self.alarm_id or None, "group_id": self.group_id or None,
                "logged_start": self.logged_start or None,
                "alarm_running": self.alarm_running, "snooze_running": self.snooze_running,
                "snoozed": self.snoozed}


def upnp_error(text: str) -> str | None:
    """The `errorCode` of a SOAP fault, or None if `text` isn't one."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    code = next((el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "errorCode"), None)
    return (code.text or "").strip() if code is not None else None


def parse_running_alarm(xml: str) -> Running | None:
    r = _response(xml, "GetRunningAlarmProperties")
    if r.get("AlarmID", "") in ("", "0"):
        return None
    return Running(r["AlarmID"], r.get("GroupID", ""), r.get("LoggedStartTime", ""))


def parse_last_change(notify: str) -> dict[str, str]:
    """An AVTransport NOTIFY body's LastChange, flattened to {variable: val}
    for instance 0: `TransportState`, `AlarmRunning`, `SnoozeRunning`, ..."""
    prop = ET.fromstring(notify)
    lc = next((el.text for el in prop.iter() if el.tag.rsplit("}", 1)[-1] == "LastChange"), "")
    if not lc:
        return {}
    event = ET.fromstring(lc)
    inst = next((el for el in event if el.tag.rsplit("}", 1)[-1] == "InstanceID"
                 and el.get("val") == "0"), None)
    if inst is None:
        return {}
    return {el.tag.rsplit("}", 1)[-1]: el.get("val", "") for el in inst}


def _flag(state: dict[str, str], name: str) -> bool | None:
    return state[name] == "1" if name in state else None


def _av_read(ip: str, action: str) -> str:
    if action not in AV_READS:
        raise ValueError(f"{action} is not an AVTransport alarm read")
    return play._av(ip, action, "")


def running_alarm(ip: str) -> Running | None:
    """The alarm going off on the group `ip` coordinates, or None (read-only)."""
    try:
        return parse_running_alarm(_av_read(ip, "GetRunningAlarmProperties"))
    except requests.HTTPError as exc:
        text = exc.response.text if exc.response is not None else ""
        if upnp_error(text) == NOT_RUNNING:
            return None
        raise


def last_change(ip: str, wait: float = 3.0) -> dict[str, str] | None:
    """AVTransport's state from one GENA event: subscribe, take the initial
    NOTIFY (it carries every variable), unsubscribe. Read-only, the same
    subscription `monitor` keeps on the anchor. None if no event came.

    The callback listens only on the address facing the speaker. Whatever a
    NOTIFY does (stall halfway, trickle bytes forever), this returns once
    `wait` is up: each request is handled on a daemon thread, and at the end
    every connection still open is shut, so nothing is waited for."""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    got: list[str] = []
    arrived = threading.Event()
    live: set[socket.socket] = set()

    class Handler(BaseHTTPRequestHandler):
        timeout = wait                       # each socket read, the body's included

        def setup(self):
            live.add(self.request)
            super().setup()

        def finish(self):
            live.discard(self.request)
            super().finish()

        def do_NOTIFY(self):  # noqa: N802 - UPnP verb
            n = int(self.headers.get("Content-Length", 0) or 0)
            got.append(self.rfile.read(n).decode("utf-8", "replace"))
            self.send_response(200)
            self.end_headers()
            arrived.set()

        def log_message(self, *_args):
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False

        def handle_error(self, request, client_address):
            pass                             # a connection we shut, or a bad NOTIFY

    path = f"http://{ip}:{PORT}/MediaRenderer/AVTransport/Event"
    server, sid = None, ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((ip, PORT))
            me = s.getsockname()[0]
        server = Server((me, 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        r = requests.request("SUBSCRIBE", path, timeout=wait,
                             headers={"CALLBACK": f"<http://{me}:{server.server_port}/>",
                                      "NT": "upnp:event", "TIMEOUT": "Second-60"})
        sid = r.headers.get("SID", "")
        if sid:
            arrived.wait(wait)
    except (OSError, requests.RequestException):
        return None
    finally:
        if sid:
            try:
                requests.request("UNSUBSCRIBE", path, headers={"SID": sid}, timeout=wait)
            except requests.RequestException:
                pass
        if server is not None:
            server.shutdown()
            for conn in list(live):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            server.server_close()
    try:
        return parse_last_change(got[0]) if got else None
    except (ET.ParseError, ValueError):
        return None


def alarm_now(ip: str, events: bool = True) -> Running | None:
    """Whether an alarm is going off, or snoozed, on the group `ip`
    coordinates: GetRunningAlarmProperties, then (with `events`) LastChange's
    AlarmRunning/SnoozeRunning. Read-only.

    None when neither shows one, and also when an event says plainly that
    neither is running whatever GetRunningAlarmProperties named: callers stop
    the room on this answer, and wrongly refusing costs less than a bare Stop
    on ordinary playback. With no event, GetRunningAlarmProperties decides."""
    found = running_alarm(ip)
    state = last_change(ip) if events else None
    if state:
        alarm, snooze = _flag(state, "AlarmRunning"), _flag(state, "SnoozeRunning")
        if alarm is False and snooze is False:
            return None
        if found is None and not (alarm or snooze):
            return None
        found = found or Running()
        found.alarm_running, found.snooze_running = alarm, snooze
    return found


def _span(name: str, ip: str, at: datetime, **journal) -> None:
    """Journal a span around a moment the speaker will act on its own (an
    alarm's duration-stop, a snooze running out), so `analyse` discounts it."""
    for edge, t in (("start", at - timedelta(seconds=60)),
                    ("end", at + timedelta(seconds=FIRE_MARGIN_S))):
        play.journal_span(f"{name}_{edge}", ip, ts=t.isoformat(timespec="milliseconds"),
                          **journal)


def _av_write(ip: str, action: str, args: list[tuple[str, str]], journal: str,
              **extra) -> None:
    """Journal, then send, one AVTransport alarm write (play's own order)."""
    if action not in AV_WRITES:
        raise ValueError(f"{action} is not an AVTransport alarm write")
    play._journal(journal, ip, **extra)
    play._av(ip, action, "".join(f"<{k}>{play._esc(v)}</{k}>" for k, v in args))


def run_args(alarm: Alarm, logged_start: str) -> list[tuple[str, str]]:
    """RunAlarm's arguments for `alarm`, in the speaker's order."""
    return [("AlarmID", alarm.id or ""), ("LoggedStartTime", logged_start),
            ("Duration", alarm.duration), ("ProgramURI", alarm.program_uri),
            ("ProgramMetaData", alarm.program_metadata), ("PlayMode", alarm.play_mode),
            ("Volume", str(alarm.volume)),
            ("IncludeLinkedZones", "1" if alarm.include_linked_zones else "0")]


def run_alarm(ip: str, alarm: Alarm, logged_start: str) -> datetime | None:
    """Fire `alarm` now on the group `ip` coordinates (**writes** transport
    and volume). `logged_start` is the household's local time, as GetTimeNow
    writes it; the Roam took that and reported it back the same way from
    GetRunningAlarmProperties (2026-10-04). Returns when its duration will
    stop it, or None if it has none."""
    _av_write(ip, "RunAlarm", run_args(alarm, logged_start), "alarm_run",
              alarm_id=alarm.id, alarm=_record(alarm))
    seconds = play.parse_hms(alarm.duration)
    if not seconds:
        return None
    stops = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    _span("alarm_run_stop", ip, stops, alarm_id=alarm.id)
    return stops


def snooze_alarm(ip: str, minutes: int = DEFAULT_SNOOZE, alarm_id: str | None = None
                 ) -> datetime:
    """The speaker's own snooze, for `minutes` (**writes** transport).
    Returns when it will ring again."""
    if minutes not in SNOOZE_MINUTES:
        raise ValueError(f"snooze for {', '.join(map(str, SNOOZE_MINUTES))} minutes, "
                         f"not {minutes}")
    _av_write(ip, "SnoozeAlarm", [("Duration", play._hms(minutes * 60))], "alarm_snooze",
              minutes=minutes, alarm_id=alarm_id)
    rings = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    _span("alarm_snooze_ring", ip, rings, alarm_id=alarm_id)
    return rings


def stop_alarm(ip: str, alarm_id: str | None = None) -> None:
    """Stop the alarm going off: the group's own `Stop` (**writes** transport).
    The caller checks one is going off first; this stops whatever plays."""
    _av_write(ip, "Stop", [], "alarm_stop", alarm_id=alarm_id)
