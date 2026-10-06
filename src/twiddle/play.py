"""Play audio on Sonos without Spotify.

Sonos will fetch a plain HTTP URL and play it, so we can serve local files
straight off this Mac. That removes Spotify, the Sonos cloud queue and the
Connect handoff from the path, leaving only the LAN and the speaker -- which
is exactly the control condition needed to tell a radio problem from a
service problem.

Unlike the rest of the package this module WRITES to speakers (transport and
volume), so every entry point is explicit about it.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .devices import soap

# Every write we make is journalled here, so analysis can tell a real dropout
# from one our own transport change provoked. Re-pointing a stereo pair's
# coordinator briefly unsettles the pair, which looks exactly like a fault.
#
# Anchored to the repo, not the working directory: `scripts/radio.zsh` runs
# `uv run --project` from wherever the shell happens to be, and a relative
# path there scattered real writes into other directories' logs/ where
# `analyse` never saw them (found 2026-09-22: 16 of that day's tunes).
_REPO = Path(__file__).resolve().parents[2]
INTERVENTION_LOG = Path(os.environ.get("TWIDDLE_INTERVENTIONS",
                                       _REPO / "logs" / "interventions.jsonl"))

AUDIO_EXT = {".mp3", ".m4a", ".mp4", ".aac", ".flac", ".wav", ".ogg", ".oga", ".aiff", ".wma"}

# Radio-style streams play via this scheme; Sonos treats them as endless.
RADIO_SCHEME = "x-rincon-mp3radio://"


def _journal(action: str, ip: str, **extra) -> None:
    """Record a state-changing call. Never let logging break playback."""
    try:
        INTERVENTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "action": action, "ip": ip} | extra
        with INTERVENTION_LOG.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# A span's start always says how long it can last. Whatever opened it may die
# before it closes it, and an unclosed span must end somewhere finite: left
# open forever it would discount every real fault that followed.
DEFAULT_SPAN_MAX_S = 6 * 3600


def new_span_id() -> str:
    return uuid.uuid4().hex


def journal_span(action: str, ip: str, *, span_id: str | None = None,
                 max_s: float | None = None, **extra) -> str | None:
    """Record a sustained activity, not a one-off command.

    While this Mac serves audio to a speaker it is part of the audio path, so
    anything observed during that whole window is suspect -- not just the
    instant a command was sent. Analysis needs the span, so mark its start and
    end explicitly.

    A start (`..._start`) gets a `span_id` (made here unless given) and a finite
    `max_s`, the longest it can last: `analyse` ends an unclosed span there. An
    end names the same `span_id`, so overlapping spans of one action on one
    speaker stay apart; an end without one closes the latest open span of that
    action and speaker. Returns the id a start was given.
    """
    if action.endswith("_start"):
        span_id = span_id or new_span_id()
        extra["max_s"] = DEFAULT_SPAN_MAX_S if max_s is None else max_s
    if span_id:
        extra["span_id"] = span_id
    _journal(action, ip, span=True, **extra)
    return span_id


def local_ip_for(peer: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, 1400))
        return s.getsockname()[0]
    finally:
        s.close()


class _QuietHandler(SimpleHTTPRequestHandler):
    """Serves files without logging, and without shouting when Sonos hangs up.

    Sonos buffers ahead and then closes the connection, which surfaces as
    BrokenPipeError/ConnectionResetError. That is normal client behaviour, not
    an error worth a traceback -- and the tracebacks bury the actual output.
    """

    def log_message(self, *_args):
        pass

    def copyfile(self, source, outputfile):
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


@dataclass
class FileServer:
    """Serves a directory over HTTP on the LAN so speakers can pull from it."""
    root: Path
    port: int = 0
    _srv: ThreadingHTTPServer | None = None

    def start(self) -> int:
        handler = partial(_QuietHandler, directory=str(self.root))
        self._srv = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self.port = self._srv.server_port
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self.port

    def url_for(self, path: Path, peer: str) -> str:
        rel = path.resolve().relative_to(self.root.resolve())
        quoted = "/".join(urllib.parse.quote(p) for p in rel.parts)
        return f"http://{local_ip_for(peer)}:{self.port}/{quoted}"

    def stop(self):
        if self._srv:
            self._srv.shutdown()


def find_audio(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and p.suffix.lower() in AUDIO_EXT)


# ---- transport control (writes) -------------------------------------------

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _av(ip: str, action: str, body: str) -> str:
    return soap(ip, "AVTransport", action,
                "<InstanceID>0</InstanceID>" + body,
                path="/MediaRenderer/AVTransport/Control")


def set_uri(ip: str, uri: str, metadata: str = "") -> None:
    _journal("set_uri", ip, uri=uri)
    _av(ip, "SetAVTransportURI",
        f"<CurrentURI>{_esc(uri)}</CurrentURI>"
        f"<CurrentURIMetaData>{_esc(metadata)}</CurrentURIMetaData>")


def add_to_queue(ip: str, uri: str, metadata: str = "") -> str:
    _journal("add_to_queue", ip)
    return _av(ip, "AddURIToQueue",
               f"<EnqueuedURI>{_esc(uri)}</EnqueuedURI>"
               f"<EnqueuedURIMetaData>{_esc(metadata)}</EnqueuedURIMetaData>"
               "<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued>"
               "<EnqueueAsNext>0</EnqueueAsNext>")


def clear_queue(ip: str) -> None:
    _journal("clear_queue", ip)
    _av(ip, "RemoveAllTracksFromQueue", "")


def play_queue(ip: str, uuid: str) -> None:
    """Point the renderer at its own queue, then start it."""
    set_uri(ip, f"x-rincon-queue:{uuid}#0")
    play(ip)


def play(ip: str) -> None:
    _journal("play", ip)
    _av(ip, "Play", "<Speed>1</Speed>")


def pause(ip: str) -> None:
    _journal("pause", ip)
    _av(ip, "Pause", "")


def stop(ip: str) -> None:
    _journal("stop", ip)
    _av(ip, "Stop", "")


def _hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def configure_sleep_timer(ip: str, duration_s: int) -> None:
    """Arm (or, with `duration_s <= 0`, cancel) the speaker's own sleep timer.

    Runs entirely on the speaker: nothing on this Mac has to stay alive to
    stop playback later, which matters overnight -- a process started via
    `uv run` and then killed is exactly the SIGTERM trap this repo already
    hit once with the relay supervisor.

    Verified on the Roam (2026-09-25, KALX, 5 min): the timer counts down,
    reads back as off the moment it expires, then the speaker fades the
    volume out over ~40s, stops, and puts the volume back as it was.
    """
    hms = "" if duration_s <= 0 else _hms(duration_s)
    _journal("configure_sleep_timer", ip, duration_s=duration_s)
    _av(ip, "ConfigureSleepTimer",
        f"<NewSleepTimerDuration>{hms}</NewSleepTimerDuration>")


SLEEP_FIRE_MARGIN_S = 180     # the timer's own precision is not promised


def parse_hms(text: str | None) -> int | None:
    """Seconds in an "H:MM:SS", or None for "" or anything else."""
    parts = (text or "").strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    h, m, s = (int(p) for p in parts)
    return h * 3600 + m * 60 + s


def fmt_left(seconds: int | None) -> str:
    """A sleep timer's time left as a clock would say it: "42:05", "1:30:00"."""
    if not seconds:
        return "off"
    h, rest = divmod(int(seconds), 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def get_sleep_timer(ip: str) -> int | None:
    """Seconds left on the room's sleep timer, or None when none is set.

    Read-only. Ask the group's coordinator: the timer is the group's.
    """
    import re
    r = _av(ip, "GetRemainingSleepTimerDuration", "")
    m = re.search(r"<RemainingSleepTimerDuration>([^<]*)</RemainingSleepTimerDuration>", r)
    left = parse_hms(m.group(1)) if m else None
    return left or None


def set_sleep_timer(ip: str, duration_s: int, **journal) -> datetime | None:
    """Arm the sleep timer and journal when it will stop the room; `<= 0`
    cancels. Returns when it fires, or None.

    The stop it causes hours later is ours, not a fault, so a span is
    journalled around that moment for `analyse` to discount -- the same
    reasoning as `comedy sleep`'s tail span. A timer cancelled or re-armed
    later leaves its span behind: a few minutes wrongly discounted, which is
    the cheaper mistake than a stop wrongly counted.
    """
    configure_sleep_timer(ip, duration_s)
    if duration_s <= 0:
        return None
    fires = datetime.now(timezone.utc) + timedelta(seconds=duration_s)
    sid, bound = new_span_id(), 60 + SLEEP_FIRE_MARGIN_S
    for name, at in (("sleep_timer_fire_start", fires - timedelta(seconds=60)),
                     ("sleep_timer_fire_end", fires + timedelta(seconds=SLEEP_FIRE_MARGIN_S))):
        journal_span(name, ip, span_id=sid, max_s=bound,
                     ts=at.isoformat(timespec="milliseconds"), **journal)
    return fires


def set_volume(ip: str, volume: int) -> None:
    volume = max(0, min(100, int(volume)))
    _journal("set_volume", ip, volume=volume)
    soap(ip, "RenderingControl", "SetVolume",
         f"<InstanceID>0</InstanceID><Channel>Master</Channel>"
         f"<DesiredVolume>{volume}</DesiredVolume>",
         path="/MediaRenderer/RenderingControl/Control")


def get_volume(ip: str) -> int:
    import re
    r = soap(ip, "RenderingControl", "GetVolume",
             "<InstanceID>0</InstanceID><Channel>Master</Channel>",
             path="/MediaRenderer/RenderingControl/Control")
    m = re.search(r"<CurrentVolume>(\d+)</CurrentVolume>", r)
    return int(m.group(1)) if m else -1


def transport_info(ip: str) -> dict:
    import html
    import re
    out: dict = {}
    try:
        r = _av(ip, "GetTransportInfo", "")
        for tag in ("CurrentTransportState", "CurrentTransportStatus"):
            m = re.search(rf"<{tag}>([^<]*)</{tag}>", r)
            if m:
                out[tag] = html.unescape(m.group(1))
    except Exception as exc:
        out["error"] = type(exc).__name__
    try:
        r = _av(ip, "GetPositionInfo", "")
        for tag in ("Track", "TrackDuration", "RelTime", "TrackURI"):
            m = re.search(rf"<{tag}>([^<]*)</{tag}>", r)
            if m:
                # SOAP responses arrive XML-escaped. Unescape once here, so
                # callers hold the real URI -- a stream URI carries &amp; in
                # its query string, and re-escaping it on the way back out
                # would replay a URI the speaker cannot resolve.
                out[tag] = html.unescape(m.group(1))
    except Exception:
        pass
    return out


def radio_didl(title: str, art: str | None = None) -> str:
    """DIDL-Lite for a stream. `art` is what the Sonos app shows as its cover;
    the speaker fetches it itself, so it must be reachable from the speaker."""
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="R:0/0/0" parentID="R:0/0" restricted="true">'
        f"<dc:title>{_esc(title)}</dc:title>"
        + (f"<upnp:albumArtURI>{_esc(art)}</upnp:albumArtURI>" if art else "")
        + "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
        "</item></DIDL-Lite>"
    )


