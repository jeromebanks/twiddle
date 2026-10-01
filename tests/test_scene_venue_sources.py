"""The Stork Club's and Gilman's own listings, and merging them into The List's.

Fixtures are trimmed copies of the real pages on 2026-09-26: two pages of
theestorkclub.com/calendar/'s list view (page 4 holds the Halloween nights
and a drag show), and Gilman's public ShowSlinger widget.
"""
from datetime import date
from pathlib import Path

import pytest

from twiddle.scene.model import Show, dedupe
from twiddle.scenedata.sources import SourceError, fetch_all
from twiddle.scenedata.sources import seetickets
from twiddle.scenedata.sources.gilman import parse_widget, split_title
from twiddle.scenedata.sources.seetickets import SeeTickets, parse_page

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 26)
STORK = "Thee Stork Club, Oakland"


@pytest.fixture(scope="module")
def stork():
    return (parse_page((FIX / "storkclub_list_page1.html").read_text(), TODAY)
            + parse_page((FIX / "storkclub_list_page4.html").read_text(), TODAY))


@pytest.fixture(scope="module")
def gilman():
    return parse_widget((FIX / "gilman_showslinger.html").read_text())


def _on(shows, d):
    return [s for s in shows if s.day == d]


# ---- Stork Club ---------------------------------------------------------------


def test_stork_bands_come_from_headliners_not_the_truncated_title(stork):
    s = _on(stork, date(2026, 10, 1))[0]
    assert s.bands == ["M. Sayyid", "Jel", "Heavy Arts Ensemble", "mars kumari"]
    assert (s.age, s.price, s.times) == ("21+", "$15-$20", "8pm")
    assert s.flyer.startswith("https://prod-images.seetickets.us/")
    assert s.tickets.startswith("https://wl.seetickets.us/event/") and s.source == "storkclub"


def test_stork_splits_on_commas_only_and_drops_a_secret_guest(stork):
    assert _on(stork, date(2026, 10, 6))[0].bands == ["Kelly McFarling", "Desiree Cannon"]
    assert seetickets._bands("The Grease Traps, Tori Roze and the Hot Mess") == [
        "The Grease Traps", "Tori Roze and the Hot Mess"]
    assert _on(stork, date(2026, 10, 4))[0].bands[0] == "Shearling"     # "Shearling ,"


def test_a_night_with_no_bands_keeps_its_name_without_the_date(stork):
    s = _on(stork, date(2026, 10, 5))[0]
    assert s.bands == [] and s.title == "Freakyoke" and s.price == "free"
    assert s.billing == "Freakyoke" and "Karaoke" in s.notes
    hell = _on(stork, date(2026, 10, 30))[0]
    assert hell.title == "HELL COMES TO OAKLAND IV"      # "- 10/30 & 10/31" stripped


def test_the_presenter_and_subtitle_become_notes(stork):
    drag = _on(stork, date(2026, 10, 29))[0]
    assert drag.title == "The Vampire Lestat drag show"
    assert drag.notes[:2] == ["Theatre des Vampires presents",
                              "w/ live music from Pretty Frankenstein"]


def test_the_year_comes_from_the_weekday():
    assert seetickets._day("Sat Sep 26", TODAY) == date(2026, 9, 26)
    # January next year: only 2027 puts Jan 2 on a Saturday
    assert seetickets._day("Sat Jan 2", date(2026, 12, 20)) == date(2027, 1, 2)
    assert seetickets._day("Blah", TODAY) is None


def test_private_hires_are_dropped_and_a_double_listing_is_kept_once():
    block = ('<div class="mdc-card seetickets-list-event-container">'
             '<p class="fs-18 bold mb-12 title"><a href="https://t/{u}">{t}</a></p>'
             '<p class="fs-18 bold mt-1r date">Sat Oct {d}</p>'
             '<p class="fs-12 genre">{g}</p></div>')
    page = "".join(block.format(u=i, t=t, d=d, g="DJ/Dance") for i, (t, d) in enumerate([
        ("Closed for Private Event 10/17", 17),
        ("HOT GOTH NIGHT: HALLOWEEN", 24), ("Hot Goth Halloween", 24)]))
    shows = seetickets._once(parse_page(page, TODAY))
    assert [s.title for s in shows] == ["HOT GOTH NIGHT: HALLOWEEN"]


