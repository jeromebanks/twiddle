"""Stay under a service's rate limit *before* it tells us off.

`netstats` watches requests after the fact. This is the other half: one
`Governor` per service that every client of that service goes through, so the
whole machine -- `scene build`, the scene TUI, `dial`, the relay, the next
Spotify tool -- shares one budget and one memory of being told to stop.

    gov = ratelimit.governor("spotify")
    gov.acquire()                       # sleeps a little, or raises RateLimited
    resp = requests.get(...)
    gov.report(resp.status_code, retry_after)

**A budget is several windows at once.** "10 a second, 100 a minute, 1500 a
day": a burst, a rate, and a total, and a request waits until all of them have
room. A long window matters as much as a short one -- Spotify answered a
first full build with a `Retry-After` of 23 hours, which no per-second pacing
prevents.

**Whose number is it?** Each `Policy` says. MusicBrainz publishes its limit.
Spotify publishes none (it meters a rolling 30 seconds per client id and
punishes overshoot with a `Retry-After` that can be a day), so its windows are
a budget of ours, conservative and meant to be calibrated from `twiddle limits`
and `scene status`. Override any of them in `~/.config/twiddle/ratelimits.toml`:

    [spotify]
    limits = [[10, 10], [100, 60], [1500, 86400]]    # [requests, seconds] ...

**It remembers across processes.** Requests are kept in a small ledger file
(per process, merged on read), and a 429's `Retry-After` is kept as
"blocked until" there, so the next build, or any other tool, doesn't send the
request that would lengthen the penalty -- it raises `RateLimited` with the
time left instead.

**It never blocks for long.** `acquire` sleeps at most `max_block_s` (short, so
a worker thread isn't parked for minutes); anything longer raises
`RateLimited(retry_after=...)` and the caller decides -- `scene build` waits
out a stated limit once, or pauses the service and lets the next build resume.

Adding a service is a `Policy` in `POLICIES` and two calls around its request.
"""
from __future__ import annotations

import atexit
import fcntl
import os
import threading
import time
import tomllib
from collections import deque
from dataclasses import dataclass
from pathlib import Path

LEDGER_PATH = Path(os.environ.get("TWIDDLE_RATELIMIT_LEDGER",
                                  Path.home() / ".cache" / "twiddle" / "ratelimits.json"))
CONFIG_PATH = Path(os.environ.get("TWIDDLE_RATELIMIT_CONFIG",
                                  Path.home() / ".config" / "twiddle" / "ratelimits.toml"))
FLUSH_S = 5.0           # how often this process's requests reach the ledger
REFRESH_S = 2.0         # how often other processes' requests are re-read
DEFAULT_PENALTY_S = 60.0    # a 429 that named no time


class RateLimited(RuntimeError):
    """Not now: `retry_after` seconds until there is room (or the penalty ends)."""

    def __init__(self, service: str, retry_after: float, why: str = ""):
        self.service, self.retry_after = service, retry_after
        super().__init__(f"{service}: {why or 'over our rate budget'}; "
                         f"room again in {retry_after:.0f}s")


@dataclass(frozen=True)
class Limit:
    count: int
    per_s: float

    @property
    def label(self) -> str:
        p = self.per_s
        unit = ("second" if p == 1 else f"{p:g}s" if p < 60 else "minute" if p == 60
                else f"{p / 60:g}min" if p < 3600 else "hour" if p == 3600
                else f"{p / 3600:g}h" if p < 86400 else "day" if p == 86400 else f"{p:g}s")
        return f"{self.count} per {unit}"


@dataclass(frozen=True)
class Policy:
    service: str
    limits: tuple[Limit, ...]
    source: str                 # "published by X" or "our own budget": never blur the two
    max_block_s: float = 5.0    # the longest `acquire` will sleep before raising instead


POLICIES: dict[str, Policy] = {
    "musicbrainz": Policy("musicbrainz", (Limit(1, 1),),
                          "published by MusicBrainz: 1 request a second"),
    "bandcamp": Policy("bandcamp", (Limit(1, 1), Limit(600, 3600)),
                       "our own budget: a polite guest on an endpoint that isn't ours"),
    "discogs": Policy("discogs", (Limit(25, 60),),
                      "published by Discogs: 25 a minute without a token (60 with one)"),
    "wikipedia": Policy("wikipedia", (Limit(5, 1), Limit(200, 60)),
                        "our own budget; Wikimedia asks for a descriptive User-Agent and <200 a second"),
    "nominatim": Policy("nominatim", (Limit(1, 1), Limit(4, 60)),
                        "published by OpenStreetMap: 1 a second, one thread; we keep to 4 a minute "
                        "because a build is a script on a timer (usage policy, read 2026-10)",
                        max_block_s=20.0),
    "spotify": Policy("spotify", (Limit(10, 10), Limit(100, 60), Limit(1500, 86400)),
                      "our own budget; Spotify publishes no number (it meters a rolling 30s "
                      "per client id, and its penalty can be a day)"),
}


