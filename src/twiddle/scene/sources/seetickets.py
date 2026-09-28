"""Venues whose own calendar is See Tickets' WordPress plugin.

Thee Stork Club first (theestorkclub.com/calendar/); the same plugin, and
markup, turned out to run Great American Music Hall, the Chapel, Hotel Utah
and Rickshaw Stop (measured 2026-09-26), so each is one line in `VENUES`.
The Midway's site mentions See Tickets but doesn't use this plugin.

The plugin renders two views of the same events into one page: a month grid
(~5 weeks) and a paginated list. The list is the one to read:

- it reaches ~3 months ahead, one page of 8 per `?list1page=N` -- a plain
  GET, no nonce and no JavaScript, although the site's own pager uses AJAX;
- each event carries its date ("Sat Sep 26"), `headliners`, sometimes
  `supporting-talent`, a `header` ("T-Slur Thursday presents"), age, price,
  a genre, the flyer (600px) and the See Tickets link.

Measured quirks, each handled below:
- Titles are cut at ~60 characters ("... Artificial Muscl"), so bands come
  from `headliners`, split on commas only: "Make Do and Mend" and "Tori
  Roze and the Hot Mess" are one band each.
- DJ, karaoke and queer dance nights bill no bands. They are kept, with
  their name as `Show.title`, because they have flyers; nothing looks them
  up. Private hires ("Closed for Private Event 10/17") are dropped.
- Recurring nights carry their date in the title ("Freakyoke 10/5").
- One night was listed twice under two spellings (Oct 24, "HOT GOTH NIGHT:
  HALLOWEEN" and "Hot Goth Halloween").
- Dates carry no year. The weekday settles it: only one of this year and
  next puts "Sat Sep 26" on a Saturday.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime

import requests

from ..model import Show, _loose
from .base import SourceError

# source name -> (calendar page, the venue as a watched venue matches it)
VENUES = {
    "storkclub": ("https://theestorkclub.com/calendar/", "Thee Stork Club, Oakland"),
    "gamh": ("https://gamh.com/calendar/", "Great American Music Hall, S.F."),
    "chapel": ("https://thechapelsf.com/calendar/", "Chapel, S.F."),
    "hotelutah": ("https://hotelutah.com/calendar/", "Hotel Utah, S.F."),
    "rickshaw": ("https://rickshawstop.com/calendar/", "Rickshaw Stop, S.F."),
}
MAX_PAGES = 30                  # ~8 a page; a runaway loop is a format change

_BLOCK = re.compile(r'<div[^>]*seetickets-list-event-container[^>]*>(.*?)'
                    r'(?=<div[^>]*seetickets-list-event-container|<ul class="seetickets-list-view-pagination|\Z)',
                    re.DOTALL)
_IMG = re.compile(r'<img[^>]+src="([^"]+)"[^>]*seetickets-list-view-event-image')
_TITLE_A = re.compile(r'<p class="[^"]*\btitle"><a href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_FIELD = r'<p class="[^"]*\b{}">(.*?)</p>'
_SPAN = r'<span class="{}">(.*?)</span>'
_TAG = re.compile(r"<[^>]+>")
_TRAILING_DATE = re.compile(r"\s*(?:-\s*)?\d{1,2}/\d{1,2}(?:\s*&\s*\d{1,2}/\d{1,2})*\s*$")
_TBA = re.compile(r"^(?:and\s+)?(?:a\s+)?(?:secret|special)\s+guests?\b.*$|^tba$|^more$",
                  re.IGNORECASE)
# genres See Tickets gives a night that bills no bands
_NO_BANDS = {"dj/dance", "karaoke", "other content"}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment))).strip()


def _field(block: str, name: str, pat: str = _FIELD) -> str:
    m = re.search(pat.format(re.escape(name)), block, re.DOTALL)
    return _text(m.group(1)) if m else ""


def _day(label: str, today: date) -> date | None:
    """"Sat Sep 26" -> the date in whichever of this year and next is a Saturday."""
    try:
        wd, rest = label.split(" ", 1)
    except ValueError:
        return None
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            d = datetime.strptime(f"{rest} {year}", "%b %d %Y").date()
        except ValueError:
            continue
        if d.strftime("%a") == wd[:3]:
            return d
    return None


def _time(s: str) -> str | None:
    """"Show at 8:00PM" -> "8pm", The List's style."""
    m = re.search(r"(\d{1,2}(?::\d\d)?\s*[AP]M)", s, re.IGNORECASE)
    return m.group(1).replace(" ", "").lower().replace(":00", "") if m else None


