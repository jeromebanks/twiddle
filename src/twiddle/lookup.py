"""Who is this? -- artist and album background from open music databases.

Three free sources, no API keys, chained:

1. **MusicBrainz** identifies the artist (and album) and supplies the facts:
   type, origin, active years, genres, and links out to Bandcamp, Discogs,
   the official site and so on.
2. **Wikidata**, reached through MusicBrainz's `wikidata` relation, maps that
   exact entity to its English Wikipedia article.
3. **Wikipedia**'s REST summary supplies the readable paragraph.

The hard part is not fetching, it is **identifying the right artist**. A
radio play of "Spoon" is not Jam & Spoon, and the band called Low is not the
David Bowie album. So identity is settled from the strongest evidence
available, in this order:

- MusicBrainz ids handed over by the station (KEXP does this) -- exact.
- artist + album, via a release-group search -- the credited artist on the
  matching album is the right one, and the album details come for free.
- artist + song, via a recording search -- same idea, one level down.
- the name alone, accepted only when exactly one artist has that exact name;
  otherwise the candidates are returned rather than a guess.

Wikipedia is only ever reached *through* the identified entity's Wikidata
link, never by searching Wikipedia for the name -- that is precisely how you
get the wrong Spoon. No link means no paragraph, and the MusicBrainz facts
and links stand on their own.

MusicBrainz asks for at most one request per second and a descriptive
User-Agent; both are honoured below. Results are cached on disk for a month,
so asking twice about the same record is instant.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

USER_AGENT = "twiddle/0.1 (+https://github.com/jeromebanks/twiddle)"
MB = "https://musicbrainz.org/ws/2"
TIMEOUT = 10
CACHE_FILE = Path.home() / ".cache" / "twiddle" / "lookup.json"
CACHE_TTL_S = 30 * 86400
MIN_SCORE = 90  # MusicBrainz search score (0-100) below which a hit is noise

# Links worth showing, in the order worth showing them. MusicBrainz relation
# type -> label; the Wikipedia link is added separately, first.
LINK_TYPES = [
    ("bandcamp", "bandcamp"),
    ("official homepage", "official"),
    ("discogs", "discogs"),
    ("allmusic", "allmusic"),
    ("last.fm", "last.fm"),
]


class LookupFailed(RuntimeError):
    """A source could not be reached or answered nonsense."""


@dataclass
class AlbumInfo:
    title: str
    mbid: str
    year: str | None = None
    kind: str | None = None          # "Album", "EP", "Single", "Album + Live" ...
    genres: list[str] = field(default_factory=list)
    summary: str | None = None
    links: dict[str, str] = field(default_factory=dict)


@dataclass
class ArtistInfo:
    name: str
    mbid: str = ""                   # empty when only Discogs/Bandcamp knew them
    disambiguation: str | None = None
    kind: str | None = None          # Person / Group / Orchestra ...
    origin: str | None = None
    years: str | None = None
    genres: list[str] = field(default_factory=list)
    description: str | None = None   # Wikipedia's one-liner
    summary: str | None = None       # Wikipedia's lead paragraph, else Discogs' profile
    summary_source: str | None = None
    members: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    album: AlbumInfo | None = None
    matched_by: str = ""             # how identity was settled -- say it, don't hide it

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    """An identified artist, or the candidates that made identification unsafe."""
    artist: ArtistInfo | None = None
    candidates: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"artist": self.artist.to_dict() if self.artist else None,
                "candidates": self.candidates}


# ---- transport --------------------------------------------------------------

_last_mb_call = 0.0


def _get_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"}
                                 | (headers or {}))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def _mb(path: str, **params) -> dict:
    """One MusicBrainz call, spaced at least a second from the last.

    MusicBrainz answers 503 when it is rate limiting, and it does so even
    below the documented 1/s when its global budget is spent -- observed on
    the first call of a fresh process. So a 503 is retried with backoff
    rather than reported as "unreachable".
    """
    global _last_mb_call
    url = f"{MB}/{path}?" + urllib.parse.urlencode(params | {"fmt": "json"})
    for attempt in range(4):
        wait = 1.0 - (time.monotonic() - _last_mb_call)
        if wait > 0:
            time.sleep(wait)
        _last_mb_call = time.monotonic()
        try:
            return _get_json(url)
        except urllib.error.HTTPError as exc:
            if exc.code != 503 or attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def _q(s: str) -> str:
    """A Lucene phrase: quoted, with the characters that break the parser escaped."""
    return '"' + re.sub(r'([\\"])', r"\\\1", s) + '"'


def norm(s: str | None) -> str:
    """Loose name equality: case, accents, punctuation, "the" and "&" ignored.

    Radio logs are hand-typed ("In The Aeroplane Over The Sea" against
    MusicBrainz's "In the Aeroplane Over the Sea"), so exact comparison
    would reject the right answer most of the time.

    Only combining marks are stripped, not everything non-ASCII: folding to
    ASCII would turn every Japanese, Korean or Cyrillic name -- and "!!!" --
    into "", and two empty strings compare equal, silently disabling every
    identity check below. `_same` treats an empty norm as never matching.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w ]+|_", " ", s)
    s = re.sub(r"^the\s+", "", s.strip())
    return " ".join(s.split())


