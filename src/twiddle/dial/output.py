"""Where `dial`'s sound goes: a Sonos room, or this Mac's speakers.

`Output` is the extension point, like `scene`'s `Player`. Every method may
raise `spotify_ops.PlaybackError` (message + hint) for the UI to show, and
all of them touch the network, so call them from a worker thread. A new
kind of place to play -- a Bluetooth device, a socket -- is an `Output`;
one fed by a local process only needs `ProcessOutput.argv`, and
`Outputs.register` puts it in the picker.

## One stream at a time

Start everything through `Outputs.play`, never `Output.play` directly.
`Outputs` remembers what it started where, and once something starts on
one output it stops the others -- but only an output still playing what
*we* put there (same stream, `same_stream`). One that has since moved to
the relay, or to anything you chose yourself, is left alone. It starts
the new one first, so a refusal (`NeedsConfirmation`) or a failure leaves
the old one playing rather than leaving silence; the confirmed retry stops
it. `movable` is what switching outputs (`d`) carries across.

## Tuning away from the relay

While the relay experiment runs, the Roams play Spotify through the relay.
Tuning them to a station takes them off it. Spotify plays no part in the
radio itself (the room fetches the station's stream directly), so it is
only consulted when the room is on the relay, and only matters when music
is actually playing through it:

- then the first `tune` raises `NeedsConfirmation`, and a second one goes
  ahead, pausing Spotify rather than leaving it playing into a stream
  nobody is reading -- journalled (`spotify_paused_for_radio`) next to the
  room's own journalled `set_uri`, so `analyse` discounts the switch;
- a relay with nothing playing is only silence, and is tuned over at once;
- `back_to_relay` (`R`) re-points the room and resumes Spotify there.

It is deliberately *not* a journal span. A span says "this Mac is in the
audio path, discount everything inside"; radio on the Roam is the opposite
-- the Mac is out of the path, and a dropout on radio is exactly the organic
evidence the Sonos diagnostics are built on. `relay.jsonl` shows the switch on its
own: listeners falls to 0 (so `dropped_chunks` cannot climb).
"""
from __future__ import annotations

import functools
import itertools
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .. import here, play, spotify_ops, stations
from ..gain import LiveGain
from ..scene.players import NeedsConfirmation
from ..stations import Station
from . import state as dial_state

MAC = "mac"
MAC_LABEL = here.LABEL
RELAY = "relay"
PlaybackError = spotify_ops.PlaybackError


@dataclass(frozen=True)
class Media:
    """Something to play: a station (endless) or a track (it ends)."""
    url: str
    name: str
    art: str | None = None
    station: str | None = None      # a station key; None for a track

    @classmethod
    def of(cls, station: Station) -> Media:
        return cls(station.url, station.name, station.logo, station.key)

    @classmethod
    def track(cls, url: str, name: str, art: str | None = None) -> Media:
        return cls(url, name, art)


@dataclass
class OutputState:
    tuned: str | None = None     # a station key, RELAY, or None (off / something else)
    playing: bool = False
    volume: int | None = None
    muted: bool = False
    other: str = ""              # what it's on, when that's not a station
    sleep_s: int | None = None   # seconds left on the sleep timer; None when off
    uri: str = ""                # what it is fetching, for `same_stream`


class Output(Protocol):
    id: str
    label: str
    def state(self) -> OutputState: ...
    def play(self, media: Media, *, confirmed: bool = False, source: str = "dial") -> str: ...
    def stop(self) -> str: ...
    def set_volume(self, volume: int) -> int: ...
    def set_mute(self, muted: bool) -> None: ...
    def set_sleep_timer(self, seconds: int) -> int | None: ...
    def back_to_relay(self) -> str: ...
    def capture(self): ...
    def restore(self, token, *, volume_only: bool = False) -> str: ...
    def close(self) -> None: ...


def bare(uri: str | None) -> str:
    """A stream URI with its scheme gone, so Sonos's and ours compare equal."""
    uri = uri or ""
    for prefix in (play.RADIO_SCHEME, "http://", "https://"):
        uri = uri.removeprefix(prefix)
    return uri


def same_stream(a: str | None, b: str | None) -> bool:
    """Is a speaker still fetching the URL we gave it? Scheme and query
    aside: a Sonos reports a station back under its radio scheme, and a
    Bandcamp URL's query is a token that may come back re-encoded."""
    a, b = bare(a).split("?")[0], bare(b).split("?")[0]
    return bool(a) and a == b


def station_for(uri: str | None) -> Station | None:
    b = bare(uri)
    return next((s for s in stations.STATIONS.values() if b and bare(s.url) == b), None)


def _clamp(v: int) -> int:
    return max(0, min(100, int(v)))


