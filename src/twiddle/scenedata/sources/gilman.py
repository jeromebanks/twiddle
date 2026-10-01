"""924 Gilman (Berkeley) from its ShowSlinger ticket widget.

Gilman's own calendar page is a dead Wix widget ("App Unavailable"), and
ShowSlinger's venue page asks for a login -- but only without its token:
the link on Gilman's homepage, `/e1/460/924-gilman/8c96699fd6`, is public
and server-rendered (measured 2026-09-26; the tokenless URL redirects to a
sign-in page, which is why an earlier session wrote this source off).

What it adds over The List, which already covers Gilman's dates well: each
show's flyer (the `thumb_` prefix dropped gives the full-size image) and
its ticket link. So its lineups only need to be good enough to match The
List's (`model.dedupe`), whose bands, ages and prices win.

Measured quirks, each handled below:
- Every price read "$5" while The List gave $10-$30 for the same nights:
  it is not the ticket price, so it is not recorded.
- Titles are free text: "Worst Party Ever + Equipment + Ogbert The Nerd",
  "Burndown Tour 2026: Febuary, Love Letter, Stella & more", "Doll Fest
  Presents: We Are Actually Funny (Benefit)". "Salt +" is a band.
- A weekend is one event with one title and several dates, and its nights
  have different lineups, so a run gets no bands, only its name.
- The date shows no year ("Oct  2"); the event's class carries it
  ("mrk_filter_event_by_venue 2102026" is 2 Oct 2026).
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime

import requests

from ...scene.model import Show
from .base import SourceError

BASE = "https://app.showslinger.com"
WIDGET = BASE + "/e1/460/924-gilman/8c96699fd6"
VENUE = "924 Gilman, Berkeley"

_EVENT = re.compile(r'mrk_filter_event_by_venue (\d{6,8}) list-layout[^"]*"(.*?)'
                    r'(?=mrk_filter_event_by_venue \d{6,8} list-layout|id="widget-grid"|\Z)',
                    re.DOTALL)
_IMG = re.compile(r'<img[^>]+src="([^"]+)"')
_MONTH_DAY = re.compile(r'widget-date-month[^"]*">\s*([A-Z][a-z]{2})\s+(\d{1,2})\s*<')
_NAME = re.compile(r'<h4 class="widget-name[^"]*">(.*?)</h4>', re.DOTALL)
_TIMES = re.compile(r'<span class="[^"]*widget-time[^"]*">(.*?)</span>', re.DOTALL)
_TICKETS = re.compile(r'<a class="[^"]*mrk_ticket_event_url[^"]*" href="([^"]+)"')
_TAG = re.compile(r"<[^>]+>")
# "Doll Fest Presents: X" is presented by Doll Fest; "Burndown Tour 2026: X" is a tour
_PRESENTS = (re.compile(r"^(.+?\bpresents)\s*:?\s+(.+)$", re.IGNORECASE),
             re.compile(r"^(.+?\b(?:tour \d{4}|weekend|fest))\s*[:!]\s*(.+)$", re.IGNORECASE))
_SPLIT = re.compile(r"\s*,\s*|\s+\+\s+|\s+&\s+")
_MORE = re.compile(r"^(?:and\s+)?(?:more|friends|special guests?)!?$", re.IGNORECASE)
# a night named for what it is, not who plays
_EVENTISH = re.compile(r"\b(?:benefit|fest|meeting|yoga|night|party|weekend|tour)\b",
                       re.IGNORECASE)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment))).strip()


def _time(s: str) -> str:
    """"6:30 PM" -> "6:30pm", "7:00 pm" -> "7pm": The List's style."""
    return s.replace(" ", "").lower().replace(":00", "")


def split_title(title: str) -> tuple[list[str], list[str]]:
    """A billing -> (bands, notes). Empty bands when it names an event, not acts."""
    notes: list[str] = []
    t = title.strip()
    for paren in re.findall(r"\(([^)]*)\)", t):
        notes.append(paren.strip())
    t = re.sub(r"\s*\([^)]*\)", "", t).strip()
    pre = _PRESENTS[0].match(t) or _PRESENTS[1].match(t)
    if pre:
        notes.append(pre.group(1).strip())
        t = pre.group(2)
    if " - " in t:
        t, sub = t.split(" - ", 1)
        notes.append(sub.strip())
    parts = [p.strip(" !") for p in _SPLIT.split(t)]
    bands = [p for p in parts if p and not _MORE.match(p)]
    if len(bands) == 1 and _EVENTISH.search(bands[0]) and not pre:
        return [], notes
    return bands, notes


def parse_widget(page: str) -> list[Show]:
    shows = []
    for token, block in _EVENT.findall(page):
        name = _NAME.search(block)
        md = _MONTH_DAY.search(block)
        if not name or not md:
            continue
        title = _text(name.group(1))
        img = _IMG.search(block)
        flyer = html.unescape(img.group(1)).replace("/thumb_", "/") if img else ""
        if flyer.startswith("/"):
            flyer = ""                              # ShowSlinger's placeholder
        tickets = _TICKETS.search(block)
        link = BASE + html.unescape(tickets.group(1)).split("?")[0] if tickets else WIDGET
        times = [_text(t) for t in _TIMES.findall(block)]
        # A run lists each night in full: "Sat, September 26, 7:30 pm".
        nights = []
        for t in times:
            m = re.match(r"[A-Z][a-z]{2}, ([A-Z][a-z]+ \d{1,2}), (\d{1,2}(?::\d\d)? [ap]m)", t)
            if m:
                d = datetime.strptime(f"{m.group(1)} {token[-4:]}", "%B %d %Y").date()
                nights.append((d, _time(m.group(2))))
        if not nights:
            d = datetime.strptime(f"{md.group(1)} {md.group(2)} {token[-4:]}", "%b %d %Y").date()
            nights = [(d, _time(times[0]) if times else None)]
        bands, notes = split_title(title)
        if len(nights) > 1:
            bands, notes = [], []
        for d, when in nights:
            shows.append(Show(day=d, venue=VENUE, bands=list(bands), age="a/a", times=when,
                              notes=list(notes), source="gilman", source_url=link,
                              title="" if bands else title, flyer=flyer, tickets=link))
    return shows


class Gilman:
    name = "gilman"

    def __init__(self, url: str = WIDGET, timeout: float = 15.0):
        self.url = url
        self.timeout = timeout

    def fetch(self) -> list[Show]:
        try:
            resp = requests.get(self.url, timeout=self.timeout,
                                headers={"User-Agent": "twiddle scene"})
        except requests.RequestException as exc:
            raise SourceError(f"could not reach showslinger.com: {exc}") from exc
        if resp.status_code != 200 or "sign_in" in resp.url:
            raise SourceError(f"Gilman's ShowSlinger page answered HTTP {resp.status_code} "
                              f"at {resp.url} -- its public link may have changed")
        shows = parse_widget(resp.text)
        if not shows:
            raise SourceError("Gilman's ShowSlinger page parsed to zero shows -- "
                              "its format may have changed")
        return shows
