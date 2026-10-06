"""Regenerate the standard alarm sounds in src/twiddle/alarms/sounds/.

    uv run python tools/make_alarm_sounds.py            # the generated four
    uv run python tools/make_alarm_sounds.py --recording FILE   # birdsong.mp3 from a CC0 recording

The generated sounds are synthesised in `twiddle.tone` from sines, nothing
copied. The birdsong is a recording, trimmed and encoded here; its source and
licence are in src/twiddle/alarms/sounds/LICENSES.md. Each is 45 seconds, since
an alarm's track plays once and stops. Encoding needs `ffmpeg`.
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from twiddle import tone

OUT = Path(__file__).resolve().parent.parent / "src/twiddle/alarms/sounds"
LENGTH = 45
GENERATED = {"bell": tone.classic_bell, "beep": tone.digital_beep,
             "rise": tone.gentle_rise, "chimes": tone.chimes}


def encode(src: Path, dest: Path, start: str = "0", *filt: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", start, "-i", str(src), "-t", str(LENGTH),
                    *filt, "-ac", "1", "-ar", str(tone.SOUND_RATE), "-b:a", "48k", "-map_metadata", "-1",
                    "-fflags", "+bitexact", str(dest)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--recording", type=Path, help="a CC0 birdsong recording to trim into birdsong.mp3")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.recording:
        encode(args.recording, OUT / "birdsong.mp3", "5",
               "-af", "afade=t=in:d=1,afade=t=out:st=43:d=2")
    else:
        for name, make in GENERATED.items():
            with tempfile.TemporaryDirectory() as tmp:
                wav = tone.write_mono_wav(Path(tmp) / f"{name}.wav", make(LENGTH))
                encode(wav, OUT / f"{name}.mp3")
    for f in sorted(OUT.glob("*.mp3")):
        print(f"{f.name}: {f.stat().st_size // 1024} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
