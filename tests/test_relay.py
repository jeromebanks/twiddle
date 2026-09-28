"""What the relay has to get right, pinned so it cannot quietly regress.

Three of these encode bugs that are invisible in a short demo and fatal in
use: a stream that dies when Spotify is paused, a stereo image that inverts
for the rest of the session, and one slow listener stalling every other.
"""
import queue
import threading
import time

import pytest

from twiddle import relay


# ---- the jitter buffer -----------------------------------------------------


def test_take_pads_with_silence_rather_than_returning_short():
    """A silent source must still yield a full chunk, or the stream stalls.

    librespot writes nothing at all while playback is paused. If that reached
    the encoder as "no data", the speaker's buffer would drain and Sonos would
    hang up -- and pressing play in the Spotify app would not bring it back,
    because nothing re-issues the URI.
    """
    j = relay.JitterBuffer(capacity=10_000)
    j.write(b"\x01\x02\x03\x04")
    out = j.take(16)
    assert len(out) == 16
    assert out == b"\x01\x02\x03\x04" + b"\x00" * 12
    assert j.underrun_chunks == 1
    assert j.silence_bytes == 12


def test_take_never_splits_a_sample_frame():
    """Handing over 3 of 4 bytes would swap the channels for good.

    The missing byte gets padded with silence, and from then on every sample
    is one byte out of phase -- audible as the stereo image inverting, and
    nothing downstream ever recovers.
    """
    j = relay.JitterBuffer(capacity=10_000)
    j.write(b"\x01\x02\x03")           # three bytes: not a whole frame
    out = j.take(8)
    assert len(out) == 8
    # The partial frame is held back, not split.
    assert out == b"\x00" * 8
    j.write(b"\x04")
    assert j.take(4) == b"\x01\x02\x03\x04"


def test_write_blocks_once_full_so_a_fast_source_is_rate_limited():
    """The backpressure here is what makes `tone` and `file` play at 1x."""
    j = relay.JitterBuffer(capacity=64)
    j.write(b"x" * 64)
    done = threading.Event()

    def filler():
        j.write(b"y" * 8)
        done.set()

    threading.Thread(target=filler, daemon=True).start()
    assert not done.wait(0.4), "write returned while the buffer was full"
    j.take(64)
    assert done.wait(2.0), "write stayed blocked after the buffer drained"


# ---- the pacer -------------------------------------------------------------


def test_output_rate_holds_when_the_source_stalls_completely():
    """The property the whole design exists to guarantee.

    With a source producing nothing at all, the encoder must still be fed
    176400 bytes per second of silence. Anything less and the stream dies
    whenever anyone pauses.
    """
    j = relay.JitterBuffer(capacity=relay.PCM_BYTES_PER_SEC)  # never written to
    stop = threading.Event()
    written = []
    t0 = time.monotonic()
    t = threading.Thread(target=relay.pace, args=(j, written.append, stop),
                         daemon=True)
    t.start()
    time.sleep(1.0)
    stop.set()
    t.join(timeout=2)
    elapsed = time.monotonic() - t0
    rate = sum(len(c) for c in written) / elapsed
    assert 0.8 <= rate / relay.PCM_BYTES_PER_SEC <= 1.25, (
        f"paced at {rate:,.0f} B/s, expected ~{relay.PCM_BYTES_PER_SEC:,}")
    assert all(c == b"\x00" * len(c) for c in written), "stall should be silence"


def test_pacer_stops_when_the_encoder_goes_away():
    j = relay.JitterBuffer(capacity=1024)
    stop = threading.Event()

    def broken(_data):
        raise BrokenPipeError()

    t = threading.Thread(target=relay.pace, args=(j, broken, stop), daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive(), "pacer kept running after the encoder died"


# ---- fan-out ---------------------------------------------------------------


def test_one_stuck_listener_does_not_block_the_others():
    """A speaker that stopped reading must not back up into the encoder.

    Sonos does exactly this: it buffers ahead, stops reading, and only later
    closes the socket. If that applied backpressure to the encoder, every
    other listener would stutter in sympathy.
    """
    f = relay.Fanout(queue_chunks=4)
    _stuck, _sq = f.subscribe()
    healthy_key, hq = f.subscribe()
    for i in range(20):
        f.publish(bytes([i]))
    assert f.dropped_chunks > 0, "a full queue should have shed old frames"
    # The healthy listener still holds its most recent frames...
    drained = [hq.get_nowait() for _ in range(hq.qsize())]
    assert drained[-1] == bytes([19])
    f.unsubscribe(healthy_key)
    assert f.listeners == 1


def test_close_releases_every_listener():
    f = relay.Fanout()
    _key, q = f.subscribe()
    f.close()
    assert q.get(timeout=1) is None
    assert f.listeners == 0


def test_subscribe_counts_are_reported_for_status():
    f = relay.Fanout()
    k1, _ = f.subscribe()
    f.subscribe()
    f.unsubscribe(k1)
    assert f.listeners == 1
    assert f.total_clients == 2


def test_an_observer_is_never_counted_as_a_speaker():
    """The visualizer's tap reads the stream too, but `listeners`,
    `clients_total` and `dropped_chunks` are the experiment's evidence about
    the Roams. A stalled visualizer must not read as "the Roam stopped
    reading", so it has counters of its own."""
    f = relay.Fanout(queue_chunks=4)
    _obs, oq = f.subscribe(observer=True)
    _spk, sq = f.subscribe()
    for i in range(20):
        f.publish(bytes([i]))
        sq.get_nowait()                          # the speaker keeps up
    assert (f.listeners, f.total_clients, f.dropped_chunks) == (1, 1, 0)
    assert f.observers == 1 and f.observer_dropped > 0
    assert [oq.get_nowait() for _ in range(oq.qsize())][-1] == bytes([19])
    f.close()
    assert f.observers == 0


def _serve(fanout):
    from http.server import ThreadingHTTPServer
    handler = type("_T", (relay._StreamHandler,), {"fanout": fanout})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}{relay.STREAM_PATH}"


