"""A goniometer: left against right, rotated 45°, as a studio's phase
scope draws it. Mono is a vertical line; wide stereo is a cloud; out of
phase lies down flat. Braille, with trails."""
from __future__ import annotations

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import cell_max, dots_shape, pack_braille, points

DECAY = 0.72


@visualizer("lissajous", blurb="stereo goniometer: mid up, side across", palette="neon")
class Lissajous(Visualizer):
    def resize(self, w: int, h: int) -> None:
        self.glow = np.zeros(dots_shape(h, w), dtype=np.float32)
        self.gain = 1.0

    def render(self, f: Frame, w: int, h: int):
        rows, cols = self.glow.shape
        side = (f.left - f.right) * np.float32(0.7071)
        mid = (f.left + f.right) * np.float32(0.7071)
        peak = float(max(np.abs(side).max(initial=0), np.abs(mid).max(initial=0)))
        target = 0.95 / peak if peak > 1e-3 else self.gain
        self.gain += (min(target, 40.0) - self.gain) * 0.15
        # Braille dots are about square, so one radius serves both axes.
        r = min(rows, cols) / 2 - 0.5
        xs = (cols - 1) / 2 + side * self.gain * r
        ys = (rows - 1) / 2 - mid * self.gain * r
        self.glow *= DECAY
        hit = np.zeros(self.glow.shape, dtype=bool)
        points(hit, xs, ys)
        self.glow[hit] = 1.0
        return pack_braille(self.glow > 0.1), cell_max(self.glow) * (0.4 + 0.6 * f.energy)
