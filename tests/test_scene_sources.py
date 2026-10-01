"""The List parser and venue matching.

The fixture is a trimmed copy of the real by-club pages from 2026-09-22 --
five venues, 74 rows -- not a hand-written imitation, so it carries The
List's real quirks: annotations inside band anchors, a "$!2.67" price, and
legend symbols trailing the row.
"""
from datetime import date
from pathlib import Path

import pytest

from twiddle.scene import cache, venues
from twiddle.scene.model import Show, dedupe
from twiddle.scene.sources import SourceError, fetch_all
from twiddle.scene.sources.thelist import infer_year, parse_page

FIXTURE = Path(__file__).parent / "fixtures" / "thelist_by_club.html"
TODAY = date(2026, 9, 22)


@pytest.fixture(scope="module")
def shows():
    return parse_page(FIXTURE.read_text(encoding="latin-1"), TODAY)


def _find(shows, venue, month, day):
    return next(s for s in shows if venue in s.venue and (s.day.month, s.day.day) == (month, day))


def test_every_row_parses(shows):
    assert len(shows) == 74
    assert all(s.bands for s in shows)


def test_bands_come_from_anchors_and_keep_their_commas_and_apostrophes(shows):
    s = _find(shows, "Stay Gold", 10, 3)
    assert s.bands == ["Phazed Out", "Kill 'Em All", "Into Dust", "Drop Step", "All Hope Lost"]
    assert _find(shows, "Ivy Room", 9, 24).bands[1] == "J Brock & The Shakedowns"


def test_an_annotation_inside_a_band_anchor_is_not_part_of_the_name(shows):
    """Measured: `<A ..>Totalna Tama (record release)</A>`. Left in, every
    lookup would search for a band called "Totalna Tama (record release)"."""
    s = _find(shows, "Mile High", 10, 24)
    assert s.bands[0] == "Totalna Tama"
    assert "Totalna Tama: record release" in s.notes
    assert any("Benefit" in n for n in s.notes)


def test_age_price_and_times_are_kept_raw(shows):
    s = _find(shows, "Ivy Room", 9, 24)
    assert (s.age, s.price, s.times) == ("21+", "$!2.67", "7pm/8pm")
    assert _find(shows, "Stay Gold", 10, 3).age == "a/a"


def test_legend_symbols_become_flags(shows):
    assert _find(shows, "Stay Gold", 10, 3).flags == ["pit warning"]


@pytest.mark.parametrize("today,month,day,expected", [
    (date(2026, 9, 22), 9, 23, date(2026, 9, 23)),
    (date(2026, 9, 22), 9, 20, date(2026, 9, 20)),    # just past: still this year
    (date(2026, 12, 20), 1, 3, date(2027, 1, 3)),     # a December list into January
    (date(2026, 9, 22), 2, 23, date(2027, 2, 23)),
])
def test_year_inference(today, month, day, expected):
    assert infer_year(month, day, today) == expected


def test_watched_venues_match_the_lists_spelling():
    v = venues.find(venues.DEFAULT_VENUES, "Thee Stork Club, Oakland")
    assert v is not None and v.name == "Stork Club"
    assert venues.find(venues.DEFAULT_VENUES, "Eli's Mile High Club, Oakland").name == "Eli's Mile High"
    assert venues.find(venues.DEFAULT_VENUES, "Hardly Strictly Bluegrass, Golden Gate Park, S.F.") is None
    assert venues.resolve(venues.DEFAULT_VENUES, "stork").name == "Stork Club"
    assert venues.resolve(venues.DEFAULT_VENUES, "eli").name == "Eli's Mile High"   # not D*eli*
    assert venues.resolve(venues.DEFAULT_VENUES, "gold").name == "Stay Gold Deli"
    assert venues.display_name(venues.DEFAULT_VENUES, "Rio Theater, Santa Cruz") == "Rio Theater"


def test_every_spelling_of_gilman_and_gamh_is_matched():
    """All measured on The List, 2026-09-23."""
    for listed in ["924 Gilman Street, Berkeley", "924 Gilman St., Berkeley",
                   "924 Gilman, Berkeley", "924 Gilman Street, S.F.",
                   "924 Gilman Street and Gilman Brewery, Berkeley"]:
        assert venues.find(venues.DEFAULT_VENUES, listed).name == "924 Gilman"
    assert venues.find(venues.DEFAULT_VENUES, "Gilman Brewing Co., 912 Gilman Street, Berkeley") is None
    for listed in ["Great American Music Hall, S.F.", "Great American Music Hall, S.f."]:
        assert venues.find(venues.DEFAULT_VENUES, listed).name == "Great American"
    assert venues.resolve(venues.DEFAULT_VENUES, "gilman").name == "924 Gilman"
    assert venues.resolve(venues.DEFAULT_VENUES, "great").name == "Great American"


