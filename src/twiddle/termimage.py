"""Draw an image inline in the terminal -- album art and station logos for `np`.

Uses the kitty graphics protocol, which Ghostty, kitty and WezTerm speak. It
only carries PNG (or raw pixels), while covers arrive as JPEG, GIF or PNG, so
everything goes through Pillow and is re-encoded as a small PNG.

Read-only and best-effort: art is garnish, so any failure -- an unsupported
terminal, a dead image URL, output piped to a file -- draws nothing and
raises nothing.
"""
from __future__ import annotations

import base64
import io
import os
import sys
import urllib.request

TERMINALS = {"ghostty", "kitty", "wezterm"}
CHUNK = 4096        # the protocol's maximum payload per escape
MAX_PX = 400        # plenty for ~20 cells; keeps the escape small
TIMEOUT = 6


def supported(stream=None) -> bool:
    stream = stream or sys.stdout
    if not stream.isatty():
        return False
    term = os.environ.get("TERM", "").lower()
    prog = os.environ.get("TERM_PROGRAM", "").lower()
    return prog in TERMINALS or any(t in term for t in TERMINALS)


def to_png(data: bytes, max_px: int = MAX_PX) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im = im.convert("RGBA")
    im.thumbnail((max_px, max_px))
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def escape(png: bytes, cols: int) -> str:
    """The kitty-protocol escape sequence(s) that draw `png`, `cols` cells wide.

    a=T transmit-and-display, f=100 PNG, q=2 suppress the terminal's reply
    (which would otherwise land in the shell's input), C=0 move the cursor
    past the image so text continues below it.
    """
    b64 = base64.standard_b64encode(png).decode("ascii")
    chunks = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)] or [""]
    parts = []
    for i, chunk in enumerate(chunks):
        more = 1 if i < len(chunks) - 1 else 0
        ctrl = f"a=T,f=100,q=2,c={cols},m={more}" if i == 0 else f"m={more}"
        parts.append(f"\x1b_G{ctrl};{chunk}\x1b\\")
    return "".join(parts)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 twiddle"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def show(url: str | None, cols: int = 20, stream=None) -> bool:
    """Draw the image at `url`; True if something was drawn."""
    stream = stream or sys.stdout
    if not url or not supported(stream):
        return False
    try:
        seq = escape(to_png(fetch(url)), cols)
    except Exception:
        return False
    stream.write(seq + "\n")
    stream.flush()
    return True
