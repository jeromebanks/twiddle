"""The audio tap: its ring buffer, its delay, and that it drains and stops."""
import os
import threading
import time

import numpy as np

from twiddle.viz import tap as tap_mod
from twiddle.viz.analysis import RATE
from twiddle.viz.tap import AudioTap, Ring, decoder_argv


def test_ring_returns_the_newest_frames_in_order_across_the_wrap():
    r = Ring(10)
    for i in range(3):
        r.write(np.arange(i * 4, i * 4 + 4, dtype=np.float32).repeat(2).reshape(-1, 2))
    assert r.window(4)[:, 0].tolist() == [8, 9, 10, 11]
    assert r.window(3, delay=2)[:, 0].tolist() == [7, 8, 9]


def test_ring_history_it_never_had_is_silence():
    r = Ring(10)
    r.write(np.ones((3, 2), np.float32))
    assert r.window(5)[:, 0].tolist() == [0, 0, 1, 1, 1]


def test_decoder_reads_at_playback_pace_and_writes_float_stereo():
    argv = decoder_argv("ffmpeg", "http://example.com/s.mp3")
    assert "-re" in argv and argv[argv.index("-f", argv.index("-i")) + 1] == "f32le"
    assert argv[argv.index("-ar") + 1] == str(RATE) and argv[argv.index("-ac") + 1] == "2"
    assert "-reconnect" in argv
    assert "-reconnect" not in decoder_argv("ffmpeg", "/tmp/a.mp3")


class FakeProc:
    """ffmpeg, but a pipe we write into."""

    def __init__(self):
        r, self.w = os.pipe()
        self.stdout = os.fdopen(r, "rb", buffering=0)
        self.terminated = False

    def feed(self, frames: np.ndarray):
        os.write(self.w, frames.astype("<f4").tobytes())

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        if not self.terminated:
            self.terminated = True
            os.close(self.w)

    kill = terminate

    def wait(self, timeout=None):
        return 0


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    return cond()


def test_the_tap_drains_the_pipe_into_the_ring_and_stop_kills_ffmpeg():
    procs = []

    def spawn(argv, log):
        procs.append(FakeProc())
        return procs[-1]

    tap = AudioTap("http://x/y", spawn=spawn, ffmpeg="ffmpeg", log=None).start()
    assert _wait(lambda: procs)
    assert tap.stale and tap.status == "connecting"
    # An odd number of bytes first: frames split across reads still line up.
    data = np.column_stack([np.linspace(0, 1, 1000), -np.linspace(0, 1, 1000)]).astype("<f4").tobytes()
    os.write(procs[0].w, data[:13])
    os.write(procs[0].w, data[13:])
    assert _wait(lambda: tap.ring.written == 1000)
    w = tap.window(4)
    assert np.allclose(w[:, 0], np.linspace(0, 1, 1000)[-4:]) and np.allclose(w[:, 1], -w[:, 0])
    assert not tap.stale and tap.status == "live"
    tap.stop()
    assert procs[0].terminated
    assert not tap._thread.is_alive()
    assert tap not in tap_mod._live


def test_a_stream_that_ends_is_reconnected():
    procs = []

    def spawn(argv, log):
        procs.append(FakeProc())
        return procs[-1]

    tap = AudioTap("http://x/y", spawn=spawn, ffmpeg="ffmpeg").start()
    assert _wait(lambda: procs)
    procs[0].terminate()                     # EOF: the station hung up
    assert _wait(lambda: len(procs) == 2)
    tap.stop()


def test_no_ffmpeg_is_an_error_not_a_crash():
    tap = AudioTap("http://x/y", ffmpeg=None)
    tap._ffmpeg = None
    tap.start()
    assert tap.error and "ffmpeg" in tap.error and tap.stale


def test_delay_reaches_back_in_time():
    tap = AudioTap("http://x/y", ffmpeg="ffmpeg", capacity_s=2)
    tap.ring.write(np.column_stack([np.arange(RATE * 2), np.arange(RATE * 2)]).astype(np.float32))
    newest = tap.window(1)[0, 0]
    half = tap.window(1, delay_s=0.5)[0, 0]
    assert newest - half == RATE // 2
