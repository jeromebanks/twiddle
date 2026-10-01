"""Yoshi's own calendar as a listings source.

Fixtures are trimmed copies of yoshis.com on 2026-09-25: the calendar page
(12 entries chosen for their quirks) and the Musiq Soulchild run's detail
page, whose nights are not consecutive.
"""
from datetime import date
from pathlib import Path

import pytest

from twiddle.scenedata import venues
from twiddle.scenespec import venue
from twiddle.scenespec.model import Show
from twiddle.scenedata.sources import SourceError, fetch_all
from twiddle.scenedata.sources import yoshis
from twiddle.scenedata.sources.yoshis import (parse_calendar, parse_performances, split_title,
                                           tidy_case, to_shows)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def events():
    return parse_calendar((FIX / "yoshis_calendar.html").read_text())


def _ev(events, title):
    return next(e for e in events if title in e["title"])


def test_add_ons_that_are_not_shows_are_dropped(events):
    # The fixture has two Twin Peaks VIP packages beside the show itself.
    assert [e["title"] for e in events if "TWIN PEAKS" in e["title"]] == [
        "TWIN PEAKS: CONVERSATION WITH THE STARS"]
    assert not any("TICKET NOT INCLUDED" in e["title"] for e in events)


def test_dates_carry_their_own_year(events):
    assert _ev(events, "ANA POPOVIC")["day"] == date(2027, 1, 13)
    assert _ev(events, "SYLEENA")["day"] == date(2026, 9, 25)
    assert _ev(events, "SYLEENA")["time"] == "7:30pm"
    assert _ev(events, "ISAIAH COLLIER")["time"] == "7pm"


def test_a_run_is_expanded_to_the_nights_its_detail_page_lists(events):
    ev = _ev(events, "MUSIQ")
    assert ev["run"]
    nights = parse_performances((FIX / "yoshis_run_detail.html").read_text())
    shows = to_shows(ev, nights)
    # 10.9 to 10.16 on the calendar, but 10.12-10.14 belong to other acts.
    assert [s.day.day for s in shows] == [9, 10, 11, 15, 16]
    assert shows[0].times == "7:30pm/9:30pm"
    assert shows[2].times == "7pm/9pm"


def test_a_run_without_its_detail_page_still_shows_its_first_night(events):
    shows = to_shows(_ev(events, "MUSIQ"), None)
    assert [s.day for s in shows] == [date(2026, 10, 9)]


def test_sold_out_is_noted(events):
    [s] = to_shows(_ev(events, "BRIAN CULBERTSON"))
    assert "sold out" in s.notes
    assert s.price == "$89-$139"


@pytest.mark.parametrize("title, bands, note, age", [
    ("FRED WESLEY'S NEW JBS W/ MARTHA HIGH'S FUNKY DIVAS",
     ["Fred Wesley's New Jbs", "Martha High's Funky Divas"], None, None),
    ("ISAIAH COLLIER: ‘COLLIER PLAYS COLTRANE’", ["Isaiah Collier"], "‘Collier Plays Coltrane’", None),
    ("THE OZ NOY TRIO feat. DAVE WECKL", ["The Oz Noy Trio", "Dave Weckl"], None, None),
    ("MILES ELECTRIC BAND FEAT KEYON HARROLD & MORE", ["Miles Electric Band", "Keyon Harrold"], None, None),
    ("HOUSE OF VELVET: HALLOWEEN (MUST BE 18+)", ["House Of Velvet"], "Halloween", "18+"),
    ("MUSIC MONDAY PRESENTS: SANG SIS!", ["Sang Sis!"], "Music Monday Presents", None),
    ("THE JAMES HUNTER SIX - OFF THE FENCE TOUR", ["The James Hunter Six"], "Off The Fence Tour", None),
    ("BONEY JAMES LIVE", ["Boney James"], None, None),
    ("SYLVIA ANDERSON FEAT DARREL WALLS AND MUSIC DIRECTOR CARL WHEELER",
     ["Sylvia Anderson", "Darrel Walls", "Carl Wheeler"], None, None),
])
def test_titles_become_bands_that_lookups_can_find(title, bands, note, age):
    got_bands, notes, got_age = split_title(title)
    assert got_bands == bands
    assert got_age == age
    if note:
        assert note in notes


def test_tidy_case_leaves_mixed_case_and_numerals_alone():
    assert tidy_case("KRS-ONE") == "Krs-One"
    assert tidy_case("CHRISTMAS IN EGYPT IV") == "Christmas In Egypt IV"
    assert tidy_case("feat. Córdoba") == "feat. Córdoba"


def test_every_show_is_matched_by_the_watched_yoshis(events):
    shows = [s for e in events for s in to_shows(e)]
    assert shows and all(venue.find(venues.DEFAULT_VENUES, s.venue).name == "Yoshi's"
                         for s in shows)
    assert all(s.source == "yoshis" for s in shows)


def test_fetch_expands_runs_and_survives_a_broken_detail_page(monkeypatch):
    pages = {yoshis.CALENDAR: (FIX / "yoshis_calendar.html").read_text()}
    musiq = "https://yoshis.com/events/buy-tickets/musiq-soulchild-46/detail"
    pages[musiq] = (FIX / "yoshis_run_detail.html").read_text()

    def get(self, url):
        if url not in pages:
            raise SourceError("404")
        return pages[url]
    monkeypatch.setattr(yoshis.Yoshis, "_get", get)
    shows = yoshis.Yoshis().fetch()
    assert [s.day.day for s in shows if s.headliner == "Musiq Soulchild"] == [9, 10, 11, 15, 16]
    # Boney James's detail page is "missing": its first night survives.
    assert [s.day for s in shows if s.headliner == "Boney James"] == [date(2026, 12, 29)]


def test_a_failed_source_keeps_its_cached_shows():
    old = Show(day=date(2026, 10, 1), venue="Ivy Room, Albany", bands=["X"], source="thelist")

    class Down:
        name = "thelist"

        def fetch(self):
            raise SourceError("foopee.com is down")

    class Up:
        name = "yoshis"

        def fetch(self):
            return [Show(day=date(2026, 10, 2), venue="Yoshi's, Oakland", bands=["Y"],
                         source="yoshis")]

    stale_yoshis = Show(day=date(2026, 9, 1), venue="Yoshi's, Oakland", bands=["Old"],
                        source="yoshis")
    shows, errors = fetch_all([Down(), Up()], stale=[old, stale_yoshis])
    assert [s.headliner for s in shows] == ["X", "Y"]     # not the stale Yoshi's
    assert errors == ["thelist: foopee.com is down"]
