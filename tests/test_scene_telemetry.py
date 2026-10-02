"""Request counters, caps, progress, ETA and the status file `scene status` reads."""
import json

from twiddle import cli, lookup, netstats
from twiddle.scenedata.pacing import Progress
from twiddle.scenespec import buildstatus, dataset


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


def _clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(netstats, "clock", c)
    return c


def test_requests_are_counted_with_a_one_minute_rate_against_the_cap(monkeypatch):
    c = _clock(monkeypatch)
    for _ in range(30):
        netstats.record("musicbrainz", status=200)
        c.tick(1)
    snap = netstats.snapshot()["musicbrainz"]
    assert snap["total"] == 30 and snap["rpm"] == 30 and snap["cap_rpm"] == 60
    assert snap["utilisation"] == 0.5 and "published by MusicBrainz" in snap["cap_source"]
    c.tick(45)                                  # the oldest fall out of the window
    assert netstats.snapshot()["musicbrainz"]["rpm"] == 15 and netstats.snapshot()["musicbrainz"]["total"] == 30


def test_a_429_is_counted_as_slow_down_with_what_it_asked_and_other_failures_as_errors(monkeypatch):
    c = _clock(monkeypatch)
    netstats.record("spotify", status=200)
    netstats.record("spotify", status=429, retry_after=45)
    c.tick(10)
    netstats.record("spotify", status=500)
    netstats.record("spotify")                  # failed before any answer
    netstats.record_wait("spotify", 12.5)
    s = netstats.snapshot()["spotify"]
    assert (s["total"], s["limited"], s["errors"]) == (4, 1, 2)
    assert s["retry_after_s"] == 45 and s["limited_ago_s"] == 10 and s["waited_s"] == 12.5
    assert "publishes no number" in s["cap_source"]          # a budget, not Spotify's limit


def test_reporting_never_raises_and_unknown_services_have_no_cap():
    netstats.record_response("x", object())                  # not a response at all
    netstats.record("whatever", status=200)
    assert netstats.snapshot()["whatever"]["cap_rpm"] is None
    assert netstats.service_for("https://musicbrainz.org/ws/2/artist") == "musicbrainz"
    assert netstats.service_for("https://en.wikipedia.org/api/x") == "wikipedia"
    assert netstats.service_for("https://api.discogs.com/artists/1") == "discogs"


def test_the_http_helpers_report_their_requests(monkeypatch):
    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return b"{}"

    monkeypatch.setattr(lookup.urllib.request, "urlopen", lambda req, timeout=None: Resp())
    lookup._get_json("https://musicbrainz.org/ws/2/x")
    lookup._get_json("https://en.wikipedia.org/api/rest_v1/page/summary/x")
    snap = netstats.snapshot()
    assert snap["musicbrainz"]["total"] == 1 and snap["wikipedia"]["total"] == 1


def test_the_eta_is_measured_seconds_a_band_times_the_bands_left_waits_included():
    c = Clock()
    p = Progress(log=lambda m: None, clock=c, wall=lambda: 5e9, write=False)
    p.begin(100)
    for _ in range(2):
        c.tick(3)
        p.band_done()
    assert p.rate_and_eta() == (None, None)                  # too few to say
    c.tick(3)
    p.band_done()
    rate, eta = p.rate_and_eta()
    assert rate == 20.0 and eta == 97 * 3                    # 3s a band, 97 to go
    p.waiting("spotify", 600)
    assert p.rate_and_eta()[1] == 97 * 3 + 600               # a wait in progress is added
    snap = p.snapshot()
    assert snap["waiting"] == {"service": "spotify", "remaining_s": 600}
    c.tick(600)
    assert p.snapshot()["waiting"] is None


def test_the_progress_line_shows_rate_eta_and_caps(monkeypatch):
    _clock(monkeypatch)
    c = Clock()
    p = Progress(log=lambda m: None, clock=c, wall=lambda: 5e9, write=False)
    p.begin(50)
    netstats.record("musicbrainz", status=200)
    netstats.record("spotify", status=429, retry_after=30)
    for _ in range(5):
        c.tick(2)
        p.band_done()
    line = p.line()
    assert "5/50 bands" in line and "30.0/min" in line and "eta 1m" in line      # 45 left at 2s = 90s
    assert "musicbrainz 1/60rpm" in line and "spotify slowed us 1x" in line


def test_the_status_file_is_written_atomically_and_scene_status_reads_it(tmp_path, capsys):
    c = Clock()
    path = dataset.default_path()
    p = Progress(path, log=lambda m: None, clock=c, wall=lambda: 5e9, min_write_s=2)
    p.begin(10)
    for _ in range(4):
        c.tick(5)
        p.band_done()
    p.flush(force=True)
    st = json.loads(buildstatus.path(path).read_text())
    assert st["state"] == "running" and (st["done"], st["total"]) == (4, 10) and st["eta_s"] == 30
    assert cli.main(["scene", "status"]) == 0
    out = capsys.readouterr().out
    assert "build: " in out and "phase: enriching" in out and "4/10 bands" in out and "eta 30s" in out
    p.finish("done", "10 shows")
    assert json.loads(buildstatus.path(path).read_text())["state"] == "done"


def test_scene_status_says_so_when_no_build_has_reported(capsys):
    assert cli.main(["scene", "status"]) == 1
    assert "no build has reported" in capsys.readouterr().err


def test_a_running_build_that_stopped_reporting_is_called_stalled():
    st = {"state": "running", "pid": 2 ** 30, "updated_at": 1000.0, "phase": "enriching"}
    assert "stalled" in buildstatus.render(st, now=1000.0 + buildstatus.STALE_S + 1)
    assert "stalled" in buildstatus.render(st, now=1001.0)      # the process is gone either way
