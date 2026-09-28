"""Venues whose calendar is a Squarespace events collection: the Sound Room.

Any Squarespace collection answers `?format=json` (measured 2026-09-26 on
soundroom.org/events: 29 upcoming events, each with a title, start and end
as epoch milliseconds, an image, its page and a blurb). No key, no login.
Kilowatt, Great Northern, 4 Star and the Knockout also run on Squarespace;
if one of them keeps an events collection, it is one more line in `VENUES`.

Titles are billings: "Saúl Sierra with Cascada de Flores", "Brahm Sasner
Trio". The price, when there is one, is in the blurb.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from ..model import Show
from .base import SourceError

PACIFIC = ZoneInfo("America/Los_Angeles")
# source name -> (events collection URL, the venue as a watched venue matches it)
VENUES = {
    "soundroom": ("https://www.soundroom.org/events", "Sound Room, Oakland"),
}
_TAG = re.compile(r"<[^>]+>")
_DOLLARS = re.compile(r"\$\s?(\d+(?:\.\d\d)?)")
# a night that isn't a band's: storytelling, jams, classes
_NAMED_NIGHT = re.compile(r"(?:slam\b|\bjam session|open mic|workshop|class|storytelling|"
                          r"fundraiser|gala|party)\b", re.IGNORECASE)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment or ""))).strip()


def _clock(t: datetime) -> str:
    return t.strftime("%-I:%M%p").lower().replace(":00", "")


def to_show(ev: dict, source: str) -> Show | None:
    title = _text(ev.get("title"))
    start = ev.get("startDate")
    if not title or not start:
        return None
    when = datetime.fromtimestamp(start / 1000, PACIFIC)
    url, venue = VENUES[source]
    base = url.split("/", 3)[:3]
    page = "/".join(base) + (ev.get("fullUrl") or "")
    amounts = [a.replace(".00", "") for a in _DOLLARS.findall(_text(ev.get("body")))]
    price = "/".join("$" + a for a in dict.fromkeys(amounts)) or None
    if _NAMED_NIGHT.search(title):
        bands, name = [], title
    else:
        bands = [b.strip() for b in re.split(r"\s+(?:with|w/|feat\.?|featuring)\s+|\s*,\s*|\s+\+\s+",
                                             title, flags=re.IGNORECASE) if b.strip()]
        name = ""
    notes = [_text(t) for t in (ev.get("categories") or []) + (ev.get("tags") or []) if t]
    return Show(day=when.date(), venue=venue, bands=bands, price=price, times=_clock(when),
                notes=notes, source=source, source_url=page, title=name,
                flyer=ev.get("assetUrl") or "", tickets=page)


class Squarespace:
    """One venue's Squarespace events collection; `Squarespace("soundroom")`."""

    def __init__(self, name: str, timeout: float = 20.0):
        self.name = name
        self.url, self.venue = VENUES[name]
        self.host = self.url.split("/")[2]
        self.timeout = timeout

    def fetch(self) -> list[Show]:
        try:
            resp = requests.get(self.url, params={"format": "json"}, timeout=self.timeout,
                                headers={"User-Agent": "Mozilla/5.0 (Macintosh) twiddle scene"})
        except requests.RequestException as exc:
            raise SourceError(f"could not reach {self.host}: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"{self.host} returned HTTP {resp.status_code}")
        try:
            upcoming = resp.json().get("upcoming") or []
        except ValueError as exc:
            raise SourceError(f"{self.host} did not answer with JSON") from exc
        shows = [s for s in (to_show(ev, self.name) for ev in upcoming) if s]
        if not shows:
            raise SourceError(f"{self.host}'s events collection has no upcoming events -- "
                              "its format may have changed")
        return shows
