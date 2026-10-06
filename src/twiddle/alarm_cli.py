"""`twiddle alarm` -- the household's Sonos alarms from the command line.

`alarm list` is read-only: one `ListAlarms` plus the household's clock and
display format, from any speaker (alarms are household-wide), and the
station catalog, to recognise station alarms. Where each alarm's sound comes
from is read from its `ProgramURI` (`alarms/soundsource.py`). Every alarm is
shown under its room's name, including one aimed at a bonded follower or at a
speaker that has vanished: labelled, never hidden. An alarm twiddle can't read
(`model.UnreadableAlarm`) is listed apart with why, and the rest still show;
`alarm status` reads the same way. Every other verb refuses while one is
unreadable, before writing anything: they need the whole list to be safe.

`alarm snapshot` is read-only too: it saves that `ListAlarms` to a file.
`alarm restore` WRITES: it creates, updates and destroys alarms until the
household matches the snapshot (`alarms/baseline.py`), journalled, and
`--dry-run` prints the plan without writing.

`alarm enable|disable <id>` WRITE: one `UpdateAlarm` that changes only
`Enabled`, sending every other field back exactly as `ListAlarms` gave it, so
a source twiddle doesn't recognise is untouched. `alarm rm <id>` WRITES: it
asks twice, then `DestroyAlarm`; the journal keeps the whole alarm, and
`clock.recreate` makes it again from there. Each takes `--dry-run`, and each
write is refused if the alarm list moved since it was read (`alarms/clock.py`).

`alarm add` WRITES one `CreateAlarm`; `alarm edit <id>` one `UpdateAlarm`
changing only the fields named on the command line. Between them every field
`AlarmClock` takes is settable: time, days, duration, volume, play mode,
include-grouped-rooms, room, on/off and source (`--source chime`,
`station:<key>`, or any other registered in `alarms/sources/`; on edit, by
default, the source as it is, byte-for-byte). A room is named, never
addressed: a bonded follower's alarm goes on its room's primary, and the
answer says so. Both take `--dry-run`.

`alarm status` is read-only: whether an alarm is going off (or snoozed) in
each room, from each group coordinator's `GetRunningAlarmProperties` and
AVTransport's `AlarmRunning`/`SnoozeRunning`. `alarm try <id>` WRITES: the
speaker's `RunAlarm`, now, on the alarm's room. `alarm stop|snooze --room`
WRITE: the group's own `Stop` / `SnoozeAlarm` (5, 10, 15 or 30 minutes,
default 10), refused when no alarm is going off there. Each takes
`--dry-run` and is journalled (`alarm_run`/`alarm_stop`/`alarm_snooze`).

`alarm sources [<name> [<query>]]` is read-only and offline (but for
`alarm sources spotify <words>` and `bandcamp <words>`, which search Spotify and
Bandcamp): every source `--source` takes, whether it needs this Mac at fire time and its fallback; or
one source's choices (`alarm sources station kexp`). `alarm list` marks an
alarm whose source needs this Mac with ⌁.

`alarm serve --dir DIR` runs in the foreground and serves DIR's audio files, and
Bandcamp tracks (a fresh stream URL per request), to the household's speakers
(or just `--room`'s) on a fixed port, for an alarm whose source needs this Mac (`alarms/server.py`). It writes to no speaker; each
file sent is a bounded span in the journal, and it closes the spans a crash of
its own left open. Anything it can't serve is refused at once. `--dry-run`
resolves everything and prints the plan without listening or journalling.
`alarm serve --install --dir DIR` keeps that server running as a launchd agent
(`daemon.ALARM_LABEL`), so a Mac-hosted alarm works with no terminal open;
`--uninstall` removes it and `--status` (read-only) says whether it answers on
its port. Install and uninstall take `--dry-run` and are journalled
(`alarm_serve_install`/`alarm_serve_uninstall`).

Follows the rest of the package: the `ok`/`error` envelope, `--json` anywhere.
"""
from __future__ import annotations

import argparse
import re
import signal
import sys
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path

from .alarms import baseline, clock, server, soundsource, sources
from .alarms.model import NAMED, Alarm, Recurrence, Unreadable, UnreadableAlarm
from . import play, report
from .control_cli import (SNAPSHOT_DIR, BadSpec, add_write_args, emit, fail,
                          parse_sleep_spec)
from .household import Ambiguous, Household, NotFound, Speaker

NEEDS_MAC = "⌁"
SNAPSHOT_FILE = SNAPSHOT_DIR / "alarms.json"
# Monday first for reading; Sonos numbers the days from Sunday = 0.
_READING_ORDER = (1, 2, 3, 4, 5, 6, 0)
_DAY = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}


def _household(args) -> Household:
    return Household.load(anchor=getattr(args, "anchor", None))


# ---- what an alarm is aimed at ---------------------------------------------

def _bond(house: Household, sp: Speaker) -> set[str]:
    """Every unit bonded with `sp`, itself included: the UUIDs of whichever
    ChannelMapSet or HTSatChanMapSet in the topology lists it (a surround's
    may be only on its soundbar's member). Empty when none does."""
    out: set[str] = set()
    for m in house.topology.members if house.topology else ():
        for raw in (m.chan_map, m.sat_chan_map):
            uuids = {pair.split(":", 1)[0] for pair in raw.split(";") if pair}
            if sp.uuid in uuids:
                out |= uuids
    return out


def _bonded_with(house: Household, sp: Speaker) -> list[Speaker]:
    """The visible units bonded with `sp` into one room (a stereo pair), `sp`
    among them: the units its map lists. With no map naming it in the
    topology but a channel given from one, the visible units of its room
    that were given one too. Two units that only share a room name are two
    rooms."""
    members = [m for m in house.group_of(sp).members if not m.invisible]
    bond = _bond(house, sp)
    if bond:
        return [m for m in members if m.uuid in bond]
    if sp.channel:
        return [m for m in members if m.room == sp.room and m.channel]
    return [sp]


def _is_bonded_follower(house: Household, sp: Speaker) -> bool:
    """A unit that is part of a room but not the room itself.

    Invisible units (surrounds, sub) always are. Of the visible units in one
    bond (a stereo pair) the primary is the group's coordinator when it is
    one of them, else the first listed in the bond's ChannelMapSet -- which in
    the author's household is also the coordinator, the left Roam; the
    fallback is inferred, not observed.
    """
    if sp.invisible:
        return True
    mates = _bonded_with(house, sp)
    if len(mates) < 2:
        return False
    group = house.group_of(sp)
    if group.coordinator in mates:
        return sp is not group.coordinator
    member = house.topology.by_uuid(sp.uuid) if house.topology else None
    first = (member.chan_map.split(":", 1)[0] if member and member.chan_map else "")
    return bool(first) and sp.uuid != first


def room_primaries(house: Household, sp: Speaker) -> list[Speaker]:
    """The unit that is `sp`'s room: `sp` itself unless it is a bonded
    follower, else the visible unit of its bond that isn't one. A satellite
    no map names falls back to its room's visible units, which can be none
    or several: the caller refuses either rather than guess."""
    if not _is_bonded_follower(house, sp):
        return [sp]
    members = house.group_of(sp).members
    bond = _bond(house, sp)
    if bond:
        units = [m for m in members if m.uuid in bond and not m.invisible]
    elif sp.invisible:
        units = [m for m in members if not m.invisible and m.room == sp.room]
    else:
        units = _bonded_with(house, sp)
    return [m for m in units if not _is_bonded_follower(house, m)]