class BaseOutput:
    """What every output shares; a new one subclasses this and fills in
    `state`, `play`, `stop`, and whichever controls it really has."""
    id = ""
    label = ""
    dry_run = False
    # How often a held volume key may reach `set_volume`: a Sonos write is a
    # journalled SOAP call, so it is paced; a pipe write is free.
    volume_interval_s = 0.3

    def tune(self, station: Station, *, confirmed: bool = False) -> str:
        return self.play(Media.of(station), confirmed=confirmed)

    def play_url(self, url: str, name: str, *, art: str | None = None,
                 confirmed: bool = False, source: str = "scene") -> str:
        return self.play(Media.track(url, name, art), confirmed=confirmed, source=source)

    def set_volume(self, volume: int) -> int:
        raise PlaybackError(f"{self.label} has no volume control here")

    def set_mute(self, muted: bool) -> None:
        raise PlaybackError(f"{self.label} has no mute here")

    def set_sleep_timer(self, seconds: int) -> int | None:
        raise PlaybackError(f"{self.label} has no sleep timer")

    def back_to_relay(self) -> str:
        raise PlaybackError("R is for a Sonos room", "press d and pick the Roam first")

    def capture(self):
        """What is playing here now, to put back later (`restore`); None when this
        output can't say. Only a Sonos room can: another process's ffmpeg is not ours."""
        return None

    def restore(self, token, *, volume_only: bool = False) -> str:
        raise PlaybackError(f"{self.label} can't put back what was playing")

    def disconnect(self) -> str:
        """Let go of this output when you say so (`D`): a plain stop, unless
        the output can leave nothing behind (`SonosOutput`). A handoff never
        calls this: it stops, and the speaker keeps its resume point."""
        return self.stop()

    def close(self) -> None:
        pass


# ---- a Sonos room -------------------------------------------------------------


