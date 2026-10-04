"""What is this stream, and how would we learn what it's playing? (read-only)

Given a stream URL (and, better, the station's call sign or homepage), this
answers the questions adding a station turns on:

1. **Does it play on a Sonos?** `play_radio` hands the speaker an
   `x-rincon-mp3radio://` URI, which it fetches over plain http. An https-only
   stream, or an http one that redirects to https (NTS), plays on the Mac
   and not on the Roam.
2. **What is it?** Codec and bitrate (ffprobe, via `streaminfo`), and the
   `icy-*` headers the server sends about itself.
3. **What does ICY say?** A few `StreamTitle`s, a few seconds apart, and
   their shape: "Artist - Song" is usable as-is; blank means ICY alone
   will show nothing.
4. **Which fetcher?** A table of platforms (`PLATFORMS`, and the URL rules in
   `directory.fetch_for_url`): a stream URL, a Spinitron page for the call
   sign, or a widget on the homepage settles it. A platform with no fetcher
   yet is reported as such -- that is when to write one.

It ends with a draft catalog entry. Writing it is left to whoever read the
report: the blurb and tags are judgement, not measurement.
"""
from __future__ import annotations

import io
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

from .. import streaminfo
from . import icy, net, titles
from .directory import fetch_for_url
from .fetchers import FETCHERS
from .fetchers.spinitron import parse_spinitron

PLAYER_UA = "VLC/3.0.9"
ICY_READ_S = 15          # a stalled stream must not hang the probe (NTS's edge did)
PLAYLIST_TYPES = ("audio/x-scpls", "audio/x-mpegurl", "audio/mpegurl", "application/pls+xml",
                  "application/vnd.apple.mpegurl", "application/x-mpegurl")

# A link to spinitron.com/<CALL>, or a widget's ?station=<call>. Its own asset
# paths (spinitron.com/static/..., /images/...) are not call signs.
SPINITRON_RX = (r"spinitron\.com/(?!(?:static|images|assets|js|css|api|widget|w|radio|"
                r"station|public|m)\b)(?:[^\"'\s]*?[?&]station=)?([A-Za-z][A-Za-z0-9-]{2,})"
                r"|spinitron\.com/[^\"'\s]*?[?&]station=([A-Za-z][A-Za-z0-9-]{2,})")

# Markers on a station's homepage that name the platform behind it. `fetch`
# is the fetcher that handles it, or None: a known platform we have no
# fetcher for yet -- write one as a *platform* fetcher taking fetch_args,
# so the next station on it is data only.
PLATFORMS = [
    # (name, regex on the homepage, fetch, what the match gives / what to do)
    ("spinitron", SPINITRON_RX, "spinitron",
     "the call sign (callsign in fetch_args if it isn't the name)"),
    ("airtime-pro", r"([a-z0-9-]+)\.airtime\.pro", None,
     "Airtime Pro: https://<station>.airtime.pro/api/live-info-v2 is public JSON"),
    ("radio.co", r"(?:public|streamer)\.radio\.co/stations?/(s[0-9a-f]+)", None,
     "Radio.co: https://public.radio.co/stations/<id>/status is public JSON"),
    ("radioking", r"radioking\.(?:com|io)/[^\"']*?/(\d{3,})", None,
     "RadioKing: api.radioking.io/widget/radio/<id>/track/current"),
    ("creek", r"([a-z0-9-]+)\.creek\.fm", None,
     "Creek (community radio CMS): /api/broadcasts and schedule JSON"),
]


@dataclass
class Probe:
    url: str
    stream_url: str | None = None        # after playlists and redirects
    asked_url: str | None = None         # after playlists, before redirects
    playlist: bool = False
    content_type: str | None = None
    http_url: str | None = None          # a plain-http URL that stays http, if any
    http_note: str | None = None
    icy_name: str | None = None
    icy_genre: str | None = None
    icy_site: str | None = None
    icy_metadata: bool = False
    titles: list[str | None] = field(default_factory=list)
    title_shape: str | None = None       # artist-song | a DETECTABLE shape | show-like | blank
    codec: str | None = None
    bitrate: int | None = None
    callsign: str | None = None
    homepage: str | None = None
    platform: str | None = None
    fetch: str | None = None
    fetch_args: dict = field(default_factory=dict)
    platform_note: str | None = None
    key_hint: str | None = None          # a better catalog key than the name gives
    logos: list[dict] = field(default_factory=list)   # {url, width, height, square}
    verdict: str = ""
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], {}, "")}


