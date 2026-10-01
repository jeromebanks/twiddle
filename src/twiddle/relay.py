"""Relay any local audio source to Sonos as a never-ending MP3 stream.

`play.py` already proved the reliable path: a speaker pulls plain HTTP from
this Mac and plays it, with no Sonos cloud and no Connect handoff anywhere in
the chain. That works for files sitting on disk. This module widens the same
pipe to accept *live* audio -- Spotify decoded locally by librespot, a test
tone, a file, or a virtual audio device -- and pushes it down the identical
`x-rincon-mp3radio://` route.

The shape is three stages, each a thread:

    source process  ->  JitterBuffer  ->  ffmpeg (MP3)  ->  Fanout  ->  Sonos
                     (paced, silence-
                      filled writer)

Two things in there are not decoration, and getting either wrong produces a
relay that demos perfectly and fails the first time it is used in anger:

**The writer into ffmpeg must never starve.** librespot stops writing to its
pipe the moment playback is paused. If that silence propagates, ffmpeg emits
nothing, the speaker's buffer drains, its transport goes to STOPPED -- and
pressing play in the Spotify app does *not* bring it back, because Sonos
already hung up and nothing is going to re-issue the URI. So the writer is
paced against the wall clock at exactly `PCM_BYTES_PER_SEC` and substitutes
digital silence for whatever the source failed to provide. A pause becomes
quiet on an endless stream, which is precisely the radio semantics Sonos
wants.

**One HTTP connection is not enough.** `ffmpeg -listen 1` serves a single
client and exits; Sonos buffers ahead, closes the socket and reopens it, so
that arrangement yields one burst of audio followed by silence. `Fanout`
below is a miniature Icecast instead: one encoder, many subscribers, each
with its own bounded queue so a slow listener is dropped frames rather than
a stalled encoder.

The bounded `JitterBuffer` also does double duty as a rate limiter. A source
that produces faster than real time (ffmpeg reading a file, or synthesising a
tone) blocks on `write()` once the buffer is full, so every source arrives at
the encoder at real-time speed without needing `-re` or per-source pacing.

This module WRITES to speakers when asked to point one at the relay, and
routes every such write through `play.py` so `logs/interventions.jsonl` and
`analyse` keep working -- see the note at the top of `play.py`.
"""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .play import local_ip_for

# librespot's pipe backend emits signed 16-bit little-endian stereo at 44.1kHz
# and offers no way to ask it what it just sent, so these are the contract.
# 44100 * 2 channels * 2 bytes = 176400 B/s; if a capture of the real binary
# does not measure that rate, the ffmpeg input flags below are wrong.
PCM_RATE = 44_100
PCM_CHANNELS = 2
PCM_SAMPLE_BYTES = 2
PCM_FRAME_BYTES = PCM_CHANNELS * PCM_SAMPLE_BYTES
PCM_BYTES_PER_SEC = PCM_RATE * PCM_FRAME_BYTES

# 20ms of audio. Small enough that a pause turns into silence imperceptibly
# fast, large enough that the pacing loop is not spinning.
PUMP_CHUNK = int(PCM_BYTES_PER_SEC * 0.020) // PCM_FRAME_BYTES * PCM_FRAME_BYTES

DEFAULT_PORT = 8899
DEFAULT_BITRATE = "192k"

# The relay's URL has to stay put across a restart, or the speaker is left
# pointed at a port nothing is listening on. Hence a fixed default port and
# a fixed path rather than anything generated.
STREAM_PATH = "/stream.mp3"

# The current track's cover, at a URL that never changes: it goes into the
# speaker's DIDL once, when the room is pointed at the relay, and whatever
# is playing *now* is served there. Changing the URL per track would mean a
# journalled transport write per track -- see `Covers`.
COVER_PATH = "/cover.jpg"

# A listener that isn't a speaker -- `twiddle`'s visualizer -- asks for
# `STREAM_PATH?observer`, and is kept out of the speaker counters (`Fanout`).
# The header on every stream response is how a client tells a relay that
# knows this from one that would count it as a speaker.
OBSERVER_QUERY = "observer"
OBSERVER_HEADER = "X-Twiddle-Observer"
COVER_STATE = "now-playing.json"


