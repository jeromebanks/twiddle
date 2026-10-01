"""Yoshi's (Oakland) from its own calendar page, yoshis.com/events/calendar.

The List doesn't carry Yoshi's (measured 2026-09-25: not one show in its 15
date pages), so this reads the venue directly. One page holds ~6 months as
plain HTML; each event's `aria-label` carries the full date with its year,
so no year inference is needed.

Measured quirks, each handled below:
- A run ("Fri 10.9 to Fri 10.16") is not every night in between: Musiq
  Soulchild's run skipped 10.12-10.14, when other acts played. Its detail
  page lists each performance, so runs are expanded from there.
- Pre-show VIP packages and meet & greets are listed as their own events,
  marked "(SHOW TICKET NOT INCLUDED)". They are not shows and are dropped.
- Titles are in capitals ("FRED WESLEY'S NEW JBS W/ MARTHA HIGH'S FUNKY
  DIVAS") and carry subtitles ("ISAIAH COLLIER: 'COLLIER PLAYS COLTRANE'").
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime

import requests

from ...scenespec.model import Show
from .base import SourceError

CALENDAR = "https://yoshis.com/events/calendar"
VENUE = "Yoshi's, Oakland"

_ITEM = re.compile(r'<li\s+class="event-indv">(.*?)</li>', re.DOTALL)
_SD = re.compile(r'class="sd"><strong>(.*?)</strong>', re.DOTALL)
_TITLE = re.compile(r'<h3><a\s+aria-label="([^"]*)"\s+href="([^"]*)">(.*?)</a></h3>', re.DOTALL)
_TOPLINE = re.compile(r'class="topline">(.*?)</p>', re.DOTALL)
_PRICE = re.compile(r'class="price">(.*?)</p>', re.DOTALL)
_WHEN = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4}) - (\d{1,2}:\d\d ?[AP]M)", re.IGNORECASE)
_PERF = re.compile(r'aria-label="(?:Buy Tickets|Sold Out) [^"]*?'
                   r'([A-Z][a-z]+ \d{1,2}, \d{4}) - (\d{1,2}:\d\d ?[AP]M)"', re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_PAREN = re.compile(r"\(([^)]*)\)")
_GUESTS = re.compile(r"\s+(?:w/|feat\.?|featuring|with)\s+", re.IGNORECASE)
_AND = re.compile(r"\s*(?:,|\band\b|&)\s*", re.IGNORECASE)
_ROMAN = re.compile(r"^(?=[IVX]+$)[IVX]{2,}$")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment))).strip()


def _day(s: str) -> date:
    return datetime.strptime(s, "%B %d, %Y").date()


def _time(s: str) -> str:
    """"7:30 PM" -> "7:30pm", "7:00PM" -> "7pm": The List's style."""
    t = s.replace(" ", "").lower()
    return t.replace(":00", "")


def tidy_case(name: str) -> str:
    """Soften an all-caps word, leave anything already mixed-case alone.

    "FRED WESLEY'S" -> "Fred Wesley's" (str.title would give "Wesley'S"),
    "KRS-ONE" -> "Krs-One", "IV" stays "IV", "feat." is untouched.
    """
    def word(w: str) -> str:
        if not any(c.isalpha() for c in w) or w != w.upper() or _ROMAN.match(w):
            return w
        return re.sub(r"(^\W*|[-/.])([^\W\d_])", lambda m: m.group(1) + m.group(2).upper(),
                      w.lower(), count=0)
    return " ".join(word(w) for w in name.split())