# ---- the stream ------------------------------------------------------------------


def parse_playlist(body: str) -> str | None:
    """The first stream in a .pls / .m3u body."""
    for line in body.splitlines():
        line = line.strip()
        m = re.match(r"File\d+=(.+)", line, re.I)
        if m:
            return m.group(1).strip()
        if line.startswith(("http://", "https://")):
            return line
    return None


def _open(url: str, timeout: float = net.TIMEOUT):
    req = urllib.request.Request(url, headers={"Icy-MetaData": "1", "User-Agent": PLAYER_UA})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_capped(url: str, kinds: tuple[str, ...], cap: int = 2_000_000) -> bytes:
    """A page or image, at most `cap` bytes, and only of a content type in
    `kinds`: a station's icy-url can be the endless stream itself (NTS's is)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=net.TIMEOUT) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not ctype.startswith(kinds):
            raise ValueError(f"{ctype or 'no content type'}, not {'/'.join(kinds)}")
        return resp.read(cap)


def resolve(url: str) -> tuple[str, bool, dict, str]:
    """(the audio URL, whether a playlist was followed, the stream's headers,
    the URL asked for before any redirect)."""
    playlist = False
    for _ in range(3):
        with _open(url) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            is_list = ctype in PLAYLIST_TYPES or re.search(r"\.(pls|m3u8?)(\?|$)", resp.url)
            if not is_list:
                return resp.url, playlist, {k.lower(): v for k, v in resp.headers.items()}, url
            body = resp.read(65536).decode("utf-8", "replace")
        if "#EXT-X-" in body:
            raise ValueError("HLS, not a plain stream: Sonos x-rincon-mp3radio can't play it")
        inner = parse_playlist(body)
        if not inner:
            raise ValueError("a playlist with no stream in it")
        url, playlist = inner, True
    raise ValueError("playlists nested too deep")


def host_of(url: str | None) -> str:
    return urllib.parse.urlsplit(url or "").hostname or ""


def plain_http(url: str) -> tuple[str | None, str | None]:
    """A plain-http URL for this stream that stays http, and a note if none."""
    candidate = "http://" + re.sub(r"^https?://", "", url)
    try:
        with _open(candidate, timeout=6) as resp:
            final = resp.url
            ctype = (resp.headers.get("Content-Type") or "").lower()
            resp.read(1)
    except Exception as exc:
        return None, f"no plain-http stream ({type(exc).__name__}): plays on the Mac, not a Sonos"
    if final.startswith("https://"):
        return None, "http redirects to https: plays on the Mac, not yet confirmed on a Sonos"
    if not (ctype.startswith("audio") or "ogg" in ctype or "aac" in ctype or not ctype):
        return None, f"plain http answers with {ctype!r}, not audio"
    return final if final != candidate else candidate, None


def bounded(fn, timeout: float):
    """fn() if it returns within `timeout`, else TimeoutError. A daemon
    thread, so one still blocked on a socket can't keep the CLI alive."""
    out: dict = {}

    def run():
        try:
            out["v"] = fn()
        except Exception as exc:
            out["e"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"no answer in {timeout:.0f}s")
    if "e" in out:
        raise out["e"]
    return out.get("v")


