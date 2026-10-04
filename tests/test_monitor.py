"""Monitor log integrity, including the two-thread case that broke it."""
import json
import threading
from pathlib import Path
from xml.sax.saxutils import escape

from twiddle import devices, monitor, play, report
from twiddle.monitor import Monitor


def _monitor(tmp_path: Path, **kw) -> tuple[Monitor, Path]:
    out = tmp_path / "mon.jsonl"
    m = Monitor("192.168.1.2", out, interval=1, quiet=True, **kw)
    m._fh = out.open("a")
    return m, out


def test_rotation_caps_size_and_keeps_n_files(tmp_path: Path):
    m, out = _monitor(tmp_path, max_bytes=500, keep=3)
    for i in range(80):
        m._emit({"kind": "sample", "n": i, "pad": "x" * 40})
    m._fh.close()
    rotated = sorted(p.name for p in tmp_path.glob("mon.jsonl.*"))
    assert rotated == ["mon.jsonl.1", "mon.jsonl.2", "mon.jsonl.3"]
    # No file is allowed to grow far past the cap.
    for p in tmp_path.glob("mon.jsonl*"):
        assert p.stat().st_size < 1200, p


def test_concurrent_writers_never_produce_malformed_json(tmp_path: Path):
    """GENA notifications land on the HTTP thread while the poll loop writes.

    Without serialisation these interleave mid-line and the log becomes
    unparseable -- which also silently corrupts every later analysis.
    """
    m, out = _monitor(tmp_path, max_bytes=800, keep=3)
    failures: list[str] = []

    def writer(tag: str):
        for i in range(300):
            try:
                m._emit({"kind": "gena" if tag == "a" else "sample",
                         "who": tag, "n": i, "pad": "y" * 30})
            except Exception as exc:          # noqa: BLE001 - recording it is the test
                failures.append(type(exc).__name__)

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    m._fh.close()

    # The original bug: one thread rotating closed the handle under the other,
    # raising ValueError("I/O operation on closed file") and losing records.
    assert not failures, f"writes raised: {failures}"

    total = 0
    for p in sorted(tmp_path.glob("mon.jsonl*")):
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            json.loads(line)  # raises if a write was torn
            total += 1
    # Rotation deliberately discards older files, so the total retained is
    # bounded by (keep + 1) * max_bytes -- the point here is only that every
    # surviving line is intact and nothing raised.
    assert total > 0
    assert sorted(q.name for q in tmp_path.glob("mon.jsonl.*")) == [
        "mon.jsonl.1", "mon.jsonl.2", "mon.jsonl.3"]


def test_emit_after_close_is_ignored(tmp_path: Path):
    # The event thread can fire once more as we are shutting down; that must
    # not raise on a closed handle.
    m, out = _monitor(tmp_path)
    m._fh.close()
    m._emit({"kind": "gena", "late": True})


# ---- the alarm schedule ----------------------------------------------------

ROAM_L = "RINCON_00000000000101400"
ALARMS_XML = (f'<Alarms><Alarm ID="7" StartTime="07:00:00" Duration="01:00:00" '
              f'Recurrence="DAILY" Enabled="1" RoomUUID="{ROAM_L}" '
              f'ProgramURI="x-rincon-buzzer:0" ProgramMetaData="" PlayMode="NORMAL" '
              f'Volume="20" IncludeLinkedZones="0"/></Alarms>')


def _envelope(action, inner):
    return ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:AlarmClock:1">'
            f"{inner}</u:{action}Response></s:Body></s:Envelope>")


class FakeSpeaker:
    """Answers AlarmClock reads at the HTTP layer and records every action."""

    def __init__(self, monkeypatch):
        self.sent: list[tuple[str, str]] = []
        self.version = f"{ROAM_L}:1"
        self.local, self.utc = "2026-10-31 13:20:01", "2026-10-31 20:20:01"   # PDT
        monkeypatch.setattr(devices.requests, "post", self.post)

    def post(self, url, data=None, headers=None, timeout=None):
        action = headers["SOAPACTION"].strip('"').rsplit("#", 1)[-1]
        self.sent.append((url, action))
        text = {
            "ListAlarms": _envelope("ListAlarms",
                                    f"<CurrentAlarmList>{escape(ALARMS_XML)}</CurrentAlarmList>"
                                    f"<CurrentAlarmListVersion>{self.version}"
                                    "</CurrentAlarmListVersion>"),
            "GetTimeNow": _envelope("GetTimeNow",
                                    f"<CurrentUTCTime>{self.utc}</CurrentUTCTime>"
                                    f"<CurrentLocalTime>{self.local}</CurrentLocalTime>"),
            "GetFormat": _envelope("GetFormat", "<CurrentTimeFormat>INV</CurrentTimeFormat>"
                                                "<CurrentDateFormat>INV</CurrentDateFormat>"),
        }[action]
        return type("Reply", (), {"text": text, "raise_for_status": lambda self: None})()


