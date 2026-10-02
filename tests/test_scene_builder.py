"""The builder: collection, dedupe, enrichment and publication -- all faked.

Nothing here touches the network: sources, enrichers, the Bandcamp genre
search and Wikipedia are injected.
"""
import copy
from datetime import date

import pytest

from twiddle import spotify, spotify_ops
from twiddle.scene import cache
from twiddle.scenedata import bandcamp, cache as data_cache
from twiddle.scenespec import dataset
from twiddle.scenedata import builder
from twiddle.scenespec.band import BandProfile
from twiddle.scenespec.model import Show
from twiddle.scenedata.sources import SourceError

TODAY = date(2026, 10, 1)
STORK = "Thee Stork Club, Oakland"
IVY = "Ivy Room, Albany"
ELSEWHERE = "Somewhere Else, S.F."
GIRL = Show(date(2026, 10, 5), STORK, ["Girl Chow", "Wiseacre"], source="thelist")
GIRL_SITE = Show(date(2026, 10, 5), STORK, ["Girl Chow"], source="storkclub",
                 flyer="https://f/g", tickets="https://t/g")
COUP = Show(date(2026, 10, 6), IVY, ["Coup Dville"], source="thelist")
NOBODY = Show(date(2026, 10, 7), ELSEWHERE, ["Nobody"], source="thelist")
FAR = Show(date(2027, 3, 1), IVY, ["Far Away"], source="thelist")


class Src:
    def __init__(self, name, shows=(), error=None):
        self.name, self.shows, self.error = name, list(shows), error
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        if self.error:
            raise SourceError(self.error)
        return list(self.shows)


class Enr:
    """An enricher that counts who it was asked about."""
    serial = False

    def __init__(self, name, fill=None, raises=None):
        self.name, self.fill, self.raises = name, fill, raises
        self.asked: list[str] = []

    def enrich(self, p: BandProfile):
        self.asked.append(p.band)
        if self.raises:
            raise self.raises
        if self.fill:
            self.fill(p)


def spotify_fill(p):
    p.spotify_artist = {"id": "sp-" + p.band, "name": p.band}
    p.tracks = [{"uri": "spotify:track:1", "name": "One"}]


PACED: list[float] = []         # every pause the builder asked for, in order


def run(tmp_path, sources, enrichers=None, **kw):
    PACED.clear()
    kw.setdefault("pace", PACED.append)        # no real sleeping, however long it asks
    kw.setdefault("genre_search", lambda name, offline=False: None)
    kw.setdefault("wiki", lambda title: None)
    kw.setdefault("today", TODAY)
    kw.setdefault("spotify_gap", 0)
    kw.setdefault("now", lambda: 1_790_000_000.0)
    enrichers = [Enr("lookup"), Enr("spotify", spotify_fill), Enr("bandcamp")] \
        if enrichers is None else enrichers
    return builder.build(path=tmp_path / "d.json", sources=sources, enrichers=enrichers, **kw)


def read(tmp_path):
    return dataset.load(tmp_path / "d.json")


def test_two_sources_for_one_night_are_published_as_one_show_with_provenance(tmp_path):
    run(tmp_path, [Src("thelist", [GIRL, COUP]), Src("storkclub", [GIRL_SITE])])
    snap = read(tmp_path)
    [girl] = [s for s in snap.shows if s.day == date(2026, 10, 5)]
    assert girl.source == "thelist" and girl.also == ["storkclub"]
    assert girl.flyer == "https://f/g" and girl.tickets == "https://t/g"
    assert len(snap.shows) == 2
    assert snap.sources["storkclub"] == {"ok": True, "count": 1, "error": None,
                                         "fetched_at": snap.sources["storkclub"]["fetched_at"]}
    assert len(set(snap.show_ids)) == 2 and snap.complete
    assert snap.venue("Stork Club") is not None


