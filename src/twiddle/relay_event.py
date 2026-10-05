"""librespot's `--onevent` hook: remember what the relay is playing.

librespot runs `--onevent PROGRAM` once per playback event, with no
arguments and everything in the environment (`PLAYER_EVENT`, and for
`track_changed`: `NAME`, `ARTISTS`, `ALBUM`, `URI`, `COVERS`, ...). `--onevent`
takes a bare program, so the relay installs a two-line shell script that
execs this module with the state file's path (see `relay.install_onevent_hook`).

This writes that state file (the current track, and whether it is playing,
paused or stopped) and nothing else. The relay reads it when a
speaker asks for `/cover.jpg`. So this is how the relay knows *its own*
track, without asking the Web API's `/me/player`: that call reports
whichever device the account is using, which may not be this relay.

It must never fail loudly: its exit status and output go nowhere useful,
and a traceback on librespot's stderr would only clutter `relay.out`.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _list(value: str | None) -> list[str]:
    # librespot joins multi-valued fields (ARTISTS, COVERS) with newlines.
    return [v.strip() for v in (value or "").splitlines() if v.strip()]


def track_from_env(env: dict) -> dict | None:
    """The track state worth keeping, or None for an event that isn't a track change."""
    if env.get("PLAYER_EVENT") != "track_changed":
        return None
    return {
        "ts": time.time(),
        "state": "playing",
        "name": env.get("NAME", ""),
        "artists": _list(env.get("ARTISTS")),
        "album": env.get("ALBUM", ""),
        "uri": env.get("URI", ""),
        "covers": [c for c in _list(env.get("COVERS")) if c.startswith("http")],
    }


# librespot events that say whether it is making sound, and what that means
# for the state file. `paused` keeps the title (you paused mid-song); `stopped`
# is the end of the queue, or the app letting go.
STATES = {"playing": "playing", "paused": "paused", "stopped": "stopped",
          "session_disconnected": "stopped"}


def write_state(path: Path, track: dict) -> None:
    # Atomic, so the relay never reads half a file mid-write.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(track))
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        env = dict(os.environ)
        track = track_from_env(env)
        state = STATES.get(env.get("PLAYER_EVENT", ""))
        if argv and track is not None:
            write_state(Path(argv[0]), track)
        elif argv and state:
            path = Path(argv[0])
            try:
                cur = json.loads(path.read_text())
            except (OSError, ValueError):
                cur = {}
            write_state(path, cur | {"state": state, "state_ts": time.time()})
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
