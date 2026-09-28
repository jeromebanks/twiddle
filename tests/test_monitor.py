"""Monitor log integrity, including the two-thread case that broke it."""
import json
import threading
from pathlib import Path

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
