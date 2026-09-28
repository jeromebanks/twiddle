"""What `dial` remembers between runs: output, theme, last station.

One small JSON file in `~/.cache/twiddle/dial/`, written atomically like
`scene`'s, so a crash mid-write cannot leave one the next launch rejects.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CACHE_DIR = Path(os.environ.get("TWIDDLE_DIAL_CACHE",
                                Path.home() / ".cache" / "twiddle" / "dial"))


def _path() -> Path:
    return CACHE_DIR / "state.json"


def _read() -> dict:
    try:
        return json.loads(_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_state(key: str, default=None):
    return _read().get(key, default)


def set_state(key: str, value) -> None:
    data = _read()
    data[key] = value
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(p)
