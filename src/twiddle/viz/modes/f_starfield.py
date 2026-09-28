"""Flying through stars in braille: the music's energy sets the warp speed
and each beat kicks it."""
from __future__ import annotations

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import cell_max, dots_shape, pack_braille


@visualizer("starfield", blurb="warp speed set by the energy, kicked by beats")
class Starfield(Visualizer):
    def resize(self, w: int, h: int) -> None:
        self.rng = np.random.default_rng(11)
        n = max(12, w * h // 14)
        self.xyz = self._spawn(n, far=False)
        self.glow = np.zeros(dots_shape(h, w), dtype=np.float32)
        self.kick = 0.0

    def _spawn(self, n: int, far: bool) -> np.ndarray:
        xy = self.rng.uniform(-1, 1, size=(n, 2)).astype(np.float32)
        z = (np.ones(n) if far else self.rng.uniform(0.05, 1, n)).astype(np.float32)
        return np.column_stack([xy, z])

    def render(self, f: Frame, w: int, h: int):
        rows, cols = self.glow.shape
        self.kick = 1.0 if f.beat else self.kick * 0.9
        # Silence stops the ship: nothing moves that the music didn't move.
        speed = 0.0 if f.silent else 0.05 + 0.9 * f.energy + 0.8 * self.kick
        self.xyz[:, 2] -= speed * f.dt
        z = self.xyz[:, 2]
        sx = (cols - 1) / 2 + self.xyz[:, 0] / z * cols / 2
        sy = (rows - 1) / 2 + self.xyz[:, 1] / z * rows / 2
        gone = (z <= 0.02) | (sx < 0) | (sx >= cols) | (sy < 0) | (sy >= rows)
        if gone.any():
            self.xyz[gone] = self._spawn(int(gone.sum()), far=True)
        self.glow *= 0.3 + 0.45 * min(speed, 1.0)      # longer streaks when fast
        near = np.clip(1.2 - self.xyz[:, 2], 0, 1)
        ok = ~gone
        hit = np.zeros(self.glow.shape, dtype=np.float32)
        xi = np.clip(np.round(sx[ok]).astype(int), 0, cols - 1)
        yi = np.clip(np.round(sy[ok]).astype(int), 0, rows - 1)
        hit[yi, xi] = near[ok]
        self.glow = np.maximum(self.glow, hit)
        return pack_braille(self.glow > 0.08), cell_max(self.glow)
