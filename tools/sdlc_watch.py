#!/usr/bin/env python3
"""The SDLC watcher's local record, its config, pause/resume and status (#263 M1).

The watcher (a launchd agent, later in M1) minds the issues a person acts on and runs
each one's `next:` step headless. This module is its ground floor: what it remembers,
where, and the commands a person uses to hold it back.

    uv run python tools/sdlc_watch.py status            # read-only: the record as it stands
    uv run python tools/sdlc_watch.py pause [N]         # all issues, or one
    uv run python tools/sdlc_watch.py resume [N]

**One state root for every checkout.** The record and the log live in the *primary*
checkout's `logs/sdlc_watch/`, found from `git rev-parse --git-common-dir` run where this
file is, so the pinned checkout the watcher runs from, the primary and a `.worktrees/*`
one all read and write the same files. `logs/` is gitignored.

- `logs/sdlc_watch/state.json`: the record, rewritten atomically under a lock: the
  start time and the pin, the pauses (all, or per issue), the issues in care, per issue
  the last command, the `state` fingerprint it ran on and its no-progress count, the
  waits (a Max limit) and parked steps (a Codex limit), and the current and last run.
- `logs/sdlc_watch/sdlc_watch.jsonl`: append-only, one line for every command (`status`
  included) and, later, every poll and run.

**The pin.** At first start the record takes the start time and the running checkout's
HEAD. It is refused unless that checkout is clean (untracked files count) and HEAD is
reachable from `origin/main` or an `origin/epic/*` ref, read locally with no fetch: a
run's permissions (T1.4's tool list) come from that pushed commit, never a working tree.

**A lost record starts empty.** No care set and a fresh start time: only a person's
action after that time brings an issue into care, so nothing runs until someone acts
again. (Rebuilding from GitHub is M2's.)

`pause` and `resume` write only the record and the log, and refuse inside a run the
watcher started (`TWIDDLE_SDLC_WATCH_RUN` set, even empty): a headless run can't pause
or unpause the watcher. They work from a dirty checkout and never take the pin.

Stdlib only, and no `twiddle` import, like the rest of the SDLC tooling.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent              # where this copy of the code runs: pinned, primary or a worktree
CONFIG_PATH = Path(".sdlc") / "config.json"         # relative to the running checkout
WATCH_RUN_ENV = "TWIDDLE_SDLC_WATCH_RUN"            # = tools/sdlc.py's WATCH_RUN_ENV (a test holds them together)
STATE_DIR = Path("logs") / "sdlc_watch"             # under the primary checkout
RECORD_NAME = "state.json"
LOG_NAME = "sdlc_watch.jsonl"
LOCK_NAME = "state.lock"
SCHEMA = 1
GIT_TIMEOUT_S = 30
LOCK_WAIT_S = 10                                    # the record's lock: a read-modify-write takes milliseconds
CREDITS_REMINDER = ("usage credits can't be checked: keep them off, or their spend limit at $0 "
                    "(claude.ai/settings/usage)")

# The `watch` section of .sdlc/config.json, and its defaults. Every key is a count of
# seconds or runs, positive and finite; any other key is refused, so no budget (a run
# cap, a turn cap, a dollar figure) can be added by config. tools_file is a repo path.
DEFAULT_WATCH: dict[str, Any] = {
    "interval_s": 60,              # how often a poll checks GitHub
    "hang_timeout_s": 14400,       # a run still going after this has wedged: stop it (4 hours)
    "kill_grace_s": 30,            # SIGTERM to SIGKILL
    "max_failures": 2,             # runs on one issue with no progress, then escalate
    "limit_backoff_s": 3600,       # a Max limit with no reset time given
    "codex_backoff_s": 3600,       # a Codex limit with no reset time given
    "tools_file": ".sdlc/watch-tools.json",
}
INT_KEYS = ("interval_s", "hang_timeout_s", "kill_grace_s", "max_failures", "limit_backoff_s", "codex_backoff_s")
MAX_INT = 7 * 24 * 3600            # a week: anything longer is a typo, not a setting


class WatchError(Exception):
    """A refusal or a failure, reported on stderr; nothing is written past it."""


# --- time -------------------------------------------------------------------------------

def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


# --- git --------------------------------------------------------------------------------

def git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise WatchError(f"git {' '.join(args[:2])} timed out after {GIT_TIMEOUT_S}s") from None
    except OSError as exc:
        raise WatchError(f"cannot run git: {exc}") from None
    if check and proc.returncode != 0:
        raise WatchError(f"git {' '.join(args[:2])} failed in {cwd}: {proc.stderr.strip()}")
    return proc


def checkout_root(where: Path | None = None) -> Path:
    """The top of the checkout `where` is in (the one this code runs from, by default)."""
    return Path(git(["rev-parse", "--show-toplevel"], where or HERE).stdout.strip())


def primary_root(where: Path | None = None) -> Path:
    """The primary checkout, from any worktree of it: the parent of the shared git directory."""
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], where or HERE).stdout.strip()
    return Path(common).parent


def state_root(where: Path | None = None) -> Path:
    """The one absolute directory every checkout's watcher record and log live in."""
    return primary_root(where) / STATE_DIR


