"""Venues whose calendar is VenuePilot's widget: Ivy Room, Ashkenaz.

The widget asks a public GraphQL endpoint (`venuepilot.co/graphql`) for an
account's events: no key, no login, JSON back. The account id is in the
venue page's `window.venuepilotSettings` (measured 2026-09-26: Ivy Room
992, 54 events over ~3 months, every one with a flyer; Ashkenaz 1228).
Of the 66 accounts with upcoming events among ids 1-3200, those two were
the only Bay Area rooms. The query below is the widget's own
`paginatedEvents`, trimmed to the fields used.

Measured quirks, each handled below:
- `artists` is unreliable: the venue copies events, and Oct 4 carried Oct
  3's four artists. Bands come from the event `name` instead ("SLEEPBOMB +
  THEYA + OMINESS"), split on " + " and commas.
- `support` is free text: often "genre - gothic doom, black metal", but
  also a presenter or the rest of the bill. It becomes a note.
- There is no price field; the `description` HTML usually says it
  ("Advance Tickets Available / $18 Door"), so the dollar amounts are read
  from there.
- Weekly nights (Happy Hour, line dancing, BandWorks) name no bands and
  have no artists: kept by name, like the Stork Club's DJ nights. Private
  parties are dropped.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime

import requests

from ...scene.model import Show
from .base import SourceError

ENDPOINT = "https://www.venuepilot.co/graphql"
# source name -> (account id, the venue as a watched venue matches it, its site)
VENUES = {
    "ivyroom": (992, "Ivy Room, Albany", "https://www.ivyroom.com/"),
    "ashkenaz": (1228, "Ashkenaz, Berkeley", "https://www.ashkenaz.com/"),
}
PER_PAGE = 50
MAX_PAGES = 10

QUERY = """
query ($accountIds: [Int!]!, $startDate: String!, $limit: Int, $page: Int) {
  paginatedEvents(arguments: {accountIds: $accountIds, startDate: $startDate,
                              limit: $limit, page: $page}) {
    collection {
      id name date doorTime startTime minimumAge support description status ticketsUrl
      announceImages { highlighted versions { cover { src } } }
      artists { name }
    }
    metadata { totalPages }
  }
}"""

_TAG = re.compile(r"<[^>]+>")
_SPLIT = re.compile(r"\s+\+\s+|\s*,\s*")
_DOLLARS = re.compile(r"\$\s?(\d+(?:\.\d\d)?)")
_EXTRA = re.compile(r"\s*\((?:[^)]*)\)\s*|\s+-\s+(?:record release|food by .*)$", re.IGNORECASE)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment or ""))).strip()


def _clock(hms: str | None) -> str | None:
    """"19:30:00" -> "7:30pm", The List's style."""
    if not hms:
        return None
    t = datetime.strptime(hms, "%H:%M:%S")
    return t.strftime("%-I:%M%p").lower().replace(":00", "")


def _times(door: str | None, show: str | None) -> str | None:
    d, s = _clock(door), _clock(show)
    if s == "12am":             # "00:00:00": no show time was entered
        s = None
    return "/".join(dict.fromkeys(x for x in (d, s) if x)) or None


def _price(description: str) -> str | None:
    """Every distinct dollar amount the blurb mentions: "$15/$18"."""
    amounts = [a.replace(".00", "") for a in _DOLLARS.findall(_text(description))]
    return "/".join("$" + a for a in dict.fromkeys(amounts)) or None


def _flyer(images: list[dict] | None) -> str:
    images = sorted(images or [], key=lambda i: not i.get("highlighted"))
    for im in images:
        src = ((im.get("versions") or {}).get("cover") or {}).get("src")
        if src:
            return src
    return ""


def split_name(name: str) -> tuple[list[str], list[str]]:
    """An event name -> (bands, notes). "BESTIAL MOUTHS (record release) +
    BLOOD RAVE" -> bands ["BESTIAL MOUTHS", "BLOOD RAVE"], note "record release"."""
    notes = [p.strip() for p in re.findall(r"\(([^)]*)\)", name)]
    bands = []
    for part in _SPLIT.split(name):
        part = _EXTRA.sub(" ", part).strip(" -")
        if part:
            bands.append(part)
    return bands, notes


def to_show(ev: dict, source: str = "ivyroom") -> Show | None:
    name = _text(ev.get("name"))
    if not name or "private" in name.lower():
        return None
    day = date.fromisoformat(ev["date"])
    bands, notes = split_name(name)
    if not ev.get("artists") and " + " not in name:
        bands = []      # "Happy Hour - No Fuss, just good times!", "BandWorks"
    support = _text(ev.get("support"))
    if support:
        notes.append(re.sub(r"^genre\s*-\s*", "", support, flags=re.IGNORECASE))
    status = (ev.get("status") or "").strip()
    if "sold out" in status.lower():
        notes.append("sold out")
    age = ev.get("minimumAge")
    tickets = ev.get("ticketsUrl") or ""
    _, venue, site = VENUES[source]
    return Show(day=day, venue=venue, bands=bands, age=f"{age}+" if age else None,
                price=_price(ev.get("description") or "") or
                ("free" if re.search(r"\bfree\b|no cover", status, re.IGNORECASE) else None),
                times=_times(ev.get("doorTime"), ev.get("startTime")),
                notes=notes, source=source, source_url=tickets or site,
                title="" if bands else name, flyer=_flyer(ev.get("announceImages")),
                tickets=tickets)


class VenuePilot:
    """One venue's VenuePilot account; `VenuePilot("ashkenaz")` etc."""

    def __init__(self, name: str = "ivyroom", timeout: float = 20.0, today: date | None = None):
        self.name = name
        self.account, self.venue, self.site = VENUES[name]
        self.timeout = timeout
        self.today = today

    def _page(self, page: int) -> dict:
        body = {"query": QUERY, "variables": {
            "accountIds": [self.account], "startDate": (self.today or date.today()).isoformat(),
            "limit": PER_PAGE, "page": page}}
        try:
            resp = requests.post(ENDPOINT, json=body, timeout=self.timeout,
                                 headers={"User-Agent": "twiddle scene",
                                          "Origin": self.site.rstrip("/")})
        except requests.RequestException as exc:
            raise SourceError(f"could not reach venuepilot.co: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"venuepilot.co returned HTTP {resp.status_code}")
        data = resp.json()
        if data.get("errors") or not (data.get("data") or {}).get("paginatedEvents"):
            raise SourceError(f"venuepilot.co refused the query: {data.get('errors')}")
        return data["data"]["paginatedEvents"]

    def fetch(self) -> list[Show]:
        shows: list[Show] = []
        page, pages = 1, 1
        while page <= min(pages, MAX_PAGES):
            got = self._page(page)
            pages = (got.get("metadata") or {}).get("totalPages") or 1
            shows += [s for s in (to_show(ev, self.name) for ev in got.get("collection") or []) if s]
            page += 1
        if not shows:
            raise SourceError(f"{self.venue}'s VenuePilot calendar returned zero shows -- "
                              "its format may have changed")
        return shows
