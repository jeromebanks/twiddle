"""`twiddle alarm serve`: audio from this Mac, every serve a bounded span.

The server is real, on 127.0.0.1, with a temp directory of files; the journal
is conftest's temp file. No speaker, network or Spotify is touched.
"""
import http.client
import json
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from twiddle import alarm_cli, cli, play, report
from twiddle.alarms import server
from twiddle.alarms.sources import bandcamp as bc
from tests.test_alarm_cli import household, run

T0 = datetime(2026, 10, 4, 6, 0, 0, tzinfo=timezone.utc)
SPEAKER = "192.168.1.4"


def journal():
    p = play.INTERVENTION_LOG
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def write(rec):
    with play.INTERVENTION_LOG.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def iso(t):
    return t.isoformat(timespec="milliseconds")


@pytest.fixture
def sounds(tmp_path):
    d = tmp_path / "sounds"
    (d / "sub").mkdir(parents=True)
    (d / "bell.mp3").write_bytes(b"ID3" + b"x" * 1000)
    (d / "sub" / "rise.wav").write_bytes(b"RIFF" + b"y" * 100)
    (d / "notes.txt").write_text("not audio")
    (tmp_path / "secret.mp3").write_bytes(b"outside")
    return d


@pytest.fixture
def srv(sounds):
    s = server.AlarmServer(sounds, None, port=0, host="127.0.0.1")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.stop()


