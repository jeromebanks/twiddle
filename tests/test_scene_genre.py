"""Genre guesses, and the Bandcamp search they come from."""
import urllib.error

import pytest

from twiddle import lookup
from twiddle.scene import bandcamp, genre
from twiddle.scene.bands import BandBook, BandcampEnricher, BandProfile, near

SLEEPBOMB = {"name": "Sleepbomb", "item_url_root": "https://sleepbomb.bandcamp.com",
             "location": "San Francisco, California", "genre_name": "Metal",
             "tag_names": ["Metal", "soundtrack", "doom", "drone", "sludge", "post-metal"]}


# ---- tags -> families ----------------------------------------------------------------


@pytest.mark.parametrize("tag,family", [
    ("post-punk", "indie"),          # not punk
    ("dubstep", "electronic"),       # not dub
    ("dub", "reggae"),
    ("folk punk", "punk"),           # not folk
    ("Hardcore Punk", "punk"),
    ("atmospheric sludge", "metal"),  # unknown phrase: its last word
    ("San Francisco", None),          # a place, not a sound
    ("soundtrack", None),
])
def test_tags_are_matched_whole_never_by_substring(tag, family):
    assert genre.family_of(tag) == family


def test_bandcamps_own_genre_counts_double():
    g = genre.for_band(SLEEPBOMB)
    assert g.label() == "metal"
    assert "doom" in g.tags and "soundtrack" not in g.tags
    assert g.sources == {"Bandcamp"}


def test_a_close_runner_up_is_shown_too():
    g = genre.guess("Punk", ["hardcore", "powerviolence", "punk"])
    assert g.label() == "punk/hardcore"


def test_show_genre_weights_the_headliner_and_each_band_votes_once():
    metal = genre.guess("Metal", ["doom", "sludge", "stoner", "black metal"])  # heavily tagged
    reggae = genre.guess("Reggae")
    assert genre.for_show([reggae, metal]).top[0] == "reggae"   # headliner, one vote
    assert not genre.for_show([None, None])


# ---- choosing the band --------------------------------------------------------------


def test_choose_prefers_the_linked_page_then_unique_then_the_local_one():
    a = {"name": "Shape", "item_url_root": "https://shape.bandcamp.com", "location": "Leeds, UK"}
    b = {"name": "Shape", "item_url_root": "https://shapeband.bandcamp.com",
         "location": "Oakland, California"}
    label = {"name": "Shape", "item_url_root": "https://shaperecs.bandcamp.com",
             "is_label": True}
    assert bandcamp.choose([a, b], "https://shape.bandcamp.com/music") is a
    assert bandcamp.choose([a, label]) is a                  # a label is never the band
    assert bandcamp.choose([a, b], is_local=near) is b       # the one near the venue
    assert bandcamp.choose([a, b]) is None                   # otherwise, no guess


# ---- the search: cache and manners ---------------------------------------------------


@pytest.fixture
def fast(monkeypatch):
    bandcamp.reset()
    monkeypatch.setattr(bandcamp, "MIN_INTERVAL_S", 0)
    yield
    bandcamp.reset()


def test_search_is_cached_and_offline_says_when_it_does_not_know(monkeypatch, fast):
    calls = []
    monkeypatch.setattr(lookup, "bandcamp_bands", lambda n: calls.append(n) or [SLEEPBOMB])
    assert bandcamp.search("Sleepbomb", offline=True) is None
    assert bandcamp.search("Sleepbomb")[0]["genre_name"] == "Metal"
    assert bandcamp.search("sleepbomb")[0]["genre_name"] == "Metal"
    assert bandcamp.search("Sleepbomb", offline=True)
    assert calls == ["Sleepbomb"]


def test_a_429_stops_every_bandcamp_request_for_a_while(monkeypatch, fast):
    def slow_down(name):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(lookup, "bandcamp_bands", slow_down)
    with pytest.raises(urllib.error.HTTPError):
        bandcamp.search("One")
    asked = []
    monkeypatch.setattr(lookup, "bandcamp_bands", lambda n: asked.append(n) or [])
    with pytest.raises(bandcamp.BlockedError):
        bandcamp.search("Two")
    assert asked == []


def test_a_run_of_failures_backs_off_too(monkeypatch, fast):
    def down(name):
        raise TimeoutError("slow")
    monkeypatch.setattr(lookup, "bandcamp_bands", down)
    for i in range(bandcamp.MAX_ERRORS):
        with pytest.raises(TimeoutError):
            bandcamp.search(f"band {i}")
    with pytest.raises(bandcamp.BlockedError):
        bandcamp.search("next")