def load_policies(path: Path | None = None) -> dict[str, Policy]:
    """The stock policies, with any `[service] limits = [[n, seconds], ...]` from the config file."""
    out = dict(POLICIES)
    try:
        cfg = tomllib.loads((path or CONFIG_PATH).read_text())
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return out
    for name, table in cfg.items():
        try:
            limits = tuple(Limit(int(n), float(s)) for n, s in table["limits"])
        except (KeyError, TypeError, ValueError):
            continue
        base = out.get(name)
        out[name] = Policy(name, limits, "set in ratelimits.toml",
                           float(table.get("max_block_s", base.max_block_s if base else 5.0)))
    return out


# ---- the ledger -------------------------------------------------------------


class Ledger:
    """Requests and lockouts, shared between processes through one JSON file.

    Each process owns a list of request times under its pid, so nobody counts
    anybody twice; a lockout (`blocked_until`) is per service. Writes merge
    under a file lock. It only ever answers "how many lately" and "blocked?",
    so a lost or unreadable ledger just means starting from empty.
    """

    HORIZON_S = 86400.0

    def __init__(self, path: Path | None = None, wall=time.time, pid: int | None = None):
        self.path = path
        self.wall = wall
        self.pid = str(pid if pid is not None else os.getpid())

    def _file(self) -> Path:
        return self.path or LEDGER_PATH

    def _locked(self):
        p = self._file()
        p.parent.mkdir(parents=True, exist_ok=True)
        f = open(p.with_name(p.name + ".lock"), "w")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def _read(self) -> dict:
        import json
        try:
            return json.loads(self._file().read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _write(self, doc: dict) -> None:
        from . import jsonstore
        jsonstore.write(self._file(), doc)

    def read(self, service: str) -> tuple[float, list[float]]:
        """(blocked_until, every process's request times except ours) for `service`."""
        svc = self._read().get(service) or {}
        others = [t for pid, ts in (svc.get("events") or {}).items() if pid != self.pid
                  for t in ts]
        return float(svc.get("blocked_until") or 0.0), others

    def reserve(self, service: str, mine: "deque[float]", now: float, decide) -> float | None:
        """Check the shared budget and take a request from it in ONE step, under the
        file lock, so two processes cannot both see room for the same slot.

        `decide(blocked_until, others)` says how long to wait (0 = go ahead, which
        records `now` as ours before the lock is released; `inf` = locked out, nothing
        is recorded). Returns that wait, or None when the ledger cannot be used (the
        caller then counts locally)."""
        try:
            lock = self._locked()
        except OSError:
            return None
        try:
            doc = self._read()
            svc = doc.setdefault(service, {})
            events = svc.setdefault("events", {})
            others = [t for pid, ts in events.items() if pid != self.pid for t in ts]
            wait = decide(float(svc.get("blocked_until") or 0.0), others)
            if wait <= 0:
                mine.append(now)
                while mine and now - mine[0] > self.HORIZON_S:
                    mine.popleft()
                events[self.pid] = list(mine)
                self._write(doc)
            return wait
        except OSError:
            return None
        finally:
            lock.close()

    def merge(self, service: str, mine: list[float], blocked_until: float | None = None) -> None:
        """Write this process's request times, and a lockout if there is one."""
        now = self.wall()
        try:
            lock = self._locked()
        except OSError:
            return
        try:
            doc = self._read()
            svc = doc.setdefault(service, {})
            events = svc.setdefault("events", {})
            events[self.pid] = [t for t in mine if now - t < self.HORIZON_S]
            for pid in [p for p, ts in events.items()
                        if p != self.pid and (not ts or now - max(ts) >= self.HORIZON_S)]:
                del events[pid]                      # a process that stopped long ago
            if blocked_until is not None and blocked_until > float(svc.get("blocked_until") or 0):
                svc["blocked_until"] = blocked_until
            elif float(svc.get("blocked_until") or 0) < now:
                svc.pop("blocked_until", None)
            self._write(doc)
        except OSError:
            pass
        finally:
            lock.close()


# ---- the governor -----------------------------------------------------------


class Governor:
    def __init__(self, policy: Policy, ledger: Ledger | None = None, *,
                 wall=time.time, sleep=time.sleep):
        self.policy = policy
        self.ledger = ledger
        self.wall, self.sleep = wall, sleep
        self._lock = threading.Lock()
        self._mine: deque[float] = deque()          # our request times (wall), oldest first
        self._others: list[float] = []
        self._ledger_blocked = 0.0
        self._blocked_until = 0.0
        self._read_at = float("-inf")
        self._flushed_at = float("-inf")
        self._dirty = False

    # -- the ledger side --

    def _refresh(self, now: float) -> None:
        if self.ledger is not None and now - self._read_at >= REFRESH_S:
            self._read_at = now
            try:
                self._ledger_blocked, self._others = self.ledger.read(self.policy.service)
            except Exception:
                pass

    def flush(self, force: bool = False, blocked_until: float | None = None) -> None:
        now = self.wall()
        if self.ledger is None or not (self._dirty or blocked_until is not None):
            return
        if not force and blocked_until is None and now - self._flushed_at < FLUSH_S:
            return
        self._flushed_at, self._dirty = now, False
        try:
            self.ledger.merge(self.policy.service, list(self._mine), blocked_until)
        except Exception:
            pass

    # -- the three questions --

    def blocked_for(self) -> float:
        """Seconds left of a lockout we or another process were handed (0: none)."""
        now = self.wall()
        with self._lock:
            self._refresh(now)
            return max(0.0, max(self._blocked_until, self._ledger_blocked) - now)

    def _wait_needed(self, now: float) -> float:
        """Seconds until every window has room for one more request."""
        events = sorted([*self._mine, *self._others])
        wait = 0.0
        for lim in self.policy.limits:
            recent = [t for t in events if now - t < lim.per_s]
            if len(recent) >= lim.count:
                wait = max(wait, recent[-lim.count] + lim.per_s - now)
        return wait

    def _reserve_shared(self, now: float) -> float | None:
        """Ask the ledger for a slot atomically (see `Ledger.reserve`). Caller holds `_lock`."""
        if self.ledger is None:
            return None

        def decide(blocked: float, others: list[float]) -> float:
            self._ledger_blocked, self._others = blocked, others
            self._read_at = now
            if max(self._blocked_until, blocked) - now > 0:
                return float("inf")             # locked out: take nothing
            return self._wait_needed(now)
        return self.ledger.reserve(self.policy.service, self._mine, now, decide)

    def acquire(self, max_block_s: float | None = None) -> float:
        """Reserve one request. Sleeps (at most `max_block_s`) until the budget has
        room and returns the seconds it slept; raises `RateLimited` instead of sleeping
        longer, or at all while a lockout lasts."""
        cap = self.policy.max_block_s if max_block_s is None else max_block_s
        slept = 0.0
        while True:
            now = self.wall()
            with self._lock:
                wait = self._reserve_shared(now)
                if wait is None:                    # no ledger (or unusable): count locally
                    self._refresh(now)
                    locked = max(self._blocked_until, self._ledger_blocked) - now
                    if locked > 0:
                        raise RateLimited(self.policy.service, locked, "locked out by the service")
                    wait = self._wait_needed(now)
                    if wait <= 0:
                        self._mine.append(now)
                        while self._mine and now - self._mine[0] > Ledger.HORIZON_S:
                            self._mine.popleft()
                        self._dirty = True
                else:
                    locked = max(self._blocked_until, self._ledger_blocked) - now
                    if locked > 0:
                        raise RateLimited(self.policy.service, locked, "locked out by the service")
                if wait <= 0:
                    break
            if slept + wait > cap:
                raise RateLimited(self.policy.service, wait)
            self.sleep(wait)
            slept += wait
        self.flush()
        return slept

    def report(self, status: int | None, retry_after: float = 0.0) -> None:
        """What the service answered. A 429 starts a lockout everyone shares."""
        if status != 429:
            return
        until = self.wall() + (retry_after or DEFAULT_PENALTY_S)
        with self._lock:
            self._blocked_until = max(self._blocked_until, until)
        self.flush(force=True, blocked_until=until)

    def report_response(self, resp) -> None:
        """`report` for a `requests` response (status and Retry-After). Never raises."""
        try:
            status = resp.status_code
            after = float(resp.headers.get("Retry-After", 0)) if status == 429 else 0.0
        except Exception:
            return
        self.report(status, after)

    # -- for display --

    def usage(self) -> list[dict]:
        now = self.wall()
        with self._lock:
            self._refresh(now)
            events = [*self._mine, *self._others]
        return [{"label": lim.label, "max": lim.count, "per_s": lim.per_s,
                 "used": sum(1 for t in events if now - t < lim.per_s)}
                for lim in self.policy.limits]


# ---- the registry -----------------------------------------------------------

_governors: dict[str, Governor] = {}
_registry_lock = threading.Lock()
_policies: dict[str, Policy] | None = None


def reset(flush: bool = True) -> None:
    """Forget every governor and the loaded policies (tests; or after editing the config)."""
    global _policies
    with _registry_lock:
        if flush:
            for g in _governors.values():
                g.flush(force=True)
        _governors.clear()
        _policies = None


def governor(service: str) -> Governor:
    """The one governor for `service` in this process (created on first use). A
    service with no policy gets none: it is unlimited, but still counted."""
    global _policies
    with _registry_lock:
        g = _governors.get(service)
        if g is None:
            if _policies is None:
                _policies = load_policies()
            policy = _policies.get(service) or Policy(service, (), "no limit set")
            g = _governors[service] = Governor(policy, Ledger())
        return g


def status() -> dict[str, dict]:
    """Every known service's budget, what has been used of it, and any lockout."""
    global _policies
    if _policies is None:
        _policies = load_policies()
    return {name: {"source": g.policy.source, "blocked_for_s": round(g.blocked_for()),
                   "limits": g.usage()}
            for name in _policies for g in [governor(name)]}


@atexit.register
def _flush_all() -> None:
    for g in list(_governors.values()):
        g.flush(force=True)
