"""`twiddle alarm` -- the household's Sonos alarms from the command line.

`alarm list` is read-only: one `ListAlarms` plus the household's clock and
display format, from any speaker (alarms are household-wide). Every alarm is
shown under its room's name, including one aimed at a bonded follower or at a
speaker that has vanished: labelled, never hidden.

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

Follows the rest of the package: the `ok`/`error` envelope, `--json` anywhere.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path

from .alarms import baseline, clock
from .alarms.model import Alarm, Recurrence
from . import play
from .control_cli import SNAPSHOT_DIR, add_write_args, emit, fail
from .household import Household, Speaker

CHIME = "Sonos chime"
SNAPSHOT_FILE = SNAPSHOT_DIR / "alarms.json"
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
            f"  {'#' + r['id']:>4} {on} {r['time_text']:>8}  {r['days_text']:<18} vol {r['volume']:<3} "
            f"{r['duration_text']:<7} {r['play_mode']:<11} {r['source_title']}")
        if r["status"] != "ok":
            lines.append(f"      ! {_LABEL[r['status']].format(**r)}")
        if r["next_fire"]:
            lines.append(f"      next: {r['next_fire_text']}")
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


def cmd_list(args):
    house, ip, err = _anchor(args)
    if err is not None:
        return err
    try:
        found = clock.list_alarms(ip)
        hh = clock.household_time(ip)
    except Exception as exc:
        return fail(args, f"could not read the alarms from {ip}: {exc}")
    rows = listing(house, found.alarms, hh)
    return emit(args, {"version": found.version, "household_time": hh.to_dict(),
                       "alarms": rows}, human(rows, hh))


def cmd_snapshot(args):
    """Save every alarm, exactly as ListAlarms gave it. Read-only."""
    house, ip, err = _anchor(args)
    if err is not None:
        return err
    try:
        found = clock.list_alarms(ip)
    except Exception as exc:
        return fail(args, f"could not read the alarms from {ip}: {exc}")
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
    except Exception as exc:
        return fail(args, f"could not read the snapshot {path}: {exc}")
    house, ip, err = _anchor(args)
    if err is not None:
        return err
    try:
        found = clock.list_alarms(ip)
    except Exception as exc:
        return fail(args, f"could not read the alarms from {ip}: {exc}")
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


def _target(args):
    """The household, a speaker to ask, the alarm list and the alarm named by
    `args.alarm_id`; or an exit code last."""
    house, ip, err = _anchor(args)
    if err is not None:
        return None, None, None, None, err
    try:
        found = clock.list_alarms(ip)
    except Exception as exc:
        return None, None, None, None, fail(args, f"could not read the alarms from {ip}: {exc}")
    alarm = found.get(args.alarm_id)
    if alarm is None:
        return None, None, None, None, fail(
            args, f"no alarm {args.alarm_id}", "`twiddle alarm list` shows every alarm's ID")
    return house, ip, found, alarm, None


def _about(house: Household, alarm: Alarm, found: clock.AlarmList) -> dict:
    return {"id": alarm.id, "room": aimed_at(house, alarm.room_uuid)["room"],
            "alarm": alarm.to_attributes(), "version": found.version}


def _write_failed(args, doing: str, about: dict, exc: Exception, *, done: str,
                  update: bool) -> int:
    """A write that raised: say whether it happened, as far as can be told.

    One that landed (another alarm moved in the same moment, or the list
    couldn't be read back) is reported done, `done` saying so, with why it
    raised as a warning. The version is the list's after the write, null if
    it couldn't be read. For an update, `alarm` becomes the alarm as read
    back (null when it couldn't be) and `before` the one written over; for a
    delete, `alarm` stays the alarm deleted.
    """
    landed = getattr(exc, "landed", False)
    if landed is True:
        now = {"version": exc.current.version if exc.current else None}
        if update:
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
        seen = getattr(exc, "alarm", None)
        if seen is not None:
            done = f"{verb}d alarm {alarm.id}: {brief(house, seen)}"
        elif getattr(exc, "current", None) is None:
            done = f"{verb}d alarm {alarm.id} (the list couldn't be read back to show it)"
        else:
            done = f"{verb}d alarm {alarm.id}, but it isn't in the list read back"
        return _write_failed(args, f"{verb} alarm {alarm.id}", about, exc,
                             done=done, update=True)
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
