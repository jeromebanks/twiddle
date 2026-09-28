"""`comedy.py`'s picking logic, and `comedy_cli`'s dry-run contract.

Nothing here reaches the real Spotify API, MusicBrainz, or a speaker.
`conftest.py` already redirects `comedy.CACHE_FILE` and `comedy.PLAYED_LOG`
into a tmp dir; a `FakeSession` here stands in for `spotify.Session` the same
way `test_discover_cli.py`'s does, except it answers `.request()` (what
`comedy.py` actually calls) rather than `.search()`.
"""
from datetime import datetime, timedelta, timezone

import pytest

from twiddle import cli, comedy, comedy_cli, lookup, play, report


class FakeSession:
    """Records every call instead of hitting the Spotify Web API."""

    def __init__(self):
        self.top_artists = {"long_term": [], "medium_term": []}
        self.recently_played = []
        self.search_results = {"albums": {"items": []}}
        self.albums_by_artist = {}   # artist id -> list of album dicts
        self.tracks_by_album = {}    # album id -> list of track dicts
        self.calls = []
        self.played_calls = []

    def request(self, method, path, params=None, **kw):
        params = params or {}
        self.calls.append((method, path, dict(params)))
        if path == "/me/top/artists":
            return {"items": self.top_artists.get(params.get("time_range"), [])}
        if path == "/me/player/recently-played":
            return {"items": self.recently_played}
        if path == "/search":
            return self.search_results
        if path.startswith("/artists/") and path.endswith("/albums"):
            aid = path.split("/")[2]
            items = self.albums_by_artist.get(aid, [])
            offset, limit = params.get("offset", 0), params.get("limit", 10)
            assert limit == 10, "this app 400s above limit=10 for this endpoint"
            page = items[offset:offset + limit]
            return {"items": page,
                    "next": "more" if offset + limit < len(items) else None}
        if path.startswith("/albums/") and path.endswith("/tracks"):
            aid = path.split("/")[2]
            return {"items": self.tracks_by_album.get(aid, [])}
        raise AssertionError(f"unexpected request: {method} {path} {params}")

    def play(self, uri="", device_id="", position_ms=0, *, uris=None,
              offset=0, offset_uri=""):
        self.played_calls.append({"uris": uris, "device_id": device_id})


def _artist(name, aid):
    return comedy.ComedyArtist(id=aid, name=name, added_at="2026-01-01T00:00:00+00:00")


# ---- classify ----------------------------------------------------------------


def test_classify_true_from_the_description(monkeypatch):
    info = lookup.ArtistInfo(name="Marc Maron", description="American comedian")
    monkeypatch.setattr(comedy.lookup, "identify",
                        lambda *a, **kw: lookup.Result(artist=info))
    verdict, why = comedy.classify("Marc Maron")
    assert verdict is True
    assert "comedian" in why.lower()


def test_classify_true_from_a_comedy_genre_tag(monkeypatch):
    info = lookup.ArtistInfo(name="X", genres=["comedy"])
    monkeypatch.setattr(comedy.lookup, "identify",
                        lambda *a, **kw: lookup.Result(artist=info))
    assert comedy.classify("X")[0] is True


def test_classify_false_for_a_music_artist(monkeypatch):
    info = lookup.ArtistInfo(name="Orbital", genres=["electronic"],
                             description="English electronic music duo")
    monkeypatch.setattr(comedy.lookup, "identify",
                        lambda *a, **kw: lookup.Result(artist=info))
    assert comedy.classify("Orbital")[0] is False


def test_classify_none_when_nothing_is_found(monkeypatch):
    monkeypatch.setattr(comedy.lookup, "identify", lambda *a, **kw: lookup.Result())
    assert comedy.classify("Dry Bar Comedy")[0] is None


def test_classify_none_when_lookup_is_unreachable(monkeypatch):
    def boom(*a, **kw):
        raise lookup.LookupFailed("down")
    monkeypatch.setattr(comedy.lookup, "identify", boom)
    assert comedy.classify("Whoever")[0] is None


# ---- the classified list -----------------------------------------------------


