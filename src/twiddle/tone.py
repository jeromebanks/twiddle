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