def _same(a: str | None, b: str | None) -> bool:
    na = norm(a)
    return bool(na) and na == norm(b)


def _credit_matches(credit: list[dict], artist: str) -> bool:
    joined = "".join(c.get("name", "") + c.get("joinphrase", "") for c in credit)
    names = [joined] + [c.get("name", "") for c in credit]
    return any(_same(n, artist) for n in names)


# ---- identity ----------------------------------------------------------------


def bare_title(title: str) -> str:
    """A title without its edition suffix: Spotify's "Kind Of Blue (Legacy
    Edition)" is MusicBrainz's release group "Kind of Blue"."""
    return re.sub(r"\s*[(\[][^)\]]*(edition|remaster|deluxe|version|expanded|anniversary|mono|stereo)[^)\]]*[)\]]",
                  "", title, flags=re.IGNORECASE).strip() or title


def _by_album(artist: str, album: str) -> tuple[str, dict] | None:
    """(artist mbid, release-group) for this artist's release called `album`."""
    album = bare_title(album)
    hits = _mb("release-group/", query=f"releasegroup:{_q(album)} AND artist:{_q(artist)}",
               limit=5).get("release-groups", [])
    for rg in hits:
        credit = rg.get("artist-credit", [])
        if rg.get("score", 0) >= MIN_SCORE and credit and _credit_matches(credit, artist) \
                and _same(rg.get("title"), album):
            return credit[0]["artist"]["id"], rg
    return None


def _by_song(artist: str, song: str) -> str | None:
    hits = _mb("recording/", query=f"recording:{_q(song)} AND artist:{_q(artist)}",
               limit=5).get("recordings", [])
    for rec in hits:
        credit = rec.get("artist-credit", [])
        if rec.get("score", 0) >= MIN_SCORE and credit and _credit_matches(credit, artist):
            return credit[0]["artist"]["id"]
    return None


def _by_name(artist: str) -> tuple[str | None, list[dict]]:
    """(mbid, []) when exactly one artist has this name, else (None, candidates)."""
    hits = _mb("artist/", query=f"artist:{_q(artist)}", limit=10).get("artists", [])
    exact = [a for a in hits if _same(a.get("name"), artist)
             and a.get("score", 0) >= MIN_SCORE]
    if len(exact) == 1:
        return exact[0]["id"], []
    pool = exact or [a for a in hits if a.get("score", 0) >= MIN_SCORE]
    return None, [{"name": a.get("name"), "mbid": a.get("id"),
                   "disambiguation": a.get("disambiguation"),
                   "country": a.get("country"), "type": a.get("type")}
                  for a in pool[:6]]


