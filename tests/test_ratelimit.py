"""Staying under a rate limit before being told off: windows, lockouts, the shared ledger."""
import pytest

from twiddle import cli, ratelimit, spotify
from twiddle.ratelimit import Governor, Ledger, Limit, Policy, RateLimited


class Time:
    """A clock that only moves when something sleeps or the test says so."""

    def __init__(self, t=1_000_000.0):
        self.t = t
        self.slept = []

    def wall(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def gov(limits, clock, ledger=None, max_block_s=5.0):
    return Governor(Policy("svc", tuple(limits), "test", max_block_s), ledger,
                    wall=clock.wall, sleep=clock.sleep)


def test_a_burst_is_paced_to_the_short_window():
    c = Time()
    g = gov([Limit(2, 1)], c)
    assert g.acquire() == 0 and g.acquire() == 0         # two fit in a second
    assert g.acquire() == pytest.approx(1.0)             # the third waits for the first to age out
    assert c.slept == [pytest.approx(1.0)]


def test_every_window_must_have_room_not_just_the_shortest():
    c = Time()
    g = gov([Limit(10, 1), Limit(3, 60)], c, max_block_s=100)
    for _ in range(3):
        g.acquire()
    assert g.acquire() == pytest.approx(60.0)            # the minute is the binding one


def test_it_raises_instead_of_sleeping_longer_than_max_block():
    c = Time()
    g = gov([Limit(1, 3600)], c, max_block_s=5)
    g.acquire()
    with pytest.raises(RateLimited) as e:
        g.acquire()
    assert e.value.retry_after == pytest.approx(3600) and c.slept == []   # never parked a thread for an hour


def test_a_429_locks_the_service_out_without_sending_anything_for_the_time_it_named():
    c = Time()
    g = gov([Limit(100, 60)], c)
    g.acquire()
    g.report(429, retry_after=600)
    with pytest.raises(RateLimited) as e:
        g.acquire()
    assert e.value.retry_after == pytest.approx(600) and "locked out" in str(e.value)
    assert g.blocked_for() == pytest.approx(600)
    c.t += 601
    assert g.blocked_for() == 0 and g.acquire() == 0     # over: requests flow again


def test_a_429_with_no_time_gets_a_default_penalty_and_other_statuses_none():
    c = Time()
    g = gov([Limit(100, 60)], c)
    g.report(500)
    g.report(200)
    assert g.blocked_for() == 0
    g.report(429)
    assert g.blocked_for() == pytest.approx(ratelimit.DEFAULT_PENALTY_S)


def test_the_lockout_and_the_requests_are_shared_through_the_ledger(tmp_path):
    c = Time()
    path = tmp_path / "ledger.json"
    a = gov([Limit(3, 60)], c, Ledger(path, c.wall, pid=1))
    b = gov([Limit(3, 60)], c, Ledger(path, c.wall, pid=2))
    a.acquire()
    a.acquire()
    a.flush(force=True)
    c.t += ratelimit.REFRESH_S + 0.1
    b.acquire()                                          # b sees a's two: that is the third
    with pytest.raises(RateLimited):
        b.acquire()                                      # a fourth, over the budget the two share
    a.report(429, retry_after=1000)                      # a is told to stop...
    c.t += ratelimit.REFRESH_S + 0.1
    with pytest.raises(RateLimited) as e:
        b.acquire()                                      # ...so b never sends
    assert e.value.retry_after == pytest.approx(1000 - ratelimit.REFRESH_S - 0.1, abs=1)
    # and a brand-new process knows too
    d = gov([Limit(3, 60)], c, Ledger(path, c.wall, pid=3))
    assert d.blocked_for() > 900


def test_a_lost_or_garbled_ledger_means_starting_from_empty(tmp_path):
    c = Time()
    path = tmp_path / "ledger.json"
    path.write_text("{not json")
    g = gov([Limit(3, 60)], c, Ledger(path, c.wall, pid=1))
    assert g.acquire() == 0 and g.blocked_for() == 0
    g.flush(force=True)
    assert "events" in path.read_text()                  # and it heals the file


def test_policies_say_whose_number_each_is_and_the_config_overrides_them(tmp_path):
    p = ratelimit.POLICIES
    assert "published by MusicBrainz" in p["musicbrainz"].source
    assert "publishes no number" in p["spotify"].source
    cfg = tmp_path / "r.toml"
    cfg.write_text("[spotify]\nlimits = [[5, 1], [50, 60]]\nmax_block_s = 2\n[newsvc]\nlimits = [[1, 10]]\n")
    pol = ratelimit.load_policies(cfg)
    assert pol["spotify"].limits == (Limit(5, 1), Limit(50, 60)) and pol["spotify"].max_block_s == 2
    assert pol["newsvc"].limits == (Limit(1, 10),) and pol["musicbrainz"] == p["musicbrainz"]
    cfg.write_text("[spotify]\nlimits = 'nonsense'\n")
    assert ratelimit.load_policies(cfg)["spotify"] == p["spotify"]      # a bad entry is ignored
    assert ratelimit.load_policies(tmp_path / "missing.toml") == p


def test_a_session_request_goes_through_the_governor_and_a_lockout_sends_nothing(monkeypatch):
    sent = []

    class Resp:
        status_code = 200
        headers = {}
        content = b"{}"
        reason = "OK"
        text = "{}"

        def json(self):
            return {}

    monkeypatch.setattr(spotify.requests, "request", lambda *a, **k: sent.append(a) or Resp())
    sess = spotify.Session(spotify.Tokens("t", "r", 9e12), path=None)
    sess.request("GET", "/me")
    assert len(sent) == 1 and ratelimit.governor("spotify").usage()[0]["used"] == 1
    ratelimit.governor("spotify").report(429, retry_after=3600)
    with pytest.raises(spotify.ApiError) as e:
        sess.request("GET", "/me")
    assert e.value.status == 429 and e.value.retry_after > 3000 and len(sent) == 1   # not sent


def test_limits_command_shows_budgets_and_a_lockout(capsys):
    ratelimit.governor("spotify").acquire()
    ratelimit.governor("spotify").report(429, retry_after=7200)
    assert cli.main(["limits"]) == 0
    out = capsys.readouterr().out
    assert "spotify" in out and "1/10 per 10s" in out and "LOCKED OUT" in out
    assert "published by MusicBrainz" in out


def test_two_processes_cannot_both_take_the_last_slot_of_a_shared_window(tmp_path):
    # Codex: a governor checked its cached copy of the ledger and published its
    # reservation seconds later, so two processes could both admit the same slot.
    c = Time()
    path = tmp_path / "ledger.json"
    a = gov([Limit(3, 10)], c, Ledger(path, wall=c.wall, pid=1), max_block_s=0)
    b = gov([Limit(3, 10)], c, Ledger(path, wall=c.wall, pid=2), max_block_s=0)
    a.acquire(); a.acquire()            # two of three, and `a` has not flushed or been refreshed
    b.acquire()                         # b takes the third: it saw a's two at once
    with pytest.raises(RateLimited):
        a.acquire()                     # a's own cached view is stale; the ledger is not
    with pytest.raises(RateLimited):
        b.acquire()
    c.t += 11
    a.acquire()                         # the window moved on


def test_a_reservation_prunes_what_has_aged_out_of_the_shared_ledger(tmp_path):
    # Codex: only `merge` pruned, and a shared reservation never calls it, so every scheduled
    # build left its timestamps behind for every later request to read, sort and rewrite
    import json
    c = Time()
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"svc": {"events": {"7": [c.t - 2 * Ledger.HORIZON_S, c.t - 1.5 * Ledger.HORIZON_S],
                                                    "8": [c.t - 5]}}}))
    g = gov([Limit(5, 10)], c, Ledger(path, wall=c.wall, pid=1))
    g.acquire()
    events = json.loads(path.read_text())["svc"]["events"]
    assert "7" not in events and events["8"] == [c.t - 5] and len(events["1"]) == 1


def test_a_failed_ledger_write_costs_one_slot_not_two(tmp_path, monkeypatch):
    # Codex: reserve() appended to the local list, then the write failed and the local
    # fallback appended the same request again
    c = Time()
    ledger = Ledger(tmp_path / "ledger.json", wall=c.wall, pid=1)
    monkeypatch.setattr(ledger, "_write", lambda doc: (_ for _ in ()).throw(OSError("disk full")))
    g = gov([Limit(2, 60)], c, ledger, max_block_s=0)
    g.acquire()
    assert len(g._mine) == 1
    g.acquire()                                     # the second of two: must not be refused
    with pytest.raises(RateLimited):
        g.acquire()