def test_a_config_venue_list_replaces_the_defaults(tmp_path):
    cfg = tmp_path / "scene.toml"
    cfg.write_text('[[venue]]\nname = "Cornerstone"\nmatch = ["Cornerstone, Berkeley"]\n')
    watched = venues.watched(venues.load_config(cfg))
    assert [v.name for v in watched] == ["Cornerstone"]
    assert watched[0].matches("Cornerstone, Berkeley")


def test_dedupe_merges_the_same_night_from_two_sources():
    a = Show(date(2026, 10, 1), "Ivy Room, Albany", ["X", "Y"], source="thelist")
    b = Show(date(2026, 10, 1), "ivy room, albany", ["x"], source="venuepilot")
    c = Show(date(2026, 10, 2), "Ivy Room, Albany", ["X"], source="thelist")
    assert [s.source for s in dedupe([a, b, c])] == ["thelist", "thelist"]


def test_one_failing_source_does_not_blank_the_others():
    class Good:
        name = "good"

        def fetch(self):
            return [Show(date(2026, 10, 1), "Ivy Room, Albany", ["X"])]

    class Bad:
        name = "bad"

        def fetch(self):
            raise SourceError("down")

    shows, errors = fetch_all([Bad(), Good()])
    assert len(shows) == 1 and errors == ["bad: down"]


def test_pins_are_keyed_by_normalised_name():
    cache.pin("M.D.C.", "abc", "MDC")
    assert cache.pinned("m.d.c.")["spotify_id"] == "abc"
    cache.pin("Shape", None)
    assert cache.pinned("Shape") == {"spotify_id": None, "name": "", "at": cache.pinned("Shape")["at"]}
    cache.unpin("Shape")
    assert cache.pinned("Shape") is None


def test_yoshis_is_watched_whatever_the_apostrophe():
    assert venues.find(venues.DEFAULT_VENUES, "Yoshi's, Oakland").name == "Yoshi's"
    assert venues.find(venues.DEFAULT_VENUES, "Yoshi’s, Oakland").name == "Yoshi's"
    assert venues.resolve(venues.DEFAULT_VENUES, "yosh").name == "Yoshi's"


@pytest.mark.parametrize("listed, name", [
    ("Greek Theater, UC Berkeley Campus", "Greek Theatre"),
    ("Fox Theater, Oakland", "Fox Theater"),
    ("Paramount Theatre, Oakland", "Paramount"),
    ("Midway, S.F.", "The Midway"),
])
def test_the_big_rooms_match_the_lists_spelling(listed, name):
    assert venues.find(venues.DEFAULT_VENUES, listed).name == name


def test_the_redwood_city_fox_is_not_oaklands():
    assert venues.find(venues.DEFAULT_VENUES, "Fox Theater, 2215 Broadwa, Redwood City") is None


@pytest.mark.parametrize("query, name", [
    ("greek", "Greek Theatre"), ("fox", "Fox Theater"),
    ("paramount", "Paramount"), ("midway", "The Midway")])
def test_the_big_rooms_resolve_by_partial_name(query, name):
    assert venues.resolve(venues.DEFAULT_VENUES, query).name == name


def test_the_same_show_spelled_differently_by_two_sources_appears_once():
    a = Show(date(2026, 10, 1), "Yoshi's, Oakland", ["Spyro Gyra"], source="yoshis")
    b = Show(date(2026, 10, 1), "Yoshis Jazz Club, Oakland", ["SPYRO GYRA"], source="other")
    c = Show(date(2026, 10, 1), "Greek Theater, UC Berkeley Campus", ["M.D.C."], source="thelist")
    d = Show(date(2026, 10, 1), "Greek Theatre, Berkeley", ["MDC"], source="other")

    class Src:
        def __init__(self, name, shows):
            self.name, self.shows = name, shows

        def fetch(self):
            return self.shows

    shows, _ = fetch_all([Src("a", [a, c]), Src("b", [b, d])])
    assert [s.source for s in shows] == ["thelist", "yoshis"]


