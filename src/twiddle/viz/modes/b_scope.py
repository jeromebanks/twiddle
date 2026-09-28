"""An oscilloscope in braille, triggered on a rising zero crossing so a
steady tone stands still, with phosphor that fades."""
from __future__ import annotations

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import cell_max, dots_shape, pack_braille, polyline

DECAY = 0.55


def trigger(wave: np.ndarray, span: int) -> int:
    """Where to start drawing: the first rising zero crossing that leaves
    `span` samples after it, or wherever does."""
    head = wave[: max(1, len(wave) - span)]
    rise = np.flatnonzero((head[:-1] < 0) & (head[1:] >= 0))
    return int(rise[0]) + 1 if len(rise) else 0


@visualizer("scope", blurb="braille oscilloscope with phosphor", palette="ice")
class Scope(Visualizer):
    def resize(self, w: int, h: int) -> None:
        self.glow = np.zeros(dots_shape(h, w), dtype=np.float32)
        self.gain = 1.0

    def render(self, f: Frame, w: int, h: int):
        rows, cols = self.glow.shape
        span = min(len(f.wave), max(cols * 4, 256))
        start = trigger(f.wave, span)
        seg = f.wave[start:start + span]
        # Auto-gain, smoothed so the trace breathes rather than jumps.
        peak = float(np.max(np.abs(seg))) if len(seg) else 0.0
        target = 0.9 / peak if peak > 1e-3 else self.gain
        self.gain += (min(target, 40.0) - self.gain) * 0.2
        xs = np.linspace(0, cols - 1, len(seg))
        mid = (rows - 1) / 2
        ys = mid - seg * self.gain * mid
        dots = np.zeros_like(self.glow, dtype=bool)
        if not f.silent:
            polyline(dots, xs, ys)
        else:
            dots[int(round(mid)), :] = True      # a flat line: silence, drawn as silence
        self.glow *= DECAY
        self.glow[dots] = 1.0
        lit = self.glow > 0.12
        return pack_braille(lit), cell_max(self.glow) * (0.5 + 0.5 * f.energy)