def room_target(house: Household, query: str) -> dict:
    """Where an alarm for the room named `query` goes: the RoomUUID of the
    room's primary unit, never a bonded follower, and never the group
    coordinator (an alarm belongs to its room, not to whatever the room is
    grouped with today). Raises the household's NotFound/Ambiguous.

    A name that matches more than one room is ambiguous even when those
    rooms are grouped (`resolve` would happily pick one: a group shares
    transport, not alarms). Naming a follower alone (its own ZoneName, or
    its IP) is redirected and says so; naming the room is not.
    """
    hits = house.matches(query)
    rooms: dict[str, Speaker] = {}
    for sp in hits:
        primaries = room_primaries(house, sp)
        if not primaries:
            raise NotFound(query, house.names)
        if len(primaries) > 1:
            raise Ambiguous(query, sorted(p.label for p in primaries))
        rooms.setdefault(primaries[0].uuid, primaries[0])
    if len(rooms) > 1:
        names = sorted(p.room or p.name for p in rooms.values())
        if len(set(names)) < len(names):        # two rooms of one name: tell them apart
            names = sorted(p.label for p in rooms.values())
        raise Ambiguous(query, names)
    [primary] = rooms.values()
    out = {"requested": query, "room": primary.room or primary.name,
           "speaker": primary.name, "room_uuid": primary.uuid}
    if primary not in hits:
        out |= {"redirected_from": hits[0].label,
                "reason": f"{hits[0].label} is a bonded follower; the alarm goes on "
                          f"{primary.label}, its room's primary"}
    return out


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
    """The alarm's own title for its source: what the source that built it
    says, else the DIDL's dc:title, or failing both the URI's scheme. The
    model never parses the metadata; this does, for display only."""
    uri, metadata = alarm.program_uri, alarm.program_metadata
    source = sources.recognise(uri, metadata)
    return ((source and source.describe(uri, metadata)) or sources.didl_title(metadata)
            or uri.split(":", 1)[0] or "(no source)")


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
    secs = f":{t.second:02d}" if t.second else ""
    if time_format == "12H":
        return f"{t.hour % 12 or 12}:{t.minute:02d}{secs} {'AM' if t.hour < 12 else 'PM'}"
    return f"{t.hour:02d}:{t.minute:02d}{secs}"


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
    source = sources.recognise(alarm.program_uri, alarm.program_metadata)
    sound = soundsource.classify(alarm.program_uri, alarm.program_metadata)
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
        "source": source.name if source else None,
        "needs_mac": bool(source and source.needs_mac),
        "sound_source": sound.key,
        "sound_source_text": sound.text,
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


def unreadable_rows(house: Household, bad: list[Unreadable]) -> list[dict]:
    """The alarms that couldn't be read, JSON-ready: ID, room when the alarm
    names one, and why."""
    out = []
    for u in bad:
        where = aimed_at(house, u.room_uuid) if u.room_uuid else {"room": None, "status": None}
        out.append({"id": u.id, **where, "room_uuid": u.room_uuid, "reason": u.reason})
    return out


def unreadable_text(rows: list[dict]) -> str:
    many = len(rows) != 1
    lines = [f"\ncan't read {len(rows)} alarm{'s' if many else ''}"
             f" (fix or delete {'them' if many else 'it'} in the Sonos app):"]
    for r in rows:
        name = f"#{r['id']}" if r["id"] else "an alarm with no ID"
        lines.append(f"  {name:>4} {r['room'] or 'no room'}: {r['reason']}")
    return "\n".join(lines)


def human(rows: list[dict], hh: clock.HouseholdTime, bad: list[dict] = ()) -> str:
    if not rows:
        return "no alarms" + (f"\n{unreadable_text(bad)}" if bad else "")
    lines, room = [], None
    sound_width = max(len(r["sound_source_text"]) for r in rows)
    for r in rows:
        if r["room"] != room:
            room = r["room"]
            lines.append(("" if not lines else "\n") + room)
        on = "on " if r["enabled"] else "off"
        lines.append(
            f"  {'#' + r['id']:>4} {on} {r['time_text']:>8}  {r['days_text']:<18} vol {r['volume']:<3} "
            f"{r['duration_text']:<7} {r['play_mode']:<11} {r['sound_source_text']:<{sound_width}}  "
            f"{NEEDS_MAC + ' ' if r['needs_mac'] else ''}{r['source_title']}")
        if r["status"] != "ok":
            lines.append(f"      ! {_LABEL[r['status']].format(**r)}")
        if r["next_fire"]:
            lines.append(f"      next: {r['next_fire_text']}")
    if any(r["needs_mac"] for r in rows):
        lines.append(f"\n{NEEDS_MAC} needs this Mac when it goes off")
    if bad:
        lines.append(unreadable_text(bad))
    lines.append(f"\nhousehold time {clock_text(hh.local.time(), hh.time_format)}"
                 f" {_date_text(hh.local, hh.date_format)}")
    return "\n".join(lines)


def brief(house: Household, alarm: Alarm) -> str:
    """One alarm in a line: where, when, which days, what."""
    on = "on" if alarm.enabled else "off"
    return (f"{aimed_at(house, alarm.room_uuid)['room']} {alarm.start_time} "
            f"{days_text(alarm.recurrence)} ({on}, vol {alarm.volume}) {source_title(alarm)}")


def change_text(house: Household, c: baseline.Change) -> str:
    if c.op == "create":
        return f"create  {brief(house, c.want)}  (was alarm {c.want.id})"
    if c.op == "destroy":
        return f"destroy alarm {c.have.id}: {brief(house, c.have)}"
    return f"update  alarm {c.have.id}: {brief(house, c.want)}  ({', '.join(c.fields)})"


# ---- what `add` and `edit` set ---------------------------------------------

# The CLI's names for the speaker's play modes, from the same table `shuffle`
# and `repeat` use: SHUFFLE is shuffle *and* repeat-all, SHUFFLE_NOREPEAT the
# plain shuffle.
_MODE_NAME = {(False, "off"): "normal", (False, "all"): "repeat",
              (False, "one"): "repeat-one", (True, "off"): "shuffle",
              (True, "all"): "shuffle-repeat", (True, "one"): "shuffle-repeat-one"}
PLAY_MODES = {name: play.encode_play_mode(*k) for k, name in _MODE_NAME.items()}
_DAY_NAMES = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
DAYS_HELP = ("once, daily, weekdays, weekends, days like mon,wed,fri or mon-fri, "
             "or the speaker's own ON_<days> (Sunday 0)")


