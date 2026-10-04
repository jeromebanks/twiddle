"""What every station is playing, kept fresh in the background.

Each station is polled on its own schedule: the one on screen and the one
tuned every `FAST_S`, the rest every `SLOW_S`, and a station that failed
waits `RETRY_S` rather than hammering a site that is down. That is about
one request every six seconds per ten stations listed -- polite to
Spinitron, whose pages are scraped, and to KEXP's API.

Only the *active* stations are polled at all (`set_active`: the ones a tag
filter leaves on screen), plus the hot ones. The catalog can grow to
hundreds; polling them all every minute would not be polite to anyone.

`on_change(key)` fires (on a worker thread) only when what a station
reports actually changed, so the UI redraws per song, not per poll.

Stations that publish their own history (KEXP, Spinitron, WFMU) supply "just
played" directly; for the ICY-only ones it is built from what this app saw
change while it was open, started over whenever a fetch names a new show.
A station on a fixed program schedule (KQED) supplies `schedule` instead,
and the panel shows the day's lineup.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..stations import NowPlaying, Station

FAST_S = 20
SLOW_S = 60
RETRY_S = 60
SEEN_MAX = 12


def _ident(np: NowPlaying | None) -> tuple:
    # The parsed song when there is one: a fetch that also carries the raw
    # title (WFMU's ICY fallback) names the same song, not a new one.
    if np is None:
        return ()
    return (np.artist, np.song) if np.artist or np.song else (None, None, np.raw_title)


@dataclass
class StationState:
    station: Station
    np: NowPlaying | None = None
    error: str | None = None
    fetched_at: float | None = None
    since: float | None = None          # when the current song was first seen
    next_at: float = 0.0
    busy: bool = False
    seen: list[dict] = field(default_factory=list)   # our own history, newest first
    # The last show a fetch actually named. A fetch naming none (an ICY
    # fallback) leaves it; only a different named show is a new show.
    show: str | None = None

    @property
    def key(self) -> str:
        return self.station.key

    def recent(self) -> list[dict]:
        return (self.np.recent if self.np and self.np.recent else None) or self.seen


class StationFeed:
    def __init__(self, stations: Iterable[Station], on_change: Callable[[str], None],
                 *, clock: Callable[[], float] = time.time, workers: int = 4):
        self.states = {s.key: StationState(s) for s in stations}
        self.on_change = on_change
        self.clock = clock
        self._hot: set[str] = set()
        self._active: set[str] | None = None       # None: every station
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="dial-feed")
        self._thread: threading.Thread | None = None

    # -- scheduling --------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="dial-feed-clock",
                                            daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(1.0)

    def tick(self) -> None:
        now = self.clock()
        with self._lock:
            due = [st for st in self.states.values() if not st.busy and st.next_at <= now
                   and (self._active is None or st.key in self._active or st.key in self._hot)]
            for st in due:
                st.busy = True
        for st in due:
            try:
                self._pool.submit(self.poll, st.key)
            except RuntimeError:        # shutting down
                return

    def set_hot(self, keys: Iterable[str | None]) -> None:
        """Poll these fast from now on -- and soon, if they were on the slow lane."""
        keys = {k for k in keys if k in self.states}
        with self._lock:
            self._hot = keys
            for k in keys:
                st = self.states[k]
                if st.fetched_at is not None and st.error is None:
                    st.next_at = min(st.next_at, st.fetched_at + FAST_S)

    def set_active(self, keys: Iterable[str] | None) -> None:
        """Poll only these (and the hot ones) from now on; None polls every
        station. What was already fetched is kept, so narrowing and widening
        again shows it at once, and a station overdue when it comes back is
        polled on the next tick."""
        with self._lock:
            self._active = None if keys is None else {k for k in keys if k in self.states}

    def refresh(self, key: str | None = None) -> None:
        with self._lock:
            for st in self.states.values():
                if key is None or st.key == key:
                    st.next_at = 0.0

    def close(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- one poll ------------------------------------------------------------

    def poll(self, key: str) -> bool:
        """Fetch one station now (synchronously). True if anything changed."""
        st = self.states[key]
        try:
            np, err = st.station.now_playing(), None
        except Exception as exc:  # any station's site can be down or reshaped
            np, err = None, f"{type(exc).__name__}: {exc}"
        now = self.clock()
        with self._lock:
            st.busy = False
            if np is None:
                changed = st.error is None
                st.error = err
                st.next_at = now + RETRY_S
            else:
                old = st.np
                changed = old is None or st.error is not None or old.to_dict() != np.to_dict()
                # A new show starts our own history over: what played before
                # it, its last song included, belongs to the show that ended.
                new_show = bool(np.show) and st.show is not None and np.show != st.show
                if np.show:
                    st.show = np.show
                if new_show:
                    st.seen.clear()
                if _ident(old) != _ident(np):
                    if not new_show and old is not None and (old.artist or old.song
                                                             or old.raw_title):
                        st.seen.insert(0, {k: v for k, v in {
                            "time": time.strftime("%H:%M", time.localtime(st.since or now)),
                            "artist": old.artist, "song": old.song or old.raw_title,
                            "album": old.album, "art_url": old.art_url}.items() if v})
                        del st.seen[SEEN_MAX:]
                    st.since = now
                st.np, st.error, st.fetched_at = np, None, now
                st.next_at = now + (FAST_S if key in self._hot else SLOW_S)
        if changed:
            self.on_change(key)
        return changed
