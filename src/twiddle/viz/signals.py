"""Made-up audio, for tests, for `twiddle viz demo --synthetic` and for
writing a visualizer without anything playing. Each generator hands out
consecutive windows of stereo float32, advancing a frame's worth (1/30 s)
per call, like a live tap read at 30 fps."""
from __future__ import annotations

import numpy as np

from .analysis import RATE

STEP = RATE // 30


class Signal:
    def __init__(self, seed: int = 3):
        self.rng = np.random.default_rng(seed)
        self.pos = 0

    def samples(self, t: np.ndarray) -> np.ndarray:        # (n,) seconds -> (n, 2)
        raise NotImplementedError

    def window(self, n: int) -> np.ndarray:
        t = (np.arange(self.pos - n, self.pos) / RATE).astype(np.float64)
        self.pos += STEP
        return np.clip(self.samples(t), -1, 1).astype(np.float32)


class Silence(Signal):
    def samples(self, t):
        return np.zeros((len(t), 2))


class Sine(Signal):
    def samples(self, t):
        s = 0.5 * np.sin(2 * np.pi * 440 * t)
        return np.column_stack([s, s])


class Noise(Signal):
    def samples(self, t):
        return self.rng.uniform(-0.5, 0.5, size=(len(t), 2))


class Inverted(Signal):
    """Right is left upside down: the stereo case a goniometer lays flat."""
    def samples(self, t):
        s = 0.4 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * 1330 * t)
        return np.column_stack([s, -s])


class Music(Signal):
    """Something with a pulse: a kick on every beat at 120 bpm, a chord that
    changes every two bars, hats on the off-beats, a little stereo width."""
    CHORDS = ((110.0, 138.6, 164.8), (98.0, 123.5, 146.8), (87.3, 110.0, 130.8), (82.4, 103.8, 123.5))

    def samples(self, t):
        beat = (t * 2.0) % 1.0
        kick = np.exp(-beat * 18) * np.sin(2 * np.pi * (50 + 90 * np.exp(-beat * 30)) * t)
        chord = self.CHORDS[int(max(t[-1], 0) // 4) % len(self.CHORDS)]
        pad = sum(np.sin(2 * np.pi * f * t) for f in chord) * 0.08
        lead = 0.1 * np.sin(2 * np.pi * chord[2] * 4 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.25 * t))
        off = ((t * 2.0 + 0.5) % 1.0)
        hat = np.exp(-off * 60) * self.rng.uniform(-1, 1, len(t)) * 0.15
        left = 0.6 * kick + pad + lead * 1.2 + hat
        right = 0.6 * kick + pad + lead * 0.6 - hat * 0.5
        return np.column_stack([left, right])


SIGNALS = {"silence": Silence, "sine": Sine, "noise": Noise, "inverted": Inverted, "music": Music}