def parse_time(text: str) -> str:
    """"7:15", "07:15" or "07:15:30" -> the speaker's "HH:MM:SS"."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59 or int(m[3] or 0) > 59:
        raise ValueError(f"can't read {text!r} as a time: 24-hour HH:MM or HH:MM:SS")
    return f"{int(m[1]):02d}:{m[2]}:{m[3] or '00'}"


def _day(token: str) -> int:
    hits = [i for i, name in enumerate(_DAY_NAMES) if len(token) >= 2 and name.startswith(token)]
    if len(hits) != 1:
        raise ValueError(f"can't read {token!r} as a day")
    return hits[0]


def parse_days(text: str) -> Recurrence:
    """A recurrence in the speaker's own spelling (`Recurrence.on`), so
    `sat,sun` is WEEKENDS. Day ranges run forward through the week."""
    low = text.strip().lower()
    if low.upper() in NAMED:
        return Recurrence.parse(low.upper())
    if low.startswith("on_"):
        return Recurrence.on(Recurrence.parse(low.upper()).days)
    days: set[int] = set()
    for token in filter(None, re.split(r"[,\s]+", low)):
        first, _, last = token.partition("-")
        d, end = _day(first), _day(last) if last else _day(first)
        days.add(d)
        while d != end:
            d = (d + 1) % 7
            days.add(d)
    if not days:
        raise ValueError(f"no days in {text!r}: {DAYS_HELP}")
    return Recurrence.on(days)


def parse_duration(text: str) -> str:
    """The auto-stop, "HH:MM:SS": "1h", "30m", "1h30", "1:30", "01:30:00";
    `none` (or 0) is no auto-stop, an empty Duration as soco sends it."""
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text.strip()):
        h, m, s = (int(x) for x in text.split(":"))
        if m > 59 or s > 59 or not 0 < (h * 60 + m) * 60 + s < 24 * 3600:
            raise ValueError(f"can't read {text!r} as a duration: more than nothing, "
                             "less than a day (`none` is no auto-stop)")
        return text.strip()
    try:
        secs = parse_sleep_spec(text)
    except BadSpec as exc:
        raise ValueError(f"{exc}: 1h, 30m, 1h30, HH:MM:SS, or none") from None
    return "" if secs == 0 else f"{secs // 3600:02d}:{secs // 60 % 60:02d}:00"


def parse_volume(text: str) -> int:
    if not re.fullmatch(r"\d{1,3}", text.strip()) or int(text) > 100:
        raise ValueError(f"volume {text!r} is not 0-100")
    return int(text)


def parse_mode(text: str) -> str:
    low = text.strip().lower()
    if low in PLAY_MODES:
        return PLAY_MODES[low]
    if text.strip().upper() in PLAY_MODES.values():
        return text.strip().upper()
    raise ValueError(f"unknown play mode {text!r}: {', '.join(PLAY_MODES)}")


def settings(args) -> dict:
    """The Alarm fields named on the command line, parsed; ValueError names
    the one that can't be read. A field not named is not in the result, so
    an edit leaves it, the source included, exactly as it was."""
    out: dict = {}
    for flag, name, parse in (("time", "start_time", parse_time),
                              ("days", "recurrence", parse_days),
                              ("duration", "duration", parse_duration),
                              ("volume", "volume", parse_volume),
                              ("mode", "play_mode", parse_mode)):
        if getattr(args, flag, None) is not None:
            out[name] = parse(getattr(args, flag))
    if getattr(args, "enabled", None) is not None:
        out["enabled"] = args.enabled
    if getattr(args, "include_grouped_rooms", None) is not None:
        out["include_linked_zones"] = args.include_grouped_rooms
    spec = getattr(args, "source", None)
    editing = getattr(args, "alarm_id", None) is not None
    if spec is not None and not (editing and spec.strip().lower() == sources.KEEP):
        _, out["program_uri"], out["program_metadata"] = sources.build(
            spec, getattr(args, "anchor", None))
    return out


def details(alarm: Alarm) -> str:
    """What `brief` leaves out: the auto-stop, play mode and grouped rooms."""
    stop = (f"stops after {duration_text(alarm.duration)}" if alarm.duration
            else "no auto-stop")
    grouped = "includes grouped rooms" if alarm.include_linked_zones else "this room only"
    return f"{stop}, play mode {_mode_name(alarm.play_mode)}, {grouped}"


def _mode_name(value: str) -> str:
    return next((n for n, v in PLAY_MODES.items() if v == value), value)


# ---- commands --------------------------------------------------------------

def _anchor(args):
    """The household and one speaker to ask, or (None, None, exit code)."""
    try:
        house = _household(args)
    except Exception as exc:
        return None, None, fail(args, f"could not reach the household: {exc}",
                                "check you are on the same LAN, or pass --anchor <ip>")
    if not house.groups:
        return None, None, fail(args, "no speakers in the household")
    return house, house.groups[0].coordinator.ip, None


def _read_failed(args, ip: str, exc: Exception, doing: str) -> int:
    """A strict read, before anything was written, that raised."""
    if isinstance(exc, UnreadableAlarm):
        name = f"alarm {exc.alarm_id}" if exc.alarm_id else "an alarm with no ID"
        return fail(args, f"twiddle can't read {name} ({exc.reason}), so it won't "
                          f"{doing}: nothing written",
                    "fix or delete it in the Sonos app; `twiddle alarm list` shows it",
                    unreadable=[{"id": exc.alarm_id, "room_uuid": exc.attributes.get("RoomUUID"),
                                 "reason": exc.reason}])
    return fail(args, f"could not read the alarms from {ip}: {exc}")


def cmd_list(args):
    house, ip, err = _anchor(args)
    if err is not None:
        return err
    try:
        found = clock.list_alarms(ip, tolerant=True)
        hh = clock.household_time(ip)
    except Exception as exc:
        return fail(args, f"could not read the alarms from {ip}: {exc}")
    rows = listing(house, found.alarms, hh)
    bad = unreadable_rows(house, found.unreadable)
    return emit(args, {"version": found.version, "household_time": hh.to_dict(),
                       "alarms": rows, "unreadable": bad}, human(rows, hh, bad))


def cmd_snapshot(args):
    """Save every alarm, exactly as ListAlarms gave it. Read-only."""
    house, ip, err = _anchor(args)
    if err is not None:
        return err
    try:
        found = clock.list_alarms(ip)
    except Exception as exc:
        return _read_failed(args, ip, exc, "take a snapshot")
    snap = baseline.AlarmSnapshot.of(found)
    path = snap.save(Path(args.out) if args.out else SNAPSHOT_FILE)
    return emit(args, {"path": str(path), "snapshot": snap.to_dict(),
                       "alarms": len(found.alarms)},
                f"{len(found.alarms)} alarms (list version {found.version}) -> {path}")


def cmd_restore(args):
    """Make the household's alarms match a snapshot again."""
    path = Path(args.path) if args.path else SNAPSHOT_FILE
    if not path.exists():
        return fail(args, f"no alarm snapshot at {path}",
                    "take one first with `twiddle alarm snapshot`")
    try:
        snap = baseline.AlarmSnapshot.load(path)
        want = snap.alarms
    except UnreadableAlarm as exc:
        return _read_failed(args, str(path), exc, "restore that snapshot")
    except Exception as exc:
        return fail(args, f"could not read the snapshot {path}: {exc}")
    house, ip, err = _anchor(args)
    if err is not None:
        return err
    try:
        found = clock.list_alarms(ip)
    except Exception as exc:
        return _read_failed(args, ip, exc, "restore")
    changes = baseline.plan(want, found.alarms)
    lines = [change_text(house, c) for c in changes]
    head = f"alarms as at {snap.taken_utc} ({path})"
    if getattr(args, "dry_run", False) or not changes:
        notes = baseline.leftovers(want, found.alarms)[1]
        if not changes:
            human = f"nothing to do: the alarms already match {head}"
        else:
            human = f"[dry-run] would restore {head}:\n  " + "\n  ".join(lines)
        human += "".join(f"\n  note: {n}" for n in notes)
        return emit(args, {"would": [c.to_dict() for c in changes], "performed": False,
                           "notes": notes, "version": found.version,
                           "taken_utc": snap.taken_utc}, human)
    out = baseline.restore(ip, snap, found)
    payload = {"ok": out.ok, "done": out.done, "id_map": out.id_map,
               "left": [c.to_dict() for c in out.left], "notes": out.notes,
               "error": out.error or None, "version": out.version,
               "taken_utc": snap.taken_utc}
    human = [f"restored {head}" if out.ok else f"restore of {head} is INCOMPLETE"]
    human += [f"  {line}" for line in lines[:len(out.done)]]
    human += [f"  alarm {old} is now alarm {new}" for old, new in out.id_map.items()]
    if out.error:
        human.append(f"  stopped: {out.error}")
    human += [f"  STILL DIFFERENT: {change_text(house, c)}" for c in out.left]
    human += [f"  note: {n}" for n in out.notes]
    emit(args, payload, "\n".join(human))
    return 0 if out.ok else 1