def test_artist_cache_round_trips():
    comedy.save_artists([comedy.ComedyArtist(id="1", name="A", added_at="t")])
    assert [a.name for a in comedy.load_artists()] == ["A"]


def test_confirm_is_idempotent():
    comedy.confirm("1", "A")
    comedy.confirm("1", "A")
    assert len(comedy.load_artists()) == 1


# ---- refresh ------------------------------------------------------------------


def test_refresh_adds_confident_comedians_and_defers_ambiguous(monkeypatch):
    sess = FakeSession()
    sess.top_artists["long_term"] = [{"id": "c1", "name": "Comedian"},
                                      {"id": "m1", "name": "Musician"}]
    verdicts = {"Comedian": (True, "comedian"), "Musician": (False, "no signal")}
    monkeypatch.setattr(comedy, "classify", lambda n: verdicts[n])

    result = comedy.refresh(sess)

    assert [a.name for a in result.added] == ["Comedian"]
    assert result.ambiguous == []
    assert {a.name for a in comedy.load_artists()} == {"Comedian"}


def test_refresh_skips_already_known_artists_without_reclassifying(monkeypatch):
    comedy.save_artists([comedy.ComedyArtist(id="c1", name="Comedian", added_at="t")])
    sess = FakeSession()
    sess.top_artists["long_term"] = [{"id": "c1", "name": "Comedian"}]
    monkeypatch.setattr(comedy, "classify",
                        lambda n: (_ for _ in ()).throw(AssertionError("should not run")))

    result = comedy.refresh(sess)

    assert result.unchanged == 1
    assert result.added == []


def test_refresh_never_auto_adds_an_ambiguous_name(monkeypatch):
    sess = FakeSession()
    sess.top_artists["long_term"] = [{"id": "x1", "name": "Unknown Act"}]
    monkeypatch.setattr(comedy, "classify", lambda n: (None, "not found"))

    result = comedy.refresh(sess)

    assert result.added == []
    assert result.ambiguous == [{"id": "x1", "name": "Unknown Act", "why": "not found"}]
    assert comedy.load_artists() == []


# ---- new releases -------------------------------------------------------------


def test_artist_albums_pages_past_the_ten_item_limit():
    sess = FakeSession()
    sess.albums_by_artist["a1"] = [
        {"id": f"al{i}", "name": f"Album {i}", "release_date": f"2020-01-{i:02d}"}
        for i in range(1, 13)]

    albums = comedy.artist_albums(sess, "a1")

    assert len(albums) == 12
    assert albums[0]["release_date"] == "2020-01-12", "newest first"


def test_artist_albums_uses_a_warm_cache_without_a_new_fetch():
    """A classified list of 40+ comedians times a live fetch every call is
    the burst that tripped a ~24h quota lockout on 2026-09-24."""
    sess = FakeSession()
    sess.albums_by_artist["a1"] = [{"id": "al1", "name": "A", "release_date": "2020-01-01"}]

    first = comedy.artist_albums(sess, "a1")
    calls_before = len(sess.calls)
    second = comedy.artist_albums(sess, "a1")

    assert second == first
    assert len(sess.calls) == calls_before, "a warm cache hit must not call the API"


def test_artist_albums_cache_survives_a_full_day_for_the_nightly_case():
    """`snooze` runs roughly once every 24h; a cache TTL shorter than that
    (the first draft used 20h) would be stale on every single nightly run,
    which is the case that actually matters -- unlike a same-evening `new`
    followed by `sleep`."""
    sess = FakeSession()
    sess.albums_by_artist["a1"] = [{"id": "al1", "name": "A", "release_date": "2020-01-01"}]
    comedy.artist_albums(sess, "a1")
    cache = comedy._load_albums_cache()
    cache["a1"]["at"] -= 25 * 3600  # 25h old, but still inside a multi-day TTL
    comedy._save_albums_cache(cache)

    calls_before = len(sess.calls)
    comedy.artist_albums(sess, "a1")

    assert len(sess.calls) == calls_before, "25h old must still be a warm hit"


