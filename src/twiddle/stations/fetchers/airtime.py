"""Airtime Pro (airtime.pro): a hosted scheduler that small stations run
their programme on. `https://<id>.airtime.pro/api/live-info-v2` is public
JSON with the show on air, the shows after it, and the track when the
station plays from its library. The id is read from the stream URL
(`<id>.out.airtime.pro`), or given as `fetch_args = { id = "..." }`.

Measured 2026-09-27 on Ottava, FM Setagaya and Shonan Beach FM: all three
feed a live source, so `tracks.current` is a "livestream" with no song, and
their ICY title is a placeholder ("Airtime - offline"). So this never falls
back to ICY: "Airtime" would become an artist. The show name is what there
is; Shonan Beach FM publishes no schedule either, so it shows nothing.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import net
from ..model import NowPlaying, Station

API = "https://{id}.airtime.pro/api/live-info-v2"
# A library track whose "title" is its upload filename ("60min_0239.mp3").
FILENAME = re.compile(r"\.(mp3|wav|flac|m4a|aac|ogg)\s*$", re.IGNORECASE)


def id_of(url: str) -> str | None:
    m = re.search(r"//([a-z0-9]+)\.out\.airtime\.pro", url)
    return m.group(1) if m else None


def _local_hhmm(starts: str | None, tz: ZoneInfo | None) -> str | None:
    # Times are naive, in the station's own timezone: shown in ours.
    try:
        at = datetime.strptime(starts or "", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (at.replace(tzinfo=tz).astimezone() if tz else at).strftime("%H:%M")


def parse_airtime(info: dict) -> NowPlaying:
    try:
        tz = ZoneInfo(info.get("station", {}).get("timezone") or "")
    except (ValueError, KeyError):
        tz = None
    shows = info.get("shows") or {}
    show = shows.get("current") or {}
    track = (info.get("tracks") or {}).get("current") or {}
    meta = track.get("metadata") or {} if track.get("type") == "track" else {}
    artist = (meta.get("artist_name") or "").strip() or None
    song = (meta.get("track_title") or "").strip() or None
    if not (artist and song) or FILENAME.search(song):
        artist = song = None

    def row(s: dict, on_now: bool = False) -> dict:
        r = {"time": _local_hhmm(s.get("starts"), tz), "show": (s.get("name") or "").strip()}
        return {k: v for k, v in r.items() if v} | ({"on_now": True} if on_now else {})
    schedule = ([row(show, True)] if show.get("name") else []) + \
        [row(s) for s in shows.get("next") or [] if s.get("name")]
    name = (show.get("name") or "").strip() or None
    return NowPlaying("", artist=artist, song=song, album=(meta.get("album_title") or None),
                      raw_title=None if song else name, show=name if song else None,
                      art_url=show.get("image_path") or None, schedule=schedule)


def airtime(station: Station, id: str | None = None) -> NowPlaying:
    np = parse_airtime(net.get_json(API.format(id=id or id_of(station.url))))
    np.source = station.name
    return np
