"""A volume and mute of twiddle's own, for anything that plays by running ffmpeg.

Not the system's volume: turning twiddle down must not turn down the rest of
the machine's audio, and some sinks (a Bluetooth device, a socket) have no
system volume to reach. The level is an ffmpeg `volume@v` filter in the
command line (`args()`), retuned *live* afterwards by writing ffmpeg's
interactive command to its stdin (`push()`), so there is no restart, no gap,
and a track keeps its place. Spawn the process with `stdin=PIPE` and without
`-nostdin`.

100 is unity and never boosts. The curve is squared, since loudness is not
linear in amplitude. The level lives here, not in the process: every respawn
starts at it. `dial.output.ProcessOutput` uses this; a podcast or Bandcamp
player that runs ffmpeg can too. `ffplay` cannot be commanded this way.
"""
from __future__ import annotations

FILTER = "volume@v"


def clamp(v: int) -> int:
    return max(0, min(100, int(v)))


class LiveGain:
    def __init__(self, volume: int = 100, muted: bool = False):
        self.volume, self.muted = clamp(volume), bool(muted)

    @property
    def factor(self) -> float:
        return 0.0 if self.muted else (self.volume / 100) ** 2

    def args(self) -> list[str]:
        """Put these in the ffmpeg command line, before the output."""
        return ["-af", f"{FILTER}={self.factor:.4f}"]

    def command(self) -> bytes:
        # `c` then one line: ffmpeg reads the line after the key.
        return f"c{FILTER} -1 volume {self.factor:.4f}\n".encode()

    def push(self, proc) -> bool:
        """Retune a running ffmpeg; False if there's nothing to tell (not
        running, no stdin, or it just exited). The level is kept regardless."""
        stdin = getattr(proc, "stdin", None)
        if stdin is None:
            return False
        try:
            stdin.write(self.command())
            stdin.flush()
        except (OSError, ValueError):
            return False
        return True