def test_stork_reads_pages_until_one_is_empty_and_a_failed_page_fails_all(monkeypatch):
    pages = {1: (FIX / "storkclub_list_page1.html").read_text(),
             2: (FIX / "storkclub_list_page4.html").read_text(), 3: "<html></html>"}
    src = SeeTickets(today=TODAY)
    monkeypatch.setattr(src, "_get", lambda n: pages[n])
    assert len(src.fetch()) == 16

    def boom(n):
        if n == 2:
            raise SourceError("down")
        return pages[n]
    monkeypatch.setattr(src, "_get", boom)
    with pytest.raises(SourceError):     # not page 1 alone: the cache stands in whole
        src.fetch()


def test_stork_stops_if_served_page_one_again(monkeypatch):
    page = (FIX / "storkclub_list_page1.html").read_text()
    src = SeeTickets(today=TODAY)
    calls = []
    monkeypatch.setattr(src, "_get", lambda n: calls.append(n) or page)
    assert len(src.fetch()) == 8 and calls == [1, 2]


# ---- Gilman -------------------------------------------------------------------


def test_gilman_flyer_is_full_size_and_price_is_not_recorded(gilman):
    s = _on(gilman, date(2026, 10, 2))[0]
    assert s.bands == ["COUNTERPARTS"] and s.times == "6:30pm" and s.price is None
    assert "/thumb_" not in s.flyer and s.flyer.endswith("oct2ssheader(2).jpg")
    assert s.tickets == "https://app.showslinger.com/v/counterparts"


def test_a_gilman_weekend_is_one_show_a_night_with_no_lineup(gilman):
    nights = [s for s in gilman if "Weekend" in s.title]
    assert [(s.day, s.times) for s in nights] == [(date(2026, 9, 26), "7:30pm"),
                                                  (date(2026, 9, 27), "6:30pm")]
    assert all(s.bands == [] for s in nights)


def test_gilman_titles():
    assert split_title("Worst Party Ever + Equipment + Ogbert The Nerd + Flight Patterns")[0] \
        == ["Worst Party Ever", "Equipment", "Ogbert The Nerd", "Flight Patterns"]
    assert split_title("Salt +, Splendor, and more!")[0] == ["Salt +", "Splendor"]
    assert split_title("Doll Fest Presents: We Are Actually Funny (Benefit)") == (
        ["We Are Actually Funny"], ["Benefit", "Doll Fest Presents"])
    assert split_title("Burndown Tour 2026: Febuary, Love Letter, Stella & more") == (
        ["Febuary", "Love Letter", "Stella"], ["Burndown Tour 2026"])
    assert split_title("Hardcore For Pits West Benefit") == ([], [])
    assert split_title("Ho99o9 & N8NOFACE - Sincerely___ The Void") == (
        ["Ho99o9", "N8NOFACE"], ["Sincerely___ The Void"])


# ---- merging ------------------------------------------------------------------


def _list(d, bands, **kw):
    return Show(d, STORK, bands, age="21+", price="$12/$15", source="thelist",
                source_url="http://foopee/#stork", **kw)


def _site(d, bands, title="", **kw):
    return Show(d, STORK, bands, price="$12-$15", source="storkclub", title=title,
                flyer=f"https://img/{d.day}", tickets=f"https://tix/{d.day}", **kw)


