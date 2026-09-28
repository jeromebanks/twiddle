"""A spectrogram falling down the screen: the newest spectrum on top,
low frequencies left, loudness as density and heat."""
from __future__ import annotations

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import SOLID, resample, shade

ROWS_PER_S = 12


@visualizer("waterfall", blurb="scrolling spectrogram, newest on top", palette="aurora")
class Waterfall(Visualizer):
    def resize(self, w: int, h: int) -> None:
        self.rows = np.zeros((h, w), dtype=np.float32)
        self.pending = np.zeros(w, dtype=np.float32)
        self.clock = 0.0

    def render(self, f: Frame, w: int, h: int):
        # Rows fall at a steady ROWS_PER_S whatever the frame rate; what
        # happened between two rows is kept as its loudest.
        self.pending = np.maximum(self.pending, np.sqrt(resample(f.raw_bands, w)))
        self.clock += f.dt
        if self.clock >= 1 / ROWS_PER_S:
            self.clock %= 1 / ROWS_PER_S
            self.rows = np.roll(self.rows, 1, axis=0)
            self.rows[0] = self.pending
            self.pending = np.zeros(w, dtype=np.float32)
        return shade(self.rows, SOLID), self.rows.copy()
