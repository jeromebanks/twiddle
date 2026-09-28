"""Bluetooth headphones and speakers paired with this Mac, as an `Output`.

`ffmpeg -f audiotoolbox -audio_device_index N` sends the stream to one
CoreAudio device -- the headphones -- without touching the Mac's default
output, so nothing else on the Mac moves to them. It paces itself: 6s of
audio took 6.5s to play, with or without `-re` (Mac mini speakers,
2026-09-25).

Two things are looked up by *name*, fresh on every play, because neither is
stable:

- which Bluetooth devices are paired (`system_profiler`), to offer them in
  the picker even when they're off -- a device appears in CoreAudio only
  while it's connected;
- the CoreAudio index to play to (`ffmpeg -list_devices`), which shifts
  whenever any audio device comes or goes.

If the device isn't connected, `blueutil --connect` (brew install blueutil)
is tried when it's installed; otherwise the error says to connect it from
Control Center. There is no volume control here: `audiotoolbox` has none,
and per-device volume isn't reachable from `osascript`. The headphones'
own buttons work (they set the device's volume over AVRCP).

**Not yet tried with the Bose** -- only the `audiotoolbox` path itself, on
the Mac's built-in speakers.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from .output import Media, PlaybackError, ProcessOutput, _spawn

PREFIX = "bt:"
# system_profiler's minor types for things that play sound.
AUDIO_TYPES = {"headphones", "headset", "speaker", "portable audio", "hifi audio",
               "loudspeaker", "car audio"}
CONNECT_WAIT_S = 10


@dataclass(frozen=True)
class Paired:
    name: str
    address: str
    connected: bool


def _run(argv: list[str], timeout: float = 15) -> str:
    """stdout and stderr together: ffmpeg lists devices on stderr."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout + r.stderr


def norm(name: str) -> str:
    """Names compare across system_profiler and CoreAudio: "Sam’s" vs
    "Sam's", and case."""
    name = unicodedata.normalize("NFKC", name).replace("’", "'").replace("‘", "'")
    return " ".join(name.casefold().split())


def paired(run: Callable[[list[str]], str] = _run) -> list[Paired]:
    """Paired Bluetooth devices that play audio, connected or not."""
    try:
        data = json.loads(run(["system_profiler", "SPBluetoothDataType", "-json"]) or "{}")
        root = (data.get("SPBluetoothDataType") or [{}])[0]
    except (ValueError, IndexError, AttributeError):
        return []
    out = []
    for key, connected in (("device_connected", True), ("device_not_connected", False)):
        for entry in root.get(key) or []:
            for name, info in entry.items():
                if (info.get("device_minorType") or "").lower() in AUDIO_TYPES:
                    out.append(Paired(name, info.get("device_address", ""), connected))
    return out


# "[AudioToolbox @ 0x6000..] [2]      Mac mini Speakers, BuiltInSpeakerDevice":
# the name ends at the first ", " -- UIDs have commas and spaces of their own.
_DEVICE_LINE = re.compile(r"\]\s+\[(\d+)\]\s+(.*?), ")


def coreaudio_devices(ffmpeg: str, run: Callable[[list[str]], str] = _run) -> dict[str, int]:
    """CoreAudio device name (normalised) -> the index `audiotoolbox` wants."""
    text = run([ffmpeg, "-hide_banner", "-f", "lavfi", "-i", "anullsrc", "-t", "0.01",
                "-f", "audiotoolbox", "-list_devices", "true", "-"])
    found = {}
    for line in text.splitlines():
        if "AudioToolbox" not in line:
            continue
        m = _DEVICE_LINE.search(line)
        if m and m.group(2).strip() != "(null)":
            found.setdefault(norm(m.group(2)), int(m.group(1)))
    return found


class BluetoothOutput(ProcessOutput):
    """One paired Bluetooth device, by name. See the module docstring."""
    log_name = "bluetooth.log"

    def __init__(self, name: str, address: str = "", *, dry_run: bool = False,
                 spawn=_spawn, run: Callable[[list[str]], str] = _run,
                 ffmpeg: str | None = None, blueutil: str | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        super().__init__(dry_run=dry_run, spawn=spawn)
        self.name, self.address = name, address
        self.id = PREFIX + name
        self.label = f"{name} (Bluetooth)"
        self._run = run
        self._ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self._blueutil = blueutil if blueutil is not None else shutil.which("blueutil")
        self._sleep = sleep
        self._index: int | None = None

    def unavailable(self) -> PlaybackError | None:
        return None if self._ffmpeg else PlaybackError("ffmpeg isn't installed",
                                                       "brew install ffmpeg")

    def _find(self) -> int | None:
        return coreaudio_devices(self._ffmpeg, self._run).get(norm(self.name))

    def _connect(self) -> int:
        """The device's CoreAudio index, connecting it first if need be."""
        index = self._find()
        if index is not None:
            return index
        if not self._blueutil or not self.address:
            raise PlaybackError(f"{self.name} isn't connected",
                                "turn them on and connect from Control Center -> Bluetooth"
                                + ("" if self._blueutil else
                                   " (or `brew install blueutil` and dial will do it)"))
        self._run([self._blueutil, "--connect", self.address.replace(":", "-")])
        deadline = time.monotonic() + CONNECT_WAIT_S
        while time.monotonic() < deadline:
            self._sleep(1)
            if (index := self._find()) is not None:
                return index
        raise PlaybackError(f"{self.name} didn't connect",
                            "are they on, and not connected to your phone?")

    def play(self, media: Media, *, confirmed: bool = False, source: str = "dial") -> str:
        if (why := self.unavailable()) is not None:
            raise why
        if not self.dry_run:
            self._index = self._connect()
        return super().play(media, confirmed=confirmed, source=source)

    def argv(self, media: Media) -> list[str]:
        return [self._ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
                "-i", media.url, "-vn", "-f", "audiotoolbox",
                "-audio_device_index", str(self._index), "-"]


def choices(run: Callable[[list[str]], str] = _run) -> list[tuple[str, str, Paired]]:
    """(output id, label, device) for each paired audio device."""
    return [(PREFIX + p.name,
             f"{p.name}  (Bluetooth{'' if p.connected else ', not connected'})", p)
            for p in paired(run)]