def test_the_list_keeps_its_facts_and_gains_the_flyer():
    d = date(2026, 10, 23)
    [s] = dedupe([_list(d, ["Well", "Pink Fuzz"]), _site(d, ["The Well", "Pink Fuzz"])])
    assert s.source == "thelist" and s.also == ["storkclub"]
    assert (s.bands, s.price) == (["Well", "Pink Fuzz"], "$12/$15")
    assert (s.flyer, s.tickets) == ("https://img/23", "https://tix/23")


def test_billed_in_another_order_is_still_one_show():
    d = date(2026, 11, 11)
    shows = dedupe([_list(d, ["Glass Egg", "Sachi's Mirror"]),
                    _site(d, ["Sachi's Mirror", "glass egg"])])
    assert len(shows) == 1 and shows[0].flyer


def test_one_show_each_side_under_two_names_is_one_show():
    d = date(2026, 10, 30)
    shows = dedupe([_list(d, ["Halloween tribute band Extravaganza"]),
                    _site(d, [], title="HELL COMES TO OAKLAND IV")])
    assert len(shows) == 1 and shows[0].flyer == "https://img/30"


def test_two_shows_one_night_are_not_merged_by_guesswork():
    """Gilman, 10/3: a members' meeting and a gig on The List, one gig on
    ShowSlinger. The gig matches by band; the meeting must stay alone."""
    d = date(2026, 10, 3)
    g = "924 Gilman, Berkeley"
    shows = dedupe([Show(d, g, ["Membership Meeting"], source="thelist"),
                    Show(d, g, ["Salt +", "Splendor", "Plummet"], source="thelist"),
                    Show(d, g, ["Salt +", "Splendor"], source="gilman", flyer="f")])
    assert [(s.headliner, bool(s.flyer)) for s in shows] == [
        ("Membership Meeting", False), ("Salt +", True)]
    # ... and an unrelated night beside two List shows stays its own row
    shows = dedupe([Show(d, g, ["A"], source="thelist"), Show(d, g, ["B"], source="thelist"),
                    Show(d, g, [], title="Punk Yoga", source="gilman")])
    assert len(shows) == 3


def test_a_merged_show_survives_its_second_source_failing(tmp_path):
    """The flyer lives on The List's row; if the Stork site is down, the
    cached row brings it back instead of the refresh dropping it."""
    d = date(2026, 10, 23)
    [cached] = dedupe([_list(d, ["Well"]), _site(d, ["The Well"])])

    class List:
        name = "thelist"

        def fetch(self):
            return [_list(d, ["Well"])]

    class Down:
        name = "storkclub"

        def fetch(self):
            raise SourceError("down")

    shows, errors = fetch_all([List(), Down()], stale=[cached])
    assert errors == ["storkclub: down"]
    assert len(shows) == 1 and shows[0].flyer == "https://img/23"


def test_real_stork_and_gilman_nights_merge_with_the_list(stork, gilman):
    """The measured cases, from the real pages, against The List's spelling."""
    listed = [_list(date(2026, 10, 1), ["M.sayyid", "Jel", "Heavy Arts Ensemble"]),
              _list(date(2026, 10, 30), ["Halloween tribute band Extravaganza"]),
              Show(date(2026, 9, 27), "924 Gilman Street, Berkeley",
                   ["Victims Family", "Toys That Kill"], source="thelist"),
              Show(date(2026, 10, 24), "924 Gilman Street, Berkeley",
                   ["Ho9909. N8Noface", "Slay Squad"], source="thelist")]

    def room(v):
        return "Gilman" if "gilman" in v.lower() else "Stork"
    merged = dedupe(listed + stork + gilman, room)
    for s in listed:
        m = next(x for x in merged if x.day == s.day and x.source == "thelist")
        assert m.flyer and m.bands == s.bands, s
    assert len([x for x in merged if x.day == date(2026, 10, 1)]) == 1


# ---- the record ---------------------------------------------------------------


def test_share_text_carries_the_tickets_and_flyer():
    s = Show(date(2026, 10, 5), STORK, [], title="Freakyoke", price="free", times="8pm",
             source_url="https://tix/5", tickets="https://tix/5", flyer="https://img/5")
    assert s.share_text().splitlines() == [
        "Freakyoke", "Mon Oct 5, 8pm", STORK, "free", "https://tix/5", "https://img/5"]