def test_artist_albums_refetches_once_the_cache_goes_stale():
    sess = FakeSession()
    sess.albums_by_artist["a1"] = [{"id": "al1", "name": "A", "release_date": "2020-01-01"}]
    comedy.artist_albums(sess, "a1")
    cache = comedy._load_albums_cache()
    cache["a1"]["at"] -= comedy.ALBUMS_CACHE_TTL_S + 1
    comedy._save_albums_cache(cache)

    calls_before = len(sess.calls)
    comedy.artist_albums(sess, "a1")

    assert len(sess.calls) > calls_before


def test_new_releases_trusts_only_day_precision_dates(monkeypatch):
    sess = FakeSession()
    monkeypatch.setattr(comedy, "artist_albums", lambda s, aid: [
        {"id": "al1", "name": "Day-precise", "release_date": "2026-09-20",
         "release_date_precision": "day"},
        {"id": "al2", "name": "Year-only", "release_date": "2026",
         "release_date_precision": "year"}])

    out = comedy.new_releases(sess, [_artist("Someone", "a1")], days=45)

    assert [a["name"] for a in out] == ["Day-precise"]


def test_new_releases_excludes_anything_outside_the_window(monkeypatch):
    sess = FakeSession()
    monkeypatch.setattr(comedy, "artist_albums", lambda s, aid: [
        {"id": "old", "name": "Old", "release_date": "2020-01-01",
         "release_date_precision": "day"}])

    assert comedy.new_releases(sess, [_artist("Someone", "a1")], days=45) == []


def test_album_tracks_sums_durations_in_order():
    sess = FakeSession()
    sess.tracks_by_album["al1"] = [{"uri": "spotify:track:1", "duration_ms": 1000},
                                    {"uri": "spotify:track:2", "duration_ms": 2000}]

    uris, total = comedy.album_tracks(sess, "al1")

    assert uris == ["spotify:track:1", "spotify:track:2"]
    assert total == 3000


# ---- the played/rated log -----------------------------------------------------


def test_record_played_and_rate_round_trip():
    comedy.record_played("al1", "Special", "Comedian")
    rec = comedy.rate("up")
    assert rec == {"ts": rec["ts"], "kind": "rated", "album_id": "al1",
                   "name": "Special", "artist": "Comedian", "direction": "up"}


def test_rate_with_nothing_played_raises():
    with pytest.raises(ValueError):
        comedy.rate("up")


def test_rate_an_unplayed_album_id_raises():
    comedy.record_played("al1", "Special", "Comedian")
    with pytest.raises(ValueError):
        comedy.rate("up", album_id="nope")


# ---- pick_queue ---------------------------------------------------------------


def test_pick_queue_refuses_an_empty_artist_list():
    with pytest.raises(ValueError):
        comedy.pick_queue(FakeSession(), [])


def test_pick_queue_prefers_an_unplayed_new_release(monkeypatch):
    sess = FakeSession()
    monkeypatch.setattr(comedy, "new_releases", lambda s, artists, days=45: [
        {"id": "new1", "name": "New Special", "_artist": "Fresh Comedian",
         "uri": "spotify:album:new1", "release_date": "2026-09-20"}])
    monkeypatch.setattr(comedy, "artist_albums", lambda s, aid: [])
    monkeypatch.setattr(comedy, "album_tracks",
                        lambda s, aid: (["t1"], 1000) if aid == "new1" else ([], 0))

    picks = comedy.pick_queue(sess, [_artist("Fresh Comedian", "a1")], n=1)

    assert [p.album_id for p in picks] == ["new1"]


def test_pick_queue_never_repeats_an_artist_within_the_avoid_window(monkeypatch):
    """The rotation excludes the whole artist, not just the specific album
    they were heard on -- see the two `..._eligible_candidates` tests above
    for why that has to be exclusion, not mere deprioritization."""
    sess = FakeSession()
    comedy.record_played("old1", "Old Special", "Recent")  # played just now
    monkeypatch.setattr(comedy, "new_releases", lambda s, artists, days=45: [])
    albums = {"a1": [{"id": "old1", "name": "Old Special", "uri": "x"}],
              "a2": [{"id": "fresh1", "name": "Something else", "uri": "y"}]}
    monkeypatch.setattr(comedy, "artist_albums", lambda s, aid: albums[aid])
    monkeypatch.setattr(comedy, "album_tracks", lambda s, aid: ([f"t-{aid}"], 1000))

    picks = comedy.pick_queue(
        sess, [_artist("Recent", "a1"), _artist("Someone Else", "a2")], n=1)

    assert [p.album_id for p in picks] == ["fresh1"]


