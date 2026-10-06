"""A Spotify track, album or playlist, played through Sonos's own Spotify link:
the speaker fetches it from Spotify itself, so neither this Mac nor the relay
has any part in it once the alarm is set.

`--source spotify:<choice>`, where a choice is `playlist:<id>` (or
`album` / `track`; a full `spotify:playlist:<id>` URI also works) or a whole
open.spotify.com link or the open.spotify.com link for one. `alarm sources
spotify <words>` searches Spotify with twiddle's own sign-in for choices.

The URI and DIDL copy the shape of the household's existing Sonos-Spotify
alarms (`tests/fixtures/alarms_listalarms.xml`): `x-rincon-cpcontainer`,
`sid=12`, and `sn=`, the serial of the Spotify account linked in the Sonos
app. The album and track forms (and the item-ID prefixes and flags behind them)
follow what Sonos uses for them elsewhere; no speaker has been seen to
accept them from here yet, which the milestone demo covers.

The account is read, never assumed: first from the Spotify favourites already
on the speakers (their URIs carry `sn=`, so no existing alarm is needed), then
from an existing Spotify alarm. With neither, `build` says no account is
linked rather than make an alarm that can't play. Nothing here writes to a
speaker.

This source doesn't *own* the alarms it builds: they are Sonos's own Spotify
link, which `alarms/soundsource.py` already names `spotify_sonos`.
"""
from __future__ import annotations

import copy
import re
from html import unescape
from typing import Callable
from ... import devices, play
from ...household import Household
from .. import clock
from . import Choice, Source

SID = "12"                                  # Sonos's service ID for Spotify
_DESC = "SA_RINCON3079_X_#Svc3079-0-Token"  # the same service: 12 * 256 + 7
_KINDS = ("playlist", "album", "track")

# kind -> (item-ID prefix, URI scheme, flags, DIDL class)
_SHAPE = {
    "playlist": ("00060000", "x-rincon-cpcontainer", "0", "object.container.playlistContainer"),
    "album": ("00040000", "x-rincon-cpcontainer", "0", "object.container.album.musicAlbum"),
    "track": ("00032020", "x-sonos-spotify", "8224", "object.item.audioItem.musicTrack"),
}

_SPOTIFY_URI = re.compile(r"^(?:spotify:)?(playlist|album|track):([A-Za-z0-9]+)$")
_SPOTIFY_URL = re.compile(r"^https?://open\.spotify\.com/(?:intl-\w+/)?"
                          r"(playlist|album|track)/([A-Za-z0-9]+)/?(?:[?#].*)?$")
_SN = re.compile(r"[?&]sn=(\d+)")
_FAVOURITES = ('<ObjectID>FV:2</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag>'
               "<Filter>*</Filter><StartingIndex>0</StartingIndex>"
               "<RequestedCount>200</RequestedCount><SortCriteria></SortCriteria>")


def parse_choice(choice: str) -> tuple[str, str]:
    """`(kind, id)` from `<kind>:<id>` (so `--source spotify:playlist:<id>`), a full
    Spotify URI or an open.spotify.com link."""
    text = choice.strip()
    m = _SPOTIFY_URI.match(text) or _SPOTIFY_URL.match(text)
    if not m:
        raise ValueError(f"{choice!r} isn't a Spotify track, album or playlist: use "
                         "playlist:<id> or its open.spotify.com link "
                         "(`alarm sources spotify <words>` searches)")
    return m.group(1), m.group(2)


def program(kind: str, ident: str, sn: str, title: str, art: str = "") -> tuple[str, str]:
    """`(ProgramURI, ProgramMetaData)` for one Spotify item on account `sn`."""
    prefix, scheme, flags, klass = _SHAPE[kind]
    ref = f"spotify%3a{kind}%3a{ident}"        # Sonos writes the colons lower-case; the ID keeps its case
    item = f"{prefix}{ref}"
    uri = (f"x-sonos-spotify:{ref}" if kind == "track" else f"{scheme}:{item}")
    uri += f"?sid={SID}&flags={flags}&sn={sn}"
    didl = (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        f'<item id="{item}" parentID="-1" restricted="true">'
        f"<dc:title>{play._esc(title)}</dc:title>"
        f"<upnp:class>{klass}</upnp:class>"
        f'<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">{_DESC}</desc>'
        + (f"<upnp:albumArtURI>{play._esc(art)}</upnp:albumArtURI>" if art else "")
        + "</item></DIDL-Lite>")
    return uri, didl