def track_didl(title: str, art: str | None = None, url: str | None = None,
               mime: str = "audio/mpeg") -> str:
    """DIDL-Lite for a single finite track (a file at an http(s) URL), as
    opposed to `radio_didl`'s endless broadcast.

    Pass `url` whenever its path has no audio extension: without a `<res>`
    naming the type, a Sonos guesses it from the path, and a Bandcamp
    stream URL (`.../mp3-128/3766738436?...`) gets UPnP error 714,
    "illegal MIME type" -- on the Roam, 2026-09-25. With it, the same URL
    is accepted, and plays as a track, position advancing."""
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="-1" parentID="-1" restricted="true">'
        f"<dc:title>{_esc(title)}</dc:title>"
        + (f'<res protocolInfo="http-get:*:{mime}:*">{_esc(url)}</res>' if url else "")
        + (f"<upnp:albumArtURI>{_esc(art)}</upnp:albumArtURI>" if art else "")
        + "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        "</item></DIDL-Lite>"
    )


def radio_uri(stream_url: str) -> str:
    """The URI a speaker fetches a stream by itself: `x-rincon-mp3radio://`
    in place of http(s), which a Sonos fetches over http."""
    for prefix in ("http://", "https://"):
        if stream_url.startswith(prefix):
            return RADIO_SCHEME + stream_url[len(prefix):]
    return stream_url


