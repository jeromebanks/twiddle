"""Domain types for local shows. No UI, no network."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import date

from ..lookup import norm


@dataclass
class Show:
    """One night at one venue, as a listing source described it."""
    day: date
    venue: str                       # the source's own spelling: "Ivy Room, Albany"
    bands: list[str]                 # headliner first, as listed
    age: str | None = None           # "a/a", "21+", "18+"
    price: str | None = None         # raw: "$15/$18", and sometimes "$!2.67"
    times: str | None = None         # raw: "7pm/8pm til 11pm"
    notes: list[str] = field(default_factory=list)   # "(record release)" ...
    flags: list[str] = field(default_factory=list)   # "recommended", "pit warning" ...
    source: str = ""
    source_url: str = ""
    title: str = ""                  # the night's name when it bills no bands: "Freakyoke"
    flyer: str = ""                  # the poster, full size
    tickets: str = ""                # where to buy
    also: list[str] = field(default_factory=list)    # other sources merged into this one

    @property
    def key(self) -> tuple:
        """Identity across sources: the same night, room and headliner."""
        return (self.day, self.venue.lower(), (self.bands[0] if self.bands else "").lower())

    @property
    def headliner(self) -> str:
        return self.bands[0] if self.bands else ""

    def share_text(self, venue: str | None = None, address: str | None = None) -> str:
        """A message to send someone: who, when, where, how much, and the listing.

        `venue` / `address` are the watched venue's name and street, when known;
        otherwise the source's own spelling ("Kilowatt, S.F.") is used.
        """
        when = f"{self.day:%a %b} {self.day.day}" + (f", {self.times}" if self.times else "")
        where = venue or self.venue
        if address:
            where += " · " + address.split(", CA")[0]
        extra = " · ".join(x for x in (self.age, self.price, *self.flags, *self.notes) if x)
        lines = [", ".join(self.bands) or self.title, when, where]
        if extra:
            lines.append(extra)
        lines += dict.fromkeys(u for u in (self.source_url, self.tickets, self.flyer) if u)
        return "\n".join(lines)

    @property
    def billing(self) -> str:
        """What the night is called in a list: its bands, else its name."""
        return ", ".join(self.bands) or self.title

    def to_dict(self) -> dict:
        d = asdict(self)
        d["day"] = self.day.isoformat()
        return d


def _loose(name: str) -> str:
    """Letters and digits only: "Yoshi's" and "Yoshis", "M.D.C." and "MDC" agree."""
    return re.sub(r"[^0-9a-z]", "", norm(name))


def _band_key(name: str) -> str:
    """`_loose` without a leading "The": The List writes "Well" for The Well."""
    return _loose(re.sub(r"^\s*the\s+", "", name, flags=re.IGNORECASE))


def _band_keys(bands: list[str]) -> set[str]:
    """Each band, plus the halves of a joint billing: a site's "Nef the
    Pharaoh & D-Lo" is The List's "Nef The Pharaoh" and "D-Lo" (Crybaby,
    2026-09-26). For matching only; nothing is renamed."""
    keys = set()
    for b in bands:
        keys.add(_band_key(b))
        parts = re.split(r"\s+(?:&|and|x|×)\s+", b, flags=re.IGNORECASE)
        if len(parts) > 1:
            keys.update(_band_key(p) for p in parts)
    return keys - {""}


def _merge(into: Show, other: Show) -> None:
    """Fill what `into` lacks from `other`. The first source's facts stand:
    The List's advance/door price and age beat a ticketing site's."""
    for f in ("age", "price", "times", "title", "flyer", "tickets"):
        if not getattr(into, f) and getattr(other, f):
            setattr(into, f, getattr(other, f))
    if not into.bands:
        into.bands = list(other.bands)
    for src in (other.source, *other.also):
        if src and src != into.source and src not in into.also:
            into.also.append(src)


def dedupe(shows: list[Show], room: Callable[[str], str] | None = None) -> list[Show]:
    """Merge listings of the same show from several sources, first one wins.

    Sources spell rooms their own way, so `room` maps a spelling to one name
    per room (`sources.fetch_all` passes the watched venue's name), and
    names are compared loosely. Without it, "Yoshi's, Oakland" from one
    source and "Yoshis, Oakland" from another would be two rows.

    Two listings of one night in one room are the same show when their
    lineups share a band, or when each source lists only that one show
    there that night. Headliners alone don't agree often enough: measured
    against The List (2026-09-26), the Stork Club's own site bills openers
    first, and a themed night goes by different names ("Hell Comes to
    Oakland IV" / "Halloween tribute band Extravaganza").
    """
    slots: dict[tuple, list[Show]] = {}
    for s in shows:
        where = room(s.venue) if room else s.venue
        slots.setdefault((s.day, _loose(where)), []).append(s)

    kept: list[Show] = []
    for group in slots.values():
        merged: list[Show] = []
        for s in group:
            if not merged:
                merged.append(replace(s, bands=list(s.bands), also=list(s.also)))
                continue
            names = _band_keys(s.bands)
            match = next((m for m in merged if names & _band_keys(m.bands)), None)
            if match is None and not names and s.title:
                match = next((m for m in merged if _loose(m.title) == _loose(s.title)), None)
            if match is None:
                # One show each side: the same night under two names.
                mine = [m for m in merged if s.source not in (m.source, *m.also)]
                if len(mine) == 1 and len(merged) == 1 and \
                        sum(1 for x in group if x.source == s.source) == 1:
                    match = mine[0]
            if match is None:
                merged.append(replace(s, bands=list(s.bands), also=list(s.also)))
            else:
                _merge(match, s)
        kept += merged
    return sorted(kept, key=lambda s: (s.day, s.venue))