# --- config -----------------------------------------------------------------------------

def watch_config(config: dict[str, Any]) -> dict[str, Any]:
    """The `watch` section over its defaults, validated: refuses an unknown key or a bad value."""
    section = config.get("watch", {})
    if not isinstance(section, dict):
        raise WatchError("config: `watch` must be an object")
    unknown = sorted(set(section) - set(DEFAULT_WATCH))
    if unknown:
        raise WatchError(f"config: unknown `watch` key(s) {', '.join(unknown)} "
                         f"(the watcher has no budgets; known: {', '.join(DEFAULT_WATCH)})")
    merged = {**DEFAULT_WATCH, **section}
    for key in INT_KEYS:
        v = merged[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
                or v != int(v) or not 0 < v <= MAX_INT:
            raise WatchError(f"config: watch.{key} must be a whole number from 1 to {MAX_INT}, not {v!r}")
        merged[key] = int(v)
    tools = merged["tools_file"]
    if not isinstance(tools, str) or not tools or Path(tools).is_absolute() or ".." in Path(tools).parts:
        raise WatchError(f"config: watch.tools_file must be a path inside the repo, not {tools!r}")
    return merged


def load_watch_config(where: Path | None = None) -> dict[str, Any]:
    path = checkout_root(where) / CONFIG_PATH
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise WatchError(f"cannot read config {path}: {exc}") from None
    return watch_config(config)


# --- the record -------------------------------------------------------------------------

def empty_record() -> dict[str, Any]:
    """No start, no pin, nothing in care: a record that runs nothing."""
    return {"schema": SCHEMA, "started_at": None, "pin": None,
            "paused": {"all": None, "issues": {}},      # a pause is {"at": ...}; None means not paused
            "care": {},                                  # "N": {"since": ..., "why": ...}
            "issues": {},                                # "N": {"last_command", "fingerprint", "no_progress", "at"}
            "waits": {"max": None, "parked": {}},        # a Max limit; "N": a step parked on a Codex limit
            "run": {"current": None, "last": None}}


def read_record(root: Path) -> dict[str, Any] | None:
    """The record, or None when there is none. A record that can't be read is an error, never
    silently replaced: a person decides whether to move it aside."""
    path = root / RECORD_NAME
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WatchError(f"cannot read {path}: {exc}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise WatchError(f"{path} is not valid JSON ({exc}); move it aside to start afresh") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise WatchError(f"{path} is not a schema-{SCHEMA} watcher record; move it aside to start afresh")
    record = empty_record()
    for key, value in data.items():
        if key in ("paused", "waits", "run") and isinstance(value, dict):
            record[key] = {**record[key], **value}
        else:
            record[key] = value
    return record


def write_record(root: Path, record: dict[str, Any]) -> None:
    """Write-then-rename in the same directory, so a reader sees the old record or the new, never half."""
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, root / RECORD_NAME)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked(root: Path) -> Iterator[None]:
    """One read-modify-write of the record at a time, across processes. Held for milliseconds;
    a holder that keeps it past LOCK_WAIT_S is reported, never waited on for ever."""
    root.mkdir(parents=True, exist_ok=True)
    with open(root / LOCK_NAME, "a") as fh:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WatchError(f"{root / LOCK_NAME} has been held for over {LOCK_WAIT_S}s; "
                                     "nothing was written") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def log(root: Path, event: str, **fields: Any) -> None:
    """Append one line to the watcher's log."""
    root.mkdir(parents=True, exist_ok=True)
    line = {"ts": iso(now()), "event": event, "pid": os.getpid(), **fields}
    with open(root / LOG_NAME, "a") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")


# --- the pin and the first start --------------------------------------------------------

def pin_of(checkout: Path) -> dict[str, Any]:
    """The checkout's HEAD, if it may be the pin: clean, and reachable from a pushed ref."""
    dirty = git(["status", "--porcelain", "--untracked-files=all"], checkout).stdout
    if dirty.strip():
        raise WatchError(f"{checkout} is not clean (commit or remove {len(dirty.splitlines())} change(s)): "
                         "the watcher runs only from a pushed commit")
    sha = git(["rev-parse", "HEAD"], checkout).stdout.strip()
    refs = git(["for-each-ref", "--format=%(refname)", "refs/remotes/origin/main", "refs/remotes/origin/epic/"],
               checkout).stdout.split()
    refs.sort(key=lambda r: (r != "refs/remotes/origin/main", r))
    for ref in refs:
        if git(["merge-base", "--is-ancestor", sha, ref], checkout, check=False).returncode == 0:
            return {"sha": sha, "ref": ref.removeprefix("refs/remotes/"), "checkout": str(checkout)}
    raise WatchError(f"HEAD {sha[:12]} of {checkout} is not on origin/main or any origin/epic/* "
                     "(as last fetched): push it first")


def start(where: Path | None = None, at: dt.datetime | None = None) -> dict[str, Any]:
    """The watcher's first start: stamp the start time and the pin, once.

    A record that has both is returned unchanged. A missing one (never made, or lost) starts
    empty: nothing in care, and only a person's action after this start time counts.
    The pin is validated before anything is written, so a refused start writes no record (only its log line)."""
    root = state_root(where)
    with locked(root):
        record = read_record(root)
        if record is not None and record.get("started_at") and record.get("pin"):
            return record
        try:
            pin = pin_of(checkout_root(where))
        except WatchError as exc:
            log(root, "start", outcome="refused", detail=str(exc))
            raise
        fresh = record is None
        record = record or empty_record()
        record["started_at"] = iso(at or now())
        record["pin"] = {**pin, "at": record["started_at"]}
        write_record(root, record)
    log(root, "start", outcome="ok", pin=pin["sha"], ref=pin["ref"], fresh=fresh)
    return record


def counts_as_new(record: dict[str, Any] | None, acted_at: dt.datetime) -> bool:
    """Whether a person's action at `acted_at` can bring an issue into care: only after the start."""
    started = parse_iso((record or {}).get("started_at"))
    return started is not None and acted_at > started


# --- pause / resume ---------------------------------------------------------------------

def refuse_in_watch_run() -> None:
    if WATCH_RUN_ENV in os.environ:
        raise WatchError(f"refused: inside a run the watcher started ({WATCH_RUN_ENV} is set). "
                         "Only a person pauses or resumes the watcher.")


def set_pause(root: Path, issue: int | None, paused: bool) -> str:
    """Pause or resume all issues (issue None) or one; return what changed, in words."""
    with locked(root):
        record = read_record(root) or empty_record()
        pauses = record["paused"]
        if issue is None:
            was = pauses.get("all") is not None
            pauses["all"] = {"at": iso(now())} if paused else None
            what = "all issues"
        else:
            key = str(issue)
            was = key in pauses["issues"]
            if paused:
                pauses["issues"][key] = {"at": iso(now())}
            else:
                pauses["issues"].pop(key, None)
            what = f"#{issue}"
        write_record(root, record)
    if paused:
        return f"paused {what}" + (" (already paused)" if was else "")
    return f"resumed {what}" + ("" if was else " (was not paused)")


def is_paused(record: dict[str, Any] | None, issue: int) -> bool:
    """Whether the next poll must start nothing for `issue` (T1.6 holds the poll to this)."""
    pauses = (record or {}).get("paused") or {}
    return pauses.get("all") is not None or str(issue) in (pauses.get("issues") or {})


# --- status -----------------------------------------------------------------------------

def _clock(s: str | None) -> str:
    t = parse_iso(s)
    return t.astimezone().strftime("%Y-%m-%d %H:%M") if t else "?"


def render_status(record: dict[str, Any] | None, root: Path) -> str:
    """The PRD's *Screens*, with what exists so far. Read-only."""
    if record is None:
        record, head = empty_record(), "no record yet (never started, or the record was lost: starts empty)"
    elif not record.get("started_at"):
        head = "not started"
    else:
        pin = record.get("pin") or {}
        head = f"started {_clock(record['started_at'])}, pinned at {str(pin.get('sha', '?'))[:12]} " \
               f"({pin.get('ref', '?')})"
    pauses = record["paused"]
    if pauses.get("all"):
        head += f"; PAUSED (all issues) since {_clock(pauses['all'].get('at'))}"
    lines = [f"SDLC watcher: {head}",
             f"  billing:  {CREDITS_REMINDER}"]

    run = record["run"]
    cur, last = run.get("current"), run.get("last")
    lines.append("  running:  " + (f"#{cur.get('issue')} {cur.get('command')}, started {_clock(cur.get('start'))}"
                                   if cur else "nothing"))
    lines.append("  last run: " + (f"#{last.get('issue')} {last.get('command')}, {last.get('outcome', '?')}, "
                                   f"ended {_clock(last.get('end'))}" if last else "none"))

    care = record["care"]
    paused_issues = pauses.get("issues") or {}
    if care:
        rows = []
        for n in sorted(care, key=int):
            tag = f", paused by you since {_clock(paused_issues[n].get('at'))}" if n in paused_issues else ""
            rows.append(f"#{n} since {_clock(care[n].get('since'))}{tag}")
        lines.append("  in care:  " + "\n            ".join(rows))
    else:
        lines.append("  in care:  none (an issue joins when a person acts on it after the start)")
    outside = sorted((n for n in paused_issues if n not in care), key=int)
    if outside:
        lines.append("  paused:   " + ", ".join(f"#{n} (not in care)" for n in outside))

    mx = record["waits"].get("max")
    lines.append("  Max:      " + (f"waiting on the {mx.get('limit', '?')} limit until {_clock(mx.get('until'))}"
                                   if mx else "not limited"))
    parked = record["waits"].get("parked") or {}
    lines.append("  Codex:    " + ("; ".join(f"#{n} {p.get('step', 'step')} parked, retry {_clock(p.get('until'))}"
                                             for n, p in sorted(parked.items(), key=lambda kv: int(kv[0])))
                                   or "nothing parked"))
    failing = {n: i.get("no_progress", 0) for n, i in record["issues"].items() if i.get("no_progress")}
    lines.append("  failures: " + (", ".join(f"#{n}: {k} run(s) with no progress"
                                             for n, k in sorted(failing.items(), key=lambda kv: int(kv[0])))
                                   or "none"))
    lines.append(f"  record:   {root / RECORD_NAME}")
    lines.append(f"  log:      {root / LOG_NAME}")
    return "\n".join(lines)


# --- command line -----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sdlc_watch.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="read-only: paused, in care, runs, waits")
    for name, words in (("pause", "start no new run"), ("resume", "lift a pause")):
        p = sub.add_parser(name, help=f"{words}: all issues, or one")
        p.add_argument("issue", nargs="?", type=int, help="one issue (default: all)")
    args = parser.parse_args(argv)
    issue = getattr(args, "issue", None)
    if issue is not None and issue <= 0:
        parser.error("an issue number is positive")

    try:
        root = state_root()
    except WatchError as exc:
        print(f"sdlc_watch: {exc}", file=sys.stderr)
        return 1
    try:
        if args.cmd == "status":
            record = read_record(root)
            log(root, "command", command="status", outcome="ok")
            print(render_status(record, root))
            return 0
        try:
            refuse_in_watch_run()
        except WatchError as exc:
            log(root, "command", command=args.cmd, issue=issue, outcome="refused", detail=str(exc))
            raise
        said = set_pause(root, issue, args.cmd == "pause")
        log(root, "command", command=args.cmd, issue=issue, outcome="ok", detail=said)
        print(said)
        print()
        print(render_status(read_record(root), root))
        return 0
    except WatchError as exc:
        print(f"sdlc_watch: {exc}", file=sys.stderr)
        refused = str(exc).startswith("refused")      # already logged, as `refused`, where it was raised
        if not refused:
            with contextlib.suppress(OSError):
                log(root, "command", command=args.cmd, issue=issue, outcome="error", detail=str(exc))
        return 2 if refused else 1


if __name__ == "__main__":
    sys.exit(main())
