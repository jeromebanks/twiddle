"""A warp tunnel of rings rushing at you, in braille.

- the energy sets the speed; the tunnel curves and sways with the bass
- every kick launches a bright ring from the far end that races at you
- every 16 beats the rings change shape: square, hexagon, triangle, circle
- the rings twist as they come, faster when it's loud

In silence the tunnel stops dead: a still picture.
"""
from __future__ import annotations

import math

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import cell_max, dots_shape, pack_braille

N_RINGS = 18
FAR = 4.5
NEAR = 0.2
SPACING = (FAR - NEAR) / N_RINGS
BEATS_PER_SHAPE = 16
SHAPES = (4, 6, 3, 48)


@visualizer("tunnel", blurb="warp tunnel: rings rush at you, a bright one launched on every kick", palette="ice")
class Tunnel(Visualizer):
    def resize(self, w: int, h: int) -> None:
        self.field = np.zeros(dots_shape(h, w), dtype=np.float32)
        self.z = NEAR + SPACING * (np.arange(N_RINGS) + 0.5)
        self.lit = np.zeros(N_RINGS, dtype=np.float32)
        self.twist = 0.0
        self.sway = 0.0
        self.beats = 0

    def render(self, f: Frame, w: int, h: int):
        H, W = self.field.shape
        if not f.silent:
            speed = 0.4 + 3.0 * f.energy
            self.z -= speed * f.dt
            gone = self.z < NEAR
            self.z[gone] += FAR - NEAR
            self.lit[gone] = 0.0
            if f.beat:
                self.beats += 1
                self.lit[np.argmax(self.z)] = 1.0            # the far ring lights up
            self.twist += f.dt * (0.2 + 1.2 * f.energy)
            self.sway += f.dt * (0.3 + 1.5 * f.bass)
        sides = SHAPES[(self.beats // BEATS_PER_SHAPE) % len(SHAPES)]
        R0 = min(W, H) * 0.3
        all_x, all_y, all_l = [], [], []
        for z, lit in sorted(zip(self.z.tolist(), self.lit.tolist()), reverse=True):
            r = R0 / z
            if r > 2 * (W + H) or r < 4:
                continue                                    # past the screen, or too small to read
            # Far rings drift off-centre: the tunnel bends.
            cx = W / 2 + W * 0.12 * math.sin(self.sway + 0.9 * z) * (z / FAR)
            cy = H / 2 + H * 0.1 * math.cos(0.8 * self.sway + 0.7 * z) * (z / FAR)
            rot = self.twist + 0.35 * z
            ang = rot + np.linspace(0, 2 * math.pi, sides + 1)
            vx, vy = cx + r * np.cos(ang), cy + r * np.sin(ang)
            per = max(8, int(2 * math.pi * r))
            t = np.linspace(0, sides, per, endpoint=False)
            i = t.astype(int)
            u = t - i
            xs = vx[i] + (vx[i + 1] - vx[i]) * u
            ys = vy[i] + (vy[i + 1] - vy[i]) * u
            near = min(1.0, 0.9 / z)                         # far rings fade into the dark
            level = min(1.0, 0.1 + 0.55 * near + 0.7 * lit * near ** 0.5)
            all_x.append(xs), all_y.append(ys), all_l.append(np.full(per, level, np.float32))
        hit = np.zeros(self.field.shape, dtype=np.float32)
        if all_x:
            xi = np.round(np.concatenate(all_x)).astype(np.int64)
            yi = np.round(np.concatenate(all_y)).astype(np.int64)
            lv = np.concatenate(all_l)
            ok = (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)
            np.maximum.at(hit, (yi[ok], xi[ok]), lv[ok])
        self.field = np.maximum(self.field * 0.25, hit)       # a little motion blur
        self.field[self.field < 0.05] = 0.0
        return pack_braille(self.field > 0.08), cell_max(self.field)