_VERB = {"rm": "delete", "try": "fire"}


def _target(args):
    """The household, a speaker to ask, the alarm list and the alarm named by
    `args.alarm_id`; or an exit code last."""
    house, ip, err = _anchor(args)
    if err is not None:
        return None, None, None, None, err
    try:
        found = clock.list_alarms(ip)
    except Exception as exc:
        return None, None, None, None, _read_failed(
            args, ip, exc, f"{_VERB.get(args.alarm_cmd, args.alarm_cmd)} alarm {args.alarm_id}")
    alarm = found.get(args.alarm_id)
    if alarm is None:
        return None, None, None, None, fail(
            args, f"no alarm {args.alarm_id}", "`twiddle alarm list` shows every alarm's ID")
    return house, ip, found, alarm, None


def _about(house: Household, alarm: Alarm, found: clock.AlarmList) -> dict:
    return {"id": alarm.id, "room": aimed_at(house, alarm.room_uuid)["room"],
            "alarm": alarm.to_attributes(), "version": found.version}


def _write_failed(args, doing: str, about: dict, exc: Exception, *, done: str,
                  update: bool, created: bool = False) -> int:
    """A write that raised: say whether it happened, as far as can be told.

    One that landed (another alarm moved in the same moment, or the list
    couldn't be read back) is reported done, `done` saying so, with why it
    raised as a warning. The version is the list's after the write, null if
    it couldn't be read. For an update, `alarm` becomes the alarm as read
    back (null when it couldn't be) and `before` the one written over; for a
    create, likewise but with no `before`, and `id` the one the speaker
    assigned; for a delete, `alarm` stays the alarm deleted.
    """
    landed = getattr(exc, "landed", False)
    if landed is True:
        now = {"version": exc.current.version if exc.current else None}
        if created:
            now |= {"id": exc.alarm_id,
                    "alarm": exc.alarm.to_attributes() if exc.alarm else None}
        elif update:
            now |= {"before": about["alarm"],
                    "alarm": exc.alarm.to_attributes() if exc.alarm else None}
        return emit(args, about | now | {"performed": True, "warning": str(exc)},
                    f"{done}\n  warning: {exc}")
    if landed is None:
        hint = "it may have happened anyway: check `twiddle alarm list`"
    elif isinstance(exc, clock.VersionChanged) and not landed:
        hint = "someone changed an alarm meanwhile (the Sonos app?); check `twiddle alarm list`"
    else:
        hint = "check `twiddle alarm list`"
    return fail(args, f"could not {doing}: {exc}", hint, written=landed)


def _done(house: Household, exc: Exception, did: str) -> str:
    """A write that landed although it raised, as far as the list read back shows."""
    seen = getattr(exc, "alarm", None)
    if seen is not None:
        return f"{did}: {brief(house, seen)}"
    if getattr(exc, "current", None) is None:
        return f"{did} (the list couldn't be read back to show it)"
    return f"{did}, but it isn't in the list read back"


def _set_enabled(args, on: bool) -> int:
    house, ip, found, alarm, err = _target(args)
    if err is not None:
        return err
    verb = "enable" if on else "disable"
    about = _about(house, alarm, found)
    if alarm.enabled == on:
        return emit(args, about | {"performed": False},
                    f"alarm {alarm.id} is already {'on' if on else 'off'}: {brief(house, alarm)}")
    if getattr(args, "dry_run", False):
        return emit(args, about | {"would": verb, "performed": False},
                    f"[dry-run] would {verb} alarm {alarm.id}: {brief(house, alarm)}")
    try:
        after, now = clock.update_alarm(ip, replace(alarm, enabled=on), found.version)
    except Exception as exc:
        return _write_failed(args, f"{verb} alarm {alarm.id}", about, exc,
                             done=_done(house, exc, f"{verb}d alarm {alarm.id}"),
                             update=True)
    return emit(args, about | {"performed": True, "alarm": after.to_attributes(),
                               "version": now.version},
                f"{verb}d alarm {alarm.id}: {brief(house, after)}")


def cmd_enable(args):
    return _set_enabled(args, True)


def cmd_disable(args):
    return _set_enabled(args, False)


def _ask(prompt: str) -> str:
    """One answer from the terminal. The prompt goes to stderr, so stdout stays
    one `--json` envelope; end of input counts as no."""
    print(prompt, end="", file=sys.stderr, flush=True)
    try:
        return input().strip()
    except (EOFError, OSError):
        return ""


def _confirmed(alarm: Alarm, what: str) -> bool:
    """Asked twice, differently: a y, then the alarm's ID typed out. Only a
    person at a terminal can answer: piped answers are never asked for."""
    if not sys.stdin.isatty():
        return False
    if _ask(f"delete alarm {alarm.id}: {what}? [y/N] ").lower() not in ("y", "yes"):
        return False
    return _ask(f"type its ID ({alarm.id}) to delete it: ") == alarm.id


def cmd_rm(args):
    """Delete one alarm, after asking twice. The journal keeps all of it."""
    house, ip, found, alarm, err = _target(args)
    if err is not None:
        return err
    what = brief(house, alarm)
    about = _about(house, alarm, found)
    if getattr(args, "dry_run", False):
        return emit(args, about | {"would": "delete", "performed": False},
                    f"[dry-run] would delete alarm {alarm.id}: {what}")
    if not _confirmed(alarm, what):
        hint = ("" if sys.stdin.isatty()
                else "`alarm rm` asks twice, so it needs a terminal; --dry-run shows what it would do")
        return fail(args, f"not deleted: alarm {alarm.id}", hint, **about)
    try:
        now = clock.destroy_alarm(ip, alarm.id, found.version)
    except Exception as exc:
        return _write_failed(args, f"delete alarm {alarm.id}", about, exc,
                             done=f"deleted alarm {alarm.id}: {what}", update=False)
    return emit(args, about | {"performed": True, "version": now.version},
                f"deleted alarm {alarm.id}: {what}\n"
                f"  journalled whole (alarm_destroy in {play.INTERVENTION_LOG}), "
                "so it can be recreated")


