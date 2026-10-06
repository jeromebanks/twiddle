"""`twiddle alarm serve`: audio from this Mac, every serve a bounded span.

The server is real, on 127.0.0.1, with a temp directory of files; the journal
is conftest's temp file. No speaker, network or Spotify is touched.
"""
import http.client
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from twiddle import alarm_cli, cli, play, report
from twiddle.alarms import server
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
    import threading
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


def test_a_symlink_loop_is_a_404_not_a_dropped_request(srv, sounds):
    (sounds / "loop.mp3").symlink_to(sounds / "loop.mp3")
    assert server.resolve(sounds, "/loop.mp3") is None
    assert get(srv, "/loop.mp3")[0] == 404


def test_a_known_audio_file_is_served_as_audio(srv):
    for path, body in (("/bell.mp3", b"ID3" + b"x" * 1000), ("/sub/rise.wav", b"RIFF" + b"y" * 100)):
        assert get(srv, path) == (200, body)


def test_this_mac_can_fetch_so_a_curl_can_try_it(sounds):
    s = server.AlarmServer(sounds, {"10.9.9.9"}, port=0, host="127.0.0.1")
    import threading
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
    import threading
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
    import threading
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
    import threading
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
    import threading
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
    import threading
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
        cli.main(["alarm", "serve", "--dir", str(sounds), "--max-s", bad])
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
    code, out, _ = run(["alarm", "serve", "--dir", str(sounds), "--port", "0",
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
    code, _, err = run(["alarm", "serve", "--dir", str(sounds), "--room", "Nowhere"], capsys)
    assert code == 1 and "Nowhere" in err
    code, _, err = run(["alarm", "serve", "--dir", str(sounds / "missing")], capsys)
    assert code == 1 and "not a directory" in err
    assert journal() == []


def test_a_second_serve_on_a_busy_port_fails_and_closes_nobodys_spans(fake_house, sounds,
                                                                      capsys):
    write({"ts": iso(T0), "action": "alarm_serve_start", "ip": SPEAKER, "span": True,
           "span_id": "live", "max_s": 600})
    first = server.AlarmServer(sounds, None, port=0)
    try:
        code, _, err = run(["alarm", "serve", "--dir", str(sounds),
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
    code, out, _ = run(["alarm", "serve", "--dir", str(sounds), "--port", "0"], capsys)
    assert code == 0 and "1 stale span(s) to close" in out
    assert report.open_spans(play.INTERVENTION_LOG) == []
    assert journal()[-1]["stale"] is True