def test_pick_queue_prefers_a_liked_artist_among_eligible_candidates(monkeypatch):
    """Liked only breaks ties among artists not recently played -- see the
    next test for why a liked-but-recent artist must not win anyway."""
    sess = FakeSession()
    old_ts = (datetime.now(timezone.utc)
              - timedelta(days=comedy.AVOID_REPEAT_DAYS + 5)).isoformat()
    comedy._append_played({"ts": old_ts, "kind": "played", "album_id": "liked-old",
                            "name": "Liked Old", "artist": "Liked"})
    comedy.rate("up", album_id="liked-old")
    monkeypatch.setattr(comedy, "new_releases", lambda s, artists, days=45: [])
    albums = {"a1": [{"id": "unrated-new", "name": "Unrated New", "uri": "y"}],
              "a2": [{"id": "liked-new", "name": "Liked New", "uri": "x"}]}
    monkeypatch.setattr(comedy, "artist_albums", lambda s, aid: albums[aid])
    monkeypatch.setattr(comedy, "album_tracks", lambda s, aid: ([f"t-{aid}"], 1000))

    picks = comedy.pick_queue(
        sess, [_artist("Unrated", "a1"), _artist("Liked", "a2")], n=1)

    assert picks[0].artist == "Liked"


def test_pick_queue_excludes_a_liked_artist_played_within_the_avoid_window(monkeypatch):
    """A liked artist heard last night must not still outrank someone never
    played -- the bug this test used to encode (fixed 2026-09-24)."""
    sess = FakeSession()
    comedy.record_played("liked-recent", "Liked Recent", "Liked")  # just now
    comedy.rate("up", album_id="liked-recent")
    monkeypatch.setattr(comedy, "new_releases", lambda s, artists, days=45: [])
    albums = {"a1": [{"id": "unrated-new", "name": "Unrated New", "uri": "y"}],
              "a2": [{"id": "liked-new", "name": "Liked New", "uri": "x"}]}
    monkeypatch.setattr(comedy, "artist_albums", lambda s, aid: albums[aid])
    monkeypatch.setattr(comedy, "album_tracks", lambda s, aid: ([f"t-{aid}"], 1000))

    picks = comedy.pick_queue(
        sess, [_artist("Unrated", "a1"), _artist("Liked", "a2")], n=1)

    assert picks[0].artist == "Unrated"


def test_pick_queue_drops_a_pick_rather_than_exceed_the_track_cap(monkeypatch):
    sess = FakeSession()
    releases = [
        {"id": "r1", "name": "R1", "_artist": "Wordy", "uri": "x",
         "release_date": "2026-09-20"},
        {"id": "r2", "name": "R2", "_artist": "AlsoWordy", "uri": "y",
         "release_date": "2026-09-19"},
    ]
    monkeypatch.setattr(comedy, "new_releases", lambda s, artists, days=45: releases)
    monkeypatch.setattr(comedy, "artist_albums", lambda s, aid: [])
    big = [f"t{i}" for i in range(comedy.MAX_QUEUE_TRACKS)]
    monkeypatch.setattr(comedy, "album_tracks",
                        lambda s, aid: (big, 1000) if aid == "r1" else (["only-one"], 1000))

    picks = comedy.pick_queue(
        sess, [_artist("Wordy", "a1"), _artist("AlsoWordy", "a2")], n=2)

    assert [p.album_id for p in picks] == ["r1"]


# ---- flatten / start_queue -----------------------------------------------------


def test_flatten_orders_tracks_and_sums_durations():
    picks = [comedy.QueuePick("1", "A", "X", "u1", ["t1", "t2"], 1000),
             comedy.QueuePick("2", "B", "Y", "u2", ["t3"], 500)]
    uris, total = comedy.flatten(picks)
    assert uris == ["t1", "t2", "t3"]
    assert total == 1500