def get(srv, path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    c.request(method, path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def wait_for(cond, what, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# ---- what a request may name -------------------------------------------------

@pytest.mark.parametrize("path", ["/nope.mp3", "/", "/sub", "/sub/", "/notes.txt",
                                  "/../secret.mp3", "/%2e%2e/secret.mp3", "/bell.mp3/..",
                                  "/%ff.mp3", "/bell.mp3%00"])
def test_an_unknown_or_unresolvable_request_fails_fast_and_opens_no_span(srv, path):
    started = time.monotonic()
    status, _ = get(srv, path)
    assert status in (403, 404)
    assert time.monotonic() - started < 2
    assert journal() == []


@pytest.mark.parametrize("name", ["x" * 300 + ".mp3", "a/" * 3000 + "b.mp3"])
def test_a_name_the_filesystem_rejects_is_a_404(srv, name):
    assert server.resolve(srv.root, "/" + name) is None
    assert get(srv, "/" + name)[0] == 404


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_a_file_deleted_after_its_name_resolved_is_a_404(srv, sounds, monkeypatch, method):
    gone = sounds / "gone.mp3"
    gone.write_bytes(b"x")
    real = server.resolve

    def resolve_then_delete(root, request_path):
        found = real(root, request_path)
        gone.unlink()
        return found
    monkeypatch.setattr(server, "resolve", resolve_then_delete)
    assert get(srv, "/gone.mp3", method)[0] == 404
    assert journal() == []


def test_a_symlink_loop_is_a_404_not_a_dropped_request(srv, sounds):
    (sounds / "loop.mp3").symlink_to(sounds / "loop.mp3")
    assert server.resolve(sounds, "/loop.mp3") is None
    assert get(srv, "/loop.mp3")[0] == 404


def test_a_known_audio_file_is_served_as_audio(srv):
    for path, body in (("/bell.mp3", b"ID3" + b"x" * 1000), ("/sub/rise.wav", b"RIFF" + b"y" * 100)):
        assert get(srv, path) == (200, body)


def test_this_mac_can_fetch_so_a_curl_can_try_it(sounds):
    s = server.AlarmServer(sounds, {"10.9.9.9"}, port=0, host="127.0.0.1")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        assert get(s, "/bell.mp3")[0] == 200      # loopback is this Mac
    finally:
        s.stop()


def test_a_client_that_is_neither_a_speaker_nor_this_mac_is_refused():
    assert not server.permitted("192.168.1.77", {"10.9.9.9"})
    assert server.permitted("10.9.9.9", {"10.9.9.9"})
    assert server.permitted("127.0.0.1", {"10.9.9.9"})


def test_a_head_request_is_answered_without_a_span(srv):
    assert get(srv, "/bell.mp3", "HEAD")[0] == 200
    assert journal() == []


# ---- every serve is a bounded span -------------------------------------------

def test_a_serve_is_one_span_with_an_id_and_a_bound(srv):
    srv.max_s = 90
    assert get(srv, "/bell.mp3")[0] == 200
    wait_for(lambda: len(journal()) == 2, "the span to close")
    start, end = journal()
    assert (start["action"], end["action"]) == ("alarm_serve_start", "alarm_serve_end")
    assert start["span"] and end["span"]
    assert start["span_id"] == end["span_id"]
    assert start["max_s"] == 90 and start["ip"] == end["ip"] == "127.0.0.1"
    assert start["file"] == "bell.mp3"
    _, spans = report._load_interventions(play.INTERVENTION_LOG)
    assert [(n, ip) for _, _, n, ip in spans] == [("alarm_serve", "127.0.0.1")]


def test_two_overlapping_serves_to_one_speaker_are_two_independent_spans(sounds):
    (sounds / "long.mp3").write_bytes(b"z" * (48 * 1024 * 1024))   # more than the sockets buffer
    s = server.AlarmServer(sounds, None, port=0, host="127.0.0.1")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    conns = []
    try:
        for _ in range(2):
            c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
            c.request("GET", "/long.mp3")
            r = c.getresponse()
            assert r.status == 200
            conns.append((c, r))
        wait_for(lambda: sum(j["action"] == "alarm_serve_start" for j in journal()) == 2,
                 "both serves to open")
        starts = [j for j in journal() if j["action"] == "alarm_serve_start"]
        assert starts[0]["span_id"] != starts[1]["span_id"]
        assert starts[0]["ip"] == starts[1]["ip"]
        # Both are open, as two spans, until their own end arrives.
        _, spans = report._load_interventions(play.INTERVENTION_LOG)
        assert len(spans) == 2
        # The first hangs up; the second is still serving, still open.
        conns[0][1].close()      # the response holds the socket, not the connection
        conns[0][0].close()
        wait_for(lambda: sum(j["action"] == "alarm_serve_end" for j in journal()) == 1,
                 "the first serve to end")
        ended = next(j for j in journal() if j["action"] == "alarm_serve_end")
        assert ended["span_id"] == starts[0]["span_id"]
        still = report.open_spans(play.INTERVENTION_LOG)
        assert [o["span_id"] for o in still] == [starts[1]["span_id"]]
    finally:
        for c, r in conns:
            r.close()
            c.close()
        s.stop()
    assert report.open_spans(play.INTERVENTION_LOG) == []


def test_the_pairing_keeps_interleaved_spans_of_one_action_and_speaker_apart(tmp_path):
    for action, sid, t in (("start", "a", 0), ("start", "b", 10), ("end", "a", 20), ("end", "b", 50)):
        write({"ts": iso(T0 + timedelta(seconds=t)), "action": f"alarm_serve_{action}",
               "ip": SPEAKER, "span": True, "span_id": sid, "max_s": 600})
    _, spans = report._load_interventions(play.INTERVENTION_LOG)
    assert sorted((e - s) for s, e, _, _ in spans) == [20, 40]


def test_stopping_the_server_closes_the_spans_still_open(sounds):
    (sounds / "long.mp3").write_bytes(b"z" * (48 * 1024 * 1024))
    s = server.AlarmServer(sounds, None, port=0, host="127.0.0.1")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    c.request("GET", "/long.mp3")
    r = c.getresponse()
    wait_for(lambda: len(journal()) == 1, "the serve to open")
    s.stop()
    # the transfer was cut before the span was recorded as over: what is left
    # to read is only what was already in flight
    got = 0
    try:
        while chunk := r.read(1 << 20):
            got += len(chunk)
    except http.client.IncompleteRead:
        pass
    assert got < 48 * 1024 * 1024
    r.close()
    c.close()
    assert [j["action"] for j in journal()].count("alarm_serve_end") == 1
    assert report.open_spans(play.INTERVENTION_LOG) == []


def test_a_reader_that_stalls_cannot_hold_a_span_past_its_bound(sounds):
    (sounds / "long.mp3").write_bytes(b"z" * (48 * 1024 * 1024))
    s = server.AlarmServer(sounds, None, port=0, max_s=0.5, host="127.0.0.1")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    try:
        c.request("GET", "/long.mp3")
        r = c.getresponse()            # headers only; the body is never read
        started = time.monotonic()
        wait_for(lambda: len(journal()) == 2, "the stalled serve to end", timeout=4)
        assert time.monotonic() - started < 3
        r.close()
    finally:
        c.close()
        s.stop()


def test_the_server_refuses_a_bound_that_is_not_finite_and_positive(sounds):
    for bad in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            server.AlarmServer(sounds, None, port=0, max_s=bad)


def test_a_handler_accepted_before_stop_cannot_open_a_span_after_it(sounds):
    s = server.AlarmServer(sounds, None, port=0, host="127.0.0.1")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    paused, go = threading.Event(), threading.Event()
    real = s._open

    def slow(*a):
        paused.set()
        go.wait(5)
        return real(*a)
    s._open = slow
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    try:
        c.request("GET", "/bell.mp3")
        assert paused.wait(5)          # the handler is accepted, not yet registered
        s.stop()
        go.set()
        assert c.getresponse().status == 503
        assert journal() == []
    finally:
        go.set()
        c.close()


# ---- a serve killed mid-stream -----------------------------------------------

VANISH = {"kind": "vanish", "ip": SPEAKER, "name": "Sonos Roam", "probe": {"http_ok": True}}


def test_a_killed_serve_ends_at_start_plus_max_s_and_a_later_vanish_still_counts(tmp_path):
    write({"ts": iso(T0), "action": "alarm_serve_start", "ip": SPEAKER, "span": True,
           "span_id": "dead", "max_s": 600})            # killed: never closed
    mon = tmp_path / "monitor.jsonl"
    mon.write_text("\n".join(json.dumps(r) for r in (
        {"kind": "sample", "samples": []},
        VANISH | {"ts": iso(T0 + timedelta(seconds=300))},       # inside the bound
        VANISH | {"ts": iso(T0 + timedelta(seconds=601 + 45))}))) # after it, past the window
    _, spans = report._load_interventions(play.INTERVENTION_LOG)
    assert spans == [(T0.timestamp(), T0.timestamp() + 600, "alarm_serve", SPEAKER)]
    findings = report.summarise_log(mon, play.INTERVENTION_LOG)
    assert any("our own commands" in f.title for f in findings)      # the first, discounted
    assert any("left the household" in f.title for f in findings)    # the second, a fault


def test_records_without_span_id_or_max_s_are_read_as_before(tmp_path):
    for action, t in (("serving_start", 0), ("serving_end", 360)):
        write({"ts": iso(T0 + timedelta(seconds=t)), "action": action, "ip": SPEAKER,
               "span": True})
    write({"ts": iso(T0 + timedelta(seconds=1000)), "action": "relay_start", "ip": SPEAKER,
           "span": True})                                  # never closed, no bound
    _, spans = report._load_interventions(play.INTERVENTION_LOG)
    assert (T0.timestamp(), T0.timestamp() + 360, "serving", SPEAKER) in spans
    assert (T0.timestamp() + 1000, float("inf"), "relay", SPEAKER) in spans


def test_an_old_crashed_span_is_still_replaced_by_the_next_start_of_its_kind(tmp_path):
    # Read as before: of two id-less starts, the second replaces the unclosed
    # first, so a relay that crashed once does not discount every fault after.
    for action, t in (("relay_start", 0), ("relay_start", 100), ("relay_end", 160)):
        write({"ts": iso(T0 + timedelta(seconds=t)), "action": action, "ip": SPEAKER,
               "span": True})
    _, spans = report._load_interventions(play.INTERVENTION_LOG)
    assert spans == [(T0.timestamp() + 100, T0.timestamp() + 160, "relay", SPEAKER)]
    mon = tmp_path / "monitor.jsonl"
    mon.write_text("\n".join(json.dumps(r) for r in (
        {"kind": "sample", "samples": []},
        VANISH | {"ts": iso(T0 + timedelta(hours=5))})))
    assert any("left the household" in f.title
               for f in report.summarise_log(mon, play.INTERVENTION_LOG))


def test_an_old_crashed_span_is_replaced_by_the_next_start_even_an_id_ful_one():
    write({"ts": iso(T0), "action": "relay_start", "ip": SPEAKER, "span": True})  # crashed, old
    write({"ts": iso(T0 + timedelta(seconds=100)), "action": "relay_start", "ip": SPEAKER,
           "span": True, "span_id": "new", "max_s": 600})
    write({"ts": iso(T0 + timedelta(seconds=160)), "action": "relay_end", "ip": SPEAKER,
           "span": True, "span_id": "new"})
    _, spans = report._load_interventions(play.INTERVENTION_LOG)
    assert spans == [(T0.timestamp() + 100, T0.timestamp() + 160, "relay", SPEAKER)]


def test_an_end_without_an_id_closes_the_latest_open_span_that_has_one(tmp_path):
    write({"ts": iso(T0), "action": "x_start", "ip": SPEAKER, "span": True,
           "span_id": "a", "max_s": 600})
    write({"ts": iso(T0 + timedelta(seconds=30)), "action": "x_end", "ip": SPEAKER,
           "span": True})
    _, spans = report._load_interventions(play.INTERVENTION_LOG)
    assert spans == [(T0.timestamp(), T0.timestamp() + 30, "x", SPEAKER)]


def test_every_span_start_the_code_writes_has_an_id_and_a_finite_bound():
    play.journal_span("anything_start", SPEAKER)
    play.journal_span("anything_start", SPEAKER, max_s=5, span_id="x")
    play.journal_span("anything_end", SPEAKER, span_id="x")
    a, b, c = journal()
    assert a["span_id"] and 0 < a["max_s"] < float("inf")
    assert (b["span_id"], b["max_s"]) == ("x", 5) and "max_s" not in c


# ---- the server closes its own crash-left spans ------------------------------

def test_at_start_it_closes_its_own_open_spans_when_they_could_no_longer_run():
    write({"ts": iso(T0), "action": "alarm_serve_start", "ip": SPEAKER, "span": True,
           "span_id": "old", "max_s": 600})
    write({"ts": iso(T0), "action": "alarm_serve_start", "ip": SPEAKER, "span": True,
           "span_id": "done", "max_s": 600})
    write({"ts": iso(T0 + timedelta(seconds=5)), "action": "alarm_serve_end", "ip": SPEAKER,
           "span": True, "span_id": "done"})
    write({"ts": iso(T0), "action": "relay_start", "ip": SPEAKER, "span": True,
           "span_id": "relay", "max_s": 86400})            # a live relay's, not ours
    closed = server.close_stale(now=T0 + timedelta(days=2))
    assert [c["span_id"] for c in closed] == ["old"]
    end = journal()[-1]
    # not two days of real faults discounted: it ends where its bound ran out
    assert (end["action"], end["span_id"], end["stale"]) == ("alarm_serve_end", "old", True)
    assert datetime.fromisoformat(end["ts"]) == T0 + timedelta(seconds=600)
    assert [o["span_id"] for o in report.open_spans(play.INTERVENTION_LOG)] == ["relay"]


def test_a_span_of_a_live_server_is_not_stale_but_a_dead_ones_is(sounds):
    import os, subprocess, sys
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True).stdout.strip()
    for sid, pid in (("live", os.getpid()), ("dead", int(dead))):
        write({"ts": iso(T0), "action": "alarm_serve_start", "ip": SPEAKER, "span": True,
               "span_id": sid, "max_s": 600, "pid": pid})
    closed = server.close_stale(now=T0 + timedelta(hours=1))
    assert [c["span_id"] for c in closed] == ["dead"]
    assert [o["span_id"] for o in report.open_spans(play.INTERVENTION_LOG)] == ["live"]


def test_a_second_server_on_another_port_leaves_the_first_ones_live_serves_open(sounds):
    (sounds / "long.mp3").write_bytes(b"z" * (48 * 1024 * 1024))
    first = server.AlarmServer(sounds, None, port=0, host="127.0.0.1")
    second = server.AlarmServer(sounds, None, port=0, host="127.0.0.1")   # binds fine
    threading.Thread(target=first.serve_forever, daemon=True).start()
    c = http.client.HTTPConnection("127.0.0.1", first.port, timeout=10)
    try:
        c.request("GET", "/long.mp3")
        r = c.getresponse()
        wait_for(lambda: len(journal()) == 1, "the serve to open")
        assert server.close_stale() == []          # what `second`'s start would do
        assert len(report.open_spans(play.INTERVENTION_LOG)) == 1
        r.close()
    finally:
        c.close()
        first.stop()
        second.stop()


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), 0, -5, "x"])
def test_a_span_start_refuses_a_bound_that_is_not_finite_and_positive(bad):
    with pytest.raises(ValueError):
        play.journal_span("anything_start", SPEAKER, max_s=bad)
    assert journal() == []


