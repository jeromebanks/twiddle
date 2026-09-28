"""Spotify playback operations with no command line attached.

`spotify_cli` grew these as CLI helpers: they took argparse `args`, printed
progress to stderr, and reported failure as an exit code via `fail()`. That
is fine for a command, and useless inside a TUI, where stdout is captured and
an exit code has nowhere to go -- errors would simply vanish.

So the logic lives here, shaped for any caller: explicit parameters, failures
raised as `PlaybackError` (message + hint, the same two things `fail()`
prints), and progress reported through an optional `notify` callback. The
CLI functions in `spotify_cli` are thin wrappers over these and behave
exactly as before; `scene` calls them directly.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable

from . import play, relay, spotify, stations, supervisor

Notify = Callable[[str], None]


def _quiet(_msg: str) -> None:
    pass


class PlaybackError(RuntimeError):
    """A failure worth showing a person: what went wrong, and what fixes it."""

    def __init__(self, message: str, hint: str = "", status: int | None = None,
                 **extra):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.status = status
        self.extra = extra


def hint_for(exc: Exception) -> str:
    """What to do about a Spotify API or auth failure."""
    if isinstance(exc, spotify.AuthError):
        if str(exc) == spotify.NO_CLIENT_ID:
            return "set up your own Spotify app first (the steps above)"
        return "run `twiddle spotify auth` again"
    if not isinstance(exc, spotify.ApiError):
        return ""
    if exc.status == 404:
        return ("no active device -- start one with `twiddle relay up "
                "--room roam`, or pass --device")
    if exc.status == 403:
        return "Spotify refused this; Premium is required for playback control"
    if exc.status == 429:
        wait = getattr(exc, "retry_after", 0)
        hint = (f"rate limited by Spotify; retry in {wait:.0f}s"
                if wait else "rate limited by Spotify; wait and retry")
        return hint
    return ""


def as_playback_error(exc: Exception) -> PlaybackError:
    """An ApiError/AuthError, restated the way a person should see it."""
    if isinstance(exc, spotify.ApiError):
        return PlaybackError(f"Spotify API {exc}", hint_for(exc), status=exc.status)
    return PlaybackError(str(exc), hint_for(exc))


def not_signed_in(exc: BaseException) -> bool:
    """Is this "no usable Spotify sign-in" (never signed in, or it expired)?

    `session()` and `as_playback_error` keep the AuthError as the cause.
    """
    return isinstance(exc, spotify.AuthError) or isinstance(exc.__cause__, spotify.AuthError)


def session() -> spotify.Session:
    try:
        return spotify.Session.load()
    except spotify.AuthError as exc:
        raise PlaybackError(str(exc),
                            "run `uv run twiddle spotify auth` once; it needs "
                            "one browser approval and then runs unattended") from exc


# ---- the relay as a Spotify device ------------------------------------------


def relay_device(sess, *, device: str | None, device_name: str,
                 room: str | None = None, cache: str = "",
                 no_restore: bool = False, volume: int | None = None,
                 notify: Notify = _quiet) -> dict:
    """Find the relay's librespot in Spotify's device list, starting it if asked.

    With `room`, a relay is started if none is running -- the only place in
    this package where asking to play something can start a background
    process, which is why it says so through `notify` rather than doing it
    quietly.
    """
    name = device_name
    devices = sess.devices()
    dev = spotify.find_device(devices, device) if device else \
        spotify.find_device(devices, name)
    if dev:
        return dev
    if not room:
        known = [d.get("name") for d in devices]
        raise PlaybackError(
            f"no Spotify device named {device or name!r}",
            "pass --room to start a relay, or --device with one of the names "
            "from `twiddle spotify devices`", devices=known)

    state = supervisor.status()
    if not state.running:
        notify(f"No relay running -- starting one on {room}...")
        relay_args = ["start", "--room", room, "--source", "spotify",
                      "--device-name", name, "--cache", str(cache),
                      # 0 = run until stopped. A supervised relay has no
                      # business timing out halfway through an album.
                      "--duration", "0"]
        if no_restore:
            relay_args.append("--no-restore")
        if volume is not None:
            relay_args += ["--volume", str(volume)]
        state = supervisor.up(relay_args)
        if not state.running:
            raise PlaybackError(f"could not start a relay: {state.detail}",
                                f"run `twiddle relay up --room {room}` to see why")
    # librespot registers with Spotify a moment after the process is up, so
    # the device list needs a beat before it shows the new target.
    for _ in range(20):
        try:
            dev = spotify.find_device(sess.devices(), name)
        except spotify.ApiError as exc:
            if exc.status == 429:
                # Do not poll into a rate limit. Each retry inside the penalty
                # window extends it, which is how a 35s wait becomes minutes.
                raise PlaybackError(
                    f"rate limited while waiting for {name!r} to appear",
                    f"wait {exc.retry_after or 60:.0f}s, then "
                    "`twiddle spotify devices`", status=429) from exc
            raise
        if dev:
            return dev
        time.sleep(1.0)
    raise PlaybackError(
        f"the relay is running but Spotify does not list {name!r}",
        "check `twiddle spotify devices`; librespot may still be connecting")


def relay_argv_value(flag: str) -> str | None:
    """A flag's value from the supervised relay's recorded argv, if any."""
    argv = (supervisor.status().record or {}).get("argv", [])
    if flag in argv and argv.index(flag) + 1 < len(argv):
        return argv[argv.index(flag) + 1]
    return None


def relay_url(group) -> str:
    """The relay's own stream URL, built the same way `relay_cli.cmd_start` does.

    The port stays put across a restart (`relay.DEFAULT_PORT` unless
    overridden), so it's read back from the supervised relay's recorded
    argv rather than assumed.
    """
    port = int(relay_argv_value("--port") or relay.DEFAULT_PORT)
    return f"http://{play.local_ip_for(group.ip)}:{port}{relay.STREAM_PATH}"


def relay_cover_url(stream_url: str, timeout: float = 1.0) -> str | None:
    """The relay's cover URL, but only if the running relay really serves one.

    A relay started before `/cover.jpg` existed answers *every* path with the
    endless MP3 stream. A speaker or app fetching "the cover" from one of those
    would sit on the fan-out as a listener that never reads, and its queue
    overflowing would count as `dropped_chunks` -- the experiment's own signal
    for a Roam that stopped reading. So ask first, with HEAD, which the old
    relay answers with headers alone and never subscribes.

    An image, or a 404 (a new relay, before the first track change), means
    yes. Audio, or no answer, means no cover.
    """
    url = stream_url.replace(relay.STREAM_PATH, relay.COVER_PATH)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"),
                                    timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return url if exc.code == 404 else None
    except Exception:
        return None
    return url if ctype.startswith("image/") else None


def ensure_room_on_relay(group, dry_run: bool, want: str | None = None,
                         notify: Notify = _quiet) -> bool:
    """Re-point the room at the relay's stream, but only if it isn't already.

    `Group.now_playing()` is a pure read (no journalling), so checking first
    costs nothing -- and it avoids an audible drop-and-reopen plus a
    journalled write, which `analyse` then discounts for 45s, on every
    single track pick. Sampling ten tracks unconditionally would quietly
    poison minutes of the exact dropout data this project exists to collect.

    Returns True when the room was (or, dry-run, would be) re-pointed.
    """
    want = want or relay_url(group)
    want_bare = want.removeprefix("http://")
    have = group.now_playing().get("uri", "") or ""
    have = have.removeprefix(play.RADIO_SCHEME)
    if dry_run:
        if have != want_bare:
            notify(f"[dry-run] would re-point {group.name} to the relay "
                   f"({have or 'nothing'} -> {want_bare})")
        return have != want_bare
    if have != want_bare:
        # The DIDL (and so the cover URL) is only ever sent here, alongside
        # the URI -- a room already on the relay keeps whatever it was given.
        group.play_radio(want, "Spotify (relay)", art=relay_cover_url(want))
    stations.remember(stations.SPOTIFY)
    return have != want_bare


def play_and_check(sess, dev: dict, play_fn: Callable[[], object]) -> dict | None:
    """Run a play call, tolerating the transfer-then-play 403 race.

    Passing `device_id` to /play already moves playback to that device, and
    doing it as two calls (transfer, then play) races: the transfer can
    auto-resume, and the play that follows is then rejected with `403
    Restriction violated` -- an error reported for something that in fact
    worked. So a failure here is usually that race rather than a real
    refusal; ask what is actually happening before calling it a failure.

    Returns the now-playing dict when the race was detected and playback is
    in fact under way on `dev`, None on a clean success. Re-raises the
    original ApiError when playback really did fail.
    """
    try:
        play_fn()
        return None
    except spotify.ApiError as exc:
        try:
            np = spotify.now_playing(sess.current())
        except spotify.ApiError:
            raise exc from None
        if np.get("playing") and np.get("device") == dev.get("name"):
            return np
        raise