def _settings_or_fail(args):
    """`settings(args)`, or (None, an exit code) when a value can't be read."""
    try:
        return settings(args), None
    except ValueError as exc:
        return None, fail(args, str(exc), "`twiddle alarm add --help` lists every field")


def _room_or_fail(args, house: Household):
    """`room_target` for `args.room`, or (None, an exit code)."""
    try:
        return room_target(house, args.room), None
    except Ambiguous as exc:
        return None, fail(args, str(exc), "be more specific, or name the speaker by its IP",
                          candidates=exc.candidates)
    except NotFound as exc:
        return None, fail(args, str(exc), "run `twiddle rooms` to list targets",
                          known=exc.known)


def _note(target: dict | None) -> str:
    return f"\n  note: {target['reason']}" if target and "reason" in target else ""


def _aim(target: dict | None) -> dict:
    """What the payload says about naming the room: asked for, and redirected."""
    if not target:
        return {}
    return {k: target[k] for k in ("requested", "redirected_from", "reason") if k in target}


def cmd_add(args):
    """Create one alarm: the fields given, the model's defaults for the rest."""
    found_set, err = _settings_or_fail(args)
    if err is not None:
        return err
    house, ip, err = _anchor(args)
    if err is not None:
        return err
    target, err = _room_or_fail(args, house)
    if err is not None:
        return err
    alarm = replace(Alarm(start_time="", recurrence=Recurrence.parse("DAILY"),
                          room_uuid=target["room_uuid"]), **found_set)
    about = {"room": target["room"], "alarm": alarm.to_attributes(), **_aim(target)}
    what = f"{brief(house, alarm)}\n  {details(alarm)}{_note(target)}"
    # Read before the dry run too, so it refuses whatever the real one would.
    try:
        found = clock.list_alarms(ip)
    except Exception as exc:
        return _read_failed(args, ip, exc, "add an alarm")
    if getattr(args, "dry_run", False):
        return emit(args, about | {"would": "create", "performed": False},
                    f"[dry-run] would create an alarm: {what}")
    try:
        after, now = clock.create_alarm(ip, alarm, found.version)
    except Exception as exc:
        did = f"created alarm {exc.alarm_id}" if getattr(exc, "alarm_id", None) else \
            "created an alarm"
        return _write_failed(args, "create the alarm", about, exc, update=True, created=True,
                             done=_done(house, exc, did) + _note(target))
    return emit(args, about | {"performed": True, "id": after.id,
                               "alarm": after.to_attributes(), "version": now.version},
                f"created alarm {after.id}: {brief(house, after)}\n  {details(after)}"
                f"{_note(target)}")


def cmd_edit(args):
    """Change only the fields given; everything else, the source included
    unless `--source` names one, goes back exactly as ListAlarms gave it."""
    changes, err = _settings_or_fail(args)
    if err is not None:
        return err
    if not changes and args.room is None:
        return fail(args, "nothing to change",
                    "give at least one of --time, --days, --duration, --volume, --mode, "
                    "--include-grouped-rooms, --room, --on/--off, --source")
    house, ip, found, alarm, err = _target(args)
    if err is not None:
        return err
    target = None
    if args.room is not None:
        target, err = _room_or_fail(args, house)
        if err is not None:
            return err
        changes["room_uuid"] = target["room_uuid"]
    want = replace(alarm, **changes)
    fields = baseline.differences(want, alarm)
    about = _about(house, alarm, found) | _aim(target)
    if not fields:
        return emit(args, about | {"performed": False, "fields": []},
                    f"alarm {alarm.id} is already so: {brief(house, alarm)}{_note(target)}")
    if target:
        about["to_room"] = target["room"]

    def what(a: Alarm) -> str:
        return (f"alarm {alarm.id}: {brief(house, a)}  ({', '.join(fields)})\n"
                f"  {details(a)}{_note(target)}")
    if getattr(args, "dry_run", False):
        return emit(args, about | {"would": "update", "performed": False, "fields": fields,
                                   "would_be": want.to_attributes()},
                    f"[dry-run] would update {what(want)}")
    try:
        after, now = clock.update_alarm(ip, want, found.version)
    except Exception as exc:
        return _write_failed(args, f"update alarm {alarm.id}", about | {"fields": fields},
                             exc, update=True,
                             done=_done(house, exc, f"updated alarm {alarm.id}")
                             + f"  ({', '.join(fields)}){_note(target)}")
    return emit(args, about | {"performed": True, "fields": fields, "before": about["alarm"],
                               "alarm": after.to_attributes(), "version": now.version},
                f"updated {what(after)}")


def cmd_sources(args):
    """Every registered source, or one source's choices. Reads the registry
    and the files behind it; asks no speaker."""
    if args.name is None:
        rows = [{"name": s.name, "title": s.title, "needs_mac": s.needs_mac,
                 "takes_choice": s.takes_choice, "fallback": s.fallback}
                for s in sources.all_sources()]
        lines = [f"  {(r['name'] + ':<key>') if r['takes_choice'] else r['name']:<18} "
                 f"{NEEDS_MAC if r['needs_mac'] else ' '} {r['title']}"
                 f"\n{'':<23}if it can't play: {r['fallback']}" for r in rows]
        mark = any(r["needs_mac"] for r in rows)
        return emit(args, {"sources": rows},
                    "--source takes:\n" + "\n".join(lines)
                    + (f"\n\n{NEEDS_MAC} needs this Mac when the alarm goes off" if mark else ""))
    try:
        source = sources.get(args.name)
    except ValueError as exc:
        return fail(args, str(exc))
    try:
        found = source.choices(args.query)
    except ValueError as exc:
        return fail(args, str(exc))
    rows = [{"key": c.key, "title": c.title, "detail": c.detail} for c in found]
    head = f"{source.title}{' ' + NEEDS_MAC if source.needs_mac else ''}"
    if not source.takes_choice:
        text = f"{head}: --source {source.name} (no choice to make)"
    elif not rows:
        text = f"{head}: nothing matches {args.query!r}" if args.query else f"{head}: nothing"
    else:
        width = max(len(r["key"]) for r in rows)
        text = f"{head}, --source {source.name}:<key>\n" + "\n".join(
            f"  {r['key']:<{width}}  {r['title']}" + (f" -- {r['detail']}" if r["detail"] else "")
            for r in rows)
    return emit(args, {"source": source.name, "needs_mac": source.needs_mac,
                       "query": args.query, "choices": rows}, text)


# ---- ringing: status, try, stop, snooze ----------------------------------------

def ringing_row(house: Household, group, running: clock.Running | None,
                alarms: clock.AlarmList | None) -> dict:
    """One group's alarm state: the room from the alarm's own RoomUUID when the
    speaker names the alarm, else the group's."""
    row = {"group": group.name, "speaker": group.coordinator.label,
           "ringing": False, "snoozed": False}
    if running is None:
        return row
    alarm = alarms.get(running.alarm_id) if alarms and running.alarm_id else None
    row |= running.to_dict() | {"ringing": not running.snoozed, "snoozed": running.snoozed}
    row["room"] = aimed_at(house, alarm.room_uuid)["room"] if alarm else group.name
    if alarm is not None:
        row |= {"alarm": alarm.to_attributes(), "what": brief(house, alarm)}
    return row


