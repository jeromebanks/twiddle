"""What the producer remembers between runs: the caches that make a build incremental.

Everything lives in `~/.cache/twiddle/scene/` (the same directory as the
client's state, so the location and `$TWIDDLE_SCENE_CACHE` are unchanged) as
small JSON files, written atomically (`jsonstore`).

  aliases.json      billing -> the trimmed name the music databases knew it by
                    ("Mindi Abair Christmas Show" -> "Mindi Abair"), or "" if none
  bandcamp.json     Bandcamp search answers (`scenedata/bandcamp.py`)
  venue_wiki.json   Wikipedia summaries for venues (`scenedata/venue_info.py`)
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


# ---- aliases ----------------------------------------------------------------

ALIAS_TTL_S = 14 * 86400        # a band that finds nothing today may be added later


def alias(billed: str) -> str | None:
    """The name a billing was found under; "" if trims were tried and failed;
    None if never tried (or long enough ago to try again)."""
    hit = _read("aliases.json").get(norm(billed))
    if not hit or time.time() - hit.get("at", 0) > ALIAS_TTL_S:
        return None
    return hit.get("alias", "")


def pick(billed: str) -> str:
    """The MusicBrainz id someone (or a rule) chose for this billing, else ""."""
    return (_read("picks.json").get(norm(billed)) or {}).get("mbid", "")


def save_pick(billed: str, mbid: str) -> None:
    picks = _read("picks.json")
    picks[norm(billed)] = {"mbid": mbid, "billed": billed, "at": time.time()}
    _write("picks.json", picks)


def drop_pick(billed: str, only: str = "") -> None:
    """Forget a chosen MusicBrainz id (`only`: just if it is that one)."""
    picks = _read("picks.json")
    if norm(billed) in picks and (not only or picks[norm(billed)].get("mbid") == only):
        del picks[norm(billed)]
        _write("picks.json", picks)


def drop_alias(billed: str, only: str = "") -> None:
    """Forget an alias (`only`: just if it is that name; a build's own are left alone)."""
    aliases = _read("aliases.json")
    if norm(billed) in aliases and (not only or aliases[norm(billed)].get("alias") == only):
        del aliases[norm(billed)]
        _write("aliases.json", aliases)


def save_alias(billed: str, name: str) -> None:
    aliases = _read("aliases.json")
    aliases[norm(billed)] = {"alias": name, "billed": billed, "at": time.time()}
    _write("aliases.json", aliases)