def _by_fuzzy(artist: str, song: str | None, album: str | None) -> str | None:
    """A loosely-matched artist, accepted only if the song or album confirms it.

    Radio logs abbreviate and misspell: KALX logged "Sabotage Q.C.Q.C.?",
    which MusicBrainz knows as "Sabotage qu'est-ce que c'est?". An exact
    phrase search misses that, and a loose one alone would happily return
    the Brazilian rapper "Sabotage". Requiring the candidate to actually have
    a recording of the song being played (or a release of the album) is what
    makes a loose name search safe.
    """
    if not (song or album):
        return None
    loose = norm(artist)
    if not loose:
        return None
    hits = _mb("artist/", query=loose, limit=5).get("artists", [])
    for a in [h for h in hits if h.get("score", 0) >= 70][:3]:
        aid = a["id"]
        if song:
            q = f"recording:{_q(song)} AND arid:{aid}"
            if _mb("recording/", query=q, limit=1).get("count", 0):
                return aid
        elif _mb("release-group/", query=f"releasegroup:{_q(bare_title(album))} AND arid:{aid}",
                 limit=1).get("count", 0):
            return aid
    return None


# ---- Discogs -------------------------------------------------------------------
#
# The deepest catalogue for obscure physical releases -- and, usefully, the
# spelling college-radio DJs log from. Search works without a key at 25
# requests/minute; a free personal token (discogs.com/settings/developers)
# raises that to 60. Save one with `twiddle info --save-discogs-token <T>`;
# like the Spotify tokens it lives under ~/.cache/twiddle, outside the repo,
# and DISCOGS_TOKEN in the environment overrides it.

DISCOGS = "https://api.discogs.com"
DISCOGS_TOKEN_FILE = Path.home() / ".cache" / "twiddle" / "discogs.json"


def discogs_token() -> str | None:
    if os.environ.get("DISCOGS_TOKEN"):
        return os.environ["DISCOGS_TOKEN"]
    try:
        return json.loads(DISCOGS_TOKEN_FILE.read_text()).get("token") or None
    except (OSError, ValueError):
        return None


def save_discogs_token(token: str) -> Path:
    """Write the token owner-readable only; it is a credential, if a mild one."""
    DISCOGS_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    DISCOGS_TOKEN_FILE.touch(mode=0o600, exist_ok=True)
    DISCOGS_TOKEN_FILE.chmod(0o600)
    DISCOGS_TOKEN_FILE.write_text(json.dumps({"token": token.strip()}) + "\n")
    return DISCOGS_TOKEN_FILE


def _discogs(path: str, **params) -> dict:
    token = discogs_token()
    headers = {"Authorization": f"Discogs token={token}"} if token else {}
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    return _get_json(f"{DISCOGS}/{path}{q}", headers)


