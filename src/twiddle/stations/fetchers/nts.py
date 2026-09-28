"""NTS publishes the show, not the song: title, genres and artwork.
Needs `fetch_args = { channel = "1" }` (or "2")."""
from __future__ import annotations

import html

from .. import net
from ..model import NowPlaying, Station


def nts(station: Station, channel: str) -> NowPlaying:
    live = net.get_json("https://www.nts.live/api/v2/live")["results"]
    now = next(r["now"] for r in live if r.get("channel_name") == channel)
    details = (now.get("embeds") or {}).get("details") or {}
    genres = [g.get("value") for g in details.get("genres") or [] if g.get("value")]
    media = details.get("media") or {}
    return NowPlaying(station.name, raw_title=html.unescape(now.get("broadcast_title") or ""),
                      note=f"({', '.join(genres[:4])})" if genres else None,
                      art_url=media.get("picture_large") or media.get("background_large"))
