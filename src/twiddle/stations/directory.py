"""Find stations in public directories, to add to the catalog (read-only).

- **radio-browser.info**: the big one. Free, no key, community-maintained.
  Good for *finding* a station and a working stream; its tags are often
  empty and its URL is not always the best one (its KEXP is a 64k stream),
  so `probe` the stream and check the station's own site before trusting it.
- **SomaFM** `channels.json`: every Soma channel. Each maps straight onto
  the `somafm` fetcher, so these are the easiest adds there are.
- **TuneIn** OPML: a second opinion on the stream URL, which is how KXSF's
  was found when its own site linked a dead one.

Each result says whether the catalog already has it (same stream, or a
name that matches a catalog station's).
"""
from __future__ import annotations

import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from . import net

SOURCES = ("radio-browser", "somafm", "tunein")
RADIO_BROWSER_FALLBACK = "de1.api.radio-browser.info"


@dataclass
class Found:
    source: str
    name: str
    url: str | None                  # a stream URL, when the directory gave one
    homepage: str | None = None
    logo: str | None = None
    about: str | None = None
    tags: list[str] = field(default_factory=list)      # the directory's, not ours
    country: str | None = None
    codec: str | None = None
    bitrate: int | None = None
    votes: int | None = None
    ok: bool | None = None           # the directory's own last check
    fetch: str | None = None         # a fetcher this maps onto for certain
    in_catalog: str | None = None    # "kexp (same stream)" / "kexp (same name?)"

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}

    def render(self) -> str:
        fmt = " ".join(x for x in (self.codec, f"{self.bitrate}k" if self.bitrate else None) if x)
        head = f"{self.name}  [{self.source}]" + (f"  {fmt}" if fmt else "") \
            + (f"  {self.country}" if self.country else "") \
            + (f"  votes {self.votes}" if self.votes else "") \
            + ("  (directory says: broken)" if self.ok is False else "")
        lines = [head]
        if self.in_catalog:
            lines.append(f"    ALREADY IN CATALOG: {self.in_catalog}")
        for label, v in (("stream", self.url), ("site", self.homepage), ("about", self.about),
                         ("tags", ", ".join(self.tags[:8])), ("logo", self.logo),
                         ("fetch", self.fetch)):
            if v:
                lines.append(f"    {label:7}{v}")
        return "\n".join(lines)


# ---- matching against the catalog ---------------------------------------------


def _bare(url: str | None) -> str:
    url = (url or "").split("?")[0].rstrip("/")
    return re.sub(r"^[a-z0-9+-]+://", "", url)