class SonosOutput(BaseOutput):
    def __init__(self, room: str, group_factory: Callable[[], object], *,
                 dry_run: bool = False, spotify_player=None,
                 relay_url: Callable[[object], str] = spotify_ops.relay_url):
        self.room = room
        self.id = f"room:{room}"
        self._group_factory = group_factory
        self._group = None
        self._lock = threading.Lock()
        self.dry_run = dry_run
        self._spotify_player = spotify_player
        self._relay_url = relay_url

    @property
    def label(self) -> str:
        return self._group.name if self._group is not None else self.room

    @property
    def group(self):
        with self._lock:
            if self._group is None:
                try:
                    self._group = self._group_factory()
                except Exception as exc:
                    raise PlaybackError(f"couldn't find the room {self.room!r}: {exc}",
                                        "is it on? `twiddle rooms` lists what answers") from exc
            return self._group

    @property
    def spotify(self):
        if self._spotify_player is None:
            from ..scene.players import SpotifyConnectPlayer
            self._spotify_player = SpotifyConnectPlayer(room=self.room)
        return self._spotify_player

    def _relay_bare(self, g) -> str:
        try:
            return bare(self._relay_url(g))
        except Exception:
            return ""

    def state(self) -> OutputState:
        g = self.group
        try:
            info = play.transport_info(g.ip)
            uri = play.media_info(g.ip).get("CurrentURI", "") or info.get("TrackURI", "")
            vol = play.get_volume(g.ip)
            muted = play.get_mute(g.ip)
        except Exception as exc:
            raise PlaybackError(f"{g.name} didn't answer: {type(exc).__name__}") from exc
        try:
            sleep_s = play.get_sleep_timer(g.ip)
        except Exception:
            sleep_s = None      # garnish: never let it cost the rest of the state
        st = station_for(uri)
        tuned = st.key if st else (RELAY if uri and bare(uri) == self._relay_bare(g) else None)
        return OutputState(tuned=tuned, playing=info.get("CurrentTransportState") == "PLAYING",
                           volume=vol if vol >= 0 else None, muted=muted,
                           other="" if st else ("Spotify (relay)" if tuned == RELAY else bare(uri)),
                           sleep_s=sleep_s, uri=bare(uri))

    def play(self, media: Media, *, confirmed: bool = False, source: str = "dial") -> str:
        """A station, or a track -- a Bandcamp one from `scene` -- with the
        same relay safety. A track is not `remember`ed as the last station,
        nor saved as dial's `left_relay`: those are read back as station keys."""
        g = self.group
        now = self.state()
        if media.station is None:
            return self._play(media.url, media.name, media.art, now, confirmed,
                              source=source, track=True)
        if now.tuned == media.station and now.playing:
            return f"{g.name} is already on {media.name}"
        msg = self._play(media.url, media.name, media.art, now, confirmed, source=source)
        if not self.dry_run:
            stations.remember(media.station)
            if now.tuned == RELAY:
                dial_state.set_state("left_relay", {"room": g.name, "station": media.station})
        return msg

    def _play(self, url: str, name: str, art: str | None, now: OutputState,
              confirmed: bool, source: str = "dial", track: bool = False) -> str:
        g = self.group
        # Spotify has no part in playing a station: the room fetches the
        # stream itself. It matters only if the room is on the relay *and*
        # Spotify is actually playing music through it -- then tuning would
        # cut off what you're listening to, so ask. A relay with nothing
        # playing is just silence; tune straight over it.
        spotify_np = self._relay_spotify() if now.tuned == RELAY else None
        if spotify_np is not None and not confirmed:
            what = spotify_np.get("track") or "Spotify"
            key = "enter" if source == "dial" else "p"
            raise NeedsConfirmation(
                f"{what} is playing on {g.name} through the relay. Press {key} again "
                f"to switch to {name} (Spotify pauses; R brings it back).")
        if self.dry_run:
            return (f"[dry-run] would play {name} on {g.name}"
                    + (" and pause Spotify on the relay" if spotify_np else ""))
        note = self._pause_relay_spotify(g, spotify_np, source) if spotify_np else ""
        try:
            if track:
                # A finite file, as itself: the radio scheme would make the
                # speaker fetch plain http, and Bandcamp's CDN only 301s
                # that to https -- a redirect a Sonos may not follow.
                g.play_url(url, play.track_didl(name, art, url=url))
            else:
                g.play_radio(url, name, art=art)
        except Exception as exc:
            raise PlaybackError(f"couldn't start {name} on {g.name}: "
                                f"{type(exc).__name__}: {exc}") from exc
        return f"▶ {name} on {g.name}" + note

    def _relay_spotify(self) -> dict | None:
        """What Spotify is playing through the relay, or None if nothing is.

        If Spotify can't be asked, assume nothing: the room is on the relay
        either way, and an unanswerable question must not block the radio.
        """
        try:
            np = self.spotify.now()
        except PlaybackError:
            return None
        return np if self.spotify.relay_is_live(np) else None

    def _pause_relay_spotify(self, g, np: dict, source: str = "dial") -> str:
        try:
            self.spotify.pause()
        except PlaybackError as exc:
            return f"  (Spotify not paused: {exc.message})"
        play._journal("spotify_paused_for_radio", g.ip, room=g.name, source=source,
                      was_playing=np.get("uri", ""), track=np.get("track", ""))
        return "  · Spotify paused on the relay (R resumes)"

    def back_to_relay(self) -> str:
        g = self.group
        if self.dry_run:
            return f"[dry-run] would put {g.name} back on the relay and resume Spotify"
        try:
            spotify_ops.ensure_room_on_relay(g, False)
        except Exception as exc:
            raise PlaybackError(f"couldn't re-point {g.name} at the relay: {exc}",
                                "is it running? `twiddle relay status`") from exc
        relay = next((d for d in self.spotify.devices() if d.relay and d.id), None)
        if relay is None:
            raise PlaybackError(f"{g.name} is back on the relay, but Spotify doesn't list it",
                                "is it running? `twiddle relay status`")
        sess = self.spotify.session()
        play._journal("spotify_resume_on_relay", g.ip, room=g.name, source="dial")
        try:
            spotify_ops.play_and_check(sess, {"id": relay.id, "name": relay.name},
                                       lambda: sess.transfer(relay.id, play=True))
        except Exception as exc:
            raise spotify_ops.as_playback_error(exc) from exc
        dial_state.set_state("left_relay", None)
        return f"▶ {g.name} back on the relay"

    def capture(self):
        """The room as found: what it plays, where, how loud (`household.Snapshot`).
        `relay`: it was on the relay, which Spotify's own handback puts back."""
        from ..household import Snapshot
        if self.dry_run:
            return None
        try:
            on_relay = self.state().tuned == RELAY
            return {"snapshot": Snapshot.capture(self.group), "relay": on_relay}
        except Exception:
            return None                 # unreadable: nothing to promise to put back

    def restore(self, token, *, volume_only: bool = False) -> str:
        """Put the room back as `capture` found it. `volume_only`: only the volume
        and mute (a relay room's stream comes back through Spotify, not here).
        The writes are journalled by `play.*`, like every other."""
        snap = token["snapshot"]
        if self.dry_run:
            return f"[dry-run] would put {self.label} back"
        g = self.group
        if volume_only:
            import copy
            snap = copy.copy(snap)
            snap.uri = ""
        done = snap.restore(g)
        if done["problems"]:
            raise PlaybackError(f"couldn't fully put {g.name} back: " + ", ".join(done["problems"]))
        return f"▶ {g.name} back as it was" if snap.uri else f"{g.name} volume back"

    def stop(self) -> str:
        g = self.group
        if self.state().tuned == RELAY:
            raise PlaybackError(f"{g.name} is on the relay",
                                "pause Spotify instead (`shows`, or the phone)")
        if self.dry_run:
            return f"[dry-run] would stop {g.name}"
        g.stop()
        return f"■ {g.name} stopped"

    def disconnect(self) -> str:
        """Stop, then clear the transport URI, so the Sonos app shows nothing
        instead of the station sitting there paused. Refuses the relay, like
        `stop`. The clear is best effort: a speaker that refuses an empty URI
        is left stopped, and we say so."""
        msg = self.stop()
        if self.dry_run:
            return msg.replace("would stop", "would disconnect")
        try:
            play.set_uri(self.group.ip, "")
        except Exception:
            return f"■ {self.group.name} stopped (the speaker kept its station)"
        return f"⏏ {self.group.name} disconnected"

    def set_volume(self, volume: int) -> int:
        volume = _clamp(volume)
        if not self.dry_run:
            self.group.set_volume(volume)     # every member, journalled
        return volume

    def set_mute(self, muted: bool) -> None:
        if not self.dry_run:
            self.group.set_mute(muted)

    def set_sleep_timer(self, seconds: int) -> int | None:
        """The speaker's own timer: it fires even if dial is long gone."""
        g = self.group
        if self.dry_run:
            return seconds or None
        try:
            play.set_sleep_timer(g.ip, seconds, source="dial")
            return play.get_sleep_timer(g.ip)
        except Exception as exc:
            raise PlaybackError(f"sleep timer on {g.name}: {type(exc).__name__}: {exc}") from exc

    # close: the room keeps playing after the app quits, like `tune`