def play_radio(ip: str, stream_url: str, title: str = "Stream",
               art: str | None = None) -> None:
    """Play an internet radio stream directly, bypassing all Sonos services."""
    set_uri(ip, radio_uri(stream_url), radio_didl(title, art))
    play(ip)


# ---- additional transport verbs --------------------------------------------
#
# Everything that writes to a speaker lives in this module and goes through
# _journal(), because `analyse` discounts events within 45s of a journalled
# write. A caller that reaches for devices.soap() directly to save an import
# would silently poison the dropout investigation, so resist that.

def _rc(ip: str, action: str, body: str) -> str:
    return soap(ip, "RenderingControl", action,
                "<InstanceID>0</InstanceID>" + body,
                path="/MediaRenderer/RenderingControl/Control")


def next_track(ip: str) -> None:
    _journal("next", ip)
    _av(ip, "Next", "")


def previous_track(ip: str) -> None:
    _journal("previous", ip)
    _av(ip, "Previous", "")


def seek(ip: str, target: str, unit: str = "REL_TIME") -> None:
    """Jump within the current track. Only meaningful for queue-backed media.

    A live radio stream has no seekable position: Sonos answers Seek with a
    UPnP error rather than ignoring it, so callers restoring state must check
    what kind of source they are restoring instead of always seeking.
    """
    _journal("seek", ip, target=target, unit=unit)
    _av(ip, "Seek", f"<Unit>{unit}</Unit><Target>{_esc(target)}</Target>")


