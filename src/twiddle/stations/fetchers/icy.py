"""The fallback every station has: the stream's own ICY title."""
from __future__ import annotations

from .. import icy as icy_meta
from ..model import NowPlaying, Station


def icy(station: Station, note: str | None = None, music: bool = True,
        encoding: str | None = None) -> NowPlaying:
    """`music=False` for stations whose titles are never "Artist - Song"
    (talk, rain loops): the title is shown but never taken for an artist.
    `encoding` for a server that doesn't send UTF-8 ("cp932": Shift-JIS)."""
    raw = (icy_meta.icy_title(station.url, encoding=encoding) if encoding
           else icy_meta.icy_title(station.url))
    title = icy_meta.tidy_title(raw)
    artist, song = icy_meta.split_title(title) if music else (None, None)
    return NowPlaying(station.name, artist=artist, song=song,
                      raw_title=title, note=note)


def talk(station: Station) -> NowPlaying:
    # Talk/news: an ICY title there is a segment name, never a song, so it is
    # never trusted as an artist.
    return icy(station, note="(talk/news: this is the segment, not a song)", music=False)
