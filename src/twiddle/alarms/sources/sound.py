"""A standard alarm sound, played from this Mac: the alarm's URI points at
`twiddle alarm serve`, which serves the files in `alarms/sounds/` under
`/sound/<name>.mp3`.

Sonos has only the one built-in chime, so these live here, are labelled the
same way as a Bandcamp track (`needs_mac`) and fall back to the chime if the
Mac can't serve them. Each file is 45 seconds (a repeating pattern, or a recording), since an
alarm's track plays once and stops. Every file's origin and licence is in
`sounds/LICENSES.md`.

`--source sound:<name>`; `alarm sources sound` lists them. Nothing here writes
to a speaker.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from ... import play
from . import Choice, Source
from .bandcamp import _household_host

ROUTE = "/sound/"
SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"
SUFFIX = ".mp3"
SOUNDS = {                      # key -> (title, what it is)
    "bell": ("Classic bell", "a struck bell, every two seconds"),
    "beep": ("Digital beep", "four short beeps a second"),
    "rise": ("Gentle rise", "a soft chord that swells over fifteen seconds"),
    "birdsong": ("Birdsong", "a forest at dusk (a recording)"),
    "chimes": ("Chimes", "a falling run of soft bells"),
}


def is_route(request_path: str) -> bool:
    """Whether a request target is under `/sound/` (a target that doesn't parse is not)."""
    try:
        return urlsplit(request_path).path.startswith(ROUTE)
    except ValueError:
        return False


def name_of(request_path: str) -> str | None:
    """The sound a request path names (`/sound/bell.mp3`, query ignored), if we have it."""
    if not is_route(request_path):
        return None
    path = urlsplit(request_path).path
    if not (path.startswith(ROUTE) and path.endswith(SUFFIX)):
        return None
    name = path[len(ROUTE):-len(SUFFIX)]
    return name if name in SOUNDS else None


def file_of(name: str) -> Path:
    return SOUNDS_DIR / f"{name}{SUFFIX}"


class Sound(Source):
    name = "sound"
    title = "Alarm sound (from this Mac)"
    needs_mac = True
    fallback = ("the Sonos chime: seen when this Mac refuses the request, not yet "
                "when it is off the network")
    takes_choice = True

    def __init__(self, host: Callable[[str | None], str] = _household_host):
        self.host = host
        self.anchor: str | None = None

    def choices(self, query: str = "") -> list[Choice]:
        q = query.strip().lower()
        return [Choice(key, title, detail) for key, (title, detail) in SOUNDS.items()
                if q in f"{key} {title} {detail}".lower()]

    def bind(self, anchor: str | None) -> "Sound":
        bound = copy.copy(self)
        bound.anchor = anchor
        return bound

    def build(self, choice: str = "") -> tuple[str, str]:
        key = choice.strip().lower()
        if key not in SOUNDS:
            raise ValueError(f"no alarm sound {choice!r}: {', '.join(SOUNDS)}")
        from .. import server           # the agent's port and types; it imports this module
        uri = f"http://{self.host(self.anchor)}:{server.alarm_port()}{ROUTE}{key}{SUFFIX}"
        return uri, play.track_didl(SOUNDS[key][0], url=uri, mime=server.TYPES[SUFFIX])

    def owns(self, uri: str, metadata: str) -> bool:
        """Matches the route and a sound we have, not the address (it is the Mac's,
        and DHCP moves it) nor which port (the agent's, when the alarm was made)."""
        try:
            parts = urlsplit(uri)
            port = parts.port
        except ValueError:
            return False
        return parts.scheme == "http" and port is not None and name_of(parts.path) is not None

    def describe(self, uri: str, metadata: str) -> str | None:
        name = name_of(urlsplit(uri).path)
        return SOUNDS[name][0] if name else None
