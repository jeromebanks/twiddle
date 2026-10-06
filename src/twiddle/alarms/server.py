"""Alarm audio from this Mac: `twiddle alarm serve`.

A source that needs this Mac (a sound, a Bandcamp track) stores a URL on this
machine in the alarm itself; the speaker fetches it at fire time, hours after
anything here ran. So the port is fixed (`PORT`), not whatever was free, and
the server runs in the foreground until stopped.

It serves audio files from one directory, and Bandcamp tracks under
`/bandcamp/<token>.mp3` (`sources/bandcamp.py`): for those it asks Bandcamp for
a fresh stream URL at the moment of the request, since a stored one expires in
about a day, and passes the audio through. Both go to speakers of the
household (and to this Mac itself, so a `curl` can try it) and nothing else: an
unknown name, a directory, a path outside the directory, a file that isn't
audio, a token that isn't ours, a track Bandcamp won't resolve or open within
`resolve_s`, a client that is neither are each refused at once with an error
status, before any header or span, so a speaker that can't be served falls back
to its own chime instead of waiting on a page that never plays. Nothing here
writes to a speaker.

While a file is being sent this Mac is in the audio path, so every `GET` that
sends a body is a span in `logs/interventions.jsonl` (`alarm_serve_start`/
`alarm_serve_end`, one `span_id` each, so two at once to one speaker stay two),
and every start says how long it can last (`max_s`): the server stops sending
at that point, and `analyse` ends a span a crash left open there. At start the
server closes whatever spans of its own a previous run left open, at the
moment they could no longer have been running.
"""
from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import play, report
from .sources import bandcamp as bc

PORT = 8765          # fixed: an alarm stores the URL, and fires hours later
ACTION = "alarm_serve"
DEFAULT_MAX_S = 3600  # the longest one serve can last
CHUNK = 64 * 1024
RESOLVE_S = 5.0       # resolving a Bandcamp track and opening its audio, before a byte goes out
UPSTREAM_S = 10.0     # then each read of it may wait this long

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
        if path.suffix.lower() not in TYPES or not path.is_file():
            return None
    except (OSError, ValueError, RuntimeError):   # too long, unreadable, a symlink loop
        return None
    return path


def permitted(ip: str, speakers: set[str] | None) -> bool:
    """Whether a client may be served: a speaker of the household, or this Mac
    (loopback) so a `curl` can try it. `speakers` None means anyone (tests)."""
    return speakers is None or ip in speakers or ip.startswith("127.")


def resolve_stream(track: dict) -> str:
    """A fresh audio URL for a Bandcamp track (the default `resolve`)."""
    return bc.scene_bandcamp().stream_url(track)


def open_stream(url: str, timeout: float = UPSTREAM_S):
    """The audio at `url`, open for reading; urllib raises for any non-2xx."""
    import urllib.request
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"not an http(s) URL: {url!r}")
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": bc.scene_bandcamp().UA}),
        timeout=timeout)


def _alive(pid) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass            # exists, but not ours to signal
    return True