def test_the_stream_says_it_knows_observers_and_head_subscribes_no_one():
    import urllib.request
    f = relay.Fanout()
    srv, url = _serve(f)
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.headers[relay.OBSERVER_HEADER] == "1"
        assert (f.listeners, f.observers, f.total_clients) == (0, 0, 0)
    finally:
        srv.shutdown()


def test_a_get_with_the_observer_query_subscribes_as_an_observer():
    import urllib.request
    f = relay.Fanout()
    srv, url = _serve(f)
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            f.publish(b"\xff\xfb" + b"\x00" * 400)
            time.sleep(0.01)
    threading.Thread(target=pump, daemon=True).start()
    try:
        with urllib.request.urlopen(f"{url}?{relay.OBSERVER_QUERY}", timeout=5) as resp:
            assert resp.read(100)
            assert (f.observers, f.listeners, f.total_clients) == (1, 0, 0)
            # A speaker's GET is unchanged: the marker is on HEAD only.
            assert resp.headers.get(relay.OBSERVER_HEADER) is None
    finally:
        stop.set()
        f.close()
        srv.shutdown()


# ---- MP3 framing -----------------------------------------------------------


def test_frame_sync_skips_a_partial_frame_at_the_live_edge():
    """A listener joining mid-frame made ffprobe report junk, and clicks.

    Sonos reconnects every time it refills, so an unaligned start is a click
    on every reconnect rather than a one-off.
    """
    header = b"\xff\xfb\x90\x00"          # MPEG1 Layer III, valid bitrate
    buf = b"\x11\x22\x33" + header + b"payload"
    assert relay._frame_sync(buf) == 3


@pytest.mark.parametrize("bad", [
    b"\xff\xe1\x90\x00",   # layer field 00 -- reserved, not a real header
    b"\xff\xfb\xf0\x00",   # bitrate index 1111 -- invalid
    b"\x00\x01\x02\x03",   # no sync word at all
])
def test_frame_sync_rejects_false_positives(bad):
    assert relay._frame_sync(bad) == -1


# ---- process arguments -----------------------------------------------------


def test_the_encoder_is_constant_bitrate_with_no_xing_header():
    """Both matter on a radio stream, and neither is ffmpeg's default.

    A Xing/LAME header describes a file of known length. On an endless stream
    it is a silent frame claiming a bogus duration at the head of every
    listener's connection.
    """
    argv = relay.Relay(source_argv=["true"]).encoder_argv()
    assert "-write_xing" in argv and argv[argv.index("-write_xing") + 1] == "0"
    assert argv[argv.index("-b:a") + 1] == relay.DEFAULT_BITRATE
    assert "-c:a" in argv and argv[argv.index("-c:a") + 1] == "libmp3lame"
    # No `-q:a` anywhere: that would make it VBR.
    assert "-q:a" not in argv


def test_the_encoder_input_matches_the_pcm_contract():
    """If these drift apart from the constants, audio plays at the wrong speed."""
    argv = relay.Relay(source_argv=["true"]).encoder_argv()
    assert argv[argv.index("-ar") + 1] == str(relay.PCM_RATE)
    assert argv[argv.index("-ac") + 1] == str(relay.PCM_CHANNELS)
    assert argv[argv.index("-f") + 1] == "s16le"
    assert relay.PCM_BYTES_PER_SEC == 176_400


def test_spotify_source_pins_the_format_it_claims_to_produce():
    """librespot's default format could change; the ffmpeg flags cannot follow."""
    argv = relay.spotify_source("Test Relay", "/tmp/cache")
    assert argv[argv.index("--format") + 1] == "S16"
    assert argv[argv.index("--backend") + 1] == "pipe"
    # No --device: the pipe backend writes to stdout, which is where the
    # relay reads PCM from.
    assert "--device" not in argv
    # Credentials must persist, or every start needs a browser sign-in.
    assert "--system-cache" in argv