# The title shapes (`titles.SHAPES`) the probe may name from a few titles alone,
# most specific first, each with the mark only its own titles carry: parsing
# isn't enough (`wfmu` reads any `"Song" by Artist`, "on WXYZ" and all). Never
# a permissive one (`artist-dot-song` splits any " · ") or one that only makes
# sense for its own station (`kcrw`). `iheart-attrs` before `iheart-space`,
# which reads comma attributes too.
DETECTABLE = {
    "wfmu": re.compile(r" on WFMU$"),
    "iheart-attrs": re.compile(r'\bartist="'),
    "iheart-space": re.compile(r' - text="|\bartist="'),       # it reads both
}
_IHEART = ("iheart-attrs", "iheart-space")
# Shapes that are the only reader of a title with their mark, so one it reads
# as nothing (WFMU's "Your DJ speaks over ...") is still its own. Not iHeart's:
# an iheart-attrs blank can hide what iheart-space reads.
_OWNS_ITS_MARK = {"wfmu"}


def _has_shape(shape: str, real: list[str]) -> bool:
    # Every title parses (or is the shape's own filler), at least one to an
    # artist (WFMU's bare `Show with Host` alone would match any "Morning
    # Edition with Steve"), and every title it takes an artist from has the
    # shape's mark.
    mark = DETECTABLE[shape]
    got = [titles.parse(shape, t) for t in real]
    return (all(g or (shape in _OWNS_ITS_MARK and mark.search(t.strip()))
                for t, g in zip(real, got))
            and any(g.get("artist") for g in got)
            and all(mark.search(t.strip()) for t, g in zip(real, got) if g.get("artist")))


def _reads_every_song(real: list[str]) -> str | None:
    """iHeart titles mixed with a spot (`title="",artist=""`), a show name or
    a plain "Artist - Song": the first reader that reads each title with an
    artist as well as its best reader does (song and cover too), and makes
    up none from the rest, or None."""
    readers = (*_IHEART, None)

    def best(t: str) -> dict:
        # An iHeart title is only its readers': a plain split would make
        # "Artist - text=..." or `title="Promo - Break"` an artist and song.
        own = (("iheart-space",) if ' - text="' in t
               else _IHEART if DETECTABLE["iheart-attrs"].search(t) else readers)
        return next((g for r in own if (g := titles.parse(r, t)).get("artist")), {})
    wanted = {t: b for t in real if (b := best(t))}
    if not wanted:
        # Only spots sampled: still iHeart's, in the form its marks show.
        return "iheart-space" if any(' - text="' in t for t in real) else "iheart-attrs"
    for r in readers:
        got = {t: titles.parse(r, t) for t in real}
        if all(got[t] == b for t, b in wanted.items()) and \
                not any(got[t].get("artist") for t in real if t not in wanted):
            return r or "artist-song"
    return None


def title_shape(samples: list[str | None], icy_name: str | None = None) -> str:
    """`blank`, `artist-song` (the default reading is enough), a shape in
    DETECTABLE that every title has, or `show-like`: consistent titles in no
    shape we know, which may still carry an artist a new shape could read."""
    # Parsed as sent, as the fetcher will; trimmed only to look for a mark.
    real = [t for t in samples if t and t.strip()]
    if not real:
        return "blank"
    # "90s90s - DIGITAL WEB" splits like Artist - Song but is only the
    # stream's own name, every time (streamabc, 2026-09-27).
    def bare(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())
    if icy_name and all(bare(t) == bare(icy_name) for t in real):
        return "show-like"
    for shape in DETECTABLE:
        if _has_shape(shape, real):
            return shape
    # A title in another known shape that it couldn't place is never split on
    # " - " instead: `"Orgies - A Tool ..." by` would give the artist "Orgies.
    if any(DETECTABLE[k].search(t.strip()) for k in DETECTABLE if k not in _IHEART
           for t in real):
        return "show-like"
    # Nor is an iHeart one: a plain split reads comedy247's `text="..."` as a song.
    if any(DETECTABLE[k].search(t.strip()) for k in _IHEART for t in real):
        return _reads_every_song(real) or "show-like"
    if all(icy.split_title(icy.tidy_title(t)) != (None, None) for t in real):
        return "artist-song"
    return "show-like"


# ---- the station ----------------------------------------------------------------------


