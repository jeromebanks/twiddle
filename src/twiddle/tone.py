"""Generate a long, quiet test signal for soak-testing playback.

The A/B test that separates "Spotify's fault" from "the network's fault"
needs a control stream with no cloud service behind it. A locally generated
WAV served off this Mac is exactly that: if it plays for 30 minutes without
a gap while Spotify cannot, the problem is not the radio.

Pink-ish noise is used rather than a sine tone because dropouts in noise are
obvious to the ear, and a steady sine at low volume is easy to stop noticing.
"""
from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

RATE = 44100


def pink_noise(seconds: float, amplitude: float = 0.08,
               seed: int = 7) -> bytes:
    """Voss-McCartney pink noise, 16-bit stereo, interleaved."""
    rng = random.Random(seed)
    rows = 12
    values = [rng.uniform(-1, 1) for _ in range(rows)]
    total = sum(values)
    frames = bytearray()
    n = int(RATE * seconds)
    for i in range(n):
        # Update one row per sample, chosen by trailing-zero count.
        idx = (i & -i).bit_length() - 1
        if 0 <= idx < rows:
            total -= values[idx]
            values[idx] = rng.uniform(-1, 1)
            total += values[idx]
        s = max(-1.0, min(1.0, (total / rows) * amplitude * 4))
        v = int(s * 32767)
        frames += struct.pack("<hh", v, v)
    return bytes(frames)


def beep_marks(seconds: float, every: float = 60.0,
               freq: float = 880.0, amplitude: float = 0.05) -> bytes:
    """Short tick once a minute, so a gap is audible and locatable."""
    frames = bytearray()
    n = int(RATE * seconds)
    tick = int(RATE * 0.08)
    period = int(RATE * every)
    for i in range(n):
        pos = i % period
        v = 0
        if pos < tick:
            v = int(math.sin(2 * math.pi * freq * (pos / RATE))
                    * amplitude * 32767)
        frames += struct.pack("<hh", v, v)
    return bytes(frames)


def write_soak_wav(path: Path, minutes: float = 30.0,
                   amplitude: float = 0.08) -> Path:
    """Noise bed plus a once-a-minute tick, mixed and written as a WAV."""
    seconds = minutes * 60
    noise = pink_noise(seconds, amplitude)
    marks = beep_marks(seconds)
    mixed = bytearray(len(noise))
    for i in range(0, len(noise), 2):
        a = struct.unpack_from("<h", noise, i)[0]
        b = struct.unpack_from("<h", marks, i)[0]
        struct.pack_into("<h", mixed, i, max(-32768, min(32767, a + b)))

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(mixed))
    return path


# ---- alarm sounds ------------------------------------------------------------
#
# Mono floats in -1..1 at `SOUND_RATE`, built from sines only (nothing copied
# from a recording). `tools/make_alarm_sounds.py` encodes them.

SOUND_RATE = 22050


def _env_in_out(n: int, attack: int, release: int, i: int) -> float:
    return min(1.0, i / max(attack, 1), (n - i) / max(release, 1))


def bell(freq: float = 660.0, seconds: float = 3.0) -> list[float]:
    """A struck bell: inharmonic partials, each fading at its own rate."""
    partials = ((1.0, 1.0, 1.6), (2.0, 0.6, 2.2), (2.76, 0.5, 2.8),
                (5.4, 0.3, 4.0), (8.93, 0.15, 6.0))
    out = []
    for i in range(int(SOUND_RATE * seconds)):
        x = i / SOUND_RATE
        s = sum(a * math.sin(2 * math.pi * freq * r * x) * math.exp(-d * x)
                for r, a, d in partials)
        out.append(s * min(x / 0.003, 1.0))
    return out


def beep(freq: float = 1000.0, seconds: float = 0.09) -> list[float]:
    n = int(SOUND_RATE * seconds)
    edge = int(SOUND_RATE * 0.005)
    return [math.sin(2 * math.pi * freq * i / SOUND_RATE) * _env_in_out(n, edge, edge, i)
            for i in range(n)]


def swell(freqs: tuple[tuple[float, float], ...], seconds: float) -> list[float]:
    """A chord of `(freq, amplitude)` that grows from nothing, with a short fade at the end."""
    n = int(SOUND_RATE * seconds)
    edge = int(SOUND_RATE * 0.5)
    return [sum(a * math.sin(2 * math.pi * f * i / SOUND_RATE) for f, a in freqs)
            * (i / n) ** 2 * min(1.0, (n - i) / edge) for i in range(n)]


def place(track: list[float], sound: list[float], at: float) -> None:
    """Mix `sound` into `track` starting `at` seconds in (cut off at the end)."""
    start = int(at * SOUND_RATE)
    for i, v in enumerate(sound[:max(len(track) - start, 0)]):
        track[start + i] += v


def classic_bell(seconds: float = 45.0) -> list[float]:
    track = [0.0] * int(seconds * SOUND_RATE)
    note = bell()
    for k in range(int(seconds // 2)):
        place(track, note, k * 2.0)
    return track


def digital_beep(seconds: float = 45.0) -> list[float]:
    """Four short beeps, then a pause, every second."""
    track = [0.0] * int(seconds * SOUND_RATE)
    tick = beep()
    for k in range(int(seconds)):
        for j in range(4):
            place(track, tick, k + j * 0.16)
    return track


def gentle_rise(seconds: float = 45.0) -> list[float]:
    """A soft chord that swells from nothing over fifteen seconds, repeating."""
    track = [0.0] * int(seconds * SOUND_RATE)
    chord = swell(((261.63, 1.0), (329.63, 0.8), (392.0, 0.7), (523.25, 0.4)), 15.0)
    for k in range(int(seconds // 15)):
        place(track, chord, k * 15.0)
    return track


def chimes(seconds: float = 45.0) -> list[float]:
    """A falling pentatonic run on soft bells, then a rest, repeating."""
    track = [0.0] * int(seconds * SOUND_RATE)
    notes = [[0.7 * v for v in bell(f)] for f in (1046.5, 880.0, 783.99, 659.25, 587.33, 523.25)]
    for k in range(int(seconds // 6)):
        for j, note in enumerate(notes):
            place(track, note, k * 6.0 + j * 0.35)
    return track


def write_mono_wav(path: Path, samples: list[float], peak: float = 0.8) -> Path:
    """Normalise to `peak` and write 16-bit mono at `SOUND_RATE`."""
    top = max((abs(v) for v in samples), default=0.0) or 1.0
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SOUND_RATE)
        w.writeframes(b"".join(struct.pack("<h", int(v / top * peak * 32767)) for v in samples))
    return path
