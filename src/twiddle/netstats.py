"""How hard are we hitting each service? Counters, a one-minute rate, and the cap.

Three libraries talk to the network for `scene build` and the rest of twiddle --
`lookup` (MusicBrainz, Wikipedia, Discogs, Bandcamp's search), `bandcamp`
(pages) and `spotify` -- so each reports its requests here, one line at the
place the request is made. A report never raises and never changes what the
caller does; with nobody reading, it costs a lock and an append.

The caps are labelled for what they are. MusicBrainz's 1 request a second is
*its* published limit. Bandcamp's is our own throttle (`bandcamp.MIN_INTERVAL_S`).
Spotify publishes no number (it meters a rolling window per client id), so
the figure is a budget we chose, not Spotify's: when it answers 429 that is
the real signal, and `limited` counts it.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

WINDOW_S = 60.0


@dataclass(frozen=True)
class Cap:
    rpm: int
    source: str         # where the number comes from, so nobody mistakes a budget for a limit


CAPS = {
    "musicbrainz": Cap(60, "published by MusicBrainz: 1 request a second"),
    "bandcamp": Cap(60, "our own throttle: 1 request a second"),
    "spotify": Cap(120, "our own budget; Spotify publishes no number"),
    "nominatim": Cap(4, "published by OpenStreetMap: 1 a second; our own 4 a minute for scripts"),
}

# Services by the host a request goes to, for callers that only have a URL.
HOSTS = {"musicbrainz.org": "musicbrainz", "wikidata.org": "wikipedia",
         "wikipedia.org": "wikipedia", "discogs.com": "discogs",
         "bandcamp.com": "bandcamp", "nominatim.openstreetmap.org": "nominatim", "spotify.com": "spotify"}


def service_for(url: str) -> str:
    host = url.split("/")[2].lower() if "://" in url else url.lower()
    return next((s for h, s in HOSTS.items() if host == h or host.endswith("." + h)), host)


class _Service:
    def __init__(self) -> None:
        self.total = 0
        self.errors = 0             # 4xx/5xx other than "limited", and failures
        self.limited = 0            # 429 and 503: the service saying slow down
        self.waited_s = 0.0         # time spent in our own throttle or backing off
        self.retry_after = 0.0      # what the last 429 asked for
        self.limited_at: float | None = None
        self.recent: deque[float] = deque()


_lock = threading.Lock()
_services: dict[str, _Service] = {}
clock = time.monotonic


def reset() -> None:
    with _lock:
        _services.clear()


def _svc(name: str) -> _Service:
    s = _services.get(name)
    if s is None:
        s = _services[name] = _Service()
    return s


def record(service: str, *, status: int | None = None, retry_after: float = 0.0) -> None:
    """One request to `service` answered with HTTP `status` (None: it failed
    before an answer). Never raises."""
    try:
        t = clock()
        with _lock:
            s = _svc(service)
            s.total += 1
            s.recent.append(t)
            if status in (429, 503):
                s.limited += 1
                s.limited_at = t
                if retry_after:
                    s.retry_after = retry_after
            elif status is None or status >= 400:
                s.errors += 1
    except Exception:
        pass


def record_response(service: str, resp) -> None:
    """`record` for a `requests` response (reads `status_code` and Retry-After)."""
    try:
        status = resp.status_code
        after = float(resp.headers.get("Retry-After", 0)) if status == 429 else 0.0
    except Exception:
        status, after = None, 0.0
    record(service, status=status, retry_after=after)


def record_wait(service: str, seconds: float) -> None:
    """Time `service`'s throttle or a backoff made a caller wait."""
    try:
        if seconds > 0:
            with _lock:
                _svc(service).waited_s += seconds
    except Exception:
        pass


def snapshot() -> dict[str, dict]:
    """Per service: totals, requests in the last minute, the cap and how much of it
    that is, and when it last said slow down."""
    t = clock()
    out = {}
    with _lock:
        for name, s in _services.items():
            while s.recent and t - s.recent[0] > WINDOW_S:
                s.recent.popleft()
            cap = CAPS.get(name)
            rpm = len(s.recent)
            out[name] = {
                "total": s.total, "rpm": rpm, "errors": s.errors, "limited": s.limited,
                "waited_s": round(s.waited_s, 1), "retry_after_s": s.retry_after,
                "limited_ago_s": None if s.limited_at is None else round(t - s.limited_at),
                "cap_rpm": cap.rpm if cap else None, "cap_source": cap.source if cap else None,
                "utilisation": round(rpm / cap.rpm, 2) if cap else None}
    return out
