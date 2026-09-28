"""Radio France's `livemeta` feed (FIP and its webradios). Needs
`fetch_args = { rf_id = N }`: the station's id in that API."""
from __future__ import annotations

import time

from .. import net
from ..model import NowPlaying, Station


def parse_radiofrance(data: dict, now: float) -> NowPlaying | None:
    """Every recent step, songs among them.

    The current song is the one whose [start, end) holds `now`; between two
    songs, the latest one started.
    """
    songs = sorted((x for x in data.get("steps", {}).values() if x.get("embedType") == "song"),
                   key=lambda x: x.get("start", 0))
    if not songs:
        return None
    started = [x for x in songs if x.get("start", 0) <= now] or songs[:1]
    cur = next((x for x in started if now < x.get("end", 0)), started[-1])

    def artist(x):
        return x.get("authors") or x.get("performers") or \
            ", ".join(x.get("highlightedArtists") or []) or None

    def row(x):
        return {k: v for k, v in {
            "time": time.strftime("%H:%M", time.localtime(x["start"])) if x.get("start") else None,
            "artist": artist(x), "song": x.get("title"), "album": x.get("titreAlbum"),
            "art_url": x.get("visual")}.items() if v}
    np = NowPlaying("", artist=artist(cur), song=cur.get("title"), album=cur.get("titreAlbum"),
                    art_url=cur.get("visual") or None)
    np.recent = [row(x) for x in reversed(started) if x is not cur][:10]
    return np


def radiofrance(station: Station, rf_id: int) -> NowPlaying:
    np = parse_radiofrance(net.get_json(f"https://api.radiofrance.fr/livemeta/pull/{rf_id}"),
                           time.time())
    if np is None:
        return NowPlaying(station.name, raw_title="(between songs)")
    np.source = station.name
    return np
