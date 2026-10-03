"""Where is this room? An address and coordinates for the rooms nobody described,
from OpenStreetMap's Nominatim.

Nominatim's usage policy (https://operations.osmfoundation.org/policies/nominatim/,
read 2026-10) asks for: at most one request a second, from one thread, and far
fewer for a script that runs on a schedule (we hold ourselves to four a minute);
a User-Agent that names the application; results cached on our side, misses
included, and no identical query repeated; no autocomplete, no systematic or bulk
queries, no scraping of details pages. One search per room, by name and city, is
inside that.

The data is OpenStreetMap's, under the ODbL. The OSM Foundation's geocoding
guideline treats one geocoding result as an insubstantial extract, so storing
about a hundred of them next to our own data creates no share-alike duty, but
attribution to OpenStreetMap is required: every row filled from here carries
`attribution` and the client shows it.

A result is kept only if it plausibly *is* the room: inside the Bay Area, in the
right city, and named like it. Anything else is recorded as a miss (and not asked
again for a month), never a guess. What a person or an AI resolved always wins
over what is found here (`deadletters.known_venue_rows`).

The rate limit is `ratelimit.POLICIES["nominatim"]`, shared with every other
process; a 429 stops the whole step.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from .. import lookup, netstats, ratelimit
from . import cache
from .venue_names import Room, _same_name

URL = "https://nominatim.openstreetmap.org/search"
FILE = "geocode.json"
ATTRIBUTION = "Location © OpenStreetMap contributors (ODbL)"
HIT_TTL_S = 180 * 86400
MISS_TTL_S = 30 * 86400
MAX_PER_BUILD = 20                  # rooms asked about in one build; the rest wait for the next
BAY_AREA = (-123.6, 36.8, -121.5, 38.9)         # west, south, east, north
_MAX_WAIT_S = 20.0                  # the longest we sleep for the budget before giving up the step


class GeocodeStopped(RuntimeError):
    """Nominatim (or our budget for it) says stop: no more asks this run."""


def _fetch(url: str) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": lookup.USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            netstats.record("nominatim", status=getattr(resp, "status", 200))
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        netstats.record("nominatim", status=exc.code)
        if exc.code in (429, 403, 503):         # told to stop (403: blocked) -- do not retry
            after = float(exc.headers.get("Retry-After", 0) or 0) if exc.code == 429 else 3600.0
            ratelimit.governor("nominatim").report(429, after)
            raise GeocodeStopped(f"Nominatim answered {exc.code}") from exc
        raise


def query_url(room: Room) -> str:
    west, south, east, north = BAY_AREA
    q = ", ".join(p for p in (room.name, room.city, "California") if p)
    return URL + "?" + urllib.parse.urlencode({
        "q": q, "format": "jsonv2", "limit": 5, "addressdetails": 1, "countrycodes": "us",
        "viewbox": f"{west},{north},{east},{south}", "bounded": 1})


def _city_of(addr: dict) -> str:
    return next((addr[k] for k in ("city", "town", "village", "hamlet", "municipality", "suburb")
                 if addr.get(k)), "")


# What a venue can be on a map. A result with no street number is accepted only as one of
# these: a path, a station or a street that shares the room's name is not the room.
PLACE_CATEGORIES = {"amenity", "leisure", "tourism", "building", "shop", "historic", "craft", "office"}
NOT_A_ROOM = {"footway", "path", "cycleway", "bus_stop", "station", "platform", "residential",
              "service", "pedestrian", "bus_station", "parking", "bicycle_parking"}


def choose(room: Room, results: list) -> dict | None:
    """The result that is this room, as the fields a `known_venues` row takes, or None.

    A room listed with no city is not looked up at all (the box is the whole Bay Area, so
    "Fireside Lounge" would match whichever one is nearest the middle)."""
    if not room.city:
        return None
    for r in results or []:
        if r.get("category") == "highway" or r.get("addresstype") == "road" \
                or r.get("type") in NOT_A_ROOM:
            continue            # a street named like the room ("Ritz Court") is not the room
        if not (r.get("address") or {}).get("house_number") and r.get("category") not in PLACE_CATEGORIES:
            continue
        addr = r.get("address") or {}
        name = r.get("name") or (r.get("display_name") or "").split(",")[0]
        city = _city_of(addr)
        if not name or not _same_name(room.name, name):
            continue
        if room.city and city and room.city.lower() != city.lower() \
                and room.city.lower() not in (r.get("display_name") or "").lower():
            continue
        try:
            lat, lon = round(float(r["lat"]), 6), round(float(r["lon"]), 6)
        except (KeyError, TypeError, ValueError):
            continue
        street = " ".join(p for p in (addr.get("house_number"), addr.get("road")) if p)
        place = ", ".join(p for p in (street, city or room.city,
                                      " ".join(p for p in ("CA", addr.get("postcode")) if p)) if p)
        return {"address": place if addr.get("house_number") and addr.get("road") else "", "city": city or room.city, "state": "CA",
                "lat": lat, "lon": lon, "attribution": ATTRIBUTION,
                "osm": f"{r.get('osm_type', '')}/{r.get('osm_id', '')}",
                "matched": f"{name} ({r.get('category')}/{r.get('type')})"}
    return None


def cached(room: Room, now: float | None = None) -> tuple[bool, dict | None]:
    """(asked recently?, what was found). A hit lasts half a year, a miss a month."""
    entry = cache._read(FILE).get(room.key)
    if not entry:
        return False, None
    age = (time.time() if now is None else now) - entry.get("at", 0)
    if age < (HIT_TTL_S if entry.get("hit") else MISS_TTL_S):
        return True, entry.get("hit")
    return False, None


def known(rooms: list[Room]) -> dict[str, dict]:
    """Every cached hit, by room key: what a build publishes without asking anyone."""
    store = cache._read(FILE)
    now = time.time()
    out = {}
    for r in rooms:
        hit = store.get(r.key, {}).get("hit")
        if hit and now - store[r.key].get("at", 0) < HIT_TTL_S:
            # a street name with no number ("Ocean Street") is not an address: keep the
            # coordinates, drop the text (earlier hits were cached before this rule)
            out[r.key] = dict(hit, address=hit["address"] if hit.get("address", "")[:1].isdigit() else "")
    return out


def geocode_rooms(rooms: list[Room], *, limit: int = MAX_PER_BUILD,
                  fetch: Callable[[str], list] | None = None, sleep: Callable[[float], None] = time.sleep,
                  log: Callable[[str], None] = lambda _m: None,
                  progress: Callable[[int, int], None] = lambda _i, _n: None) -> dict[str, int]:
    """Ask Nominatim about up to `limit` rooms not asked about lately, busiest
    first. Returns counts: found, missed, left (not reached), cached.

    Stops, without raising, on a stated limit or a 429; what was found so far
    stays cached."""
    fetch = fetch or _fetch          # looked up late, so a test can replace it
    counts = {"found": 0, "missed": 0, "left": 0, "cached": 0, "no city": 0}
    todo = []
    for room in rooms:
        if not room.city:
            counts["no city"] += 1      # `choose` would reject every answer: do not spend a request
            continue
        asked, _ = cached(room)
        if asked:
            counts["cached"] += 1
        else:
            todo.append(room)
    gov = ratelimit.governor("nominatim")
    for i, room in enumerate(todo):
        if i >= limit:
            counts["left"] = len(todo) - i
            break
        progress(i, min(limit, len(todo)))      # a heartbeat: the 4-a-minute budget makes this step minutes long
        try:
            while True:
                try:
                    gov.acquire(max_block_s=_MAX_WAIT_S)
                    break
                except ratelimit.RateLimited as exc:
                    if exc.retry_after > 60:        # a lockout, not a pause: leave it to a later build
                        raise GeocodeStopped(str(exc)) from exc
                    sleep(exc.retry_after)
            hit = choose(room, fetch(query_url(room)))
        except GeocodeStopped as exc:
            log(f"geocoding stopped: {exc}")
            counts["left"] = len(todo) - i
            break
        except (OSError, ValueError) as exc:            # unreachable or garbled: ask again next build
            log(f"geocoding {room.label}: {exc}")
            counts["left"] = len(todo) - i
            break
        store = cache._read(FILE)
        store[room.key] = {"at": time.time(), "query": room.label, "hit": hit}
        cache._write(FILE, store)
        counts["found" if hit else "missed"] += 1
    return counts
