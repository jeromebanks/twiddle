"""Venues whose calendar is a Google Calendar drawn by WordPress "Simple
Calendar" (google-calendar-events): The DeLuxe.

thedeluxesf.com/calendar/ renders its list server-side (measured
2026-09-26): each event is an `li.simcal-event` with `data-start` (epoch
seconds, exact) and a title that carries the price ("The Cosmo Alleycats
$15"), about six months of them on one page. There are no flyers or ticket
links: the room is walk-in, pay at the door.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from ...scene.model import Show
from .base import SourceError

PACIFIC = ZoneInfo("America/Los_Angeles")
# source name -> (calendar page, the venue as a watched venue matches it)
VENUES = {
    "deluxe": ("https://thedeluxesf.com/calendar/", "The DeLuxe, S.F."),
}
_EVENT = re.compile(r'<li class="simcal-event[^"]*"[^>]*data-start="(\d+)"[^>]*>(.*?)</li>', re.DOTALL)
_TITLE = re.compile(r'class="simcal-event-title"[^>]*>(.*?)</span>', re.DOTALL)
_PRICE = re.compile(r"\s*(\$\s?\d+(?:\.\d\d)?(?:\s*[-/]\s*\$?\d+)?|free)\s*$", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment or ""))).strip()


def split_title(title: str) -> tuple[list[str], list[str], str | None]:
    """"My Dog Jack (Hardly Strictly after party) $20" -> (["My Dog Jack"],
    ["Hardly Strictly after party"], "$20"); "... w/ Sax Gordon" adds a band."""
    price = None
    m = _PRICE.search(title)
    if m:
        price = m.group(1).replace(" ", "").lower() if m.group(1).lower() == "free" else \
            m.group(1).replace(" ", "")
        title = title[:m.start()]
    notes = [p.strip() for p in re.findall(r"\(([^)]*)\)", title)]
    title = re.sub(r"\s*\([^)]*\)", "", title).strip()
    bands = [b.strip() for b in re.split(r"\s+(?:w/|with|ft\.?|feat\.?|featuring)\s+|\s*,\s*",
                                         title, flags=re.IGNORECASE) if b.strip()]
    return bands, notes, price


def parse_page(page: str, today: date, venue: str, source: str, url: str) -> list[Show]:
    shows = []
    for start, body in _EVENT.findall(page):
        t = _TITLE.search(body)
        if not t:
            continue
        when = datetime.fromtimestamp(int(start), PACIFIC)
        if when.date() < today:
            continue                        # the page starts with this week's past nights
        bands, notes, price = split_title(_text(t.group(1)))
        if not bands:
            continue
        shows.append(Show(day=when.date(), venue=venue, bands=bands, price=price,
                          times=when.strftime("%-I:%M%p").lower().replace(":00", ""),
                          notes=notes, source=source, source_url=url))
    return shows


class SimpleCalendar:
    """One venue's Simple Calendar page; `SimpleCalendar("deluxe")`."""

    def __init__(self, name: str, timeout: float = 20.0, today: date | None = None):
        self.name = name
        self.url, self.venue = VENUES[name]
        self.host = self.url.split("/")[2]
        self.timeout = timeout
        self.today = today

    def fetch(self) -> list[Show]:
        try:
            resp = requests.get(self.url, timeout=self.timeout,
                                headers={"User-Agent": "Mozilla/5.0 (Macintosh) twiddle scene"})
        except requests.RequestException as exc:
            raise SourceError(f"could not reach {self.host}: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"{self.host} returned HTTP {resp.status_code}")
        shows = parse_page(resp.text, self.today or date.today(), self.venue, self.name, self.url)
        if not shows:
            raise SourceError(f"{self.host}'s calendar parsed to zero shows -- "
                              "its format may have changed")
        return shows
