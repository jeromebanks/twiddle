"""The speaker's own built-in alarm sound. It needs nothing else."""
from __future__ import annotations

import re

from ..model import CHIME_URI
from . import Source

_BUZZER = re.compile(r"x-rincon-buzzer:\d+")      # what `build` makes, and nothing else


class Chime(Source):
    name = "chime"
    title = "Sonos chime"
    needs_mac = False
    fallback = "none needed: it is the speaker's own sound"

    def build(self, choice: str = "") -> tuple[str, str]:
        return CHIME_URI, ""

    def owns(self, uri: str, metadata: str) -> bool:
        return _BUZZER.fullmatch(uri) is not None

    def describe(self, uri: str, metadata: str) -> str | None:
        return self.title

    def sound_source(self) -> tuple[str, str]:
        return "sonos_chime", "Sonos chime"         # the speaker's, not twiddle's
