"""Reading the intervention journal: one JSON object per line."""
from __future__ import annotations

import json
from pathlib import Path


def read_entries(path: Path) -> list[dict]:
    """Every entry in the journal, oldest first. A missing file is an empty journal."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
