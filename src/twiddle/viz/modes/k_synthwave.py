"""An outrun horizon: a striped sun setting behind mountains made of the
spectrum, and a neon grid floor rushing towards you.

- the grid's speed is the energy; each kick jolts it forward and brightens it
- the mountains are the live spectrum, mirrored so bass is in the middle
- the sun pulses with the kick and its stripes drift down with the music
- stars twinkle with the treble

In silence the grid stops, the mountains lie flat and the stars hold still.
"""
from __future__ import annotations

import math

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import SPACE, blocks_up, cell_max, codes_of, dots_shape, empty, pack_braille, points, resample

STARS = codes_of("·+*")


@visualizer("synthwave", blurb="outrun horizon: spectrum mountains, a striped sun, a grid rushing on the kick",
            palette="synth")
class Synthwave(Visualizer):
    def resize(self, w: int, h: int) -> None:
        rng = np.random.default_rng(3)
        self.horizon = max(1, int(h * 0.55))
        n = max(1, w * self.horizon // 40)
        self.stars = (rng.integers(0, self.horizon, n), rng.integers(0, max(w, 1), n),
                      STARS[rng.integers(0, len(STARS), n)], rng.random(n).astype(np.float32))
        self.scroll = 0.0
        self.stripes = 0.0
        self.kick = 0.0

    def render(self, f: Frame, w: int, h: int):
        codes, heat = empty(h, w)
        hz = self.horizon
        if not f.silent:
            if f.beat:
                self.kick = 1.0
                self.scroll += 0.3
            self.kick *= math.exp(-f.dt * 5)
            self.scroll += f.dt * (0.4 + 2.5 * f.energy)
            self.stripes += f.dt * (0.5 + 2.0 * f.energy)
        else:
            self.kick = 0.0

        # Stars: fixed places, brightness from the treble.
        ys, xs, cs, tw = self.stars
        codes[ys, xs] = cs
        heat[ys, xs] = 0.2 + 0.6 * tw * (f.treble if not f.silent else 0.0)

        # The sun: a circle resting on the horizon, stripes in its lower half.
        r = max(1.0, hz * 0.75 * (1 + 0.06 * self.kick))
        yy, xx = np.mgrid[0:hz, 0:w]
        dy = (yy + 0.5 - hz) / r
        dx = (xx + 0.5 - w / 2) / (2 * r)                    # a cell is twice as tall as wide
        sun = dx * dx + dy * dy <= 1
        k = hz - yy                                          # rows above the horizon
        stripe = (k < r * 0.55) & (((k + self.stripes) % 4) < 1 + 1.5 * (1 - k / (r * 0.55)))
        sun &= ~stripe
        codes[:hz][sun] = ord("█")
        heat[:hz][sun] = np.minimum(1.0, 0.6 + 0.4 * k / r)[sun]

        # Mountains: the spectrum, mirrored, in front of the sun.
        half = resample(f.bands, (w + 1) // 2) * 0.55
        levels = np.concatenate([half[::-1], half])[:w]
        mcodes, mfill = blocks_up(levels, hz)
        m = mcodes != SPACE
        codes[:hz][m] = mcodes[m]
        heat[:hz][m] = 0.18 + 0.2 * mfill[m]

        # The floor: perspective grid in braille.
        fh = h - hz
        if fh > 0:
            dots = np.zeros(dots_shape(fh, w), dtype=bool)
            R, C = dots.shape
            glow = np.zeros(dots.shape, dtype=np.float32)
            frac = self.scroll % 1.0
            for n in range(24):                              # horizontal lines, nearer = further apart
                z = 1 + n - frac
                y = R / z
                if y < R:
                    yi = int(y)
                    dots[yi, :] = True
            sp = C / 7
            t = np.linspace(0, 1, max(2, R), dtype=np.float32)
            for i in range(-12, 13):                        # lines to the vanishing point
                points(dots, C / 2 + i * sp * t, t * (R - 1))
            glow[:] = (0.35 + 0.65 * np.arange(R, dtype=np.float32) / max(R - 1, 1))[:, None]
            glow = np.where(dots, np.minimum(1.0, glow * (0.8 + 0.4 * self.kick)), 0.0)
            codes[hz:] = pack_braille(dots)
            heat[hz:] = cell_max(glow)
        return codes, heat