# ---- this Mac -----------------------------------------------------------------


def _osascript(script: str) -> str:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                          timeout=5, check=False).stdout.strip()


def _pactl(*args: str) -> str:
    # Linux, including a Chromebook's Linux container: PulseAudio, or
    # PipeWire's PulseAudio server, which is what ChromeOS gives it.
    return subprocess.run(["pactl", *args], capture_output=True, text=True,
                          timeout=5, check=False).stdout.strip()


def _spawn(argv: list[str], log: Path) -> subprocess.Popen:
    # A log file, never the terminal: anything ffmpeg prints would draw over the TUI.
    # stdin is a pipe: it is how `ProcessOutput.set_volume` reaches the running process.
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out,
                                stderr=subprocess.STDOUT)


@functools.lru_cache(maxsize=8)
def _fast_commands(ffmpeg: str | None) -> bool:
    """Does this ffmpeg take `-stats_period`? ffmpeg reads one interactive
    command per wake of its main loop, which is every stats period (0.5s by
    default): a held volume key would queue up stale levels. At 0.05s it
    applies about one every 70ms."""
    if not ffmpeg:
        return False
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "quiet", "-stats_period", "0.05",
                            "-f", "lavfi", "-i", "anullsrc", "-t", "0.01", "-f", "null", "-"],
                           capture_output=True, timeout=10, check=False)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class ProcessOutput(BaseOutput):
    """An output that is a local process fed the stream's URL.

    Subclasses give `argv`. The process is ours: it stops when the app quits.
    `ffmpeg -f audiotoolbox` is the one for this Mac, with
    `-audio_device_index N` for a Bluetooth speaker; `ffmpeg -f mp3
    tcp://host:port` (a socket) would be another `argv`.

    ## Volume and mute are twiddle's own

    A `gain.LiveGain`: an ffmpeg filter that `set_volume` retunes on the
    running process, never the system volume. Build `argv` with
    `gain_args()`; the process is spawned with a stdin pipe. The level is
    kept here, so every respawn (next station, `d`, a Bluetooth reconnect)
    starts at it.
    """
    log_name = "output.log"
    volume_interval_s = 0.03

    def __init__(self, *, dry_run: bool = False, spawn=_spawn):
        self.dry_run = dry_run
        self._spawn = spawn
        self._proc = None
        self._media: Media | None = None
        self._lock = threading.Lock()
        self._sleep: threading.Timer | None = None
        self._sleep_at: float | None = None      # monotonic deadline
        self.gain = LiveGain()
        self.gain_pace_s: float | None = None     # set from ffmpeg's rate by gain_args
        self._gain_wake = threading.Event()
        self._gain_thread: threading.Thread | None = None
        self._gain_start = threading.Lock()
        self._argv_gain: tuple[int, bool] | None = None
        self._log_path: Path | None = None
        self._acked = False             # this process has a log to read acks from
        self._gain_broken: tuple | None = None   # (proc, why): acks became unreadable
        self._closed = False

    def argv(self, media: Media) -> list[str]:
        raise NotImplementedError

    def gain_args(self) -> list[str]:
        """The filter `set_volume` later retunes; put it in every `argv`."""
        fast = _fast_commands(getattr(self, "_ffmpeg", None))
        if self.gain_pace_s is None:
            self.gain_pace_s = 0.08 if fast else 0.55    # one command per ffmpeg wake
        # One snapshot for both the filter and the later "did it change?"
        # comparison: reading the live state twice could disagree.
        snap = (self.gain.volume, self.gain.muted)
        self._argv_gain = snap
        return (["-stats_period", "0.05"] if fast else []) + LiveGain(*snap).args()

    def _push_gain(self) -> None:
        """Ask for the current level to reach the process. One sender thread
        sends only the latest level and waits for ffmpeg to acknowledge it
        before the next, so a held key never queues stale commands, and a
        stalled pipe can block only that thread -- never stop, respawn or
        close. Nothing running is fine: the level is kept for the next `argv`
        (and `play` re-asks if it changed while the process was starting)."""
        self._gain_wake.set()
        with self._gain_start:          # never blocks on a pipe, so cheap
            if self._gain_thread is None or not self._gain_thread.is_alive():
                self._gain_thread = threading.Thread(target=self._gain_loop, daemon=True,
                                                     name="gain-sender")
                self._gain_thread.start()

    @staticmethod
    def _replies(log: Path) -> int:
        """Commands ffmpeg has answered, counted in its own log (one per
        process, so nothing else's replies are counted): it prints "Command
        reply" for each one it applies. -1: no readable log."""
        try:
            return log.read_bytes().count(b"Command reply")
        except OSError:
            return -1

    def _gain_loop(self) -> None:
        while self._gain_wake.wait() and not self._closed:
            self._gain_wake.clear()
            proc, log, acked = self._proc, self.log, self._acked    # read once
            if proc is None or proc.poll() is not None:
                continue                # a respawn gets the level via argv
            if self._gain_broken and self._gain_broken[0] is proc:
                continue                # no ack can be read: sending blind would queue
            before = self._replies(log) if acked else -1
            if acked and before < 0:
                self._lose_acks(proc)
                continue
            if not self.gain.push(proc):
                continue
            if not acked:
                # A process with no log by design (a fake in a test): the
                # only fallback is a pause, as long as ffmpeg's slowest wake.
                time.sleep(self.gain_pace_s or 0.55)
                continue
            # The ack. One command stays outstanding until ffmpeg answers it
            # -- however long that takes (it may be busy opening the input) --
            # or the process is gone/replaced. Timing out and sending more is
            # exactly what builds a backlog.
            while not self._closed and self._proc is proc and proc.poll() is None:
                n = self._replies(log)
                if n < 0:
                    self._lose_acks(proc)
                    break
                if n > before:
                    break
                time.sleep(0.01)

    def _lose_acks(self, proc) -> None:
        """The log we read acks from is gone (cache cleanup): we can no longer
        tell what ffmpeg has applied, and sending blind would queue stale
        levels. Stop sending to this process and say so (the next volume or
        mute key reports it); playing again starts a fresh process and log."""
        self._gain_broken = (proc, "dial's volume control lost contact with the player "
                                   "(its log was removed)")

    def _check_gain(self) -> None:
        if self._gain_broken and self._gain_broken[0] is self._proc:
            raise PlaybackError(self._gain_broken[1], "press enter to tune again")

    def set_volume(self, volume: int) -> int:
        self.gain.volume = _clamp(volume)
        if not self.dry_run:
            self._push_gain()
            self._check_gain()
        return self.gain.volume

    def set_mute(self, muted: bool) -> None:
        self.gain.muted = bool(muted)
        if not self.dry_run:
            self._push_gain()
            self._check_gain()

    def unavailable(self) -> PlaybackError | None:
        """Why this output can't play at all (a missing binary), or None."""
        return None

    @property
    def log(self) -> Path:
        return self._log_path or dial_state.CACHE_DIR / self.log_name

    _log_seq = itertools.count(1)
    _live_logs: set[Path] = set()       # in use by a process of ours, this session

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True                 # exists, but isn't ours to signal
        return True

    def _new_log(self) -> Path:
        """A log of this process's own: acks are read from it, so another
        process (a second dial, the next respawn) must not write to it. The
        newest few of *finished* processes' logs are kept for debugging: one a
        live process still writes to (ours, or another session's) is never
        removed, or its acks could not be read."""
        d = dial_state.CACHE_DIR
        stem = self.log_name.removesuffix(".log")
        path = d / f"{stem}.{os.getpid()}.{next(self._log_seq)}.log"
        ProcessOutput._live_logs.add(path)
        try:
            finished = []
            for p in d.glob(f"{stem}.*.log"):
                try:
                    pid = int(p.name.split(".")[-3])
                except (ValueError, IndexError):
                    continue
                live = p in ProcessOutput._live_logs if pid == os.getpid() else self._pid_alive(pid)
                if not live:
                    finished.append(p)
            finished.sort(key=lambda p: p.stat().st_mtime)
            for p in finished[:-7]:
                p.unlink(missing_ok=True)
        except OSError:
            pass
        return path

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _volume(self) -> tuple[int | None, bool]:
        return self.gain.volume, self.gain.muted

    def state(self) -> OutputState:
        vol, muted = self._volume()
        alive = self._alive()
        m = self._media if alive else None
        left = self._sleep_at - time.monotonic() if self._sleep_at else None
        return OutputState(tuned=m.station if m else None, playing=alive,
                           volume=vol, muted=muted,
                           sleep_s=int(left) if left and left > 0 else None,
                           uri=bare(m.url) if m else "")

    def play(self, media: Media, *, confirmed: bool = False, source: str = "dial") -> str:
        if (why := self.unavailable()) is not None:
            raise why
        if self.dry_run:
            return f"[dry-run] would play {media.name} on {self.label}"
        with self._lock:
            self._kill()
            self._log_path = self._new_log()
            self._gain_broken = None
            self._proc = self._spawn(self.argv(media), self.log)
            self._acked = self.log.exists()     # a real spawn always creates it
            self._media = media
            changed = self._argv_gain != (self.gain.volume, self.gain.muted)
        if changed:
            self._push_gain()           # set while the process was starting
        return f"▶ {media.name} on {self.label}"

    def _kill(self) -> None:
        proc, self._proc, self._media = self._proc, None, None
        ProcessOutput._live_logs.discard(self._log_path)    # its log is now history
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    def stop(self) -> str:
        with self._lock:
            self._kill()
        return f"■ stopped on {self.label}"

    def set_sleep_timer(self, seconds: int) -> int | None:
        """A process has no speaker to hold a timer, so the app holds it:
        it stops the process if the app is still open then -- and the
        process stops with the app anyway."""
        if self._sleep is not None:
            self._sleep.cancel()
        self._sleep, self._sleep_at = None, None
        if seconds <= 0 or self.dry_run:
            return seconds if self.dry_run and seconds > 0 else None
        self._sleep = threading.Timer(seconds, self._sleep_now)
        self._sleep.daemon = True
        self._sleep.start()
        self._sleep_at = time.monotonic() + seconds
        return seconds

    def _sleep_now(self) -> None:
        self._sleep, self._sleep_at = None, None
        self.stop()

    def close(self) -> None:
        self.set_sleep_timer(0)
        self.stop()
        self._closed = True
        self._gain_wake.set()           # let the sender thread end


