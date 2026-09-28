"""A venue's Instagram profile picture, without logging in.

Measured 2026-09-26: a profile page (`instagram.com/<handle>/`) answers a
browser user agent with its `og:image` tag -- the profile picture, 100x100 --
while every API (`web_profile_info`, posts, stories) answers 401 without a
session. So the picture is the one thing to be had anonymously; posts and
their flyers are not (see docs/SCENE.md -> Pictures).

The picture's URL is signed and expires, so the image itself is kept, for
30 days, in `~/.cache/twiddle/scene/instagram/<handle>.jpg`. A failure is
remembered for a day, so a venue with no reachable picture costs one
request a day, not one per cursor move. Call from a worker: it blocks.
"""
from __future__ import annotations

import html
import io
import re
import time

import requests
from PIL import Image

from . import cache

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
KEEP_S = 30 * 86400
RETRY_S = 86400
_OG_IMAGE = re.compile(r'property="og:image" content="([^"]+)"')


def _dir():
    return cache.CACHE_DIR / "instagram"


def picture_url(page: str) -> str | None:
    """The profile picture's URL from a profile page, if it carries one."""
    m = _OG_IMAGE.search(page)
    return html.unescape(m.group(1)) if m else None


def profile_picture(handle: str, timeout: float = 15.0) -> Image.Image | None:
    """The handle's profile picture, from the disk cache or Instagram; None on any failure."""
    if not handle:
        return None
    d = _dir()
    img, failed = d / f"{handle}.jpg", d / f"{handle}.failed"
    now = time.time()
    if img.exists() and now - img.stat().st_mtime < KEEP_S:
        try:
            return Image.open(img).convert("RGB")
        except OSError:
            img.unlink(missing_ok=True)
    if failed.exists() and now - failed.stat().st_mtime < RETRY_S:
        return None
    try:
        page = requests.get(f"https://www.instagram.com/{handle}/", timeout=timeout,
                            headers={"User-Agent": UA}).text
        url = picture_url(page)
        if not url:
            raise ValueError("no og:image")
        data = requests.get(url, timeout=timeout, headers={"User-Agent": UA}).content
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except (requests.RequestException, ValueError, OSError):
        d.mkdir(parents=True, exist_ok=True)
        failed.touch()
        return None
    d.mkdir(parents=True, exist_ok=True)
    tmp = img.with_suffix(".tmp")
    im.save(tmp, "JPEG")
    tmp.replace(img)
    failed.unlink(missing_ok=True)
    return im