def test_a_failed_source_keeps_its_rows_from_the_last_dataset(tmp_path):
    run(tmp_path, [Src("thelist", [GIRL, COUP]), Src("storkclub", [GIRL_SITE])])
    first = read(tmp_path).sources["storkclub"]["fetched_at"]
    r = run(tmp_path, [Src("thelist", [COUP]), Src("storkclub", error="site is down")],
            now=lambda: 1_790_100_000.0)
    snap = read(tmp_path)
    assert "storkclub: site is down" in r.errors
    assert snap.sources["storkclub"]["ok"] is False
    assert snap.sources["storkclub"]["fetched_at"] == first      # when it last worked
    [girl] = [s for s in snap.shows if s.day == date(2026, 10, 5)]
    assert girl.flyer == "https://f/g"                            # the site's rows survived


def test_when_nothing_can_be_collected_and_nothing_is_kept_nothing_is_published(tmp_path):
    with pytest.raises(builder.BuildError, match="no listings"):
        run(tmp_path, [Src("thelist", error="down")])
    assert not (tmp_path / "d.json").exists()


def test_it_publishes_listings_first_then_the_enriched_dataset(tmp_path, monkeypatch):
    docs = []
    real = dataset.publish
    monkeypatch.setattr(dataset, "publish",
                        lambda d, path=None: (docs.append(copy.deepcopy(d)), real(d, path))[1])
    run(tmp_path, [Src("thelist", [GIRL, COUP])])
    assert [d["complete"] for d in docs] == [False, True]
    first = docs[0]
    assert len(first["shows"]) == 2                       # shows are there before enrichment
    assert first["bands"]["girl chow"]["status"] == {}    # ... and not yet enriched
    assert docs[-1]["bands"]["girl chow"]["status"]["spotify"] == "done"


def test_every_billed_band_gets_a_record_but_only_the_window_is_enriched(tmp_path):
    spot = Enr("spotify", spotify_fill)
    look = Enr("lookup")
    hits = {"Nobody": [{"name": "Nobody", "location": "Oakland, California",
                        "item_url_root": "https://n.bandcamp.com", "genre_name": "Reggae",
                        "tag_names": ["dub"]}]}
    r = run(tmp_path, [Src("thelist", [GIRL, COUP, NOBODY, FAR])], [look, spot],
            genre_search=lambda name, offline=False: hits.get(name))
    snap = read(tmp_path)
    assert set(look.asked) == {"Girl Chow", "Wiseacre", "Coup Dville"}   # watched, this month
    assert r.enriched == 3
    for name in ("Nobody", "Far Away"):                    # unwatched / too far ahead
        assert snap.band(name)["status"] == {}
    assert snap.band("Nobody")["genre"]["scores"]          # cache-only genre still there
    assert snap.band("Girl Chow")["identifiers"]["spotify_id"] == "sp-Girl Chow"
    assert snap.enrichers == {"lookup": "ok", "spotify": "ok"}


def test_all_venues_enriches_the_unwatched_rooms_too(tmp_path):
    look = Enr("lookup")
    run(tmp_path, [Src("thelist", [GIRL, NOBODY])], [look], all_venues=True)
    assert "Nobody" in look.asked


def test_a_second_build_reuses_bands_enriched_recently_and_redoes_stale_ones(tmp_path):
    look = Enr("lookup")
    run(tmp_path, [Src("thelist", [GIRL, COUP])], [look])
    assert len(look.asked) == 3
    r = run(tmp_path, [Src("thelist", [GIRL, COUP])], [look], now=lambda: 1_790_000_000.0 + 3600)
    assert len(look.asked) == 3 and r.reused == 3 and r.enriched == 0
    r = run(tmp_path, [Src("thelist", [GIRL, COUP])], [look],
            now=lambda: 1_790_000_000.0 + builder.PROFILE_TTL_S * 1.5 + 60)
    assert len(look.asked) == 6 and r.enriched == 3