def close_stale(now: datetime | None = None) -> list[dict]:
    """Close every `alarm_serve` span a crashed run left open, and return them.

    A span belongs to the process that opened it (its `pid`): one whose
    process is still running is another live server's, and is left alone.

    Each ends at `min(start + max_s, now)`: when it could no longer have been
    running, never later, so a crash two days ago doesn't discount two days of
    real faults. Only this server's own spans: a live relay's stay open.
    """
    now = now or datetime.now(timezone.utc)
    closed = []
    for st in report.open_spans(play.INTERVENTION_LOG):
        if st["name"] != ACTION or _alive(st["pid"]):
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
    tests) and to loopback, journalling one bounded span per file sent."""

    def __init__(self, root: Path, speakers: set[str] | None, port: int = PORT,
                 max_s: float = DEFAULT_MAX_S, host: str = "0.0.0.0", *,
                 stream_url=resolve_stream, fetch=open_stream, resolve_s: float = RESOLVE_S):
        if not (isinstance(max_s, (int, float)) and 0 < max_s < float("inf")):
            raise ValueError(f"max_s must be a finite number of seconds above 0, not {max_s!r}")
        if not (isinstance(resolve_s, (int, float)) and 0 < resolve_s < float("inf")):
            raise ValueError(f"resolve_s must be a finite number of seconds above 0, not {resolve_s!r}")
        self.root, self.speakers, self.max_s = Path(root), speakers, max_s
        self._stream_url, self._fetch, self.resolve_s = stream_url, fetch, resolve_s
        self._active: dict[str, tuple[str, socket.socket]] = {}   # span_id -> (ip, connection)
        self._lock = threading.Lock()
        self._serving = False
        self._stopping = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                pass

            def _refuse(self, status: int, why: str):
                self.send_error(status, why)

            def _target(self) -> Path | None:
                ip = self.client_address[0]
                if not permitted(ip, outer.speakers):
                    self._refuse(403, "not a speaker of this household")
                    return None
                path = resolve(outer.root, self.path)
                if path is None:
                    self._refuse(404, "no such audio")
                return path

            def _bandcamp(self, head: bool):
                """A Bandcamp track: every refusal comes before a header or a span."""
                if not permitted(self.client_address[0], outer.speakers):
                    return self._refuse(403, "not a speaker of this household")
                token = bc.token_of(self.path)
                track = bc.decode(token) if token else None
                if track is None:
                    return self._refuse(404, "no such audio")
                try:
                    upstream = outer._upstream(track)
                except TimeoutError:
                    return self._refuse(504, "Bandcamp did not answer in time")
                except Exception:
                    return self._refuse(502, "Bandcamp could not be reached for this track")
                with contextlib.closing(upstream):
                    if head:
                        return self._headers_for("audio/mpeg", None)
                    span_id = outer._open(self.client_address[0], f"bandcamp:{track.get('id') or track.get('title')}",
                                          self.connection)
                    if span_id is None:
                        return self._refuse(503, "shutting down")
                    try:
                        size = (getattr(upstream, "headers", None) or {}).get("Content-Length")
                        self._headers_for("audio/mpeg", size)
                        outer._send(upstream, self.connection, time.monotonic() + outer.max_s)
                    except OSError:
                        pass   # the speaker hung up, or a stall ran past the bound
                    finally:
                        outer._close(span_id)

            def do_HEAD(self):
                if self.path.startswith(bc.ROUTE):
                    return self._bandcamp(head=True)
                path = self._target()
                if path is None:
                    return
                try:
                    size = path.stat().st_size
                except OSError:        # gone since the name was resolved
                    return self._refuse(404, "no such audio")
                self._headers(path, size)

            def _headers(self, path: Path, size: int):
                self._headers_for(TYPES[path.suffix.lower()], size)

            def _headers_for(self, content_type: str, size):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                if size is not None:
                    self.send_header("Content-Length", str(size))
                self.end_headers()

            def do_GET(self):
                if self.path.startswith(bc.ROUTE):
                    return self._bandcamp(head=False)
                path = self._target()
                if path is None:
                    return
                try:       # size from the open file: a delete after this can't matter
                    fh = path.open("rb")
                    size = os.fstat(fh.fileno()).st_size
                except OSError:
                    return self._refuse(404, "no such audio")
                with fh:
                    span_id = outer._open(self.client_address[0], path.name, self.connection)
                    if span_id is None:
                        return self._refuse(503, "shutting down")
                    try:
                        self._headers(path, size)
                        outer._send(fh, self.connection, time.monotonic() + outer.max_s)
                    except OSError:
                        pass   # Sonos buffers ahead and hangs up, or stalled past the bound
                    finally:
                        outer._close(span_id)

        self._srv = ThreadingHTTPServer((host, port), Handler)
        self._srv.daemon_threads = True
        self.port = self._srv.server_port

    def _upstream(self, track: dict):
        """The track's audio, open: a fresh URL from Bandcamp, then its first
        response, all within `resolve_s` or `TimeoutError`. A worker does it, so
        a resolver that hangs can't hold the speaker; one that finishes after
        the deadline closes what it opened."""
        done = threading.Event()
        box: dict = {}
        lock = threading.Lock()

        def work():
            try:
                result = self._fetch(self._stream_url(track))
            except BaseException as exc:      # handed to the waiting handler
                result, exc_ = None, exc
            else:
                exc_ = None
            with lock:
                if box.get("abandoned"):
                    if result is not None:
                        result.close()
                    return
                box["result"], box["exc"] = result, exc_
                done.set()          # inside the lock: the deadline can't see it unset and abandon it

        threading.Thread(target=work, daemon=True).start()
        if not done.wait(self.resolve_s):
            with lock:
                if not done.is_set():
                    box["abandoned"] = True
                    raise TimeoutError("Bandcamp did not answer in time")
        if box["exc"] is not None:
            raise box["exc"]
        return box["result"]

    def _open(self, ip: str, name: str, conn: socket.socket) -> str | None:
        """Open a span and register the transfer, unless the server is stopping:
        the check, the journal line and the registration are one step, so a
        handler that was already accepted can't slip in after `stop`."""
        with self._lock:
            if self._stopping:
                return None
            span_id = play.journal_span(f"{ACTION}_start", ip, max_s=self.max_s,
                                        file=name, pid=os.getpid())
            self._active[span_id] = (ip, conn)
        return span_id

    def _close(self, span_id: str) -> None:
        with self._lock:
            entry = self._active.pop(span_id, None)
        if entry is not None:
            play.journal_span(f"{ACTION}_end", entry[0], span_id=span_id)

    @staticmethod
    def _send(fh, conn: socket.socket, deadline: float) -> None:
        """Send the file, giving up at `deadline`: each write may wait only for
        the time left, so a reader that stalls can't hold the span open."""
        while (left := deadline - time.monotonic()) > 0 and (chunk := fh.read(CHUNK)):
            conn.settimeout(left)
            conn.sendall(chunk)

    def url_for(self, name: str, peer: str) -> str:
        return (f"http://{play.local_ip_for(peer)}:{self.port}/"
                f"{urllib.parse.quote(name)}")

    def serve_forever(self) -> None:
        self._serving = True
        self._srv.serve_forever()

    def stop(self) -> None:
        """Stop listening and close every span still open: handler threads are
        daemons, so their own cleanup does not run when this process ends."""
        with self._lock:
            self._stopping = True
        if self._serving:   # `shutdown` waits forever on a loop that never ran
            self._srv.shutdown()
        self._srv.server_close()
        with self._lock:
            left = dict(self._active)
        for span_id, (_, conn) in left.items():
            try:                       # stop the sending first: the span is
                conn.shutdown(socket.SHUT_RDWR)   # only over once nothing more goes out
            except OSError:
                pass
            self._close(span_id)