def test_a_parser_crash_is_that_sources_error_not_everyones():
    """The builder would quit on an uncaught exception."""
    d = date(2026, 10, 23)

    class Broken:
        name = "storkclub"

        def fetch(self):
            raise ValueError("time data 'Octo 2' does not match")

    class List:
        name = "thelist"

        def fetch(self):
            return [_list(d, ["Well"])]

    shows, errors = fetch_all([List(), Broken()], stale=[_site(d, ["The Well"])])
    assert len(shows) == 1 and shows[0].flyer            # stale fallback still applied
    assert errors[0].startswith("storkclub: could not read it (ValueError")


def test_a_bandless_night_whose_name_was_only_a_date_does_not_crash():
    assert seetickets._once([Show(date(2026, 10, 5), STORK, [], title="")])


# ---- Ivy Room -----------------------------------------------------------------

import json  # noqa: E402

from twiddle.scenedata.sources import venuepilot  # noqa: E402


@pytest.fixture(scope="module")
def ivy():
    """venuepilot.co/graphql's answer for account 992, 7 events kept for their quirks."""
    data = json.loads((FIX / "ivyroom_venuepilot.json").read_text())
    return [s for s in map(venuepilot.to_show, data["data"]["paginatedEvents"]["collection"]) if s]


def test_ivy_bands_come_from_the_name_not_the_copied_artists(ivy):
    # Oct 4's `artists` were Oct 3's (Riki, Houses of Heaven ...)
    s = _on(ivy, date(2026, 10, 4))[0]
    assert s.bands[:2] == ["Street Walking Cheetahs", "SF Rejects"]
    assert "Riki" not in s.bands
    rr = _on(ivy, date(2026, 10, 3))[0]
    assert rr.bands[0] == "BESTIAL MOUTHS" and "record release" in rr.notes


def test_ivy_reads_price_times_genre_flyer_and_tickets(ivy):
    s = _on(ivy, date(2026, 9, 26))[0]
    assert (s.price, s.times) == ("$18", "7:30pm/8pm")
    assert s.notes == ["gothic doom, black metal ,metal"]
    assert s.flyer.startswith("https://s3.amazonaws.com/files.venuepilot.com/attachments/cover_")
    assert s.tickets.startswith("https://tickets.venuepilot.com/")
    assert "sold out" in _on(ivy, date(2026, 10, 2))[0].notes


def test_ivy_weekly_nights_are_kept_by_name_and_private_parties_dropped(ivy):
    hh = _on(ivy, date(2026, 9, 28))[0]
    assert hh.bands == [] and hh.title.startswith("Happy Hour") and hh.price == "free"
    assert _on(ivy, date(2026, 10, 19))[0].bands == []            # line dancing
    assert _on(ivy, date(2026, 11, 13)) == []                     # PRIVATE PARTY


def test_ivy_pages_until_total_pages_and_a_refused_query_is_an_error(monkeypatch):
    data = json.loads((FIX / "ivyroom_venuepilot.json").read_text())["data"]["paginatedEvents"]
    src = venuepilot.VenuePilot(today=TODAY)
    calls = []
    monkeypatch.setattr(src, "_page", lambda n: calls.append(n) or dict(
        data, metadata={"totalPages": 2}))
    assert len(src.fetch()) == 12 and calls == [1, 2]

    class Resp:
        status_code = 200

        def json(self):
            return {"errors": [{"message": "Field 'x' doesn't exist"}]}
    monkeypatch.setattr(venuepilot.requests, "post", lambda *a, **k: Resp())
    with pytest.raises(SourceError):
        venuepilot.VenuePilot(today=TODAY).fetch()