def guess_callsign(name: str | None, url: str | None = None) -> str | None:
    """A US call sign in the icy-name ("KALX 90.7"), else in the stream's
    host ("stream.kalx.berkeley.edu"). Only a guess: Spinitron confirms it."""
    for m in re.finditer(r"\b([KW][A-Z]{2,3})\b", name or ""):
        if m.group(1) not in ("WEB", "WWW", "KBPS"):       # "90s90s - DIGITAL WEB"
            return m.group(1)
    host = urllib.parse.urlsplit(url or "").hostname or ""
    for label in re.split(r"[.-]", host):
        if re.fullmatch(r"[kw][a-z]{2,3}", label) and label not in ("www", "web", "kbps"):
            return label.upper()
    return None


def spinitron_has(callsign: str) -> tuple[bool, str | None]:
    """(spinitron.com/<callsign> logs spins, its logo if it shows one)."""
    try:
        page = net.get(f"https://spinitron.com/{callsign}").decode("utf-8", "replace")
    except Exception:
        return False, None
    logo = re.search(r'(https://spinitron\.com/images/Station/[^"\']+img_logo[^"\']+)', page)
    found = parse_spinitron(page)
    return bool(found.get("artist") or found.get("song") or "data-spin" in page), \
        (logo.group(1) if logo else None)


def detect_platform(page: str) -> tuple[str, str | None, str, str] | None:
    """(platform, the id it matched, fetch or None, note) for a homepage."""
    for name, rx, fetch, note in PLATFORMS:
        m = re.search(rx, page, re.I)
        if m:
            return name, next(g for g in m.groups() if g), fetch, note
    return None


def homepage_logos(page: str, base: str) -> list[str]:
    out = []
    for rx in (r'<link[^>]+rel="apple-touch-icon[^"]*"[^>]*href="([^"]+)"',
               r'<link[^>]+href="([^"]+)"[^>]*rel="apple-touch-icon[^"]*"',
               r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"',
               r'<link[^>]+rel="icon"[^>]*sizes="(?:1[5-9]\d|[2-9]\d\d)x\d+"[^>]*href="([^"]+)"'):
        out += [urllib.parse.urljoin(base, m) for m in re.findall(rx, page, re.I)]
    return list(dict.fromkeys(out))


def measure_logo(url: str) -> dict:
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(fetch_capped(url, ("image/",), cap=5_000_000)))
        w, h = im.size
        return {"url": url, "width": w, "height": h,
                "square": abs(w - h) <= 0.1 * max(w, h) and min(w, h) >= 120}
    except Exception as exc:
        return {"url": url, "error": type(exc).__name__}


# ---- all together ------------------------------------------------------------------------


