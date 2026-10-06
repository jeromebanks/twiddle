"""A Bandcamp track, played from this Mac: the alarm's URI points at
`twiddle alarm serve`, never at a Bandcamp URL.

A Bandcamp stream URL is signed and expires in about a day, so an alarm can't
store one. It stores `http://<this Mac>:<PORT>/bandcamp/<token>.mp3` instead, where
the token is the track's page and ID (nothing secret, nothing that expires).
When the alarm fires, the agent (`alarms/server.py`) asks Bandcamp for a fresh
URL and passes the audio through. If it can't, it refuses at once, so the room
falls back to its chime rather than waiting on silence.

`--source bandcamp:<choice>`, where a choice is a Bandcamp track (or release)
page URL, with `#<track id>` to pick one track of a release. `alarm sources
bandcamp <band>` searches Bandcamp the way `scene` does and lists tracks as
choices. Nothing here writes to a speaker.

The `.mp3` and the DIDL's `<res>` are for the speaker: without them it can
refuse the URL as an illegal MIME type (`play.track_didl`).
"""
from __future__ import annotations

import base64
import binascii
import copy
import json
import re
from typing import Callable
from urllib.parse import urlsplit

from ... import play
from ...household import Household
from . import Choice, Source

ROUTE = "/bandcamp/"
_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
_PATH = re.compile(re.escape(ROUTE) + r"([A-Za-z0-9_-]+)\.mp3")


def encode(track: dict) -> str:
    """The URL-safe token for a track: its page, and its ID (or its title,
    for a track with none, which is all `stream_url` can match on)."""
    body = {"p": track["page"]}
    if track.get("id"):
        body["i"] = track["id"]
    else:
        body["t"] = track.get("title") or ""
    raw = json.dumps(body, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(token: str) -> dict | None:
    """The track a token names, as `scene.bandcamp.stream_url` takes it; None
    for anything that isn't one of ours, or names a page that isn't http(s)."""
    if not _TOKEN.fullmatch(token):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        page = body["p"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        return None
    if not (isinstance(body, dict) and isinstance(page, str)
            and urlsplit(page).scheme in ("http", "https") and urlsplit(page).hostname):
        return None
    track = {"page": page}
    for key, field in (("i", "id"), ("t", "title")):
        if key in body:
            track[field] = body[key]
    return track


def token_of(request_path: str) -> str | None:
    """The token in a request path (`/bandcamp/<token>.mp3`, query ignored)."""
    m = _PATH.fullmatch(urlsplit(request_path).path)
    return m.group(1) if m else None


def _household_host(anchor: str | None = None) -> str:
    """This Mac's address as the household's speakers see it. Failing to
    reach the household is a ValueError saying so."""
    try:
        house = Household.load(anchor=anchor)
        return play.local_ip_for(house.groups[0].coordinator.ip)
    except Exception as exc:
        raise ValueError("couldn't reach the household to find this Mac's address "
                         f"as the speakers see it ({exc}); check you are on the same "
                         "LAN, or pass --anchor <ip>") from exc


def scene_bandcamp():
    """The module holding scene's Bandcamp search, tracks and `stream_url`; the
    one place that names it (lazy: scene stays out of `alarm list`'s imports)."""
    from ...scene import bandcamp
    return bandcamp


def _page_tracks(page: str) -> list[dict]:
    bandcamp = scene_bandcamp()
    try:
        return bandcamp.page_tracks(page)
    except Exception as exc:
        raise ValueError(f"couldn't read {page} from Bandcamp: {exc}") from exc


def _search(query: str) -> list[dict]:
    """Streamable tracks of the bands named exactly `query`, each with its band."""
    bandcamp = scene_bandcamp()
    try:
        out = []
        for band in [b for b in bandcamp.search(query) or [] if not b.get("is_label")][:3]:
            for t in bandcamp.tracks(band["item_url_root"], want=5):
                out.append(dict(t, band=band.get("name") or ""))
        return out
    except Exception as exc:
        raise ValueError(f"couldn't search Bandcamp: {exc}") from exc


class Bandcamp(Source):
    name = "bandcamp"
    title = "Bandcamp track (from this Mac)"
    needs_mac = True
    fallback = ("the Sonos chime: seen when this Mac refuses the request, not yet "
                "when it is off the network")
    takes_choice = True

    def __init__(self, host: Callable[[str | None], str] = _household_host,
                 page_tracks: Callable[[str], list[dict]] = _page_tracks,
                 search: Callable[[str], list[dict]] = _search):
        self.host, self._page_tracks, self._search = host, page_tracks, search
        self.anchor: str | None = None

    def choices(self, query: str = "") -> list[Choice]:
        if not query.strip():
            return []
        return [Choice(f"{t['page']}#{t['id']}" if t.get("id") else t["page"], t["title"],
                       " · ".join(x for x in (t.get("band"), t.get("album")) if x))
                for t in self._search(query.strip())]

    def bind(self, anchor: str | None) -> "Bandcamp":
        """The same source asking the speaker the CLI was pointed at."""
        bound = copy.copy(self)
        bound.anchor = anchor
        return bound

    def build(self, choice: str = "") -> tuple[str, str]:
        page, _, wanted = choice.strip().partition("#")
        parts = urlsplit(page)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"{choice!r} isn't a Bandcamp track page: use its URL, "
                             "with #<track id> for one track of a release "
                             "(`alarm sources bandcamp <band>` searches)")
        found = self._page_tracks(page)
        if wanted:
            found = [t for t in found if str(t.get("id")) == wanted]
        if not found:
            raise ValueError(f"no streamable track {'#' + wanted + ' ' if wanted else ''}"
                             f"on {page}")
        if len(found) > 1:
            raise ValueError(f"{page} has {len(found)} tracks: add #<track id> to pick one ("
                             + ", ".join(f"{t['title']} #{t.get('id')}" for t in found[:8])
                             + ")")
        track = found[0]
        from .. import server           # the agent's port: it imports this module for the token
        uri = f"http://{self.host(self.anchor)}:{server.PORT}{ROUTE}{encode(track)}.mp3"
        return uri, play.track_didl(track["title"], track.get("art"), url=uri)

    def owns(self, uri: str, metadata: str) -> bool:
        """Matches the route and port, not the address: it is the Mac's, and DHCP moves it."""
        from .. import server
        parts = urlsplit(uri)
        try:
            port = parts.port
        except ValueError:
            return False
        return (parts.scheme == "http" and port == server.PORT
                and token_of(parts.path) is not None and decode(token_of(parts.path)) is not None)
