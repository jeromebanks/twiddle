"""Keep a relay running when no terminal is attached.

`relay start` is a foreground command you watch. That is the right shape for
a test and the wrong shape for "an agent plays a song": by the time the agent
has something to play, there has to already be a device for Spotify to play
*to*. So the relay needs to outlive the command that started it.

Two things here are deliberate and were arrived at by measurement rather than
taste:

**The child is `.venv/bin/twiddle`, never `uv run twiddle`.** `uv run` does
not forward SIGTERM; it exits and takes the child down before the child's
cleanup can run. That was measured -- it left the Roams pointed at a URL that
no longer answered. Exec'ing the venv's console script directly means a
`relay down` reaches the process that knows how to tidy up.

**The restore stays in the child, and fires on any exit.** An earlier draft
of this docstring promised the opposite -- snapshot at `up`, restore at
`down` -- on the theory that a long-lived relay should not be holding a
restore it might perform at an arbitrary moment. Implementing it that way
turned out to be worse: if a supervised relay dies on its own, the room is
left pointed at a URL that no longer answers, and nothing is watching to
notice. Restoring at an arbitrary moment beats silence on a dead URL.

So `down` sends SIGTERM and the child's own `finally` does the work, which
is verified end to end: `relay down` on the real Roam pair put it back to
KALX at volume 35. That is also why `down` never escalates to SIGKILL --
killing the child is precisely what skips the restore.
"""
from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

STATE_DIR = Path(os.environ.get("TWIDDLE_RUN_DIR", "logs"))
PID_FILE = STATE_DIR / "relay.pid"
OUT_FILE = STATE_DIR / "relay.out"

# How long to wait for a stopped relay to finish restoring the room. The
# restore is several SOAP round-trips, so this is generous on purpose.
STOP_TIMEOUT = 20.0


def console_script() -> str:
    """The venv's own `twiddle`, so the child is not wrapped by `uv run`.

    Derived from the running interpreter rather than looked up on PATH: an
    agent may invoke this through `uv run`, whose PATH does not necessarily
    contain the console script even though the venv beside it does.
    """
    candidate = Path(sys.executable).parent / "twiddle"
    if candidate.exists():
        return str(candidate)
    from shutil import which
    found = which("twiddle")
    if not found:
        raise RuntimeError("cannot locate the twiddle console script")
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _cmdline(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


@dataclass
class RelayState:
    running: bool
    pid: int = 0
    detail: str = ""
    record: dict | None = None

    def to_dict(self) -> dict:
        d = {"running": self.running, "pid": self.pid, "detail": self.detail}
        if self.record:
            d |= {k: v for k, v in self.record.items() if k != "pid"}
        return d


def status(pid_file: Path = PID_FILE) -> RelayState:
    """Is a supervised relay up? Never trusts the PID file on its own.

    A PID left behind by a crash can be recycled by an unrelated process, and
    an agent that called `relay up` blindly would then be told a relay is
    running when nothing is serving audio. So the command line is checked too.
    """
    if not pid_file.exists():
        return RelayState(False, detail="no pid file")
    try:
        record = json.loads(pid_file.read_text())
        pid = int(record["pid"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        return RelayState(False, detail=f"unreadable pid file: {exc}")
    if not _alive(pid):
        return RelayState(False, pid=pid, detail="pid is not running", record=record)
    cmd = _cmdline(pid)
    if "twiddle" not in cmd or "relay" not in cmd:
        return RelayState(False, pid=pid, record=record,
                          detail=f"pid {pid} was recycled by something else")
    return RelayState(True, pid=pid, detail=cmd, record=record)


def up(args_for_relay: list[str], pid_file: Path = PID_FILE,
       out_file: Path = OUT_FILE, wait: float = 20.0) -> RelayState:
    """Start a detached relay, or report the one already running.

    Idempotent on purpose: an agent will call this before every play, and an
    already-running relay is a success, not an error.
    """
    existing = status(pid_file)
    if existing.running:
        return existing

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    out = out_file.open("a")
    out.write(f"\n=== relay up {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ===\n")
    out.flush()
    argv = [console_script(), "relay", *args_for_relay]
    # The relay writes `logs/relay.jsonl` and its snapshot relative to its
    # working directory, and `radio.zsh`/`scene` start it from wherever the
    # shell is -- so pin it to the repo, where `analyse` and the experiment's
    # evidence live. (The same drift sent real journal writes elsewhere.)
    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.Popen(
        argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        cwd=repo if (repo / "pyproject.toml").exists() else None,
        # Its own session, so it is not killed with the terminal or the agent
        # that spawned it -- the whole point of supervising it.
        start_new_session=True)
    record = {"pid": proc.pid, "argv": argv,
              "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    pid_file.write_text(json.dumps(record, indent=2))

    # Wait for it to be serving rather than merely spawned, so a caller that
    # gets `running: true` can immediately depend on the stream existing.
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            pid_file.unlink(missing_ok=True)
            tail = _tail(out_file)
            return RelayState(False, detail=f"relay exited at once: {tail}")
        state = status(pid_file)
        if state.running and _serving(out_file, record["started"]):
            return state
        time.sleep(0.5)
    return status(pid_file)


def _serving(out_file: Path, _since: str) -> bool:
    try:
        return "Relay up:" in out_file.read_text()[-8000:]
    except OSError:
        return False


def _tail(path: Path, lines: int = 8) -> str:
    try:
        return " | ".join(path.read_text().splitlines()[-lines:])
    except OSError:
        return "(no output)"


def down(pid_file: Path = PID_FILE, timeout: float = STOP_TIMEOUT) -> RelayState:
    """Stop the relay and give it time to put the room back.

    SIGTERM, not SIGKILL: the child's handler is what restores the speaker,
    and killing it outright is precisely the failure this module exists to
    avoid.
    """
    state = status(pid_file)
    if not state.running:
        pid_file.unlink(missing_ok=True)
        return RelayState(False, detail=state.detail or "not running")
    os.kill(state.pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(state.pid):
            pid_file.unlink(missing_ok=True)
            return RelayState(False, pid=state.pid, detail="stopped cleanly")
        time.sleep(0.3)
    # It ignored SIGTERM. Say so rather than escalating silently: a SIGKILL
    # here would skip the restore, and the caller should know the room may
    # still be pointed at the relay.
    return RelayState(True, pid=state.pid,
                      detail=f"still running {timeout:.0f}s after SIGTERM; "
                             "the room may not have been restored")
