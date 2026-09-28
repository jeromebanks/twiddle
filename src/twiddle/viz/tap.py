"""An audio tap: ffmpeg decoding a stream URL into a ring of stereo PCM.

It is a second, read-only listener on the same stream a speaker is playing
-- nothing here writes to a speaker, and nothing is journalled. It is *not*
what the speaker emits: a Sonos buffers seconds ahead of what it plays
(`delay_s` shifts the picture back to meet it), and a speaker that drops out
keeps dancing here.

The reader thread always drains the pipe, whatever the screen is doing: an
undrained pipe would stall ffmpeg, and ffmpeg would stall the station's
server connection. When the stream ends or breaks, it reconnects with a
backoff until `stop()`.
"""
from __future__ import annotations

import atexit
import shutil
import subprocess
import threading
import time
import weakref
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .analysis import RATE

CHANNELS = 2
BYTES_PER_FRAME = 4 * CHANNELS          # f32le, stereo
CAPACITY_S = 20.0                       # the most `delay_s` can reach back, plus a window
MAX_DELAY_S = 15.0
STALE_S = 3.0                           # no audio for this long = say so
READ_BYTES = 4096


def decoder_argv(ffmpeg: str, url: str) -> list[str]:
    """`-re` reads at the pace of playback, as a player does: a Bandcamp
    track would otherwise decode in seconds and race ahead of the speaker,
    and a station's connect burst would land all at once."""
    net = (["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
           if url.startswith(("http://", "https://")) else [])
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *net,
            "-re", "-i", url, "-vn", "-ac", str(CHANNELS), "-ar", str(RATE), "-f", "f32le", "pipe:1"]


def _spawn(argv: list[str], log: Path):
    # Errors to a log file, never the terminal: they would draw over the TUI.
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as err:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=err)


def default_log() -> Path:
    from ..dial import state as dial_state
    return dial_state.CACHE_DIR / "viz-tap.log"


class Ring:
    """A fixed-size ring of stereo frames; thread-safe. Unfilled history
    reads as silence."""

    def __init__(self, frames: int):
        self.buf = np.zeros((frames, CHANNELS), dtype=np.float32)
        self.written = 0            # frames ever written
        self._lock = threading.Lock()

    def write(self, frames: np.ndarray) -> None:
        n = len(frames)
        size = len(self.buf)
        if n == 0:
            return
        if n >= size:
            frames, n = frames[-size:], size
        with self._lock:
            start = self.written % size
            first = min(n, size - start)
            self.buf[start:start + first] = frames[:first]
            self.buf[:n - first] = frames[first:]
            self.written += n

    def window(self, n: int, delay: int = 0) -> np.ndarray:
        """The n frames ending `delay` frames before the newest."""
        size = len(self.buf)
        n = min(n, size)
        delay = max(0, min(delay, size - n))
        with self._lock:
            end = self.written - delay
            idx = np.arange(end - n, end)
            out = self.buf[idx % size].copy()
            out[idx < max(0, self.written - size)] = 0   # overwritten long ago: can't be
        out[idx < 0] = 0
        return out


_live: weakref.WeakSet = weakref.WeakSet()


@atexit.register
def _stop_all() -> None:
    """An app that exits with the screen still up must not leave ffmpeg
    running (it would die of SIGPIPE on its next write anyway)."""
    for tap in list(_live):
        tap.stop()


class AudioTap:
    """Decode `url` until stopped. `window()` is safe from any thread."""

    def __init__(self, url: str, *, spawn: Callable = _spawn, ffmpeg: str | None = None,
                 log: Path | None = None, capacity_s: float = CAPACITY_S,
                 clock: Callable[[], float] = time.monotonic):
        self.url = url
        self._spawn = spawn
        self._ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self._log = log
        self._clock = clock
        self.ring = Ring(int(capacity_s * RATE))
        self._stop = threading.Event()
        self._proc = None
        self._thread: threading.Thread | None = None
        self.last_data: float | None = None
        self.status = "connecting"
        self.error: str | None = None

    def start(self) -> AudioTap:
        if self._ffmpeg is None:
            self.status, self.error = "error", "ffmpeg isn't installed (brew install ffmpeg)"
            return self
        _live.add(self)
        self._thread = threading.Thread(target=self._run, name="viz-tap", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                self._proc = self._spawn(decoder_argv(self._ffmpeg, self.url),
                                         self._log or default_log())
            except OSError as exc:
                self.status, self.error = "error", f"couldn't start ffmpeg: {exc}"
                return
            got = self._drain(self._proc)
            self._reap()
            if self._stop.is_set():
                break
            if got:
                backoff = 0.5
            self.status = "reconnecting"
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 8.0)

    def _drain(self, proc) -> bool:
        """Read until EOF; True if any audio came."""
        got = False
        rest = b""
        out = proc.stdout
        while not self._stop.is_set():
            chunk = out.read1(READ_BYTES) if hasattr(out, "read1") else out.read(READ_BYTES)
            if not chunk:
                return got
            data = rest + chunk
            usable = len(data) - len(data) % BYTES_PER_FRAME
            rest = data[usable:]
            if usable:
                frames = np.frombuffer(data[:usable], dtype="<f4").reshape(-1, CHANNELS)
                self.ring.write(frames)
                self.last_data = self._clock()
                self.status, self.error, got = "live", None, True
        return got

    def _reap(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop(self) -> None:
        _live.discard(self)
        self._stop.set()
        self._reap()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    @property
    def stale(self) -> bool:
        """Nothing has arrived for a while (or ever)."""
        return self.last_data is None or self._clock() - self.last_data > STALE_S

    def window(self, n: int, delay_s: float = 0.0) -> np.ndarray:
        """The newest n frames, as of `delay_s` ago: (n, 2) float32."""
        return self.ring.window(n, int(max(0.0, min(delay_s, MAX_DELAY_S)) * RATE))