def ringing_text(row: dict) -> str:
    if not (row["ringing"] or row["snoozed"]):
        return f"{row['group']}: nothing ringing"
    state = "snoozed" if row["snoozed"] else "RINGING"
    what = f"alarm {row['alarm_id']}" if row.get("alarm_id") else "an alarm"
    if row.get("what"):
        what += f": {row['what']}"
    since = f", since {row['logged_start']}" if row.get("logged_start") else ""
    return f"{row['room']}: {state} {what}{since}"


def _groups_or_fail(args, house: Household):
    """The groups `--room` names (one), or every group; or (None, exit code)."""
    if not getattr(args, "room", None):
        return house.groups, None
    try:
        return [house.resolve(args.room).group], None
    except Ambiguous as exc:
        return None, fail(args, str(exc), "be more specific", candidates=exc.candidates)
    except NotFound as exc:
        return None, fail(args, str(exc), "run `twiddle rooms` to list targets",
                          known=exc.known)


def cmd_status(args):
    """Is an alarm going off anywhere? Read-only: GetRunningAlarmProperties on
    each group's coordinator, LastChange where one is, ListAlarms to name it."""
    house, ip, err = _anchor(args)
    if err is not None:
        return err
    groups, err = _groups_or_fail(args, house)
    if err is not None:
        return err
    try:
        running = [(g, clock.alarm_now(g.coordinator.ip)) for g in groups]
        alarms = clock.list_alarms(ip, tolerant=True) if any(r for _, r in running) else None
    except Exception as exc:
        return fail(args, f"could not read whether an alarm is ringing: {exc}")
    rows = [ringing_row(house, g, r, alarms) for g, r in running]
    return emit(args, {"rooms": rows, "ringing": [r["room"] for r in rows if r["ringing"]]},
                "\n".join(ringing_text(r) for r in rows))


def cmd_try(args):
    """Fire an alarm now, through the speaker's own RunAlarm."""
    house, ip, found, alarm, err = _target(args)
    if err is not None:
        return err
    about = _about(house, alarm, found)
    aim = aimed_at(house, alarm.room_uuid)
    if aim["status"] in ("vanished", "unknown"):
        return fail(args, f"alarm {alarm.id} is aimed at a speaker that isn't here "
                          f"({aim['room']})", "`twiddle alarm edit --room` can move it", **about)
    sp = next(s for s in house.speakers if s.uuid == alarm.room_uuid)
    coordinator = house.group_of(sp).coordinator
    about["acted_on"] = coordinator.label
    what = f"alarm {alarm.id} on {coordinator.label}: {brief(house, alarm)}"
    if getattr(args, "dry_run", False):
        return emit(args, about | {"would": "run", "performed": False,
                                   "sent": dict(clock.run_args(alarm, "<household time>"))},
                    f"[dry-run] would fire {what}\n  {details(alarm)}")
    try:
        logged = clock.household_time(ip).local.strftime("%Y-%m-%d %H:%M:%S")
        stops = clock.run_alarm(coordinator.ip, alarm, logged)
    except Exception as exc:
        return fail(args, f"could not fire alarm {alarm.id}: {exc}",
                    "`twiddle alarm status` shows whether it went off", **about)
    after = f"\n  it stops itself at {stops.astimezone():%H:%M} " \
            f"(duration {duration_text(alarm.duration)})" if stops else ""
    return emit(args, about | {"performed": True, "logged_start": logged,
                               "stops_utc": stops.isoformat() if stops else None},
                f"fired {what}{after}\n  stop it with "
                f"`twiddle alarm stop --room \"{about['room']}\"`")


def _agent_journal(action: str, **extra) -> None:
    play._journal(action, "", **extra)


def _serve_status(args) -> int:
    """Is the launchd agent up and answering? Read-only: a local connect to
    the port, nothing sent, no span opened, no speaker touched."""
    from . import daemon
    project = Path(__file__).resolve().parents[2]
    job = daemon.status(daemon.ALARM_LABEL)
    running = "state = running" in job
    # Bare `--status` asks about what is installed, not the defaults.
    port, bind = args.port, args.host
    installed = daemon.installed_alarm_endpoint()
    if installed:
        port = installed[0] if port is None else port
        bind = installed[1] if bind is None else bind
    port = server.PORT if port is None else port
    bind = "0.0.0.0" if bind is None else bind
    args.port = port
    host = daemon.probe_host(bind)
    answers = daemon.serving(port, host)
    # Alive is the agent's own server answering: some other listener on the
    # port, with the job not running, is a collision, not a live server.
    alive = running and answers
    loaded = job != "not loaded"
    blocked = loaded and daemon.alarm_blocked_by_local_network(project, args.port, host)
    about = {"job": job, "plist": str(daemon.plist_path(daemon.ALARM_LABEL)),
             "port": args.port, "host": host, "alive": alive, "stale": not alive,
             "job_running": running, "port_answers": answers,
             "blocked_by_local_network": blocked}
    why = ("" if alive else
           " (something else answers on that port: the job isn't running)" if answers else
           " (the job is not running)" if not running else " (the port does not answer)")
    human = (f"launchd job {daemon.ALARM_LABEL}: {job}\n"
             f"plist: {about['plist']}\n"
             f"server: {'alive' if alive else 'STALE'}{why}")
    if blocked:
        human += "\n" + daemon.ALARM_LOCAL_NETWORK_HINT
    emit(args, about, human)
    return 0 if alive else 1


def _serve_agent(args) -> int:
    """`--install` / `--uninstall`: the launchd agent around `alarm serve`."""
    from . import daemon
    project = Path(__file__).resolve().parents[2]
    path = daemon.plist_path(daemon.ALARM_LABEL)
    dry = getattr(args, "dry_run", False)
    if args.uninstall:
        about = {"plist": str(path), "installed": path.exists()}
        if dry:
            return emit(args, about | {"would": "uninstall", "performed": False},
                        f"[dry-run] would remove {path}")
        _agent_journal("alarm_serve_uninstall", plist=str(path))
        removed = daemon.uninstall(daemon.ALARM_LABEL)
        return emit(args, about | {"performed": True, "removed": removed},
                    "Removed." if removed else "Was not installed.")
    if not 1 <= args.port <= 65535:
        return fail(args, f"port {args.port} can't be installed",
                    "an alarm stores the URL, so the port must be fixed: 1-65535")
    if not args.dir:
        return fail(args, "--install needs --dir", "`--dir` names the folder to serve")
    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        return fail(args, f"{root} is not a directory", "`--dir` names the folder to serve")
    house, first, err = _anchor(args)
    if err is not None:
        return err
    # Pinned into the plist: a mains-powered speaker, since a Roam asleep at
    # 3am would leave the restarted agent unable to find the household.
    ip = args.anchor or daemon.pick_anchor() or first
    # Names are checked now so a typo fails here, not in a launchd restart loop.
    for query in args.room or []:
        try:
            house.resolve(query)
        except Ambiguous as exc:
            return fail(args, str(exc), "be more specific", candidates=exc.candidates)
        except NotFound as exc:
            return fail(args, str(exc), "run `twiddle rooms` to list targets",
                        known=exc.known)
    about = {"plist": str(path), "dir": str(root), "port": args.port,
             "max_s": args.max_s, "host": args.host, "rooms": args.room or [], "anchor": ip}
    if dry:
        return emit(args, about | {"would": "install", "performed": False},
                    f"[dry-run] would install {path}: serve {root} on port {args.port}")
    # Journalled before the plist is written or the old job booted out: a
    # failed bootstrap has already changed what is running.
    _agent_journal("alarm_serve_install", dir=str(root), port=args.port, host=args.host)
    try:
        installed = daemon.install_alarm(project, root, args.port, args.max_s,
                                         args.room or [], ip or "", args.host)
    except SystemExit as exc:
        _agent_journal("alarm_serve_install_failed", error=str(exc))
        return fail(args, str(exc), "the plist was written; `alarm serve --uninstall` removes it")
    return emit(args, about | {"performed": True},
                f"Installed {installed}\n  serving {root} on port {args.port}, "
                f"restarted by launchd if it exits, started again at login\n"
                f"  check it: uv run twiddle alarm serve --status\n"
                f"  if it never answers, see Local Network in CLAUDE.md §The daemon")


