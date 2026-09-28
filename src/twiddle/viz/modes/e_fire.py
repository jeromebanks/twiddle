"""The Doom fire: heat rises from a floor fed by the spectrum, cools as it
climbs, and flares on each beat."""
from __future__ import annotations

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import resample, shade

RAMP = " .:-=+*#%@"


@visualizer("fire", blurb="Doom fire fed by the bass and every beat", palette="fire")
class Fire(Visualizer):
    def resize(self, w: int, h: int) -> None:
        self.heat = np.zeros((h + 1, w), dtype=np.float32)   # one hidden floor row
        self.rng = np.random.default_rng(7)
        self.flare = 0.0

    def render(self, f: Frame, w: int, h: int):
        self.flare = 1.0 if f.beat else self.flare * 0.85
        floor = resample(f.bands, w) ** 0.6 * (0.9 + 0.6 * self.flare) + 0.25 * f.energy
        floor *= 0.8 + 0.4 * self.rng.random(w, dtype=np.float32)
        self.heat[-1] = np.clip(floor, 0, 1)
        # Each cell takes from below, drifting sideways, cooling a little.
        below = self.heat[1:]
        drift = self.rng.integers(-1, 2, size=w)
        cols = np.clip(np.arange(w) + drift, 0, w - 1)
        spread = (below[:, cols] * 0.6 + below * 0.25 + np.roll(below, 1, axis=1) * 0.15)
        cool = self.rng.random(below.shape, dtype=np.float32) * (1.3 / max(h, 1)) + 0.004
        self.heat[:-1] = np.clip(spread - cool, 0, 1)
        field = self.heat[:-1]
        return shade(field, RAMP), field.copy()
