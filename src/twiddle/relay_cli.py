"""`twiddle relay` -- put live local audio on the proven playback path.

The commands here are deliberately split by what they touch, because only one
of them writes to a speaker:

| Command | Touches |
|---|---|
| `relay doctor` | nothing -- checks what is installed |
| `relay measure` | starts a source and counts its bytes; no speaker |
| `relay login` | Spotify's OAuth in a browser; no speaker |
| `relay start` | **writes transport and volume**, and keeps running |

`relay start` is the only long-running, speaker-writing command in the
package that a person is expected to leave up for hours, so it takes the
repo's two safety rituals seriously: it snapshots the room before it touches
it and restores it afterwards, and it brackets the whole session in
`journal_span` so `analyse` can discount dropouts this Mac caused. The
restore runs from a `finally` that is reachable from SIGTERM as well as
Ctrl-C -- a relay killed by `launchctl` or a reboot would otherwise leave the
Roams pointed at a dead URL.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import play, relay
from .control_cli import (_acted, _note, _preview, _target, add_target_args,
                          add_write_args, emit, fail)

DEFAULT_CACHE = Path.home() / ".cache" / "twiddle" / "librespot"
DEFAULT_LOG = "logs/relay.jsonl"
DEFAULT_DEVICE_NAME = "Sonos Roam Relay"


class _Terminated(Exception):
    """SIGTERM, raised so the same `finally` handles it as Ctrl-C."""


def _on_sigterm(_sig, _frame):
    raise _Terminated()


# ---- the relay's own log ---------------------------------------------------


class RecordLog:
    """Append-only JSONL that survives its file being replaced underneath it.

    `logs/relay.jsonl` is tracked in git, and a relay runs for days. On
    2026-09-22 a branch checkout swapped the file for the committed copy, and
    the running relay carried on appending to the old, now-unlinked inode:
    two days of samples went where nothing could read them, and stopping the
    relay would have destroyed them. So every write first checks that the
    path still names the file this has open, and reopens it if not.

    Writes come from the sampling loop and the HTTP threads (cover requests),
    hence the lock.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a")

    def _replaced(self) -> bool:
        try:
            return os.stat(self.path).st_ino != os.fstat(self._fh.fileno()).st_ino
        except FileNotFoundError:
            return True

    def write(self, rec: dict) -> None:
        with self._lock:
            if self._replaced():
                self._fh.close()
                self._fh = self.path.open("a")
            self._fh.write(json.dumps(rec) + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


# ---- source selection ------------------------------------------------------


def _build_source(args) -> tuple[list[str], str]:
    """Return (argv, human title) for the chosen source, or raise ValueError."""
    kind = args.source
    if kind == "spotify":
        cache = str(Path(args.cache).expanduser())
        Path(cache).mkdir(parents=True, exist_ok=True)
        hook, _state = relay.install_onevent_hook(cache)
        return relay.spotify_source(args.device_name, cache,
                                    bitrate=args.spotify_bitrate, onevent=hook), \
            f"Spotify via {args.device_name}"
    if kind == "tone":
        freq = float(args.source_arg or 440)
        return relay.tone_source(freq), f"{freq:.0f}Hz test tone"
    if kind == "file":
        if not args.source_arg:
            raise ValueError("--source file needs --source-arg PATH")
        path = Path(args.source_arg).expanduser()
        if not path.exists():
            raise ValueError(f"no such file: {path}")
        return relay.file_source(str(path)), path.name
    if kind == "device":
        name = args.source_arg or ":BlackHole 2ch"
        return relay.device_source(name), f"input {name}"
    raise ValueError(f"unknown source: {kind}")


# ---- read-only commands ----------------------------------------------------


def cmd_doctor(args):
    """Is everything this needs installed? Read-only."""
    checks = relay.check_prerequisites(args.source)
    if args.source == "spotify":
        cache = Path(args.cache).expanduser()
        creds = Path(relay.credentials_path(str(cache)))
        checks.append({
            "name": "spotify credentials", "ok": creds.exists(),
            "detail": str(creds) if creds.exists() else "not logged in yet",
            "fix": "uv run twiddle relay login"})
    ok = all(c["ok"] for c in checks)
    lines = [f"{'ok ' if c['ok'] else 'MISSING'}  {c['name']:20} {c['detail']}"
             + (f"\n          fix: {c['fix']}" if not c["ok"] and c["fix"] else "")
             for c in checks]
    if not ok:
        lines.append("\nFix the MISSING rows above, then re-run `relay doctor`.")
    return emit(args, {"ok": ok, "checks": checks}, "\n".join(lines))


def cmd_login(args):
    """Interactive Spotify OAuth. Writes credentials; touches no speaker.

    Deliberately hands the browser flow to you rather than automating it:
    librespot needs a real Spotify sign-in, and nothing in this repo should
    ever be holding a password.
    """
    cache = Path(args.cache).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    argv = relay.login_argv(args.device_name, str(cache))
    creds = Path(relay.credentials_path(str(cache)))
    if creds.exists() and not args.force:
        # Re-running with a valid cache is ambiguous: librespot may reuse the
        # cached credentials without rewriting them, and this command would
        # then wait for a write that never comes. Say so instead.
        return emit(args, {"credentials": str(creds), "already": True},
                    f"Already signed in -- credentials at {creds}\n"
                    "Pass --force to sign in as a different account.")
    before = creds.stat().st_mtime if creds.exists() else 0
    print("Signing in to Spotify. Browse to the URL below and approve it.")
    print(f"Credentials will be cached at {creds}\n")

    # librespot does not exit once it has authenticated -- it carries on as a
    # live device session. So watch for the credentials file instead of
    # waiting for the process, or a sign-in that worked perfectly gets
    # reported as a timeout.
    try:
        # stdout is PCM in every other use of librespot, but in OAuth mode it
        # prints the URL to visit, so let it through to the terminal.
        proc = subprocess.Popen(argv)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if creds.exists() and creds.stat().st_mtime > before:
                # Give the session a moment to settle, then stop it: this
                # command's job is the credentials, not playback.
                time.sleep(1.0)
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        return fail(args, "sign-in cancelled")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    if not (creds.exists() and creds.stat().st_mtime > before):
        return fail(args, "no credentials were written",
                    "complete the browser step, then re-run `relay login` "
                    f"(or raise --timeout above {args.timeout:.0f}s)")
    return emit(args, {"credentials": str(creds)},
                f"\nSigned in. Credentials cached at {creds}")


# Sources that are clocked by something outside this Mac and therefore
# deliver exactly one second of audio per second. Only these can have their
# PCM format inferred from their byte rate; `tone` and `file` are generated as
# fast as the CPU allows and are rate-limited later, by the jitter buffer.
REALTIME_SOURCES = {"spotify", "device"}


def cmd_measure(args):
    """Count what the source actually produces, without involving a speaker.

    For a real-time source this is the one check that validates sample format,
    rate and channel count at once: PCM in the format this relay assumes is
    exactly 176400 bytes/sec. Half that is mono, a quarter is mono 8-bit, and
    anything else means the ffmpeg input flags disagree with the source --
    all of which otherwise surface as audio playing at the wrong speed rather
    than as an error.

    For a generated source the rate says nothing (it will be thousands of
    times real time), so the verdict says that instead of crying failure.
    """
    try:
        argv, title = _build_source(args)
    except ValueError as exc:
        return fail(args, str(exc))
    realtime = args.source in REALTIME_SOURCES
    print(f"Measuring {title} for {args.seconds:.0f}s...")
    if args.source == "spotify":
        print("  Start something playing on the Connect target now -- an idle\n"
              "  librespot writes nothing, and that is not a failure.")
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    expected = relay.PCM_BYTES_PER_SEC
    # Enough to settle the question either way, and a hard stop so measuring a
    # generated source does not read gigabytes to prove a point.
    cap = int(expected * args.seconds * 20)
    total, t0 = 0, time.monotonic()
    try:
        while time.monotonic() - t0 < args.seconds and total < cap:
            data = proc.stdout.read(8192)
            if not data:
                break
            total += len(data)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    elapsed = time.monotonic() - t0
    rate = total / elapsed if elapsed else 0
    ratio = rate / expected if expected else 0
    if total == 0:
        # A paused Spotify session reads as zero, and that says nothing about
        # the format either way -- so it is not reported as a format failure.
        verdict = "no data -- nothing was playing"
    elif not realtime:
        verdict = (f"generated at {ratio:.0f}x real time, as expected -- the "
                   "relay's pacer rate-limits it. Byte rate cannot check the "
                   "format for this source.")
    elif 0.9 <= ratio <= 1.1:
        verdict = "matches the expected format"
    else:
        verdict = f"UNEXPECTED: {ratio:.2f}x the expected rate"
    return emit(args, {"bytes": total, "seconds": round(elapsed, 2),
                       "bytes_per_sec": round(rate), "expected": expected,
                       "ratio": round(ratio, 3), "realtime_source": realtime,
                       "verdict": verdict},
                f"  {total:,} bytes in {elapsed:.1f}s = {rate:,.0f} B/s "
                f"(real time would be {expected:,})\n  {verdict}")


# ---- the relay itself (WRITES) ---------------------------------------------


def _wait_for_audio(rly: relay.Relay, seconds: float) -> bool:
    """Don't point a speaker at a stream that is not yet producing bytes.

    Sonos gives up on a URL that does not answer with audio promptly, and then
    stays given-up. Proving the encoder is alive first turns a silent failure
    into a message.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if rly.encoded_bytes > 0:
            return True
        if not rly.alive():
            return False
        time.sleep(0.2)
    return rly.encoded_bytes > 0


TRACK_POLL = 1.0      # seconds between looks at which track librespot says is playing
_LIVE = {"PLAYING", "TRANSITIONING"}
IDLE = "idle"                  # what `follow_track` returns once it has cleared the title
IDLE_TITLE = "Spotify (relay)"  # the same title `ensure_room_on_relay` starts a room with
IDLE_AFTER = 4.0               # seconds of `stopped` before the title is cleared


def track_key(track: dict) -> str:
    """Short, URL-safe id of a track: the tail of its `spotify:track:...` URI."""
    return (track.get("uri") or "").rsplit(":", 1)[-1]


def track_title(track: dict) -> str:
    """"Song \u2014 Artist, Artist": what the Sonos app shows for the stream."""
    artists = ", ".join(track.get("artists") or [])
    return f"{track['name']} \u2014 {artists}" if track.get("name") and artists \
        else track.get("name") or IDLE_TITLE


def follow_track(group, rly: relay.Relay, url: str, last: str | None,
                 record) -> str | None:
    """Re-point the room at the relay with the current track's title and cover.

    The Sonos app reads the title and art from the metadata sent when the room
    is pointed, and fetches the art once per URL, so a change of track needs a
    new `SetAVTransportURI` (a journalled write, and an audible blip as the
    speaker refills its buffer). That is why this only fires on a *change*.

    Returns the key of the track now shown. Nothing is sent, and `last` comes
    back unchanged (so it retries next tick), unless the room is still on the
    relay and playing: a room someone has since moved to a radio station, or
    paused, must not be pulled back.
    """
    track = rly.covers.track()
    key = track_key(track)
    if not key:
        return last
    if track.get("state") == "stopped":
        # The song is over and nothing followed it. Clear the title and cover
        # once it has stayed that way a few seconds: a skip or the gap between
        # queued tracks also passes through `stopped` briefly.
        if last in (None, IDLE) or time.time() - track.get("state_ts", 0) < IDLE_AFTER:
            return last
        key, title, art = IDLE, IDLE_TITLE, None
    else:
        title = track_title(track)
        art = None
    if key == last:
        return last
    try:
        now = group.now_playing()
    except Exception:
        return last
    if (now.get("uri") or "").removeprefix(play.RADIO_SCHEME) != url.removeprefix("http://") \
            or now.get("state") not in _LIVE:
        return last
    try:
        if key != IDLE:
            art = rly.cover_url_for(group.ip, key)
        group.play_radio(url, title, art=art)
    except Exception:
        return last         # a title that would not write is not worth the audio: try next tick
    record("relay_retitle", track=track.get("name"), key=key, title=title)
    return key


def cmd_start(args):
    try:
        argv, title = _build_source(args)
    except ValueError as exc:
        return fail(args, str(exc), "see `twiddle relay doctor --source ...`")

    bad = [c for c in relay.check_prerequisites(args.source) if not c["ok"]]
    if bad:
        return fail(args, "missing prerequisites: "
                          + ", ".join(c["name"] for c in bad),
                    "run `twiddle relay doctor` for the fix commands",
                    checks=bad)

    res = None
    if not args.no_speaker:
        res, err = _target(args)
        if err is not None:
            return err
        preview = _preview(args, res, f"relay {title} to this room")
        if preview is not None:
            return preview
    elif getattr(args, "dry_run", False):
        return emit(args, {"would": f"serve {title} on port {args.port}",
                           "performed": False, "source_argv": argv},
                    f"[dry-run] would serve {title} on port {args.port}")

    title = args.title or title
    rly = relay.Relay(source_argv=argv, port=args.port, bitrate=args.bitrate,
                      buffer_seconds=args.buffer)
    log = RecordLog(Path(args.log))

    def record(kind: str, **extra) -> None:
        log.write({"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                   "kind": kind, "source": args.source} | extra)

    if args.source == "spotify":
        state = Path(args.cache).expanduser() / relay.COVER_STATE
        # A cover left over from the last session would be served as this
        # one's until the first track change.
        state.unlink(missing_ok=True)
        rly.covers = relay.Covers(state)
        rly.on_cover = lambda **kw: record("cover_request", **kw)

    try:
        rly.start()
    except Exception as exc:
        log.close()
        return fail(args, f"could not start the relay: {type(exc).__name__}: {exc}")

    snap = None
    prev = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        if not _wait_for_audio(rly, args.start_timeout):
            stats = rly.stats()
            return fail(args, "the encoder produced no audio", # noqa: TRY300
                        "run `twiddle relay measure --source "
                        f"{args.source}` to see what the source is emitting",
                        stats=stats)

        url, snap_path = None, None
        if res is not None:
            url = rly.url_for(res.group.ip)
            # Saved to disk *before* the first write, so the room is always
            # recoverable even if this process never gets to run its cleanup.
            # That is not hypothetical: `uv run` does not forward SIGTERM, so
            # killing the wrapper rather than the CLI takes the child down
            # before the restore. Hence the recovery line in the banner below.
            snap_path = (Path(args.snapshot) if args.snapshot else
                         Path("logs/snapshots") / "relay-before.json")
            snap = res.group.snapshot()
            snap.save(snap_path)
            # The span, not the instant: this Mac is in the audio path for the
            # whole session, so everything observed during it is suspect.
            play.journal_span("relay_start", res.group.ip,
                              source=args.source, url=url)
            if args.volume is not None:
                res.group.set_volume(args.volume)
            res.group.play_radio(url, title, art=rly.cover_url_for(res.group.ip))

        stats = rly.stats()
        record("relay_start", url=url, port=rly.port, bitrate=args.bitrate,
               room=res.group.name if res else None, title=title)
        human = [f"Relay up: {title}",
                 f"  stream   {url or f'http://<this-mac>:{rly.port}{relay.STREAM_PATH}'}",
                 f"  encoder  {args.bitrate} CBR MP3"]
        if res is not None:
            human.append(f"  playing  {res.group.name}{_note(res)}")
        if args.source == "spotify":
            human.append(f"\nOpen Spotify and cast to \"{args.device_name}\".")
            human.append("Playback control stays in the app. Pausing there goes")
            human.append("quiet here without dropping the stream.")
        human.append("\nCtrl-C to stop" +
                     ("" if args.no_restore or res is None
                      else " (the room is restored to how it was found)."))
        if snap_path is not None and not args.no_restore:
            human.append(f"If this is killed before it can tidy up:\n"
                         f"  uv run twiddle restore --room {args.room or res.group.name} "
                         f"--path {snap_path}")
        emit(args, _acted(res, url=url, port=rly.port, title=title,
                          stats=stats, snapshot=str(snap_path)) if res else
             {"url": url, "port": rly.port, "title": title, "stats": stats},
             "\n".join(human))

        deadline = time.monotonic() + args.duration * 60 if args.duration else None
        last_track = None
        next_sample = time.monotonic() + args.interval
        while deadline is None or time.monotonic() < deadline:
            time.sleep(min(TRACK_POLL, args.interval))
            if res is not None and rly.covers is not None:
                last_track = follow_track(res.group, rly, url, last_track, record)
            if time.monotonic() < next_sample:
                continue
            next_sample = time.monotonic() + args.interval
            stats = rly.stats()
            state = (play.transport_info(res.group.ip)
                     .get("CurrentTransportState", "?") if res else "-")
            record("relay_sample", state=state, **stats)
            if not rly.alive():
                print("\n  !! the source or encoder exited -- stopping")
                record("relay_source_died", **stats)
                break
            if not args.json:
                print(f"  {state:12} listeners {stats['listeners']} "
                      f"buffer {stats['buffer_bytes']:6} "
                      f"silence {stats['silence_s']:6.1f}s "
                      f"sent {stats['encoded_bytes'] / 1e6:6.1f}MB   ",
                      end="\r", flush=True)
    except (KeyboardInterrupt, _Terminated):
        print("\nStopping.")
    finally:
        signal.signal(signal.SIGTERM, prev)
        final = rly.stats()
        record("relay_end", **final)
        if res is not None:
            play.journal_span("relay_end", res.group.ip, source=args.source)
            if not args.no_restore and snap is not None:
                try:
                    result = res.group.restore(snap)
                    print("Restored: " + "; ".join(result["restored"]))
                    if result["problems"]:
                        print("  PROBLEMS: " + "; ".join(result["problems"]))
                except Exception as exc:
                    print(f"  could not restore: {type(exc).__name__}: {exc}",
                          file=sys.stderr)
            elif not args.no_restore:
                play.stop(res.group.ip)
        rly.stop()
        log.close()
    return 0


# ---- parser wiring ---------------------------------------------------------


def _add_source_args(p, *, with_spotify_opts: bool = True):
    p.add_argument("--source", default="spotify",
                   choices=sorted(relay.SOURCE_HELP),
                   help="; ".join(f"{k}: {v}" for k, v in relay.SOURCE_HELP.items()))
    p.add_argument("--source-arg", default=None,
                   help="file path, input device name, or tone frequency")
    if with_spotify_opts:
        p.add_argument("--cache", default=str(DEFAULT_CACHE),
                       help="where librespot keeps credentials and audio")
        p.add_argument("--device-name", default=DEFAULT_DEVICE_NAME,
                       help="the name this shows up as in the Spotify app")


def register(sub, parents=None):
    """Attach `relay` and its subcommands."""
    kw = {"parents": parents} if parents else {}
    p = sub.add_parser(**kw, name="relay",
                       help="stream live local audio (Spotify, a tone, a file) "
                            "to a room")
    rsub = p.add_subparsers(dest="relay_cmd", required=True, metavar="<command>")

    d = rsub.add_parser(**kw, name="doctor",
                        help="check what this needs is installed (read-only)")
    _add_source_args(d)
    d.set_defaults(func=cmd_doctor)

    l = rsub.add_parser(**kw, name="login",
                        help="sign in to Spotify once, in a browser")
    l.add_argument("--cache", default=str(DEFAULT_CACHE))
    l.add_argument("--device-name", default=DEFAULT_DEVICE_NAME)
    l.add_argument("--timeout", type=float, default=300)
    l.add_argument("--force", action="store_true",
                   help="sign in again even if credentials are cached")
    l.set_defaults(func=cmd_login)

    m = rsub.add_parser(**kw, name="measure",
                        help="confirm a source's byte rate (no speaker)")
    _add_source_args(m)
    m.add_argument("--seconds", type=float, default=5)
    m.set_defaults(func=cmd_measure)

    s = rsub.add_parser(**kw, name="start",
                        help="run the relay and point a room at it (WRITES)")
    _add_source_args(s)
    s.add_argument("--title", default=None, help="what the speaker displays")
    s.add_argument("--port", type=int, default=relay.DEFAULT_PORT)
    s.add_argument("--bitrate", default=relay.DEFAULT_BITRATE,
                   help="MP3 CBR bitrate, e.g. 192k")
    s.add_argument("--spotify-bitrate", default="320", choices=["96", "160", "320"],
                   help="what librespot pulls from Spotify")
    s.add_argument("--buffer", type=float, default=2.0,
                   help="seconds of jitter buffer ahead of the encoder")
    s.add_argument("--volume", type=int, default=None)
    s.add_argument("--duration", type=float, default=0,
                   help="minutes to run (0 = until interrupted)")
    s.add_argument("--interval", type=float, default=10,
                   help="seconds between status samples")
    s.add_argument("--log", default=DEFAULT_LOG)
    s.add_argument("--snapshot", default=None,
                   help="where to save the room's state before starting")
    s.add_argument("--no-restore", action="store_true",
                   help="leave the room on the relay when this exits")
    s.add_argument("--no-speaker", action="store_true",
                   help="serve the stream but do not touch any speaker")
    s.add_argument("--start-timeout", type=float, default=15,
                   help="seconds to wait for the encoder before giving up")
    add_target_args(s)
    add_write_args(s)
    s.set_defaults(func=cmd_start)

    # `up`/`down`/`status` live in spotify_cli because that is where the
    # supervised relay is actually used, but they belong on this parser.
    from .spotify_cli import register_relay_supervision
    register_relay_supervision(rsub, kw)