def test_a_band_that_errored_is_tried_again_next_build(tmp_path):
    flaky = Enr("lookup", raises=RuntimeError("MusicBrainz 503"))
    run(tmp_path, [Src("thelist", [COUP])], [flaky])
    assert read(tmp_path).band("Coup Dville")["status"]["lookup"].startswith("error")
    ok = Enr("lookup")
    run(tmp_path, [Src("thelist", [COUP])], [ok], now=lambda: 1_790_000_100.0)
    assert ok.asked == ["Coup Dville"]


def test_the_same_inputs_publish_the_same_file(tmp_path):
    run(tmp_path, [Src("thelist", [GIRL, COUP])])
    a = (tmp_path / "d.json").read_text()
    run(tmp_path, [Src("thelist", [GIRL, COUP])])
    assert (tmp_path / "d.json").read_text() == a


def test_a_second_build_cannot_run_while_one_holds_the_lock(tmp_path):
    with builder._locked(tmp_path / "d.json"):
        with pytest.raises(builder.BuildBusy):
            run(tmp_path, [Src("thelist", [GIRL])])
    run(tmp_path, [Src("thelist", [GIRL])])                  # free again


def test_rate_limited_bandcamp_pauses_that_enricher_and_the_dataset_still_publishes(tmp_path):
    blocked = Enr("bandcamp", raises=bandcamp.BlockedError("rate-limited"))
    spot = Enr("spotify", spotify_fill)
    r = run(tmp_path, [Src("thelist", [GIRL, COUP])], [spot, blocked])
    snap = read(tmp_path)
    assert blocked.asked == ["Girl Chow"]                     # stopped after the first
    assert snap.enrichers["bandcamp"] == "paused: rate-limited"
    assert snap.band("Coup Dville")["status"]["spotify"] == "done"
    assert snap.complete and r.enriched == 3


def test_spotify_is_skipped_not_prompted_when_this_mac_is_not_signed_in(tmp_path, monkeypatch):
    def no_session():
        raise spotify_ops.PlaybackError("run auth") from spotify.AuthError("no token")
    monkeypatch.setattr(spotify_ops, "session", no_session)
    monkeypatch.setattr(builder, "LookupEnricher", lambda **kw: Enr("lookup"))
    monkeypatch.setattr(builder, "BandcampEnricher", lambda **kw: Enr("bandcamp"))
    builder.build(path=tmp_path / "d.json", sources=[Src("thelist", [COUP])],
                  genre_search=lambda n, offline=False: None, wiki=lambda t: None, today=TODAY)
    snap = read(tmp_path)
    assert snap.enrichers["spotify"].startswith("skipped: not signed in")
    assert snap.band("Coup Dville")["status"]["lookup"] == "done"


def test_one_persons_pins_are_not_published_with_the_data(tmp_path, monkeypatch):
    """The dataset is graded on evidence alone. A pin is one person's click."""
    from twiddle import discover_cli

    class Sess:
        def search(self, term, kind, limit=10):
            return [{"id": "found", "name": term}]

        def request(self, method, path):
            return {"id": path.rsplit("/", 1)[1], "name": "Pinned"}
    monkeypatch.setattr(discover_cli, "_artist_tracks", lambda sess, artist: [])
    monkeypatch.setattr(spotify_ops, "session", lambda: Sess())
    monkeypatch.setattr(builder, "LookupEnricher", lambda **kw: Enr("lookup"))
    monkeypatch.setattr(builder, "BandcampEnricher", lambda **kw: Enr("bandcamp"))
    cache.pin("Coup Dville", "hand-picked", "Coup")
    builder.build(path=tmp_path / "d.json", sources=[Src("thelist", [COUP])],
                  genre_search=lambda n, offline=False: None, wiki=lambda t: None, today=TODAY)
    rec = read(tmp_path).band("Coup Dville")
    assert rec["identifiers"]["spotify_id"] == "found"        # by name, not the pin
    assert rec["why"] != "chosen by you" and rec["confidence"] == "name_only"


