"""`twiddle comedy` -- CLI shape around `comedy.py`'s picking logic.

Follows the rest of the package: the `ok`/`error` envelope, `--dry-run`
resolves and prints without writing, and the relay/room plumbing is the same
`spotify_ops`-backed machinery `spotify play` and `spotify discover` already
use -- reached here the way `discover_cli.py` reaches it, by importing
`spotify_cli`'s private helpers rather than duplicating them.
"""
from __future__ import annotations

import sys

from . import comedy, play, spotify, spotify_ops
from .control_cli import _target, add_target_args, add_write_args, emit, fail
from .spotify_cli import _add_device_args, _ensure_room_on_relay, _relay_device_or_preview, _session


def _spotify_fail(args, exc: Exception) -> int:
    """A Spotify ApiError/AuthError in the repo's envelope, hint included --
    see `spotify_cli._fail`, which this mirrors."""
    err = spotify_ops.as_playback_error(exc)
    extra = dict(err.extra)
    if err.status is not None:
        extra["status"] = err.status
    return fail(args, err.message, err.hint, **extra)


def cmd_artists(args):
    artists = comedy.load_artists()
    lines = [f"{a.name}  ({a.id})" for a in artists] or \
        ["(none yet -- run `twiddle comedy refresh`)"]
    return emit(args, {"artists": [a.to_dict() for a in artists]}, "\n".join(lines))


def cmd_refresh(args):
    sess, err = _session(args)
    if err is not None:
        return err
    human = not getattr(args, "json", False)
    notify = (lambda m: print(m, file=sys.stderr)) if human else (lambda m: None)
    try:
        result = comedy.refresh(sess, notify=notify)
    except (spotify.ApiError, spotify.AuthError) as exc:
        return _spotify_fail(args, exc)

    confirmed = []
    if human and not args.no_prompt:
        for c in result.ambiguous:
            ans = input(f"is {c['name']!r} a comedian? [{c['why']}] (y/N): ").strip().lower()
            if ans.startswith("y"):
                confirmed.append(comedy.confirm(c["id"], c["name"]))

    added = result.added + confirmed
    confirmed_ids = {a.id for a in confirmed}
    remaining = [c for c in result.ambiguous if c["id"] not in confirmed_ids]
    lines = [f"added: {a.name}" for a in added] or ["nothing new"]
    if remaining:
        lines.append(f"\n{len(remaining)} unresolved -- rerun to be asked again:")
        lines += [f"  {c['name']}  ({c['why']})" for c in remaining]
    return emit(args, {"added": [a.to_dict() for a in added],
                        "ambiguous": remaining, "unchanged": result.unchanged},
                "\n".join(lines))


def cmd_new(args):
    sess, err = _session(args)
    if err is not None:
        return err
    artists = comedy.load_artists()
    if not artists:
        return fail(args, "no classified comedians yet",
                    "run `twiddle comedy refresh` first")
    try:
        releases = comedy.new_releases(sess, artists, days=args.days)
    except (spotify.ApiError, spotify.AuthError) as exc:
        return _spotify_fail(args, exc)
    lines = [f"{r['release_date']}  {r.get('_artist', '')} -- {r.get('name', '')}"
             for r in releases] or [f"nothing in the last {args.days} days"]
    return emit(args, {"releases": releases}, "\n".join(lines))


def cmd_cancel_sleep_timer(args):
    """`comedy sleep --cancel`: turn off an armed timer without stopping
    playback -- plans change mid-evening, and the timer has no other way
    to be called off short of waiting for it to fire."""
    res, err = _target(args)
    if err is not None:
        return err
    if args.dry_run:
        return emit(args, {"would": "cancel the sleep timer", "performed": False},
                    f"[dry-run] would cancel the sleep timer on {res.group.name}")
    play.configure_sleep_timer(res.group.ip, 0)
    return emit(args, {"cancelled": True, "room": res.group.name},
                f"sleep timer cancelled on {res.group.name}")