def test_unwatched_rooms_are_only_merged_on_the_same_spelling():
    # Two Fox Theaters: Oakland's is watched, Redwood City's is not.
    a = Show(date(2026, 10, 1), "Fox Theater, Oakland", ["X"], source="thelist")
    b = Show(date(2026, 10, 1), "Fox Theater, 2215 Broadwa, Redwood City", ["X"], source="thelist")
    shows, _ = fetch_all([type("S", (), {"name": "s", "fetch": lambda self: [a, b]})()])
    assert len(shows) == 2


@pytest.mark.parametrize("query, name", [
    ("uc", "UC Theatre"),           # not the Greek, whose match says "UC Berkeley"
    ("great", "Great American"), ("castro", "Castro Theatre"), ("bottom", "Bottom of the Hill"),
    ("star", "Starry Plough"), ("4 star", "4 Star"), ("indep", "Independent")])
def test_partial_names_prefer_the_venues_own_name(query, name):
    assert venues.resolve(venues.DEFAULT_VENUES, query).name == name


def test_every_default_venue_has_an_address_and_a_site():
    for v in venues.DEFAULT_VENUES:
        assert v.info.address and v.info.url.startswith("https://"), v.name
        assert v.info.map_url.startswith("https://www.google.com/maps/")


def test_a_config_venue_keeps_built_in_details_unless_it_gives_its_own():
    config = {"venue": [{"name": "Fox Theater", "match": ["fox theater, oakland"]},
                        {"name": "Ivy Room", "match": ["ivy room"], "url": "https://mine"},
                        {"name": "Someplace", "address": "1 Main St"}]}
    fox, ivy, some = venues.watched(config)
    assert fox.info.address.startswith("1807 Telegraph")
    assert ivy.info.url == "https://mine" and ivy.info.address.startswith("860 San Pablo")
    assert some.info.address == "1 Main St" and not some.info.url


def test_the_house_venue_has_a_street_but_no_number():
    v = venues.resolve(venues.DEFAULT_VENUES, "pussy")
    # A private home: the street is how its shows are described, the number isn't.
    assert v.info.address.split(",")[0] == "34th St"
    assert venues.find(venues.DEFAULT_VENUES, "Oakland.Secret, Oakland").name == "Oakland Secret"


def test_wikipedia_summaries_are_cached(monkeypatch):
    from twiddle.scene import venue_info
    calls = []

    class Resp:
        status_code = 200

        def json(self):
            return {"extract": "A hall.", "content_urls": {"desktop": {"page": "u"}}}
    monkeypatch.setattr(venue_info.requests, "get", lambda *a, **k: calls.append(a) or Resp())
    assert venue_info.wiki_summary("Fox Oakland Theatre")["extract"] == "A hall."
    assert venue_info.wiki_summary("Fox Oakland Theatre")["extract"] == "A hall."
    assert len(calls) == 1


def test_a_show_links_to_its_venues_anchor_on_the_lists_page():
    page = FIXTURE.read_text(encoding="latin-1")
    shows = parse_page(page, TODAY, "http://www.foopee.com/punk/the-list/by-club.0.html")
    stork = next(s for s in shows if "Stork" in s.venue)
    base, anchor = stork.source_url.split("#")
    assert base.endswith("by-club.0.html")
    assert f'<A NAME="{anchor}"><B>{stork.venue}</B>' in page


def test_share_text_is_a_message_a_person_can_read():
    s = Show(date(2026, 10, 3), "Greek Theater, UC Berkeley Campus", ["Jon Batiste", "Guest"],
             age="a/a", price="$66.75+", times="8pm", notes=["sold out"],
             source_url="http://www.foopee.com/punk/the-list/by-club.1.html#Greek")
    assert s.share_text("Greek Theatre", "2001 Gayley Rd, Berkeley, CA 94720").splitlines() == [
        "Jon Batiste, Guest", "Sat Oct 3, 8pm", "Greek Theatre · 2001 Gayley Rd, Berkeley",
        "a/a · $66.75+ · sold out", "http://www.foopee.com/punk/the-list/by-club.1.html#Greek"]
    # An unwatched room: the source's own spelling, which carries the city.
    assert "Greek Theater, UC Berkeley Campus" in Show(
        date(2026, 10, 3), "Greek Theater, UC Berkeley Campus", ["X"]).share_text()