@pytest.mark.parametrize("bad", ["inf", "nan", "0", "-1", "soon"])
def test_serve_refuses_a_bound_that_is_not_finite_and_positive(bad, sounds, capsys):
    with pytest.raises(SystemExit):
        cli.main(["alarm", "serve", "--host", "127.0.0.1", "--dir", str(sounds), "--max-s", bad])
    capsys.readouterr()
    assert journal() == []


def test_a_stale_span_whose_bound_is_not_up_ends_now():
    write({"ts": iso(T0), "action": "alarm_serve_start", "ip": SPEAKER, "span": True,
           "span_id": "recent", "max_s": 3600})
    server.close_stale(now=T0 + timedelta(seconds=30))
    assert datetime.fromisoformat(journal()[-1]["ts"]) == T0 + timedelta(seconds=30)


# ---- the verb ----------------------------------------------------------------

@pytest.fixture
def fake_house(monkeypatch):
    monkeypatch.setattr(alarm_cli, "_household", lambda args: household())
    monkeypatch.setattr(play, "local_ip_for", lambda peer: "10.0.0.99")   # no socket at all


def test_dry_run_prints_the_plan_and_neither_listens_nor_journals(fake_house, sounds, capsys):
    write({"ts": iso(T0), "action": "alarm_serve_start", "ip": SPEAKER, "span": True,
           "span_id": "old", "max_s": 600})
    before = play.INTERVENTION_LOG.read_text()
    code, out, _ = run(["alarm", "serve", "--host", "127.0.0.1", "--dir", str(sounds), "--port", "0",
                        "--room", "Living Room", "--dry-run", "--json"], capsys)
    assert code == 0
    p = json.loads(out)
    assert p["performed"] is False and p["would"] == "serve"
    assert p["files"] == ["bell.mp3", "sub/rise.wav"]
    assert p["speakers"] == ["10.0.0.13", "10.0.0.14"]
    assert p["stale_spans"] == 1
    assert play.INTERVENTION_LOG.read_text() == before


