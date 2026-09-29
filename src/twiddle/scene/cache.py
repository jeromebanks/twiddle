"""What `scene` remembers between runs: band matches, aliases, the device.
(The shows themselves are `dataset.py`'s, published by `scene build`.)

Everything lives in `~/.cache/twiddle/scene/` as small JSON files, written
atomically (temp file + rename) so a crash mid-write cannot leave a file the
next launch refuses to read.

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
from pathlib import Path

from ..lookup import norm

CACHE_DIR = Path(os.environ.get("TWIDDLE_SCENE_CACHE",
                                Path.home() / ".cache" / "twiddle" / "scene"))


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