def set_mute(ip: str, muted: bool) -> None:
    _journal("set_mute", ip, muted=bool(muted))
    _rc(ip, "SetMute",
        f"<Channel>Master</Channel><DesiredMute>{1 if muted else 0}</DesiredMute>")


def get_mute(ip: str) -> bool:
    import re
    r = _rc(ip, "GetMute", "<Channel>Master</Channel>")
    m = re.search(r"<CurrentMute>(\d+)</CurrentMute>", r)
    return bool(int(m.group(1))) if m else False


def set_bass(ip: str, level: int) -> None:
    level = max(-10, min(10, int(level)))
    _journal("set_bass", ip, level=level)
    _rc(ip, "SetBass", f"<DesiredBass>{level}</DesiredBass>")


def get_bass(ip: str) -> int:
    import re
    r = _rc(ip, "GetBass", "")
    m = re.search(r"<CurrentBass>(-?\d+)</CurrentBass>", r)
    return int(m.group(1)) if m else 0


def set_treble(ip: str, level: int) -> None:
    level = max(-10, min(10, int(level)))
    _journal("set_treble", ip, level=level)
    _rc(ip, "SetTreble", f"<DesiredTreble>{level}</DesiredTreble>")


def get_treble(ip: str) -> int:
    import re
    r = _rc(ip, "GetTreble", "")
    m = re.search(r"<CurrentTreble>(-?\d+)</CurrentTreble>", r)
    return int(m.group(1)) if m else 0


def set_loudness(ip: str, on: bool) -> None:
    _journal("set_loudness", ip, on=bool(on))
    _rc(ip, "SetLoudness",
        f"<Channel>Master</Channel><DesiredLoudness>{1 if on else 0}</DesiredLoudness>")


def get_loudness(ip: str) -> bool:
    import re
    r = _rc(ip, "GetLoudness", "<Channel>Master</Channel>")
    m = re.search(r"<CurrentLoudness>(\d+)</CurrentLoudness>", r)
    return bool(int(m.group(1))) if m else False


