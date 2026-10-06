"""Where an alarm's sound comes from, read from its `ProgramURI` alone.

A registered source (`alarms/sources/`) is asked first: it recognises the
URIs it builds. Otherwise the URI is matched against the Sonos service shapes
below: its scheme, the `sid=` in its query and the start of its item ID, all
three exactly. Those shapes are conventions observed on this household's
speakers, not a table Sonos publishes; anything else is `unknown` with the
URI's scheme. The alarm's title is never consulted, so a station called
"TuneIn" on some other service is not mistaken for TuneIn.

Pure: nothing here talks to a speaker or the network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote

from . import sources

UNKNOWN = "unknown"


@dataclass(frozen=True)
class SoundSource:
    key: str        # `sound_source` in `alarm list --json`
    text: str       # `sound_source_text`, and the column


@dataclass(frozen=True)
class _Shape:
    scheme: str
    sid: str | None             # the query's `sid`, or None for a URI that has none
    item: re.Pattern            # the unquoted item ID, from its start
    source: SoundSource


_SHAPES = (
    _Shape("x-rincon-buzzer", None, re.compile(r"\d+\Z"),
           SoundSource("sonos_chime", "Sonos chime")),
    _Shape("x-sonosapi-radio", "303", re.compile(r"sonos:"),
           SoundSource("sonos_radio", "Sonos Radio")),
    _Shape("x-sonosapi-stream", "303", re.compile(r"ihr:"),
           SoundSource("iheart_sonos_radio", "iHeart through Sonos Radio")),
    _Shape("x-sonosapi-stream", "333", re.compile(r"s\d+\Z"),
           SoundSource("tunein", "TuneIn")),
    _Shape("x-rincon-cpcontainer", "12", re.compile(r"[0-9a-f]{8}spotify:", re.I),
           SoundSource("spotify_sonos", "Spotify through the Sonos app's link")),
    _Shape("x-sonos-spotify", "12", re.compile(r"spotify:track:"),
           SoundSource("spotify_sonos", "Spotify through the Sonos app's link")),
)


def _parts(uri: str) -> tuple[str, str, str | None]:
    """`(scheme, unquoted item ID, sid)`; sid is None when there is none, and
    "" when there is more than one, which no shape accepts."""
    scheme, _, rest = uri.partition(":")
    item, _, query = rest.partition("?")
    sids = parse_qs(query, keep_blank_values=True).get("sid")
    sid = None if sids is None else sids[0] if len(sids) == 1 else ""
    return scheme.lower(), unquote(item), sid


def classify(uri: str, metadata: str = "") -> SoundSource:
    """Where an alarm with this `ProgramURI` (and `ProgramMetaData`, for the
    registered sources only) gets its sound."""
    source = sources.recognise(uri, metadata)
    if source is not None:
        return SoundSource(*source.sound_source())
    scheme, item, sid = _parts(uri)
    for shape in _SHAPES:
        if shape.scheme == scheme and shape.sid == sid and shape.item.match(item):
            return shape.source
    return SoundSource(UNKNOWN, f"unknown ({scheme})" if scheme else "unknown (no URI)")
