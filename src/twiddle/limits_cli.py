"""`twiddle limits`: every service's request budget, what has been used of it, any lockout.

Read-only: it reads the shared ledger (`ratelimit.py`) and sends nothing. The
numbers are only as good as their `source`: MusicBrainz's is published; Spotify's
is a budget of ours, because Spotify publishes none.
"""
from __future__ import annotations

from . import ratelimit
from .control_cli import emit
from .scenespec.buildstatus import duration


def cmd_limits(args) -> int:
    st = ratelimit.status()
    lines = []
    for name, b in sorted(st.items()):
        used = " · ".join(f"{l['used']}/{l['max']} per {l['label'].split(' per ')[-1]}"
                          for l in b["limits"]) or "no limit set"
        lock = f"   LOCKED OUT for {duration(b['blocked_for_s'])}" if b["blocked_for_s"] else ""
        lines.append(f"{name:<12} {used}{lock}")
        lines.append(f"{'':<12} {b['source']}")
    return emit(args, st, "\n".join(lines))


def register(sub, parents=None):
    kw = {"parents": parents} if parents else {}
    p = sub.add_parser(**kw, name="limits",
                       help="each service's request budget, what has been used, any lockout "
                            "(read-only)")
    p.set_defaults(func=cmd_limits)
