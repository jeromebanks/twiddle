"""`twiddle alarm` -- the household's Sonos alarms from the command line.

`alarm list` is read-only: one `ListAlarms` plus the household's clock and
display format, from any speaker (alarms are household-wide). Every alarm is
shown under its room's name, including one aimed at a bonded follower or at a
speaker that has vanished: labelled, never hidden.

Follows the rest of the package: the `ok`/`error` envelope, `--json` anywhere.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, time, timedelta

from .alarms import clock
from .alarms.model import Alarm, Recurrence
from .control_cli import emit, fail
from .household import Household, Speaker

CHIME = "Sonos chime"
# Monday first for reading; Sonos numbers the days from Sunday = 0.
_READING_ORDER = (1, 2, 3, 4, 5, 6, 0)
_DAY = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}


def _household(args) -> Household:
    return Household.load(anchor=getattr(args, "anchor", None))


# ---- what an alarm is aimed at ---------------------------------------------

def _is_bonded_follower(house: Household, sp: Speaker) -> bool:
    """A unit that is part of a room but not the room itself.

    Invisible units (surrounds, sub) always are. Of several visible units in
    one room (a stereo pair) the primary is the group's coordinator when it is
    one of them, else the first listed in the bond's ChannelMapSet -- which in
    the author's household is also the coordinator, the left Roam; the
    fallback is inferred, not observed.
    """
    if sp.invisible:
        return True
    group = house.group_of(sp)
    mates = [m for m in group.members if m.room == sp.room and not m.invisible]
    if len(mates) < 2:
        return False
    if group.coordinator in mates:
        return sp is not group.coordinator
    member = house.topology.by_uuid(sp.uuid) if house.topology else None
    first = (member.chan_map.split(":", 1)[0] if member and member.chan_map else "")
    return bool(first) and sp.uuid != first


def aimed_at(house: Household, uuid: str) -> dict:
    """Which room an alarm's RoomUUID names, and whether that is a problem."""
    sp = next((s for s in house.speakers if s.uuid == uuid), None)
    if sp is not None:
        follower = _is_bonded_follower(house, sp)
        return {"room": sp.room or sp.name, "speaker": sp.name,
                "status": "bonded_follower" if follower else "ok"}
    vanished = house.topology.vanished if house.topology else []
    gone = next((v for v in vanished if v.uuid == uuid), None)
    if gone is not None:
        return {"room": gone.zone_name or uuid, "speaker": gone.zone_name,
                "status": "vanished"}
    return {"room": "(unknown speaker)", "speaker": "", "status": "unknown"}


# ---- describing one alarm ------------------------------------------------

def source_title(alarm: Alarm) -> str:
    """The alarm's own title for its source: the DIDL's dc:title, the chime,
    or failing both the URI's scheme. The model never parses the metadata;
    this does, for display only."""
    if alarm.program_uri.startswith("x-rincon-buzzer:"):
        return CHIME
    try:
        title = ET.fromstring(alarm.program_metadata).find(
            ".//{http://purl.org/dc/elements/1.1/}title")
        if title is not None and title.text:
            return title.text
    except ET.ParseError:
        pass
    return alarm.program_uri.split(":", 1)[0] or "(no source)"


def days_text(r: Recurrence) -> str:
    named = str(Recurrence.on(r.days))
    if not named.startswith("ON_"):
        return named.lower()
    return " ".join(_DAY[d] for d in _READING_ORDER if d in r.days)


def duration_text(duration: str) -> str:
    if not duration:
        return "no auto-stop"
    try:
        h, m, s = (int(x) for x in duration.split(":"))
    except ValueError:
        return duration
    secs = f"{s:02d}s" if s else ""
    if h:
        return f"{h}h{m:02d}m{secs}" if secs else f"{h}h{m:02d}"
    return f"{m}m{secs}"


def _start(alarm: Alarm) -> time | None:
    try:
        return time.fromisoformat(alarm.start_time)
    except ValueError:
        return None


def next_fire(alarm: Alarm, now: datetime) -> datetime | None:
    """When the alarm next goes off, in the household's local time; None if
    it is disabled or its time can't be read. A ONCE alarm fires at the next
    occurrence of its time."""
    at = _start(alarm)
    if not alarm.enabled or at is None:
        return None
    for ahead in range(8):
        day = now.date() + timedelta(days=ahead)
        when = datetime.combine(day, at)
        if when <= now:
            continue
        sonos_day = (day.weekday() + 1) % 7        # Python's Monday 0 -> Sonos's Sunday 0
        if alarm.recurrence.once or sonos_day in alarm.recurrence.days:
            return when
    return None