def account_from_text(text: str) -> str | None:
    """The Spotify account serial in some Sonos XML or a URI: the `sn=` of the
    first Spotify (`sid=12`) item. The text may be escaped once or twice."""
    for _ in range(3):
        for m in re.finditer(r"[^\s\"'<>]*sid=12(?![0-9])[^\s\"'<>]*", text):
            sn = _SN.search(m.group(0))
            if sn:
                return sn.group(1)
        text = unescape(text)
    return None


def favourites_account(ip: str) -> str | None:
    """The linked account's serial from the speaker's Sonos favourites (a
    read-only `Browse`); None when none is a Spotify one."""
    return account_from_text(devices.soap(
        ip, "ContentDirectory", "Browse", _FAVOURITES,
        path="/MediaServer/ContentDirectory/Control"))


def alarms_account(alarms) -> str | None:
    """The serial an existing Spotify alarm uses: the last resort."""
    return next((sn for a in alarms if (sn := account_from_text(a.program_uri))), None)


def _household_account(anchor: str | None = None) -> str | None:
    """The default lookup: ask the household's first speaker (`anchor`, the
    CLI's `--anchor`, when given), favourites first. Any failure is a
    ValueError saying what couldn't be read."""
    try:
        house = Household.load(anchor=anchor)
        ip = house.groups[0].coordinator.ip
    except Exception as exc:
        raise ValueError("couldn't reach the household to find the linked Spotify "
                         f"account ({exc}); check you are on the same LAN, or pass "
                         "--anchor <ip>") from exc
    try:
        found = favourites_account(ip)
    except Exception:
        found = None
    if found:
        return found
    try:
        return alarms_account(clock.list_alarms(ip, tolerant=True).alarms)
    except Exception as exc:
        raise ValueError(f"couldn't read the alarms from {ip} to find the linked "
                         f"Spotify account: {exc}") from exc


def _search(query: str) -> list[dict]:
    from ... import spotify_ops
    try:
        sess = spotify_ops.session()
        return [dict(item, _kind=kind) for kind in _KINDS for item in sess.search(query, kind, 5)]
    except spotify_ops.PlaybackError as exc:
        raise ValueError(f"{exc.message}: {exc.hint}" if exc.hint else exc.message) from exc
    except Exception as exc:
        raise ValueError(f"couldn't search Spotify: {exc}") from exc


def _lookup(kind: str, ident: str) -> tuple[str, str]:
    """`(title, cover URL)` of one item from Spotify's Web API, best effort: a
    link with no search behind it still gets its real name when signed in, else
    a generic one."""
    try:
        from ... import spotify_ops
        data = spotify_ops.session().request("GET", f"/{kind}s/{ident}") or {}
        images = data.get("images") or (data.get("album") or {}).get("images") or [{}]
        return data.get("name") or "", images[0].get("url", "")
    except Exception:
        return "", ""


class Spotify(Source):
    name = "spotify"
    title = "Spotify (Sonos's own link)"
    needs_mac = False
    fallback = "the Sonos chime (assumed, not yet verified on a speaker)"
    takes_choice = True

    def __init__(self, account: Callable[[str | None], str | None] = _household_account,
                 search: Callable[[str], list[dict]] = _search,
                 lookup: Callable[[str, str], tuple[str, str]] = _lookup):
        self.account, self._search, self._lookup = account, search, lookup
        self.anchor: str | None = None
        self._seen: dict[str, tuple[str, str]] = {}      # id -> (title, art), from a search

    def choices(self, query: str = "") -> list[Choice]:
        if not query.strip():
            return []
        out = []
        for item in self._search(query.strip()):
            kind, ident = item.get("_kind", ""), item.get("id", "")
            if not (kind and ident):
                continue
            art = ((item.get("images") or [{}])[0]).get("url", "")
            self._seen[ident] = (item.get("name", ""), art)
            by = ", ".join(a.get("name", "") for a in item.get("artists", []))
            owner = (item.get("owner") or {}).get("display_name", "")
            out.append(Choice(f"{kind}:{ident}", item.get("name", ""),
                              f"{kind}" + (f" · {by or owner}" if by or owner else "")))
        return out

    def bind(self, anchor: str | None) -> "Spotify":
        """The same source asking the speaker the CLI was pointed at."""
        bound = copy.copy(self)
        bound.anchor = anchor
        return bound

    def build(self, choice: str = "") -> tuple[str, str]:
        kind, ident = parse_choice(choice)
        sn = self.account(self.anchor)
        if not sn:
            raise ValueError("no Spotify account is linked to this household's Sonos: "
                             "add Spotify in the Sonos app first (twiddle won't build "
                             "an alarm that can't play)")
        title, art = self._seen.get(ident) or self._lookup(kind, ident)
        return program(kind, ident, sn, title or f"Spotify {kind}", art)