class JitterBuffer:
    """A bounded byte queue that pads with silence rather than blocking.

    `write` applies backpressure when full, which is what rate-limits a
    faster-than-real-time source. `take` never blocks and never returns short:
    a source that has gone quiet yields zeros, and the stream stays alive.
    """

    def __init__(self, capacity: int):
        self.capacity = capacity
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._closed = False
        self.underrun_chunks = 0
        self.silence_bytes = 0
        self.source_bytes = 0

    def write(self, data: bytes) -> None:
        with self._cv:
            while len(self._buf) >= self.capacity and not self._closed:
                self._cv.wait(0.25)
            if self._closed:
                return
            self._buf += data
            self.source_bytes += len(data)
            self._cv.notify_all()

    def take(self, n: int) -> bytes:
        with self._cv:
            have = min(n, len(self._buf))
            # Only ever hand over whole sample frames. Splitting one would
            # swap the channels for the rest of the stream -- audible as the
            # stereo image inverting, and impossible to recover from.
            have -= have % PCM_FRAME_BYTES
            out = bytes(self._buf[:have])
            del self._buf[:have]
            if have < n:
                self.underrun_chunks += 1
                self.silence_bytes += n - have
                out += b"\x00" * (n - have)
            self._cv.notify_all()
            return out

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    @property
    def depth(self) -> int:
        with self._cv:
            return len(self._buf)


def pace(jitter: JitterBuffer, write, stop: threading.Event,
         chunk: int = PUMP_CHUNK, resync_after: float = 5.0) -> None:
    """Feed the encoder at exactly real time, with silence for what is missing.

    This is the loop that keeps a paused Spotify from killing the stream. It
    writes `chunk` bytes every `chunk / PCM_BYTES_PER_SEC` seconds whatever
    the source is doing, so from the speaker's point of view the station never
    goes off the air.

    A module-level function rather than a method because it is the one piece
    here worth testing against a deliberately stalling source, and doing that
    should not require an ffmpeg subprocess.
    """
    t0 = time.monotonic()
    written = 0
    while not stop.is_set():
        try:
            write(jitter.take(chunk))
        except (BrokenPipeError, ValueError, OSError):
            return
        written += chunk
        delay = t0 + written / PCM_BYTES_PER_SEC - time.monotonic()
        if delay > 0:
            stop.wait(delay)
        elif delay < -resync_after:
            # Far behind: the Mac slept, or the encoder blocked for a long
            # time. Re-anchor rather than sprinting to catch up, which would
            # dump minutes of buffered audio into the stream at once.
            t0, written = time.monotonic(), 0


class Fanout:
    """One producer, many HTTP listeners, no listener able to stall the rest.

    Each subscriber gets a bounded queue. When it fills -- a speaker that has
    stopped reading but not yet closed the socket -- the oldest frames are
    discarded. Dropping audio for one slow client is always better than
    letting it back up into the encoder and take the others down with it.
    """

    def __init__(self, queue_chunks: int = 128):
        self.queue_chunks = queue_chunks
        self._subs: dict[int, queue.Queue] = {}
        # Observers (the TUI visualizer) get the same audio but none of the
        # counters above: `listeners`, `clients_total` and `dropped_chunks`
        # are the relay experiment's evidence about the *speakers*, and a
        # stalled visualizer must never read as "the Roam stopped reading".
        self._observers: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._next = 0
        self.dropped_chunks = 0
        self.total_clients = 0
        self.observer_dropped = 0

    def subscribe(self, observer: bool = False) -> tuple[int, queue.Queue]:
        q: queue.Queue = queue.Queue(maxsize=self.queue_chunks)
        with self._lock:
            key = self._next
            self._next += 1
            if observer:
                self._observers[key] = q
            else:
                self._subs[key] = q
                self.total_clients += 1
        return key, q

    def unsubscribe(self, key: int) -> None:
        with self._lock:
            self._subs.pop(key, None)
            self._observers.pop(key, None)

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._subs)

    @property
    def observers(self) -> int:
        with self._lock:
            return len(self._observers)

    def publish(self, data: bytes) -> None:
        with self._lock:
            subs = [(q, False) for q in self._subs.values()]
            subs += [(q, True) for q in self._observers.values()]
        for q, observer in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                    if observer:
                        self.observer_dropped += 1
                    else:
                        self.dropped_chunks += 1
                except (queue.Empty, queue.Full):
                    pass

    def close(self) -> None:
        with self._lock:
            subs = list(self._subs.values()) + list(self._observers.values())
            self._subs.clear()
            self._observers.clear()
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass


class Covers:
    """The cover of whatever the relay's librespot is playing right now.

    `relay_event` (librespot's `--onevent` hook) writes the track and its
    cover URLs to `state`; this reads that file per request and fetches the
    image, keeping only the current one in memory.

    The open question this exists to answer: the URL a speaker is given is
    fixed, so does the Sonos app ever ask again after the track changes, or
    does it cache the first cover for the session? `on_request` sees every
    fetch, and the relay journals them, so the first real session settles it.
    """

    def __init__(self, state: Path, fetch: Callable[[str], bytes] | None = None):
        self.state = state
        self._fetch = fetch or _fetch_image
        self._lock = threading.Lock()
        self._url: str | None = None
        self._image: bytes | None = None

    def track(self) -> dict:
        try:
            return json.loads(self.state.read_text())
        except (OSError, ValueError):
            return {}

    def current(self) -> tuple[dict, bytes | None]:
        """(track, image bytes or None). Never raises: no cover is a 404, not a crash."""
        track = self.track()
        urls = track.get("covers") or []
        if not urls:
            return track, None
        with self._lock:
            if self._url != urls[0]:
                # librespot lists every size Spotify has and doesn't say
                # which is which, so take the biggest of what comes back.
                got = []
                for url in urls:
                    try:
                        got.append(self._fetch(url))
                    except Exception:
                        pass
                self._url = urls[0]
                self._image = max(got, key=len) if got else None
            return track, self._image