def test_see_tickets_serves_several_venues_with_the_same_parser():
    page = (FIX / "storkclub_list_page1.html").read_text()
    [s, *_] = parse_page(page, TODAY, "Chapel, S.F.", "chapel")
    assert (s.venue, s.source) == ("Chapel, S.F.", "chapel")
    assert set(seetickets.VENUES) >= {"storkclub", "gamh", "chapel", "hotelutah", "rickshaw"}
    assert seetickets._bands("with Stardog Champions, Mind's Eye") == [
        "Stardog Champions", "Mind's Eye"]
    assert seetickets._age("All Ages") == "a/a" and seetickets._age("21+") == "21+"


def test_a_joint_billing_matches_its_halves():
    d = date(2026, 9, 26)
    c = "Crybaby, Oakland"
    shows = dedupe([Show(d, c, ["Nef The Pharaoh", "D-Lo", "dj LB"], source="thelist"),
                    Show(d, c, ["Nef the Pharaoh & D-Lo"], source="crybaby", flyer="f"),
                    Show(d, c, [], title="Crash Out", source="crybaby")])
    assert [(s.billing[:15], bool(s.flyer)) for s in shows] == [
        ("Nef The Pharaoh", True), ("Crash Out", False)]


# ---- TicketWeb ----------------------------------------------------------------

from twiddle.scenedata.sources import ticketweb  # noqa: E402


@pytest.fixture(scope="module")
def tw_pages():
    return json.loads((FIX / "ticketweb_themes.json").read_text())


def test_ticketweb_reads_each_themes_date(tw_pages):
    got = {n: ticketweb.parse_page(page, TODAY, ticketweb.VENUES[n][1], n)
           for n, page in tw_pages.items()}
    first = {n: shows[0] for n, shows in got.items()}
    assert first["independent"].day == date(2026, 9, 26)       # "Sat" + "9.26"
    assert first["crybaby"].day == date(2026, 9, 26)           # "Sat, Sep 26"
    assert first["bimbos"].day == date(2026, 9, 28)            # "September" "28" "Mon"
    assert first["augusthall"].day == date(2026, 9, 29)        # "September 29, 2026"
    assert all(s.flyer.startswith("https://") for shows in got.values() for s in shows)


def test_ticketweb_fields(tw_pages):
    [bm, *_] = ticketweb.parse_page(tw_pages["brickandmortar"], TODAY,
                                    "Brick & Mortar Music Hall, S.F.", "brickandmortar")
    assert (bm.bands, bm.age, bm.price, bm.times) == (
        ["Max Fry", "aWannabe"], "a/a", "$35.72", "8pm/9pm")
    assert bm.notes == ["Goldenvoice Presents"] and "ticketweb.com" in bm.tickets
    [indie, *_] = ticketweb.parse_page(tw_pages["independent"], TODAY, "Independent, S.F.",
                                       "independent")
    assert indie.bands == ["West 22nd", "Where’s West?"] and "sold out" in indie.notes


def test_ticketweb_names():
    assert ticketweb._split("Amor De Dios/ Forced To Suffer/ exutoire") == (
        ["Amor De Dios", "Forced To Suffer", "exutoire"], [])
    assert ticketweb._split("Nef the Pharaoh & D-Lo – Burn The City Tour") == (
        ["Nef the Pharaoh & D-Lo"], ["Burn The City Tour"])
    assert ticketweb._split("Quebradita Darks Oakland w/ Sizzle + Lizzy")[0] == [
        "Quebradita Darks Oakland", "Sizzle", "Lizzy"]


# ---- 2026-09-26 additions: Ashkenaz, Sound Room, DeLuxe, Gray Area, Make-Out Room

from twiddle.scenedata.sources import grayarea, makeoutroom, simplecal, squarespace  # noqa: E402


