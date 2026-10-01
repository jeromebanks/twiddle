"""What the `scene` client remembers between runs: your pins, the device, the theme.
(The shows themselves are the dataset's; the producer's caches are `scenedata/cache.py`.)

Everything lives in `~/.cache/twiddle/scene/` as small JSON files, written
atomically (temp file + rename) so a crash mid-write cannot leave a file the
next launch refuses to read.

  band_pins.json  "this band is *that* Spotify artist" (or "not on Spotify"),
                  chosen by hand -- the only fully trustworthy identity
  state.json      last device, theme
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .. import jsonstore
from ..lookup import norm

CACHE_DIR = Path(os.environ.get("TWIDDLE_SCENE_CACHE",
                                Path.home() / ".cache" / "twiddle" / "scene"))


def _read(name: str) -> dict:
    return jsonstore.read(CACHE_DIR / name)


def _write(name: str, data: dict) -> None:
    jsonstore.write(CACHE_DIR / name, data)


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
