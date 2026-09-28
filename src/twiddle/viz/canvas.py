"""Drawing helpers for visualizers, and the one place cells become colour.

A visualizer returns two (h, w) arrays: `codes` (a Unicode codepoint per
cell) and `heat` (0 cool .. 1 hot). It never picks a colour. `Palette` turns
heat into colour, so every visualizer works under every palette and theme
(spektr's rule, borrowed).

Sub-cell resolution, the usual terminal tricks:

- braille: each cell is 2 wide x 4 tall dots -> `dots_shape`, `pack_braille`
- eighth blocks: bars at 8 steps per cell -> `blocks_up`
- shades: density ramps for fields -> `shade`
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from rich.segment import Segment
from rich.style import Style
from textual.color import Color
from textual.strip import Strip

SPACE = 32
BLOCKS_UP = " ▁▂▃▄▅▆▇█"
SHADES = " ·:-=+*#%@"
SOLID = " ░▒▓█"
RAMP_STEPS = 24

# Braille dot -> bit, by (row, col) inside the 4x2 cell (Unicode's own order).
_BRAILLE_BITS = np.array([[0x01, 0x08],
                          [0x02, 0x10],
                          [0x04, 0x20],
                          [0x40, 0x80]], dtype=np.int32)


# ---- arrays ----------------------------------------------------------------


def empty(h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
    """Blank `codes` (spaces) and `heat` (zeros) of shape (h, w)."""
    return np.full((h, w), SPACE, dtype=np.int32), np.zeros((h, w), dtype=np.float32)


def codes_of(chars: str) -> np.ndarray:
    """A string's characters as a codepoint lookup table."""
    return np.array([ord(c) for c in chars], dtype=np.int32)


def dots_shape(h: int, w: int) -> tuple[int, int]:
    """(rows, cols) of the braille dot grid covering h x w cells."""
    return h * 4, w * 2


def pack_braille(dots: np.ndarray) -> np.ndarray:
    """A (h*4, w*2) boolean dot grid -> (h, w) codepoints. Empty cells are
    spaces, not U+2800, so the background shows through cleanly."""
    rows, cols = dots.shape
    h, w = rows // 4, cols // 2
    d = dots[:h * 4, :w * 2].reshape(h, 4, w, 2).transpose(0, 2, 1, 3)
    bits = (d * _BRAILLE_BITS).sum(axis=(2, 3))
    return np.where(bits == 0, SPACE, 0x2800 + bits).astype(np.int32)


def cell_max(field: np.ndarray, sub: tuple[int, int] = (4, 2)) -> np.ndarray:
    """A sub-cell field (by default the braille grid) -> its per-cell max."""
    r, c = sub
    h, w = field.shape[0] // r, field.shape[1] // c
    return field[:h * r, :w * c].reshape(h, r, w, c).max(axis=(1, 3))


def resample(values: np.ndarray, n: int) -> np.ndarray:
    """`values` stretched or squeezed to n points (linear)."""
    values = np.asarray(values, dtype=np.float32)
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    if len(values) == 0:
        return np.zeros(n, dtype=np.float32)
    if len(values) == 1:
        return np.full(n, values[0], dtype=np.float32)
    x = np.linspace(0, len(values) - 1, n)
    return np.interp(x, np.arange(len(values)), values).astype(np.float32)


def blocks_up(levels: np.ndarray, h: int) -> tuple[np.ndarray, np.ndarray]:
    """Bars growing up from the bottom, one per column, 8 steps per cell.

    Returns (codes, fill): fill is how far up the bar each cell is (0 at the
    bottom row, 1 at the top), zero where the bar doesn't reach -- a
    ready-made heat for a gradient."""
    levels = np.clip(np.asarray(levels, dtype=np.float32), 0, 1)
    w = len(levels)
    from_bottom = np.arange(h - 1, -1, -1, dtype=np.float32)[:, None]    # (h, 1)
    amount = np.clip(levels[None, :] * h - from_bottom, 0, 1)           # (h, w)
    idx = np.round(amount * 8).astype(np.int32)
    codes = codes_of(BLOCKS_UP)[idx]
    fill = np.where(idx > 0, (from_bottom + 1) / max(h, 1), 0.0).astype(np.float32)
    return codes, np.broadcast_to(fill, (h, w)).copy()


def shade(field: np.ndarray, ramp: str = SHADES) -> np.ndarray:
    """A 0..1 field -> codepoints from a density ramp (space at 0)."""
    table = codes_of(ramp)
    idx = np.clip((np.asarray(field) * (len(ramp) - 1) + 0.5).astype(np.int32), 0, len(ramp) - 1)
    return table[idx]


