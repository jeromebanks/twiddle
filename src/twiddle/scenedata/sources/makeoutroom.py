"""The Make-Out Room (San Francisco): CalendarWiz for listings, its blog for flyers.

Measured 2026-09-26:
- makeoutroom.com/calendar.html embeds CalendarWiz (`crd=makeoutroom`),
  which reaches months ahead. It needs a session: the first request sets a
  PHPSESSID cookie and bounces through a browser check, after which
  `calendar.php?...&op=cal&month=M&year=Y` returns a month grid. Every
  event has `data-etimestamp` (epoch seconds, exact) and one line of text:
  "TITLE ~ description ~ price ~ 7:00pm - 9:30pm". No feed: rss.php and
  ical.php are 404.
- The home page is a Weebly blog, one post per night, covering only the
  next day or two. Each act there has its flyer (an `<img>`) above its
  paragraph, so flyers are matched to CalendarWiz events by night and by
  the title's first word.

Most nights are DJ dance nights with a name (BOOM!, El SUPERRITMO!); they
are kept by name. A lineup is written with " + " ("DadCo + REWINDER",
"Talent Moat presents: TWISTED TEENS (New Orleans) + FORTY DROP FEW").
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from ...scene.model import Show, _loose
from .base import SourceError

PACIFIC = ZoneInfo("America/Los_Angeles")
VENUE = "Make-Out Room, S.F."
SITE = "https://www.makeoutroom.com/"
CALWIZ = "https://www.calendarwiz.com/calendars/calendar.php"
CRD = "makeoutroom"
MONTHS = 3                      # this month and the next two
UA = "Mozilla/5.0 (Macintosh) twiddle scene"

_EVENT = re.compile(r'data-etimestamp="(\d+)"\s+data-event_id="(\d+)".*?'
                    r'<div class="cw-e-a"><a [^>]*>(.*?)</a>', re.DOTALL)
_POST = re.compile(r'<span class="date-text">\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*</span>(.*?)'
                   r'(?=<div id="blog-post-|\Z)', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_DOLLARS = re.compile(r"\$\s?(\d+(?:\.\d\d)?)")
_SPAN = re.compile(r"(\d{1,2}(?::\d\d)?\s*[ap]m)\s*-\s*(\d{1,2}(?::\d\d)?\s*[ap]m)", re.IGNORECASE)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment or ""))).strip()


def _clock(s: str) -> str:
    return s.replace(" ", "").lower().replace(":00", "")


def split_line(line: str) -> tuple[list[str], str, list[str], str | None, str | None]:
    """"TITLE ~ description ~ $10 ~ 10:00pm - 2:00am" -> (bands, title, notes, price, end)."""
    parts = [p.strip(" ~​") for p in line.split("~")]
    parts = [p for p in parts if p]
    head, rest = (parts[0], parts[1:]) if parts else ("", [])
    price, end, notes = None, None, []
    for p in rest:
        span = _SPAN.search(p)
        if span and len(p) < 40:
            end = _clock(span.group(2))
            continue
        dollars = _DOLLARS.findall(p)
        if dollars and len(p) < 60:
            price = "/".join("$" + d for d in dict.fromkeys(dollars))
            continue
        if p.strip().upper() == "FREE":
            price = "free"
            continue
        notes.append(p)
    pre = re.match(r"(.+?\bpresents?)\s*:?\s+(.+)", head, re.IGNORECASE)
    if pre:
        notes.insert(0, pre.group(1))
        head = pre.group(2)
    if " + " in head:
        bands = []
        for b in head.split(" + "):
            where = re.findall(r"\(([^)]*)\)", b)
            name = re.sub(r"\s*\([^)]*\)", "", b).strip()
            if name:
                bands.append(name)
            notes += [f"{name}: {w}" for w in where if w]
        return bands, "", notes, price, end
    return [], head, notes, price, end


def parse_month(page: str, first: date) -> list[Show]:
    shows = []
    for stamp, _eid, text in _EVENT.findall(page):
        when = datetime.fromtimestamp(int(stamp), PACIFIC)
        if when.date() < first:
            continue
        bands, title, notes, price, end = split_line(_text(text))
        if not bands and not title:
            continue
        start = when.strftime("%-I:%M%p").lower().replace(":00", "")
        shows.append(Show(day=when.date(), venue=VENUE, bands=bands, title=title, price=price,
                          times=start + (f" til {end}" if end else ""), notes=notes,
                          source="makeoutroom", source_url=SITE + "calendar.html"))
    return shows


def parse_flyers(page: str) -> dict[date, list[tuple[str, str]]]:
    """The blog -> {night: [(flyer URL, that act's text, loosened)]}."""
    out: dict[date, list[tuple[str, str]]] = {}
    for m, d, y, body in _POST.findall(page):
        night = date(int(y), int(m), int(d))
        for seg in re.split(r'<hr class="styled-hr"', body):
            img = re.search(r'<img src="([^"]+)"', seg)
            if img:
                src = img.group(1)
                src = src if src.startswith("http") else SITE.rstrip("/") + src
                out.setdefault(night, []).append((src, _loose(_text(seg))))
    return out


def attach_flyers(shows: list[Show], flyers: dict[date, list[tuple[str, str]]]) -> None:
    for s in shows:
        name = (s.bands[0] if s.bands else s.title).split()
        key = _loose(name[0]) if name else ""
        if len(key) < 3:
            key = _loose(" ".join(name[:2]))
        for url, text in flyers.get(s.day, []):
            if key and key in text:
                s.flyer = url
                break


class MakeOutRoom:
    name = "makeoutroom"

    def __init__(self, timeout: float = 20.0, today: date | None = None):
        self.timeout = timeout
        self.today = today

    def _get(self, session: requests.Session, url: str, **params) -> str:
        try:
            resp = session.get(url, params=params, timeout=self.timeout, headers={"User-Agent": UA})
        except requests.RequestException as exc:
            raise SourceError(f"could not reach {url.split('/')[2]}: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"{url.split('/')[2]} returned HTTP {resp.status_code}")
        return resp.text

    def fetch(self) -> list[Show]:
        today = self.today or date.today()
        s = requests.Session()
        common = {"crd": CRD, "cid[]": "all", "jsenabled": 1, "winh": 900, "winw": 1400,
                  "inifr": "false"}
        self._get(s, CALWIZ, **common)          # sets the session cookie
        shows: list[Show] = []
        y, m = today.year, today.month
        for _ in range(MONTHS):
            shows += parse_month(self._get(s, CALWIZ, **common, op="cal", month=m, year=y), today)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if not shows:
            raise SourceError("the Make-Out Room's CalendarWiz calendar parsed to zero shows -- "
                              "its format (or its session dance) may have changed")
        # Flyers are a bonus: a blog that fails costs the flyers, not the listings.
        try:
            attach_flyers(shows, parse_flyers(self._get(s, SITE)))
        except SourceError:
            pass
        seen, out = set(), []
        for sh in shows:                          # a month grid repeats nothing, but be sure
            k = (sh.day, sh.billing)
            if k not in seen:
                seen.add(k)
                out.append(sh)
        return out