def _price(s: str) -> str | None:
    """"$12.00-$15.00" -> "$12-$15"; "$0.00" (karaoke) -> "free"."""
    s = s.replace(".00", "")
    return "free" if s == "$0" else s or None


def _bands(names: str) -> list[str]:
    out = []
    for b in names.split(","):
        b = re.sub(r"^\s*(?:and|with)\s+", "", b.strip(), flags=re.IGNORECASE)
        if b and not _TBA.match(b):
            out.append(b)
    return out


def _age(s: str) -> str | None:
    """"All Ages" -> "a/a", as The List writes it; "21+" stays."""
    return "a/a" if re.fullmatch(r"all ages", s.strip(), re.IGNORECASE) else s or None


def parse_page(page: str, today: date, venue: str = VENUES["storkclub"][1],
               source: str = "storkclub") -> list[Show]:
    """One page of the list view -> its shows (empty past the last page)."""
    shows = []
    for block in _BLOCK.findall(page):
        t = _TITLE_A.search(block)
        day = _day(_field(block, "date"), today)
        if not t or day is None:
            continue
        url, title = html.unescape(t.group(1)), _TRAILING_DATE.sub("", _text(t.group(2)))
        if re.match(r"closed for (?:a )?private event", title, re.IGNORECASE) or \
                re.search(r"\b\d+-day pass", title, re.IGNORECASE):   # a ticket, not a show
            continue
        genre = _field(block, "genre")
        bands = _bands(_field(block, "headliners"))
        support = re.sub(r"^Supporting Talent:\s*", "", _field(block, "supporting-talent"))
        bands += [b for b in _bands(support) if b not in bands]
        if not bands and genre.lower() not in _NO_BANDS and "," in title:
            bands = _bands(title)       # a lineup the site forgot to mark up as one
        notes = [x for x in (_field(block, "header"), _field(block, "subtitle")) if x]
        notes = [n for n in notes if _loose(n) != _loose(support)]
        if genre:
            notes.append(genre)
        if "button-soldout" in block:
            notes.append("sold out")
        img = _IMG.search(block)
        shows.append(Show(
            day=day, venue=venue, bands=bands,
            age=_age(_field(block, "ages", _SPAN)),
            price=_price(_field(block, "price", _SPAN)),
            times=_time(_field(block, "doortime-showtime")),
            notes=notes, source=source, source_url=url,
            title="" if bands else title,
            flyer=html.unescape(img.group(1)) if img else "", tickets=url))
    return shows


def _once(shows: list[Show]) -> list[Show]:
    """The same night listed twice ("HOT GOTH NIGHT: HALLOWEEN" / "Hot Goth
    Halloween"): keep the first of a night's band-less entries whose names
    share their first word."""
    seen: set[tuple] = set()
    out = []
    for s in shows:
        k = (s.day, _loose(s.headliner) if s.bands else "~" + _loose((s.title.split() or [""])[0]))
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


class SeeTickets:
    """One venue's See Tickets calendar; `SeeTickets("gamh")` etc."""

    def __init__(self, name: str = "storkclub", timeout: float = 15.0,
                 today: date | None = None):
        self.name = name
        self.url, self.venue = VENUES[name]
        self.host = self.url.split("/")[2]
        self.timeout = timeout
        self.today = today

    def _get(self, page: int) -> str:
        try:
            resp = requests.get(self.url, params={"list1page": page}, timeout=self.timeout,
                                headers={"User-Agent": "twiddle scene"})
        except requests.RequestException as exc:
            raise SourceError(f"could not reach {self.host}: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"{self.host} returned HTTP {resp.status_code} (page {page})")
        return resp.text

    def fetch(self) -> list[Show]:
        # A page that fails raises, rather than returning the pages before it:
        # the cached listing then stands in whole, instead of losing a month.
        today = self.today or date.today()
        shows: list[Show] = []
        for page in range(1, MAX_PAGES + 1):
            got = parse_page(self._get(page), today, self.venue, self.name)
            if not got or any(g.tickets == s.tickets for g in got[:1] for s in shows):
                break           # past the end (or served page 1 again)
            shows += got
        if not shows:
            raise SourceError(f"{self.host}'s calendar parsed to zero shows -- "
                              "its format may have changed")
        return _once(shows)