def _ffmpeg_sink(ffmpeg: str | None) -> str:
    """How this computer's speakers are reached: "audiotoolbox" (macOS) or
    "pulse" (PulseAudio, or PipeWire's server -- a Chromebook's Linux
    container) when this ffmpeg was built with it; else "ffplay", which can't
    take twiddle's own volume and falls back on the system's."""
    if sys.platform == "darwin":
        return "audiotoolbox"
    if ffmpeg:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-devices"], capture_output=True,
                                 text=True, timeout=5, check=False).stdout
            if re.search(r"^\s*\S*E\S*\s+pulse\b", out, re.M):
                return "pulse"
        except (OSError, subprocess.SubprocessError):
            pass
    return "ffplay"


class LocalOutput(ProcessOutput):
    """This computer's speakers, through `ffmpeg` to the default output
    (`audiotoolbox` on a Mac, `pulse` on Linux), with twiddle's own volume and
    mute (`ProcessOutput`): the system volume and other apps are untouched.

    Nothing here touches a speaker or Spotify. The stream stops when the app
    quits, the same as `scene`'s "This Mac".

    A Linux whose ffmpeg has no PulseAudio support falls back to `ffplay`
    and the default sink's volume via `pactl` (the old behaviour), since
    `ffplay` can't be retuned while it runs. `sink` forces one.
    """
    id = MAC
    label = MAC_LABEL

    def __init__(self, *, dry_run: bool = False, spawn=_spawn, pactl=None,
                 ffplay: str | None = None, ffmpeg: str | None = None,
                 sink: str | None = None):
        super().__init__(dry_run=dry_run, spawn=spawn)
        self._ffplay = ffplay or shutil.which("ffplay")
        self._ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.sink = sink or _ffmpeg_sink(self._ffmpeg)
        self._pactl = (pactl or _pactl) if self.sink == "ffplay" else None
        if self.sink == "ffplay":
            self.log_name = "ffplay.log"
            self.volume_interval_s = 0.3    # a subprocess per write
        else:
            self.log_name = "ffmpeg.log"

    def unavailable(self) -> PlaybackError | None:
        hint = "brew install ffmpeg" if here.KIND == "Mac" else "sudo apt install ffmpeg"
        name = "ffplay" if self.sink == "ffplay" else "ffmpeg"
        return None if (self._ffplay if self.sink == "ffplay" else self._ffmpeg) \
            else PlaybackError(f"{name} isn't installed", hint)

    def argv(self, media: Media) -> list[str]:
        if self.sink != "ffplay":
            # ffmpeg ends at the end of a track by itself; a station never ends.
            return [self._ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats",
                    "-i", media.url, "-vn", *self.gain_args(), "-f", self.sink,
                    "-" if self.sink == "audiotoolbox" else "default"]
        # A track ends, unlike a station: -autoexit, or ffplay would sit
        # there silent and look alive. Stations keep their old flags.
        flags = ["-nodisp"] + (["-autoexit"] if media.station is None else [])
        return [self._ffplay, *flags, "-loglevel", "warning", media.url]

    def _volume(self) -> tuple[int | None, bool]:
        if self._pactl is None:
            return super()._volume()
        try:
            # "Volume: front-left: 65536 / 100% / 0.00 dB, ..." -- the first channel's %.
            m = re.search(r"(\d+)%", self._pactl("get-sink-volume", "@DEFAULT_SINK@"))
            vol = int(m.group(1)) if m else -1
            muted = self._pactl("get-sink-mute", "@DEFAULT_SINK@").endswith("yes")
        except (ValueError, OSError, subprocess.SubprocessError):
            vol, muted = -1, False
        return (vol if vol >= 0 else None), muted

    def set_volume(self, volume: int) -> int:
        if self._pactl is None:
            return super().set_volume(volume)
        volume = _clamp(volume)
        if not self.dry_run:
            self._pactl("set-sink-volume", "@DEFAULT_SINK@", f"{volume}%")
        return volume

    def set_mute(self, muted: bool) -> None:
        if self._pactl is None:
            return super().set_mute(muted)
        if not self.dry_run:
            self._pactl("set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0")


