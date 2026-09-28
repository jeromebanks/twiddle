"""A window of stereo PCM -> one `Frame`: what every visualizer draws from.

The band maths follows cava's, the reference terminal spectrum: log-spaced
bands, an automatic sensitivity that keeps the loudest band just under the
top, and "gravity" -- a bar jumps up at once and falls back accelerating,
which is what makes bars read as music rather than noise. Beats are spectral
flux in the low bands against their own running average.

Pure numpy and deterministic: the same windows at the same `dt` give the
same frames, which is what lets the contract test pin every visualizer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

RATE = 22050             # what the tap decodes to; plenty for eyes
FFT_N = 4096             # 186 ms: fine enough for 50 Hz bands at this rate
WAVE_N = 2048            # what the waveform views get
N_BANDS = 64
F_MIN, F_MAX = 45.0, 10_000.0
SILENT_RMS = 3e-4        # about -70 dBFS: a paused relay, a dead stream
GRAVITY = 3.2            # band heights per second², falling
PEAK_HOLD_S = 0.35
PEAK_FALL = 0.6          # band heights per second, after the hold


def _zeros(n: int) -> np.ndarray:
    return np.zeros(n, dtype=np.float32)


@dataclass
class Frame:
    """Everything a visualizer may read. Arrays are float32.

    `signal` is False when there is no tap at all (the screen draws its own
    "no signal" card then, so a visualizer only ever sees real frames, and
    silence as silence)."""
    bands: np.ndarray = field(default_factory=lambda: _zeros(N_BANDS))   # 0..1, low -> high, with gravity
    raw_bands: np.ndarray = field(default_factory=lambda: _zeros(N_BANDS))   # 0..1, no smoothing
    peaks: np.ndarray = field(default_factory=lambda: _zeros(N_BANDS))   # 0..1, held caps
    wave: np.ndarray = field(default_factory=lambda: _zeros(WAVE_N))     # mono, about -1..1
    left: np.ndarray = field(default_factory=lambda: _zeros(WAVE_N))
    right: np.ndarray = field(default_factory=lambda: _zeros(WAVE_N))
    rms: float = 0.0          # of the window, raw (0..1)
    energy: float = 0.0       # 0..1, smoothed loudness relative to the song
    bass: float = 0.0         # 0..1, the lowest quarter of the bands
    treble: float = 0.0       # 0..1, the top half
    onset: float = 0.0        # 0..1, how sharply the lows just rose
    beat: bool = False        # an onset strong enough to call a beat
    silent: bool = True
    signal: bool = True
    t: float = 0.0            # seconds since the screen opened
    dt: float = 0.0           # seconds since the last frame
    frame: int = 0

    @property
    def n_bands(self) -> int:
        return len(self.bands)


def band_edges(n_bands: int = N_BANDS, n_fft: int = FFT_N, rate: int = RATE,
               fmin: float = F_MIN, fmax: float = F_MAX) -> np.ndarray:
    """FFT bin index where each band starts, n_bands + 1 of them. Every band
    gets at least one bin; the lowest few share neighbours' resolution,
    which is the honest limit of the window, not a bug."""
    freqs = np.geomspace(fmin, fmax, n_bands + 1)
    bins = np.round(freqs * n_fft / rate).astype(int)
    bins = np.clip(bins, 1, n_fft // 2)
    for i in range(1, len(bins)):
        bins[i] = max(bins[i], bins[i - 1] + 1)
    return np.minimum(bins, n_fft // 2 + 1)


class Analyser:
    """Stateful: the smoothing, the sensitivity and the beat detector all
    remember the previous frames. One per screen."""

    def __init__(self, n_bands: int = N_BANDS, rate: int = RATE):
        self.n_bands = n_bands
        self.rate = rate
        self.edges = band_edges(n_bands, FFT_N, rate)
        self.window = np.hanning(FFT_N).astype(np.float32)
        self.sens = 1.0
        self.primed = False
        self.bands = _zeros(n_bands)
        self.velocity = _zeros(n_bands)
        self.peaks = _zeros(n_bands)
        self.peak_age = _zeros(n_bands)
        self.prev_raw = _zeros(n_bands)
        self.flux_avg = 0.0
        self.since_beat = 1.0
        self.energy = 0.0
        self.t = 0.0
        self.n = 0

    def _spectrum(self, mono: np.ndarray) -> np.ndarray:
        x = mono[-FFT_N:]
        if len(x) < FFT_N:
            x = np.concatenate([np.zeros(FFT_N - len(x), dtype=np.float32), x])
        mag = np.abs(np.fft.rfft(x * self.window)) / (FFT_N / 4)
        # Mean magnitude per band, by cumulative sums: one pass, no loop.
        c = np.concatenate([[0.0], np.cumsum(mag)])
        lo, hi = self.edges[:-1], self.edges[1:]
        per = (c[hi] - c[lo]) / (hi - lo)
        # Lows carry far more energy than highs in any music; tilt it back
        # (cava does the same with its per-band EQ) so the right half moves.
        tilt = np.linspace(1.0, 4.0, self.n_bands)
        return (np.sqrt(per) * tilt).astype(np.float32)

    def frame(self, stereo: np.ndarray, dt: float) -> Frame:
        """`stereo`: (n, 2) float32, the newest sample last."""
        dt = float(min(max(dt, 1e-3), 0.25))
        self.t += dt
        self.n += 1
        stereo = np.asarray(stereo, dtype=np.float32)
        if stereo.ndim != 2 or stereo.shape[1] != 2 or len(stereo) == 0:
            stereo = np.zeros((FFT_N, 2), dtype=np.float32)
        stereo = np.nan_to_num(stereo, copy=False)
        mono = stereo.mean(axis=1)
        tail = stereo[-WAVE_N:]
        if len(tail) < WAVE_N:
            tail = np.concatenate([np.zeros((WAVE_N - len(tail), 2), np.float32), tail])
        rms = float(np.sqrt(np.mean(mono[-FFT_N:] ** 2)))
        silent = rms < SILENT_RMS

        spec = self._spectrum(mono)
        if silent:
            raw = _zeros(self.n_bands)
        else:
            if not self.primed:
                # The first sound sets the scale; the loop below then tracks it.
                self.sens, self.primed = 0.8 / max(float(spec.max()), 1e-6), True
            raw = spec * self.sens
            top = float(raw.max())
            # Auto-sensitivity: back off fast on overshoot, creep up slowly.
            if top > 0.98:
                self.sens *= 0.98 / top
                raw = raw * (0.98 / top)
            else:
                self.sens = min(self.sens * (1 + 0.12 * dt), 400.0)
            raw = np.clip(raw, 0.0, 1.0)

        # Gravity: rise at once, fall accelerating.
        rising = raw >= self.bands
        self.velocity = np.where(rising, 0.0, self.velocity + GRAVITY * dt)
        fallen = self.bands - self.velocity * dt
        self.bands = np.where(rising, raw, np.maximum(fallen, raw)).astype(np.float32)

        up = self.bands >= self.peaks
        self.peak_age = np.where(up, 0.0, self.peak_age + dt).astype(np.float32)
        held = self.peak_age < PEAK_HOLD_S
        self.peaks = np.where(up, self.bands,
                              np.where(held, self.peaks,
                                       np.maximum(self.peaks - PEAK_FALL * dt, self.bands)))
        self.peaks = self.peaks.astype(np.float32)

        # Beats: positive flux in the lowest quarter against its own average.
        low = max(1, self.n_bands // 4)
        flux = float(np.maximum(raw[:low] - self.prev_raw[:low], 0).sum() / low)
        self.prev_raw = raw
        self.flux_avg += (flux - self.flux_avg) * min(1.0, dt * 2.0)
        self.since_beat += dt
        onset = float(np.clip(flux / (self.flux_avg * 3 + 1e-3), 0.0, 1.0))
        beat = (not silent and flux > self.flux_avg * 1.6 + 0.02 and self.since_beat > 0.18)
        if beat:
            self.since_beat = 0.0

        loud = float(self.bands.mean())
        self.energy += (loud - self.energy) * min(1.0, dt * 6.0)
        return Frame(bands=self.bands.copy(), raw_bands=raw.astype(np.float32),
                     peaks=self.peaks.copy(), wave=tail.mean(axis=1),
                     left=tail[:, 0].copy(), right=tail[:, 1].copy(),
                     rms=rms, energy=float(np.clip(self.energy * 2.2, 0, 1)),
                     bass=float(self.bands[:low].mean()),
                     treble=float(self.bands[self.n_bands // 2:].mean()),
                     onset=onset, beat=beat, silent=silent, signal=True,
                     t=self.t, dt=dt, frame=self.n)
