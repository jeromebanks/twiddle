"""SomaFM's per-channel song history: the current song, album, and the
last few, which ICY alone would never give. The channel comes from the
stream URL (`ice1.somafm.com/<channel>-128-mp3`)."""
from __future__ import annotations

import re
import time

from .. import net
from ..model import NowPlaying, Station


def channel_of(url: str) -> str | None:
    m = re.search(r"somafm\.com/([a-z0-9]+)-", url)
    return m.group(1) if m else None


def somafm(station: Station) -> NowPlaying:
    songs = net.get_json(f"https://somafm.com/songs/{channel_of(station.url)}.json")["songs"]

    def row(x: dict) -> dict:
        when = time.strftime("%H:%M", time.localtime(int(x["date"]))) if x.get("date") else None
        return {k: v for k, v in {"time": when, "artist": x.get("artist"), "song": x.get("title"),
                                  "album": x.get("album"),
                                  "art_url": x.get("albumArt") or None}.items() if v}
    cur = songs[0]
    return NowPlaying(station.name, artist=cur.get("artist") or None,
                      song=cur.get("title") or None, album=cur.get("album") or None,
                      art_url=cur.get("albumArt") or None, recent=[row(x) for x in songs[1:10]])
