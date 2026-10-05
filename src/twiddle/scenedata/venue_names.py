"""Reading a venue's name the way The List spells it.

The List (and some venue calendars) write a room as free text:

    "Hopmonk, Novato"                       "Hopmonk Tavern, Novato"
    "Felton Music Hall, 6275 Hwy 9, Felton" "Guild Theater, Memlo Park"
    "Bandshell, Golden Gate Park, S.F."     "Siesta Valley Bowl, East Bay"
    "Warriors Stadiom, S.F."                "Castro,"

so one room arrives under several spellings, with a street address or a typo
in the city, or no city at all. `parse` splits a listing into name, city, state
and street address; `cluster` merges the spellings of one room. Everything here
is deterministic and offline: it only decides what is worth looking up, and
what an AI or a map service should be asked about.

The key a room gets (`Room.key`) is *not* `dataset.venue_id`: a show's venue
string, and every id built from it, stay exactly as the source spelled them.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ..scenespec import dataset

# Bay Area places a listing can name. A city in a listing is matched against
# these exactly, by prefix ("Stanford Campus") and then by near-spelling
# ("Memlo Park" -> Menlo Park), so a typo does not make a second city.
CITIES = (
    "San Francisco", "Oakland", "Berkeley", "Alameda", "Albany", "Emeryville", "Richmond",
    "El Cerrito", "San Leandro", "Hayward", "Fremont", "Walnut Creek", "Concord", "Orinda",
    "Crockett", "Vallejo", "Benicia", "Martinez", "Napa", "Sonoma", "Santa Rosa", "Petaluma",
    "Sebastopol", "Healdsburg", "Rohnert Park", "Novato", "Fairfax", "San Anselmo", "San Rafael",
    "Mill Valley", "Corte Madera", "Larkspur", "Sausalito", "Tiburon", "Pacifica", "Daly City",
    "South San Francisco", "San Mateo", "Burlingame", "Half Moon Bay", "Redwood City",
    "Menlo Park", "Palo Alto", "Stanford", "Mountain View", "Sunnyvale", "Cupertino",
    "Santa Clara", "Saratoga", "Los Gatos", "Campbell", "San Jose", "Santa Cruz", "Felton",
    "Capitola", "Bethel Island", "La Honda", "Livermore", "Pleasanton", "Sacramento")
_CITY_ALIASES = {"s.f.": "San Francisco", "sf": "San Francisco", "s f": "San Francisco",
                 "san fran": "San Francisco", "so. san francisco": "South San Francisco"}
# Not a place a room is *in*: a region label, which says nothing about the city.
_REGIONS = {"east bay", "north bay", "south bay", "peninsula", "bay area", "marin", "sonoma county"}
_STATES = {"ca": "CA", "calif": "CA", "calif.": "CA", "california": "CA"}
_ADDRESS = re.compile(r"^\d+[a-z]?\b")                        # "1345 Bush Street"
_FILLER = re.compile(r"\b(?:the|co|company|inc|llc|and|&)\b|[^\w\s]", re.IGNORECASE)
_SAME = {"theatre": "theater", "ctr": "center", "centre": "center"}


@dataclass
class Parsed:
    name: str
    city: str = ""
    state: str = ""
    address: str = ""


def _city(text: str) -> str | None:
    """The canonical city a piece of a listing names, or None if it is not one."""
    t = re.sub(r"\s+", " ", text.strip().lower())
    if t in _CITY_ALIASES:
        return _CITY_ALIASES[t]
    for c in CITIES:
        if t == c.lower() or (t.startswith(c.lower() + " ") and c == "Stanford"):
            return c
    near = difflib.get_close_matches(t, [c.lower() for c in CITIES], n=1, cutoff=0.82)
    return next(c for c in CITIES if c.lower() == near[0]) if near else None


def parse(listed: str) -> Parsed:
    """"Fox Theater, 2215 Broadwa, Redwood City" -> name "Fox Theater",
    address "2215 Broadwa", city "Redwood City"."""
    parts = [p.strip() for p in listed.split(",")]
    parts = [p for p in parts if p]
    state = ""
    while len(parts) > 1 and (parts[-1].lower() in _STATES or re.fullmatch(r"[A-Z]{2}", parts[-1])):
        state = _STATES.get(parts[-1].lower(), parts[-1].upper())
        parts.pop()
    city = ""
    if len(parts) > 1:
        c = _city(parts[-1])
        if c:
            city = c
            parts.pop()
        elif parts[-1].lower() in _REGIONS:
            parts.pop()                       # "East Bay" is not a city; do not guess one
    address = next((p for p in parts[1:] if _ADDRESS.match(p)), "")
    name = ", ".join(p for p in parts if p != address) or listed.strip()
    return Parsed(name=name, city=city, state=state, address=address)


def _tokens(name: str) -> list[str]:
    words = _FILLER.sub(" ", name.lower()).split()
    return [_SAME.get(w, w) for w in words]


def _same_name(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if ta == tb or ta == tb[:len(ta)] or tb == ta[:len(tb)]:      # Hopmonk / Hopmonk Tavern
        return True
    return difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= 0.88


@dataclass(eq=False)
class Room:
    """One physical room, under every spelling the listings used for it."""
    name: str
    city: str = ""
    state: str = ""
    address: str = ""
    variants: list[str] = field(default_factory=list)
    shows: int = 0
    key_name: str = ""              # what the key is built from: independent of how many shows each spelling has

    @property
    def key(self) -> str:
        return dataset.venue_id(f"{self.key_name or self.name} {self.city}".strip())

    @property
    def label(self) -> str:
        return f"{self.name}, {self.city}" if self.city else self.name


def cluster(listed: dict[str, int]) -> list[Room]:
    """Merge spellings of one room. `listed` maps each spelling to its show count.

    Two spellings are one room when their names match (one a prefix of the
    other, or within a typo) and their cities agree -- or one has no city, which
    then borrows the other's. The most-played spelling names the room."""
    rooms: list[Room] = []
    for text, n in sorted(listed.items(), key=lambda kv: (-kv[1], kv[0])):
        p = parse(text)
        home = next((r for r in rooms if _same_name(r.name, p.name)
                     and (not r.city or not p.city or r.city == p.city)), None)
        if home is None:
            rooms.append(Room(p.name, p.city, p.state, p.address, [text], n))
            continue
        home.city = home.city or p.city
        home.state = home.state or p.state
        home.address = home.address or p.address
        home.variants.append(text)
        home.shows += n
    for r in rooms:
        # The room's identity must not move when the spellings' show counts do (the count
        # decides only the display name): the plainest spelling, fewest words then alphabetical.
        r.key_name = min((parse(v).name for v in r.variants), key=lambda n: (len(_tokens(n)), n.lower()))
    return rooms
