"""Bandcamp, for the bands Spotify doesn't know: who they are, what they
sound like (their tags), their photo, and songs you can play.

Most bands on The List are local and small, and Bandcamp is where they
are. Three things here share one search:

  the band's photo and tags   (`pictures.py`, `genre.py`)
  a playable release          (`BandcampEnricher` in `bands.py`)
  the whole-listing genre scan (`app.py`)

## Being a polite guest

The search is Bandcamp's own site autocomplete, not a published API, and
the genre scan asks about every band in the listing. So every request goes
through one throttle (`MIN_INTERVAL_S` apart), answers are cached on disk
for weeks, and a 429 or a run of errors stops all of it for a while
(`BlockedError`) instead of hammering. A block would also cost `dial` its
Bandcamp photos and `lookup` its Bandcamp fallback -- the Spotify quota
lockout of 2026-09-24 is the same lesson.

## Which band is it?

Names collide, so a band is chosen, not guessed:
  the Bandcamp page MusicBrainz/Discogs link -> the only exact-name band
  -> the only exact-name band in the Bay Area / California -> none.
Labels are never a band.

## Songs

A release's page carries its tracks with a signed `mp3-128` stream URL.
Those expire in about a day and a pane stays open for days, so the URL is
fetched again (`stream_url`) at the moment of playing.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import lookup
from . import cache

MIN_INTERVAL_S = 1.0
HIT_TTL_S = 14 * 86400
MISS_TTL_S = 3 * 86400
BLOCK_S = 15 * 60
MAX_ERRORS = 3              # in a row, then back off as if blocked
TIMEOUT = 10
UA = "Mozilla/5.0 twiddle (personal use)"
KEEP = ("name", "item_url_root", "location", "is_label", "tag_names", "img", "genre_name")

_throttle = threading.Lock()
_cache_lock = threading.Lock()
_last = 0.0
_blocked_until = 0.0
_errors = 0


class BlockedError(RuntimeError):
    """Bandcamp said slow down (or kept failing): don't ask again for a while."""


def _gate() -> None:
    global _last
    if time.monotonic() < _blocked_until:
        raise BlockedError("Bandcamp is resting (rate limited or failing); try later")
    with _throttle:
        wait = _last + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()


def _outcome(ok: bool, exc: Exception | None = None) -> None:
    global _errors, _blocked_until
    if ok:
        _errors = 0
        return
    _errors += 1
    too_many = isinstance(exc, urllib.error.HTTPError) and exc.code == 429
    if too_many or _errors >= MAX_ERRORS:
        _blocked_until = time.monotonic() + BLOCK_S
        _errors = 0


def _guarded(fn):
    _gate()
    try:
        out = fn()
    except urllib.error.HTTPError as exc:
        _outcome(exc.code == 404, exc)    # a missing page is an answer, not a failure
        raise
    except Exception as exc:
        _outcome(False, exc)
        raise
    _outcome(True)
    return out


def _get(url: str) -> str:
    def go():
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    return _guarded(go)


# ---- search ------------------------------------------------------------------------


def cached(name: str) -> list[dict] | None:
    """The remembered answer for `name`, or None if there isn't a fresh one."""
    entry = cache._read("bandcamp.json").get(lookup.norm(name))
    if not entry:
        return None
    ttl = HIT_TTL_S if entry.get("bands") else MISS_TTL_S
    return entry["bands"] if time.time() - entry.get("at", 0) < ttl else None


def search(name: str, *, offline: bool = False) -> list[dict] | None:
    """Bandcamp bands named exactly `name` (trimmed search results), cached.

    `offline` answers from the cache alone: None when it has no answer.
    Raises on a network failure, BlockedError while backing off.
    """
    hit = cached(name)
    if hit is not None or offline:
        return hit
    bands = [{k: b.get(k) for k in KEEP}
             for b in _guarded(lambda: lookup.bandcamp_bands(name))]
    with _cache_lock:
        data = cache._read("bandcamp.json")
        data[lookup.norm(name)] = {"at": time.time(), "bands": bands}
        cache._write("bandcamp.json", data)
    return bands


def _host(url: str | None) -> str:
    return (urllib.parse.urlsplit(url or "").hostname or "").lower()


def choose(bands: list[dict], linked: str | None = None,
           is_local=None) -> dict | None:
    """Which of the same-named bands is this one (see the module docstring)."""
    bands = [b for b in bands if not b.get("is_label")]
    if linked:
        hit = next((b for b in bands if _host(b.get("item_url_root")) == _host(linked)), None)
        if hit:
            return hit
    if len(bands) == 1:
        return bands[0]
    if is_local:
        near = [b for b in bands if is_local(b.get("location") or "")]
        if len(near) == 1:
            return near[0]
    return None


# ---- releases and tracks ----------------------------------------------------------


def _tralbum(page: str) -> dict | None:
    m = re.search(r'data-tralbum="([^"]+)"', page)
    if not m:
        return None
    try:
        return json.loads(html.unescape(m.group(1)))
    except json.JSONDecodeError:
        return None


def _tracks_of(page: str, page_url: str) -> list[dict]:
    d = _tralbum(page) or {}
    cur = d.get("current") or {}
    album = cur.get("title") or ""
    year = (d.get("album_release_date") or cur.get("release_date") or "")
    year = (re.search(r"\b(19|20)\d\d\b", year) or [""])[0]
    art = f"https://f4.bcbits.com/img/a{d['art_id']}_10.jpg" if d.get("art_id") else None
    out = []
    for t in d.get("trackinfo") or []:
        if not (t.get("file") or {}).get("mp3-128"):
            continue            # not streamable (preorder, or the artist turned it off)
        out.append({"title": t.get("title") or "?", "duration": t.get("duration") or 0,
                    "album": album, "year": year, "page": page_url, "art": art,
                    "id": t.get("track_id") or t.get("id")})
    return out


def tracks(band_url: str, want: int = 8, max_releases: int = 3) -> list[dict]:
    """Streamable tracks from the band's releases, as their page orders them
    (featured / newest first), until `want` are found."""
    root = band_url.rstrip("/")
    page = _get(root + "/music")
    found = _tracks_of(page, root + "/music")      # one release: /music *is* it
    if found:
        return found[:want]
    links = list(dict.fromkeys(re.findall(r'href="(/(?:album|track)/[^"?#]+)', page)))
    for path in links[:max_releases]:
        found += _tracks_of(_get(root + path), root + path)
        if len(found) >= want:
            break
    return found[:want]


def stream_url(track: dict) -> str:
    """A fresh mp3 URL for `track`: the one on its page when it was listed
    has probably expired."""
    for t in (_tralbum(_get(track["page"])) or {}).get("trackinfo") or []:
        same = (track.get("id") and (t.get("track_id") or t.get("id")) == track["id"]) \
            or t.get("title") == track.get("title")
        if same and (t.get("file") or {}).get("mp3-128"):
            return t["file"]["mp3-128"]
    raise LookupError(f"{track.get('title')!r} is no longer streamable on Bandcamp")


def reset() -> None:
    """Forget the throttle's state (tests)."""
    global _last, _blocked_until, _errors
    _last, _blocked_until, _errors = 0.0, 0.0, 0
