"""Julia-set fractals, morphing and colour-cycling with the music.

A Julia set is z -> z^p + c iterated from every point on screen; how fast
a point escapes is its colour, and where a point that stays settles is
the colour inside. Moving c moves the whole shape. c travels just inside
the edge of the main cardioid, where the sets are connected and full of
spirals (further out they turn to dust, which at one sample a cell is
noise), so the picture never stops unfolding while there's music:

- the energy sets how fast c travels, how fast it turns, and how fast
  the colour bands flow outward (palette cycling, done in heat)
- each beat punches the zoom in and kicks the colours on
- the treble tightens the bands
- the bass pushes c towards the edge: more spirals
- every 16 beats the geometry changes: a plain Julia set, a six-fold
  kaleidoscope mandala of it, a cubic (three-lobed) Julia set, an
  eight-fold mandala of that

In silence everything stops where it is and dims: a still picture.
"""
from __future__ import annotations

import math

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer

ITER = 40
ESCAPE = 16.0
BEATS_PER_MODE = 16
# (power, kaleidoscope folds (0 = none))
MODES = ((2, 0), (2, 6), (3, 0), (3, 8))
FULL, HALF = ord("█"), ord("▓")


def _tri(x: np.ndarray) -> np.ndarray:
    """0..1..0 every unit: colours cycle smoothly instead of snapping back."""
    return 1 - np.abs(2 * (x % 1.0) - 1)


@visualizer("fractal", blurb="Julia sets and mandalas: morph with the energy, zoom on the beat, colours cycle",
            palette="psyche")
class Fractal(Visualizer):
    def resize(self, w: int, h: int) -> None:
        # y spans -1..1 over the height; a cell is twice as tall as wide.
        hh = max(h, 1)
        xs = (np.arange(w) + 0.5 - w / 2) / hh
        ys = (np.arange(h) + 0.5 - h / 2) * 2 / hh
        self.grid = (xs[None, :] + 1j * ys[:, None]).astype(np.complex64)
        self.theta = 1.2          # where c is along the cardioid's edge
        self.rot = 0.0
        self.phase = 0.0          # colour cycle
        self.breathe = 0.0
        self.freq = 1.0           # colour bands per doubling of the escape count
        self.kick = 0.0
        self.beats = 0

    def render(self, f: Frame, w: int, h: int):
        if f.silent:
            self.kick = 0.0       # hold everything where it is
        else:
            if f.beat:
                self.kick = 1.0
                self.beats += 1
                self.phase += 0.12
            self.kick *= math.exp(-f.dt * 5)
            e = f.energy
            self.theta += f.dt * (0.03 + 0.3 * e)
            spin = 1 if (self.beats // (2 * BEATS_PER_MODE)) % 2 == 0 else -1
            self.rot += f.dt * (0.06 + 0.35 * e) * spin
            self.phase += f.dt * (0.15 + 1.2 * e)
            self.breathe += f.dt * (0.1 + 0.3 * e)
            self.freq += (0.7 + 0.8 * f.treble - self.freq) * min(1.0, f.dt * 3)

        power, folds = MODES[(self.beats // BEATS_PER_MODE) % len(MODES)]
        zoom = 1.3 * (1 + 0.2 * math.sin(self.breathe)) / (1 + 0.25 * self.kick)
        z = self.grid * np.complex64(zoom * complex(math.cos(self.rot), math.sin(self.rot)))
        if folds:
            # Kaleidoscope: every sector a mirror image of the first.
            sector = 2 * math.pi / folds
            a = np.abs(np.angle(z) % sector - sector / 2)
            z = (np.abs(z) * np.exp(1j * a)).astype(np.complex64)
        # c where z^p + c has a fixed point of multiplier k e^(i theta): k just
        # under 1 is the edge of the main cardioid (its cubic analogue for p=3).
        k = 0.955 + (0.04 * f.bass if not f.silent else 0.0)
        fixed = (k * complex(math.cos(self.theta), math.sin(self.theta)) / power) ** (1 / (power - 1))
        c = np.complex64(fixed - fixed ** power)

        nu, inside = self._iterate(z.ravel(), c, power, np.complex64(fixed))
        nu, inside = nu.reshape(h, w), inside.reshape(h, w)

        # Bands on a log scale: near the set the count climbs too fast
        # between neighbouring cells to show as anything but noise.
        heat = _tri(np.where(inside, nu, np.log2(1 + np.maximum(nu, 0)) * self.freq) + self.phase)
        # Solid colour, a shade softer where it's far from the set.
        codes = np.where(inside | (nu > 1.5), FULL, HALF).astype(np.int32)
        if f.silent:
            heat = heat * 0.45
        return codes, heat.astype(np.float32)

    @staticmethod
    def _iterate(z: np.ndarray, c: np.complex64, power: int, fixed: np.complex64):
        """Smooth escape counts, iterating only the points still inside.
        A point that never escapes is spiralling into the attracting fixed
        point: it gets the angle and distance it approaches from, which
        draws petals and spirals inside the set."""
        nu = np.zeros(z.shape, np.float32)
        inside = np.ones(z.shape, bool)
        idx = np.arange(z.size)
        zz = z.copy()
        log_p = math.log(power)
        for n in range(ITER):
            zz = zz * zz * zz + c if power == 3 else zz * zz + c
            mag = zz.real * zz.real + zz.imag * zz.imag
            out = mag > ESCAPE
            if out.any():
                i = idx[out]
                inside[i] = False
                lm = np.log(np.maximum(mag[out], ESCAPE)) / 2
                nu[i] = n + 1 - np.log(np.maximum(lm, 1e-6)) / log_p
                keep = ~out
                zz, idx = zz[keep], idx[keep]
                if not idx.size:
                    break
        if idx.size:
            d = zz - fixed
            nu[idx] = 2 * np.angle(d) / (2 * np.pi) + 0.6 * np.log(np.abs(d) + 1e-12)
        return nu, inside
