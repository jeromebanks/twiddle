"""Alarm audio from this Mac: `twiddle alarm serve`.

A source that needs this Mac (a sound, a Bandcamp track) stores a URL on this
machine in the alarm itself; the speaker fetches it at fire time, hours after
anything here ran. So the port is fixed (`PORT`), not whatever was free, and
the server runs in the foreground until stopped.

It serves audio files from one directory, to speakers of the household only,
and nothing else: an unknown name, a directory, a path outside the directory, a
file that isn't audio, a client that isn't a speaker are each refused at once
with an error status, so a speaker that can't be served falls back to its own
chime instead of waiting on a page that never plays. Nothing here writes to a
speaker.

While a file is being sent this Mac is in the audio path, so every `GET` that
sends a body is a span in `logs/interventions.jsonl` (`alarm_serve_start`/
`alarm_serve_end`, one `span_id` each, so two at once to one speaker stay two),
and every start says how long it can last (`max_s`): the server stops sending
at that point, and `analyse` ends a span a crash left open there. At start the
server closes whatever spans of its own a previous run left open, at the
moment they could no longer have been running.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import play, report

PORT = 8765          # fixed: an alarm stores the URL, and fires hours later
ACTION = "alarm_serve"
DEFAULT_MAX_S = 3600  # the longest one serve can last
CHUNK = 64 * 1024

TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
         ".aac": "audio/aac", ".flac": "audio/flac", ".wav": "audio/wav",
         ".ogg": "audio/ogg", ".oga": "audio/ogg", ".aiff": "audio/aiff",
         ".wma": "audio/x-ms-wma"}


def resolve(root: Path, request_path: str) -> Path | None:
    """The audio file a request names under `root`, or None: unknown, a
    directory, outside `root`, not audio, or a name that doesn't decode."""
    try:
        rel = urllib.parse.unquote(urllib.parse.urlsplit(request_path).path,
                                   errors="strict").lstrip("/")
    except (UnicodeDecodeError, ValueError):
        return None
    if not rel or "\0" in rel:
        return None
    try:
        root = root.resolve()
        path = (root / rel).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    if path.suffix.lower() not in TYPES or not path.is_file():
        return None
    return path


def close_stale(now: datetime | None = None) -> list[dict]:
    """Close every `alarm_serve` span a crashed run left open, and return them.

    Each ends at `min(start + max_s, now)`: when it could no longer have been
    running, never later, so a crash two days ago doesn't discount two days of
    real faults. Only this server's own spans: a live relay's stay open.
    """
    now = now or datetime.now(timezone.utc)
    closed = []
    for st in report.open_spans(play.INTERVENTION_LOG):
        if st["name"] != ACTION:
            continue
        end = now.timestamp()
        if st["max_s"] is not None:
            end = min(end, st["start"] + st["max_s"])
        play.journal_span(f"{ACTION}_end", st["ip"], span_id=st["span_id"], stale=True,
                          ts=datetime.fromtimestamp(end, timezone.utc)
                          .isoformat(timespec="milliseconds"))
        closed.append(st)
    return closed


class AlarmServer:
    """Serves `root` over HTTP to `speakers` (their IPs; None = anyone, for
    tests), journalling one bounded span per file sent."""

    def __init__(self, root: Path, speakers: set[str] | None, port: int = PORT,
                 max_s: float = DEFAULT_MAX_S, host: str = "0.0.0.0"):
        self.root, self.speakers, self.max_s = Path(root), speakers, max_s
        self._active: dict[str, tuple[str, float]] = {}   # span_id -> (ip, started)
        self._lock = threading.Lock()
        self._serving = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                pass

            def _refuse(self, status: int, why: str):
                self.send_error(status, why)

            def _target(self) -> Path | None:
                ip = self.client_address[0]
                if outer.speakers is not None and ip not in outer.speakers:
                    self._refuse(403, "not a speaker of this household")
                    return None
                path = resolve(outer.root, self.path)
                if path is None:
                    self._refuse(404, "no such audio")
                return path

            def do_HEAD(self):
                path = self._target()
                if path is not None:
                    self._headers(path)

            def _headers(self, path: Path):
                self.send_response(200)
                self.send_header("Content-Type", TYPES[path.suffix.lower()])
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()

            def do_GET(self):
                path = self._target()
                if path is None:
                    return
                try:
                    fh = path.open("rb")
                except OSError:
                    return self._refuse(404, "no such audio")
                with fh:
                    span_id = outer._open(self.client_address[0], path)
                    try:
                        self._headers(path)
                        outer._send(fh, self.wfile, time.monotonic() + outer.max_s)
                    except (BrokenPipeError, ConnectionResetError):
                        pass   # Sonos buffers ahead and hangs up: normal
                    finally:
                        outer._close(span_id)

        self._srv = ThreadingHTTPServer((host, port), Handler)
        self._srv.daemon_threads = True
        self.port = self._srv.server_port

    def _open(self, ip: str, path: Path) -> str:
        span_id = play.journal_span(f"{ACTION}_start", ip, max_s=self.max_s,
                                    file=path.name)
        with self._lock:
            self._active[span_id] = (ip, time.time())
        return span_id

    def _close(self, span_id: str) -> None:
        with self._lock:
            entry = self._active.pop(span_id, None)
        if entry is not None:
            play.journal_span(f"{ACTION}_end", entry[0], span_id=span_id)

    @staticmethod
    def _send(fh, out, deadline: float) -> None:
        while time.monotonic() < deadline and (chunk := fh.read(CHUNK)):
            out.write(chunk)

    def url_for(self, name: str, peer: str) -> str:
        return (f"http://{play.local_ip_for(peer)}:{self.port}/"
                f"{urllib.parse.quote(name)}")

    def serve_forever(self) -> None:
        self._serving = True
        self._srv.serve_forever()

    def stop(self) -> None:
        """Stop listening and close every span still open: handler threads are
        daemons, so their own cleanup does not run when this process ends."""
        if self._serving:   # `shutdown` waits forever on a loop that never ran
            self._srv.shutdown()
        self._srv.server_close()
        with self._lock:
            left = list(self._active)
        for span_id in left:
            self._close(span_id)
