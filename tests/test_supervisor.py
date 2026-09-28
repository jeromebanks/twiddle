"""Supervising a relay that has to outlive the command that started it.

The two properties here both come from things that actually went wrong: a
`uv run` wrapper that swallowed SIGTERM and left the speakers on a dead URL,
and the general hazard of trusting a PID file written by a process that may
have crashed.
"""
import json
import os

from twiddle import supervisor


def test_status_is_false_with_no_pid_file(tmp_path):
    assert not supervisor.status(tmp_path / "none.pid").running


def test_status_rejects_a_pid_that_is_not_running(tmp_path):
    pid_file = tmp_path / "relay.pid"
    # PID 1 exists; a very high one almost certainly does not.
    pid_file.write_text(json.dumps({"pid": 999_999}))
    state = supervisor.status(pid_file)
    assert not state.running
    assert "not running" in state.detail


def test_status_rejects_a_recycled_pid(tmp_path):
    """A stale PID reused by something else must not read as a live relay.

    Otherwise an agent that called `relay up` blindly would be told a relay
    is running while nothing at all is serving audio.
    """
    pid_file = tmp_path / "relay.pid"
    # Our own PID is alive but is plainly not a relay.
    pid_file.write_text(json.dumps({"pid": os.getpid()}))
    state = supervisor.status(pid_file)
    assert not state.running
    assert "recycled" in state.detail


def test_status_survives_a_corrupt_pid_file(tmp_path):
    pid_file = tmp_path / "relay.pid"
    pid_file.write_text("not json at all")
    state = supervisor.status(pid_file)
    assert not state.running
    assert "unreadable" in state.detail


def test_up_is_idempotent_when_one_is_already_running(tmp_path, monkeypatch):
    """Agents call this before every play; already-up is success, not error."""
    pid_file = tmp_path / "relay.pid"
    pid_file.write_text(json.dumps({"pid": os.getpid()}))
    monkeypatch.setattr(supervisor, "_cmdline",
                        lambda pid: "twiddle relay start --room roam")
    spawned = []
    monkeypatch.setattr(supervisor.subprocess, "Popen",
                        lambda *a, **kw: spawned.append(a) or None)
    state = supervisor.up(["start"], pid_file=pid_file)
    assert state.running
    assert not spawned, "spawned a second relay over a running one"


def test_down_reports_honestly_when_sigterm_is_ignored(tmp_path, monkeypatch):
    """Escalating to SIGKILL would skip the restore, so it does not.

    The caller has to know the room may still be pointed at the relay --
    silently killing it and reporting success is how speakers get left on a
    dead URL.
    """
    pid_file = tmp_path / "relay.pid"
    pid_file.write_text(json.dumps({"pid": os.getpid()}))
    monkeypatch.setattr(supervisor, "_cmdline",
                        lambda pid: "twiddle relay start")
    monkeypatch.setattr(supervisor.os, "kill", lambda *a: None)
    monkeypatch.setattr(supervisor, "_alive", lambda pid: True)
    state = supervisor.down(pid_file, timeout=0.5)
    assert state.running
    assert "may not have been restored" in state.detail
    assert pid_file.exists(), "the pid file was dropped for a live process"


def test_down_clears_the_pid_file_once_it_is_gone(tmp_path, monkeypatch):
    pid_file = tmp_path / "relay.pid"
    pid_file.write_text(json.dumps({"pid": os.getpid()}))
    monkeypatch.setattr(supervisor, "_cmdline", lambda pid: "twiddle relay start")
    monkeypatch.setattr(supervisor.os, "kill", lambda *a: None)
    alive = iter([True, False, False])
    monkeypatch.setattr(supervisor, "_alive", lambda pid: next(alive, False))
    state = supervisor.down(pid_file, timeout=5)
    assert not state.running
    assert not pid_file.exists()


def test_the_child_is_the_venv_script_not_uv_run():
    """`uv run` does not forward SIGTERM; that left the Roams on a dead URL.

    This is the fix for a measured failure, so it is asserted rather than
    left as a comment.
    """
    script = supervisor.console_script()
    assert script.endswith("twiddle")
    assert "uv" not in os.path.basename(script)