def test_an_unknown_room_or_directory_is_refused_before_anything_is_bound(fake_house, sounds,
                                                                          capsys):
    code, _, err = run(["alarm", "serve", "--host", "127.0.0.1", "--dir", str(sounds), "--room", "Nowhere"], capsys)
    assert code == 1 and "Nowhere" in err
    code, _, err = run(["alarm", "serve", "--dir", str(sounds / "missing")], capsys)
    assert code == 1 and "not a directory" in err
    assert journal() == []


def test_a_second_serve_on_a_busy_port_fails_and_closes_nobodys_spans(fake_house, sounds,
                                                                      capsys):
    write({"ts": iso(T0), "action": "alarm_serve_start", "ip": SPEAKER, "span": True,
           "span_id": "live", "max_s": 600})
    first = server.AlarmServer(sounds, None, port=0, host="127.0.0.1")
    try:
        code, _, err = run(["alarm", "serve", "--host", "127.0.0.1", "--dir", str(sounds),
                            "--port", str(first.port)], capsys)
    finally:
        first.stop()
    assert code == 1 and "could not listen" in err
    assert [o["span_id"] for o in report.open_spans(play.INTERVENTION_LOG)] == ["live"]


def test_serve_runs_until_interrupted_then_closes_its_spans(fake_house, sounds, capsys,
                                                            monkeypatch):
    write({"ts": iso(T0), "action": "alarm_serve_start", "ip": SPEAKER, "span": True,
           "span_id": "crashed", "max_s": 600})

    def interrupt(self):
        raise KeyboardInterrupt
    monkeypatch.setattr(server.AlarmServer, "serve_forever", interrupt)
    code, out, _ = run(["alarm", "serve", "--host", "127.0.0.1", "--dir", str(sounds), "--port", "0"], capsys)
    assert code == 0 and "1 stale span(s) to close" in out
    assert report.open_spans(play.INTERVENTION_LOG) == []
    assert journal()[-1]["stale"] is True


# ---- Bandcamp through the agent ----------------------------------------------

TRACK = {"page": "https://gulls.bandcamp.com/album/salt", "id": 42}
CDN_HUNG_UP = threading.Event()
MP3 = b"ID3" + b"m" * 5000


