"""dial's opening card: "dial" in big letters with Twiddle, the mascot,
standing beside them and waving hello. The Textual side is
`dial/splash.py`; this is the picture, here because numpy stays in `viz/`.

Twiddle is the `buddy` visualizer's critter (`modes/h_buddy.py`), driven by
a hand-made `Frame` rather than audio: not silent, so he's awake, but too
quiet for a step, so he never hops or sings. Nothing plays on the splash,
and nothing on it may look like a reaction to music (the visualizer
screen's rule). He waves, blinks and his antenna bobble sways; that's all.

Everything -- letters, Twiddle, words -- goes into one codes/heat pair and
through one palette, so the whole screen is one background. Who Twiddle is:
`CHARACTERS.md`.
"""
from __future__ import annotations

import numpy as np
from textual.strip import Strip

from . import base
from .analysis import Frame
from .canvas import SPACE, Palette, empty, to_strips

SLOGAN = "all the radio. none of the static."
PALETTE = "clay"
WAVE = 4                 # MOVES index: waving with the left hand

# Lowercase, six rows, half blocks. The i's dot is a knob, in the bobble's pink.
GLYPHS = (
    ("        ███",
     "        ███",
     " ▄██▀▀▀▀███",
     "███     ███",
     "███     ███",
     " ▀██▄▄▄▄███"),
    ("▄▄▄",
     "▀▀▀",
     "███",
     "███",
     "███",
     "███"),
    ("           ",
     "           ",
     " ▄██▀▀▀▀██▄",
     "███     ███",
     "███     ███",
     " ▀██▄▄▄▄███▄"),
    ("███",
     "███",
     "███",
     "███",
     "███",
     "▀███▄"),
)
KNOB = (1, slice(0, 2))              # glyph, rows: the i's dot
LETTER_GAP = 2
MASCOT_GAP = 3
DOT, TAGLINE, STATUS, HINT = 0.83, 0.74, 0.34, 0.24


def _word() -> tuple[np.ndarray, np.ndarray]:
    """The letters as (codes, heat); heat is 1 where the knob is, else 0."""
    widths = [max(len(r) for r in g) for g in GLYPHS]
    w = sum(widths) + LETTER_GAP * (len(GLYPHS) - 1)
    h = len(GLYPHS[0])
    codes = np.full((h, w), SPACE, np.int32)
    knob = np.zeros((h, w), bool)
    x = 0
    for i, (g, gw) in enumerate(zip(GLYPHS, widths)):
        for y, row in enumerate(g):
            codes[y, x:x + len(row)] = [ord(c) for c in row]
        if i == KNOB[0]:
            knob[KNOB[1], x:x + gw] = True
        x += gw + LETTER_GAP
    return codes, knob


class SplashArt:
    """Stateful only for Twiddle (his blink, his bobble's spring)."""

    def __init__(self, palette=PALETTE):
        base.load_builtins()        # registry order stays the built-ins' own
        from .modes.h_buddy import Buddy
        self.twiddle = Buddy()
        self.size: tuple[int, int] | None = None
        self.palette = Palette.named(palette)
        self.word, self.knob = _word()
        self.frame = 0

    def _mascot(self, t: float, dt: float, w: int, h: int):
        if self.size != (w, h):
            self.twiddle.resize(w, h)
            self.size = (w, h)
        self.twiddle.step = WAVE        # resize resets it
        f = Frame(silent=False, energy=0.1, t=t, dt=dt, frame=self.frame)
        return self.twiddle.render(f, w, h)

    def draw(self, w: int, h: int, t: float, dt: float, status: str = "",
             hint: str = "press any key") -> list[Strip]:
        self.frame += 1
        codes, heat = empty(h, w)
        ww, wh = self.word.shape[1], self.word.shape[0]
        lines = [(SLOGAN, TAGLINE), ("", 0), (status, STATUS), (hint, HINT)]

        # Twiddle's box: square pixels need twice as many columns as rows.
        mh = min(17, h - len(lines) - 3, (w - ww - MASCOT_GAP) // 2 - 1)
        mh = mh if mh >= 8 else 0           # too small to read as a face: words only
        mw = 2 * mh + 2 if mh else 0
        block_w = ww + (MASCOT_GAP + mw if mh else 0)
        block_h = max(wh, mh)
        top = max(0, (h - block_h - 1 - len(lines)) // 2)
        left = max(0, (w - block_w) // 2)

        # The word stands on the same floor as Twiddle's feet.
        if ww <= w and wh <= h:
            wy = top + block_h - wh - (1 if mh else 0)
            wx = left
            shimmer = 0.62 + 0.18 * np.sin(np.arange(ww) * 0.22 - t * 2.2)
            lit = self.word != SPACE
            region = (slice(wy, wy + wh), slice(wx, wx + ww))
            codes[region] = np.where(lit, self.word, SPACE)
            heat[region] = np.where(self.knob, DOT, np.broadcast_to(shimmer, (wh, ww)))
        else:
            lines.insert(0, ("dial", 1.0))
        if mh:
            mc, mheat = self._mascot(t, dt, mw, mh)
            region = (slice(top, top + mh), slice(left + ww + MASCOT_GAP, left + ww + MASCOT_GAP + mw))
            codes[region], heat[region] = mc, mheat

        y = top + block_h + 1
        for text, level in lines:
            if 0 <= y < h and text:
                text = text[:w]
                x = (w - len(text)) // 2
                codes[y, x:x + len(text)] = [ord(c) for c in text]
                heat[y, x:x + len(text)] = level
            y += 1
        return to_strips(codes, heat, self.palette)