def cmd_sleep(args):
    if args.cancel:
        return cmd_cancel_sleep_timer(args)
    sess, err = _session(args)
    if err is not None:
        return err
    artists = comedy.load_artists()
    if not artists:
        return fail(args, "no classified comedians yet",
                    "run `twiddle comedy refresh` first")

    res, err = _target(args)
    if err is not None:
        return err

    try:
        picks = comedy.pick_queue(sess, artists, n=args.n)
    except ValueError as exc:
        return fail(args, str(exc))
    except (spotify.ApiError, spotify.AuthError) as exc:
        return _spotify_fail(args, exc)
    if not picks:
        return fail(args, "nothing left to pick -- everything classified was "
                          "played too recently",
                    "widen the list with `comedy refresh`, or wait it out")

    dev, err = _relay_device_or_preview(args, sess, res.group.name)
    if err is not None:
        return err
    _ensure_room_on_relay(res, args.dry_run)

    uris, total_ms = comedy.flatten(picks)
    duration_s = int(total_ms / 1000) + args.buffer * 60
    plan = "; ".join(f"{p.artist} -- {p.name}" for p in picks)
    if args.dry_run:
        return emit(args, {"would": f"play {plan}", "performed": False,
                            "duration_s": duration_s},
                    f"[dry-run] would play: {plan}\n"
                    f"  on {res.group.name}, sleep timer ~{duration_s // 60} min")

    try:
        result = comedy.start_queue(sess, dev, picks, res.group.ip,
                                     buffer_min=args.buffer)
    except (spotify.ApiError, spotify.AuthError) as exc:
        return _spotify_fail(args, exc)
    if not result.get("timer_armed", True):
        return emit(args, result,
                    f"▶ {plan}\n   on {res.group.name}\n"
                    f"   !! sleep timer NOT armed ({result.get('timer_error', '')}) "
                    f"-- it will play until stopped by hand:\n"
                    f"      twiddle stop --room {res.group.name}")
    return emit(args, result,
                f"▶ {plan}\n   on {res.group.name}\n"
                f"   sleep timer: ~{result['duration_s'] // 60} min "
                f"(fires {result['fires_at']})")


def cmd_rate(args):
    try:
        rec = comedy.rate(args.direction, album_id=args.album)
    except ValueError as exc:
        return fail(args, str(exc))
    return emit(args, rec,
                f"{args.direction} -- {rec.get('artist', '')} -- {rec.get('name', '')}")


# ---- parser wiring ------------------------------------------------------------


def register(sub, parents=None):
    kw = {"parents": parents} if parents else {}
    p = sub.add_parser(**kw, name="comedy",
                       help="pick and play something to fall asleep to")
    csub = p.add_subparsers(dest="comedy_cmd", required=True, metavar="<command>")

    a = csub.add_parser(**kw, name="artists",
                        help="the classified comedian list (read-only)")
    a.set_defaults(func=cmd_artists)

    r = csub.add_parser(**kw, name="refresh",
                        help="classify anyone new in your listening history (read-only)")
    r.add_argument("--no-prompt", action="store_true",
                   help="don't ask about ambiguous names; leave them unresolved")
    r.set_defaults(func=cmd_refresh)

    n = csub.add_parser(**kw, name="new",
                        help="recent releases from the classified list (read-only)")
    n.add_argument("--days", type=int, default=comedy.NEW_RELEASE_DAYS)
    n.set_defaults(func=cmd_new)

    s = csub.add_parser(**kw, name="sleep",
                        help="play tonight's queue and arm the sleep timer (WRITES)")
    s.add_argument("--n", type=int, default=comedy.DEFAULT_ALBUMS,
                   help="how many albums to queue")
    s.add_argument("--buffer", type=int, default=comedy.SLEEP_BUFFER_MIN,
                   help="minutes past the queue's own runtime before the "
                        "sleep timer fires")
    s.add_argument("--cancel", action="store_true",
                   help="cancel an armed sleep timer without stopping "
                        "playback, instead of starting a new queue")
    _add_device_args(s)
    s.add_argument("--volume", type=int, default=None,
                   help="speaker volume, if a relay has to be started")
    s.add_argument("--no-restore", action="store_true",
                   help="if a relay is started, leave the room on it")
    add_target_args(s, default="roam")
    add_write_args(s)
    s.set_defaults(func=cmd_sleep)

    rt = csub.add_parser(**kw, name="rate",
                        help="thumbs up/down the last (or a named) pick")
    rt.add_argument("direction", choices=["up", "down"])
    rt.add_argument("--album", default=None,
                    help="album id; default: the last one played")
    rt.set_defaults(func=cmd_rate)
