"""Small JSON files that survive a crash and a second process.

`scene`'s client state (pins, theme, device) and its producer's caches (band
aliases, Bandcamp answers, venue summaries) are each a few JSON files in one
directory. Both go through this, so a half-written file can never be renamed
into place by the other process.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


def read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per process and thread: `scene build` and the app are two
    # processes writing the same files, and a shared temp name could be
    # renamed into place half-written by the other.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
