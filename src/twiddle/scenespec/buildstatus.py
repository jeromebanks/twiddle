"""How a `scene build` is going, for anyone who asks while it runs.

The builder writes a small JSON file next to the dataset it is building (the
same directory as `build.lock`) every few seconds, and `scene status` reads it.
Like the dataset it is a contract between a producer and a client: the client
never imports the builder, it reads this.

    {"state": "running" | "done" | "failed", "pid", "started_at", "updated_at",
     "phase": "sources" | "venues" | "enriching" | "publishing" | "done",
     "done", "total", "bands_per_min", "eta_s", "waiting": {"service", "remaining_s"} | null,
     "services": {name: {total, rpm, cap_rpm, cap_source, utilisation, limited, ...}},
     "gaps": {service: seconds between bands},
     "budgets": {service: {"limits": [{label, used, max}], "blocked_for_s"}}, "message"}

Unknown keys are ignored, so a producer may add its own.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .. import jsonstore
from . import dataset

FILE = "build-status.json"
STALE_S = 120       # a running build that has not reported for this long is probably dead


def path(dataset_path: Path | None = None) -> Path:
    return (dataset_path or dataset.default_path()).with_name(FILE)


def write(status: dict, dataset_path: Path | None = None) -> None:
    jsonstore.write(path(dataset_path), status)


def read(dataset_path: Path | None = None) -> dict | None:
    d = jsonstore.read(path(dataset_path))
    return d or None


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def render(st: dict, now: float | None = None) -> str:
    """The status as a few plain lines."""
    now = time.time() if now is None else now
    state = st.get("state", "?")
    w = st.get("waiting")
    waiting_on_purpose = bool(w and w.get("until", 0) > now)    # a stated Retry-After is being waited out
    if state == "running" and ((now - st.get("updated_at", 0) > STALE_S and not waiting_on_purpose)
                               or not _alive(st.get("pid"))):
        state = "stalled (no report for a while, or the process is gone)"
    lines = [f"build: {state}  ·  phase: {st.get('phase', '?')}"]
    total, done = st.get("total") or 0, st.get("done") or 0
    if total:
        bits = [f"{done}/{total} bands"]
        if st.get("bands_per_min") is not None:
            bits.append(f"{st['bands_per_min']:.1f} bands/min")
        if st.get("state") == "running":
            bits.append(f"eta {duration(st.get('eta_s'))}")
        lines.append("  " + " · ".join(bits))
    if w:
        left = w["until"] - now if w.get("until") else w.get("remaining_s")
        lines.append(f"  waiting on {w['service']}: {duration(max(0, left))} left "
                     "(it asked us to slow down)")
    for name, s in sorted((st.get("services") or {}).items()):
        cap = f"{s['rpm']}/{s['cap_rpm']} per min" if s.get("cap_rpm") else f"{s['rpm']} per min"
        extra = []
        if s.get("limited"):
            extra.append(f"{s['limited']} told us to slow down")
        if s.get("errors"):
            extra.append(f"{s['errors']} errors")
        if s.get("waited_s"):
            extra.append(f"{duration(s['waited_s'])} waiting")
        lines.append(f"  {name:<12} {s['total']:>6} requests · {cap}"
                     + (" · " + ", ".join(extra) if extra else ""))
    for name, b in sorted((st.get("budgets") or {}).items()):
        used = " · ".join(f"{l['used']}/{l['max']} per {l['label'].split(' per ')[-1]}"
                          for l in b.get("limits") or [])
        lock = f" · LOCKED OUT {duration(b['blocked_for_s'])} more" if b.get("blocked_for_s") else ""
        lines.append(f"  {name} budget: {used}{lock}")
    for name, gap in sorted((st.get("gaps") or {}).items()):
        lines.append(f"  pace: {name} one band every {gap:.1f}s")
    if st.get("message"):
        lines.append("  " + st["message"])
    return "\n".join(lines)
