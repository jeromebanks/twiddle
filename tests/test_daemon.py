"""Daemon liveness detection.

The trap this guards: launchd appends stderr across restarts, so a traceback
from a failed first run sits in the file forever. Treating that as "currently
broken" told the user to go change security settings while the monitor was in
fact collecting fine.
"""
import os
import time
from pathlib import Path

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
