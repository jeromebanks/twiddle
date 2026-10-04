"""Which Bandcamp band is this? The search, the cache and the choice.

Three things here share one search: the band's photo and tags, a playable
release, and the whole-listing genre scan. The throttle and the songs are
`twiddle.bandcamp`; this module adds an on-disk answer cache (weeks long).

## Which band is it?

Names collide, so a band is chosen, not guessed:
  the Bandcamp page MusicBrainz/Discogs link -> the only exact-name band
  -> the only exact-name band in the Bay Area / California -> none.
Labels are never a band.
"""
from __future__ import annotations

import threading
import time

from .. import lookup
from ..bandcamp import BlockedError, _guarded, _host      # noqa: F401  (BlockedError: callers catch it here)
from . import cache

HIT_TTL_S = 14 * 86400
MISS_TTL_S = 3 * 86400
KEEP = ("name", "item_url_root", "location", "is_label", "tag_names", "img", "genre_name")

_cache_lock = threading.Lock()


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