def _word(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# First words too common to identify a station: "R/a/dio" reduces to "radio",
# which would otherwise claim every "Radio Paradise ..." and "Radio 80".
_GENERIC = {"radio", "the", "classic", "music", "live", "online"}


def mark_catalog(found: list[Found], catalog: dict) -> list[Found]:
    by_url = {_bare(s.url): k for k, s in catalog.items()}
    for f in found:
        if f.url and _bare(f.url) in by_url:
            f.in_catalog = f"{by_url[_bare(f.url)]} (same stream)"
            continue
        first = _word(f.name.split()[0]) if f.name.split() else ""
        for k, s in catalog.items():
            # "KALX 90.7FM Berkeley" is KALX; "Groove Salad Classic" is not Groove Salad.
            if _word(f.name) == _word(s.name) or (first and first == _word(s.name)
                                                  and len(first) >= 4
                                                  and first not in _GENERIC):
                f.in_catalog = f"{k} (same name?)"
                break
    return found


# ---- radio-browser ------------------------------------------------------------------


def parse_radio_browser(rows: list[dict]) -> list[Found]:
    out = []
    for r in rows:
        out.append(Found(
            "radio-browser", (r.get("name") or "").strip(),
            r.get("url_resolved") or r.get("url") or None,
            homepage=r.get("homepage") or None, logo=r.get("favicon") or None,
            tags=[t.strip() for t in (r.get("tags") or "").split(",") if t.strip()],
            country=r.get("countrycode") or r.get("country") or None,
            codec=r.get("codec") or None, bitrate=r.get("bitrate") or None,
            votes=r.get("votes"), ok=bool(r["lastcheckok"]) if "lastcheckok" in r else None,
            fetch=fetch_for_url(r.get("url_resolved") or r.get("url") or "")))
    return out


def _radio_browser_hosts() -> list[str]:
    try:
        hosts = [h["name"] for h in net.get_json("https://all.api.radio-browser.info/json/servers")]
    except Exception:
        hosts = []
    return list(dict.fromkeys(hosts + [RADIO_BROWSER_FALLBACK]))


def search_radio_browser(text: str, *, genre: str | None = None, country: str | None = None,
                         limit: int = 10) -> list[Found]:
    q = {"hidebroken": "true", "order": "votes", "reverse": "true", "limit": str(limit)}
    if text:
        q["name"] = text
    if genre:
        q["tag"] = genre
    if country:
        q["countrycode"] = country.upper()
    last: Exception | None = None
    for host in _radio_browser_hosts():
        try:
            rows = net.get_json(f"https://{host}/json/stations/search?{urllib.parse.urlencode(q)}")
            return parse_radio_browser(rows)
        except Exception as exc:  # one mirror down: try the next
            last = exc
    raise RuntimeError(f"radio-browser: no mirror answered ({last})")


# ---- SomaFM ----------------------------------------------------------------------------


def somafm_stream(channel_id: str) -> str:
    # Plain http, and the shape the `somafm` fetcher reads the channel from.
    return f"http://ice1.somafm.com/{channel_id}-128-mp3"


def parse_somafm(data: dict, text: str = "", genre: str | None = None) -> list[Found]:
    words = [w for w in text.lower().split() if w]
    out = []
    for c in data.get("channels", []):
        hay = " ".join(str(c.get(k, "")) for k in ("id", "title", "description", "genre")).lower()
        if words and not all(w in hay for w in words):
            continue
        if genre and genre.lower() not in (c.get("genre") or "").lower():
            continue
        out.append(Found("somafm", c.get("title") or c["id"], somafm_stream(c["id"]),
                         homepage=f"https://somafm.com/{c['id']}/",
                         logo=c.get("xlimage") or c.get("largeimage") or None,
                         about=c.get("description") or None,
                         tags=(c.get("genre") or "").replace("|", ",").split(","),
                         country="US", codec="MP3", bitrate=128,
                         votes=int(c["listeners"]) if str(c.get("listeners", "")).isdigit() else None,
                         fetch="somafm"))
    out.sort(key=lambda f: -(f.votes or 0))
    return out


def search_somafm(text: str, *, genre: str | None = None, limit: int = 10) -> list[Found]:
    return parse_somafm(net.get_json("https://somafm.com/channels.json"), text, genre)[:limit]


# ---- TuneIn ------------------------------------------------------------------------------


def parse_tunein(data: dict) -> list[Found]:
    out = []
    for x in data.get("body", []):
        if x.get("type") != "audio" or x.get("item") != "station":
            continue
        out.append(Found("tunein", x.get("text") or "", None,
                         logo=x.get("image") or None, about=x.get("subtext") or None,
                         codec=(x.get("formats") or "").upper() or None,
                         bitrate=int(x["bitrate"]) if str(x.get("bitrate", "")).isdigit() else None,
                         homepage=f"https://tunein.com/radio/{x['guide_id']}/"
                         if x.get("guide_id") else None,
                         tags=[x["guide_id"]] if x.get("guide_id") else []))
    return out


def parse_tunein_streams(data: dict) -> list[str]:
    """Tune.ashx's streams, most reliable first."""
    rows = [x for x in data.get("body", []) if x.get("url")]
    rows.sort(key=lambda x: -(x.get("reliability") or 0))
    return [x["url"] for x in rows]


def search_tunein(text: str, *, limit: int = 10) -> list[Found]:
    q = urllib.parse.urlencode({"query": text, "render": "json"})
    found = parse_tunein(net.get_json(f"https://opml.radiotime.com/Search.ashx?{q}"))[:limit]

    def resolve(f: Found) -> None:
        gid = f.tags[0] if f.tags else None
        f.tags = []
        if not gid:
            return
        try:
            streams = parse_tunein_streams(net.get_json(
                f"https://opml.radiotime.com/Tune.ashx?id={gid}&render=json"))
        except Exception:
            return
        if streams:
            f.url = streams[0]
            f.fetch = fetch_for_url(f.url)
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(resolve, found))
    return found


# ---- all of them -----------------------------------------------------------------------------


def fetch_for_url(url: str) -> str | None:
    """A fetcher the stream URL alone settles (see also probe.py)."""
    if re.search(r"somafm\.com/[a-z0-9]+-", url):
        return "somafm"
    if "ntslive" in url:
        return "nts"
    if "radiofrance.fr" in url:
        return "radiofrance"
    return None


def search(text: str, *, source: str | None = None, genre: str | None = None,
           country: str | None = None, limit: int = 10,
           catalog: dict | None = None) -> tuple[list[Found], list[str]]:
    """Results from each source asked (all by default), and any errors."""
    jobs = {"radio-browser": lambda: search_radio_browser(text, genre=genre, country=country,
                                                          limit=limit),
            "somafm": lambda: search_somafm(text, genre=genre, limit=limit),
            "tunein": lambda: search_tunein(text, limit=limit)}
    if genre or country:
        jobs.pop("tunein")          # it filters by neither
    if country and country.upper() != "US":
        jobs.pop("somafm")
    # TuneIn's search is fuzzy (ask for "kexp", get KCSB): only when asked for,
    # to resolve a stream the others lack.
    names = [source] if source else [n for n in jobs if n != "tunein"]
    found: list[Found] = []
    errors: list[str] = []
    with ThreadPoolExecutor(3) as pool:
        futures = {n: pool.submit(jobs[n]) for n in names if n in jobs}
        for n, fut in futures.items():
            try:
                found += fut.result()
            except Exception as exc:
                errors.append(f"{n}: {type(exc).__name__}: {exc}")
    if catalog is not None:
        mark_catalog(found, catalog)
    return found, errors