def test_start_queue_plays_arms_the_timer_and_journals_only_the_tail(monkeypatch):
    sess = FakeSession()
    picks = [comedy.QueuePick("1", "Special", "Comedian", "spotify:album:1",
                              ["t1", "t2"], 600_000)]  # 10 minutes
    dev = {"id": "dev1", "name": "relay"}
    journalled = []
    timer_calls = []
    monkeypatch.setattr(comedy.play, "configure_sleep_timer",
                        lambda ip, dur: timer_calls.append((ip, dur)))
    monkeypatch.setattr(comedy.play, "journal_span",
                        lambda action, ip, **extra: journalled.append((action, ip, extra)))

    result = comedy.start_queue(sess, dev, picks, "192.168.1.4", buffer_min=5)

    assert sess.played_calls == [{"uris": ["t1", "t2"], "device_id": "dev1"}]
    assert result["duration_s"] == 600 + 5 * 60
    assert result["timer_armed"] is True
    assert timer_calls == [("192.168.1.4", 900)]
    # Only two span records, both scoped to the tail, never the whole session
    assert {j[0] for j in journalled} == {"comedy_sleep_tail_start", "comedy_sleep_tail_end"}
    start = next(j for j in journalled if j[0] == "comedy_sleep_tail_start")
    end = next(j for j in journalled if j[0] == "comedy_sleep_tail_end")
    gap = (datetime.fromisoformat(end[2]["ts"]) - datetime.fromisoformat(start[2]["ts"]))
    # buffer_min*60 (the padding) + JOURNAL_MARGIN_S (sampling slack) + the
    # 60s the tail starts early
    assert gap.total_seconds() == pytest.approx(5 * 60 + comedy.JOURNAL_MARGIN_S + 60, abs=1)
    assert comedy.load_played()[-1]["album_id"] == "1"


def test_start_queue_still_records_the_play_when_the_sleep_timer_fails(monkeypatch):
    """The timer has never run on real hardware; a SOAP failure there must
    not lose the fact that the Roam is, in fact, playing -- and must not
    journal an unmatched span-start either, which `_self_induced` would
    read as open until the *next* successful `comedy sleep`, discounting
    every event on every speaker in between."""
    sess = FakeSession()
    picks = [comedy.QueuePick("1", "Special", "Comedian", "u", ["t1"], 60_000)]
    dev = {"id": "dev1", "name": "relay"}
    journalled = []

    def boom(ip, dur):
        raise OSError("no route to host")
    monkeypatch.setattr(comedy.play, "configure_sleep_timer", boom)
    monkeypatch.setattr(comedy.play, "journal_span",
                        lambda action, ip, **extra: journalled.append((action, ip, extra)))

    result = comedy.start_queue(sess, dev, picks, "192.168.1.4")

    assert sess.played_calls, "the play call must still have happened"
    assert result["timer_armed"] is False
    assert "no route to host" in result["timer_error"]
    assert journalled == [], "no span at all should be journalled for an unarmed timer"
    assert comedy.load_played()[-1]["album_id"] == "1", \
        "the play must be on record even though the timer failed"


def test_start_queue_refuses_an_empty_queue():
    with pytest.raises(ValueError):
        comedy.start_queue(FakeSession(), {"id": "d"}, [], "1.2.3.4")


# ---- the tail span, against the real journal and report code ------------------


