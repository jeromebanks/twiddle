"""How a station builds its ICY title: a registry of named title shapes.

An ICY `StreamTitle` is free text, and stations fill it differently: most
send "Artist - Song", iHeart sends `title="Song",artist="Artist",url="..."`.
A catalog file names its shape with `fetch_args = { titles = "<shape>" }`;
without one, the `icy` fetcher reads iHeart attributes first and then
"Artist - Song", which is what every station got before shapes existed.

Each shape is a pure `str -> dict` of NowPlaying fields (artist, song,
show, hosts, art_url), and `{}` when the title doesn't have its shape. A
guess where the shape doesn't fit would send a nonsense artist on to
`discover` or an info lookup, so a shape only says what it is sure of.
"""
from __future__ import annotations

import re
from collections.abc import Callable


def artist_song(title: str) -> dict:
    """"Artist - Song", split at the first " - "; nothing without one.

    An ICY title can just as well be a show name or "Your DJ speaks over
    ..." (WFMU), so only that exact shape is trusted."""
    if not title or " - " not in title:
        return {}
    artist, song = (s.strip() for s in title.split(" - ", 1))
    return {k: v for k, v in (("artist", artist), ("song", song)) if v}


def iheart_rewrite(title: str | None) -> str | None:
    """iHeart's `title="Song",artist="Artist",url="..."` as "Artist - Song";
    any other title unchanged."""
    if not title:
        return title
    fields = dict(re.findall(r'(\w+)="(.*?)"(?:,|$)', title))
    if fields.get("artist") and fields.get("title"):
        return f"{fields['artist']} - {fields['title']}"
    return title


def iheart_attrs(title: str) -> dict:
    # Rewritten and then split, rather than read straight from the
    # attributes: an artist="A - B" has always come out as artist "A", and
    # changing that here would change what those stations show.
    tidied = iheart_rewrite(title)
    return artist_song(tidied) if tidied != title else {}


SHAPES: dict[str, Callable[[str], dict]] = {
    "artist-song": artist_song,
    "iheart-attrs": iheart_attrs,
}


def parse(shape: str | None, title: str | None) -> dict:
    """The NowPlaying fields `title` yields under `shape`; None is the
    default (iHeart attributes, else "Artist - Song")."""
    if not title:
        return {}
    if shape is None:
        return iheart_attrs(title) or artist_song(title)
    return SHAPES[shape](title)