# ---- choosing one --------------------------------------------------------------


class Outputs:
    """The places `dial` can play, one `Output` per place, created on demand,
    and the rule that only one of them plays what we started (module doc).

    The household is discovered once (about six seconds of SSDP) and shared.
    """

    def __init__(self, *, dry_run: bool = False, household_factory: Callable | None = None,
                 local_factory: Callable[..., Output] = LocalOutput,
                 bluetooth: Callable[[], list] | None = None):
        self.dry_run = dry_run
        self._bluetooth = bluetooth        # () -> bluetooth.choices(); None = the real one
        self._household_factory = household_factory
        self._local_factory = local_factory
        self._house = None
        self._outputs: dict[str, Output] = {}
        self._extra: dict[str, tuple[str, Callable[..., Output]]] = {}
        self._lock = threading.Lock()
        # Held across start-then-stop: a Textual thread worker marked
        # exclusive is only *flagged* when replaced, so two handoffs can
        # otherwise overlap and each keep its own output playing.
        self._switch = threading.RLock()
        self._owned: dict[str, str] = {}      # output id -> URL we started there

    def house(self):
        with self._lock:
            if self._house is None:
                if self._household_factory is not None:
                    self._house = self._household_factory()
                else:
                    from ..household import Household
                    self._house = Household.load()
            return self._house

    def register(self, oid: str, label: str, factory: Callable[..., Output]) -> None:
        """Offer another place to play; `factory(dry_run=...)` makes it."""
        self._extra[oid] = (label, factory)

    def _paired(self) -> list:
        if self._bluetooth is not None:
            return self._bluetooth()
        from . import bluetooth
        return bluetooth.choices()

    def choices(self) -> list[tuple[str, str]]:
        """(output id, label): every Sonos room that answered, this Mac, each
        paired Bluetooth audio device, then anything `register`ed."""
        try:
            rooms = sorted({g.name for g in self.house().groups}, key=str.lower)
        except Exception:
            rooms = []
        try:
            bt = self._paired()
        except Exception:
            bt = []
        for oid, label, dev in bt:
            if oid not in self._extra:
                self._register_bluetooth(oid, dev)
        return ([(f"room:{r}", f"{r}  (Sonos)") for r in rooms] + [(MAC, MAC_LABEL)]
                + [(oid, label) for oid, label, _d in bt]
                + [(oid, label) for oid, (label, _f) in self._extra.items()
                   if not oid.startswith("bt:")])

    def _register_bluetooth(self, oid: str, dev) -> None:
        from .bluetooth import BluetoothOutput
        self.register(oid, dev.name, lambda dry_run, d=dev: BluetoothOutput(
            d.name, d.address, dry_run=dry_run))

    def get(self, oid: str) -> Output:
        with self._lock:
            out = self._outputs.get(oid)
        if out is not None:
            return out
        if oid == MAC:
            out = self._local_factory(dry_run=self.dry_run)
        elif oid in self._extra:
            out = self._extra[oid][1](dry_run=self.dry_run)
        elif oid.startswith("bt:"):
            # Remembered from a past session: by name. `choices` fills in
            # the address (for connecting) whenever the picker is opened.
            from .bluetooth import BluetoothOutput, paired
            name = oid.removeprefix("bt:")
            addr = next((p.address for p in paired() if p.name == name), "")
            out = BluetoothOutput(name, addr, dry_run=self.dry_run)
        else:
            room = oid.removeprefix("room:")
            out = SonosOutput(room, lambda: self.house().resolve(room).group,
                              dry_run=self.dry_run)
        with self._lock:
            return self._outputs.setdefault(oid, out)

    # -- one stream at a time ---------------------------------------------------

    def play(self, oid: str, media: Media, *, confirmed: bool = False,
             source: str = "dial") -> str:
        """Start `media` on `oid`, then stop whatever we started elsewhere.

        Raises what `Output.play` raises, having stopped nothing."""
        with self._switch:
            msg = self.get(oid).play(media, confirmed=confirmed, source=source)
            if self.dry_run:
                others = [self.get(o).label for o in self._owned if o != oid]
                return msg + "".join(f"  · would stop {label}" for label in others)
            self._owned.pop(oid, None)
            notes = [self.release(o) for o in list(self._owned)]
            self._owned[oid] = media.url
            return msg + "".join(f"  · {n}" for n in notes if n)

    def movable(self, oid: str) -> Media | None:
        """The station playing on `oid`, to carry to another output -- or
        None when it is on the relay, idle, or on something that isn't a
        station. Claims it, so the next `play` elsewhere stops it."""
        with self._switch:
            try:
                st = self.get(oid).state()
            except PlaybackError:
                # Can't ask: go by what we started there, if anything.
                s = station_for(self._owned.get(oid))
                return Media.of(s) if s else None
            # Ours counts even when not PLAYING: a Sonos buffering a station
            # it was just tuned to says TRANSITIONING for seconds.
            ours = same_stream(st.uri, self._owned.get(oid))
            s = stations.STATIONS.get(st.tuned or "") if st.playing or ours else None
            if s is None:
                return None
            self._owned[oid] = s.url
            return Media.of(s)

    def owned_url(self, oid: str) -> str | None:
        """The full URL we started on `oid` -- scheme and token intact,
        unlike what a speaker reports back -- or None."""
        with self._switch:
            return self._owned.get(oid)

    def owned(self) -> list[str]:
        """The outputs playing something we started (as far as we know)."""
        with self._switch:
            return list(self._owned)

    def stop(self, oid: str) -> str:
        """Stop `oid` because you said so: unconditionally, and forget it."""
        with self._switch:
            msg = self.get(oid).stop()
            self._owned.pop(oid, None)
            return msg

    def disconnect(self, oid: str) -> str:
        """`D`: let go of `oid` unconditionally, leaving nothing on it, and
        forget it."""
        with self._switch:
            msg = self.get(oid).disconnect()
            self._owned.pop(oid, None)
            return msg

    def release(self, oid: str) -> str:
        """Stop `oid` if it is still on what we started there -- playing,
        buffering or paused: the URL is the test, not the transport state.
        Forget it either way, unless it couldn't be stopped. Returns a note
        for the UI."""
        with self._switch:
            url = self._owned.get(oid)
            if url is None:
                return ""
            out = self.get(oid)
            try:
                st = out.state()
            except PlaybackError as exc:
                return f"⚠ couldn't check {out.label}: {exc.message}"
            if not same_stream(st.uri, url):
                self._owned.pop(oid, None)        # moved on without us: not ours to stop
                return ""
            try:
                out.stop()
            except PlaybackError as exc:
                return f"⚠ {out.label} is still playing: {exc.message}"
            self._owned.pop(oid, None)
            return f"stopped {out.label}"

    def close(self) -> None:
        for out in list(self._outputs.values()):
            try:
                out.close()
            except Exception:
                pass