@pytest.fixture
def bandcamp_cdn():
    """A stand-in for Bandcamp's mp3 host on loopback: /ok serves audio, anything else 403s."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):
            if self.path == "/silent":          # headers, then nothing, for a while
                self.send_response(200)
                self.end_headers()
                return time.sleep(3)
            if self.path == "/empty":
                self.send_response(200)
                self.send_header("Content-Length", "0")
                return self.end_headers()
            if self.path == "/trickle":         # a first chunk, then a byte at a time, slowly
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(MP3[:100])
                    self.wfile.flush()
                    for _ in range(100):
                        time.sleep(0.1)
                        self.wfile.write(b"t")
                        self.wfile.flush()
                except OSError:
                    pass
                return
            if self.path == "/firstchunk":      # the first chunk's size line, a digit at a time
                try:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                    for _ in range(100):
                        time.sleep(0.1)
                        self.wfile.write(b"0")
                        self.wfile.flush()
                except OSError:
                    CDN_HUNG_UP.set()
                return
            if self.path == "/stall":           # a first chunk, then nothing, for a long while
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(MP3[:100])
                    self.wfile.flush()
                    time.sleep(5)
                except OSError:
                    pass
                return
            if self.path == "/chunked":         # a chunk, then the next chunk's size a digit at a time
                try:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                                     b"64\r\n" + MP3[:100] + b"\r\n")
                    self.wfile.flush()
                    for _ in range(100):
                        time.sleep(0.1)
                        self.wfile.write(b"0")
                        self.wfile.flush()
                except OSError:
                    pass
                return
            if self.path != "/ok":
                return self.send_error(403)
            self.send_response(200)
            self.send_header("Content-Length", str(len(MP3)))
            self.end_headers()
            self.wfile.write(MP3)

    cdn = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=cdn.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{cdn.server_port}"
    cdn.shutdown()
    cdn.server_close()


def agent(sounds, stream_url, **kw):
    s = server.AlarmServer(sounds, None, port=0, host="127.0.0.1", stream_url=stream_url, **kw)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def path_for(track=TRACK):
    return f"{bc.ROUTE}{bc.encode(track)}.mp3"


def test_a_bandcamp_request_resolves_a_fresh_url_and_serves_the_audio_as_a_span(sounds, bandcamp_cdn):
    asked = []
    s = agent(sounds, lambda t: asked.append(t) or f"{bandcamp_cdn}/ok", max_s=90)
    try:
        for _ in range(2):          # an alarm fires again: each request resolves again
            status, body = get(s, path_for())
            assert (status, body) == (200, MP3)
        assert asked == [TRACK, TRACK]
        wait_for(lambda: len(journal()) == 4, "both spans to close")
        start, end = journal()[:2]
        assert (start["action"], end["action"]) == ("alarm_serve_start", "alarm_serve_end")
        assert start["span_id"] == end["span_id"] and start["max_s"] == 90
        assert start["file"] == "bandcamp:42"
    finally:
        s.stop()


def test_a_bandcamp_response_says_what_it_is(sounds, bandcamp_cdn):
    s = agent(sounds, lambda t: f"{bandcamp_cdn}/ok")
    try:
        c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
        c.request("GET", path_for())
        r = c.getresponse()
        assert (r.status, r.getheader("Content-Type"), r.getheader("Content-Length")) == (
            200, "audio/mpeg", str(len(MP3)))
        r.read()
        c.close()
    finally:
        s.stop()


def test_a_head_on_a_bandcamp_track_is_answered_with_no_span(sounds, bandcamp_cdn):
    s = agent(sounds, lambda t: f"{bandcamp_cdn}/ok")
    try:
        assert get(s, path_for(), "HEAD") == (200, b"")
        assert get(s, bc.ROUTE + "nope.mp3", "HEAD")[0] == 404
        assert journal() == []
    finally:
        s.stop()


def boom(exc):
    def resolve(_track):
        raise exc
    return resolve


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("resolver, status", [
    (boom(LookupError("no longer streamable")), 502),
    (boom(RuntimeError("Bandcamp is resting")), 502),
    (boom(OSError("network down")), 502),
    (lambda t: "file:///etc/passwd", 502),
    (lambda t: "http://127.0.0.1:9/never", 502),     # nothing listening
])
def test_when_bandcamp_cant_be_resolved_the_request_fails_with_no_span(sounds, resolver, status, method):
    s = agent(sounds, resolver)
    try:
        assert get(s, path_for(), method)[0] == status
        assert journal() == []
    finally:
        s.stop()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_an_upstream_that_sends_headers_and_no_audio_fails_fast_with_no_span(sounds, bandcamp_cdn, method):
    s = agent(sounds, lambda t: f"{bandcamp_cdn}/silent", resolve_s=0.3)
    try:
        t0 = time.monotonic()
        assert get(s, path_for(), method)[0] == 504
        assert time.monotonic() - t0 < 2
        assert journal() == []
    finally:
        s.stop()


def test_a_first_read_that_trickles_is_cut_at_the_deadline_and_its_connection_closed(sounds, bandcamp_cdn):
    CDN_HUNG_UP.clear()
    s = agent(sounds, lambda t: f"{bandcamp_cdn}/firstchunk", resolve_s=0.3)
    try:
        t0 = time.monotonic()
        assert get(s, path_for())[0] == 504
        assert time.monotonic() - t0 < 1
        assert CDN_HUNG_UP.wait(3), "the abandoned worker still holds the upstream connection"
        assert journal() == []
    finally:
        s.stop()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_an_upstream_with_an_empty_body_is_a_502_with_no_span(sounds, bandcamp_cdn, method):
    s = agent(sounds, lambda t: f"{bandcamp_cdn}/empty")
    try:
        assert get(s, path_for(), method)[0] == 502
        assert journal() == []
    finally:
        s.stop()


def test_an_upstream_that_goes_silent_after_its_first_chunk_hits_the_idle_limit_not_max_s(
        sounds, bandcamp_cdn, monkeypatch):
    monkeypatch.setattr(server, "UPSTREAM_S", 0.3)
    s = agent(sounds, lambda t: f"{bandcamp_cdn}/stall", max_s=600)
    try:
        t0 = time.monotonic()
        status, body = get(s, path_for())
        assert status == 200 and body == MP3[:100]
        assert time.monotonic() - t0 < 2
        wait_for(lambda: len(journal()) == 2, "the span to close")
    finally:
        s.stop()


@pytest.mark.parametrize("path", ["trickle", "chunked"])
def test_a_trickling_upstream_cannot_hold_the_span_past_its_bound(sounds, bandcamp_cdn, path):
    """Bytes (or a chunk header's digits) keep arriving, so no read ever times out."""
    s = agent(sounds, lambda t: f"{bandcamp_cdn}/{path}", max_s=0.3)
    try:
        t0 = time.monotonic()
        status, body = get(s, path_for())
        assert status == 200 and body.startswith(MP3[:100])
        assert time.monotonic() - t0 < 0.9
        wait_for(lambda: len(journal()) == 2, "the span to close")
    finally:
        s.stop()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_an_upstream_refusal_is_a_502_with_no_span(sounds, bandcamp_cdn, method):
    s = agent(sounds, lambda t: f"{bandcamp_cdn}/expired")
    try:
        assert get(s, path_for(), method)[0] == 502
        assert journal() == []
    finally:
        s.stop()


def test_a_resolver_that_hangs_fails_fast_not_when_it_finishes(sounds, bandcamp_cdn):
    release = threading.Event()
    s = agent(sounds, lambda t: release.wait(10) and f"{bandcamp_cdn}/ok", resolve_s=0.2)
    try:
        t0 = time.monotonic()
        assert get(s, path_for())[0] == 504
        assert time.monotonic() - t0 < 2
        assert journal() == []
    finally:
        release.set()
        s.stop()


def test_audio_opened_after_the_deadline_is_closed_not_leaked(sounds):
    release, closed = threading.Event(), threading.Event()

    class Late:
        def read(self, n):
            return b"audio"

        def close(self):
            closed.set()

    s = agent(sounds, lambda t: "http://x/", resolve_s=0.1,
              fetch=lambda url: release.wait(5) and Late())
    try:
        assert get(s, path_for())[0] == 504
        release.set()
        assert closed.wait(5)
    finally:
        s.stop()


@pytest.mark.parametrize("name", ["", "x.mp3", "e30.mp3", "bnVsbA.mp3", "a/b.mp3",
                                  "eyJwIjoiZmlsZTovLy9ldGMvcGFzc3dkIn0.mp3"])
def test_a_bandcamp_path_that_isnt_a_token_for_a_page_is_a_404_that_asks_nobody(sounds, name):
    asked = []
    s = agent(sounds, lambda t: asked.append(t))
    try:
        assert get(s, bc.ROUTE + name)[0] == 404
        assert asked == [] and journal() == []
    finally:
        s.stop()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_a_client_that_is_not_a_speaker_gets_no_bandcamp_either(sounds, monkeypatch, method):
    asked = []
    s = agent(sounds, lambda t: asked.append(t))
    monkeypatch.setattr(server, "permitted", lambda ip, speakers: False)
    try:
        assert get(s, path_for(), method)[0] == 403
        assert asked == [] and journal() == []
    finally:
        s.stop()


def test_stopping_the_agent_closes_a_bandcamp_span_still_open(sounds):
    class Endless:
        headers = {}

        def read(self, n):
            time.sleep(0.01)
            return b"x" * n

        def close(self):
            pass

    s = agent(sounds, lambda t: "http://x/", fetch=lambda url: Endless())
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
    c.request("GET", path_for())
    c.getresponse()
    wait_for(lambda: len(journal()) == 1, "the span to open")
    s.stop()
    wait_for(lambda: len(journal()) == 2, "the span to close")
    assert journal()[1]["action"] == "alarm_serve_end"
    c.close()


def test_the_agent_refuses_a_deadline_that_is_not_finite_and_positive(sounds):
    for bad in (0, -1, float("inf"), None):
        with pytest.raises(ValueError, match="resolve_s"):
            server.AlarmServer(sounds, None, port=0, host="127.0.0.1", resolve_s=bad)


def test_a_bundled_sound_is_served_from_its_route_whatever_dir_is_served(srv):
    from twiddle.alarms.sources import sound
    assert get(srv, "/sound/bell.mp3") == (200, sound.file_of("bell").read_bytes())
    assert get(srv, "/sound/bell.mp3", "HEAD")[0] == 200
    assert [r["action"] for r in journal()] == ["alarm_serve_start", "alarm_serve_end"]
    assert journal()[0]["file"] == "sound:bell"


@pytest.mark.parametrize("path", ["/sound/nope.mp3", "/sound/",
                                  "/sound/bell.wav", "/sound/%2e%2e/secret.mp3",
                                  "/sound/../bell.mp3", "/sound/sub/rise.wav"])
def test_an_unknown_sound_is_a_404_with_no_span(srv, path):
    before = len(journal())
    status, _ = get(srv, path)
    assert status == 404 and len(journal()) == before


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("target", ["http://[", "/sound/http://[", "//["])
def test_a_request_target_that_does_not_parse_is_a_404_not_a_dropped_connection(srv, method, target):
    before = len(journal())
    with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as c:   # http.client refuses these
        c.sendall(f"{method} {target} HTTP/1.0\r\n\r\n".encode())
        reply = b""
        while chunk := c.recv(4096):
            reply += chunk
    assert reply.startswith(b"HTTP/1.0 404") and len(journal()) == before


# ---- the launchd agent ---------------------------------------------------------

@pytest.fixture
def launchd_agent(monkeypatch, tmp_path):
    from twiddle import daemon
    calls = []
    monkeypatch.setattr(daemon, "AGENT_DIR", tmp_path / "agents")
    monkeypatch.setattr(daemon, "_which", lambda cmd: f"/bin/{cmd}")
    # No SSDP, no load_device: anchor choice is mocked, and a tripwire guards the rest.
    monkeypatch.setattr(daemon, "pick_anchor", lambda: "10.0.0.13")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(
        OSError("no network in tests")))
    monkeypatch.setattr(daemon, "install_alarm",
                        lambda *a, **k: calls.append(("install", a)) or daemon.plist_path(daemon.ALARM_LABEL))
    monkeypatch.setattr(daemon, "uninstall", lambda label=daemon.LABEL: calls.append(("uninstall", label)) or True)
    monkeypatch.setattr(daemon, "status", lambda label=daemon.LABEL: "state = running")
    monkeypatch.setattr(daemon, "serving", lambda port, host="127.0.0.1", **k: True)
    return calls


