"""streamabc's per-channel metadata: the song on air, with iTunes cover art.

streamabc (regiocast) hosts the German decade stations, 80s80s, 90s90s and
Sunshine Live's channels among them, whose ICY title is only the channel
name ("90s90s - DIGITAL WEB"). Found 2026-09-27: the sites' players read
`api.streamabc.net/metadata/channel/<channelkey>.json`, public and with no
key. The channelkey (`regc_...`, `sunsl_...`) is in each site's Nuxt payload
as `audiotheque_channel_external_id`; the response's `channel` repeats the
stream's ICY name, which is how a key is matched to a stream. Tried first:
the sites' own `iris-<brand>.loverad.io/flow.json?station=<id>` works too,
but its ids are per brand and nothing ties one to a stream URL.

No ICY fallback: it would only ever show the channel name.
"""
from __future__ import annotations

from .. import net
from ..model import NowPlaying, Station

URL = "https://api.streamabc.net/metadata/channel/{}.json"


def parse_streamabc(data: dict, source: str) -> NowPlaying:
    images = data.get("images") or {}
    art = (images.get("medium") or {}).get("url") or data.get("cover") or None
    return NowPlaying(source, artist=data.get("artist") or None, song=data.get("song") or None,
                      album=data.get("album") or None, art_url=art)


def streamabc(station: Station, channel: str) -> NowPlaying:
    return parse_streamabc(net.get_json(URL.format(channel)), station.name)