def test_wikipedia_summaries_are_stored_and_survive_a_failed_fetch(tmp_path):
    fox = Show(date(2026, 10, 5), "Fox Theater, Oakland", ["Somebody"], source="thelist")
    run(tmp_path, [Src("thelist", [fox])],
        wiki=lambda title: {"extract": f"About {title}.", "url": "u"})
    assert read(tmp_path).venue("Fox Theater")["wikipedia_summary"]["extract"] \
        == "About Fox Oakland Theatre."

    def down(title):
        raise OSError("offline")
    run(tmp_path, [Src("thelist", [fox])], wiki=down, now=lambda: 1_790_000_100.0)
    assert read(tmp_path).venue("Fox Theater")["wikipedia_summary"]["extract"]


def test_dry_run_publishes_nothing(tmp_path):
    r = run(tmp_path, [Src("thelist", [GIRL])], dry_run=True)
    assert r.shows == 1 and not (tmp_path / "d.json").exists()


def test_a_build_without_spotify_does_not_claim_a_band_is_not_on_spotify(tmp_path):
    run(tmp_path, [Src("thelist", [COUP])], [Enr("lookup"), Enr("bandcamp")])
    rec = read(tmp_path).band("Coup Dville")
    assert rec["confidence"] == "pending" and "not checked" in rec["why"]
    assert "spotify" not in rec["status"]


def test_a_spotify_429_that_persists_pauses_spotify_for_the_run_and_the_build_still_publishes(tmp_path):
    limited = Enr("spotify", raises=spotify.ApiError(429, "rate limited", retry_after=30))
    other = Enr("bandcamp")
    r = run(tmp_path, [Src("thelist", [GIRL, COUP])], [limited, other])
    snap = read(tmp_path)
    # waited the 30s it asked for (+1) once and retried; the 429 came straight back, so: paused
    assert limited.asked == ["Girl Chow", "Girl Chow"] and 31.0 in PACED
    assert snap.enrichers["spotify"] == "paused: rate-limited (retry after 30s)"
    assert other.asked and snap.complete and r.enriched == 3


def test_a_spotify_429_with_no_stated_time_pauses_at_once(tmp_path):
    limited = Enr("spotify", raises=spotify.ApiError(429, "rate limited"))
    run(tmp_path, [Src("thelist", [GIRL, COUP])], [limited])
    assert limited.asked == ["Girl Chow"] and read(tmp_path).enrichers["spotify"] == "paused: rate-limited"


def test_a_429_asking_for_longer_than_the_cap_pauses_instead_of_waiting(tmp_path):
    limited = Enr("spotify", raises=spotify.ApiError(429, "rate limited", retry_after=3600))
    run(tmp_path, [Src("thelist", [GIRL, COUP])], [limited])
    assert limited.asked == ["Girl Chow"] and not [x for x in PACED if x > 100]


class FlakyOnce(Enr):
    """Says slow down for the first call only, then answers."""

    def enrich(self, p):
        self.asked.append(p.band)
        if len(self.asked) == 1:
            raise spotify.ApiError(429, "rate limited", retry_after=120)
        spotify_fill(p)


def test_a_429_that_clears_after_the_wait_lets_the_build_finish_with_spotify(tmp_path):
    flaky = FlakyOnce("spotify")
    r = run(tmp_path, [Src("thelist", [GIRL, COUP])], [flaky])
    snap = read(tmp_path)
    assert flaky.asked[0] == flaky.asked[1] and len(flaky.asked) == 4      # the first band, retried once
    assert set(flaky.asked) == {"Girl Chow", "Coup Dville", "Wiseacre"}
    assert 121.0 in PACED                                      # waited the 120s it named, plus one
    assert snap.enrichers["spotify"] == "ok"                   # never paused
    assert r.enriched == 3 and all(
        snap.band(n)["status"]["spotify"] == "done" for n in ("Girl Chow", "Coup Dville", "Wiseacre"))


