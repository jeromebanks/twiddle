"""Daemon liveness detection.

The trap this guards: launchd appends stderr across restarts, so a traceback
from a failed first run sits in the file forever. Treating that as "currently
broken" told the user to go change security settings while the monitor was in
fact collecting fine.
"""
import os
import time
from pathlib import Path

import pytest

from twiddle import daemon


def _log(tmp_path: Path, age_s: float = 0.0) -> Path:
    log = tmp_path / "daemon.jsonl"
    log.write_text('{"kind": "sample"}\n')
    if age_s:
        old = time.time() - age_s
        os.utime(log, (old, old))
    return log


def _err(tmp_path: Path, text: str) -> None:
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "daemon.err").write_text(text)


def test_fresh_log_is_alive(tmp_path: Path):
    assert daemon.log_is_fresh(_log(tmp_path), interval=30)


def test_stale_log_is_not_alive(tmp_path: Path):
    assert not daemon.log_is_fresh(_log(tmp_path, age_s=600), interval=30)


def test_missing_log_is_not_alive(tmp_path: Path):
    assert not daemon.log_is_fresh(tmp_path / "nope.jsonl", interval=30)


def test_stale_error_with_live_log_is_not_reported_as_blocked(tmp_path: Path):
    # The exact false positive: run 1 died on permissions, run 2 is healthy.
    _err(tmp_path, "OSError: [Errno 65] No route to host")
    log = _log(tmp_path)
    assert not daemon.blocked_by_local_network(tmp_path, log, interval=30)


def test_stale_error_with_stale_log_is_blocked(tmp_path: Path):
    _err(tmp_path, "OSError: [Errno 65] No route to host")
    log = _log(tmp_path, age_s=600)
    assert daemon.blocked_by_local_network(tmp_path, log, interval=30)


def test_no_error_on_record_is_never_blocked(tmp_path: Path):
    log = _log(tmp_path, age_s=600)
    assert not daemon.blocked_by_local_network(tmp_path, log, interval=30)


def test_plist_pins_the_anchor_so_multicast_is_not_needed(tmp_path: Path):
    plist = daemon.build_plist(tmp_path, 30, tmp_path / "d.jsonl",
                               50_000_000, "192.168.1.2")
    args = plist["ProgramArguments"]
    assert "--anchor" in args and args[args.index("--anchor") + 1] == "192.168.1.2"
    assert "--duration" in args and args[args.index("--duration") + 1] == "0"
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] >= 60


def test_plist_omits_anchor_when_none_known(tmp_path: Path):
    plist = daemon.build_plist(tmp_path, 30, tmp_path / "d.jsonl",
                               50_000_000, "")
    assert "--anchor" not in plist["ProgramArguments"]


# ---- the alarm server's agent -------------------------------------------------

import plistlib
import socket
import subprocess


class FakeLaunchctl:
    def __init__(self, printing: str = "", code: int = 0):
        self.calls, self.printing, self.code = [], printing, code

    def __call__(self, cmd, **_kw):
        self.calls.append(cmd)
        out = self.printing if cmd[1] == "print" else ""
        return subprocess.CompletedProcess(cmd, self.code if cmd[1] == "print" else 0,
                                           stdout=out, stderr="")


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(daemon, "AGENT_DIR", tmp_path / "agents")
    monkeypatch.setattr(daemon.subprocess, "run", fake)
    monkeypatch.setattr(daemon, "_which", lambda cmd: f"/bin/{cmd}")
    return fake


def test_alarm_plist_runs_serve_with_the_room_and_pinned_anchor(tmp_path: Path):
    plist = daemon.build_alarm_plist(
        tmp_path, tmp_path / "sounds", 8765, 600.0, ["Living Room"], "10.0.0.13")
    args = plist["ProgramArguments"]
    assert args[args.index("alarm"):args.index("alarm") + 2] == ["alarm", "serve"]
    assert args[args.index("--dir") + 1] == str(tmp_path / "sounds")
    assert args[args.index("--room") + 1] == "Living Room"
    assert args[args.index("--anchor") + 1] == "10.0.0.13"
    assert plist["Label"] == daemon.ALARM_LABEL != daemon.LABEL
    assert plist["KeepAlive"] is True and plist["ThrottleInterval"] >= 60


def test_install_alarm_writes_its_own_plist_and_never_touches_the_monitors(
        tmp_path: Path, launchd):
    path = daemon.install_alarm(tmp_path, tmp_path, 8765, 600.0, [], "")
    assert path == daemon.plist_path(daemon.ALARM_LABEL)
    assert plistlib.loads(path.read_bytes())["Label"] == daemon.ALARM_LABEL
    assert not daemon.plist_path().exists()
    assert [c[1] for c in launchd.calls] == ["bootout", "bootstrap"]
    assert all(daemon.LABEL not in c[2] for c in launchd.calls if c[1] == "bootout")


def test_uninstall_alarm_removes_only_its_plist(tmp_path: Path, launchd):
    daemon.install_alarm(tmp_path, tmp_path, 8765, 600.0, [], "")
    assert daemon.uninstall(daemon.ALARM_LABEL) is True
    assert not daemon.plist_path(daemon.ALARM_LABEL).exists()
    assert daemon.uninstall(daemon.ALARM_LABEL) is False


def test_status_reads_the_alarm_job(launchd):
    launchd.printing = "\tstate = running\n\tpid = 42\n\tother = x\n"
    assert daemon.status(daemon.ALARM_LABEL) == "state = running; pid = 42"
    assert launchd.calls[-1][-1].endswith(daemon.ALARM_LABEL)
    launchd.code = 113
    assert daemon.status(daemon.ALARM_LABEL) == "not loaded"


def test_serving_is_a_connect_that_answers_or_not():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert daemon.serving(port)
    assert not daemon.serving(port, timeout=0.2)   # closed again


def test_alarm_blocked_needs_the_error_and_a_silent_port(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "alarm_server.err").write_text("OSError: [Errno 65] No route to host")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert daemon.alarm_blocked_by_local_network(tmp_path, port)
    with socket.socket() as s:      # a live server: the old error proves nothing
        s.bind(("127.0.0.1", port))
        s.listen()
        assert not daemon.alarm_blocked_by_local_network(tmp_path, port)