def set_balance(ip: str, balance: int) -> None:
    """-10 (full left) .. 0 (centered) .. +10 (full right).

    Confirmed live against this household: Sonos's RenderingControl does not
    store balance as a signed slider value: GetVolume with Channel=LF/RF
    answers a *pair* of per-channel volumes (a centered Beam answers
    100/100). This maps the signed convenience value linearly onto that
    pair -- holding the channel you're moving toward at 100 and attenuating
    the other -- which moves the stereo image the right direction, but is
    not guaranteed to land on the exact value the Sonos app's own slider
    would use, since that curve is undocumented.
    """
    balance = max(-10, min(10, int(balance)))
    lf = 100 if balance <= 0 else round(100 - balance * 10)
    rf = 100 if balance >= 0 else round(100 + balance * 10)
    _journal("set_balance", ip, balance=balance, lf=lf, rf=rf)
    _rc(ip, "SetVolume", f"<Channel>LF</Channel><DesiredVolume>{lf}</DesiredVolume>")
    _rc(ip, "SetVolume", f"<Channel>RF</Channel><DesiredVolume>{rf}</DesiredVolume>")


def get_balance(ip: str) -> int:
    """Approximate signed balance, inverted from the raw (LF, RF) pair.

    `round((RF - LF) / 10)` is exact for anything `set_balance` itself wrote
    (the two formulas are inverses), and a reasonable approximation for a
    balance set some other way -- e.g. from the Sonos app.
    """
    import re
    vals = {}
    for chan in ("LF", "RF"):
        r = _rc(ip, "GetVolume", f"<Channel>{chan}</Channel>")
        m = re.search(r"<CurrentVolume>(\d+)</CurrentVolume>", r)
        vals[chan] = int(m.group(1)) if m else 100
    return max(-10, min(10, round((vals["RF"] - vals["LF"]) / 10)))


def set_play_mode(ip: str, mode: str) -> None:
    """NORMAL | REPEAT_ALL | REPEAT_ONE | SHUFFLE | SHUFFLE_NOREPEAT."""
    _journal("set_play_mode", ip, mode=mode)
    _av(ip, "SetPlayMode", f"<NewPlayMode>{_esc(mode)}</NewPlayMode>")


def media_info(ip: str) -> dict:
    """What the renderer is pointed at, as opposed to where it has got to.

    GetPositionInfo reports the *track*; on a radio stream that is the stream.
    GetMediaInfo reports the URI the renderer was actually handed, which is
    what has to be replayed to restore state.
    """
    import html
    import re
    out: dict = {}
    try:
        r = _av(ip, "GetMediaInfo", "")
    except Exception as exc:
        return {"error": type(exc).__name__}
    for tag in ("CurrentURI", "CurrentURIMetaData", "NrTracks", "PlayMedium"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", r, re.S)
        if m:
            out[tag] = html.unescape(m.group(1))
    return out


def play_mode(ip: str) -> str:
    import re
    try:
        r = _av(ip, "GetTransportSettings", "")
    except Exception:
        return ""
    m = re.search(r"<PlayMode>([^<]*)</PlayMode>", r)
    return m.group(1) if m else ""


# Sonos's PlayMode is one enum covering both shuffle and repeat at once;
# `shuffle`/`repeat` verbs need to flip one axis while preserving the other.
_PLAY_MODES = {
    (False, "off"): "NORMAL",
    (False, "all"): "REPEAT_ALL",
    (False, "one"): "REPEAT_ONE",
    (True, "off"): "SHUFFLE_NOREPEAT",
    (True, "all"): "SHUFFLE",
    (True, "one"): "SHUFFLE_REPEAT_ONE",
}
_PLAY_MODES_INV = {v: k for k, v in _PLAY_MODES.items()}


def decode_play_mode(mode: str) -> tuple[bool, str]:
    """PlayMode string -> (shuffle, repeat), where repeat is off/all/one."""
    return _PLAY_MODES_INV.get(mode, (False, "off"))


def encode_play_mode(shuffle: bool, repeat: str) -> str:
    return _PLAY_MODES[(bool(shuffle), repeat)]


# ---- grouping (writes) -----------------------------------------------------

def join(ip: str, coordinator_uuid: str) -> None:
    """Make this speaker follow another's group."""
    _journal("join", ip, coordinator=coordinator_uuid)
    set_uri(ip, f"x-rincon:{coordinator_uuid}")


def unjoin(ip: str) -> None:
    """Detach this speaker into a group of its own."""
    _journal("unjoin", ip)
    _av(ip, "BecomeCoordinatorOfStandaloneGroup", "")
