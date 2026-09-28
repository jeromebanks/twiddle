"""`twiddle spotify` -- choose what plays, from a script or an agent.

This is the layer a TUI or an MCP server sits on, so it is shaped for a
caller that is not a person: every command answers the repo's `ok`/`error`
envelope under `--json`, every failure names the command that fixes it, and
nothing prompts.

Only one command has a side effect beyond the Web API. `spotify play --room`
will bring a relay up if none is running, because "play this on the Roam" is
the whole point and an agent should not have to orchestrate two commands to
express it. Every other verb is a pure API call, so the TUI can compose them
without wondering what else might happen.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import relay, spotify, spotify_ops, stations, supervisor
from .control_cli import _target, add_target_args, emit, fail
from .relay_cli import DEFAULT_CACHE, DEFAULT_DEVICE_NAME

# Imported lazily inside register(), not here: discover_cli imports several
# private helpers back out of this module (_session, _api, _relay_device,
# _play_and_check), so importing it at module load time would be a circular
# import that fails before either module finishes executing.


def _session(args):
    """An authenticated session, or a CLI-shaped failure telling you to auth."""
    try:
        return spotify_ops.session(), None
    except spotify_ops.PlaybackError as exc:
        return None, _fail(args, exc)


def _fail(args, exc: spotify_ops.PlaybackError) -> int:
    """A PlaybackError in the repo's `ok`/`error` envelope."""
    extra = dict(exc.extra)
    if exc.status is not None:
        extra["status"] = exc.status
    return fail(args, exc.message, exc.hint, **extra)


def _api(args, fn):
    """Run an API call, turning Spotify's errors into the repo's envelope."""
    try:
        return fn(), None
    except (spotify.ApiError, spotify.AuthError) as exc:
        return None, _fail(args, spotify_ops.as_playback_error(exc))


# ---- auth ------------------------------------------------------------------


def cmd_auth(args):
    """One browser round-trip, exchanged for unattended access from then on."""
    path = Path(args.tokens) if args.tokens else spotify.TOKEN_PATH
    if args.client_id or args.redirect:
        spotify.save_config(args.client_id or "", args.redirect or "")
    if path.exists() and not args.force:
        return emit(args, {"tokens": str(path), "already": True},
                    f"Already authorized -- tokens at {path}\n"
                    "Pass --force to authorize a different account.")
    try:
        tokens = spotify.run_pkce_flow(timeout=args.timeout,
                                       open_browser=not args.no_browser)
    except spotify.AuthError as exc:
        if str(exc) == spotify.NO_CLIENT_ID:
            return fail(args, str(exc), "no client id yet: steps 1-3 above, once")
        return fail(args, str(exc),
                    "re-run `spotify auth`; if the browser step is slow, "
                    f"raise --timeout above {args.timeout:.0f}s")
    tokens.save(path)
    missing = tokens.missing_scopes()
    if missing:
        # Authorizing against a consent that already exists for this client
        # can hand back that consent's scopes rather than the ones requested.
        # Playback control then fails later, far from the cause.
        return fail(args, "Spotify granted a token without playback scopes: "
                          f"missing {', '.join(missing)}",
                    "revoke this app at spotify.com/account/apps, then re-run "
                    "`spotify auth --force`", granted=tokens.scope)
    return emit(args, {"tokens": str(path), "scope": tokens.scope},
                f"\nAuthorized. Tokens cached at {path}\n"
                "The refresh token is reused automatically, so this is the "
                "only browser step -- but a Spotify app left in Development\n"
                "mode expires its refresh tokens after 180 days, at which "
                "point re-run this command.")


# ---- reads -----------------------------------------------------------------


def cmd_devices(args):
    """Every device Spotify will play to, and which one is active.

    This is the diagnostic for the question that decides whether unattended
    control works at all: does the relay's librespot appear here?
    """
    sess, err = _session(args)
    if err is not None:
        return err
    devices, err = _api(args, sess.devices)
    if err is not None:
        return err
    lines = [f"{'*' if d.get('is_active') else ' '} {d.get('name','?'):28} "
             f"{d.get('type',''):10} vol {d.get('volume_percent','?')}  {d.get('id','')}"
             for d in devices] or ["(no devices visible to Spotify)"]
    if not devices:
        lines.append("\nStart one with: uv run twiddle relay up --room roam")
    return emit(args, {"devices": devices}, "\n".join(lines))


