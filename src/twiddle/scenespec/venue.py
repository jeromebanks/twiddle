"""A venue as a dataset describes it, and how to recognise one in a source's spelling.

Sources name venues their own way ("Thee Stork Club, Oakland"), so a venue is
matched on lowercase substrings, not equality. The producer decides which
venues a dataset covers (`scenedata/venues.py`) and publishes them in the
dataset's `venues` table; the client builds these from that table.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field


@dataclass(frozen=True)
class VenueInfo:
    address: str = ""
    url: str = ""
    about: str = ""
    wikipedia: str = ""         # an article title, only where one is about this room
    instagram: str = ""         # a handle, from INSTAGRAM below

    @property
    def instagram_url(self) -> str:
        return f"https://www.instagram.com/{self.instagram}/" if self.instagram else ""

    @property
    def map_url(self) -> str:
        if not self.address:
            return ""
        return "https://www.google.com/maps/search/?api=1&query=" + \
            urllib.parse.quote(self.address)

    def __bool__(self) -> bool:
        return bool(self.address or self.url or self.about or self.wikipedia)


@dataclass(frozen=True)
class Venue:
    name: str                                   # what the app shows
    match: tuple[str, ...] = field(default=())  # lowercase substrings of a source's name
    icon: str | None = None                     # a logo URL, hand-picked (see below)
    info: VenueInfo = field(default=VenueInfo(), compare=False)   # address, site, about

    def matches(self, listed: str) -> bool:
        low = listed.lower()
        return any(m in low for m in self.match)


def from_rows(rows: list[dict]) -> tuple[Venue, ...]:
    """The venues of a dataset's `venues` table. Unknown keys are ignored and a
    row without a name is skipped, as everywhere else in the reader."""
    out = []
    for r in rows:
        if not r.get("name"):
            continue
        info = VenueInfo(**{f: r.get(f) or "" for f in
                            ("address", "url", "about", "wikipedia", "instagram")})
        out.append(Venue(r["name"], tuple(m.lower() for m in r.get("match") or [r["name"]]),
                         r.get("icon") or None, info))
    return tuple(out)


def find(venues: tuple[Venue, ...], listed: str) -> Venue | None:
    """The watched venue a source's venue name refers to, if any."""
    return next((v for v in venues if v.matches(listed)), None)


def display_name(venues: tuple[Venue, ...], listed: str) -> str:
    """A watched venue's short name, else the source's name without its city."""
    v = find(venues, listed)
    return v.name if v else listed.rsplit(",", 1)[0].strip()


def resolve(venues: tuple[Venue, ...], query: str) -> Venue | None:
    """`--venue stork` -> the Stork Club: a partial, case-insensitive name.

    A match at the start of a word wins over one inside a word, or "eli"
    would find Stay Gold D*eli* before Eli's; and the venue's own name wins
    over its match strings, or "uc" would find the Greek ("... UC Berkeley
    Campus") before the UC Theatre.
    """
    q = query.lower()

    def starts(text: str) -> bool:
        words = text.replace(",", " ").split()
        return any(" ".join(words[i:]).startswith(q) for i in range(len(words)))

    return (next((v for v in venues if starts(v.name.lower())), None)
            or next((v for v in venues if any(starts(m) for m in v.match)), None)
            or next((v for v in venues if q in v.name.lower()
                     or any(q in m for m in v.match)), None))
