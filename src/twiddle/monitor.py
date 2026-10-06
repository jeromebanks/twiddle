"""Watch the household and record every disconnect with enough context to
explain it.

Two independent observers run together, because the interesting failure is
exactly when they disagree:

  * household view  -- GENA events + topology polls from a mains-powered
    speaker: "does Sonos still consider this speaker present?"
  * direct view     -- ICMP + HTTP straight from this Mac: "is the speaker
    actually up and reachable?"

A speaker that is reachable but absent from the topology lost the household
heartbeat, not power. BootSeq then says whether it rebooted.

It also records the household's alarm schedule (`alarm_schedule`), so
`analyse` can tell a vanish an alarm caused from a fault, whoever set it.

Read-only: nothing here changes speaker state.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

from . import topology
from .alarms import clock
from .devices import PORT, load_device


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------
# direct reachability
# --------------------------------------------------------------------------

def ping_stats(ip: str, count: int = 5, interval: float = 0.2) -> dict:
    """ICMP latency/jitter/loss, measured from this machine."""
    try:
        out = subprocess.run(
            ["ping", "-c", str(count), "-i", str(interval), "-t", "4", ip],
            capture_output=True, text=True, timeout=count * interval + 8,
        ).stdout
    except Exception:
        return {"loss_pct": 100.0}
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    d: dict = {"loss_pct": float(loss.group(1)) if loss else 100.0}
    if rtt:
        d |= {
            "rtt_min": float(rtt.group(1)),
            "rtt_avg": float(rtt.group(2)),
            "rtt_max": float(rtt.group(3)),
            "rtt_stddev": float(rtt.group(4)),
        }
    return d


def http_alive(ip: str, timeout: float = 4.0) -> dict:
    t0 = time.monotonic()
    try:
        r = requests.get(f"http://{ip}:{PORT}/status/zp", timeout=timeout)
        return {"http_ok": r.status_code == 200,
                "http_ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as exc:
        return {"http_ok": False, "http_err": type(exc).__name__}


# --------------------------------------------------------------------------
# GENA event listener
# --------------------------------------------------------------------------

class _EventHandler(BaseHTTPRequestHandler):
    sink = None  # set by EventListener

    def do_NOTIFY(self):  # noqa: N802 - UPnP verb
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        self.send_response(200)
        self.end_headers()
        if _EventHandler.sink:
            _EventHandler.sink({
                "ts": _now(),
                "kind": "gena",
                "sid": self.headers.get("SID", ""),
                "seq": self.headers.get("SEQ", ""),
                "body_len": length,
                "vanish_hint": "VanishedDevices" in body,
            })

    def log_message(self, *_args):
        pass


class EventListener:
    """Minimal GENA callback server plus SUBSCRIBE/renew for one service."""

    def __init__(self, sink):
        _EventHandler.sink = sink
        self.sink = sink
        self.server = HTTPServer(("0.0.0.0", 0), _EventHandler)
        self.port = self.server.server_port
        self.subs: list[tuple[str, str, str]] = []  # (ip, path, sid)
        self._stop = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _local_ip(self, peer: str) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((peer, PORT))
            return s.getsockname()[0]
        finally:
            s.close()

    def subscribe(self, ip: str, event_path: str, seconds: int = 600) -> str | None:
        cb = f"<http://{self._local_ip(ip)}:{self.port}/>"
        try:
            r = requests.request(
                "SUBSCRIBE", f"http://{ip}:{PORT}{event_path}",
                headers={"CALLBACK": cb, "NT": "upnp:event",
                         "TIMEOUT": f"Second-{seconds}"},
                timeout=6,
            )
            sid = r.headers.get("SID", "")
            if sid:
                self.subs.append((ip, event_path, sid))
            return sid or None
        except Exception:
            return None

    def renew_loop(self, seconds: int = 600):
        def run():
            while not self._stop.wait(seconds * 0.6):
                for ip, path, sid in list(self.subs):
                    try:
                        requests.request(
                            "SUBSCRIBE", f"http://{ip}:{PORT}{path}",
                            headers={"SID": sid, "TIMEOUT": f"Second-{seconds}"},
                            timeout=6)
                    except Exception:
                        pass
        threading.Thread(target=run, daemon=True).start()

    def close(self):
        self._stop.set()
        for ip, path, sid in self.subs:
            try:
                requests.request("UNSUBSCRIBE", f"http://{ip}:{PORT}{path}",
                                 headers={"SID": sid}, timeout=4)
            except Exception:
                pass
        self.server.shutdown()


# --------------------------------------------------------------------------
# the monitor
# --------------------------------------------------------------------------

@dataclass
class Watch:
    """Per-speaker rolling state, so we only log transitions."""
    uuid: str
    ip: str
    name: str
    present: bool = True
    boot_seq: str = ""
    uptime_s: int | None = None
    last_read: float | None = None


class Monitor:
    def __init__(self, anchor_ip: str, out_path: Path, interval: float = 10.0,
                 max_bytes: int = 0, keep: int = 5, quiet: bool = False):
        self.anchor = anchor_ip
        self.out = out_path
        self.interval = interval
        self.max_bytes = max_bytes   # 0 = never rotate
        self.keep = keep
        self.quiet = quiet
        self.watches: dict[str, Watch] = {}
        self.events = 0
        self._fh = None
        # The alarm schedule as last logged. Rotation (which can run on the
        # GENA thread) copies it into each new file, so it is only ever
        # replaced whole, never mutated.
        self._alarm_schedule: dict | None = None
        self._alarms_read_at: str | None = None    # the last successful read
        # GENA notifications arrive on the HTTP server's thread while the poll
        # loop is also writing, so every log write and the rotation that
        # closes the handle must be serialised. Without this, records
        # interleave into malformed JSON and rotation races a live writer.
        self._lock = threading.Lock()

    def _rotate_if_needed(self):
        """Keep an unattended run from filling the disk.

        Caller must hold ``self._lock``: this swaps the open file handle.
        """
        if not self.max_bytes or not self._fh:
            return
        if self._fh.tell() < self.max_bytes:
            return
        self._fh.close()
        for i in range(self.keep - 1, 0, -1):
            src = self.out.with_suffix(self.out.suffix + f".{i}")
            dst = self.out.with_suffix(self.out.suffix + f".{i + 1}")
            if src.exists():
                src.replace(dst)
        self.out.replace(self.out.with_suffix(self.out.suffix + ".1"))
        self._fh = self.out.open("a")
        # Carry the schedule into the new file with its original valid_from:
        # once the file that first recorded it rotates past `keep`, this is
        # the only record left that the alarms were set. Written directly --
        # `_emit` holds the lock that calling it again would deadlock on.
        schedule = self._alarm_schedule
        if schedule is not None:
            self._fh.write(json.dumps(schedule | {"ts": _now(), "checkpoint": True}) + "\n")
            self._fh.flush()

    def _emit(self, rec: dict):
        rec.setdefault("ts", _now())
        line = json.dumps(rec) + "\n"
        with self._lock:
            if self._fh is None or self._fh.closed:
                return
            self._fh.write(line)
            self._fh.flush()
            self._rotate_if_needed()
            self.events += 1
        kind = rec.get("kind")
        if self.quiet and kind not in {"vanish", "reboot", "start", "stop"}:
            return
        if kind in {"vanish", "return", "reboot", "sample_error", "start", "phy_spike"}:
            detail = rec.get("detail", "")
            print(f"  {rec['ts'][11:19]}  {kind.upper():12} {rec.get('name','')} {detail}")

    def _snapshot(self) -> dict:
        topo = topology.fetch(self.anchor)
        present = {m.uuid: m for m in topo.members}
        vanished = {v.uuid: v for v in topo.vanished}
        seen_now = set(present) | set(vanished)

        for uuid, m in present.items():
            w = self.watches.get(uuid)
            if w is None:
                w = self.watches[uuid] = Watch(uuid=uuid, ip=m.ip,
                                               name=m.zone_name or m.ip,
                                               boot_seq=m.boot_seq)
                continue
            if m.ip:
                w.ip = m.ip
            if not w.present:
                self._emit({"kind": "return", "uuid": uuid, "name": w.name,
                            "ip": w.ip, "boot_seq": m.boot_seq,
                            "detail": f"back in household (BootSeq {m.boot_seq})"})
                w.present = True
            if w.boot_seq and m.boot_seq and m.boot_seq != w.boot_seq:
                self._emit({"kind": "reboot", "uuid": uuid, "name": w.name,
                            "ip": w.ip, "from": w.boot_seq, "to": m.boot_seq,
                            "detail": f"BootSeq {w.boot_seq} -> {m.boot_seq} "
                                      "(speaker restarted)"})
            w.boot_seq = m.boot_seq or w.boot_seq

        for uuid, v in vanished.items():
            w = self.watches.get(uuid)
            if w is None:
                w = self.watches[uuid] = Watch(uuid=uuid, ip=v.last_known_ip,
                                               name=v.zone_name or v.last_known_ip,
                                               present=False)
            if w.present:
                # The decisive measurement: is it still reachable right now?
                probe = ping_stats(v.last_known_ip, count=4) | http_alive(v.last_known_ip)
                verdict = ("reachable -> lost household heartbeat, not power"
                           if probe.get("http_ok") else "unreachable -> off network")
                self._emit({"kind": "vanish", "uuid": uuid, "name": w.name,
                            "ip": v.last_known_ip, "reason": v.reason,
                            "last_seen_utc": v.last_seen_utc, "probe": probe,
                            "detail": f"reason={v.reason}; {verdict}"})
                w.present = False

        # per-speaker RF + reachability sample
        samples = []
        for uuid, w in self.watches.items():
            if not w.ip:
                continue
            row: dict = {"uuid": uuid, "name": w.name, "ip": w.ip,
                         "in_household": w.present}
            row |= ping_stats(w.ip, count=4)
            row |= http_alive(w.ip)
            try:
                dev = load_device(w.ip)
                row |= {"phy_errors": dev.phy_errors, "uptime_s": dev.uptime_s,
                        "ieee_channel": dev.ieee_channel}
                # The driver zeroes this counter on read, so the value we just
                # got covers exactly the time since our previous sample.
                now = time.monotonic()
                if w.last_read is not None and dev.phy_errors is not None:
                    window = now - w.last_read
                    if window > 0:
                        row["phy_err_per_sec"] = round(dev.phy_errors / window, 1)
                w.last_read = now
                if dev.uptime_s is not None:
                    if w.uptime_s is not None and dev.uptime_s < w.uptime_s:
                        self._emit({"kind": "reboot", "uuid": uuid,
                                    "name": w.name, "ip": w.ip,
                                    "detail": f"uptime went backwards "
                                              f"({w.uptime_s}s -> {dev.uptime_s}s): restarted"})
                    w.uptime_s = dev.uptime_s
            except Exception as exc:
                row["dev_err"] = type(exc).__name__
            samples.append(row)

        missing = set(self.watches) - seen_now
        self._emit({"kind": "sample", "samples": samples,
                    "groups": {g: [m.uuid for m in ms] for g, ms in topo.groups.items()},
                    "absent_from_topology": sorted(missing)})
        return {"samples": samples}

    def _check_alarms(self):
        """Log the alarm schedule when it, or the household's UTC offset,
        changes.

        Read-only: ListAlarms, GetTimeNow and GetFormat, on the anchor (alarms
        are household-wide, so any speaker answers). StartTime is the
        household's local time and this log is UTC, so each record carries
        the offset between them; a DST change makes a new record with the same
        version. GetTimeZone isn't used: it is an opaque index (`clock.py`),
        while GetTimeNow's local - UTC is the offset actually in force. Polled
        every sample, and logged only on a change.

        The clock changed somewhere between the last read and this one, so a
        record for an offset change alone carries `offset_from`, the last read:
        a fire in that gap is scored under both offsets rather than neither.
        """
        # Tolerant: an alarm the model can't read is named in the record, not
        # left to end it, so the other alarms are still discounted.
        found = clock.list_alarms(self.anchor, tolerant=True)
        hh = clock.household_time(self.anchor)
        offset = round((hh.local - hh.utc).total_seconds() / 900) * 900
        now, last_read = _now(), self._alarms_read_at
        self._alarms_read_at = now
        last = self._alarm_schedule
        if last and last["version"] == found.version and last["utc_offset_s"] == offset:
            return
        rooms = {w.uuid: w.name for w in self.watches.values()}
        schedule = {
            "kind": "alarm_schedule", "valid_from": now, "version": found.version,
            "utc_offset_s": offset,
            **({"offset_from": last_read}
               if last and last["version"] == found.version and last_read else {}),
            "alarms": [{"id": a.id, "start_time": a.start_time, "duration": a.duration,
                        "recurrence": str(a.recurrence), "enabled": a.enabled,
                        "room_uuid": a.room_uuid,
                        "room": rooms.get(a.room_uuid, a.room_uuid)}
                       for a in found.alarms],
            **({"unreadable": [{"id": u.id, "room_uuid": u.room_uuid,
                                "room": rooms.get(u.room_uuid, u.room_uuid),
                                "reason": u.reason} for u in found.unreadable]}
               if found.unreadable else {}),
        }
        self._alarm_schedule = schedule
        self._emit(schedule | {"ts": now})

    def _reelect_anchor(self):
        """Pick another speaker to ask about the household.

        The anchor is just whoever answers topology questions. If it stops
        answering, any other member will do, and a daemon should not sit in a
        failure loop waiting for one specific speaker to come back.
        """
        from .devices import discover
        candidates = [ip for ip in discover() if ip != self.anchor]
        # Discovery may be unavailable; we still know every speaker we have
        # previously seen in the topology.
        candidates += [w.ip for w in self.watches.values()
                       if w.ip and w.ip != self.anchor and w.ip not in candidates]
        for ip in candidates:
            try:
                topology.fetch(ip)
            except Exception:
                continue
            self._emit({"kind": "anchor_change", "from": self.anchor, "to": ip,
                        "detail": f"anchor {self.anchor} unresponsive -> {ip}"})
            self.anchor = ip
            self._consecutive_errors = 0
            return

    def run(self, duration: float):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.out.open("a")
        listener = EventListener(sink=self._emit)
        for path in ("/ZoneGroupTopology/Event", "/MediaRenderer/AVTransport/Event"):
            sid = listener.subscribe(self.anchor, path)
            self._emit({"kind": "subscribe", "path": path, "ok": bool(sid)})
        listener.renew_loop()

        forever = duration <= 0
        self._emit({"kind": "start", "anchor": self.anchor,
                    "duration_s": duration, "interval_s": self.interval,
                    "detail": ("watching until stopped" if forever
                               else f"watching for {duration/60:.0f} min")})
        deadline = time.monotonic() + duration
        try:
            while forever or time.monotonic() < deadline:
                t0 = time.monotonic()
                try:
                    self._snapshot()
                    self._consecutive_errors = 0
                except Exception as exc:
                    # Never die on a transient network failure -- that is the
                    # condition we exist to record.
                    self._consecutive_errors = getattr(
                        self, "_consecutive_errors", 0) + 1
                    self._emit({"kind": "sample_error", "err": repr(exc),
                                "consecutive": self._consecutive_errors,
                                "detail": f"{type(exc).__name__} "
                                          f"(x{self._consecutive_errors})"})
                    if self._consecutive_errors >= 10:
                        # The anchor may be gone for good; re-elect one.
                        self._reelect_anchor()
                # Not while the anchor is failing: that would add another
                # timeout to every sample during the very outage being recorded.
                if not getattr(self, "_consecutive_errors", 0):
                    try:
                        self._check_alarms()
                    except Exception:
                        # Kept apart from the sampling above: a speaker that
                        # won't answer AlarmClock is not a reason to re-elect
                        # the anchor, and the last schedule logged stays in
                        # force until it does.
                        pass
                time.sleep(max(0.5, self.interval - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            self._emit({"kind": "interrupted"})
        finally:
            # Stop the event thread first, so nothing writes after close.
            listener.close()
            self._emit({"kind": "stop", "events": self.events})
            with self._lock:
                if self._fh and not self._fh.closed:
                    self._fh.close()
