#!/usr/bin/env python3
"""Append-only observation log, so new findings do not require editing docs.

Prose docs go stale and need rewriting; an append-only log does not. Record
what was observed -- especially anything a human *heard*, which is the scarce
signal here -- and let analysis read it back.

    uv run python tools/observe.py add --heard "right speaker dropped" --speaker R
    uv run python tools/observe.py add --note "router RSSI: Roam L -74dBm" --kind measurement
    uv run python tools/observe.py list
    uv run python tools/observe.py list --kind heard
"""
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "logs" / "observations.jsonl"

def add(a):
    rec = {"ts": a.at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "kind": "heard" if a.heard else a.kind,
           "text": a.heard or a.note,
           "speaker": a.speaker, "source": a.source}
    if a.duration_s is not None: rec["duration_s"] = a.duration_s
    rec = {k: v for k, v in rec.items() if v is not None}
    if not rec.get("text"):
        sys.exit("nothing to record: pass --heard or --note")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh: fh.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))

def show(a):
    if not LOG.exists(): return print(f"no observations yet ({LOG})")
    for line in LOG.read_text().splitlines():
        try: d = json.loads(line)
        except Exception: continue
        if a.kind and d.get("kind") != a.kind: continue
        sp = f" [{d['speaker']}]" if d.get("speaker") else ""
        dur = f" ({d['duration_s']}s)" if d.get("duration_s") else ""
        print(f"{d['ts']}  {d.get('kind','?'):11}{sp}{dur}  {d.get('text','')}")

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
sub = p.add_subparsers(required=True)
s = sub.add_parser("add", help="append an observation")
s.add_argument("--heard", help="what a human heard (the scarce, highest-value signal)")
s.add_argument("--note", help="any other observation")
s.add_argument("--kind", default="note", choices=["note", "measurement", "change", "heard"])
s.add_argument("--speaker", help="L, R, Beam, ...")
s.add_argument("--duration-s", type=float, default=None)
s.add_argument("--source", default="me")
s.add_argument("--at", help="ISO timestamp; defaults to now")
s.set_defaults(func=add)
s = sub.add_parser("list", help="read them back")
s.add_argument("--kind")
s.set_defaults(func=show)
a = p.parse_args(); a.func(a)