def test_the_gap_between_bands_widens_after_a_wait_and_relaxes_with_successes():
    from twiddle.scenedata.pacing import Backpressure
    waits = []
    bp = Backpressure({"spotify": 0.5}, pace=waits.append)
    bp.wait("spotify", 10)
    assert bp.gap("spotify") == 1.0 and waits == [10]
    bp.wait("spotify", 10)
    assert bp.gap("spotify") == 2.0
    for _ in range(30):
        bp.ok("spotify")
    assert bp.gap("spotify") == 0.5                            # back to where it started
    bp.wait("spotify", 10)
    assert bp.seconds_to_wait("spotify", spotify.ApiError(429, "x", retry_after=5)) is None   # waits spent


def test_a_bandcamp_back_off_is_waited_out_when_short_and_paused_when_long(monkeypatch):
    from twiddle import bandcamp as site
    from twiddle.scenedata.pacing import Backpressure
    bp = Backpressure({}, pace=lambda s: None, max_wait_s=900)
    monkeypatch.setattr(site, "blocked_for", lambda: 600.0)
    assert bp.seconds_to_wait("bandcamp", site.BlockedError("resting")) == 601.0
    monkeypatch.setattr(site, "blocked_for", lambda: 1200.0)
    assert bp.seconds_to_wait("bandcamp", site.BlockedError("resting")) is None


def test_other_spotify_errors_do_not_pause_it(tmp_path):
    flaky = Enr("spotify", raises=spotify.ApiError(500, "oops"))
    run(tmp_path, [Src("thelist", [GIRL, COUP])], [flaky])
    assert len(flaky.asked) == 3


def test_bands_expire_spread_over_time_not_all_at_once():
    ttls = {builder._ttl(f"band {i}") for i in range(200)}
    assert min(ttls) >= builder.PROFILE_TTL_S and max(ttls) <= builder.PROFILE_TTL_S * 1.5
    assert len({round(t / 3600) for t in ttls}) > 10           # spread over many hours
    assert builder._ttl("girl chow") == builder._ttl("girl chow")


def test_spotify_lookups_are_paced_and_only_when_spotify_runs(tmp_path):
    naps = []
    run(tmp_path, [Src("thelist", [GIRL, COUP])], spotify_gap=0.5, pace=naps.append)
    assert naps == [0.5, 0.5, 0.5]
    naps.clear()
    other = tmp_path / "e.json"
    builder.build(path=other, sources=[Src("thelist", [GIRL])], enrichers=[Enr("lookup")],
                  genre_search=lambda n, offline=False: None, wiki=lambda t: None, today=TODAY,
                  pace=naps.append)
    assert naps == []


def test_the_builder_records_identity_but_not_song_lists(tmp_path, monkeypatch):
    """Track lists were most of a band's requests (2-4 pages each) and are only
    used on play: the app fetches them for the band on screen."""
    from twiddle import discover_cli

    class Sess:
        def search(self, term, kind, limit=10):
            return [{"id": "found", "name": term}]
    seen = []
    monkeypatch.setattr(discover_cli, "_artist_tracks", lambda sess, a: seen.append(a) or ["t"])
    monkeypatch.setattr(spotify_ops, "session", lambda: Sess())
    monkeypatch.setattr(builder, "LookupEnricher", lambda **kw: Enr("lookup"))
    bc_pages = []
    real = builder.BandcampEnricher       # its defaults bind the real network functions
    monkeypatch.setattr(builder, "BandcampEnricher", lambda **kw: real(
        search=lambda name: [{"name": name, "item_url_root": "https://c.bandcamp.com",
                              "location": "Oakland, California"}],
        tracks=lambda url: bc_pages.append(url) or ["t"], **kw))
    builder.build(path=tmp_path / "d.json", sources=[Src("thelist", [COUP])],
                  genre_search=lambda n, offline=False: None, wiki=lambda t: None, today=TODAY,
                  spotify_gap=0)
    rec = read(tmp_path).band("Coup Dville")
    assert rec["identifiers"]["spotify_id"] == "found"
    assert rec["identifiers"]["bandcamp"] == "https://c.bandcamp.com"
    assert rec["tracks"] == [] and rec["bc_tracks"] == [] and seen == [] and bc_pages == []
    assert "tracks" not in rec["status"]


