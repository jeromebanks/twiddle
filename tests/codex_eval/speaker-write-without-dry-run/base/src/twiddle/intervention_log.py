"""The journal of everything twiddle wrote to a speaker: `logs/interventions.jsonl`."""
from __future__ import annotations

import json
import time
from pathlib import Path

PATH = Path("logs") / "interventions.jsonl"


def record(event: str, **fields) -> None:
    """Append one journal line for a write."""
    PATH.parent.mkdir(parents=True, exist_ok=True)
    with PATH.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "event": event, **fields}) + "\n")