def probe(url: str, *, callsign: str | None = None, homepage: str | None = None,
          samples: int = 2, every: float = 8.0, logos: list[str] | None = None,
          sleep=time.sleep) -> Probe:
    p = Probe(url)
    try:
        p.stream_url, p.playlist, headers, p.asked_url = resolve(url)
    except Exception as exc:
        p.problems.append(f"stream didn't open: {type(exc).__name__}: {exc}")
        p.verdict = "not playable: fix the URL first"
        return p
    p.content_type = headers.get("content-type")
    p.icy_name = headers.get("icy-name") or None
    p.icy_genre = headers.get("icy-genre") or None
    p.icy_site = headers.get("icy-url") or None
    p.icy_metadata = bool(headers.get("icy-metaint"))

    if p.stream_url.startswith("http://"):
        p.http_url = p.stream_url
    else:
        p.http_url, p.http_note = plain_http(p.stream_url)
        if p.http_note:
            p.problems.append(p.http_note)
    if host_of(p.asked_url) != host_of(p.stream_url):
        # A load balancer handing out edges (NTS -> radiomast): the edge's
        # name is not the station's, and can change. Keep the station's URL.
        p.problems.append(f"{host_of(p.asked_url)} redirects to {host_of(p.stream_url)}: "
                          "the catalog should keep the station's own URL"
                          + ("" if p.asked_url.startswith("http://") else
                             "; it is https, so check a plain-http one on a Sonos"))

    fmt = streaminfo.info(p.stream_url)
    if fmt:
        p.codec, p.bitrate = fmt.codec, fmt.kbps

    for i in range(max(1, samples) if p.icy_metadata else 0):
        if i:
            sleep(every)
        try:
            p.titles.append(bounded(lambda: icy.icy_title(p.stream_url), ICY_READ_S))
        except Exception as exc:
            p.titles.append(None)
            p.problems.append(f"ICY title didn't arrive: {type(exc).__name__}: {exc}")
            break
    p.title_shape = title_shape(p.titles, p.icy_name) if p.icy_metadata else "blank"

    # Which fetcher: the URL, then Spinitron, then the homepage.
    p.homepage = homepage or p.icy_site
    if p.homepage and not re.match(r"https?://", p.homepage):
        p.homepage = "http://" + p.homepage       # icy-url is often a bare host
    p.callsign = callsign
    page = ""
    if p.homepage:
        try:
            page = fetch_capped(p.homepage, ("text/html",)).decode("utf-8", "replace")
        except Exception as exc:
            p.problems.append(f"homepage didn't load: {type(exc).__name__}: {exc}")
    logo_candidates = list(logos or [])
    p.fetch = fetch_for_url(p.stream_url) or fetch_for_url(url)
    if p.fetch == "somafm":
        p.platform = "somafm"
        chan = re.search(r"somafm\.com/([a-z0-9]+)-", p.stream_url + " " + url).group(1)
        p.fetch_args = {}
        p.key_hint = chan
        logo_candidates.insert(0, f"https://api.somafm.com/logos/512/{chan}512.png")
    elif p.fetch == "nts":
        p.platform = "nts"
        p.fetch_args = {"channel": "2" if re.search(r"stream2\b", url) else "1"}
    elif "streamabc.net" in p.stream_url:
        p.platform, p.fetch = "streamabc", "streamabc"
        p.platform_note = ("needs fetch_args channel: the audiotheque_channel_external_id in the "
                           "site's page whose api.streamabc.net/metadata/channel/<key>.json "
                           "'channel' equals this stream's icy-name")
    elif p.fetch == "radiofrance":
        p.platform = "radiofrance"
        p.platform_note = ("needs fetch_args rf_id: the station's id in "
                           "api.radiofrance.fr/livemeta/pull/<id> (FIP Electro is 74)")
    if not p.fetch and page:
        hit = detect_platform(page)
        if hit and hit[0] == "spinitron":
            p.callsign = p.callsign or hit[1].upper()
        elif hit:
            p.platform, _, p.fetch, p.platform_note = hit[0], hit[1], hit[2], \
                f"{hit[3]} (matched {hit[1]!r})"
    p.callsign = p.callsign or guess_callsign(p.icy_name, p.stream_url) \
        or guess_callsign(None, url)
    if not p.fetch and p.callsign:
        live, spin_logo = spinitron_has(p.callsign)
        if live:
            p.platform, p.fetch = "spinitron", "spinitron"
            p.fetch_args = {"callsign": p.callsign}
            if spin_logo:
                logo_candidates.append(spin_logo)
    if not p.fetch and (p.title_shape == "artist-song" or p.title_shape in DETECTABLE):
        p.platform, p.fetch = "icy", "icy"
    if not p.fetch and re.search(r"news|talk", p.icy_genre or "", re.I):
        p.platform, p.fetch = "icy", "talk"
    if page and p.homepage:
        logo_candidates += homepage_logos(page, p.homepage)
    p.logos = [measure_logo(u) for u in list(dict.fromkeys(logo_candidates))[:6]]

    if p.fetch == "icy" and p.title_shape in DETECTABLE:
        p.verdict = (f"ICY titles in the {p.title_shape!r} shape: data only -- fetch = 'icy' "
                     f"with fetch_args titles = {p.title_shape!r}")
    elif p.fetch and p.fetch in FETCHERS and not p.platform_note:
        p.verdict = f"known platform ({p.platform}): data only -- fetch = {p.fetch!r}"
    elif p.fetch and p.fetch in FETCHERS:
        p.verdict = f"known platform ({p.platform}): data only, but {p.platform_note}"
    elif p.platform:
        p.verdict = f"needs a custom fetcher: {p.platform_note}"
    elif p.title_shape == "show-like":
        p.verdict = ("ICY only, and its titles fit no shape we know: if they name an artist, "
                     "write a title shape for them (stations/titles.py, with a test) and use "
                     "fetch_args titles; only if there's no artist to recover, music = false. "
                     "Or look for a feed")
    else:
        p.verdict = ("needs a custom fetcher: ICY is blank and no known platform matched -- "
                     "look for a now-playing feed on the station's site")
    return p