def test_listings_are_published_before_the_slow_wikipedia_requests(tmp_path, monkeypatch):
    """Codex: a cold Wikipedia (up to 10 s a venue) must not delay first paint."""
    docs, order = [], []
    real = dataset.publish
    monkeypatch.setattr(dataset, "publish", lambda d, path=None: (
        order.append("publish"), docs.append(copy.deepcopy(d)), real(d, path))[2])
    fox = Show(date(2026, 10, 5), "Fox Theater, Oakland", ["Somebody"], source="thelist")
    run(tmp_path, [Src("thelist", [fox])],
        wiki=lambda t: order.append("wiki") or {"extract": "A hall.", "url": "u"})
    assert order[:2] == ["publish", "wiki"]
    first = next(v for v in docs[0]["venues"] if v["name"] == "Fox Theater")
    assert first["wikipedia_summary"] is None                # not yet
    last = next(v for v in docs[-1]["venues"] if v["name"] == "Fox Theater")
    assert last["wikipedia_summary"]["extract"] == "A hall."


def test_a_skipped_or_failed_enricher_does_not_erase_what_the_last_build_learned(tmp_path):
    """Codex: a signed-out rebuild must keep last time's Spotify artist."""
    run(tmp_path, [Src("thelist", [COUP])], [Enr("lookup"), Enr("spotify", spotify_fill),
                                             Enr("bandcamp")])
    assert read(tmp_path).band("Coup Dville")["identifiers"]["spotify_id"] == "sp-Coup Dville"
    later = 1_790_000_000.0 + builder.PROFILE_TTL_S * 2          # the record has expired
    run(tmp_path, [Src("thelist", [COUP])], [Enr("lookup"), Enr("bandcamp")], now=lambda: later)
    rec = read(tmp_path).band("Coup Dville")
    assert rec["identifiers"]["spotify_id"] == "sp-Coup Dville"
    assert rec["status"]["spotify"].startswith("kept") and rec["tracks"]
    assert rec["updated_at"] == dataset.iso(later)               # the rest was refreshed
    # a failing one keeps its old answer too
    run(tmp_path, [Src("thelist", [COUP])],
        [Enr("lookup"), Enr("spotify", raises=RuntimeError("503")), Enr("bandcamp")],
        now=lambda: later + builder.PROFILE_TTL_S * 2)
    assert read(tmp_path).band("Coup Dville")["identifiers"]["spotify_id"] == "sp-Coup Dville"


def test_a_build_will_not_overwrite_a_dataset_it_cannot_read(tmp_path):
    import json
    path = tmp_path / "d.json"
    newer = {"schema": dataset.SCHEMA, "version": dataset.VERSION + 1, "shows": []}
    path.write_text(json.dumps(newer))
    with pytest.raises(builder.BuildError, match="not replacing"):
        run(tmp_path, [Src("thelist", [GIRL])])
    assert json.loads(path.read_text()) == newer
    path.write_text(json.dumps({"schema": "something.else"}))
    with pytest.raises(builder.BuildError, match="not replacing"):
        run(tmp_path, [Src("thelist", [GIRL])])
    path.write_text("{torn")                                     # damage, on the other hand, is replaced
    run(tmp_path, [Src("thelist", [GIRL])])
    assert dataset.load(path).shows


