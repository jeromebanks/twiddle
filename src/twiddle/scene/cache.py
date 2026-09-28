"""What `scene` remembers between runs: listings, band matches, the device.

Everything lives in `~/.cache/twiddle/scene/` as small JSON files, written
atomically (temp file + rename) so a crash mid-write cannot leave a file the
next launch refuses to read.

  listings.json   the last fetch, shown instantly at launch, refreshed after
  band_pins.json  "this band is *that* Spotify artist" (or "not on Spotify"),
                  chosen by hand -- the only fully trustworthy identity
  aliases.json    billing -> the trimmed name the music databases knew it by
                  ("Mindi Abair Christmas Show" -> "Mindi Abair"), or "" if none
  state.json      last device, theme
"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

from ..lookup import norm
from .model import Show

CACHE_DIR = Path(os.environ.get("TWIDDLE_SCENE_CACHE",
                                Path.home() / ".cache" / "twiddle" / "scene"))
LISTINGS_TTL_S = 6 * 3600


def _path(name: str) -> Path:
    return CACHE_DIR / name


def _read(name: str) -> dict:
    try:
        return json.loads(_path(name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write(name: str, data: dict) -> None:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(p)


# ---- listings ---------------------------------------------------------------


def save_listings(shows: list[Show], sources: list[str] | None = None) -> None:
    """`sources`: the names of the sources this fetch asked, so a new one
    (a venue's own calendar, added since) makes the cache stale at once."""
    _write("listings.json", {"at": time.time(), "sources": sorted(sources or []),
                             "shows": [s.to_dict() for s in shows]})


def load_listings() -> tuple[list[Show], float | None]:
    """(shows, fetched_at) from the last fetch; ([], None) if there is none."""
    data = _read("listings.json")
    shows = []
    for d in data.get("shows", []):
        try:
            d = dict(d, day=date.fromisoformat(d["day"]))
            shows.append(Show(**d))
        except (KeyError, TypeError, ValueError):
            continue            # an older cache shape: skip, the refresh fixes it
    return shows, data.get("at")


def listings_fresh(at: float | None, sources: list[str] | None = None) -> bool:
    """Young enough, and fetched from every source in `sources`.

    Without the second test a source added since the last fetch waited out
    the rest of the 6h: Ivy Room's flyers (2026-09-26) didn't appear until
    a manual refresh.
    """
    if at is None or time.time() - at >= LISTINGS_TTL_S:
        return False
    return not sources or set(sources) <= set(_read("listings.json").get("sources") or [])


# ---- aliases ----------------------------------------------------------------

ALIAS_TTL_S = 14 * 86400        # a band that finds nothing today may be added later


def alias(billed: str) -> str | None:
    """The name a billing was found under; "" if trims were tried and failed;
    None if never tried (or long enough ago to try again)."""
    hit = _read("aliases.json").get(norm(billed))
    if not hit or time.time() - hit.get("at", 0) > ALIAS_TTL_S:
        return None
    return hit.get("alias", "")


def save_alias(billed: str, name: str) -> None:
    aliases = _read("aliases.json")
    aliases[norm(billed)] = {"alias": name, "billed": billed, "at": time.time()}
    _write("aliases.json", aliases)


# ---- band pins --------------------------------------------------------------


def pin(band: str, spotify_id: str | None, name: str = "") -> None:
    """Remember which Spotify artist a band is. `None` = "not on Spotify"."""
    pins = _read("band_pins.json")
    pins[norm(band)] = {"spotify_id": spotify_id, "name": name, "at": time.time()}
    _write("band_pins.json", pins)


def unpin(band: str) -> None:
    pins = _read("band_pins.json")
    if pins.pop(norm(band), None) is not None:
        _write("band_pins.json", pins)


def pinned(band: str) -> dict | None:
    """{"spotify_id": str | None, "name": str} if chosen by hand, else None."""
    return _read("band_pins.json").get(norm(band))


# ---- ui state ---------------------------------------------------------------


def get_state(key: str, default=None):
    return _read("state.json").get(key, default)


def set_state(key: str, value) -> None:
    state = _read("state.json")
    state[key] = value
    _write("state.json", state)
