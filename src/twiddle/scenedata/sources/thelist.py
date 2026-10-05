"""The List -- Steve Koepke's Bay Area concert guide, as HTMLized at foopee.com.

Volunteer-maintained since the '90s and still updated daily. It covers the
small rooms (Stork Club, Stay Gold Deli, Eli's) that no ticketing API does.
It can lag the venues themselves, and it spells bands its own way, so a
name here is a search term, not an identity.

Format, measured 2026-09-22 across 1,120 rows: the by-club pages are
`by-club.0.html` .. `by-club.N.html`, and the page after the last is a 404.
Each venue is `<LI><A NAME=..><B>Venue, City</B></A>` followed by a `<UL>`
of rows:

    <LI><B><A HREF="by-date..">Oct 24</A></B> <A HREF="by-band..">Totalna Tama</A>
        (record release), <A HREF="by-band..">Xui</A> 21+ $20 7pm/8pm (Benefit ...)

Every band is its own `by-band` anchor -- none were bare text in the sample
-- so band names come from anchors and never from splitting on commas
("Mrs Robinson And The Dadbeats", "Kill 'Em All" both survive). Dates carry
no year; the legend symbols trail the row.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime

import requests

from ...scenespec.model import Show
from .base import SourceError, join_open_parens

BASE = "http://www.foopee.com/punk/the-list/"
MAX_PAGES = 20          # a runaway guard; 4 pages cover ~5 months today

# The List's own legend, from its index page.
FLAGS = {"*": "recommended", "$": "may sell out", "@": "pit warning",
         "^": "under 21 must buy drink tickets", "#": "no ins/outs"}

_VENUE = re.compile(r'<A NAME="([^"]*)"><B>(.*?)</B>', re.DOTALL | re.IGNORECASE)
_DATE = re.compile(r'<B><A HREF="by-date[^"]*">\s*([A-Za-z]{3})\s+(\d{1,2})\s*</A></B>', re.IGNORECASE)
_BAND = re.compile(r'<A HREF="by-band[^"]*">(.*?)</A>', re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_AGE = re.compile(r"(?<!\S)(a/a|\d\d\+)(?!\S)", re.IGNORECASE)
_PRICE = re.compile(r"(?<!\S)\S*\$[^\s]*\d[^\s]*")
_TIME = re.compile(r"(?<!\S)\d{1,2}(?::\d\d)?[ap]m(?:/\d{1,2}(?::\d\d)?[ap]m)*"
                   r"(?:\s+til\s+\S+)?", re.IGNORECASE)
_PAREN = re.compile(r"\([^)]*\)")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", fragment))).strip()


def infer_year(month: int, day: int, today: date) -> date:
    """The List gives "Jan 3" with no year: the next such date, within reason.

    A date more than 30 days in the past is next year's (a December list
    running into January). Recent past dates stay this year, since the list
    keeps a show up for a few days after it happens.
    """
    d = date(today.year, month, day)
    if (today - d).days > 30:
        d = date(today.year + 1, month, day)
    return d


def parse_row(row: str, venue: str, today: date, url: str = "") -> Show | None:
    m = _DATE.search(row)
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    try:
        day = infer_year(_MONTHS[m.group(1).lower()], int(m.group(2)), today)
    except ValueError:          # "Feb 30" -- a typo in the list, not our bug
        return None
    body = row[m.end():]
    # Annotations sometimes sit *inside* the band's anchor -- measured:
    # `<A ..>Totalna Tama (record release)</A>` -- so they are split off the
    # name here, or every lookup would search for "Totalna Tama (record release)".
    bands: list[str] = []
    notes: list[str] = []
    for raw in join_open_parens([_text(r) for r in _BAND.findall(body)]):
        name = raw
        for p in _PAREN.findall(name):
            notes.append(f"{_PAREN.sub('', name).strip()}: {p.strip('() ')}")
        name = re.sub(r"\s+", " ", _PAREN.sub(" ", name)).strip()
        if name:
            bands.append(name)

    rest = _text(_BAND.sub(" ", body))
    notes += [p.strip("() ") for p in _PAREN.findall(rest)]
    rest = _PAREN.sub(" ", rest)

    age = _AGE.search(rest)
    times = _TIME.search(rest)
    rest_wo_time = _TIME.sub(" ", rest)
    price = _PRICE.search(rest_wo_time)

    flags = []
    for tok in rest_wo_time.split():
        if tok and set(tok) <= set(FLAGS):
            flags += [FLAGS[c] for c in tok]

    return Show(day=day, venue=venue, bands=bands,
                age=age.group(1).lower() if age else None,
                price=price.group(0) if price else None,
                times=times.group(0) if times else None,
                notes=notes, flags=flags, source="thelist", source_url=url)


def parse_page(page: str, today: date, url: str = "") -> list[Show]:
    """Every show on one by-club page.

    Each show links to its venue's anchor on the page ("by-club.1.html#Greek_
    Theater__UC_Berkeley_Campus"), so a shared link lands on that room's
    listings. The List renumbers its pages as shows are added, so such a link
    can drift to the wrong page after a few days; it is for sharing this
    week, not for keeping.
    """
    shows: list[Show] = []
    venue = anchor = ""
    for chunk in re.split(r"<LI>", page, flags=re.IGNORECASE):
        v = _VENUE.search(chunk)
        if v:
            anchor, venue = v.group(1), _text(v.group(2))
            continue
        if venue and "by-date" in chunk:
            link = f"{url}#{anchor}" if url and anchor else url
            show = parse_row(chunk.split("</UL>")[0], venue, today, link)
            if show and show.bands:
                shows.append(show)
    return shows


class TheList:
    name = "thelist"

    def __init__(self, base: str = BASE, timeout: float = 15.0):
        self.base = base
        self.timeout = timeout

    def fetch(self, today: date | None = None) -> list[Show]:
        today = today or datetime.now().date()
        shows: list[Show] = []
        for n in range(MAX_PAGES):
            url = f"{self.base}by-club.{n}.html"
            try:
                resp = requests.get(url, timeout=self.timeout,
                                    headers={"User-Agent": "twiddle scene"})
            except requests.RequestException as exc:
                raise SourceError(f"could not reach The List: {exc}") from exc
            if resp.status_code == 404 and n > 0:
                break
            if resp.status_code != 200:
                raise SourceError(f"The List returned HTTP {resp.status_code} for {url}")
            resp.encoding = resp.encoding or "latin-1"
            shows += parse_page(resp.text, today, url)
        if not shows:
            raise SourceError("The List parsed to zero shows -- its format may have changed")
        return shows