def split_title(title: str) -> tuple[list[str], list[str], str | None]:
    """A Yoshi's title -> (bands, notes, age).

    "ISAIAH COLLIER: 'COLLIER PLAYS COLTRANE'" -> bands ["Isaiah Collier"],
    note "'Collier Plays Coltrane'". Guests after w/ / feat. become more bands.
    """
    notes: list[str] = []
    age = None
    for p in _PAREN.findall(title):
        m = re.search(r"\b(\d\d)\+", p)
        if m:
            age = f"{m.group(1)}+"
        else:
            notes.append(tidy_case(p.strip()))
    t = re.sub(r"\s+", " ", _PAREN.sub(" ", title)).strip()
    pre = re.match(r"(.+?\bpresents):\s*(.+)", t, re.IGNORECASE)
    if pre:
        notes.append(tidy_case(pre.group(1)))
        t = pre.group(2)
    for sep in (": ", " - "):
        if sep in t:
            t, sub = t.split(sep, 1)
            notes.append(tidy_case(sub.strip()))
    head, *guests = _GUESTS.split(t, maxsplit=1)
    head = re.sub(r"(?<=\S)\s+live$", "", head.strip(), flags=re.IGNORECASE)  # "BONEY JAMES LIVE"
    bands = [tidy_case(head)]
    for g in _AND.split(guests[0]) if guests else []:
        g = re.sub(r"^music director\s+", "", g.strip(), flags=re.IGNORECASE)
        if g and g.lower() not in ("more", "friends", "special guests"):
            bands.append(tidy_case(g))
    return [b for b in bands if b], notes, age


def parse_calendar(page: str) -> list[dict]:
    """Every event on the calendar page, before runs are expanded."""
    events = []
    for item in _ITEM.findall(page):
        t = _TITLE.search(item)
        if not t:
            continue
        label, url, raw_title = html.unescape(t.group(1)), t.group(2), _text(t.group(3))
        if "ticket not included" in raw_title.lower():
            continue
        when = _WHEN.search(label)
        if not when:
            continue
        sd = _SD.search(item)
        top, price = _TOPLINE.search(item), _PRICE.search(item)
        events.append({
            "title": raw_title,
            "day": _day(when.group(1)),
            "time": _time(when.group(2)),
            "run": bool(sd and re.search(r"\bto\b", _text(sd.group(1)))),
            "url": url,
            "topline": _text(top.group(1)) if top else "",
            "price": _text(price.group(1)).replace(" - ", "-").replace(" -", "-") if price else None,
            "sold_out": "sold out" in label.lower() or "/sold-out/" in url,
        })
    return events


def parse_performances(page: str) -> dict[date, list[str]]:
    """A detail page -> {night: [set times]}, e.g. two sets a night."""
    nights: dict[date, list[str]] = {}
    for d, t in _PERF.findall(page):
        times = nights.setdefault(_day(d), [])
        if _time(t) not in times:
            times.append(_time(t))
    return nights


def to_shows(event: dict, nights: dict[date, list[str]] | None = None) -> list[Show]:
    bands, notes, age = split_title(event["title"])
    if event["topline"] and event["topline"].lower() not in (b.lower() for b in bands):
        notes.append(event["topline"])
    if event["sold_out"]:
        notes.append("sold out")
    nights = nights or {event["day"]: [event["time"]]}
    return [Show(day=d, venue=VENUE, bands=list(bands), age=age, price=event["price"],
                 times="/".join(times), notes=list(notes), source="yoshis",
                 source_url=event["url"])
            for d, times in sorted(nights.items())]


class Yoshis:
    name = "yoshis"

    def __init__(self, url: str = CALENDAR, timeout: float = 15.0):
        self.url = url
        self.timeout = timeout

    def _get(self, url: str) -> str:
        try:
            resp = requests.get(url, timeout=self.timeout,
                                headers={"User-Agent": "twiddle scene"})
        except requests.RequestException as exc:
            raise SourceError(f"could not reach yoshis.com: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"yoshis.com returned HTTP {resp.status_code} for {url}")
        return resp.text

    def fetch(self) -> list[Show]:
        events = parse_calendar(self._get(self.url))
        if not events:
            raise SourceError("Yoshi's calendar parsed to zero shows -- its format may have changed")
        shows: list[Show] = []
        for ev in events:
            nights = None
            if ev["run"]:
                # A failed detail page costs that run its other nights, not
                # the whole source: the first night is still on the calendar.
                try:
                    nights = parse_performances(self._get(ev["url"])) or None
                except SourceError:
                    nights = None
            shows += to_shows(ev, nights)
        return shows