def test_install_dry_run_resolves_rooms_and_writes_nothing(fake_house, launchd_agent, sounds, capsys):
    calls = launchd_agent
    before = play.INTERVENTION_LOG.read_text() if play.INTERVENTION_LOG.exists() else ""
    code, out, _ = run(["alarm", "serve", "--install", "--dir", str(sounds), "--room", "Living Room",
                        "--dry-run", "--json"], capsys)
    p = json.loads(out)
    assert code == 0 and p["would"] == "install" and p["performed"] is False
    assert calls == []
    assert (play.INTERVENTION_LOG.read_text() if play.INTERVENTION_LOG.exists() else "") == before


def test_install_journals_and_hands_the_plan_to_the_agent(fake_house, launchd_agent, sounds, capsys):
    calls = launchd_agent
    code, _, _ = run(["alarm", "serve", "--install", "--dir", str(sounds), "--room", "Living Room"], capsys)
    assert code == 0
    (kind, args), = calls
    assert kind == "install"
    assert args[1] == sounds.resolve() and args[4] == ["Living Room"]
    rec = json.loads(play.INTERVENTION_LOG.read_text().splitlines()[-1])
    assert rec["action"] == "alarm_serve_install"


def test_install_refuses_an_unknown_room_or_missing_dir_before_writing(fake_house, launchd_agent, sounds, capsys):
    calls = launchd_agent
    code, _, err = run(["alarm", "serve", "--install", "--dir", str(sounds), "--room", "Nowhere"], capsys)
    assert code == 1 and "Nowhere" in err
    code, _, _ = run(["alarm", "serve", "--install"], capsys)
    assert code == 1
    assert calls == []


