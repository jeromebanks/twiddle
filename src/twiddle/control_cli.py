"""The agent-facing half of the CLI: name a room, do a thing, say what happened.

Everything here follows four rules, which together are what "agentically
ergonomic" actually means in practice:

1. **Targets are named, never addressed.** IPs are DHCP here; a tool that
   requires one is a tool that breaks next week.
2. **Every command can answer in JSON**, with one envelope shape: ``ok`` plus
   either the payload or ``error`` and a ``hint`` naming valid alternatives.
   A caller that gets a machine-readable failure can correct itself.
3. **Redirection is reported, never silent.** Naming a bonded follower is a
   reasonable thing to do, and the answer says which speaker actually took the
   command and why -- see ``Resolution``.
4. **Writes are previewable.** ``--dry-run`` resolves and prints the plan
   without touching a speaker, so a caller can check its aim first.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import play
from .household import Ambiguous, Household, NotFound, Snapshot

SNAPSHOT_DIR = Path("logs/snapshots")

VOLUME_STEP = 5  # default step for a bare +, ++, -, or --
EQ_STEP = 2       # default step for bass/treble/balance's bare ++ or --

_VOLUME_UP_RE = re.compile(r"^\+{1,2}(\d+)?$")
_VOLUME_DOWN_RE = re.compile(r"^-{1,2}(\d+)?$")
_EQ_ABS_RE = re.compile(r"^[+-]?\d+$")
_EQ_REL_RE = re.compile(r"^([+-]{2})(\d+)?$")
# Only these two shapes trip argparse: a bare `--` is its own end-of-options
# marker, and `--8` looks like an unknown long option. Everything else
# (`44`, `+4`, `-4`, a lone `-`) already survives as a plain positional.
_ARGPARSE_HOSTILE_RE = re.compile(r"^--(\d+)?$")
# Subcommands whose `spec` token needs the same argparse rescue.
_SHORTHAND_COMMANDS = ("volume", "vol", "bass", "treble", "balance")


class BadSpec(ValueError):
    pass


def parse_volume_spec(token: str) -> tuple[str, int | None]:
    """Turn a shorthand token into ("abs"|"rel"|"mute"|"unmute", value).

    `44` sets absolute; `+4`/`++4` and `-4`/`--8` step relative by that
    amount; a bare `+`, `++`, `-`, or `--` steps by VOLUME_STEP; `mute` and
    `unmute` toggle mute. The leading `@` is a sentinel `normalize_argv`
    prepends to tokens argparse can't pass through untouched.
    """
    t = token[1:] if token.startswith("@") else token
    low = t.lower()
    if low == "mute":
        return "mute", None
    if low == "unmute":
        return "unmute", None
    if t.isdigit():
        return "abs", int(t)
    m = _VOLUME_UP_RE.match(t)
    if m:
        return "rel", int(m.group(1)) if m.group(1) else VOLUME_STEP
    m = _VOLUME_DOWN_RE.match(t)
    if m:
        return "rel", -(int(m.group(1)) if m.group(1) else VOLUME_STEP)
    raise BadSpec(f"unrecognized volume spec: {token!r}")


def parse_eq_spec(token: str) -> tuple[str, int]:
    """Turn a shorthand token into ("abs"|"rel", value) for a signed range.

    Bass, treble and balance are signed (-10..10), so unlike volume a
    single `-4` is a legitimate absolute value, not "step down" -- that
    would be ambiguous. So here a single sign is absolute (`-4` sets to
    -4) and doubling it steps relative (`--4` steps down by 4; a bare `++`
    or `--` steps by EQ_STEP).
    """
    t = token[1:] if token.startswith("@") else token
    m = _EQ_REL_RE.match(t)
    if m:
        n = int(m.group(2)) if m.group(2) else EQ_STEP
        return "rel", n if m.group(1) == "++" else -n
    if _EQ_ABS_RE.match(t):
        return "abs", int(t)
    raise BadSpec(f"unrecognized spec: {token!r}")


def normalize_argv(argv: list[str]) -> list[str]:
    """Sentinel the shorthand tokens argparse would otherwise mangle.

    A bare `--` is argparse's own end-of-options marker (it gets dropped,
    never reaches `spec`), and `--8` parses as an unrecognized long option.
    Prefixing either with `@` makes it an ordinary positional string;
    `parse_volume_spec`/`parse_eq_spec` strip the sentinel back off.
    """
    i = next((k for k, tok in enumerate(argv) if tok in _SHORTHAND_COMMANDS), None)
    if i is None:
        return argv
    out = list(argv)
    j = i + 1
    while j < len(out):
        tok = out[j]
        if tok in ("--room", "--anchor"):
            j += 2
            continue
        if tok in ("--json", "--dry-run"):
            j += 1
            continue
        if _ARGPARSE_HOSTILE_RE.match(tok):
            out[j] = "@" + tok
        break
    return out


# ---- output ----------------------------------------------------------------

def emit(args, payload: dict, human: str = "") -> int:
    """One envelope for every command, in whichever dialect was asked for."""
    payload = {"ok": True} | payload
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    elif human:
        print(human)
    return 0


def fail(args, error: str, hint: str = "", **extra) -> int:
    payload = {"ok": False, "error": error} | extra
    if hint:
        payload["hint"] = hint
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(f"error: {error}", file=sys.stderr)
        if hint:
            print(f"hint:  {hint}", file=sys.stderr)
    return 1


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


# ---- target resolution -----------------------------------------------------

def _household(args) -> Household:
    return Household.load(anchor=getattr(args, "anchor", None))


def _target(args):
    """Resolve --room into a group, or raise a CLI-shaped failure.

    Returns (resolution, None) or (None, exit_code) so callers stay flat.
    """
    try:
        house = _household(args)
    except Exception as exc:
        return None, fail(args, f"could not reach the household: {exc}",
                          "check you are on the same LAN, or pass --anchor <ip>")
    room = getattr(args, "room", None)
    if not room:
        if len(house.groups) == 1:
            return house.resolve(house.groups[0].name), None
        return None, fail(
            args, "no --room given and more than one group exists",
            "name one of: " + ", ".join(g.name for g in house.groups),
            rooms=[g.name for g in house.groups])
    try:
        return house.resolve(room), None
    except Ambiguous as exc:
        return None, fail(args, str(exc), "be more specific",
                          candidates=exc.candidates)
    except NotFound as exc:
        return None, fail(args, str(exc), "run `twiddle rooms` to list targets",
                          known=exc.known)


def _preview(args, res, action: str) -> int | None:
    """If --dry-run, describe the write and decline to perform it."""
    if not getattr(args, "dry_run", False):
        return None
    d = res.to_dict() | {"would": action, "performed": False}
    return emit(args, d, f"[dry-run] would {action} on {d['acted_on']}"
                         + (f"\n  ({d['reason']})" if res.redirected else ""))


def _acted(res, **extra) -> dict:
    return res.to_dict() | extra


def _note(res) -> str:
    return f"\n  note: {res.reason}" if res.redirected else ""


# ---- read-only commands ----------------------------------------------------

def cmd_rooms(args):
    """List every target a command can name, and what it stands for."""
    try:
        house = _household(args)
    except Exception as exc:
        return fail(args, f"could not reach the household: {exc}",
                    "check you are on the same LAN, or pass --anchor <ip>")
    payload = {"groups": [g.to_dict() for g in house.groups]}
    lines = []
    for g in house.groups:
        lines.append(f"{g.name}   -> {g.coordinator.ip} (coordinator)")
        for m in g.members:
            mark = " " if m.addressable else "*"
            chan = f" {m.channel}" if m.channel else ""
            lines.append(f"  {mark} {m.name}{chan}  {m.ip}  {m.model}")
    lines.append("\n* bonded follower: not independently addressable; "
                 "commands sent to it are routed to the coordinator.")
    return emit(args, payload, "\n".join(lines))


def cmd_status(args):
    """What is playing -- for one room, or the whole household."""
    if getattr(args, "room", None):
        res, err = _target(args)
        if err is not None:
            return err
        groups = [res.group]
    else:
        try:
            groups = _household(args).groups
        except Exception as exc:
            return fail(args, f"could not reach the household: {exc}")
    states = [g.now_playing() for g in groups]
    lines = []
    for s in states:
        vols = ", ".join(f"{ip.rsplit('.', 1)[-1]}:{v}"
                         for ip, v in s["volume"].items())
        lines.append(f"{s['group']:16} {s['state']:12} vol {vols}")
        if s["uri"]:
            kind = "stream" if s["is_stream"] else "track"
            pos = "" if s["is_stream"] else f"  {s['position']}/{s['duration']}"
            lines.append(f"                 {kind}: {s['uri'][:70]}{pos}")
    return emit(args, {"groups": states}, "\n".join(lines))


# ---- transport -------------------------------------------------------------

def _simple(args, verb: str, fn):
    res, err = _target(args)
    if err is not None:
        return err
    preview = _preview(args, res, verb)
    if preview is not None:
        return preview
    try:
        fn(res.group)
    except Exception as exc:
        return fail(args, f"{verb} failed: {type(exc).__name__}: {exc}",
                    **_acted(res))
    state = play.transport_info(res.group.ip).get("CurrentTransportState", "?")
    return emit(args, _acted(res, action=verb, state=state),
                f"{verb} -> {res.group.name} ({state}){_note(res)}")


def cmd_play(args):
    return _simple(args, "play", lambda g: g.play())


def cmd_pause(args):
    return _simple(args, "pause", lambda g: g.pause())


def cmd_stop(args):
    return _simple(args, "stop", lambda g: g.stop())


def cmd_next(args):
    return _simple(args, "next", lambda g: g.next())


def cmd_prev(args):
    return _simple(args, "previous", lambda g: g.previous())


def cmd_volume(args):
    res, err = _target(args)
    if err is not None:
        return err
    group = res.group
    current = group.volume()
    if args.spec is None:
        return emit(args, _acted(res, volume=current),
                    f"{group.name}: " + ", ".join(f"{ip} {v}"
                                                  for ip, v in current.items()))
    try:
        kind, value = parse_volume_spec(args.spec)
    except BadSpec as exc:
        return fail(args, str(exc),
                    "use a number (44), a step (+4, ++4, -4, --8, -, --), "
                    "or mute/unmute")
    if kind in ("mute", "unmute"):
        muted = kind == "mute"
        preview = _preview(args, res, f"mute {'on' if muted else 'off'}")
        if preview is not None:
            return preview
        group.set_mute(muted)
        return emit(args, _acted(res, muted=muted),
                    f"{group.name} mute -> {'on' if muted else 'off'}{_note(res)}")
    if kind == "rel":
        # Step from the coordinator's level so a bond does not drift apart.
        # Group.volume() reports -1 for a read it could not make; treating
        # that as 0 would turn a step-down into a silent mute of the whole
        # bond, reported as a small step. Refuse instead.
        base = current.get(group.ip)
        if base is None or base < 0:
            return fail(args,
                        f"could not read the current volume of {group.name}",
                        "set an absolute level instead of a relative step",
                        **_acted(res, volume=current))
        level = max(0, min(100, base + value))
    else:
        level = max(0, min(100, value))
    preview = _preview(args, res, f"set volume to {level}")
    if preview is not None:
        return preview
    after = group.set_volume(level)
    return emit(args, _acted(res, volume=after, requested_level=level),
                f"{group.name} volume -> " +
                ", ".join(f"{ip} {v}" for ip, v in after.items()) + _note(res))


def cmd_mute(args):
    res, err = _target(args)
    if err is not None:
        return err
    muted = args.state == "on"
    preview = _preview(args, res, f"mute {args.state}")
    if preview is not None:
        return preview
    res.group.set_mute(muted)
    return emit(args, _acted(res, muted=muted),
                f"{res.group.name} mute -> {args.state}{_note(res)}")


# ---- sleep timer ------------------------------------------------------------

def parse_sleep_spec(token: str) -> int:
    """Seconds out of "30" (minutes), "45m", "1h", "1h30", "1:30"; 0 for off."""
    low = token.strip().lower()
    if low in ("off", "cancel", "none", "0"):
        return 0
    if mins := re.fullmatch(r"(\d+)m?", low):
        hours, minutes = 0, int(mins.group(1))
    elif hm := re.fullmatch(r"(\d+)(?:h|:)(\d{0,2})m?", low):
        hours, minutes = int(hm.group(1)), int(hm.group(2) or 0)
    else:
        raise BadSpec(f"can't read {token!r} as a duration")
    seconds = (hours * 60 + minutes) * 60
    if not 0 < seconds < 24 * 3600:
        raise BadSpec(f"{token!r} is not between a minute and a day")
    return seconds


def cmd_sleep(args):
    """Read, set or cancel a room's sleep timer -- the speaker's own, so it
    fires with nothing on this Mac still running."""
    res, err = _target(args)
    if err is not None:
        return err
    g = res.group
    if args.spec is None:
        try:
            left = play.get_sleep_timer(g.ip)
        except Exception as exc:
            return fail(args, f"couldn't read the sleep timer: {type(exc).__name__}: {exc}",
                        **_acted(res))
        return emit(args, _acted(res, remaining_s=left),
                    f"{g.name} sleep timer: {play.fmt_left(left)}"
                    + (" left" if left else "") + _note(res))
    try:
        seconds = parse_sleep_spec(args.spec)
    except BadSpec as exc:
        return fail(args, str(exc), "minutes (30), 45m, 1h, 1h30 or 1:30; `off` cancels")
    what = f"set the sleep timer to {play.fmt_left(seconds)}" if seconds else "cancel the sleep timer"
    preview = _preview(args, res, what)
    if preview is not None:
        return preview
    try:
        fires = play.set_sleep_timer(g.ip, seconds, source="cli")
        left = play.get_sleep_timer(g.ip)
    except Exception as exc:
        return fail(args, f"sleep timer failed: {type(exc).__name__}: {exc}", **_acted(res))
    if seconds and not left:
        return fail(args, f"{g.name} accepted the timer but reports none running",
                    "is the room playing? try `twiddle sleep --room ...` to read it",
                    **_acted(res))
    local = fires.astimezone().strftime("%H:%M") if fires else ""
    return emit(args, _acted(res, remaining_s=left,
                             fires_at=fires.isoformat(timespec="seconds") if fires else None),
                (f"{g.name} sleeps in {play.fmt_left(left)} (at {local})" if seconds
                 else f"{g.name} sleep timer off") + _note(res))


# ---- EQ: bass, treble, balance, loudness -----------------------------------

def _cmd_eq(args, kind: str, lo: int, hi: int, read, write):
    """Shared plumbing for bass/treble/balance: read, set, or step."""
    res, err = _target(args)
    if err is not None:
        return err
    group = res.group
    current = read(group)
    if args.spec is None:
        return emit(args, _acted(res, **{kind: current}),
                    f"{group.name} {kind}: " +
                    ", ".join(f"{ip} {v}" for ip, v in current.items()))
    try:
        mode, value = parse_eq_spec(args.spec)
    except BadSpec as exc:
        return fail(args, str(exc),
                    f"use a number from {lo} to {hi}, or a step "
                    f"(++2, --2, bare ++/--)")
    if mode == "rel":
        base = current.get(group.ip)
        if base is None:
            return fail(args,
                        f"could not read the current {kind} of {group.name}",
                        "set an absolute level instead of a relative step",
                        **_acted(res, **{kind: current}))
        level = max(lo, min(hi, base + value))
    else:
        level = max(lo, min(hi, value))
    preview = _preview(args, res, f"set {kind} to {level}")
    if preview is not None:
        return preview
    after = write(group, level)
    return emit(args, _acted(res, **{kind: after}, requested_level=level),
                f"{group.name} {kind} -> " +
                ", ".join(f"{ip} {v}" for ip, v in after.items()) + _note(res))


def cmd_bass(args):
    return _cmd_eq(args, "bass", -10, 10,
                   lambda g: g.bass(), lambda g, lvl: g.set_bass(lvl))


def cmd_treble(args):
    return _cmd_eq(args, "treble", -10, 10,
                   lambda g: g.treble(), lambda g, lvl: g.set_treble(lvl))


def cmd_balance(args):
    """-10 full left .. 0 centered .. +10 full right."""
    return _cmd_eq(args, "balance", -10, 10,
                   lambda g: g.balance(), lambda g, lvl: g.set_balance(lvl))


def cmd_loudness(args):
    res, err = _target(args)
    if err is not None:
        return err
    group = res.group
    if args.state is None:
        current = group.loudness()
        return emit(args, _acted(res, loudness=current),
                    f"{group.name} loudness: " +
                    ", ".join(f"{ip} {'on' if v else 'off'}"
                             for ip, v in current.items()))
    on = args.state == "on"
    preview = _preview(args, res, f"loudness {args.state}")
    if preview is not None:
        return preview
    after = group.set_loudness(on)
    return emit(args, _acted(res, loudness=after),
                f"{group.name} loudness -> {args.state}{_note(res)}")


# ---- playback mode: shuffle, repeat ----------------------------------------

def cmd_shuffle(args):
    res, err = _target(args)
    if err is not None:
        return err
    group = res.group
    if args.state is None:
        on = group.shuffle()
        return emit(args, _acted(res, shuffle=on),
                    f"{group.name} shuffle: {'on' if on else 'off'}")
    on = args.state == "on"
    preview = _preview(args, res, f"shuffle {args.state}")
    if preview is not None:
        return preview
    after = group.set_shuffle(on)
    return emit(args, _acted(res, shuffle=after),
                f"{group.name} shuffle -> {'on' if after else 'off'}{_note(res)}")


def cmd_repeat(args):
    res, err = _target(args)
    if err is not None:
        return err
    group = res.group
    if args.state is None:
        mode = group.repeat()
        return emit(args, _acted(res, repeat=mode),
                    f"{group.name} repeat: {mode}")
    preview = _preview(args, res, f"repeat {args.state}")
    if preview is not None:
        return preview
    after = group.set_repeat(args.state)
    return emit(args, _acted(res, repeat=after),
                f"{group.name} repeat -> {after}{_note(res)}")


def cmd_play_radio(args):
    """Point a room at an HTTP stream and return, rather than babysitting it.

    The long-running watch that used to be the only behaviour still exists as
    `twiddle diag radio --watch`; this is the fire-and-return form an agent
    needs, since a command that never exits cannot be composed.
    """
    res, err = _target(args)
    if err is not None:
        return err
    preview = _preview(args, res, f"play stream {args.url}")
    if preview is not None:
        return preview
    if args.volume is not None:
        res.group.set_volume(args.volume)
    try:
        # Only `tune` knows a station's logo; a bare `stream` URL has none.
        res.group.play_radio(args.url, args.title, art=getattr(args, "art", None))
    except Exception as exc:
        return fail(args, f"could not start stream: {type(exc).__name__}: {exc}",
                    **_acted(res))
    info = play.transport_info(res.group.ip)
    state = info.get("CurrentTransportState", "?")
    return emit(args, _acted(res, url=args.url, title=args.title, state=state),
                f"{res.group.name} -> {args.title} ({state}){_note(res)}")


# ---- state preservation ----------------------------------------------------

def cmd_snapshot(args):
    """Record a room's state so it can be put back exactly.

    Written to a file *and* printed as JSON: a file survives the process, and
    the printed copy lets a caller hold the state in its own context and hand
    it back later without depending on this machine's filesystem.
    """
    res, err = _target(args)
    if err is not None:
        return err
    snap = res.group.snapshot()
    path = Path(args.out) if args.out else SNAPSHOT_DIR / f"{_slug(res.group.name)}.json"
    snap.save(path)
    return emit(args, _acted(res, snapshot=snap.to_dict(), path=str(path)),
                f"snapshot of {res.group.name} -> {path}\n"
                f"  {snap.state}  vol {snap.volumes}\n"
                f"  {'stream' if snap.is_stream else 'track'}: {snap.uri[:70]}")


def cmd_restore(args):
    res, err = _target(args)
    if err is not None:
        return err
    if args.state:
        try:
            snap = Snapshot.from_dict(json.loads(args.state))
        except Exception as exc:
            return fail(args, f"could not parse --state: {exc}",
                        "pass the JSON object printed by `twiddle snapshot`")
    else:
        path = Path(args.path) if args.path else \
            SNAPSHOT_DIR / f"{_slug(res.group.name)}.json"
        if not path.exists():
            return fail(args, f"no snapshot at {path}",
                        "take one first with `twiddle snapshot --room ...`")
        snap = Snapshot.load(path)
    preview = _preview(args, res, f"restore {snap.state} from {snap.taken_utc}")
    if preview is not None:
        return preview
    result = res.group.restore(snap)
    ok = not result["problems"]
    payload = _acted(res, restored=result["restored"],
                     problems=result["problems"], taken_utc=snap.taken_utc)
    payload["ok"] = ok
    human = f"restored {res.group.name} to its state at {snap.taken_utc}\n  " + \
            "\n  ".join(result["restored"])
    if result["problems"]:
        human += "\n  PROBLEMS: " + "; ".join(result["problems"])
    emit(args, payload, human)
    return 0 if ok else 1


# ---- grouping --------------------------------------------------------------

def cmd_group(args):
    """Make one room follow another's transport."""
    try:
        house = _household(args)
        target = house.resolve(args.room)
        leader = house.resolve(args.to)
    except (Ambiguous, NotFound) as exc:
        return fail(args, str(exc), "run `twiddle rooms` to list targets")
    except Exception as exc:
        return fail(args, f"could not reach the household: {exc}")
    if target.group.gid == leader.group.gid:
        return emit(args, {"already_grouped": True, "group": leader.group.name},
                    f"{target.group.name} already plays with {leader.group.name}")
    if getattr(args, "dry_run", False):
        return emit(args, {"would": f"group {target.group.name} with "
                                    f"{leader.group.name}", "performed": False},
                    f"[dry-run] would group {target.group.name} with {leader.group.name}")
    play.join(target.group.ip, leader.group.coordinator.uuid)
    return emit(args, {"joined": target.group.name, "to": leader.group.name},
                f"{target.group.name} now follows {leader.group.name}")