def test_venuepilot_serves_ashkenaz_too():
    assert venuepilot.VENUES["ashkenaz"][0] == 1228
    ev = {"name": "Mark St. Mary", "date": "2026-10-03", "artists": [{"name": "Mark St. Mary"}],
          "support": "genre - Cajun/Zydeco", "description": "$20 adv / $25 door",
          "announceImages": [], "doorTime": "19:00:00", "startTime": "19:30:00"}
    s = venuepilot.to_show(ev, "ashkenaz")
    assert (s.venue, s.source, s.price, s.notes) == (
        "Ashkenaz, Berkeley", "ashkenaz", "$20/$25", ["Cajun/Zydeco"])


def test_squarespace_events_collection():
    data = json.loads((FIX / "squarespace_soundroom.json").read_text())
    shows = [squarespace.to_show(ev, "soundroom") for ev in data["upcoming"]]
    first = shows[0]
    assert first.bands == ["Saúl Sierra", "Cascada de Flores"]
    assert (first.day, first.times, first.venue) == (date(2026, 9, 26), "7:30pm", "Sound Room, Oakland")
    assert first.flyer.startswith("https://images.squarespace-cdn.com/")
    assert first.tickets == "https://www.soundroom.org/events/sal-sierra-with-cascada-de-flores"
    assert "$25" in first.price
    slam = shows[1]
    assert slam.bands == [] and slam.title == "StorySlam Oakland"


def test_simple_calendar_titles_carry_the_price():
    assert simplecal.split_title("My Dog Jack (Hardly Strictly after party) $20") == (
        ["My Dog Jack"], ["Hardly Strictly after party"], "$20")
    assert simplecal.split_title("Hammond Cheese Combo w/ Sax Gordon $10")[0] == [
        "Hammond Cheese Combo", "Sax Gordon"]
    assert simplecal.split_title("Patrick Wolff Bebop Truth Quartet") == (
        ["Patrick Wolff Bebop Truth Quartet"], [], None)


def test_simple_calendar_page_drops_past_nights():
    page = (FIX / "simplecal_deluxe.html").read_text()
    shows = simplecal.parse_page(page, TODAY, "The DeLuxe, S.F.", "deluxe", "u")
    assert shows and min(s.day for s in shows) == TODAY           # Thu/Fri before are gone
    tonight = [s for s in shows if s.day == TODAY]
    assert [(s.headliner, s.price, s.times) for s in tonight] == [
        ("Emmett Van Leer", "$10", "6pm"), ("Mitch Polzak & the Royal Deuces", "$15", "9:30pm")]


def test_gray_area_keeps_shows_by_name_and_drops_courses():
    shows = grayarea.parse_page((FIX / "grayarea_events.html").read_text(), TODAY)
    names = [s.title for s in shows]
    assert "COCOON An Orchestra Sleepover Concert" in names
    assert not any("Course" in n or "MadMapper" in n for n in names)
    assert all(s.bands == [] and s.flyer.startswith("https://grayarea.org/") for s in shows)
    cocoon = next(s for s in shows if s.title.startswith("COCOON"))
    assert cocoon.notes == ["Magik*Magik Orchestra Presents"] and cocoon.day == TODAY


def test_make_out_room_lines():
    bands, title, notes, price, end = makeoutroom.split_line(
        "Talent Moat presents: TWISTED TEENS (New Orleans) + FORTY DROP FEW (Portland) "
        "+ DJ FOODCOURT ~ Doors at 6:30pm, $20adv/ $25 door ~ 7:00pm - 9:30pm")
    assert bands == ["TWISTED TEENS", "FORTY DROP FEW", "DJ FOODCOURT"]
    assert (price, end, notes[0]) == ("$20/$25", "9:30pm", "Talent Moat presents")
    assert "TWISTED TEENS: New Orleans" in notes
    bands, title, _, price, _ = makeoutroom.split_line(
        "BOOM! ~ Friday Night Dance Party ~ DJ 2shy-shy ~ $10 ~ 10:00pm - 2:00am")
    assert (bands, title, price) == ([], "BOOM!", "$10")
    assert makeoutroom.split_line("DIMENSIONS w/ DJ 2NITE ~ vibes ~ FREE ~ 10:00pm - 2:00am")[3] \
        == "free"


