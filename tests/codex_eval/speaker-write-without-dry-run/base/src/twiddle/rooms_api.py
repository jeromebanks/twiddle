"""Finding a speaker by its room's name, and writing to it."""
from __future__ import annotations

import soco


def find_room(name: str) -> soco.SoCo:
    """The speaker for the room called `name`. Raises `LookupError` when there is none."""
    speaker = soco.discovery.by_name(name)
    if speaker is None:
        raise LookupError(f"no room called {name!r}")
    return speaker


def set_mute(room: str, muted: bool) -> None:
    """Mute or unmute the room's speaker. This writes to the speaker."""
    find_room(room).mute = muted
