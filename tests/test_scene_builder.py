"""The builder: collection, dedupe, enrichment and publication -- all faked.

Nothing here touches the network: sources, enrichers, the Bandcamp genre
search and Wikipedia are injected.
"""
import copy
from datetime import date

import pytest

from twiddle import spotify, spotify_ops
from twiddle.scene import bandcamp, builder, cache, dataset
from twiddle.scene.bands import BandProfile
from twiddle.scene.model import Show
from twiddle.scene.sources import SourceError

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


def run(tmp_path, sources, enrichers=None, **kw):
    kw.setdefault("genre_search", lambda name, offline=False: None)
    kw.setdefault("wiki", lambda title: None)
    kw.setdefault("today", TODAY)
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
            now=lambda: 1_790_000_000.0 + builder.PROFILE_TTL_S + 60)
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
    monkeypatch.setattr(builder, "LookupEnricher", lambda: Enr("lookup"))
    monkeypatch.setattr(builder, "BandcampEnricher", lambda: Enr("bandcamp"))
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
    monkeypatch.setattr(builder, "LookupEnricher", lambda: Enr("lookup"))
    monkeypatch.setattr(builder, "BandcampEnricher", lambda: Enr("bandcamp"))
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