def cmd_search(args):
    sess, err = _session(args)
    if err is not None:
        return err
    items, err = _api(args, lambda: sess.search(args.query, args.type, args.limit))
    if err is not None:
        return err
    lines = [f"{i:2}. {spotify.describe(it)}\n    {it.get('uri','')}"
             for i, it in enumerate(items, 1)] or ["(no matches)"]
    return emit(args, {"query": args.query, "type": args.type,
                       "results": [{"uri": it.get("uri"),
                                    "name": it.get("name"),
                                    "description": spotify.describe(it)}
                                   for it in items]},
                "\n".join(lines))


def cmd_now(args):
    sess, err = _session(args)
    if err is not None:
        return err
    state, err = _api(args, sess.current)
    if err is not None:
        return err
    np = spotify.now_playing(state)
    if not np.get("track"):
        return emit(args, np, "Nothing is playing.")
    pos = np.get("progress_ms") or 0
    dur = np.get("duration_ms") or 0
    human = (f"{'▶' if np['playing'] else '❚❚'} {np['track']} - "
             f"{', '.join(np['artists'])}\n"
             f"   {np['album']}\n"
             f"   {pos // 60000}:{pos // 1000 % 60:02d} / "
             f"{dur // 60000}:{dur // 1000 % 60:02d}   on {np['device']}")
    return emit(args, np, human)


# ---- playback --------------------------------------------------------------


def _relay_device(args, sess):
    """Find the relay's librespot in Spotify's device list, starting it if asked.

    Returns (device, error_code). See `spotify_ops.relay_device`.
    """
    try:
        return spotify_ops.relay_device(
            sess, device=args.device, device_name=args.device_name,
            room=args.room, cache=args.cache,
            no_restore=getattr(args, "no_restore", False),
            volume=getattr(args, "volume", None),
            notify=lambda m: print(m, file=sys.stderr)), None
    except spotify_ops.PlaybackError as exc:
        return None, _fail(args, exc)


# ---- keeping the room pointed at the relay ----------------------------------


def _relay_url(group) -> str:
    return spotify_ops.relay_url(group)


def _ensure_room_on_relay(res, dry_run: bool) -> None:
    """See `spotify_ops.ensure_room_on_relay`; never re-points needlessly."""
    spotify_ops.ensure_room_on_relay(res.group, dry_run, _relay_url(res.group),
                                     notify=lambda m: print(m, file=sys.stderr))


def _relay_device_or_preview(args, sess, room_name: str):
    """`_relay_device`, except under --dry-run: look up only, never start.

    `_relay_device` can start a real, detached relay; a dry run must not.
    """
    if not getattr(args, "dry_run", False):
        return _relay_device(args, sess)
    devices = sess.devices()
    dev = spotify.find_device(devices, args.device or args.device_name)
    if dev is None:
        print(f"[dry-run] would start a relay on {room_name}", file=sys.stderr)
        dev = {"id": "", "name": args.device_name}
    return dev, None


# Spotify's search ranking is not stable across `limit`, which is not
# documented anywhere and is easy to get wrong. Measured, searching albums
# for "kind of blue":
#
#   limit=1   -> Jazz Impressions Of A Boy Named Charlie Brown (!)
#   limit=2   -> Kind Of Blue (Legacy Edition)
#   limit=5   -> Kind Of Blue
#   limit=10  -> Kind Of Blue
#
# So asking for exactly the one result you want returns the wrong one. Ask
# for a page and take the top of it, which is what a person sees when they
# search in the app.
RESOLVE_LIMIT = 10


def _resolve_uri(args, sess, term: str) -> tuple[str, str, int | None]:
    """A Spotify URI, or the top search hit for whatever was typed."""
    if term.startswith("spotify:") or "open.spotify.com" in term:
        if "open.spotify.com" in term:
            parts = term.split("?")[0].rstrip("/").split("/")
            term = f"spotify:{parts[-2]}:{parts[-1]}"
        return term, term, None
    items = sess.search(term, args.type, limit=RESOLVE_LIMIT)
    if not items:
        return "", "", None
    return items[0].get("uri", ""), spotify.describe(items[0]), None


def _play_and_check(args, sess, dev, play_fn):
    """Run a play call, tolerating the transfer-then-play 403 race.

    Returns (now_playing, error). `now_playing` is set only when the race
    was detected and playback is, in fact, already under way on `dev`. See
    `spotify_ops.play_and_check`.
    """
    try:
        return spotify_ops.play_and_check(sess, dev, play_fn), None
    except (spotify.ApiError, spotify.AuthError) as exc:
        return None, _fail(args, spotify_ops.as_playback_error(exc))