def test_uninstall_is_journalled_and_dry_run_is_not(launchd_agent, capsys):
    calls = launchd_agent
    code, out, _ = run(["alarm", "serve", "--uninstall", "--dry-run", "--json"], capsys)
    assert json.loads(out)["performed"] is False and calls == []
    code, _, _ = run(["alarm", "serve", "--uninstall"], capsys)
    assert code == 0 and calls == [("uninstall", "local.twiddle.alarmserver")]
    assert json.loads(play.INTERVENTION_LOG.read_text().splitlines()[-1])["action"] == "alarm_serve_uninstall"


def test_status_is_alive_when_the_job_runs_and_the_port_answers_else_stale(
        launchd_agent, monkeypatch, capsys):
    from twiddle import daemon
    code, out, _ = run(["alarm", "serve", "--status", "--json"], capsys)
    assert code == 0 and json.loads(out)["alive"] is True
    monkeypatch.setattr(daemon, "serving", lambda port, host="127.0.0.1", **k: False)
    code, out, _ = run(["alarm", "serve", "--status", "--json"], capsys)
    p = json.loads(out)
    assert code == 1 and p["alive"] is False and p["stale"] is True
    code, out, _ = run(["alarm", "serve", "--status"], capsys)
    assert "STALE" in out and not play.INTERVENTION_LOG.exists()


def test_another_listener_on_the_port_is_a_collision_not_a_live_server(
        launchd_agent, monkeypatch, capsys):
    from twiddle import daemon
    monkeypatch.setattr(daemon, "status", lambda label=daemon.LABEL: "not loaded")
    code, out, _ = run(["alarm", "serve", "--status", "--json"], capsys)    # serving() says True
    p = json.loads(out)
    assert code == 1 and p["alive"] is False and p["port_answers"] is True
    code, out, _ = run(["alarm", "serve", "--status"], capsys)
    assert "something else answers" in out


def test_status_probes_the_address_the_server_binds(launchd_agent, monkeypatch, capsys):
    from twiddle import daemon
    seen = []
    monkeypatch.setattr(daemon, "serving", lambda port, host="127.0.0.1", **k: seen.append(host) or True)
    run(["alarm", "serve", "--status", "--host", "192.168.1.5", "--json"], capsys)
    run(["alarm", "serve", "--status", "--json"], capsys)       # the wildcard default
    assert seen[0] == "192.168.1.5" and seen[-1] == "127.0.0.1"


def test_bare_status_asks_about_the_installed_endpoint(launchd_agent, monkeypatch, capsys, tmp_path):
    from twiddle import daemon
    monkeypatch.setattr(daemon, "_which", lambda cmd: f"/bin/{cmd}")
    plist = daemon.build_alarm_plist(tmp_path, tmp_path, 9123, 600.0, [], "", "192.168.1.5")
    daemon.plist_path(daemon.ALARM_LABEL).parent.mkdir(parents=True, exist_ok=True)
    daemon.plist_path(daemon.ALARM_LABEL).write_bytes(__import__("plistlib").dumps(plist))
    seen = []
    monkeypatch.setattr(daemon, "serving", lambda port, host="127.0.0.1", **k: seen.append((port, host)) or True)
    code, out, _ = run(["alarm", "serve", "--status", "--json"], capsys)
    assert code == 0 and set(seen) == {(9123, "192.168.1.5")} and json.loads(out)["port"] == 9123


