"""Pictures for `dial`: album covers, artist photos, station logos, colour.

Where a picture comes from, best first:
  cover   the station's own (KEXP, Spinitron) -> Cover Art Archive for the
          album MusicBrainz named -> the station logo -> a generated tile
  artist  Wikipedia's page thumbnail -> their Bandcamp band photo
          -> a generated monogram

Fetched images are cached on disk as PNGs (`~/.cache/twiddle/dial/art/`),
so a station's logo or a song heard twice costs nothing. Every function
here is best-effort and never raises: art is garnish, like `termimage`.
"""
from __future__ import annotations

import colorsys
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .. import lookup, termimage
from . import state

MAX_PX = 600
RETRY_FAILED_S = 300        # a timeout is not forever: try a failed URL again
_failed: dict[str, float] = {}      # url -> when it failed
_wiki: dict[str, str | None] = {}
_bandcamp: dict[tuple, str | None] = {}
_lock = threading.Lock()


def art_dir() -> Path:
    return state.CACHE_DIR / "art"


def load(url: str | None) -> Image.Image | None:
    """The image at `url`, from the disk cache or the network; None on any failure.

    Flicking between stations runs several loads at once, often of the same
    cover, so the cache file is written whole or not at all: a reader that
    caught it half-written used to fail, and the failure hid that cover for
    the rest of the session.
    """
    if not url:
        return None
    failed_at = _failed.get(url)
    if failed_at is not None and time.monotonic() - failed_at < RETRY_FAILED_S:
        return None
    path = art_dir() / (hashlib.sha1(url.encode()).hexdigest()[:24] + ".png")
    try:
        if path.exists():
            with Image.open(path) as im:
                return im.convert("RGBA")
        im = Image.open(io.BytesIO(termimage.fetch(url)))
        im.load()
        im = im.convert("RGBA")
        im.thumbnail((MAX_PX, MAX_PX))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        im.save(tmp, format="PNG")
        os.replace(tmp, path)
        return im
    except Exception:
        with _lock:
            _failed[url] = time.monotonic()
        return None


def cover_art_archive(release_group_mbid: str | None) -> str | None:
    if not release_group_mbid:
        return None
    return f"https://coverartarchive.org/release-group/{release_group_mbid}/front-500"


def wikipedia_image(page_url: str | None) -> str | None:
    """The lead image of an English Wikipedia article, via its REST summary.

    `lookup` already reads that summary but keeps only the text, and its
    month-long cache would hide a new field for a month -- so ask here.
    """
    if not page_url or "/wiki/" not in page_url:
        return None
    if page_url in _wiki:
        return _wiki[page_url]
    title = page_url.rsplit("/wiki/", 1)[-1]
    url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
           + urllib.parse.quote(urllib.parse.unquote(title), safe=""))
    try:
        page = json.loads(termimage.fetch(url))
        found = (page.get("thumbnail") or {}).get("source")
    except Exception:
        found = None
    _wiki[page_url] = found
    return found


def bandcamp_photo(name: str | None, page_url: str | None = None) -> str | None:
    """The band photo from the artist's Bandcamp page -- where the small
    artists college radio plays have a picture and no Wikipedia article.

    A shared name must not borrow someone else's face: the band `page_url`
    (a Bandcamp link MusicBrainz or Discogs gave) names wins; without one,
    only a name exactly one Bandcamp band has is trusted.
    """
    if not name:
        return None
    key = (name, page_url)
    if key in _bandcamp:
        return _bandcamp[key]
    try:
        bands = [b for b in lookup.bandcamp_bands(name) if b.get("img")]
    except Exception:
        return None             # not remembered: a network blip may pass
    host = urllib.parse.urlsplit(page_url or "").hostname
    linked = [b for b in bands
              if host and urllib.parse.urlsplit(b.get("item_url_root", "")).hostname == host]
    band = linked[0] if linked else bands[0] if len(bands) == 1 else None
    # `_23` is Bandcamp's 300px size; `_16` is 700px, enough for MAX_PX
    found = re.sub(r"_\d+\.jpg$", "_16.jpg", band["img"]) if band else None
    _bandcamp[key] = found
    return found


def artist_photo(info: lookup.ArtistInfo, as_heard: str | None = None) -> str | None:
    """Wikipedia's picture for the well known, else their Bandcamp photo.

    Bandcamp is searched by name as MusicBrainz spells it and, failing that,
    as the station did: MusicBrainz's "Lænz" is "Laenz" on Bandcamp.
    """
    found = wikipedia_image(info.links.get("wikipedia"))
    for name in dict.fromkeys([info.name, as_heard]):
        found = found or bandcamp_photo(name, info.links.get("bandcamp"))
    return found


# ---- colour -------------------------------------------------------------------


def _rgb(h: float, l: float, s: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return int(r * 255), int(g * 255), int(b * 255)


def color_for(key: str) -> tuple[int, int, int]:
    """A stable, pleasant colour per name, for things with no picture."""
    h = int(hashlib.md5(key.encode()).hexdigest()[:6], 16) / 0xFFFFFF
    return _rgb(h, 0.55, 0.55)


def dominant(im: Image.Image | None, fallback: str = "") -> tuple[int, int, int]:
    """The picture's most characterful colour, lifted to read on a dark theme.

    The most *common* colour of a cover is usually its black or white border,
    so saturation is weighted in: a small red logo beats a big grey field.
    """
    if im is None:
        return color_for(fallback)
    small = im.convert("RGB").resize((48, 48))
    q = small.quantize(colors=8)
    pal = q.getpalette() or []
    best, best_score = None, -1.0
    for count, idx in q.getcolors() or []:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        if l < 0.08 or l > 0.95:
            continue
        score = count * (0.15 + s) * (0.4 + min(l, 1 - l))
        if score > best_score:
            best, best_score = (h, l, s), score
    if best is None:
        return color_for(fallback)
    h, l, s = best
    return _rgb(h, min(max(l, 0.5), 0.72), max(s, 0.35) if s > 0.05 else s)


def plate(im: Image.Image | None) -> Image.Image | None:
    """A logo with see-through parts, laid on a light square.

    Station logos are drawn for white web pages: KSPC's is black on
    transparent, which on a dark theme is no logo at all.
    """
    if im is None or im.mode != "RGBA" or im.getextrema()[3][0] == 255:
        return im
    bg = Image.new("RGBA", im.size, (242, 242, 238, 255))
    return Image.alpha_composite(bg, im)


def hex_of(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# ---- generated tiles ------------------------------------------------------------


def tile(text: str, rgb: tuple[int, int, int], size: int = 400) -> Image.Image:
    """A gradient square with `text` on it: the picture for things with none."""
    im = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(im)
    top = tuple(min(255, int(c * 1.05)) for c in rgb)
    bottom = tuple(int(c * 0.25) for c in rgb)
    for y in range(size):
        t = y / size
        draw.line([(0, y), (size, y)],
                  fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    text = text[:4]
    try:
        font = ImageFont.load_default(size=int(size / max(2.2, len(text) * 0.62)))
    except TypeError:           # Pillow < 10.1
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    draw.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), text,
              font=font, fill=(250, 250, 250))
    return im


def monogram(name: str) -> Image.Image:
    words = [w for w in name.replace("&", " ").split() if w[:1].isalnum()]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    return tile(initials, color_for(name))
