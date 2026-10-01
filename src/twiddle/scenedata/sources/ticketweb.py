"""Venues whose own site lists shows with TicketWeb's WordPress plugin.

Measured 2026-09-26: the Independent, Brick & Mortar, Neck of the Woods,
Bimbo's, August Hall, Crybaby and Cafe du Nord / the Swedish American Hall
all render TicketWeb's `tw-section` markup, server-side, on one page: a
date, a name, "with" support acts, an optional prefix (a tour or
presenter), times, the event image (`i.ticketweb.com/...`, or
Ticketmaster's for August Hall), the venue's own event page and a ticket
link.

The event image is the promoter's upload: often the tour poster, sometimes
a band photo. Either way it is what the venue shows, so it goes in `flyer`.
Each theme lays the same classes out differently -- the date alone comes
four ways -- so fields are found by class, not by position. Where a theme
shows age and price they are read; merged rows keep The List's anyway.
"""
from __future__ import annotations

import html
import re
from datetime import date, timedelta

import requests

from ...scenespec.model import Show
from .base import SourceError
from .seetickets import _day

# source name -> (listing page, the venue as a watched venue matches it)
VENUES = {
    "independent": ("https://www.theindependentsf.com/", "Independent, S.F."),
    "brickandmortar": ("https://www.brickandmortarmusic.com/", "Brick & Mortar Music Hall, S.F."),
    "neckofthewoods": ("https://www.neckofthewoodssf.com/", "Neck of the Woods, S.F."),
    "bimbos": ("https://bimbos365club.com/", "Bimbo's 365 Club, S.F."),
    "augusthall": ("https://www.augusthallsf.com/", "August Hall, S.F."),
    "crybaby": ("https://crybaby.live/", "Crybaby, Oakland"),
    "cafedunord": ("https://cafedunord.com/", "Cafe du Nord, S.F."),
}

_SECTION = re.compile(r'<div class="tw-section">(.*?)(?=<div class="tw-section">|\Z)', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment or ""))).strip()


def _div(block: str, cls: str) -> str:
    # the whole class token: "tw-event-date" must not match "tw-event-date-time"
    m = re.search(rf'<(?:div|span) class="(?:[^"]*\s)?{cls}(?:\s[^"]*)?"[^>]*>(.*?)</(?:div|span)>',
                  block, re.DOTALL)
    return _text(m.group(1)) if m else ""


def _date(block: str, today: date) -> date | None:
    """The themes write the date four ways (measured 2026-09-26):
    "Sat" + "9.26" (Independent), "Sat, Sep 26" (Crybaby), "September" +
    "28" + "Mon" (Bimbo's), "September 29, 2026" (August Hall)."""
    text = " ".join(_div(block, c) for c in ("tw-day-of-week", "tw-event-month", "tw-event-date",
                                             "tw-image-date")).lower()
    full = re.search(r"([a-z]{3})[a-z]*\.? (\d{1,2}), (\d{4})", text)
    if full and full.group(1) in _MONTHS:
        return date(int(full.group(3)), _MONTHS[full.group(1)], int(full.group(2)))
    num = re.search(r"\b(\d{1,2})\.(\d{1,2})\b", text)
    named = re.search(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})\b", text)
    if num:
        month, day = int(num.group(1)), int(num.group(2))
    elif named and named.group(1) in _MONTHS:
        month, day = _MONTHS[named.group(1)], int(named.group(2))
    else:
        return None
    wd = next((w for w in re.findall(r"\b([a-z]{3})", text) if w in _WEEKDAYS), None)
    if wd:
        return _day(f"{wd.title()} {date(2000, month, 1):%b} {day}", today)
    for year in (today.year, today.year + 1):          # no weekday: the next such date
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= today - timedelta(days=7):
            return d
    return None


def _clock(s: str) -> str | None:
    m = re.search(r"\d{1,2}(?::\d\d)?\s*[AP]M", s, re.IGNORECASE)
    return m.group(0).replace(" ", "").lower().replace(":00", "") if m else None


def _split(name: str) -> tuple[list[str], list[str]]:
    """"Max Fry, aWannabe" / "Amor De Dios/ Forced To Suffer/ exutoire" ->
    bands; "Nef the Pharaoh & D-Lo – Burn The City Tour" -> the tour is a note."""
    notes = []
    name = re.sub(r"\s+w/\s*", ", ", name)          # "Darks Oakland w/ Sizzle"
    head, *rest = re.split(r"\s+[–—-]\s+", name, maxsplit=1)
    if rest:
        notes.append(rest[0])
    bands = [b.strip() for b in re.split(r"\s*,\s*|\s*/\s+|\s+/\s*|\s+\+\s+", head) if b.strip()]
    return bands, notes


def parse_page(page: str, today: date, venue: str, source: str) -> list[Show]:
    shows = []
    for block in _SECTION.findall(page):
        day = _date(block, today)
        name_m = re.search(r'<div class="tw-name">\s*<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                           block, re.DOTALL)
        if day is None or not name_m:
            continue
        page_url, name = html.unescape(name_m.group(1)), _text(name_m.group(2))
        if re.search(r"private event|closed for", name, re.IGNORECASE):
            continue
        bands, notes = _split(name)
        support = re.sub(r"^(?:with|w/)\s+", "", _div(block, "tw-attractions"), flags=re.IGNORECASE)
        for b in re.split(r"\s*,\s*", support):
            if b and b not in bands:
                bands.append(b)
        notes = [x for x in (_div(block, "tw-prefix"), _div(block, "tw-name-presenting")) if x] + notes
        if re.search(r"tw_soldout|>\s*Sold Out", block, re.IGNORECASE):
            notes.append("sold out")
        img = re.search(r'<img[^>]+src="([^"]+)"[^>]*class="event-img|'
                        r'<img[^>]+class="event-img[^"]*"[^>]+src="([^"]+)"|'
                        r"tw-image.*?background-image:\s*url\('([^']+)'\)", block, re.DOTALL)
        tix = re.search(r'href="(https://www\.(?:ticketweb|ticketmaster)\.com/[^"]+)"', block)
        age = re.sub(r"\$.*", "", _div(block, "tw-age-restriction")).strip()
        price = _text(re.search(r'class="tw-price"[^>]*>(.*?)<', block, re.DOTALL).group(1)) \
            if 'class="tw-price"' in block else ""
        times = "/".join(dict.fromkeys(t for t in (_clock(_div(block, "tw-event-door-time")),
                                                    _clock(_div(block, "tw-event-time"))) if t))
        where = _div(block, "tw-venue-details") + " " + _div(block, "tw-venue-name")
        # Cafe du Nord's page also lists the Swedish American Hall upstairs.
        show_venue = "Swedish American Hall, S.F." if "swedish" in where.lower() else venue
        shows.append(Show(
            day=day, venue=show_venue, bands=bands,
            age="a/a" if age.lower() == "all ages" else (age or None),
            price=price or None, times=times or None, notes=notes,
            source=source, source_url=page_url,
            flyer=html.unescape(next(g for g in img.groups() if g)) if img else "",
            tickets=html.unescape(tix.group(1)) if tix else page_url))
    return shows


class TicketWeb:
    """One venue's TicketWeb listing page; `TicketWeb("independent")` etc."""

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
        shows = parse_page(resp.text, self.today or date.today(), self.venue, self.name)
        if not shows:
            raise SourceError(f"{self.host}'s listing parsed to zero shows -- "
                              "its format may have changed")
        return shows