def cmd_serve(args):
    """Serve alarm audio from this Mac until stopped (foreground), or manage
    the launchd agent that keeps it running."""
    if args.status:
        return _serve_status(args)
    # Omitted until here so `--status` can tell "not given" from "given as the default".
    args.port = server.PORT if args.port is None else args.port
    args.host = "0.0.0.0" if args.host is None else args.host
    if args.install or args.uninstall:
        return _serve_agent(args)
    if not args.dir:
        return fail(args, "--dir is required", "`--dir` names the folder to serve")
    root = Path(args.dir).expanduser()
    if not root.is_dir():
        return fail(args, f"{root} is not a directory", "`--dir` names the folder to serve")
    house, _, err = _anchor(args)
    if err is not None:
        return err
    if args.room:
        allowed = set()
        for query in args.room:
            try:
                allowed |= {m.ip for m in house.resolve(query).group.members}
            except Ambiguous as exc:
                return fail(args, str(exc), "be more specific", candidates=exc.candidates)
            except NotFound as exc:
                return fail(args, str(exc), "run `twiddle rooms` to list targets",
                            known=exc.known)
    else:
        allowed = {s.ip for s in house.speakers}
    # This Mac's own address as a speaker would see it, so `curl` from here works.
    this_mac = set()
    for ip in allowed:
        try:
            this_mac.add(play.local_ip_for(ip))
        except OSError:
            pass
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                   if server.resolve(root, p.relative_to(root).as_posix()))
    stale = [st for st in report.open_spans(play.INTERVENTION_LOG)
             if st["name"] == server.ACTION]
    about = {"dir": str(root), "port": args.port, "max_s": args.max_s,
             "speakers": sorted(allowed), "this_mac": sorted(this_mac), "files": files, "stale_spans": len(stale)}
    plan = (f"serving {len(files)} file(s) from {root} on port {args.port} to "
            f"{len(allowed)} speaker(s), each serve bounded to {args.max_s:.0f}s; "
            f"{len(stale)} stale span(s) to close")
    # New alarms point at the agent's port (else the default): say so rather
    # than serve audio no alarm will ask for.
    points = server.alarm_port()
    if args.port != points:
        about["warning"] = (f"new alarms point at port {points}, not {args.port}: they won't "
                            f"reach this server (`alarm serve --install --port {args.port}` "
                            "points new ones here)")
        plan += f"\n  warning: {about['warning']}"
    if getattr(args, "dry_run", False):
        return emit(args, about | {"would": "serve", "performed": False},
                    f"[dry-run] would start: {plan}")
    try:
        srv = server.AlarmServer(root, allowed | this_mac, port=args.port,
                                    max_s=args.max_s, host=args.host)
    except OSError as exc:
        return fail(args, f"could not listen on port {args.port}: {exc}",
                    "another `alarm serve` may already be running")
    # Only after the port is ours: a second copy must not close a live one's spans.
    closed = server.close_stale()
    emit(args, about | {"performed": True, "stale_spans": len(closed)},
         f"{plan}\nCtrl-C to stop")

    def _stop(_sig, _frame):
        raise KeyboardInterrupt

    prev = signal.signal(signal.SIGTERM, _stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, prev)
        srv.stop()
    return 0


def _ringing_or_fail(args, house: Household, verb: str):
    """The room's group, its coordinator's running alarm, and an `about`; or
    an exit code last. Refused when nothing is going off: a bare Stop would
    silence whatever the room is playing."""
    try:
        res = house.resolve(args.room)
    except Ambiguous as exc:
        return None, None, None, fail(args, str(exc), "be more specific",
                                      candidates=exc.candidates)
    except NotFound as exc:
        return None, None, None, fail(args, str(exc), "run `twiddle rooms` to list targets",
                                      known=exc.known)
    group, about = res.group, res.to_dict()
    try:
        running = clock.alarm_now(group.coordinator.ip)
    except Exception as exc:
        return None, None, None, fail(args, f"could not read whether {group.name} is "
                                            f"ringing: {exc}", **about)
    if running is None:
        return None, None, None, fail(
            args, f"no alarm is going off in {group.name}; nothing to {verb}",
            (f"`twiddle stop --room \"{args.room}\"` stops ordinary playback"
             if verb == "stop" else "`twiddle alarm status` shows where one is"), **about)
    return group, running, about | {"running": running.to_dict()}, None


def cmd_stop(args):
    """Stop the alarm going off in a room: the speaker's own Stop."""
    house, ip, err = _anchor(args)
    if err is not None:
        return err
    group, running, about, err = _ringing_or_fail(args, house, "stop")
    if err is not None:
        return err
    what = f"the alarm in {group.name} ({group.coordinator.label})"
    if getattr(args, "dry_run", False):
        return emit(args, about | {"would": "stop", "performed": False},
                    f"[dry-run] would stop {what}")
    try:
        clock.stop_alarm(group.coordinator.ip, running.alarm_id or None)
    except Exception as exc:
        return fail(args, f"could not stop {what}: {exc}", **about)
    return emit(args, about | {"performed": True}, f"stopped {what}")


def cmd_snooze(args):
    """Snooze the alarm going off in a room: the speaker's own SnoozeAlarm."""
    house, ip, err = _anchor(args)
    if err is not None:
        return err
    group, running, about, err = _ringing_or_fail(args, house, "snooze")
    if err is not None:
        return err
    what = f"the alarm in {group.name} ({group.coordinator.label}) for {args.minutes} minutes"
    about["minutes"] = args.minutes
    if getattr(args, "dry_run", False):
        return emit(args, about | {"would": "snooze", "performed": False},
                    f"[dry-run] would snooze {what}")
    try:
        rings = clock.snooze_alarm(group.coordinator.ip, args.minutes,
                                   running.alarm_id or None)
    except Exception as exc:
        return fail(args, f"could not snooze {what}: {exc}", **about)
    return emit(args, about | {"performed": True, "rings_utc": rings.isoformat()},
                f"snoozed {what}: it rings again at {rings.astimezone():%H:%M}")


