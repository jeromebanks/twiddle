"""ICY in-stream metadata (the Shoutcast/Icecast de facto standard).

The stream carries a `StreamTitle` chunk every `icy-metaint` bytes. It is
only a free-text string the station chooses to populate -- no DJ,
sometimes blank, sometimes an ad-break placeholder.
"""
from __future__ import annotations

import re
import urllib.request

from . import net, titles


def icy_title(url: str, encoding: str = "utf-8") -> str | None:
    """The stream's current `StreamTitle`, or None if it carries none.

    `encoding` is per station, never guessed: a Japanese server sending
    Shift-JIS (cp932) is decoded as such only when its catalog entry says
    so, because a cp932 fallback would turn a Western station's stray
    latin-1 "é" plus the next byte into a plausible-looking kanji."""
    # Some Icecast servers only send metadata to player-looking agents.
    req = urllib.request.Request(url, headers={"Icy-MetaData": "1", "User-Agent": "VLC/3.0.9"})
    with urllib.request.urlopen(req, timeout=net.TIMEOUT) as resp:
        metaint = resp.headers.get("icy-metaint")
        if not metaint:
            return None
        resp.read(int(metaint))
        length = resp.read(1)[0] * 16
        if length == 0:
            return None
        raw = resp.read(length)
    return parse_icy(decode_meta(raw, encoding))


def decode_meta(raw: bytes, encoding: str = "utf-8") -> str:
    return raw.rstrip(b"\x00").decode(encoding, "replace")


def parse_icy(meta: str) -> str | None:
    # Titles contain unescaped apostrophes ("johnny's theme"), so stop at the
    # "';" before the next field (StreamUrl=, NTS's json=) or the end, not at
    # the first "'".
    m = re.search(r"StreamTitle='(.*?)';(?:[A-Za-z_]+=|$)", meta, re.DOTALL)
    return m.group(1) if m and m.group(1) else None


def split_title(title: str | None) -> tuple[str | None, str | None]:
    """("Artist", "Song") out of an ICY "Artist - Song", or (None, None):
    the `artist-song` shape in `titles.py`, as a pair."""
    got = titles.artist_song(title or "")
    return got.get("artist"), got.get("song")


def tidy_title(title: str | None) -> str | None:
    """iHeart streams send `title="Song",artist="Artist",url="..."` where
    everyone else sends "Artist - Song"; turn the one into the other."""
    return titles.iheart_rewrite(title)
