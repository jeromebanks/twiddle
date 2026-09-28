"""What to call the machine this runs on, for "play here" labels.

A Mac is "This Mac"; a Chromebook's Linux container (ChromeOS mounts
`/dev/.cros_milestone` into it) is "This Chromebook"; anything else is
"This computer". The output id stays `mac` everywhere -- it is saved state,
not a label.
"""
from __future__ import annotations

import sys
from pathlib import Path


def kind() -> str:
    if sys.platform == "darwin":
        return "Mac"
    if Path("/dev/.cros_milestone").exists():
        return "Chromebook"
    return "computer"


KIND = kind()
THIS = f"This {KIND}"
LABEL = f"{THIS} (speakers)"