def register(sub, parents=None):
    kw = {"parents": parents} if parents else {}
    p = sub.add_parser(**kw, name="alarm", help="the household's Sonos alarms")
    asub = p.add_subparsers(dest="alarm_cmd", required=True, metavar="<command>")
    ls = asub.add_parser(**kw, name="list",
                         help="every alarm, grouped by room (read-only)")
    ls.add_argument("--anchor", default=None,
                    help="speaker IP to query instead of SSDP discovery")
    ls.set_defaults(func=cmd_list)

    sn = asub.add_parser(**kw, name="snapshot",
                         help="save every alarm to a file (read-only)")
    sn.add_argument("--anchor", default=None,
                    help="speaker IP to query instead of SSDP discovery")
    sn.add_argument("--out", default=None, help=f"where to save it (default {SNAPSHOT_FILE})")
    sn.set_defaults(func=cmd_snapshot)

    rs = asub.add_parser(**kw, name="restore",
                         help="make the alarms match a snapshot again (WRITES)")
    rs.add_argument("--anchor", default=None,
                    help="speaker IP to query instead of SSDP discovery")
    rs.add_argument("--path", default=None,
                    help=f"the snapshot to restore (default {SNAPSHOT_FILE})")
    add_write_args(rs)
    rs.set_defaults(func=cmd_restore)

    for name, fn, helptext in (
        ("enable", cmd_enable, "switch an alarm on (WRITES)"),
        ("disable", cmd_disable, "switch an alarm off (WRITES)"),
        ("rm", cmd_rm, "delete an alarm, after asking twice (WRITES)"),
    ):
        w = asub.add_parser(**kw, name=name, help=helptext)
        w.add_argument("alarm_id", metavar="id", help="the alarm's ID, from `alarm list`")
        w.add_argument("--anchor", default=None,
                       help="speaker IP to query instead of SSDP discovery")
        add_write_args(w)
        w.set_defaults(func=fn)

    ad = asub.add_parser(**kw, name="add", help="create an alarm (WRITES)")
    ad.add_argument("--room", required=True,
                    help="the room, by name; a bonded follower's goes on its room's primary")
    ad.add_argument("--time", required=True, help="24-hour HH:MM or HH:MM:SS, household time")
    _field_args(ad, adding=True)
    ad.set_defaults(func=cmd_add)

    ed = asub.add_parser(**kw, name="edit",
                         help="change some fields of an alarm, leaving the rest (WRITES)")
    ed.add_argument("alarm_id", metavar="id", help="the alarm's ID, from `alarm list`")
    ed.add_argument("--room", default=None,
                    help="move it to this room, by name; a bonded follower's goes on "
                         "its room's primary")
    ed.add_argument("--time", default=None, help="24-hour HH:MM or HH:MM:SS, household time")
    _field_args(ed, adding=False)
    ed.set_defaults(func=cmd_edit)

    so = asub.add_parser(**kw, name="sources",
                         help="what an alarm can play, or one source's choices (read-only)")
    so.add_argument("name", nargs="?", default=None,
                    help="one source, to list what it can play")
    so.add_argument("query", nargs="?", default="", help="narrow that list")
    so.set_defaults(func=cmd_sources)

    st = asub.add_parser(**kw, name="status",
                         help="is an alarm going off, and where (read-only)")
    st.add_argument("--room", default=None, help="only this room, by name")
    st.add_argument("--anchor", default=None,
                    help="speaker IP to query instead of SSDP discovery")
    st.set_defaults(func=cmd_status)

    tr = asub.add_parser(**kw, name="try", help="fire an alarm now, to hear it (WRITES)")
    tr.add_argument("alarm_id", metavar="id", help="the alarm's ID, from `alarm list`")
    tr.add_argument("--anchor", default=None,
                    help="speaker IP to query instead of SSDP discovery")
    add_write_args(tr)
    tr.set_defaults(func=cmd_try)

    sv = asub.add_parser(**kw, name="serve",
                         help="serve alarm audio from this Mac, in the foreground")
    sv.add_argument("--dir", default=None, help="the folder of audio files to serve")
    mode = sv.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true",
                      help="keep the server running as a launchd agent (WRITES a plist)")
    mode.add_argument("--uninstall", action="store_true",
                      help="remove that agent (WRITES)")
    mode.add_argument("--status", action="store_true",
                      help="is the agent's server answering (read-only)")
    sv.add_argument("--port", type=int, default=None,
                    help=f"port (default {server.PORT}: alarms store the URL)")
    sv.add_argument("--host", default=None,
                    help="the address to listen on (default every interface, so speakers can reach it)")
    sv.add_argument("--max-s", type=_bound, default=server.DEFAULT_MAX_S,
                    help="the longest one serve may last, in seconds "
                         f"(default {server.DEFAULT_MAX_S})")
    sv.add_argument("--room", action="append",
                    help="serve only this room (repeatable); default: every speaker")
    sv.add_argument("--anchor", default=None,
                    help="speaker IP to query instead of SSDP discovery")
    add_write_args(sv)
    sv.set_defaults(func=cmd_serve)

    for name, fn, helptext in (
        ("stop", cmd_stop, "stop the alarm going off in a room (WRITES)"),
        ("snooze", cmd_snooze, "snooze the alarm going off in a room (WRITES)"),
    ):
        w = asub.add_parser(**kw, name=name, help=helptext)
        w.add_argument("--room", required=True, help="the room, by name")
        if name == "snooze":
            w.add_argument("--minutes", type=int, choices=clock.SNOOZE_MINUTES,
                           default=clock.DEFAULT_SNOOZE,
                           help=f"how long (default {clock.DEFAULT_SNOOZE})")
        w.add_argument("--anchor", default=None,
                       help="speaker IP to query instead of SSDP discovery")
        add_write_args(w)
        w.set_defaults(func=fn)


def _bound(text: str) -> float:
    """A `--max-s`: a finite number of seconds above 0 (an unbounded span would
    discount every fault after a crash)."""
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not (value > 0 and value != float("inf")):
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite number of seconds above 0")
    return value


def _field_args(p, adding: bool):
    """The fields `add` and `edit` share. Each defaults to None, "not given":
    `add` then takes the model's default, `edit` leaves the field alone."""
    d = Alarm(start_time="", recurrence=Recurrence.parse("DAILY"), room_uuid="")
    default = (lambda text: f" (default {text})") if adding else (lambda text: "")
    p.add_argument("--days", default="daily" if adding else None,
                   help=DAYS_HELP + default("daily"))
    p.add_argument("--duration", default=None,
                   help="auto-stop: 1h, 30m, 1h30, HH:MM:SS, or none"
                        + default(duration_text(d.duration)))
    p.add_argument("--volume", default=None, help="0-100" + default(str(d.volume)))
    p.add_argument("--mode", default=None,
                   help=", ".join(PLAY_MODES) + ", or the speaker's own value"
                        + default(_mode_name(d.play_mode)))
    p.add_argument("--include-grouped-rooms", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="also play in rooms grouped with it at the time"
                        + default("no"))
    on = p.add_mutually_exclusive_group()
    on.add_argument("--on", dest="enabled", action="store_const", const=True, default=None,
                    help="switched on" + default("on"))
    on.add_argument("--off", dest="enabled", action="store_const", const=False,
                    help="switched off")
    # No `choices`: the registry is read when the command runs, so a source
    # registered in `alarms/sources/` needs no edit here.
    p.add_argument("--source", default=None, metavar="NAME[:CHOICE]",
                   help="what it plays: chime, station:<key>, ... (`alarm sources` lists them)"
                        + (" (default chime)" if adding
                           else "; keep (the default) leaves it byte-for-byte"))
    p.add_argument("--anchor", default=None,
                   help="speaker IP to query instead of SSDP discovery")
    add_write_args(p)
