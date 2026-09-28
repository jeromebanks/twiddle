"""What a station's stream actually is: codec, bitrate, sample rate, channels.

Measured, not declared: `icy-br` headers are optional and sometimes wrong
(KEXP's `kexp128.mp3` is really 96 kbps), so this asks `ffprobe`, which reads
the first frames of the stream itself. That takes a second or two and costs
the station a listener connection, so results are cached for a week -- a
station changes its encoder rarely.

Read-only: it talks to the stations, never to a speaker.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CACHE_FILE = Path.home() / ".cache" / "twiddle" / "streams.json"
TTL_S = 7 * 24 * 3600
PROBE_TIMEOUT_S = 15
# Below this a music stream audibly loses its top end; talk survives it.
LOW_KBPS = 96


@dataclass(frozen=True)
class StreamInfo:
    codec: str                 # "mp3", "aac"
    kbps: int | None
    rate_hz: int | None
    channels: int | None
    profile: str | None = None  # AAC's "LC" / "HE-AACv2"

    @property
    def low(self) -> bool:
        return (self.kbps is not None and self.kbps < LOW_KBPS) or self.channels == 1

    def label(self) -> str:
        """`MP3 128k · 44.1kHz stereo`."""
        codec = self.codec.upper()
        if self.profile and self.profile.startswith("HE"):
            codec += f" {self.profile}"
        head = f"{codec} {self.kbps}k" if self.kbps else codec
        tail = []
        if self.rate_hz:
            tail.append(f"{self.rate_hz / 1000:g}kHz")
        if self.channels:
            tail.append({1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels}ch"))
        return head + (" · " + " ".join(tail) if tail else "")


def parse_ffprobe(out: str) -> StreamInfo | None:
    data = json.loads(out or "{}")
    audio = next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), None)
    if not audio:
        return None

    def num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    bps = num(audio.get("bit_rate")) or num((data.get("format") or {}).get("bit_rate"))
    profile = audio.get("profile")
    return StreamInfo(codec=audio.get("codec_name") or "?",
                      kbps=round(bps / 1000) if bps else None,
                      rate_hz=num(audio.get("sample_rate")),
                      channels=num(audio.get("channels")),
                      profile=None if profile in (None, "unknown") else profile)


def probe(url: str) -> StreamInfo | None:
    """ffprobe the stream; None if ffprobe is missing or the stream won't say."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-rw_timeout", str(PROBE_TIMEOUT_S * 1_000_000),
             "-show_entries", "stream=codec_type,codec_name,profile,bit_rate,sample_rate,channels"
             ":format=bit_rate", "-of", "json", url],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S + 5).stdout
        return parse_ffprobe(out)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _load() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def cached(url: str, now: float | None = None) -> StreamInfo | None:
    """The cached measurement, without probing; None if absent or stale."""
    hit = _load().get(url)
    if hit and (now or time.time()) - hit.get("at", 0) < TTL_S and hit.get("info"):
        return StreamInfo(**hit["info"])
    return None


def info(url: str) -> StreamInfo | None:
    """The stream's format, from the cache or a fresh probe."""
    now = time.time()
    hit = cached(url, now)
    if hit:
        return hit
    found = probe(url)
    if found:
        data = _load()
        data[url] = {"at": now, "info": asdict(found)}
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(data, indent=1))
        except OSError:
            pass
    return found
