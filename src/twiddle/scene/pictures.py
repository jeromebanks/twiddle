"""Which picture to show for a band.

(A venue's is simpler: the hand-picked `Venue.icon`, else a tile.)

Only *which URL*, and where it came from, lives here; fetching, caching and
generated tiles are `dial.art`'s, shared so a band heard on the radio and
seen on The List costs one download.

In order: their Bandcamp photo (the page `BandcampEnricher` chose), then
Spotify's image when there is a chosen Spotify artist -- confirmed (✓) or
the only one with that name (~), captioned so the two can be told apart --
then Wikipedia's lead image, else None and the UI draws a monogram. With
several same-named Spotify artists (?) there is no chosen one to show.

Wikipedia is asked over the network here, so call this from a worker.
"""
from __future__ import annotations

import re

from ..dial import art
from ..scenespec.band import CORROBORATED, NAME_ONLY, BandProfile


def spotify_image(artist: dict | None) -> str | None:
    """The artist's largest image. Spotify lists them biggest first."""
    images = (artist or {}).get("images") or []
    return images[0].get("url") if images else None


def bandcamp_image(band: dict | None) -> str | None:
    img = (band or {}).get("img")
    # `_23` is Bandcamp's 300px size; `_16` is 700px
    return re.sub(r"_\d+\.jpg$", "_16.jpg", img) if img else None


def signature(p: BandProfile) -> tuple:
    """What the chosen photo depends on: when this changes, choose again."""
    return (p.band, p.info.name if p.info else None,
            (p.spotify_artist or {}).get("id"), p.confidence,
            (p.bandcamp or {}).get("img"))


def band_photo(p: BandProfile) -> tuple[str | None, str]:
    """(url, where it came from) -- (None, "") when there is none."""
    url = bandcamp_image(p.bandcamp)
    if url:
        return url, "Bandcamp"
    if p.confidence in (CORROBORATED, NAME_ONLY):
        url = spotify_image(p.spotify_artist)
        if url:
            return url, "Spotify" if p.confidence == CORROBORATED else "Spotify (name match)"
    if p.info:
        url = art.wikipedia_image(p.info.links.get("wikipedia"))
        if url:
            return url, "Wikipedia"
    return None, ""
