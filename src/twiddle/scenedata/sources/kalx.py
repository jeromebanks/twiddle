"""KALX 90.7 (UC Berkeley) weekly event calendar, kalx.berkeley.edu.

The station posts one "Events: September 28 - October 4, 2026" page a week
(a WordPress `event` post; measured 2026-09-29). The post is one long
listing: a day heading with the year ("Monday September 28, 2026"), a region
("East Bay", "San Francisco"), then a line per room, `Venue: act, act, act`.
About 75 rooms across the two weeks measured, including some no other source
carries (Sweetwater Music Hall, Hillside Club). What it does *not* have is a
time, a price, a flyer or a ticket link, so it goes last in the registry: where
another source knows the night, its facts win and this only confirms it.

Read through the site's REST API, which lists the recent posts in one request
with their rendered HTML, rather than by guessing next week's URL.

Two things are guessed, and said so:

* **Which entries are bands.** "Open Mic", "Irish Céili Dance with live
  band" and "Karaokiki" are nights, not acts. An entry that reads like one
  is kept by its name (`Show.title`), not split into bands that every lookup
  would then chase. "Name: act, act" is a named night with a lineup. The
  rest is split on commas ("featuring" dropped). A wrong guess costs a
  lookup that finds nothing, or a lineup shown as a title.
* **Which city.** Only a region heading is given, and it is loose
  (Sweetwater, in Mill Valley, sits under "San Francisco"). A room is spelled
  "Name, City" as the watched-venue matching expects, using the city where it
  matters (`CITY`), else the region.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta

import requests

from ...scenespec.model import Show
from .base import SourceError

API = "https://kalx.berkeley.edu/wp-json/wp/v2/event"
PARAMS = {"per_page": 4, "orderby": "date", "order": "desc",
          "_fields": "link,date,title,content"}
REGIONS = {"san francisco": "S.F.", "east bay": "East Bay"}

# KALX drops the city. These are the rooms whose watched-venue match strings
# need one (or whose name KALX shortens), and the two added for issue #2.
CITY = {"cornerstone": "Berkeley", "fox theater": "Oakland", "freight": "Berkeley",
        "greek theater": "Berkeley", "uc theater": "Berkeley", "uc theatre": "Berkeley",
        "crybaby": "Oakland", "spats": "Berkeley", "hillside club": "Berkeley",
        "sweetwater music hall": "Mill Valley"}
SPELLED = {"regency": "Regency Ballroom, S.F.", "paramount": "Paramount Theatre, Oakland"}

# A <p> may be left unclosed: the last one of each post ends at `</div>`
# (measured 2026-09-29), and dropping it lost every Sunday's S.F. section.
_TOKEN = re.compile(r"<h2>(.*?)</h2>|<h3>(.*?)</h3>|<p>(.*?)(?=</p>|<h2>|<h3>|<p>|</div>|\Z)",
                    re.DOTALL)
_LINE = re.compile(r"\s*<strong>(.*?)</strong>(.*)", re.DOTALL)
_DAY = re.compile(r"([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?")
_TAG = re.compile(r"<[^>]+>")
NOT_BANDS = re.compile(
    r"\b(?:open\s+(?:mic|jam|bluegrass)|jams?|slam|storyslam|karaok\w*|dance|dancing|nights?|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)days|singalong|sing-along|presents?|"
    r"conversation|monthly|honou?ring|birthday|bingo|trivia|poetry|comedy|screening|"
    r"workshop|class|lecture|reading|session)\b", re.IGNORECASE)
MAX_WEEKS_AHEAD = timedelta(days=45)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", fragment)).replace("\xa0", " ")).strip()


def _key(name: str) -> str:
    low = name.lower().replace("’", "'").replace("é", "e").strip()
    return re.sub(r"^(?:the|thee)\s+", "", low)


def room(name: str, region: str) -> str:
    """A room as "Name, City", so `venues.find` recognises the watched ones."""
    key = _key(name)
    if key in SPELLED:
        return SPELLED[key]
    where = CITY.get(key) or REGIONS.get(region.lower(), region)
    return f"{name}, {where}" if where else name


def lineup(text: str) -> tuple[list[str], str]:
    """(bands, title) for what follows a room's name. See the module docstring."""
    head, sep, tail = text.partition(": ")
    if sep and "," in tail:                     # "KALW's Birthday: Rozzi, DEATHX_XHEAD, DJ X"
        return _acts(tail), head.strip()
    if NOT_BANDS.search(text) or sep:           # "Open Mic", "Critic to Critic: a conversation"
        return [], text
    return _acts(text), ""