def test_status_probes_an_explicit_default_endpoint_as_given(launchd_agent, monkeypatch, capsys, tmp_path):
    from twiddle import daemon
    monkeypatch.setattr(daemon, "installed_alarm_endpoint", lambda: (9123, "192.168.1.5"))
    seen = []
    monkeypatch.setattr(daemon, "serving", lambda port, host="127.0.0.1", **k: seen.append((port, host)) or True)
    run(["alarm", "serve", "--status", "--port", "8765", "--host", "0.0.0.0", "--json"], capsys)
    assert set(seen) == {(8765, "127.0.0.1")}


@pytest.mark.parametrize("port", ["0", "-1", "70000"])
def test_install_refuses_an_unusable_port_before_touching_the_job(fake_house, launchd_agent, sounds,
                                                                  capsys, port):
    calls = launchd_agent
    code, _, err = run(["alarm", "serve", "--install", "--dir", str(sounds), "--port", port], capsys)
    assert code == 1 and "port" in err
    assert calls == [] and not play.INTERVENTION_LOG.exists()


def test_a_failed_bootstrap_is_still_journalled(fake_house, sounds, capsys, monkeypatch, tmp_path):
    from twiddle import daemon
    monkeypatch.setattr(daemon, "AGENT_DIR", tmp_path / "agents")
    monkeypatch.setattr(daemon, "_which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(daemon, "pick_anchor", lambda: "10.0.0.13")

    def launchctl(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 5 if cmd[1] == "bootstrap" else 0, stdout="",
                                           stderr="Bootstrap failed")
    monkeypatch.setattr(daemon.subprocess, "run", launchctl)
    code, _, err = run(["alarm", "serve", "--install", "--dir", str(sounds)], capsys)
    assert code == 1 and "bootstrap failed" in err
    actions = [json.loads(l)["action"] for l in play.INTERVENTION_LOG.read_text().splitlines()]
    assert actions == ["alarm_serve_install", "alarm_serve_install_failed"]


def test_install_keeps_the_requested_host_in_the_agent(fake_house, launchd_agent, sounds, capsys):
    calls = launchd_agent
    run(["alarm", "serve", "--install", "--dir", str(sounds), "--host", "127.0.0.1"], capsys)
    assert calls[0][1][-1] == "127.0.0.1"


def test_the_agents_own_command_line_is_one_the_cli_accepts(fake_house, sounds, capsys, tmp_path,
                                                           monkeypatch):
    from twiddle import daemon
    monkeypatch.setattr(daemon, "_which", lambda cmd: f"/bin/{cmd}")
    argv = daemon.build_alarm_plist(tmp_path, sounds, 0, 600.0, ["Living Room"],
                                    "10.0.0.13", "127.0.0.1")["ProgramArguments"]
    argv = argv[argv.index("twiddle") + 1:] + ["--dry-run", "--json"]
    code, out, _ = run(argv, capsys)
    assert code == 0 and json.loads(out)["would"] == "serve"


def test_install_pins_the_given_anchor_else_a_mains_powered_one(fake_house, launchd_agent, sounds, capsys):
    calls = launchd_agent
    run(["alarm", "serve", "--install", "--dir", str(sounds)], capsys)
    run(["alarm", "serve", "--install", "--dir", str(sounds), "--anchor", "10.0.0.99"], capsys)
    assert [c[1][5] for c in calls] == ["10.0.0.13", "10.0.0.99"]


# ---- which port alarms point at ----------------------------------------------

def test_a_foreground_server_off_the_alarms_port_says_so(fake_house, sounds, capsys, monkeypatch, tmp_path):
    from tests.test_alarm_sources import install_fake_agent
    argv = ["alarm", "serve", "--dir", str(sounds), "--port", "9123", "--dry-run"]
    code, out, _ = run(argv + ["--json"], capsys)       # no agent: alarms point at 8765
    assert code == 0 and "8765" in json.loads(out)["warning"]
    code, out, _ = run(argv, capsys)
    assert "warning: new alarms point at port 8765, not 9123" in out
    install_fake_agent(monkeypatch, 9123)
    code, out, _ = run(argv + ["--json"], capsys)
    assert code == 0 and "warning" not in json.loads(out)
    code, out, _ = run(argv, capsys)
    assert "warning" not in out


def test_installing_the_agent_on_a_port_points_new_alarms_at_it(sounds, capsys, monkeypatch):
    from tests.test_alarm_sources import add_dry_run
    from tests.test_alarm_clock import FakeClock
    from twiddle import daemon, devices
    monkeypatch.setattr(devices.requests, "post", FakeClock().post)
    monkeypatch.setattr(alarm_cli, "_household", lambda args: household())
    monkeypatch.setattr(daemon, "_which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(daemon, "pick_anchor", lambda: "10.0.0.13")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(
        OSError("no network in tests")))
    ran = []
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda cmd, **_kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    code, _, err = run(["alarm", "serve", "--install", "--dir", str(sounds), "--port", "9123"], capsys)
    assert code == 0, err
    assert ran and all(cmd[0] == "launchctl" for cmd in ran)
    assert daemon.installed_alarm_endpoint()[0] == 9123
    assert add_dry_run(capsys, monkeypatch, "sound:bell") == 9123