def cmd_play(args):
    """Play something, on the Roam, in one command.

    With no argument it resumes whatever was paused; with a query it searches
    and plays the top hit; with a URI it plays that exactly.
    """
    sess, err = _session(args)
    if err is not None:
        return err

    uri = label = ""
    if args.what:
        term = " ".join(args.what)
        try:
            uri, label, _ = _resolve_uri(args, sess, term)
        except spotify.ApiError as exc:
            return fail(args, f"search failed: {exc}")
        if not uri:
            return fail(args, f"nothing on Spotify matched {term!r}",
                        "try `twiddle spotify search` to see what exists")

    # The room itself has to be listening to the relay: it may have drifted
    # to a radio station since the relay started, and playing into a relay
    # nobody is reading from is silence that reports success. With no
    # --room, that's the room the running relay feeds -- skipping the check
    # then is exactly how `spotify play "Spyro Gyra"` played to nobody on
    # 2026-09-25, the Roam still on FIP from `dial`.
    if not args.room and not args.device:
        args.room = spotify_ops.relay_argv_value("--room")
    res = None
    if args.room:
        res, err = _target(args)
        if err is not None:
            return err
    dev, err = _relay_device_or_preview(args, sess, args.room or args.device or args.device_name)
    if err is not None:
        return err
    device_id = dev.get("id", "")
    if res is not None:
        _ensure_room_on_relay(res, args.dry_run)

    if args.dry_run:
        return emit(args, {"would": f"play {label or 'current queue'} on "
                                    f"{dev.get('name')}", "performed": False,
                           "uri": uri, "device": dev.get("name")},
                    f"[dry-run] would play {label or 'the current queue'} "
                    f"on {dev.get('name')}")

    def go():
        sess.play(uri, device_id=device_id)

    np, err = _play_and_check(args, sess, dev, go)
    if err is not None:
        return err
    if res is None:
        # No room named, so nothing re-pointed one; `np` should still follow.
        stations.remember(stations.SPOTIFY)
    if np is not None:
        return emit(args, {"uri": uri, "playing": np.get("track") or label,
                           "device": dev.get("name"), "device_id": device_id,
                           "note": "already playing on this device"},
                    f"▶ {np.get('track') or label}\n   on {dev.get('name')}")
    return emit(args, {"uri": uri, "playing": label or "resumed",
                       "device": dev.get("name"), "device_id": device_id},
                f"▶ {label or 'resumed'}\n   on {dev.get('name')}")


def _transport(args, verb: str, method: str):
    sess, err = _session(args)
    if err is not None:
        return err
    _res, err = _api(args, lambda: getattr(sess, method)())
    if err is not None:
        return err
    return emit(args, {"action": verb}, f"{verb} -> Spotify")


def cmd_pause(args):
    return _transport(args, "pause", "pause")


def cmd_next(args):
    return _transport(args, "next", "next_track")


def cmd_prev(args):
    return _transport(args, "previous", "previous_track")


def cmd_queue(args):
    sess, err = _session(args)
    if err is not None:
        return err
    term = " ".join(args.what)
    try:
        uri, label, _ = _resolve_uri(args, sess, term)
    except spotify.ApiError as exc:
        return fail(args, f"search failed: {exc}")
    if not uri:
        return fail(args, f"nothing on Spotify matched {term!r}")
    _res, err = _api(args, lambda: sess.queue(uri))
    if err is not None:
        return err
    return emit(args, {"queued": uri, "description": label},
                f"queued {label}")


# ---- relay supervision -----------------------------------------------------


def cmd_relay_up(args):
    """Start a detached relay, or report the one already running."""
    state = supervisor.status()
    if state.running:
        return emit(args, state.to_dict(),
                    f"Relay already running (pid {state.pid})")
    relay_args = ["start", "--room", args.room, "--source", args.source,
                  "--device-name", args.device_name, "--duration", "0"]
    if args.volume is not None:
        relay_args += ["--volume", str(args.volume)]
    if args.source_arg:
        relay_args += ["--source-arg", args.source_arg]
    state = supervisor.up(relay_args)
    if not state.running:
        return fail(args, f"the relay did not come up: {state.detail}",
                    f"check {supervisor.OUT_FILE} for what it printed")
    return emit(args, state.to_dict(),
                f"Relay up (pid {state.pid}) on {args.room}\n"
                f"  log: {supervisor.OUT_FILE}\n"
                f"  stop with: uv run twiddle relay down")


def cmd_relay_down(args):
    state = supervisor.down()
    if state.running:
        return fail(args, state.detail,
                    "the room may still be on the relay; restore it with "
                    "`twiddle restore --room roam`")
    return emit(args, state.to_dict(), f"Relay stopped ({state.detail})")


def cmd_relay_status(args):
    state = supervisor.status()
    human = (f"Relay running, pid {state.pid}\n  {state.detail}"
             if state.running else f"No relay running ({state.detail})")
    return emit(args, state.to_dict(), human)


