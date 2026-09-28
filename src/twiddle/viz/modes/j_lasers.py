"""A club laser show in braille: beams from one or more emitters, sweeping
with the groove, hanging in the haze for a moment after they move.

- the energy sets the sweep speed; the bass sets how many beams
- every kick flashes the beams bright
- every 8 beats the rig changes: a fan from the floor, two fans crossing
  from the corners, beams raining down from the ceiling, a starburst

In silence the beams go out and the haze clears to black.
"""
from __future__ import annotations

import math

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import cell_max, dots_shape, pack_braille, points

BEATS_PER_PATTERN = 8
PATTERNS = ("fan", "cross", "rain", "burst")


@visualizer("lasers", blurb="a club laser show: beams sweep with the groove, flash and change rig on the kick",
            palette="neon")
class Lasers(Visualizer):
    def resize(self, w: int, h: int) -> None:
        self.field = np.zeros(dots_shape(h, w), dtype=np.float32)
        self.phase = 0.0
        self.kick = 0.0
        self.beats = 0

    def _beams(self, pattern: str, n: int, W: int, H: int):
        """(x, y, angle) per beam, in dots; angles with y pointing down."""
        p = self.phase
        if pattern == "fan":
            spread = 0.9 + 0.5 * math.sin(p * 0.7)
            return [(W / 2, H - 1, -math.pi / 2 + spread * (k / max(n - 1, 1) - 0.5) + 0.5 * math.sin(p))
                    for k in range(n)]
        if pattern == "cross":
            m = max(1, n // 2)
            left = [(0, H - 1, -math.pi / 4 + 0.45 * math.sin(p) + 0.5 * (k / max(m - 1, 1) - 0.5))
                    for k in range(m)]
            right = [(W - 1, H - 1, -3 * math.pi / 4 - 0.45 * math.sin(p) + 0.5 * (k / max(m - 1, 1) - 0.5))
                     for k in range(m)]
            return left + right
        if pattern == "rain":
            return [((k + 0.5) * W / n, 0, math.pi / 2 + 0.4 * math.sin(p + 0.8 * k)) for k in range(n)]
        return [(W / 2, H / 2, 2 * math.pi * k / n + 0.5 * p) for k in range(n)]      # burst

    def render(self, f: Frame, w: int, h: int):
        H, W = self.field.shape
        if f.silent:
            self.field *= 0.6
            self.field[self.field < 0.05] = 0.0
            return pack_braille(self.field > 0.08), cell_max(self.field)
        if f.beat:
            self.kick = 1.0
            self.beats += 1
        self.kick *= math.exp(-f.dt * 6)
        self.phase += f.dt * (0.4 + 2.0 * f.energy)

        pattern = PATTERNS[(self.beats // BEATS_PER_PATTERN) % len(PATTERNS)]
        n = 3 + int(round(7 * f.bass))
        length = math.hypot(W, H)
        steps = max(2, int(length))
        t = np.linspace(0, length, steps, dtype=np.float32)
        hit = np.zeros(self.field.shape, dtype=bool)
        for x, y, a in self._beams(pattern, n, W, H):
            points(hit, x + t * math.cos(a), y + t * math.sin(a))
        self.field *= 0.55                                   # haze: where the beams just were
        self.field = np.maximum(self.field, hit * (0.45 + 0.55 * self.kick))
        return pack_braille(self.field > 0.08), cell_max(self.field)
