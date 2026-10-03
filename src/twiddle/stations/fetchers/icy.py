"""The fallback every station has: the stream's own ICY title."""
from __future__ import annotations

from .. import icy as icy_meta
from .. import titles as title_shapes
from ..model import NowPlaying, Station


def icy(station: Station, note: str | None = None, music: bool = True,
        encoding: str | None = None, titles: str | None = None) -> NowPlaying:
    """`music=False` for stations whose titles are never "Artist - Song"
    (talk, rain loops): the title is shown but never taken for an artist.
    `encoding` for a server that doesn't send UTF-8 ("cp932": Shift-JIS).
    `titles` names the title's shape in `titles.SHAPES`; by default iHeart
    attributes, else "Artist - Song". Whatever the shape, `raw_title` is the
    title with any iHeart attributes rewritten, and under `music=False` a
    shape can still give the show or hosts but never an artist or song."""
    raw = (icy_meta.icy_title(station.url, encoding=encoding) if encoding
           else icy_meta.icy_title(station.url))
    fields = title_shapes.parse(titles, raw)
    if not music:
        fields = {k: v for k, v in fields.items() if k not in ("artist", "song")}
    return NowPlaying(station.name, raw_title=icy_meta.tidy_title(raw), note=note, **fields)

def talk(station: Station) -> NowPlaying:
    # Talk/news: an ICY title there is a segment name, never a song, so it is
    # never trusted as an artist.
    return icy(station, note="(talk/news: this is the segment, not a song)", music=False)
