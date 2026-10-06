"""Regenerate the standard alarm sounds in src/twiddle/alarms/sounds/.

    uv run python tools/make_alarm_sounds.py

Every sound is synthesised here from sines (no recording, no sample, nothing
copied), so the author dedicates them to the public domain (CC0); see
src/twiddle/alarms/sounds/LICENSES.md. Each is `LENGTH` seconds of a pattern that
repeats, because an alarm's track plays once in NORMAL mode and then stops.
Encoding needs `ffmpeg`.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

RATE = 22050
LENGTH = 45
OUT = Path(__file__).resolve().parent.parent / "src/twiddle/alarms/sounds"


def t(seconds: float) -> np.ndarray:
    return np.arange(int(RATE * seconds)) / RATE


def place(track: np.ndarray, sound: np.ndarray, at: float) -> None:
    i = int(at * RATE)
    n = min(len(sound), len(track) - i)
    if n > 0:
        track[i:i + n] += sound[:n]


def bell(freq: float = 660.0) -> np.ndarray:
    """A struck bell: inharmonic partials, each fading at its own rate."""
    x = t(3.0)
    out = np.zeros_like(x)
    for ratio, amp, decay in ((1.0, 1.0, 1.6), (2.0, 0.6, 2.2), (2.76, 0.5, 2.8),
                              (5.4, 0.3, 4.0), (8.93, 0.15, 6.0)):
        out += amp * np.sin(2 * np.pi * freq * ratio * x) * np.exp(-decay * x)
    out *= np.minimum(x / 0.003, 1.0)
    return out


def beep(freq: float = 1000.0, length: float = 0.09) -> np.ndarray:
    x = t(length)
    return np.sin(2 * np.pi * freq * x) * np.minimum(np.minimum(x / 0.005, (length - x) / 0.005), 1.0)


def classic_bell() -> np.ndarray:
    track = np.zeros(LENGTH * RATE)
    for k in range(LENGTH // 2):
        place(track, bell(), k * 2.0)
    return track


def digital_beep() -> np.ndarray:
    track = np.zeros(LENGTH * RATE)
    for k in range(LENGTH):                       # four beeps, a pause, every second
        for j in range(4):
            place(track, beep(), k * 1.0 + j * 0.16)
    return track


def gentle_rise() -> np.ndarray:
    """A soft chord that swells from nothing over fifteen seconds, three times."""
    track = np.zeros(LENGTH * RATE)
    x = t(15.0)
    swell = (x / 15.0) ** 2 * np.minimum((15.0 - x) / 0.5, 1.0)
    chord = sum(np.sin(2 * np.pi * f * x) * a for f, a in ((261.63, 1.0), (329.63, 0.8), (392.0, 0.7), (523.25, 0.4)))
    for k in range(LENGTH // 15):
        place(track, chord * swell, k * 15.0)
    return track


def birdsong() -> np.ndarray:
    """Chirps: short rising and falling whistles in little phrases."""
    rng = np.random.default_rng(5)
    track = np.zeros(LENGTH * RATE)
    at = 0.2
    while at < LENGTH - 0.5:
        for _ in range(int(rng.integers(3, 7))):
            length = float(rng.uniform(0.06, 0.16))
            x = t(length)
            lo, hi = rng.uniform(2200, 3200), rng.uniform(3300, 5000)
            freq = lo + (hi - lo) * (x / length) ** (1 if rng.random() < 0.5 else 0.4)
            if rng.random() < 0.4:
                freq = hi - (freq - lo)
            phase = 2 * np.pi * np.cumsum(freq) / RATE
            env = np.sin(np.pi * x / length) ** 2
            place(track, np.sin(phase) * env * 0.8, at)
            at += length + float(rng.uniform(0.03, 0.08))
        at += float(rng.uniform(0.7, 1.6))
    return track


def chimes() -> np.ndarray:
    """A falling pentatonic run on soft bells, then a rest, repeating."""
    track = np.zeros(LENGTH * RATE)
    scale = (1046.5, 880.0, 783.99, 659.25, 587.33, 523.25)
    for k in range(LENGTH // 6):
        for j, f in enumerate(scale):
            place(track, bell(f) * 0.7, k * 6.0 + j * 0.35)
    return track


SOUNDS = {"bell": classic_bell, "beep": digital_beep, "rise": gentle_rise,
          "birdsong": birdsong, "chimes": chimes}


def write(name: str, samples: np.ndarray) -> Path:
    samples = samples / max(float(np.abs(samples).max()), 1e-9) * 0.8
    pcm = (samples * 32767).astype("<i2")
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{name}.mp3"
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / f"{name}.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm.tobytes())
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1",
                        "-b:a", "48k", "-map_metadata", "-1", "-fflags", "+bitexact",
                        str(dest)], check=True)
    return dest


def main() -> int:
    for name, make in SOUNDS.items():
        dest = write(name, make())
        print(f"{dest.name}: {dest.stat().st_size // 1024} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