def cmd_ungroup(args):
    res, err = _target(args)
    if err is not None:
        return err
    preview = _preview(args, res, "leave its group")
    if preview is not None:
        return preview
    play.unjoin(res.group.ip)
    return emit(args, _acted(res, ungrouped=True),
                f"{res.group.name} is now standalone")


# ---- parser wiring ---------------------------------------------------------

def add_target_args(p, *, room_required: bool = False, default: str | None = None):
    p.add_argument("--room", required=room_required, default=default,
                   help="room, speaker or IP; partial names are matched")
    p.add_argument("--anchor", default=None,
                   help="speaker IP to query instead of SSDP discovery")


def add_write_args(p):
    p.add_argument("--dry-run", action="store_true",
                   help="resolve the target and print the plan without writing")


def register(sub, parents=None):
    """Attach the control commands to an existing subparser set."""
    kw = {"parents": parents} if parents else {}

    p = sub.add_parser(**kw, name="rooms", help="list every target a command can name")
    add_target_args(p)
    p.set_defaults(func=cmd_rooms)

    p = sub.add_parser(**kw, name="status", help="what is playing (all rooms, or one)")
    add_target_args(p)
    p.set_defaults(func=cmd_status)

    for name, fn, helptext in (
        ("play", cmd_play, "resume playback"),
        ("pause", cmd_pause, "pause playback"),
        ("stop", cmd_stop, "stop playback"),
        ("next", cmd_next, "skip to the next track"),
        ("prev", cmd_prev, "go back a track"),
    ):
        p = sub.add_parser(**kw, name=name, help=helptext + " (WRITES)")
        add_target_args(p)
        add_write_args(p)
        p.set_defaults(func=fn)

    p = sub.add_parser(**kw, name="volume", aliases=["vol"],
                        help="read, set, step, or mute volume (WRITES if setting)")
    p.add_argument("spec", nargs="?", default=None,
                   help="omit to read; 44 sets absolute; +4/++4 or -4/--8 "
                        "steps relative; bare +/++/-/-- steps by "
                        f"{VOLUME_STEP}; mute/unmute toggles mute")
    add_target_args(p)
    add_write_args(p)
    p.set_defaults(func=cmd_volume)

    p = sub.add_parser(**kw, name="mute", help="mute or unmute a room (WRITES)")
    p.add_argument("state", choices=["on", "off"])
    add_target_args(p)
    add_write_args(p)
    p.set_defaults(func=cmd_mute)

    p = sub.add_parser(**kw, name="sleep",
                        help="read, set or cancel a room's sleep timer (WRITES if setting)")
    p.add_argument("spec", nargs="?", default=None,
                   help="omit to read; 30 (minutes), 45m, 1h, 1h30 or 1:30 sets; off cancels")
    add_target_args(p)
    add_write_args(p)
    p.set_defaults(func=cmd_sleep)

    for name, fn, extra_help in (
        ("bass", cmd_bass, ""),
        ("treble", cmd_treble, ""),
        ("balance", cmd_balance, "; -10 full left .. +10 full right"),
    ):
        p = sub.add_parser(**kw, name=name,
                            help=f"read, set, or step {name} (-10..10{extra_help}) "
                                 "(WRITES if setting)")
        p.add_argument("spec", nargs="?", default=None,
                       help=f"omit to read; -4 sets absolute; ++2/--2 steps "
                            f"relative; bare ++/-- steps by {EQ_STEP}")
        add_target_args(p)
        add_write_args(p)
        p.set_defaults(func=fn)

    p = sub.add_parser(**kw, name="loudness",
                        help="read or toggle loudness compensation (WRITES if setting)")
    p.add_argument("state", nargs="?", choices=["on", "off"], default=None,
                   help="omit to read")
    add_target_args(p)
    add_write_args(p)
    p.set_defaults(func=cmd_loudness)

    p = sub.add_parser(**kw, name="shuffle",
                        help="read or toggle shuffle (WRITES if setting)")
    p.add_argument("state", nargs="?", choices=["on", "off"], default=None,
                   help="omit to read")
    add_target_args(p)
    add_write_args(p)
    p.set_defaults(func=cmd_shuffle)

    p = sub.add_parser(**kw, name="repeat",
                        help="read or set repeat: off, all, or one (WRITES if setting)")
    p.add_argument("state", nargs="?", choices=["off", "all", "one"], default=None,
                   help="omit to read")
    add_target_args(p)
    add_write_args(p)
    p.set_defaults(func=cmd_repeat)

    p = sub.add_parser(**kw, name="stream", help="play an HTTP stream and return (WRITES)")
    p.add_argument("url")
    p.add_argument("--title", default="Stream")
    p.add_argument("--volume", type=int, default=None)
    add_target_args(p)
    add_write_args(p)
    p.set_defaults(func=cmd_play_radio)

    p = sub.add_parser(**kw, name="snapshot", help="record a room's state so it can be restored")
    p.add_argument("--out", default=None, help="where to write it")
    add_target_args(p)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser(**kw, name="restore", help="put a room back as it was (WRITES)")
    p.add_argument("--path", default=None, help="snapshot file to restore from")
    p.add_argument("--state", default=None,
                   help="snapshot JSON inline, as printed by `snapshot`")
    add_target_args(p)
    add_write_args(p)
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser(**kw, name="group", help="make a room follow another (WRITES)")
    p.add_argument("--to", required=True, help="the room to follow")
    add_target_args(p, room_required=True)
    add_write_args(p)
    p.set_defaults(func=cmd_group)

    p = sub.add_parser(**kw, name="ungroup", help="detach a room from its group (WRITES)")
    add_target_args(p)
    add_write_args(p)
    p.set_defaults(func=cmd_ungroup)
