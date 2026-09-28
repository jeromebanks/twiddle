"""Spinitron, the DJ-logging software most college/community stations run.

Its JSON API needs a per-station key none of them publish, but the public
page at spinitron.com/<CALLSIGN> is small server-rendered HTML with stable
class names (`show-title`, `dj-name`, `data-spin`), so it is scraped. The
call sign is the station's `name` unless `fetch_args = { callsign = ... }`
says otherwise.
"""
from __future__ import annotations

import html
import json
import re

from .. import net
from ..model import NowPlaying, Station
from .icy import icy


def parse_spinitron(page: str) -> dict:
    """The current spin and show out of a spinitron.com/<CALL> page."""
    out: dict = {}
    spin = re.search(r'data-spin="([^"]+)"', page)
    if spin:
        s = json.loads(html.unescape(spin.group(1)))
        out |= {"artist": s.get("a"), "song": s.get("s"), "album": s.get("r")}
    show = re.search(r'<h3 class="show-title">\s*<a[^>]*>([^<]+)</a>', page)
    if show:
        out["show"] = html.unescape(show.group(1).strip())
    djs = re.search(r'<p class="dj-name">(.*?)</p>', page, re.DOTALL)
    if djs:
        out["hosts"] = [html.unescape(n) for n in re.findall(r"<a[^>]*>([^<]+)</a>", djs.group(1))]
    return out


def bigger_art(url: str | None, px: int = 600) -> str | None:
    """Spinitron's covers are Apple Music thumbnails ("…/150x150bb.jpg"),
    which render any size asked for; ask for one worth drawing."""
    return re.sub(r"/\d+x\d+bb\.(jpg|png|webp)$", rf"/{px}x{px}bb.\1", url) if url else url


def parse_spinitron_spins(page: str) -> list[dict]:
    """Every spin on a spinitron.com/<CALL> page, newest first.

    Each row carries the same `data-spin` JSON as the current one, plus its
    air time and, usually, a cover (a placeholder loudspeaker when not).
    """
    out = []
    for row in page.split('class="spin-item"')[1:]:
        spin = re.search(r'data-spin="([^"]+)"', row)
        if not spin:
            continue
        s = json.loads(html.unescape(spin.group(1)))
        when = re.search(r'<td class="spin-time">\s*(?:<a[^>]*>)?\s*([^<]+?)\s*<', row)
        art = re.search(r'<td class="spin-art">.*?<img[^>]*\bsrc="([^"]+)"', row, re.DOTALL)
        art_url = html.unescape(art.group(1)) if art else None
        if art_url and ("placeholder" in art_url or art_url.endswith(".svg")):
            art_url = None
        out.append({k: v for k, v in {
            "time": when.group(1) if when else None, "artist": s.get("a"),
            "song": s.get("s"), "album": s.get("r"),
            "art_url": bigger_art(art_url)}.items() if v})
    return out


def spinitron(station: Station, callsign: str | None = None) -> NowPlaying:
    page = net.get(f"https://spinitron.com/{callsign or station.name}").decode("utf-8", "replace")
    found = parse_spinitron(page)
    spins = parse_spinitron_spins(page)
    if found.get("artist") or found.get("song"):
        np = NowPlaying(station.name)
    else:
        # Page shape changed, or nothing logged recently: the stream's own
        # ICY title beats saying nothing.
        np = icy(station)
    for k, v in found.items():
        if v:
            setattr(np, k, v)
    # The first row is the spin `data-spin` named above; trust its cover only
    # when it really is the same song.
    if spins and (spins[0].get("artist"), spins[0].get("song")) == (np.artist, np.song):
        np.art_url = spins[0].get("art_url")
        np.recent = spins[1:]
    elif spins:
        np.recent = spins
    return np