def discogs_markup(text: str) -> str:
    """Discogs profiles use BBCode-ish links: [a=Name], [l=Label], [a12345]."""
    text = re.sub(r"\[(?:a|l|m|r)=([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[(?:a|l|m|r)\d+\]", "", text)
    text = re.sub(r"\[url=[^\]]+\](.*?)\[/url\]", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\[/?[biu]\]", "", text)
    return " ".join(text.split())


def _discogs_name(name: str) -> str:
    # Discogs disambiguates same-named artists with a suffix: "Low (2)".
    return re.sub(r"\s*\(\d+\)$", "", name or "")


def _discogs_enrich(info: ArtistInfo) -> None:
    """Members and, when Wikipedia had nothing, the Discogs profile."""
    m = re.search(r"discogs\.com/artist/(\d+)", info.links.get("discogs", ""))
    if not m:
        return
    try:
        d = _discogs(f"artists/{m.group(1)}")
    except (OSError, ValueError):
        return  # garnish; never fail a lookup over it
    info.members = [_discogs_name(x["name"]) for x in d.get("members", [])
                    if x.get("active", True)] or info.members
    profile = discogs_markup(d.get("profile") or "")
    if profile and not info.summary:
        info.summary, info.summary_source = profile, "Discogs"


def _by_discogs(artist: str) -> tuple[ArtistInfo | None, list[dict]]:
    hits = _discogs("database/search", q=artist, type="artist", per_page=10).get("results", [])
    exact = [h for h in hits if _same(_discogs_name(h.get("title", "")), artist)]
    if len(exact) != 1:
        return None, [{"name": h.get("title"), "url": f"https://www.discogs.com/artist/{h['id']}"}
                      for h in exact[:6]]
    d = _discogs(f"artists/{exact[0]['id']}")
    profile = discogs_markup(d.get("profile") or "") or None
    info = ArtistInfo(name=_discogs_name(d.get("name", artist)),
                      summary=profile, summary_source="Discogs" if profile else None,
                      members=[_discogs_name(x["name"]) for x in d.get("members", [])],
                      links={"discogs": d.get("uri") or f"https://www.discogs.com/artist/{d.get('id')}"})
    for u in d.get("urls") or []:
        if "bandcamp.com" in u:
            info.links.setdefault("bandcamp", u)
    return info, []


# ---- Bandcamp --------------------------------------------------------------------
#
# Where a lot of very small bands exist *only*. There is no public API; this
# is the endpoint bandcamp.com's own search box calls, so it may change
# without notice -- which is why it is the last resort, and fails quietly.

BANDCAMP_SEARCH = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"


def bandcamp_bands(artist: str) -> list[dict]:
    """Bandcamp bands named exactly `artist`: raw search results, each with
    `item_url_root` (their page) and `img` (their band photo, if they set one)."""
    body = json.dumps({"search_text": artist, "search_filter": "b",
                       "full_page": False, "fan_id": None}).encode()
    req = urllib.request.Request(BANDCAMP_SEARCH, data=body, method="POST",
                                 headers={"User-Agent": USER_AGENT,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        results = json.load(resp).get("auto", {}).get("results", [])
    return [r for r in results if r.get("type") == "b" and _same(r.get("name"), artist)]


def _by_bandcamp(artist: str) -> ArtistInfo | None:
    exact = bandcamp_bands(artist)
    if len(exact) != 1:
        return None  # ambiguous on Bandcamp is not worth a guess
    b = exact[0]
    return ArtistInfo(name=b.get("name", artist), origin=b.get("location") or None,
                      kind="Label" if b.get("is_label") else None,
                      genres=b.get("tag_names") or [],
                      links={"bandcamp": b.get("item_url_root", "")})


# ---- details ------------------------------------------------------------------


def _wikipedia(relations: list[dict]) -> tuple[str | None, str | None, str | None]:
    """(description, summary, url) via the entity's own Wikidata link, or Nones."""
    qid = next((r["url"]["resource"].rstrip("/").rsplit("/", 1)[-1]
                for r in relations if r.get("type") == "wikidata"), None)
    if not qid:
        return None, None, None
    try:
        ent = _get_json("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
            {"action": "wbgetentities", "ids": qid, "props": "sitelinks",
             "sitefilter": "enwiki", "format": "json"}))
        title = ent["entities"][qid]["sitelinks"]["enwiki"]["title"]
        page = _get_json("https://en.wikipedia.org/api/rest_v1/page/summary/"
                         + urllib.parse.quote(title.replace(" ", "_"), safe=""))
    except (KeyError, OSError, ValueError):
        return None, None, None
    return (page.get("description"), page.get("extract"),
            page.get("content_urls", {}).get("desktop", {}).get("page"))


def _genres(entity: dict, n: int = 6) -> list[str]:
    g = sorted(entity.get("genres") or [], key=lambda x: -x.get("count", 0))
    return [x["name"] for x in g[:n]]


def _links(relations: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel_type, label in LINK_TYPES:
        for r in relations:
            if r.get("type") == rel_type and label not in out and not r.get("ended"):
                out[label] = r["url"]["resource"]
    # MusicBrainz files Spotify under "free streaming". Kept because it is
    # the one *linked* identity between the two databases: `scene` uses it to
    # confirm a same-named Spotify artist really is this band.
    for r in relations:
        res = (r.get("url") or {}).get("resource", "")
        if "open.spotify.com/artist/" in res and not r.get("ended"):
            out.setdefault("spotify", res)
    return out


def _years(span: dict) -> str | None:
    begin = (span.get("begin") or "")[:4]
    end = (span.get("end") or "")[:4]
    if not begin:
        return None
    if end:
        return f"{begin}-{end}"
    return f"{begin}-" if not span.get("ended") else begin


def artist_details(mbid: str) -> ArtistInfo:
    a = _mb(f"artist/{mbid}", inc="url-rels+genres")
    rels = a.get("relations", [])
    desc, summary, wiki = _wikipedia(rels)
    places = [(a.get(k) or {}).get("name") for k in ("begin-area", "area")]
    origin = ", ".join(dict.fromkeys(p for p in places if p)) or None
    links = ({"wikipedia": wiki} if wiki else {}) | _links(rels)
    links["musicbrainz"] = f"https://musicbrainz.org/artist/{mbid}"
    return ArtistInfo(name=a.get("name", ""), mbid=mbid,
                      disambiguation=a.get("disambiguation") or None,
                      kind=a.get("type"), origin=origin,
                      years=_years(a.get("life-span") or {}),
                      genres=_genres(a), description=desc, summary=summary,
                      links=links)


def album_details(rg_id: str) -> AlbumInfo:
    rg = _mb(f"release-group/{rg_id}", inc="url-rels+genres")
    rels = rg.get("relations", [])
    _desc, summary, wiki = _wikipedia(rels)
    kind = " + ".join(([rg["primary-type"]] if rg.get("primary-type") else [])
                      + rg.get("secondary-types", [])) or None
    links = ({"wikipedia": wiki} if wiki else {}) | _links(rels)
    links["musicbrainz"] = f"https://musicbrainz.org/release-group/{rg_id}"
    return AlbumInfo(title=rg.get("title", ""), mbid=rg_id,
                     year=(rg.get("first-release-date") or "")[:4] or None,
                     kind=kind, genres=_genres(rg), summary=summary, links=links)


# ---- the one entry point -------------------------------------------------------


# Bumped when a cached Result gains a field worth refetching for: v2 added
# the MusicBrainz -> Spotify link that `scene` uses to confirm identity.
CACHE_VERSION = "v2"


def _cache_key(*parts) -> str:
    return CACHE_VERSION + "|" + "|".join(norm(p) if p else "" for p in parts)


def _cache_load() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _cache_save(cache: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        fresh = {k: v for k, v in cache.items() if now - v.get("at", 0) < CACHE_TTL_S}
        CACHE_FILE.write_text(json.dumps(fresh))
    except OSError:
        pass


def _from_dict(d: dict) -> Result:
    a = d.get("artist")
    if a:
        album = a.pop("album", None)
        a = ArtistInfo(**a, album=AlbumInfo(**album) if album else None)
    return Result(artist=a, candidates=d.get("candidates", []))


def identify(artist: str | None, album: str | None = None, song: str | None = None, *,
             mb_artist_id: str | None = None, mb_release_group_id: str | None = None,
             use_cache: bool = True) -> Result:
    """Everything worth knowing about who is playing, from the best evidence given.

    Raises only for network failure; "couldn't tell who this is" is an empty
    Result (possibly with candidates), because that is an answer, not an error.
    """
    key = _cache_key(mb_artist_id or artist, mb_release_group_id or album,
                     None if (album or mb_release_group_id) else song)
    cache = _cache_load() if use_cache else {}
    hit = cache.get(key)
    if hit and time.time() - hit.get("at", 0) < CACHE_TTL_S:
        return _from_dict(hit["result"])

    try:
        result = _identify(artist, album, song, mb_artist_id, mb_release_group_id)
    except OSError as exc:  # URLError, timeouts, and HTTPError are all OSErrors
        raise LookupFailed(f"music databases unreachable: {exc}") from exc

    if use_cache and (result.artist or result.candidates):
        cache[key] = {"at": time.time(), "result": result.to_dict()}
        _cache_save(cache)
    return result


def _identify(artist, album, song, mb_artist_id, mb_release_group_id) -> Result:
    rg_id = mb_release_group_id
    how = "MusicBrainz id from the station" if mb_artist_id else ""
    if not mb_artist_id and artist and album:
        found = _by_album(artist, album)
        if found:
            mb_artist_id, rg = found
            rg_id = rg_id or rg["id"]
            how = f"artist + album ({rg.get('title')})"
    if not mb_artist_id and artist and song:
        mb_artist_id = _by_song(artist, song)
        how = f"artist + song ({song})" if mb_artist_id else how
    candidates: list[dict] = []
    if not mb_artist_id and artist:
        mb_artist_id, candidates = _by_name(artist)
        how = "unique name match" if mb_artist_id else how
    if not mb_artist_id and artist and not candidates:
        mb_artist_id = _by_fuzzy(artist, song, album)
        how = f"loose name match, confirmed by {'the song' if song else 'the album'}" \
            if mb_artist_id else how

    if mb_artist_id:
        info = artist_details(mb_artist_id)
        info.matched_by = how
        _discogs_enrich(info)
        if rg_id:
            info.album = album_details(rg_id)
        return Result(artist=info)
    if candidates or not artist:
        return Result(candidates=candidates)

    # Not in MusicBrainz at all: the smaller the band, the likelier this.
    for source, find in (("Discogs", _by_discogs), ("Bandcamp", _by_bandcamp)):
        try:
            found = find(artist)
        except (OSError, ValueError, KeyError):
            continue  # one source being down shouldn't hide the next
        info, cands = found if isinstance(found, tuple) else (found, [])
        if info:
            info.matched_by = f"unique name match on {source}"
            return Result(artist=info)
        if cands:
            return Result(candidates=cands)
    return Result()


# ---- rendering ------------------------------------------------------------------


def _wrap(text: str, indent: str = "  ", width: int = 78) -> str:
    import textwrap
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _link_lines(links: dict[str, str]) -> list[str]:
    w = max((len(k) for k in links), default=0)
    return [f"  {k:<{w}}  {v}" for k, v in links.items()]


def render(result: Result, asked: str | None = None) -> str:
    a = result.artist
    if a is None:
        if not result.candidates:
            return (f"Couldn't find {asked!r} on MusicBrainz, Discogs or Bandcamp."
                    if asked else
                    "Nothing to look up -- the station isn't naming an artist right now.")
        lines = [f"Several artists are called {asked!r}; which one?"]
        for c in result.candidates:
            extra = ", ".join(x for x in (c.get("disambiguation"), c.get("type"),
                                          c.get("country")) if x)
            url = c.get("url") or f"https://musicbrainz.org/artist/{c['mbid']}"
            lines.append(f"  {c['name']}" + (f"  ({extra})" if extra else "")
                         + f"\n    {url}")
        lines.append("Name the album too (`--album`) to settle it.")
        return "\n".join(lines)

    head = a.name + (f" -- {a.description or a.disambiguation}"
                     if (a.description or a.disambiguation) else "")
    lines = [head]
    facts = " · ".join(x for x in (a.kind, a.origin, a.years) if x)
    if facts:
        lines.append(f"  {facts}")
    if a.genres:
        lines.append(f"  {', '.join(a.genres)}")
    if a.members:
        lines.append(f"  members: {', '.join(a.members)}")
    if a.summary:
        lines += ["", _wrap(a.summary)]
        if a.summary_source:
            lines.append(f"  -- {a.summary_source}")
    lines += [""] + _link_lines(a.links)

    al = a.album
    if al:
        what = " ".join(x for x in (al.year, (al.kind or "").lower()) if x)
        lines += ["", al.title + (f" -- {what}" if what else "")]
        if al.genres:
            lines.append(f"  {', '.join(al.genres)}")
        if al.summary:
            lines += ["", _wrap(al.summary)]
        # MusicBrainz's own link is noise here when it's the only one.
        if set(al.links) - {"musicbrainz"}:
            lines += [""] + _link_lines(al.links)
    if a.matched_by:
        lines += ["", f"  (identified by {a.matched_by})"]
    return "\n".join(lines)
