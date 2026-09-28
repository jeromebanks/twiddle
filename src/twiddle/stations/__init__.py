"""Radio stations, and what each one is playing right now.

`catalog/` holds one TOML file per station and is the single place a
station's stream URL lives: `tune` plays it, `np` and `dial` ask about it,
and `scripts/radio.zsh` only names a few keys. Adding a station is adding a
file there (see `model.py` for the format, and the `add-radio-station`
skill for the whole procedure).

There is no universal standard for internet-radio "now playing" info, so each
station names the mechanism it has, one module each in `fetchers/`:

1. **ICY in-stream metadata** (`icy.py`): every station has it, but it is a
   free-text string the station chooses to populate.
2. **A station's own feed.** KEXP's API (song, show, MusicBrainz ids), KQED's
   schedule page, WMBR's `dynamic.xml`, WFMU's playlist RSS.
3. **A platform many stations share.** Spinitron (college/community radio,
   scraped from spinitron.com/<CALLSIGN>), SomaFM's song history, Radio
   France's `livemeta`, NTS's live API. These take the station as data, so
   another station on the same platform needs no new code.

Every stream URL is plain http where one exists: `play.play_radio` hands
the speaker an `x-rincon-mp3radio://` URI, which a Sonos fetches over http.

Everything here is read-only: it talks to the stations, never to a speaker.
"""
from __future__ import annotations

from pathlib import Path

from .fetchers.kqed import kqed_row, kqed_slot, parse_kqed_schedule
from .fetchers.radiofrance import parse_radiofrance
from .fetchers.spinitron import bigger_art, parse_spinitron, parse_spinitron_spins
from .icy import icy_title, parse_icy, split_title, tidy_title
from .model import CatalogError, NowPlaying, Station, load_catalog
from .tags import TAGS

__all__ = [
    "STATIONS", "TAGS", "SPOTIFY", "LAST_SOURCE_FILE", "NowPlaying", "Station",
    "CatalogError", "load_catalog", "with_tag", "tag_counts", "remember", "last_source",
    "icy_title", "parse_icy", "split_title", "tidy_title", "parse_spinitron",
    "parse_spinitron_spins", "bigger_art", "parse_radiofrance", "parse_kqed_schedule",
    "kqed_slot", "kqed_row",
]

# What `np` asks about when no station is named: whatever was last tuned, or
# "spotify" once something was played through the relay. Written only by the
# commands that actually change the source (`tune`, `spotify play`, a
# `discover` pick), because reading the speaker instead costs ~6s of SSDP.
LAST_SOURCE_FILE = Path.home() / ".cache" / "twiddle-last-station"
SPOTIFY = "spotify"

STATIONS: dict[str, Station] = load_catalog()


def with_tag(tag: str | None, stations=None) -> list[Station]:
    """The stations carrying `tag`, in list order; all of them for None/"all"."""
    rows = list((stations if stations is not None else STATIONS.values()))
    return rows if tag in (None, "", "all") else [s for s in rows if tag in s.tags]


def tag_counts(stations=None) -> dict[str, int]:
    """How many stations carry each tag, in the vocabulary's order (unused tags left out)."""
    rows = list((stations if stations is not None else STATIONS.values()))
    counts = {t: sum(t in s.tags for s in rows) for t in TAGS}
    return {t: n for t, n in counts.items() if n}


# ---- remembering the source -------------------------------------------------


def remember(source: str) -> None:
    try:
        LAST_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SOURCE_FILE.write_text(source + "\n")
    except OSError:
        pass  # a convenience, never worth failing a play over


def last_source() -> str | None:
    try:
        return LAST_SOURCE_FILE.read_text().strip() or None
    except OSError:
        return None
