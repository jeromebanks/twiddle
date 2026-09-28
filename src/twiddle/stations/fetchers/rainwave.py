"""Rainwave (rainwave.cc): listener-voted video game music, one channel per
stream. The stream's ICY title is blank, but its public API gives the song,
its composers or remixers, the game (as the album), cover art and the last
few plays. The channel is read from the stream URL
(`relay.rainwave.cc/<key>.mp3`).

Measured 2026-09-27: ICY blank on all channels; `api4/info?sid=N` needs no
key; channel ids are from `api4/stations`.
"""
from __future__ import annotations

import re
import time

from .. import net
from ..model import NowPlaying, Station

API = "https://rainwave.cc/api4/info?sid={sid}"
ART = "https://rainwave.cc{art}_320.jpg"
# `api4/stations`, 2026-09-27.
CHANNELS = {"game": 1, "ocremix": 2, "covers": 3, "chiptune": 4, "all": 5, "chill": 6}


def channel_of(url: str) -> int | None:
    m = re.search(r"rainwave\.cc/([a-z]+)\.(?:mp3|ogg)", url)
    return CHANNELS.get(m.group(1)) if m else None


def rainwave_song(song: dict) -> dict:
    """One API song -> artist/song/album/art_url, the album being the game."""
    album = (song.get("albums") or [{}])[0]
    row = {"artist": ", ".join(a["name"] for a in song.get("artists") or [] if a.get("name")),
           "song": song.get("title"), "album": album.get("name"),
           "art_url": ART.format(art=album["art"]) if album.get("art") else None}
    return {k: v for k, v in row.items() if v}


def parse_rainwave(info: dict) -> NowPlaying:
    cur = info["sched_current"]
    now = rainwave_song(cur["songs"][0])
    # `artist` is what the dial and `np -i` look up on MusicBrainz, and six
    # comma-joined composers match nothing: look up the first, show the rest.
    names = [a["name"] for a in cur["songs"][0].get("artists") or [] if a.get("name")]

    def row(event: dict) -> dict:
        when = time.strftime("%H:%M", time.localtime(event["start_actual"])) \
            if event.get("start_actual") else None
        return ({"time": when} if when else {}) | rainwave_song(event["songs"][0])
    recent = [row(e) for e in info.get("sched_history") or [] if e.get("songs")]
    return NowPlaying("", artist=names[0] if names else None, song=now.get("song"),
                      album=now.get("album"), art_url=now.get("art_url"), recent=recent,
                      note=f"(with {', '.join(names[1:])})" if len(names) > 1 else None)


def rainwave(station: Station, sid: int | None = None) -> NowPlaying:
    np = parse_rainwave(net.get_json(API.format(sid=sid or channel_of(station.url))))
    np.source = station.name
    return np