# ---- parser wiring ---------------------------------------------------------


def _add_device_args(p):
    p.add_argument("--device", default=None,
                   help="Spotify device name to play to (default: the relay)")
    p.add_argument("--device-name", default=DEFAULT_DEVICE_NAME,
                   help="the relay's name in Spotify")
    p.add_argument("--cache", default=str(DEFAULT_CACHE))


def register(sub, parents=None):
    kw = {"parents": parents} if parents else {}
    p = sub.add_parser(**kw, name="spotify",
                       help="search and control Spotify playback on a room")
    ssub = p.add_subparsers(dest="spotify_cmd", required=True, metavar="<command>")

    a = ssub.add_parser(**kw, name="auth", help="authorize once, in a browser")
    a.add_argument("--tokens", default=None)
    a.add_argument("--timeout", type=float, default=300)
    a.add_argument("--force", action="store_true",
                   help="authorize again even if tokens are cached")
    a.add_argument("--no-browser", action="store_true",
                   help="print the URL instead of opening it")
    a.add_argument("--client-id", default=None,
                   help="your own Spotify app's client id (saved for reuse)")
    a.add_argument("--redirect", default=None,
                   help=f"its redirect URI (default {spotify.DEFAULT_REDIRECT})")
    a.set_defaults(func=cmd_auth)

    d = ssub.add_parser(**kw, name="devices",
                        help="what Spotify will play to (read-only)")
    d.set_defaults(func=cmd_devices)

    s = ssub.add_parser(**kw, name="search", help="find something (read-only)")
    s.add_argument("query")
    s.add_argument("--type", default="track",
                   choices=["track", "album", "artist", "playlist"])
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_search)

    n = ssub.add_parser(**kw, name="now", help="what is playing (read-only)")
    n.set_defaults(func=cmd_now)

    pl = ssub.add_parser(**kw, name="play",
                         help="play a track, album or playlist on a room")
    pl.add_argument("what", nargs="*",
                    help="a search query, a spotify: URI, or nothing to resume")
    pl.add_argument("--type", default="track",
                    choices=["track", "album", "artist", "playlist"],
                    help="what a bare query should search for")
    pl.add_argument("--volume", type=int, default=None,
                    help="speaker volume, if a relay has to be started")
    pl.add_argument("--no-restore", action="store_true",
                    help="if a relay is started, leave the room on it")
    pl.add_argument("--dry-run", action="store_true")
    _add_device_args(pl)
    add_target_args(pl)
    pl.set_defaults(func=cmd_play)

    from . import discover_cli

    dc = ssub.add_parser(**kw, name="discover",
                         help="search an artist, sample tracks, play one (interactive)")
    dc.add_argument("artist", nargs="*", help="an artist name to search immediately")
    _add_device_args(dc)
    dc.add_argument("--volume", type=int, default=None,
                    help="speaker volume, if a relay has to be started")
    dc.add_argument("--no-restore", action="store_true",
                    help="if a relay is started, leave the room on it")
    dc.add_argument("--dry-run", action="store_true")
    add_target_args(dc, default="roam")
    dc.set_defaults(func=discover_cli.cmd_discover)

    for name, fn, helptext in (("pause", cmd_pause, "pause playback"),
                               ("next", cmd_next, "skip forward"),
                               ("prev", cmd_prev, "skip back")):
        t = ssub.add_parser(**kw, name=name, help=helptext)
        t.set_defaults(func=fn)

    q = ssub.add_parser(**kw, name="queue", help="add something to the queue")
    q.add_argument("what", nargs="+")
    q.add_argument("--type", default="track",
                   choices=["track", "album", "artist", "playlist"])
    q.set_defaults(func=cmd_queue)


def register_relay_supervision(rsub, kw):
    """`relay up/down/status`, attached to the existing `relay` parser."""
    u = rsub.add_parser(**kw, name="up",
                        help="start a detached relay that outlives this command")
    u.add_argument("--source", default="spotify", choices=sorted(relay.SOURCE_HELP))
    u.add_argument("--source-arg", default=None)
    u.add_argument("--device-name", default=DEFAULT_DEVICE_NAME)
    u.add_argument("--volume", type=int, default=None)
    u.add_argument("--room", required=True, help="the room to play on")
    u.set_defaults(func=cmd_relay_up)

    dn = rsub.add_parser(**kw, name="down",
                         help="stop the detached relay and restore the room")
    dn.set_defaults(func=cmd_relay_down)

    st = rsub.add_parser(**kw, name="status", help="is a relay running?")
    st.set_defaults(func=cmd_relay_status)