def draft_toml(p: Probe, key: str | None = None) -> str:
    """A starting catalog entry. Blurb and tags are left for a person to write."""
    def q(s):
        return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
    # icy-name is often a slogan ("Groove Salad: a nicely chilled plate … [SomaFM]").
    name = re.split(r":| \[| \| ", p.icy_name or "")[0].strip()
    if p.callsign and (not name or " " not in name):   # "kalx-128-mp3" is a mount, not a name
        name = p.callsign
    name = name or "?"
    key = key or p.key_hint or re.sub(r"[^a-z0-9]", "", (p.callsign or name).lower()) or "station"
    lines = [f"# {key}.toml -- draft from `twiddle stations probe`; check every line."]
    if p.http_note:
        lines.append(f"# {p.http_note}")
    url = (p.asked_url if host_of(p.asked_url) != host_of(p.stream_url)
           else p.http_url or p.stream_url) or p.url
    lines += [f"name  = {q(name)}", f"url   = {q(url)}",
              'blurb = "City -- what it is"            # TODO: the dial splits on " -- "',
              "tags  = []                               # TODO: from `twiddle stations tags`"]
    fetch_args = {k: v for k, v in p.fetch_args.items()
                  if not (k == "callsign" and v.upper() == name.upper())}
    if p.fetch and p.fetch != "icy":
        lines.append(f"fetch = {q(p.fetch)}")
    if p.fetch in (None, "icy") and p.title_shape in DETECTABLE:
        fetch_args["titles"] = p.title_shape
    elif p.fetch in (None, "icy") and p.title_shape == "show-like":
        lines.append("# titles fit no known shape: write one if they name an artist, "
                     "else keep music = false")
        fetch_args["music"] = False
    if fetch_args:
        lines.append("fetch_args = { " + ", ".join(
            f"{k} = {str(v).lower() if isinstance(v, bool) else q(v)}"
            for k, v in fetch_args.items()) + " }")
    square = [x for x in p.logos if x.get("square")]
    if square:
        lines.append(f"logo  = {q(square[0]['url'])}")
    return "\n".join(lines) + "\n"


def render(p: Probe, key: str | None = None) -> str:
    lines = [f"stream   {p.stream_url or p.url}" + ("   (from a playlist)" if p.playlist else "")]
    fmt = " ".join(x for x in (p.codec, f"{p.bitrate}k" if p.bitrate else None) if x)
    lines.append(f"format   {fmt or '?'}   {p.content_type or ''}")
    lines.append(f"sonos    {'plain http OK: ' + p.http_url if p.http_url else p.http_note}")
    if p.icy_name or p.icy_genre or p.icy_site:
        lines.append(f"icy      name={p.icy_name!r} genre={p.icy_genre!r} url={p.icy_site!r}")
    lines.append(f"titles   {p.title_shape}: " + (" | ".join(repr(t) for t in p.titles)
                                                  if p.titles else "(no ICY metadata)"))
    if p.callsign:
        lines.append(f"callsign {p.callsign}")
    if p.platform:
        lines.append(f"platform {p.platform}" + (f"  -- {p.platform_note}" if p.platform_note
                                                 else ""))
    for x in p.logos:
        desc = (f"{x['width']}x{x['height']}{'  square' if x['square'] else ''}"
                if "width" in x else f"didn't load ({x['error']})")
        lines.append(f"logo     {desc}  {x['url']}")
    for prob in p.problems:
        if prob != p.http_note:
            lines.append(f"problem  {prob}")
    lines += ["", f"VERDICT  {p.verdict}"]
    if p.stream_url:
        lines += ["", draft_toml(p, key).rstrip()]
    return "\n".join(lines)
