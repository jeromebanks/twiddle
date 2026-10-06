"""Install the monitor as a launchd agent, so it watches while you are not.

The dropouts are intermittent and unpredictable, which makes an attended
session the wrong tool: by the time you notice, the useful context is gone.
A daemon that is already running when it happens costs nothing and captures
the event with its surroundings.

The agent runs `twiddle watch` with no time limit, rotates its log, and is
restarted by launchd if it ever exits.
"""
from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "local.twiddle.monitor"
ALARM_LABEL = "local.twiddle.alarmserver"
AGENT_DIR = Path.home() / "Library" / "LaunchAgents"


def plist_path(label: str = LABEL) -> Path:
    return AGENT_DIR / f"{label}.plist"


def build_plist(project: Path, interval: float, log: Path,
                max_bytes: int, anchor: str) -> dict:
    uv = _which("uv")
    args = [
        uv, "run", "--project", str(project), "twiddle", "watch",
        "--duration", "0",
        "--interval", str(interval),
        "--log", str(log),
        "--max-bytes", str(max_bytes),
        "--quiet",
    ]
    # launchd gives no Local Network grant, so SSDP multicast fails here even
    # though unicast HTTP to a known speaker works. Pin the anchor.
    if anchor:
        args += ["--anchor", anchor]
    return {
        "Label": LABEL,
        # `uv run` resolves the project's own venv, so the agent does not
        # depend on whatever python happens to be on PATH at boot.
        "ProgramArguments": args,
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": True,
        # Do not hammer if it fails at boot before the network is up.
        "ThrottleInterval": 60,
        "StandardOutPath": str(project / "logs" / "daemon.out"),
        "StandardErrorPath": str(project / "logs" / "daemon.err"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
    }


def _which(cmd: str) -> str:
    from shutil import which
    found = which(cmd)
    if not found:
        raise SystemExit(f"{cmd} not found on PATH; cannot build a launchd job")
    return found


def pick_anchor() -> str:
    """A mains-powered speaker to ask about the household.

    Portables sleep and move, so they make poor anchors; prefer anything
    else, and fall back to whatever answers.
    """
    from .devices import discover, load_device
    portable = {"S27", "S17", "S33"}
    ips = discover()
    fallback = ""
    for ip in ips:
        try:
            dev = load_device(ip)
        except Exception:
            continue
        fallback = fallback or ip
        if dev.model_number not in portable:
            return ip
    return fallback


def install(project: Path, interval: float, log: Path, max_bytes: int,
            anchor: str = "") -> Path:
    anchor = anchor or pick_anchor()
    return _install(build_plist(project, interval, log, max_bytes, anchor),
                    LABEL, project)


def _install(plist: dict, label: str, project: Path) -> Path:
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    (project / "logs").mkdir(parents=True, exist_ok=True)
    path = plist_path(label)
    path.write_bytes(plistlib.dumps(plist))
    # bootout first so a re-install picks up the new plist.
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{label}"],
                   capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{_uid()}", str(path)],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    return path


def build_alarm_plist(project: Path, audio_dir: Path, port: int, max_s: float,
                      rooms: list[str], anchor: str, host: str = "0.0.0.0") -> dict:
    """The agent that keeps `twiddle alarm serve` up, so a Mac-hosted alarm
    (Bandcamp, a sound, a file) has something to answer when it fires hours
    after anyone had a terminal open."""
    args = [
        _which("uv"), "run", "--project", str(project), "twiddle", "alarm", "serve",
        "--dir", str(audio_dir), "--port", str(port), "--max-s", str(max_s),
        "--host", host,
    ]
    for room in rooms:
        args += ["--room", room]
    # No Local Network grant means no SSDP multicast: pin the speaker to ask.
    if anchor:
        args += ["--anchor", anchor]
    return {
        "Label": ALARM_LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "StandardOutPath": str(project / "logs" / "alarm_server.out"),
        "StandardErrorPath": str(project / "logs" / "alarm_server.err"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
    }


def install_alarm(project: Path, audio_dir: Path, port: int, max_s: float,
                  rooms: list[str], anchor: str = "", host: str = "0.0.0.0") -> Path:
    return _install(build_alarm_plist(project, audio_dir, port, max_s, rooms, anchor, host),
                    ALARM_LABEL, project)


def serving(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Is something accepting connections on the server's port? A local
    connect that sends nothing, so it opens no span and touches no speaker."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_host(bind: str) -> str:
    """Where to knock for a server bound to `bind`: a wildcard answers on loopback."""
    return "127.0.0.1" if bind in ("", "0.0.0.0", "::") else bind


def alarm_blocked_by_local_network(project: Path, port: int,
                                   host: str = "127.0.0.1") -> bool:
    """As `blocked_by_local_network`, for the server: a denied-access error on
    record *and* nothing answering on the port."""
    if serving(port, host):
        return False
    err = project / "logs" / "alarm_server.err"
    try:
        tail = err.read_text()[-4000:]
    except OSError:
        return False
    return "No route to host" in tail or "EHOSTUNREACH" in tail


def uninstall(label: str = LABEL) -> bool:
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{label}"],
                   capture_output=True)
    path = plist_path(label)
    if path.exists():
        path.unlink()
        return True
    return False


# macOS 15 gates LAN access per responsible binary. A launchd agent has no
# way to show the consent prompt, so it is denied until granted by hand, and
# every connection fails with EHOSTUNREACH even for plain unicast.
LOCAL_NETWORK_HINT = """
This is macOS Local Network privacy, not a bug in the monitor.

A launchd agent cannot show the permission prompt, so it is denied LAN access
until you grant it:

    System Settings -> Privacy & Security -> Local Network
    enable the entry for "uv" (or python3.12 / Terminal)

launchd retries every 60s, so the monitor will start collecting on its own
within a minute of you enabling it -- no reinstall needed. Check with:

    uv run twiddle daemon status

If you would rather not grant it, run the watcher inside a terminal session
instead, which already has the permission:

    uv run twiddle watch --duration 0 --interval 30 \\
        --log logs/daemon.jsonl --max-bytes 50000000 --anchor <speaker-ip>
"""


ALARM_LOCAL_NETWORK_HINT = """
This is macOS Local Network privacy, not a bug in the alarm server.

A launchd agent cannot show the permission prompt, so it is denied LAN access
until you grant it:

    System Settings -> Privacy & Security -> Local Network
    enable the entry for "uv" (or python3.12 / Terminal)

launchd retries every 60s, so no reinstall is needed. Check with:

    uv run twiddle alarm serve --status

Or run it in a terminal session, which already has the permission:

    uv run twiddle alarm serve --dir <folder>
"""


def log_is_fresh(log: Path, interval: float, slack: float = 3.0) -> bool:
    """Has the log been written to recently enough to call the agent alive?

    launchd appends to the same stderr file across restarts, so a stale
    traceback proves nothing -- a failed first run followed by a healthy one
    leaves the old error in place. Liveness has to be measured on the log the
    agent is actually writing.
    """
    import time
    if not log.exists():
        return False
    try:
        age = time.time() - log.stat().st_mtime
    except OSError:
        return False
    return age <= max(interval * slack, 60.0)


def blocked_by_local_network(project: Path, log: Path | None = None,
                             interval: float = 30.0) -> bool:
    """Is the agent currently unable to reach the LAN?

    Requires both a denied-access error on record *and* a log that has gone
    stale: either alone is a false positive.
    """
    if log is not None and log_is_fresh(log, interval):
        return False
    err = project / "logs" / "daemon.err"
    if not err.exists():
        return False
    try:
        tail = err.read_text()[-4000:]
    except OSError:
        return False
    return "No route to host" in tail or "EHOSTUNREACH" in tail


def status(label: str = LABEL) -> str:
    r = subprocess.run(["launchctl", "print", f"gui/{_uid()}/{label}"],
                       capture_output=True, text=True)
    if r.returncode:
        return "not loaded"
    out = []
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith(("state =", "pid =", "last exit code =", "runs =")):
            out.append(s)
    return "; ".join(out) or "loaded"


def _uid() -> int:
    import os
    return os.getuid()
