"""Backpressure and progress for `scene build`.

A build asks three services for roughly a thousand bands' worth of answers
(MusicBrainz, Bandcamp, Spotify), each with a limit of its own. This module is
what keeps the build polite and tells whoever is watching how it is going.

**Backpressure** (`Backpressure`). A service that answers "slow down" with a
time (Spotify's 429 and its `Retry-After`; Bandcamp's own back-off) is waited
out *once*, in full, and the band is retried -- so a long first build can
finish unattended. It is not retried early: Spotify has been seen to lengthen
`Retry-After` for every early retry (docs/SPOTIFY.md). When a wait is not
worth it -- no stated time, longer than `max_wait_s`, the limit again straight
after waiting, or `max_waits` already spent -- the service is paused for the
rest of the run and the next build tries it again, as before. Each wait also
widens the gap between bands for that service (doubling, up to `max_gap_s`),
and every success narrows it again toward where it started.

**Progress** (`Progress`). The ETA is measured seconds per band over the last
`window` bands, waits included, times the bands left: request counts would
mislead, because reused and cached bands cost almost nothing and fresh ones
cost several requests. It is written, with the per-service request rates
against their caps (`netstats`), to `scenespec/buildstatus.py`'s file every
few seconds, and as one line to the build log every checkpoint.
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from .. import bandcamp, netstats, spotify
from ..scenespec import buildstatus


class Backpressure:
    def __init__(self, base_gaps: dict[str, float] | None = None, *,
                 pace: Callable[[float], None] = time.sleep,
                 max_wait_s: float = 900.0, max_waits: int = 3, max_gap_s: float = 8.0,
                 on_wait: Callable[[str, float], None] | None = None):
        self.base = dict(base_gaps or {})
        self.gaps = dict(self.base)
        self.pace = pace
        self.max_wait_s, self.max_waits, self.max_gap_s = max_wait_s, max_waits, max_gap_s
        self.waits: dict[str, int] = {}
        self.on_wait = on_wait

    def seconds_to_wait(self, service: str, exc: Exception) -> float | None:
        """How long to wait out `exc` before one retry, or None: pause the service."""
        if self.waits.get(service, 0) >= self.max_waits:
            return None
        if isinstance(exc, spotify.ApiError) and exc.status == 429:
            seconds = exc.retry_after
        elif isinstance(exc, bandcamp.BlockedError):
            seconds = bandcamp.blocked_for()
        else:
            return None
        if seconds <= 0 or seconds > self.max_wait_s:
            return None
        return seconds + 1.0            # the time it named, and a second past it

    def wait(self, service: str, seconds: float) -> None:
        self.waits[service] = self.waits.get(service, 0) + 1
        base = self.base.get(service, 0.0)
        if base:
            self.gaps[service] = min(self.max_gap_s, max(base * 2, self.gaps.get(service, base) * 2))
        netstats.record_wait(service, seconds)
        if self.on_wait:
            self.on_wait(service, seconds)
        self.pace(seconds)

    def ok(self, service: str) -> None:
        """A band went through: let the gap relax toward where it started."""
        base = self.base.get(service)
        if base:
            self.gaps[service] = max(base, self.gaps.get(service, base) * 0.9)

    def gap(self, service: str) -> float:
        return self.gaps.get(service, 0.0)

    @staticmethod
    def reason(exc: Exception) -> str:
        """Why a service was paused, for the dataset's `enrichers` note."""
        after = getattr(exc, "retry_after", 0) or 0
        return f"rate-limited (retry after {after:.0f}s)" if after else "rate-limited"


class Progress:
    """Where a build is, how fast, and when it should finish."""

    def __init__(self, dataset_path=None, log: Callable[[str], None] = lambda _m: None, *,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, window: int = 50,
                 min_write_s: float = 2.0, gaps: Callable[[], dict] = lambda: {},
                 pid: int | None = None, write: bool = True):
        import os
        self.dataset_path, self.log = dataset_path, log
        self.clock, self.wall, self.min_write_s, self.gaps = clock, wall, min_write_s, gaps
        self.pid = pid if pid is not None else os.getpid()
        self.write = write
        self.started_wall = wall()
        self.phase_name, self.message = "starting", ""
        self.total = self.done = 0
        self._durations: deque[float] = deque(maxlen=window)
        self._mark = clock()
        self._waiting: tuple[str, float] | None = None     # (service, ends at on `clock`)
        self._last_write = float("-inf")

    # -- what the builder reports --

    def phase(self, name: str, message: str = "") -> None:
        self.phase_name, self.message = name, message
        self.flush(force=True)

    def begin(self, total: int) -> None:
        self.phase_name, self.total, self.done = "enriching", total, 0
        self._mark = self.clock()
        self.flush(force=True)

    def band_done(self) -> None:
        now = self.clock()
        self._durations.append(now - self._mark)
        self._mark = now
        self.done += 1
        self.flush()

    def waiting(self, service: str, seconds: float) -> None:
        self._waiting = (service, self.clock() + seconds)
        self.flush(force=True)

    def finish(self, state: str = "done", message: str = "") -> None:
        self._waiting = None
        self.phase_name, self.message = "done", message
        self.flush(force=True, state=state)

    # -- what it reads out --

    def rate_and_eta(self) -> tuple[float | None, float | None]:
        if len(self._durations) < 3:
            return None, None
        avg = sum(self._durations) / len(self._durations)
        if avg <= 0:
            return None, None
        left = max(0, self.total - self.done)
        wait_left = max(0.0, self._waiting[1] - self.clock()) if self._waiting else 0.0
        return 60.0 / avg, left * avg + wait_left

    def snapshot(self, state: str = "running") -> dict:
        rate, eta = self.rate_and_eta()
        waiting = None
        if self._waiting and self._waiting[1] > self.clock():
            waiting = {"service": self._waiting[0],
                       "remaining_s": round(self._waiting[1] - self.clock())}
        return {"state": state, "pid": self.pid, "started_at": self.started_wall,
                "updated_at": self.wall(), "phase": self.phase_name, "message": self.message,
                "done": self.done, "total": self.total,
                "bands_per_min": None if rate is None else round(rate, 2),
                "eta_s": None if eta is None else round(eta),
                "waiting": waiting, "services": netstats.snapshot(),
                "gaps": {k: round(v, 2) for k, v in self.gaps().items()}}

    def line(self) -> str:
        """One progress line for the log."""
        st = self.snapshot()
        bits = [f"  {self.done}/{self.total} bands"]
        if st["bands_per_min"] is not None:
            bits.append(f"{st['bands_per_min']:.1f}/min")
            bits.append(f"eta {buildstatus.duration(st['eta_s'])}")
        for name, s in sorted(st["services"].items()):
            if s["cap_rpm"] and s["total"]:
                bits.append(f"{name} {s['rpm']}/{s['cap_rpm']}rpm")
            if s["limited"]:
                bits.append(f"{name} slowed us {s['limited']}x")
        if st["waiting"]:
            bits.append(f"waiting on {st['waiting']['service']} "
                        f"{buildstatus.duration(st['waiting']['remaining_s'])}")
        return " · ".join(bits)

    def flush(self, force: bool = False, state: str = "running") -> None:
        """Write the status file (at most every `min_write_s`). Never raises."""
        if not self.write:
            return
        t = self.clock()
        if not force and t - self._last_write < self.min_write_s:
            return
        self._last_write = t
        try:
            buildstatus.write(self.snapshot(state), self.dataset_path)
        except Exception:
            pass