def _fetch_image(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "twiddle relay"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read()


def install_onevent_hook(cache: str) -> tuple[str, Path]:
    """Write the `--onevent` script librespot will run; (script, state file).

    librespot takes a bare program with no arguments, so this is a two-line
    shell script that execs `relay_event` under this same Python. That is
    the venv's interpreter when the relay runs from the console script,
    which is how the supervisor starts it.
    """
    state = Path(cache) / COVER_STATE
    script = Path(cache) / "onevent.sh"
    script.write_text("#!/bin/sh\n"
                      f'exec "{sys.executable}" -m twiddle.relay_event "{state}"\n')
    script.chmod(0o755)
    return str(script), state


def _frame_sync(buf: bytes) -> int:
    """Index of the first MP3 frame header in `buf`, or -1.

    A frame header is eleven set bits: 0xFF followed by a byte whose top
    three bits are set. Two extra bits are checked as well, because 0xFF is
    common enough in compressed audio that the sync word alone finds false
    positives: `11` in the layer field is reserved, and `1111` in the bitrate
    index is invalid, so a byte with either is not a real header.
    """
    for i in range(len(buf) - 3):
        if buf[i] != 0xFF or (buf[i + 1] & 0xE0) != 0xE0:
            continue
        if (buf[i + 1] & 0x06) == 0 or (buf[i + 2] & 0xF0) == 0xF0:
            continue
        return i
    return -1


class _StreamHandler(BaseHTTPRequestHandler):
    """Serves the live MP3. Deliberately HTTP/1.0 and deliberately endless.

    Sonos wants a radio stream: `audio/mpeg`, no `Content-Length`, no chunked
    transfer encoding. HTTP/1.0 gets all three for free -- 1.1 would invite
    keep-alive and chunking, both of which confuse the renderer.

    `Icy-MetaData: 1` is answered by *not* sending `icy-metaint`. A client
    that asks and is not told simply gets no track titles; one that is told a
    metadata interval and then handed a stream without the interleaved blocks
    hears corruption, so a half-implemented ICY is worse than none.
    """

    protocol_version = "HTTP/1.0"
    fanout: Fanout = None  # type: ignore[assignment]
    covers: Covers | None = None
    on_cover: Callable[..., None] | None = None

    def _is_cover(self) -> bool:
        return self.path.split("?", 1)[0] == COVER_PATH

    def _serve_cover(self, body: bool) -> None:
        track, image = self.covers.current() if self.covers else ({}, None)
        if self.on_cover:
            try:
                self.on_cover(client=self.client_address[0],
                              agent=self.headers.get("User-Agent", ""),
                              track=track.get("name"), served=image is not None)
            except Exception:
                pass
        if image is None:
            self.send_error(404, "no cover for what is playing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(image)))
        # Ask everything between here and the app not to keep it: the URL is
        # fixed and the picture behind it changes with every track.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        if body:
            self.wfile.write(image)

    def _write_aligned(self, chunk: bytes, aligned: bool) -> bool:
        """Start a new listener on an MP3 frame boundary, not mid-frame.

        A subscriber joins at the live edge, which lands in the middle of
        whatever frame the encoder was emitting. Decoders resynchronise, but
        they announce it -- `ffprobe` on a capture taken here reported
        "Skipping 250 bytes of junk at 0" -- and the recovery is audible as a
        click at the top of every reconnect. Sonos reconnects often, so that
        would be a click every time it refills its buffer.
        """
        if aligned:
            self.wfile.write(chunk)
            return True
        start = _frame_sync(chunk)
        if start < 0:
            return False
        self.wfile.write(chunk[start:])
        return True

    def log_message(self, *_args):
        pass

    def _is_observer(self) -> bool:
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        return OBSERVER_QUERY in query.split("&")

    def _headers(self, head: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        if head:
            # Says "this relay counts observers apart": a relay started
            # before they existed would count a visualizer as a speaker, so
            # the tap HEADs for this first and stays away when it's missing.
            # HEAD only, so what a Roam GETs is byte-for-byte what it was.
            self.send_header(OBSERVER_HEADER, "1")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_HEAD(self):
        if self._is_cover():
            return self._serve_cover(body=False)
        self._headers(head=True)

    def do_GET(self):
        if self._is_cover():
            return self._serve_cover(body=True)
        self._headers()
        key, q = self.fanout.subscribe(observer=self._is_observer())
        aligned = False
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    return
                aligned = self._write_aligned(chunk, aligned) or aligned
        except (BrokenPipeError, ConnectionResetError, ValueError):
            # Normal: the speaker buffered ahead and hung up. `play.py` makes
            # the same allowance for the same reason.
            pass
        finally:
            self.fanout.unsubscribe(key)


# ---- sources ---------------------------------------------------------------
#
# A source is just a process that writes raw PCM on stdout in the format the
# constants above describe. Adding one -- a different decoder, your own app --
# means adding an argv here and nothing else.


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("ffmpeg not found on PATH; `brew install ffmpeg`")
    return found


def librespot_bin() -> str:
    found = shutil.which("librespot")
    if not found:
        raise RuntimeError("librespot not found on PATH; `brew install librespot`")
    return found


def _ffmpeg_pcm_out() -> list[str]:
    return ["-f", "s16le", "-ar", str(PCM_RATE), "-ac", str(PCM_CHANNELS), "pipe:1"]


def tone_source(frequency: float = 440.0) -> list[str]:
    """A test tone, so the speaker half can be proven without Spotify at all.

    This is the source that makes the relay verifiable: if a tone reaches the
    Roam cleanly, then ffmpeg, the fan-out server and the speaker's radio path
    all work, and anything still broken is Spotify-side.
    """
    return [ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"sine=frequency={frequency}:sample_rate={PCM_RATE}",
            "-ac", str(PCM_CHANNELS), *_ffmpeg_pcm_out()]


def file_source(path: str, loop: bool = True) -> list[str]:
    args = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error"]
    if loop:
        args += ["-stream_loop", "-1"]
    return args + ["-i", path, *_ffmpeg_pcm_out()]


def device_source(device: str) -> list[str]:
    """Capture a macOS audio input -- BlackHole, or any other avfoundation device.

    Kept as a fallback for sources that cannot be piped (a browser tab, an app
    with no CLI). The Spotify path does not need it: librespot writes PCM
    straight to a pipe, which skips the virtual-device install entirely.
    """
    return [ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-i", device, *_ffmpeg_pcm_out()]


def spotify_source(name: str, cache: str, bitrate: str = "320",
                   extra: list[str] | None = None,
                   onevent: str | None = None) -> list[str]:
    """librespot as a Spotify Connect target whose audio comes out of a pipe.

    Control stays in the Spotify app: it sees this as a speaker to cast to.
    Nothing here talks to Sonos's cloud, and no Connect handoff to the Roam
    ever happens -- which matters, because that handoff is the single worst
    measured trigger for dropouts in the household this was built for.

    `--backend pipe` with no `--device` means stdout, which is why stdout must
    stay clean: librespot's own logging goes to stderr, and anything that
    redirects it into stdout injects text into the PCM as a burst of noise.

    `--format S16` is pinned rather than left to the default so the ffmpeg
    input flags cannot silently disagree with what arrives. Verify with
    `relay measure`: the pipe must carry 176400 bytes/sec.
    """
    args = [librespot_bin(), "--name", name, "--backend", "pipe",
            "--format", "S16", "--bitrate", bitrate,
            # Full scale into the pipe, and nothing able to turn it down.
            #
            # librespot defaults to a software volume of 50 on a 60dB log
            # curve, which is about -30dB -- and that attenuation multiplies
            # with the speaker's own. Measured on this relay: the stream
            # carried real audio at mean -50.8dB, inaudible through a Roam at
            # volume 25, and went to -21.8dB the instant softvol was set to
            # 100. `fixed` makes the Spotify app's slider a no-op and leaves
            # loudness to exactly one control, `twiddle volume --room`.
            # It also avoids attenuating in 16-bit and losing bits to do it.
            "--initial-volume", "100", "--volume-ctrl", "fixed",
            # `--cache` holds downloaded audio; `--system-cache` holds the
            # credentials `relay login` writes. Same directory here, but named
            # separately because only the second one must survive a cleanup.
            "--cache", cache, "--system-cache", cache]
    if onevent:
        # Track changes -> relay_event -> the cover `Covers` serves.
        args += ["--onevent", onevent]
    return args + list(extra or [])


def local_argv(name: str, cache: str, bitrate: str = "320") -> list[str]:
    """librespot as a Connect target that plays out of this Mac's speakers.

    For anyone without the Roams: `rodio` goes to CoreAudio's default output
    (measured: "Mac mini Speakers"), so no Spotify desktop app is needed.
    Unlike `spotify_source`, volume is left adjustable -- here the Spotify
    slider is the only control besides the Mac's own.

    `--disable-discovery`: it signs in with cached credentials, so it has no
    need to advertise itself to every phone on the LAN.
    """
    return [librespot_bin(), "--name", name, "--backend", "rodio",
            "--device-type", "computer", "--disable-discovery",
            "--bitrate", bitrate, "--initial-volume", "100",
            "--cache", cache, "--system-cache", cache]


def login_argv(name: str, cache: str) -> list[str]:
    """Interactive OAuth, run once, so the relay itself never sees a password.

    Spotify stopped accepting username/password from third-party clients, so
    this opens a browser. It writes a credentials blob under `cache`, and
    every later `relay start` reuses it non-interactively.
    """
    return [librespot_bin(), "--name", name, "--backend", "pipe",
            "--cache", cache, "--system-cache", cache,
            "--enable-oauth", "--disable-discovery"]


def credentials_path(cache: str) -> str:
    from pathlib import Path
    return str(Path(cache) / "credentials.json")


SOURCE_HELP = {
    "spotify": "librespot as a Connect target; cast to it from the Spotify app",
    "tone": "a 440Hz sine -- proves the speaker path with no Spotify involved",
    "file": "an audio file, looped (--source-arg PATH)",
    "device": "an avfoundation input such as BlackHole (--source-arg NAME)",
}


# ---- the relay -------------------------------------------------------------


@dataclass
class Relay:
    """Source process -> paced PCM -> ffmpeg -> HTTP fan-out.

    Start it, hand `url_for()` to a speaker, and leave it running. Nothing in
    here touches a speaker; pointing one at the stream is the caller's job, so
    that every speaker write stays in `play.py` where it is journalled.
    """

    source_argv: list[str]
    port: int = DEFAULT_PORT
    bitrate: str = DEFAULT_BITRATE
    buffer_seconds: float = 2.0
    source_stderr = None
    covers: Covers | None = None
    on_cover: Callable[..., None] | None = None

    _src: subprocess.Popen | None = field(default=None, init=False)
    _enc: subprocess.Popen | None = field(default=None, init=False)
    _srv: ThreadingHTTPServer | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _threads: list[threading.Thread] = field(default_factory=list, init=False)
    jitter: JitterBuffer | None = field(default=None, init=False)
    fanout: Fanout = field(default_factory=Fanout, init=False)
    started_at: float = field(default=0.0, init=False)
    encoded_bytes: int = field(default=0, init=False)

    # -- lifecycle --

    def encoder_argv(self) -> list[str]:
        return [ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", str(PCM_RATE), "-ac", str(PCM_CHANNELS),
                "-i", "pipe:0",
                "-c:a", "libmp3lame", "-b:a", self.bitrate,
                # Constant bitrate, and no Xing/LAME header: both exist to
                # describe a file of known length, which a live stream is not.
                # A leading Xing frame is a silent frame of bogus duration at
                # the head of every listener's connection.
                "-write_xing", "0", "-id3v2_version", "0",
                "-f", "mp3", "pipe:1"]

    def start(self) -> "Relay":
        self.jitter = JitterBuffer(int(PCM_BYTES_PER_SEC * self.buffer_seconds))
        self._enc = subprocess.Popen(self.encoder_argv(),
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE)
        self._src = subprocess.Popen(self.source_argv, stdout=subprocess.PIPE,
                                     stderr=self.source_stderr)

        handler = type("_Bound", (_StreamHandler,), {
            "fanout": self.fanout, "covers": self.covers,
            # staticmethod: a plain function stored on the class would be
            # bound as a method and handed the handler as its first argument.
            "on_cover": staticmethod(self.on_cover) if self.on_cover else None})
        self._srv = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self._srv.daemon_threads = True
        self.port = self._srv.server_port

        self._spawn(self._read_source, "source")
        self._spawn(self._pace_into_encoder, "pace")
        self._spawn(self._read_encoder, "encode")
        self._spawn(self._srv.serve_forever, "http")
        self.started_at = time.monotonic()
        return self

    def _spawn(self, fn, name: str) -> None:
        t = threading.Thread(target=fn, name=f"relay-{name}", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        if self.jitter:
            self.jitter.close()
        self.fanout.close()
        if self._srv:
            self._srv.shutdown()
        # Signal both first, then wait on both: waiting on each in turn makes
        # the worst case the sum of the timeouts rather than the longest one,
        # which showed up as a seven-second pause on the way out.
        procs = [p for p in (self._src, self._enc) if p and p.poll() is None]
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def __enter__(self) -> "Relay":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- threads --

    def _read_source(self) -> None:
        """Drain the source into the jitter buffer, blocking when it is full.

        That block is the rate limiter: a file or a tone would otherwise be
        produced as fast as the CPU allows and arrive hours early.
        """
        assert self._src and self._src.stdout and self.jitter
        try:
            while not self._stop.is_set():
                data = self._src.stdout.read(PUMP_CHUNK)
                if not data:
                    break
                self.jitter.write(data)
        except (BrokenPipeError, ValueError, OSError):
            pass

    def _pace_into_encoder(self) -> None:
        assert self._enc and self._enc.stdin and self.jitter

        def write(data: bytes) -> None:
            self._enc.stdin.write(data)  # type: ignore[union-attr]
            self._enc.stdin.flush()      # type: ignore[union-attr]

        pace(self.jitter, write, self._stop)

    def _read_encoder(self) -> None:
        assert self._enc and self._enc.stdout
        try:
            while not self._stop.is_set():
                data = self._enc.stdout.read(4096)
                if not data:
                    break
                self.encoded_bytes += len(data)
                self.fanout.publish(data)
        except (BrokenPipeError, ValueError, OSError):
            pass

    # -- reporting --

    def url_for(self, peer: str) -> str:
        """The URL a given speaker should be pointed at.

        Addressed by the interface that reaches *that* peer, so a Mac on both
        wired and wireless networks hands the speaker a route it can use.
        """
        return f"http://{local_ip_for(peer)}:{self.port}{STREAM_PATH}"

    def cover_url_for(self, peer: str, key: str | None = None) -> str | None:
        """Where the speaker should look for the cover, if this source has one.

        `key` (the track) goes in the query string: the Sonos app fetches the
        art once per URL it is given and keeps it, so a fixed URL shows the
        first cover (or a stale one from an earlier session) forever. The
        server ignores the query (`_is_cover`) and serves the current track.
        """
        if not self.covers:
            return None
        url = f"http://{local_ip_for(peer)}:{self.port}{COVER_PATH}"
        return f"{url}?t={key}" if key else url

    def alive(self) -> bool:
        return bool(self._src and self._src.poll() is None
                    and self._enc and self._enc.poll() is None)

    def stats(self) -> dict:
        j = self.jitter
        up = time.monotonic() - self.started_at if self.started_at else 0.0
        return {
            "uptime_s": round(up, 1),
            "listeners": self.fanout.listeners,
            "clients_total": self.fanout.total_clients,
            "encoded_bytes": self.encoded_bytes,
            "buffer_bytes": j.depth if j else 0,
            "source_bytes": j.source_bytes if j else 0,
            # Silence is not an error -- it is what a paused Spotify looks
            # like -- but a relay that is *only* silence means the source
            # never produced anything, which is.
            "silence_s": round((j.silence_bytes / PCM_BYTES_PER_SEC), 1) if j else 0.0,
            "dropped_chunks": self.fanout.dropped_chunks,
            # The visualizer's tap: never in the three counters above.
            "observers": self.fanout.observers,
            "observer_dropped_chunks": self.fanout.observer_dropped,
            "source_alive": bool(self._src and self._src.poll() is None),
            "encoder_alive": bool(self._enc and self._enc.poll() is None),
        }


# ---- prerequisites ---------------------------------------------------------


def check_prerequisites(source: str = "spotify") -> list[dict]:
    """What is installed, what is missing, and the command that fixes it."""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    ff = shutil.which("ffmpeg")
    add("ffmpeg", bool(ff), ff or "not on PATH", "brew install ffmpeg")
    if ff:
        try:
            out = subprocess.run([ff, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=20).stdout
            add("libmp3lame", "libmp3lame" in out,
                "present" if "libmp3lame" in out else "this ffmpeg cannot encode MP3",
                "brew reinstall ffmpeg")
        except Exception as exc:
            add("libmp3lame", False, f"could not ask ffmpeg: {exc}", "")

    if source == "spotify":
        ls = shutil.which("librespot")
        add("librespot", bool(ls), ls or "not on PATH", "brew install librespot")
    if source == "device":
        add("BlackHole", bool(shutil.which("ffmpeg")),
            "list inputs with: ffmpeg -f avfoundation -list_devices true -i \"\"",
            "brew install blackhole-2ch")
    return checks
