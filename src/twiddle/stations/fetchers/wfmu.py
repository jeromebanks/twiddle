"""WFMU: its ICY title names song, artist and show (`titles.wfmu`); the
playlist RSS (not real-time) only when the title names no show."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from .. import net
from ..model import NowPlaying, Station
from .icy import icy


def wfmu(station: Station) -> NowPlaying:
    np = icy(station, titles="wfmu")
    try:
        root = ET.fromstring(net.get("https://wfmu.org/playlistfeed.xml"))
        latest = root.find("./channel/item/title")
        if latest is not None and latest.text and not np.show:
            # "WFMU Playlist: <Name>'s show from <date>" -- not real-time, so say so
            np.show = latest.text.replace("WFMU Playlist: ", "") + " (latest published)"
    except Exception:
        pass
    return np