def _acts(text: str) -> list[str]:
    acts = [re.sub(r"^featuring\s+", "", a.strip(), flags=re.IGNORECASE) for a in text.split(",")]
    return [a for a in acts if a]


def _heading_day(text: str, posted: date) -> date | None:
    """"Monday September 28, 2026" -> the date. A heading with no year takes the
    first such date on or after the post's own."""
    parts = text.split(None, 1)
    m = _DAY.search(parts[-1]) if parts else None
    if not m:
        return None
    for fmt in ("%B", "%b"):
        try:
            month = datetime.strptime(m.group(1), fmt).month
            break
        except ValueError:
            continue
    else:
        return None
    try:
        if m.group(3):
            return date(int(m.group(3)), month, int(m.group(2)))
        for year in (posted.year, posted.year + 1):
            d = date(year, month, int(m.group(2)))
            if d >= posted - timedelta(days=7):
                return d
    except ValueError:
        return None
    return None


def parse_post(content: str, url: str, posted: date) -> tuple[list[Show], int]:
    """(shows, days seen) for one weekly post. A page with no day headings at
    all is a format change, and the caller says so."""
    shows: list[Show] = []
    day: date | None = None
    region = ""
    seen = 0
    for h2, h3, p in _TOKEN.findall(content):
        if h2:
            day = _heading_day(_text(h2), posted)
            seen += day is not None
            region = ""
        elif h3:
            region = _text(h3)
        elif p and day is not None:
            for line in re.split(r"<br\s*/?>", p):
                m = _LINE.match(line)
                if not m:
                    continue
                name = _text(m.group(1)).rstrip(":").strip()
                rest = _text(m.group(2))
                if not name or not rest:
                    continue
                bands, title = lineup(rest)
                shows.append(Show(day=day, venue=room(name, region), bands=bands, title=title,
                                  source="kalx", source_url=url))
    return shows, seen


def parse_posts(posts: list[dict], today: date) -> list[Show]:
    shows: list[Show] = []
    days = 0
    for post in posts:
        try:
            posted = date.fromisoformat(str(post.get("date", ""))[:10])
        except ValueError:
            posted = today
        found, seen = parse_post((post.get("content") or {}).get("rendered", ""),
                                 post.get("link", ""), posted)
        days += seen
        shows += found
    if not days:
        raise SourceError("KALX's events posts had no day headings -- its format may have changed")
    return [s for s in shows if today <= s.day <= today + MAX_WEEKS_AHEAD]


class KALX:
    name = "kalx"

    def __init__(self, url: str = API, timeout: float = 20.0, today: date | None = None):
        self.url = url
        self.timeout = timeout
        self.today = today

    def fetch(self) -> list[Show]:
        try:
            resp = requests.get(self.url, params=PARAMS, timeout=self.timeout,
                                headers={"User-Agent": "Mozilla/5.0 (Macintosh) twiddle scene"})
        except requests.RequestException as exc:
            raise SourceError(f"could not reach kalx.berkeley.edu: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"kalx.berkeley.edu returned HTTP {resp.status_code}")
        try:
            posts = resp.json()
        except ValueError as exc:
            raise SourceError("kalx.berkeley.edu's event list was not JSON") from exc
        if not isinstance(posts, list):
            raise SourceError("kalx.berkeley.edu's event list had an unexpected shape")
        return parse_posts(posts, self.today or date.today())