def test_a_bandcamp_pause_also_stops_the_lookups_last_resort_bandcamp_search(monkeypatch):
    """Codex: MusicBrainz's fallback is a Bandcamp name search that ignored the pause."""
    from twiddle import lookup
    from twiddle.scenespec.band import BandProfile
    asked = []
    monkeypatch.setattr(lookup, "_by_name", lambda a: (None, []))
    monkeypatch.setattr(lookup, "_by_fuzzy", lambda *a: None)
    monkeypatch.setattr(lookup, "_by_discogs", lambda a: (None, []))
    monkeypatch.setattr(lookup, "_by_bandcamp", lambda a: asked.append(a) or None)
    monkeypatch.setattr(lookup, "use_cache", True, raising=False)
    skip: set[str] = set()
    monkeypatch.setattr(spotify_ops, "session", lambda: 1 / 0)
    lookup_enricher = builder._default_enrichers(False, {}, skip)[0]
    lookup_enricher.enrich(BandProfile("Unknown One"))
    assert asked == ["Unknown One"]                       # Bandcamp fine: last resort used
    skip.add("bandcamp")
    lookup_enricher.enrich(BandProfile("Unknown Two"))
    assert asked == ["Unknown One"]                       # paused: not asked again
    assert data_cache.alias("Unknown Two") is None             # and no false "nothing worked" recorded


def test_a_carried_over_answer_is_retried_next_build_not_treated_as_fresh(tmp_path):
    """Codex: a failed refresh must not reset the record's age."""
    run(tmp_path, [Src("thelist", [COUP])], [Enr("lookup"), Enr("spotify", spotify_fill),
                                             Enr("bandcamp")])
    later = 1_790_000_000.0 + builder.PROFILE_TTL_S * 2
    run(tmp_path, [Src("thelist", [COUP])],
        [Enr("lookup"), Enr("spotify", raises=spotify.ApiError(429, "slow down")), Enr("bandcamp")],
        now=lambda: later)
    rec = read(tmp_path).band("Coup Dville")
    assert rec["identifiers"]["spotify_id"] == "sp-Coup Dville"          # kept ...
    assert rec["status"]["spotify"].startswith("kept") and "slow down" in rec["status"]["spotify"]
    retry = Enr("spotify", spotify_fill)
    r = run(tmp_path, [Src("thelist", [COUP])], [Enr("lookup"), retry, Enr("bandcamp")],
            now=lambda: later + 60)                                       # well inside the TTL
    assert retry.asked == ["Coup Dville"] and r.enriched == 1             # ... and retried
    assert read(tmp_path).band("Coup Dville")["status"]["spotify"] == "done"


def test_a_kept_answer_still_counts_as_known_to_the_app_and_the_badge():
    from twiddle.scenespec import profiles
    from twiddle.scenespec.band import BandProfile
    p = BandProfile("X")
    p.status = {"lookup": "done", "spotify": "kept: error: 429", "bandcamp": "done"}
    p.spotify_artist = {"id": "s1"}
    rec = profiles.to_record(p, updated_at=1.0, guess=None)
    assert rec["confidence"] != "pending" or "not checked" not in rec["why"]
    back = profiles.from_record(rec)
    assert back.status["spotify"] == "done" and back.spotify_artist == {"id": "s1"}
    assert not profiles.enriched(rec)


def test_kept_answers_survive_more_than_one_failed_build(tmp_path):
    """Codex: a second failure must not erase what the first one kept."""
    run(tmp_path, [Src("thelist", [COUP])], [Enr("lookup"), Enr("spotify", spotify_fill),
                                             Enr("bandcamp")])
    t = 1_790_000_000.0
    for n in (2, 4, 6):                                   # three builds in which everything fails
        run(tmp_path, [Src("thelist", [COUP])],
            [Enr("lookup", raises=RuntimeError("503")), Enr("spotify", raises=RuntimeError("503")),
             Enr("bandcamp", raises=RuntimeError("503"))],
            now=lambda n=n: t + builder.PROFILE_TTL_S * n)
    rec = read(tmp_path).band("Coup Dville")
    assert rec["identifiers"]["spotify_id"] == "sp-Coup Dville"
    assert all(v.startswith("kept") for v in rec["status"].values())


def test_unwatched_rooms_get_a_usable_venue_id_so_they_can_be_queried(tmp_path):
    run(tmp_path, [Src("thelist", [NOBODY, GIRL])])
    snap = read(tmp_path)
    assert all(vid for vid in snap.venue_ids)
    assert [s.billing for s in snap.shows_at("Somewhere Else, S.F.")] == ["Nobody"]


