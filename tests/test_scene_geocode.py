"""Finding a room on OpenStreetMap's Nominatim: only plausible matches, cached, rate-limited, never in a test."""
import time

import pytest

from twiddle import ratelimit
from twiddle.scenedata import deadletters as dl, geocode
from twiddle.scenedata.venue_names import cluster

ROOM = cluster({"Hopmonk, Novato": 3})[0]


def result(name="Hopmonk Tavern", city="Novato", lat="38.11", lon="-122.57", **kw):
    return {"name": name, "lat": lat, "lon": lon, "osm_type": "node", "osm_id": 42,
            "display_name": f"{name}, {city}, Marin County, California, United States",
            "address": {"house_number": "224", "road": "Vintage Way", "city": city, "postcode": "94945"}, **kw}


def test_a_result_that_is_the_room_becomes_an_address_and_coordinates_with_the_osm_credit():
    hit = geocode.choose(ROOM, [result()])
    assert hit["address"] == "224 Vintage Way, Novato, CA 94945" and hit["lat"] == 38.11
    assert hit["attribution"] == geocode.ATTRIBUTION and hit["osm"] == "node/42"


def test_a_result_that_is_not_the_room_is_a_miss_never_a_guess():
    assert geocode.choose(ROOM, [result(name="Safeway")]) is None                 # another place
    assert geocode.choose(ROOM, [result(city="Petaluma", display_name="Hopmonk, Petaluma")]) is None
    assert geocode.choose(ROOM, [result(lat="x")]) is None and geocode.choose(ROOM, []) is None


def test_a_street_named_like_the_room_is_not_the_room():
    # measured: "Ritz, San Jose" matched Ritz Court, a road in east San Jose
    ritz = cluster({"Ritz, San Jose": 1})[0]
    road = result(name="Ritz Court", city="San Jose", category="highway", addresstype="road")
    assert geocode.choose(ritz, [road]) is None
    assert geocode.choose(ritz, [road, result(name="The Ritz", city="San Jose", category="amenity")])["state"] == "CA"


def test_the_query_names_the_room_stays_in_the_bay_area_and_asks_for_few():
    url = geocode.query_url(ROOM)
    assert "q=Hopmonk%2C+Novato%2C+California" in url and "bounded=1" in url and "limit=5" in url


def test_each_room_is_asked_once_a_miss_is_remembered_and_the_budget_is_four_a_minute():
    calls = []
    sleeps = []
    rooms = cluster({"Hopmonk, Novato": 3, "Ritz, San Jose": 2, "Catalyst, Santa Cruz": 1})

    def fetch(url):
        calls.append(url)
        return [result()] if "Hopmonk" in url else []
    n = geocode.geocode_rooms(rooms, fetch=fetch, sleep=sleeps.append)
    assert n["found"] == 1 and n["missed"] == 2 and len(calls) == 3
    again = geocode.geocode_rooms(rooms, fetch=fetch, sleep=sleeps.append)      # nothing new to ask
    assert again["cached"] == 3 and len(calls) == 3
    assert geocode.known(rooms) == {ROOM.key: geocode.known(rooms)[ROOM.key]}   # only the hit is published


def test_a_build_asks_at_most_limit_rooms_and_says_how_many_are_left():
    rooms = cluster({f"Room {c}, Napa": 1 for c in "ABCDE"})
    n = geocode.geocode_rooms(rooms, limit=2, fetch=lambda u: [], sleep=lambda s: None)
    assert (n["missed"], n["left"]) == (2, 3)


def test_a_stated_limit_stops_the_step_and_the_rest_wait(monkeypatch):
    rooms = cluster({"A Room, Napa": 2, "B Room, Napa": 1})

    def fetch(url):
        raise geocode.GeocodeStopped("Nominatim answered 429")
    n = geocode.geocode_rooms(rooms, fetch=fetch, sleep=lambda s: None)
    assert n["found"] == n["missed"] == 0 and n["left"] == 2


def test_a_lockout_longer_than_a_pause_ends_the_step_without_asking():
    ratelimit.governor("nominatim").report(429, 3600)
    called = []
    n = geocode.geocode_rooms(cluster({"A Room, Napa": 1}), fetch=lambda u: called.append(u) or [],
                              sleep=lambda s: None)
    assert not called and n["left"] == 1


def test_a_map_result_fills_the_row_and_a_person_beats_it(tmp_path):
    from datetime import date
    from twiddle.scenespec.model import Show
    shows = [Show(date(2026, 10, 6), "Hopmonk, Novato", ["X"], source="thelist")]
    path = tmp_path / "dataset.json"
    dl.sync(dl.venue_letters(shows, ()), path)
    geocode.geocode_rooms(cluster({"Hopmonk, Novato": 1}), fetch=lambda u: [result()], sleep=lambda s: None)
    (row,) = dl.known_venue_rows(shows, (), path)
    assert row["address"].startswith("224 Vintage Way") and row["lat"] == 38.11 and row["attribution"]
    dl.resolve("venue:hopmonk-novato", {"address": "1 Real St, Novato", "lat": 1.0, "lon": 2.0}, "human", path)
    (row,) = dl.known_venue_rows(shows, (), path)
    assert row["address"] == "1 Real St, Novato" and row["lat"] == 1.0
    assert "attribution" not in row                                  # nothing of OSM's is left in the row


def test_nominatim_has_its_own_policy_and_its_host_is_counted_under_it():
    from twiddle import netstats
    p = ratelimit.POLICIES["nominatim"]
    assert {(l.count, l.per_s) for l in p.limits} == {(1, 1), (4, 60)} and "published" in p.source
    assert netstats.service_for("https://nominatim.openstreetmap.org/search?q=x") == "nominatim"