def test_login_never_takes_a_password():
    argv = relay.login_argv("Test Relay", "/tmp/cache")
    assert "--enable-oauth" in argv
    assert "--password" not in argv and "-p" not in argv


def test_pump_chunk_is_a_whole_number_of_sample_frames():
    assert relay.PUMP_CHUNK % relay.PCM_FRAME_BYTES == 0
    assert 0 < relay.PUMP_CHUNK <= relay.PCM_BYTES_PER_SEC


# ---- end to end, no speaker ------------------------------------------------


@pytest.mark.skipif(not relay.shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_a_tone_arrives_over_http_as_real_time_mp3():
    """The whole chain minus Spotify and minus the speaker.

    Proves source -> pacer -> ffmpeg -> fan-out -> HTTP, and that the result
    is paced: roughly one second of audio per second, not a burst.
    """
    import urllib.request

    rly = relay.Relay(source_argv=relay.tone_source(440), port=0)
    with rly:
        deadline = time.monotonic() + 15
        while rly.encoded_bytes == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert rly.encoded_bytes > 0, "the encoder produced nothing"
        url = f"http://127.0.0.1:{rly.port}{relay.STREAM_PATH}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            assert resp.headers["Content-Type"] == "audio/mpeg"
            # A Content-Length would make Sonos treat this as a file.
            assert resp.headers.get("Content-Length") is None
            assert resp.headers.get("Transfer-Encoding") is None
            t0 = time.monotonic()
            data = b""
            while time.monotonic() - t0 < 3.0:
                data += resp.read(4096)
        assert data[:1] == b"\xff", "listener did not start on a frame boundary"
        rate = len(data) / (time.monotonic() - t0)
        # 192kbit/s = 24000 B/s. Generous bounds: this asserts "real time",
        # not "to the byte".
        assert 15_000 < rate < 40_000, f"streamed at {rate:,.0f} B/s"
        assert rly.stats()["clients_total"] >= 1


# ---- login -----------------------------------------------------------------


def test_login_watches_for_credentials_rather_than_process_exit(tmp_path, monkeypatch):
    """librespot keeps running after it authenticates; the command must not.

    It stays up as a live device session, so waiting for the process to exit
    turns a sign-in that worked into a reported timeout. Watching the
    credentials file is what makes the outcome truthful.
    """
    import argparse

    from twiddle import relay_cli

    creds = tmp_path / "credentials.json"

    class FakeProc:
        """Authenticates, writes credentials, then keeps running forever."""

        def __init__(self, *_a, **_kw):
            creds.write_text("{}")
            self.terminated = False

        def poll(self):
            return None if not self.terminated else 0

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True

    made = []
    monkeypatch.setattr(relay_cli.subprocess, "Popen",
                        lambda *a, **kw: made.append(FakeProc()) or made[-1])
    args = argparse.Namespace(cache=str(tmp_path), device_name="T",
                              timeout=10, json=True, force=False)
    assert relay_cli.cmd_login(args) == 0
    assert made[0].terminated, "the device session was left running"


def test_login_reports_failure_when_nothing_was_written(tmp_path, monkeypatch):
    import argparse

    from twiddle import relay_cli

    class DeadProc:
        def __init__(self, *_a, **_kw):
            pass

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 1

        def kill(self):
            pass

    monkeypatch.setattr(relay_cli.subprocess, "Popen", DeadProc)
    args = argparse.Namespace(cache=str(tmp_path), device_name="T",
                              timeout=5, json=True, force=False)
    assert relay_cli.cmd_login(args) == 1


def test_login_does_not_re_prompt_when_already_signed_in(tmp_path):
    """Re-running with a valid cache must not hang waiting for a fresh write.

    librespot may reuse cached credentials without rewriting the file, so
    watching for a new write would block until the timeout on a session that
    is in fact perfectly signed in.
    """
    import argparse

    from twiddle import relay_cli

    (tmp_path / "credentials.json").write_text("{}")
    args = argparse.Namespace(cache=str(tmp_path), device_name="T",
                              timeout=5, json=True, force=False)
    assert relay_cli.cmd_login(args) == 0


def test_librespot_does_not_attenuate_before_the_speaker_does():
    """Two volume controls in series made real music inaudible.

    librespot defaults to software volume 50 on a 60dB log curve, roughly
    -30dB, which multiplies with the speaker's own setting. Measured on this
    relay: the stream carried genuine audio at mean -50.8dB -- silent through
    a Roam at volume 25 -- and jumped to -21.8dB when softvol went to 100.
    Loudness belongs to exactly one control, and it is the speaker's.
    """
    argv = relay.spotify_source("Test Relay", "/tmp/cache")
    assert argv[argv.index("--initial-volume") + 1] == "100"
    assert argv[argv.index("--volume-ctrl") + 1] == "fixed"