def test_make_out_room_month_and_blog_flyers():
    shows = makeoutroom.parse_month((FIX / "makeoutroom_calwiz.html").read_text(),
                                    date(2026, 10, 1))
    langford = next(s for s in shows if s.day == date(2026, 10, 3) and s.bands)
    assert langford.bands[-1] == "KELLY HOGAN" and langford.times == "6:30pm til 9:30pm"
    flyers = makeoutroom.parse_flyers((FIX / "makeoutroom_blog.html").read_text())
    assert date(2026, 9, 27) in flyers and all(u.startswith("https://www.makeoutroom.com/")
                                               for u, _ in flyers[date(2026, 9, 27)])
    night = [Show(date(2026, 9, 27), "Make-Out Room, S.F.", ["DadCo", "REWINDER"]),
             Show(date(2026, 9, 27), "Make-Out Room, S.F.", [], title="DIMENSIONS w/ DJ 2NITE"),
             Show(date(2026, 9, 27), "Make-Out Room, S.F.", [], title="Unrelated Night")]
    makeoutroom.attach_flyers(night, flyers)
    assert "dad-co" in night[0].flyer and "dimensions" in night[1].flyer and not night[2].flyer


# ---- KALX's weekly calendar --------------------------------------------------------------

def _kalx_posts():
    import json
    return json.loads((FIX / "kalx_events.json").read_text())


def test_kalx_reads_the_weekly_posts_into_shows():
    from twiddle.scenedata.sources import kalx
    shows = kalx.parse_posts(_kalx_posts(), date(2026, 9, 29))
    assert shows and all(s.source == "kalx" and s.source_url.startswith("https://kalx.") for s in shows)
    assert min(s.day for s in shows) == date(2026, 9, 29)         # nothing before today
    fox = next(s for s in shows if s.day == date(2026, 9, 29) and s.venue.startswith("Fox"))
    assert fox.venue == "Fox Theater, Oakland"
    assert fox.bands == ["Social Distortion", "Descendents", "The Chats"]
    assert fox.price is None and fox.flyer == "" and fox.tickets == ""   # KALX has none


def test_kalx_rooms_resolve_to_watched_venues_including_the_two_new_ones():
    from twiddle.scene import venues as v
    from twiddle.scenedata.sources import kalx
    watched = v.watched()
    shows = kalx.parse_posts(_kalx_posts(), date(2026, 9, 22))
    by = {s.venue: v.find(watched, s.venue) for s in shows}
    for spelled, want in (("Thee Stork Club, East Bay", "Stork Club"),
                          ("Cornerstone, Berkeley", "Cornerstone"),
                          ("The Freight, Berkeley", "Freight"),
                          ("Regency Ballroom, S.F.", "Regency"),
                          ("Paramount Theatre, Oakland", "Paramount"),
                          ("Hillside Club, Berkeley", "Hillside Club"),
                          ("Sweetwater Music Hall, Mill Valley", "Sweetwater"),
                          ("Bill Graham Civic Auditorium, S.F.", "Civic"),
                          ("Eli’s Mile High Club, East Bay", "Eli's Mile High"),
                          ("Yoshi’s, East Bay", "Yoshi's"), ("Cafe Du Nord, S.F.", "Cafe du Nord")):
        assert spelled in by, spelled
        assert by[spelled] is not None and by[spelled].name == want, spelled
    assert "Hillside Club, Berkeley" in by and "Sweetwater Music Hall, Mill Valley" in by
    assert v.resolve(watched, "sweetwater").info.address.startswith("19 Corte Madera")
    assert v.resolve(watched, "hillside").info.url == "https://www.hillsideclub.org/"