def _records(out: Path, kind: str) -> list[dict]:
    return [r for r in map(json.loads, out.read_text().splitlines()) if r["kind"] == kind]


def test_recording_the_alarm_schedule_is_read_only(tmp_path: Path, monkeypatch):
    speaker = FakeSpeaker(monkeypatch)
    m, out = _monitor(tmp_path)
    m._check_alarms()
    m._fh.close()
    actions = {a for _, a in speaker.sent}
    assert "ListAlarms" in actions
    assert actions <= {"ListAlarms", "GetTimeNow", "GetFormat"}, actions
    assert all("/AlarmClock/Control" in url for url, _ in speaker.sent)   # no AVTransport
    assert not play.INTERVENTION_LOG.exists()
    [rec] = _records(out, "alarm_schedule")
    assert rec["version"] == f"{ROAM_L}:1"
    assert rec["utc_offset_s"] == -7 * 3600
    assert rec["valid_from"] == rec["ts"]
    assert rec["alarms"] == [{"id": "7", "start_time": "07:00:00", "duration": "01:00:00",
                              "recurrence": "DAILY", "enabled": True, "room_uuid": ROAM_L,
                              "room": ROAM_L}]


def test_a_schedule_is_logged_when_it_or_the_utc_offset_changes(tmp_path: Path,
                                                                monkeypatch):
    speaker = FakeSpeaker(monkeypatch)
    m, out = _monitor(tmp_path)
    m.watches[ROAM_L] = monitor.Watch(uuid=ROAM_L, ip="192.168.1.2", name="Sonos Roam")
    m._check_alarms()
    m._check_alarms()                                    # nothing changed: nothing logged
    speaker.local, speaker.utc = "2026-11-01 01:00:30", "2026-11-01 09:00:30"   # PST now
    m._check_alarms()                                    # same version, new offset
    speaker.version = f"{ROAM_L}:2"
    m._check_alarms()                                    # edited in the Sonos app
    m._fh.close()
    recs = _records(out, "alarm_schedule")
    assert [(r["version"][-1], r["utc_offset_s"]) for r in recs] == [
        ("1", -7 * 3600), ("1", -8 * 3600), ("2", -8 * 3600)]
    assert recs[0]["alarms"][0]["room"] == "Sonos Roam"


def test_rotation_past_retention_keeps_the_alarm_discounted(tmp_path: Path,
                                                           monkeypatch):
    clock_now = ["2026-10-04T12:00:00.000Z"]
    monkeypatch.setattr(monitor, "_now", lambda: clock_now[0])
    FakeSpeaker(monkeypatch)
    m, out = _monitor(tmp_path, max_bytes=600, keep=2)
    m._check_alarms()
    clock_now[0] = "2026-10-09T03:00:00.000Z"
    for i in range(60):
        m._emit({"kind": "sample", "samples": [], "n": i, "pad": "x" * 40})
    m._emit({"kind": "vanish", "ts": "2026-10-09T14:00:20.000Z", "ip": "192.168.1.8",
             "name": "Sonos Roam", "probe": {"http_ok": True}})
    m._fh.close()

    # Only checkpoints survive: the record that first logged it is gone.
    kept = [r for p in report.rotated_logs(out)
            for r in map(json.loads, p.read_text().splitlines())
            if r["kind"] == "alarm_schedule"]
    assert kept and all(r.get("checkpoint") for r in kept)
    assert {r["valid_from"] for r in kept} == {"2026-10-04T12:00:00.000Z"}

    findings = report.summarise_log(out)
    assert any("right after an alarm" in f.title for f in findings)
    assert not any(f.title.startswith("Sonos Roam") and "left the household" in f.title
                   for f in findings)


def test_an_unanswered_alarmclock_never_disturbs_sampling(tmp_path: Path, monkeypatch):
    """The run loop's guard: an AlarmClock error is swallowed, not counted
    towards re-electing the anchor, and the last schedule stays."""
    m, out = _monitor(tmp_path)
    calls = []

    def snapshot():
        calls.append("snapshot")
        if len(calls) >= 3:
            raise KeyboardInterrupt

    def check_alarms():
        raise ConnectionError("AlarmClock down")

    monkeypatch.setattr(m, "_snapshot", snapshot)
    monkeypatch.setattr(m, "_check_alarms", check_alarms)
    monkeypatch.setattr(monitor, "EventListener", lambda sink: type(
        "L", (), {"subscribe": lambda *a: None, "renew_loop": lambda *a: None,
                  "close": lambda *a: None})())
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    m._fh.close()
    m.run(0)
    kinds = [r["kind"] for r in map(json.loads, out.read_text().splitlines())]
    assert "sample_error" not in kinds
    assert getattr(m, "_consecutive_errors", 0) == 0
