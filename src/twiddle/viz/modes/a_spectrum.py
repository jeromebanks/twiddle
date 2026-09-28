"""Bars, cava-style: log-spaced bands, gravity, held peak caps, and a dim
reflection underneath."""
from __future__ import annotations

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import SPACE, blocks_up, codes_of, empty, resample

REFLECT = codes_of(" ░▒")
CAP = ord("▔")


@visualizer("spectrum", blurb="bars with gravity and peak caps, reflected")
class Spectrum(Visualizer):
    def render(self, f: Frame, w: int, h: int):
        codes, heat = empty(h, w)
        # Bars two cells wide with a one-cell gap, as many as fit.
        n = max(1, (w + 1) // 3)
        col_bar = np.minimum(np.arange(w) // 3, n - 1)
        gap = (np.arange(w) % 3) == 2
        levels = resample(f.bands, n)[col_bar]
        peaks = resample(f.peaks, n)[col_bar]
        levels[gap], peaks[gap] = 0, 0

        top = max(1, h * 3 // 4) if h >= 4 else h
        bars, fill = blocks_up(levels, top)
        codes[:top], heat[:top] = bars, 0.15 + 0.85 * fill
        # A cap where a held peak sits above the bar.
        prow = np.clip(np.round((1 - peaks) * top).astype(int), 0, top - 1)
        above = (peaks * top - levels * top >= 1) & (peaks > 0.02)
        cols = np.flatnonzero(above)
        codes[prow[cols], cols] = CAP
        heat[prow[cols], cols] = 1.0

        rest = h - top
        if rest > 0:
            depth = np.arange(1, rest + 1, dtype=np.float32)[:, None] / rest      # 0..1 down
            reach = levels[None, :] * 0.8 - depth + 1 / rest
            idx = np.clip((reach * rest).astype(int), 0, 2)
            idx = np.where((reach > 0) & (levels[None, :] > 0.03), np.maximum(idx, 1), 0)
            codes[top:] = np.where(idx > 0, REFLECT[idx], SPACE)
            heat[top:] = np.where(idx > 0, 0.25 * levels[None, :] * (1 - depth), 0)
        return codes, heat