def polyline(dots: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> None:
    """Draw connected points into a boolean dot grid, in place. Coordinates
    are in dots (floats fine); anything off the grid is clipped."""
    rows, cols = dots.shape
    if len(xs) == 0 or rows == 0 or cols == 0:
        return
    xs, ys = np.asarray(xs, np.float32), np.asarray(ys, np.float32)
    if len(xs) > 1:
        # Enough steps between neighbours that no gap is left.
        seg = np.maximum(np.abs(np.diff(xs)), np.abs(np.diff(ys)))
        steps = int(min(max(float(seg.max()) if len(seg) else 1.0, 1.0), 64.0)) + 1
        t = np.linspace(0, 1, steps, endpoint=False, dtype=np.float32)[None, :]
        xs = (xs[:-1, None] + np.diff(xs)[:, None] * t).ravel()
        ys = (ys[:-1, None] + np.diff(ys)[:, None] * t).ravel()
    points(dots, xs, ys)


def points(dots: np.ndarray, xs: np.ndarray, ys: np.ndarray, value=True) -> None:
    """Set single dots, in place; off-grid ones are dropped."""
    rows, cols = dots.shape
    xi = np.round(np.asarray(xs)).astype(np.int64)
    yi = np.round(np.asarray(ys)).astype(np.int64)
    ok = (xi >= 0) & (xi < cols) & (yi >= 0) & (yi < rows)
    dots[yi[ok], xi[ok]] = value


# ---- colour ----------------------------------------------------------------


def _ramp(stops: Sequence[Color], n: int = RAMP_STEPS) -> list[Color]:
    out = []
    for i in range(n):
        x = i / (n - 1) * (len(stops) - 1)
        k = min(int(x), len(stops) - 2)
        out.append(stops[k].blend(stops[k + 1], x - k))
    return out


PALETTES: dict[str, Sequence[str] | None] = {
    "theme": None,      # from the app's current Textual theme
    "fire": ("#1a0500", "#7a1000", "#d83a00", "#ff8c00", "#ffd000", "#fff6c0"),
    "ice": ("#020818", "#0b2d5c", "#1f6fb2", "#4fc3f7", "#b3ecff", "#ffffff"),
    "neon": ("#12002b", "#6a00f4", "#db00b6", "#ff4d6d", "#00f5d4", "#f0fff0"),
    "aurora": ("#001a14", "#00563f", "#00b894", "#55efc4", "#a29bfe", "#fd79a8"),
    "mono": ("#202020", "#5a5a5a", "#9a9a9a", "#d0d0d0", "#ffffff"),
    "clay": ("#1b120e", "#4a2419", "#9c4a2e", "#d9774f", "#f2a57e", "#ff8fa8", "#fff5e8"),
    "psyche": ("#10002b", "#5a00c8", "#ff00b4", "#ff5a00", "#ffe600", "#3dff7a", "#00e5ff", "#ffffff"),
    "synth": ("#0d0221", "#241056", "#6a0dad", "#d4008f", "#ff2a6d", "#ff8b3d", "#ffd319"),
    "candy": ("#1c1228", "#7c6bb0", "#8fd3ec", "#a8f0c6", "#ffe38a", "#ffb0d0", "#ff5fa2"),
}


class Palette:
    """Heat -> Rich style, precomputed. `background` is drawn behind every
    cell, so a visualizer's spaces are the screen's colour."""

    def __init__(self, name: str, stops: Sequence[Color], background: Color):
        self.name = name
        self.background = background
        bg = background.rich_color
        self.styles = [Style(color=c.rich_color, bgcolor=bg) for c in _ramp(stops)]
        self.blank = Style(bgcolor=bg)

    @classmethod
    def named(cls, name: str, theme=None) -> Palette:
        """A palette by name; "theme" (or an unknown name) follows the Textual
        `theme` given (anything with primary/secondary/accent/background)."""
        stops = PALETTES.get(name)
        if stops is not None:
            colours = [Color.parse(s) for s in stops]
            return cls(name, colours, colours[0].darken(0.5))
        get = (lambda k, d: Color.parse(getattr(theme, k, None) or d))
        bg = get("background", "#101010")
        primary, secondary = get("primary", "#4fc3f7"), get("secondary", "#81a1c1")
        accent, warning = get("accent", "#ff79c6"), get("warning", "#ffd000")
        return cls("theme", [bg.blend(primary, 0.35), primary, secondary, accent, warning,
                             warning.lighten(0.25)], bg)


def to_strips(codes: np.ndarray, heat: np.ndarray, palette: Palette) -> list[Strip]:
    """(h, w) codes + heat -> one Strip per row. Cells are grouped into runs
    of the same quantised heat, so a row costs a handful of Segments rather
    than one per cell; spaces take the background style."""
    h, w = codes.shape
    idx = np.clip((heat * (RAMP_STEPS - 1) + 0.5).astype(np.int32), 0, RAMP_STEPS - 1)
    idx = np.where(codes == SPACE, -1, idx)
    text = codes.astype("<u4").tobytes().decode("utf-32-le")
    strips = []
    for y in range(h):
        row = idx[y]
        line = text[y * w:(y + 1) * w]
        cuts = np.flatnonzero(np.diff(row)) + 1
        starts = np.concatenate([[0], cuts])
        ends = np.concatenate([cuts, [w]])
        segs = [Segment(line[a:b], palette.blank if row[a] < 0 else palette.styles[row[a]])
                for a, b in zip(starts.tolist(), ends.tolist())]
        strips.append(Strip(segs, w))
    return strips