def clock_text(t: time, time_format: str) -> str:
    """A time the way the household writes them. Only INV (unset) has been
    seen on a real speaker; 12H is assumed, and anything else is 24-hour."""
    if time_format == "12H":
        return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"
    return f"{t.hour:02d}:{t.minute:02d}"


def _date_text(d: datetime, date_format: str) -> str:
    if date_format == "DMY":
        return f"{d.day:02d}/{d.month:02d}"
    if date_format == "MDY":
        return f"{d.month:02d}/{d.day:02d}"
    return f"{d.month:02d}-{d.day:02d}"


def fire_text(when: datetime | None, now: datetime, hh: clock.HouseholdTime) -> str:
    if when is None:
        return "-"
    days = (when.date() - now.date()).days
    day = ("today" if days == 0 else "tomorrow" if days == 1
           else f"{_DAY[(when.weekday() + 1) % 7]} {_date_text(when, hh.date_format)}")
    return f"{day} {clock_text(when.time(), hh.time_format)}"


def row(house: Household, alarm: Alarm, hh: clock.HouseholdTime) -> dict:
    """Everything `alarm list` shows about one alarm, JSON-ready."""
    when = next_fire(alarm, hh.local)
    at = _start(alarm)
    return {
        "id": alarm.id,
        **aimed_at(house, alarm.room_uuid),
        "room_uuid": alarm.room_uuid,
        "time": alarm.start_time,
        "time_text": clock_text(at, hh.time_format) if at else alarm.start_time,
        "recurrence": str(alarm.recurrence),
        "days": sorted(alarm.recurrence.days),
        "days_text": days_text(alarm.recurrence),
        "enabled": alarm.enabled,
        "volume": alarm.volume,
        "duration": alarm.duration,
        "duration_text": duration_text(alarm.duration),
        "play_mode": alarm.play_mode,
        "include_linked_zones": alarm.include_linked_zones,
        "source_title": source_title(alarm),
        "program_uri": alarm.program_uri,
        "next_fire": when.isoformat() if when else None,
        "next_fire_text": fire_text(when, hh.local, hh),
    }


def listing(house: Household, alarms: list[Alarm], hh: clock.HouseholdTime) -> list[dict]:
    """Rows grouped by room name, each room's alarms in time order."""
    rows = [row(house, a, hh) for a in alarms]
    return sorted(rows, key=lambda r: (r["room"].lower(), r["time"], r["id"] or ""))


_LABEL = {"bonded_follower": "set on {speaker}, a bonded follower",
          "vanished": "set on {speaker}, a speaker no longer in the household",
          "unknown": "set on {room_uuid}, not a speaker in this household"}


def human(rows: list[dict], hh: clock.HouseholdTime) -> str:
    if not rows:
        return "no alarms"
    lines, room = [], None
    for r in rows:
        if r["room"] != room:
            room = r["room"]
            lines.append(("" if not lines else "\n") + room)
        on = "on " if r["enabled"] else "off"
        lines.append(
            f"  {on} {r['time_text']:>8}  {r['days_text']:<18} vol {r['volume']:<3} "
            f"{r['duration_text']:<7} {r['play_mode']:<11} {r['source_title']}")
        if r["status"] != "ok":
            lines.append(f"      ! {_LABEL[r['status']].format(**r)}")
        if r["next_fire"]:
            lines.append(f"      next: {r['next_fire_text']}")
    lines.append(f"\nhousehold time {clock_text(hh.local.time(), hh.time_format)}"
                 f" {_date_text(hh.local, hh.date_format)}")
    return "\n".join(lines)


# ---- commands --------------------------------------------------------------

def cmd_list(args):
    try:
        house = _household(args)
    except Exception as exc:
        return fail(args, f"could not reach the household: {exc}",
                    "check you are on the same LAN, or pass --anchor <ip>")
    if not house.groups:
        return fail(args, "no speakers in the household")
    ip = house.groups[0].coordinator.ip
    try:
        found = clock.list_alarms(ip)
        hh = clock.household_time(ip)
    except Exception as exc:
        return fail(args, f"could not read the alarms from {ip}: {exc}")
    rows = listing(house, found.alarms, hh)
    return emit(args, {"version": found.version, "household_time": hh.to_dict(),
                       "alarms": rows}, human(rows, hh))


def register(sub, parents=None):
    kw = {"parents": parents} if parents else {}
    p = sub.add_parser(**kw, name="alarm", help="the household's Sonos alarms")
    asub = p.add_subparsers(dest="alarm_cmd", required=True, metavar="<command>")
    ls = asub.add_parser(**kw, name="list",
                         help="every alarm, grouped by room (read-only)")
    ls.add_argument("--anchor", default=None,
                    help="speaker IP to query instead of SSDP discovery")
    ls.set_defaults(func=cmd_list)
