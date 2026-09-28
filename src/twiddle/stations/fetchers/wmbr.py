"""WMBR logs no songs in real time, but names the show and its host."""
from __future__ import annotations

import html
import re

from .. import net
from ..model import NowPlaying, Station
from .icy import icy


def wmbr(station: Station) -> NowPlaying:
    xml = net.get("https://wmbr.org/dynamic.xml").decode("utf-8", "replace")
    m = re.search(r"<wmbr_show>(.*?)</wmbr_show>", xml, re.DOTALL)
    inner = html.unescape(m.group(1)) if m else ""
    show = re.search(r"<a[^>]*>([^<]+)</a>", inner)
    host = re.search(r"with ([^<]+)", inner)
    if not show:
        return icy(station, music=False)
    return NowPlaying(station.name, raw_title=show.group(1).strip(),
                      note=f"(with {host.group(1).strip()})" if host else None)