def test_kalx_tells_nights_from_lineups():
    from twiddle.scenedata.sources.kalx import lineup
    assert lineup("Thelma And The Sleaze, Hypnotic Pattern") == \
        (["Thelma And The Sleaze", "Hypnotic Pattern"], "")
    assert lineup("Open Mic") == ([], "Open Mic")
    assert lineup("Karaokiki") == ([], "Karaokiki")
    assert lineup("Irish Céili (“KAY-LEE”) Dance with live band")[0] == []
    assert lineup("Monday Night Hubba: Damsels & Dragons") == \
        ([], "Monday Night Hubba: Damsels & Dragons")
    assert lineup("KALW’s 85th Birthday: Rozzi, DEATHX_XHEAD") == \
        (["Rozzi", "DEATHX_XHEAD"], "KALW’s 85th Birthday")
    assert lineup("Lukas Nelson & Friends, featuring Grahame Lesh, Holly Bowling") == \
        (["Lukas Nelson & Friends", "Grahame Lesh", "Holly Bowling"], "")
    assert lineup("Fred Wesley’s New JBs")[0] == ["Fred Wesley’s New JBs"]


def test_kalx_merges_under_the_list_and_adds_what_it_lacks():
    from twiddle.scenedata.sources import kalx
    ivy = Show(date(2026, 9, 29), "Ivy Room, Albany", ["Thelma And The Sleaze", "Hypnotic Pattern"],
               price="$15", source="thelist")
    shows = [s for s in kalx.parse_posts(_kalx_posts(), date(2026, 9, 29))
             if s.day == date(2026, 9, 29)]
    merged = dedupe([ivy] + shows, room=lambda name: "Ivy Room" if "ivy room" in name.lower() else name)
    [night] = [s for s in merged if "ivy" in s.venue.lower()]
    assert night.source == "thelist" and night.price == "$15" and night.also == ["kalx"]
    assert night.bands == ivy.bands                    # The List's lineup stands


def test_kalx_says_so_when_the_page_changes_or_the_site_is_down(monkeypatch):
    from twiddle.scenedata.sources import kalx
    import pytest
    with pytest.raises(SourceError, match="no day headings"):
        kalx.parse_posts([{"date": "2026-09-24", "link": "u", "content": {"rendered": "<p>x</p>"}}],
                         date(2026, 9, 29))

    class Resp:
        status_code = 503
    monkeypatch.setattr(kalx.requests, "get", lambda *a, **k: Resp())
    with pytest.raises(SourceError, match="HTTP 503"):
        kalx.KALX().fetch()

    def boom(*a, **k):
        raise kalx.requests.ConnectionError("down")
    monkeypatch.setattr(kalx.requests, "get", boom)
    with pytest.raises(SourceError, match="could not reach"):
        kalx.KALX().fetch()


def test_kalx_past_weeks_only_is_not_an_error():
    from twiddle.scenedata.sources import kalx
    assert kalx.parse_posts(_kalx_posts(), date(2027, 3, 1)) == []


def test_kalx_is_registered_last():
    from twiddle.scenedata.sources import base
    assert list(base._registry())[-1] == "kalx"


def test_kalx_keeps_the_last_section_of_a_post_that_leaves_its_paragraph_unclosed():
    """Codex: both posts end `...</strong> x</div>` with no </p>; the regex used to drop them."""
    from twiddle.scenedata.sources import kalx
    posts = _kalx_posts()
    assert not posts[0]["content"]["rendered"].rstrip().endswith("</p>")     # the fixture is real
    shows = kalx.parse_posts(posts, date(2026, 9, 29))
    last = [s for s in shows if s.day == date(2026, 10, 4)]
    venues = {s.venue for s in last}
    assert {"The Chapel, S.F.", "The Independent, S.F.", "Rickshaw Stop, S.F.",
            "SF Jazz Center, S.F."} <= venues                 # all in the unclosed paragraph
    assert any("Rickshaw" in s.venue and s.bands == ["Ecca Vandal", "Speed of Light"] for s in shows)
    assert any("SF Jazz" in s.venue for s in shows)
