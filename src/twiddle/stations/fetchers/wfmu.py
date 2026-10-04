"""WFMU: what's on air from wfmu.org itself, the stream's title as fallback.

`currentliveshows.php` (under 1 KB) names the song, the artist, the show and
its host, and links the live show's playlist. That playlist page
(`/playlists/shows/<id>`, 100 KB or more) gives the current song's album and
the show so far, one row per song with label, year, format and the DJ's
comment. It is fetched only when the playlist or the song changes.

Off air, or with wfmu.org unreachable, the stream's own ICY title
(`titles.wfmu`) is what's shown.
"""
from __future__ import annotations

import html
import re

from .. import net, titles
from ..model import NowPlaying, Station
from .icy import icy

CURRENT = "https://wfmu.org/currentliveshows.php"
PLAYLIST = "https://wfmu.org/playlists/shows/{}"
# The last playlist fetched: for which (playlist id, artist, song), and its rows.
_cache: dict = {"key": None, "rows": []}

_BLOCK = re.compile(r'<div id="nowplaying">(.*?)</div>', re.DOTALL)
_PLAYLIST_ID = re.compile(r'/playlists/shows/(\d+)')
_ROW = re.compile(r'<tr id="drop_\d+"[^>]*>(.*?)</tr>', re.DOTALL)
# Row fields by the cell's class, in the page's column order.
_CELLS = {"artist": "col_artist", "song": "col_song_title", "album": "col_album_title",
          "label": "col_record_label", "year": "col_year", "format": "col_media",
          "comment": "col_comments"}


def _text(fragment: str) -> str:
    """A cell's visible text: tags dropped, entities decoded, spaces collapsed."""
    fragment = re.sub(r"<br\s*/?>", " ", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return " ".join(html.unescape(fragment).replace("\xa0", " ").split())


def parse_current_live_shows(page: str) -> dict:
    """artist/song/show/hosts/playlist_id for WFMU's main (freeform) stream,
    or {} when the page names no song (off air, or reshaped).

    The page covers one channel today. Should it ever list several, the
    freeform one is the block whose Listen link is the main stream's
    `/wfmu_mp3.pls`; with none such, nothing is guessed."""
    page = re.sub(r"<noscript>.*?</noscript>", "", page, flags=re.DOTALL)
    blocks = _BLOCK.findall(page)
    if len(blocks) > 1:
        blocks = [b for b in blocks if 'href="/wfmu_mp3.pls"' in b]
    if len(blocks) != 1:
        return {}
    block = blocks[0]
    now = re.search(r'<span class="nowplayingtext">(.*?)</span>', block, re.DOTALL)
    out = titles.wfmu(_text(now.group(1))) if now else {}
    if not out.get("song"):
        return {}
    line = re.search(r'<p class="nowplayingreload">(.*?)</p>', block, re.DOTALL)
    if line and _text(line.group(1)):
        show = titles.wfmu(_text(line.group(1)))
        # "Fool's Paradise with Rex" is show and host; "Marty McSorley's
        # show" is just the show, as the stream's own title names it.
        # Only show and host: a line shaped like a song never replaces the song.
        out.update({k: v for k, v in show.items() if k in ("show", "hosts")}
                   if show.get("show") else {"show": _text(line.group(1))})
    pid = _PLAYLIST_ID.search(block)
    if pid:
        out["playlist_id"] = pid.group(1)
    return out


def parse_playlist(page: str, url: str | None = None) -> list[dict]:
    """The playlist's songs, newest first: dicts of artist, song, album,
    label, year, format, comment, playlist_url, song_id, with empty cells
    left out. `url` defaults to the one the page links itself by."""
    if url is None:
        pid = re.search(r"page_type=playlist&amp;page_id=(\d+)", page)
        url = PLAYLIST.format(pid.group(1)) if pid else None
    rows = []
    for tr in _ROW.findall(page):
        row = {}
        for key, cls in _CELLS.items():
            cell = re.search(rf'<td[^>]*class="song {cls}"[^>]*>(.*?)</td>', tr, re.DOTALL)
            if not cell:
                continue
            # The title cell also holds comment buttons and a hidden summary.
            body = re.sub(r'<span class="KDBFavIcon.*', "", cell.group(1), flags=re.DOTALL)
            if value := _text(body):
                row[key] = value
        if not (row.get("artist") or row.get("song")):
            continue
        if row.get("artist"):
            row["artist"] = titles.the_first(row["artist"])
        song_id = re.search(r'KDBsong-(\d+)', tr)
        row.update({k: v for k, v in (("playlist_url", url),
                                      ("song_id", song_id and song_id.group(1))) if v})
        rows.append(row)
    rows.reverse()
    return rows


def _same(row: dict, artist: str | None, song: str | None) -> bool:
    def norm(s):
        return " ".join((s or "").casefold().split())
    return (norm(row.get("song")) == norm(song)
            and norm(titles.lookup_name(row.get("artist"))) == norm(titles.lookup_name(artist)))


def _playlist(pid: str, artist: str | None, song: str | None) -> list[dict]:
    """The playlist's rows, fetched again only when the playlist or the song
    changed. A failed fetch is not cached, so the next poll tries again."""
    key = (pid, artist, song)
    if _cache["key"] != key:
        url = PLAYLIST.format(pid)
        rows = parse_playlist(net.get(url).decode("utf-8", "replace"), url)
        _cache.update(key=key, rows=rows)
    return _cache["rows"]


def wfmu(station: Station) -> NowPlaying:
    try:
        live = parse_current_live_shows(net.get(CURRENT).decode("utf-8", "replace"))
    except Exception:
        live = {}
    if not live.get("playlist_id"):
        return icy(station, titles="wfmu")
    np = NowPlaying(station.name, artist=live.get("artist"), song=live.get("song"),
                    show=live.get("show"), hosts=live.get("hosts", []))
    try:
        rows = _playlist(live["playlist_id"], np.artist, np.song)
    except Exception:
        return np          # the live song and show stand without the playlist
    # The row for the song on air gives its album and is not "just played";
    # rows a DJ entered ahead of it haven't played yet, so they go too.
    at = next((i for i, r in enumerate(rows) if _same(r, np.artist, np.song)), None)
    if at is not None:
        np.album = rows[at].get("album")
        rows = rows[at + 1:]
    np.recent = [dict(r) for r in rows]
    return np