def test_a_failed_lookup_hands_its_old_alias_to_spotify_and_bandcamp_before_they_search(tmp_path):
    """Codex: restoring lookup evidence afterwards let a name collision win."""
    seen = {}

    def lookup_fill(p):
        p.alias = "Trimmed Name"

    class SeesAlias(Enr):
        def enrich(self, p):
            seen.setdefault(self.name, []).append(p.alias)
            super().enrich(p)
    run(tmp_path, [Src("thelist", [COUP])], [Enr("lookup", lookup_fill), SeesAlias("spotify", spotify_fill)])
    seen.clear()
    later = 1_790_000_000.0 + builder.PROFILE_TTL_S * 2
    run(tmp_path, [Src("thelist", [COUP])],
        [Enr("lookup", raises=RuntimeError("503")), SeesAlias("spotify", spotify_fill)],
        now=lambda: later)
    assert seen["spotify"] == ["Trimmed Name"]                 # not None: the old alias, in time
    assert read(tmp_path).band("Coup Dville")["status"]["lookup"].startswith("kept")


def test_a_build_says_which_dataset_it_is_and_the_config_can_rename_it(tmp_path, monkeypatch):
    run(tmp_path, [Src("thelist", [GIRL])])
    snap = read(tmp_path)
    assert (snap.id, snap.kind) == ("bay-area-music", "music") and "Bay Area" in snap.region
    cfg = tmp_path / "scene.toml"
    cfg.write_text('[dataset]\nid = "nyc-comedy"\nname = "NYC comedy"\nkind = "comedy"\n')
    from twiddle.scenedata import venues
    monkeypatch.setattr(venues, "CONFIG_PATH", cfg)
    run(tmp_path, [Src("thelist", [GIRL])])
    snap = read(tmp_path)
    assert (snap.id, snap.name, snap.kind) == ("nyc-comedy", "NYC comedy", "comedy")
    assert "Bay Area" in snap.region                    # a field the file does not give keeps its default


def test_a_build_never_sends_a_request_to_a_service_that_has_locked_us_out(tmp_path, monkeypatch):
    """A lockout from an earlier run (or another tool) is in the shared ledger: the
    build skips Spotify without so much as loading a session, and says why."""
    from twiddle import ratelimit, spotify_ops
    ratelimit.governor("spotify").report(429, retry_after=80_000)
    monkeypatch.setattr(spotify_ops, "session", lambda: 1 / 0)          # must not be reached
    PACED.clear()
    builder.build(path=tmp_path / "d.json", sources=[Src("thelist", [GIRL])], use_spotify=True,
                  genre_search=lambda n, offline=False: None, wiki=lambda t: None, today=TODAY,
                  spotify_gap=0, pace=PACED.append, now=lambda: 1_790_000_000.0,
                  enrichers=None)
    snap = read(tmp_path)
    assert snap.enrichers["spotify"].startswith("skipped: rate-limited (retry after 80000s")
    assert snap.complete


def test_our_own_budget_running_out_mid_build_is_waited_for_like_a_429(tmp_path):
    """The governor raises a 429 carrying the time until there is room; the builder waits
    that (when short) and retries, exactly as for Spotify's own."""
    from twiddle import ratelimit, spotify
    calls = []

    class Budgeted(Enr):
        def enrich(self, p):
            self.asked.append(p.band)
            if not calls:
                calls.append(1)
                raise spotify.ApiError(429, "twiddle's own rate limit",
                                       retry_after=ratelimit.RateLimited("spotify", 40).retry_after)
            spotify_fill(p)

    r = run(tmp_path, [Src("thelist", [GIRL])], [Budgeted("spotify")])
    assert 41.0 in PACED and read(tmp_path).enrichers["spotify"] == "ok" and r.enriched >= 1