def test_tracks_and_a_fresh_stream_url_from_the_release_page(monkeypatch, fast):
    import html
    import json
    tralbum = {"current": {"title": "Songs in the Key of Conan"},
               "album_release_date": "03 Jun 2026 00:00:00 GMT", "art_id": 7,
               "trackinfo": [{"title": "Forged in Steel", "track_id": 1, "duration": 196.6,
                              "file": {"mp3-128": "https://t4.bcbits.com/stream/new"}},
                             {"title": "Preorder only", "track_id": 2, "file": None}]}
    album = f'<div data-tralbum="{html.escape(json.dumps(tralbum))}"></div>'
    pages = {"https://sleepbomb.bandcamp.com/music":
             '<div data-tralbum="{}"></div><a href="/album/conan">x</a>',
             "https://sleepbomb.bandcamp.com/album/conan": album}
    monkeypatch.setattr(bandcamp, "_get", pages.__getitem__)
    ts = bandcamp.tracks("https://sleepbomb.bandcamp.com")
    assert [(t["title"], t["year"]) for t in ts] == [("Forged in Steel", "2026")]
    assert bandcamp.stream_url(ts[0]) == "https://t4.bcbits.com/stream/new"


# ---- the enricher --------------------------------------------------------------------


def test_bandcamp_enricher_reruns_when_musicbrainz_links_another_page():
    other = dict(SLEEPBOMB, item_url_root="https://sleepbomb-sf.bandcamp.com",
                 genre_name="Punk", tag_names=["punk"])
    e = BandcampEnricher(search=lambda n: [SLEEPBOMB, other], tracks=lambda url: [])
    p = BandProfile("Sleepbomb")
    e.enrich(p)
    assert p.bandcamp is None               # two same-named, both local: no guess
    p.info = lookup.ArtistInfo(name="Sleepbomb",
                               links={"bandcamp": "https://sleepbomb-sf.bandcamp.com/"})
    assert e.wants_rerun(p)
    e.enrich(p)
    assert p.bandcamp is other and p.genre().label() == "punk"
    assert not e.wants_rerun(p)


def test_bandcamp_enricher_runs_in_the_book():
    seen = []
    book = BandBook([BandcampEnricher(search=lambda n: [SLEEPBOMB],
                                      tracks=lambda url: [{"title": "Forged in Steel"}])],
                    on_update=seen.append)
    try:
        p = book.get("Sleepbomb")
        import time
        for _ in range(100):
            if p.status.get("bandcamp") == "done":
                break
            time.sleep(0.01)
        assert p.bc_tracks and p.links()["bandcamp"] == "https://sleepbomb.bandcamp.com"
    finally:
        book.close()


def test_a_faraway_band_found_by_name_alone_is_only_a_guess():
    """Measured: "Inayah" at the Great American is an R&B singer; by name
    on Bandcamp, a French death metal band."""
    from twiddle.scene.app import SceneApp
    inayah = [{"name": "Inayah", "item_url_root": "https://inayah.bandcamp.com",
               "location": "Valenciennes, France", "genre_name": "Metal"}]
    g = SceneApp._guess_from(inayah)
    assert g.label() == "metal?" and not g.sure
    assert SceneApp._guess_from([SLEEPBOMB]).label() == "metal"      # local: sure


def test_same_named_pages_that_agree_still_give_a_genre():
    thelma = [{"name": "Thelma And The Sleaze", "location": "Nashville, Tennessee",
               "item_url_root": f"https://t{i}.bandcamp.com", "genre_name": "Rock"}
              for i in range(2)]
    from twiddle.scene.app import SceneApp
    assert SceneApp._guess_from(thelma).label() == "rock?"
    split = [thelma[0], dict(thelma[1], genre_name="Reggae")]
    assert SceneApp._guess_from(split) is None


def test_a_show_is_sure_if_any_band_behind_its_genre_is():
    sure, unsure = genre.guess("Metal"), genre.guess("Metal")
    unsure.sure = False
    assert genre.for_show([unsure, sure]).sure
    assert not genre.for_show([unsure]).sure


def test_a_sure_opener_does_not_vouch_for_the_headliners_guess():
    unsure_metal = genre.guess("Metal", ["doom"])
    unsure_metal.sure = False
    sure_folk = genre.guess("Folk")
    show = genre.for_show([unsure_metal, sure_folk])
    assert show.top[0] == "metal" and not show.sure