def test_comedy_sleep_discounts_only_the_tail_not_the_whole_session(monkeypatch):
    """Uses the real `start_queue`/`play.journal_span`/
    `report._load_interventions`/`_self_induced` -- not mocks for the
    journal itself -- against the tmp `INTERVENTION_LOG` `conftest.py`
    already redirects.

    This is the inverse of what an earlier draft got wrong: a span covering
    the whole 2-3 hour listening session would have discounted every
    dropout during real comedy playback, household-wide, as self-induced --
    exactly the evidence the relay experiment exists to collect. Only the
    padded-silence tail should be discounted.
    """
    sess = FakeSession()
    picks = [comedy.QueuePick("1", "Special", "Comedian", "u", ["t1"],
                              3 * 3600 * 1000)]  # a realistic 3-hour queue
    dev = {"id": "dev1", "name": "relay"}
    monkeypatch.setattr(comedy.play, "configure_sleep_timer", lambda ip, dur: None)

    before = datetime.now(timezone.utc)
    result = comedy.start_queue(sess, dev, picks, "192.168.1.4", buffer_min=2)

    interventions = report._load_interventions(play.INTERVENTION_LOG)
    fires_at = datetime.fromisoformat(result["fires_at"])
    mid_playback = (before + timedelta(hours=1)).isoformat()
    just_after_the_timer_fires = (fires_at + timedelta(seconds=20)).isoformat()
    well_after_the_margin = (fires_at
                             + timedelta(seconds=comedy.JOURNAL_MARGIN_S + 600)).isoformat()

    assert report._self_induced(mid_playback, interventions) is None, \
        "a dropout during real comedy playback must still count as evidence"
    assert report._self_induced(just_after_the_timer_fires, interventions) is not None
    assert report._self_induced(well_after_the_margin, interventions) is None


# ---- CLI: `comedy sleep --dry-run` never writes --------------------------------


class _FakeGroup:
    ip = "192.168.1.4"
    name = "Roam"


class _FakeResolution:
    group = _FakeGroup()


def test_cmd_sleep_dry_run_never_plays_or_arms_the_timer(monkeypatch):
    sess = FakeSession()
    comedy.save_artists([comedy.ComedyArtist(id="a1", name="Someone", added_at="t")])
    pick = comedy.QueuePick("1", "Special", "Someone", "u", ["t1"], 60_000)

    monkeypatch.setattr(comedy_cli, "_session", lambda args: (sess, None))
    monkeypatch.setattr(comedy_cli, "_target", lambda args: (_FakeResolution(), None))
    monkeypatch.setattr(comedy_cli, "_relay_device_or_preview",
                        lambda args, s, name: ({"id": "dev1", "name": "relay"}, None))
    monkeypatch.setattr(comedy_cli, "_ensure_room_on_relay", lambda res, dry: None)
    monkeypatch.setattr(comedy, "pick_queue", lambda s, artists, n: [pick])
    timer_calls = []
    monkeypatch.setattr(comedy.play, "configure_sleep_timer",
                        lambda *a: timer_calls.append(a))

    args = cli.build_parser().parse_args(["comedy", "sleep", "--room", "roam", "--dry-run"])
    rc = args.func(args)

    assert rc == 0
    assert sess.played_calls == []
    assert timer_calls == []


def test_cmd_sleep_fails_clearly_with_no_classified_artists(monkeypatch):
    monkeypatch.setattr(comedy_cli, "_session", lambda args: (FakeSession(), None))
    args = cli.build_parser().parse_args(["comedy", "sleep", "--room", "roam", "--dry-run"])
    rc = args.func(args)
    assert rc == 1


def test_cmd_rate_reports_a_clear_error_with_nothing_played():
    args = cli.build_parser().parse_args(["comedy", "rate", "up"])
    assert args.func(args) == 1


# ---- CLI: `comedy sleep --cancel` ----------------------------------------------


def test_cmd_sleep_cancel_dry_run_does_not_touch_the_speaker(monkeypatch):
    monkeypatch.setattr(comedy_cli, "_target", lambda args: (_FakeResolution(), None))
    timer_calls = []
    monkeypatch.setattr(comedy_cli.play, "configure_sleep_timer",
                        lambda *a: timer_calls.append(a))

    args = cli.build_parser().parse_args(
        ["comedy", "sleep", "--cancel", "--room", "roam", "--dry-run"])
    rc = args.func(args)

    assert rc == 0
    assert timer_calls == []


def test_cmd_sleep_cancel_calls_configure_sleep_timer_with_zero(monkeypatch):
    monkeypatch.setattr(comedy_cli, "_target", lambda args: (_FakeResolution(), None))
    timer_calls = []
    monkeypatch.setattr(comedy_cli.play, "configure_sleep_timer",
                        lambda *a: timer_calls.append(a))

    args = cli.build_parser().parse_args(["comedy", "sleep", "--cancel", "--room", "roam"])
    rc = args.func(args)

    assert rc == 0
    assert timer_calls == [("192.168.1.4", 0)]
