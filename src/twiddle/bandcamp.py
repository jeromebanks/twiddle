"""Bandcamp's site as one polite guest: a throttled fetch, and a release's songs.

Bandcamp is where most bands on The List are. The *search* for who a band is
lives with the producer (`scenedata/bandcamp.py`); this is the part both sides
use -- the throttle, and the songs and fresh stream URLs of a release, which
the client needs at the moment of playing.

## Being a polite guest

The search is Bandcamp's own site autocomplete, not a published API, and
the genre scan asks about every band in the listing. So every request goes
through one throttle (`MIN_INTERVAL_S` apart), and a 429 or a run of errors stops all of it for a while
(`BlockedError`) instead of hammering. A block would also cost `dial` its
Bandcamp photos and `lookup` its Bandcamp fallback -- the Spotify quota
lockout of 2026-09-24 is the same lesson.

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

from . import netstats

MIN_INTERVAL_S = 1.0
BLOCK_S = 15 * 60
MAX_ERRORS = 3              # in a row, then back off as if blocked
TIMEOUT = 10
UA = "Mozilla/5.0 twiddle (personal use)"

_throttle = threading.Lock()
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
            netstats.record_wait("bandcamp", wait)
            time.sleep(wait)
        _last = time.monotonic()


def blocked_for() -> float:
    """Seconds until a back-off ends (0 when Bandcamp isn't resting)."""
    return max(0.0, _blocked_until - time.monotonic())


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
        if exc.code == 429:               # it started the back-off: say so, as every later call will
            raise BlockedError("Bandcamp answered 429 (too many requests); resting") from exc
        raise
    except Exception as exc:
        _outcome(False, exc)
        raise
    _outcome(True)
    return out


def _get(url: str) -> str:
    def go():
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                netstats.record("bandcamp", status=getattr(resp, "status", 200))
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            netstats.record("bandcamp", status=exc.code)
            raise
        except urllib.error.URLError:
            netstats.record("bandcamp")
            raise
    return _guarded(go)


def _host(url: str | None) -> str:
    return (urllib.parse.urlsplit(url or "").hostname or "").lower()


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
