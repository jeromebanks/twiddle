"""Gray Area (San Francisco) from its own events page, grayarea.org/visit/events/.

Server-rendered WordPress (measured 2026-09-26): each event is a card with
its page, a poster image (as a CSS background), a type ("In-Person",
"Learn"), a date without a year ("09/26") and a title whose small first
line is often the presenter ("Magik*Magik Orchestra Presents").

Gray Area is an art and technology center, so the page mixes concerts with
courses, workshops, talks and exhibitions. Those are dropped by name. What
remains is kept by its name (`Show.title`), not split into bands: "COCOON
An Orchestra Sleepover Concert" and "Derrick Gee's Radio Hour" aren't
lineups. When The List carries the same night ("Eraserhead Xiu Xiu"), the
merge takes its bands.
"""
from __future__ import annotations

import html
import re
from datetime import date, timedelta

import requests

from ...scenespec.model import Show
from .base import SourceError

URL = "https://grayarea.org/visit/events/"
VENUE = "Gray Area, S.F."
_CARD = re.compile(r'<a class="item-link" href="([^"]+)">(.*?)</h5>', re.DOTALL)
_IMG = re.compile(r"background-image:\s*url\(([^)]+)\)")
_DATE = re.compile(r'<div class="date">\s*(\d{1,2})/(\d{1,2})\s*</div>')
_TYPE = re.compile(r'<div class="post-type[^"]*">(.*?)</div>', re.DOTALL)
_TITLE = re.compile(r'<h5 class="item-title">(.*)', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
NOT_SHOWS = re.compile(r"\b(?:course|workshop|book club|exhibition|intensive|lab|office hours|"
                       r"conversation|panel|lecture|meetup|hackathon|critic|class|training|"
                       r"orientation|tool ?kit|community day|commissions?)\b", re.IGNORECASE)
AHEAD = timedelta(days=240)      # a listing further out than this is a past card left up


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment or ""))).strip()


def _next(month: int, day: int, today: date) -> date | None:
    """"09/26" has no year: the first such date from a week ago on."""
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= today - timedelta(days=7):
            return d
    return None


def parse_page(page: str, today: date) -> list[Show]:
    shows = []
    for url, card in _CARD.findall(page):
        m = _DATE.search(card)
        kind = _text((_TYPE.search(card) or [None, ""])[1])
        t = _TITLE.search(card)
        if not m or not t:
            continue                        # exhibitions carry a date range instead
        raw = t.group(1)
        # the small first line is a presenter or a kind ("Exhibition")
        small = re.match(r'\s*<span style="font-size:70%">(.*?)</span>\s*<br\s*/?>', raw, re.DOTALL)
        presenter = _text(small.group(1)) if small else ""
        name = _text(raw[small.end():] if small else raw)
        if not name or NOT_SHOWS.search(f"{presenter} {name} {kind}") or kind.lower() == "learn":
            continue
        day = _next(int(m.group(1)), int(m.group(2)), today)
        if day is None or day < today or day - today > AHEAD:
            continue
        img = _IMG.search(card)
        shows.append(Show(day=day, venue=VENUE, bands=[], title=name,
                          notes=[x for x in (presenter, kind) if x and x != "In-Person"],
                          source="grayarea", source_url=html.unescape(url),
                          flyer=html.unescape(img.group(1).strip("'\"")) if img else "",
                          tickets=html.unescape(url)))
    return shows


class GrayArea:
    name = "grayarea"

    def __init__(self, url: str = URL, timeout: float = 20.0, today: date | None = None):
        self.url = url
        self.timeout = timeout
        self.today = today

    def fetch(self) -> list[Show]:
        try:
            resp = requests.get(self.url, timeout=self.timeout,
                                headers={"User-Agent": "Mozilla/5.0 (Macintosh) twiddle scene"})
        except requests.RequestException as exc:
            raise SourceError(f"could not reach grayarea.org: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"grayarea.org returned HTTP {resp.status_code}")
        shows = parse_page(resp.text, self.today or date.today())
        if not shows:
            raise SourceError("Gray Area's events page parsed to zero shows -- "
                              "its format may have changed")
        return shows
