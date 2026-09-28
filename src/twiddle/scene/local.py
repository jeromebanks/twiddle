"""This Mac's speakers as a Spotify device, for anyone without the Roams.

Spotify only plays on a registered Connect device, and a Mac is one only
while the Spotify desktop app is open. Rather than require that, the app
runs its own librespot with the `rodio` backend (CoreAudio's default
output) under a name of its own, e.g. "Sam's Mac mini (scene)".

It is started on the first play to it, and stopped when the app quits --
but only if this app started it. One that is already registered (another
`scene` window, say) is reused and left alone.

Credentials live in their own directory. If it has none, the relay's are
copied (e.g. when `relay login` already ran); otherwise
`twiddle scene login` signs in. Spotify Premium is required either way.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .. import here, relay, spotify, spotify_ops
from ..relay_cli import DEFAULT_CACHE

LOCAL_CACHE = Path.home() / ".cache" / "twiddle" / "librespot-local"
LOCAL_LABEL = here.LABEL
REGISTER_TIMEOUT_S = 15.0


def machine_name() -> str:
    """The name Spotify shows: the Mac's own name, marked as this app's."""
    try:
        name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, check=False,
                              text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    name = name or socket.gethostname().split(".")[0] or here.THIS
    if here.KIND == "Chromebook" and name == "penguin":    # every Linux container's name
        name = "Chromebook"
    return f"{name} (scene)"


def _spawn(argv: list[str], log: Path) -> subprocess.Popen:
    # Its own log file, never the terminal: librespot logs a line a second
    # while playing, and on the inherited stderr that would draw over the TUI.
    with open(log, "ab") as out:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT)


class LocalSpeaker:
    """Starts, finds and stops the local librespot. Thread-safe."""

    def __init__(self, name: str | None = None, cache_dir: Path = LOCAL_CACHE,
                 relay_cache: Path = DEFAULT_CACHE,
                 spawn: Callable[[list[str], Path], object] = _spawn,
                 sleep: Callable[[float], None] = time.sleep):
        self._name = name
        self.cache_dir = Path(cache_dir)
        self.relay_cache = Path(relay_cache)
        self._spawn = spawn
        self._sleep = sleep
        self._proc = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        if self._name is None:
            self._name = machine_name()
        return self._name

    @property
    def log(self) -> Path:
        return self.cache_dir / "librespot.log"

    def find(self, devices: list[dict]) -> dict | None:
        # Exact only: a prefix match could land on the Spotify app's own
        # "Sam's Mac mini" entry.
        return next((d for d in devices
                     if d.get("name", "").casefold() == self.name.casefold()), None)

    def _credentials(self) -> None:
        creds = Path(relay.credentials_path(str(self.cache_dir)))
        if creds.exists():
            return
        theirs = Path(relay.credentials_path(str(self.relay_cache)))
        if not theirs.exists():
            raise spotify_ops.PlaybackError(
                "this Mac isn't signed in to Spotify as a speaker yet",
                "run `twiddle scene login` once (needs Spotify Premium)")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(theirs, creds)

    def start(self, sess, notify: Callable[[str], None] = lambda m: None) -> dict:
        """The local device as Spotify lists it, starting librespot if needed."""
        with self._lock:
            dev = self.find(sess.devices())
            if dev:
                return dev
            if self._proc is None or self._proc.poll() is not None:
                self._credentials()
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                notify(f"starting {LOCAL_LABEL}…")
                self._proc = self._spawn(relay.local_argv(self.name, str(self.cache_dir)),
                                         self.log)
            deadline = time.monotonic() + REGISTER_TIMEOUT_S
            while time.monotonic() < deadline:
                self._sleep(1.0)
                if self._proc.poll() is not None:
                    raise spotify_ops.PlaybackError(
                        "the local Spotify player exited as it started",
                        f"see {self.log}; `twiddle scene login` if it can't sign in")
                try:
                    dev = self.find(sess.devices())
                except spotify.ApiError as exc:
                    if exc.status == 429:
                        # Polling into a rate limit only extends it.
                        raise spotify_ops.PlaybackError(
                            f"rate limited while waiting for {self.name!r} to appear",
                            f"wait {exc.retry_after or 60:.0f}s and press p again",
                            status=429) from exc
                    raise
                if dev:
                    return dev
            raise spotify_ops.PlaybackError(
                f"the local player is running but Spotify doesn't list {self.name!r}",
                f"press p again in a moment; see {self.log}")

    def stop(self) -> None:
        """Stop librespot if this app started it; leave anyone else's alone."""
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
