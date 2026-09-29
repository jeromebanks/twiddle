"""A `BandProfile` <-> a dataset band record.

The dataset (`dataset.py`) holds plain JSON; the app and the builder work in
`BandProfile`s. This is the one place that maps between them, including the
parts JSON has no type for: `ArtistInfo.album` (a nested dataclass) and
`genre.Guess` (a Counter and a set).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import fields

from .. import lookup
from . import genre
from .bands import BandProfile, assess
from .dataset import iso

ENRICHERS = ("lookup", "spotify", "bandcamp")     # what a build runs
SEEDED = ENRICHERS + ("tracks",)                   # what the app may still run per band


def guess_to_dict(g: genre.Guess | None) -> dict | None:
    if not g:
        return None
    return {"scores": dict(g.scores), "tags": list(g.tags),
            "sources": sorted(g.sources), "sure": g.sure}


def guess_from_dict(d: dict | None) -> genre.Guess | None:
    if not d or not d.get("scores"):
        return None
    return genre.Guess(scores=Counter(d["scores"]), tags=list(d.get("tags") or []),
                       sources=set(d.get("sources") or ()), sure=bool(d.get("sure", True)))


def _pick(cls, d: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def info_from_dict(d: dict | None) -> lookup.ArtistInfo | None:
    if not d:
        return None
    d = _pick(lookup.ArtistInfo, d)
    album = d.pop("album", None)
    return lookup.ArtistInfo(**d, album=lookup.AlbumInfo(**_pick(lookup.AlbumInfo, album))
                             if album else None)


def to_record(p: BandProfile, *, updated_at: float | None, guess: genre.Guess | None) -> dict:
    """The record for an enriched profile. `updated_at` is when the enrichers ran.

    Spotify identity is only *graded* when Spotify answered: a build that had
    no Spotify (signed out, `--no-spotify`, an error) must not publish "not on
    Spotify" for a band it never asked about.
    """
    checked = p.status.get("spotify") == "done"
    return {
        "name": p.band,
        "updated_at": iso(updated_at),
        "status": dict(p.status),
        "identifiers": {
            "mbid": p.info.mbid if p.info else "",
            "spotify_id": (p.spotify_artist or {}).get("id", ""),
            "bandcamp": (p.bandcamp or {}).get("item_url_root", ""),
        },
        "info": p.info.to_dict() if p.info else None,
        "lookup_candidates": p.lookup_candidates,
        "spotify_artist": p.spotify_artist,
        "spotify_candidates": p.spotify_candidates,
        "tracks": p.tracks,
        "bandcamp": p.bandcamp,
        "bc_tracks": p.bc_tracks,
        "alias": p.alias,
        "searched": dict(p.searched),
        "confidence": p.confidence if checked else "pending",
        "why": p.why if checked else "Spotify was not checked in this build",
        "genre": guess_to_dict(guess),
    }


def minimal_record(name: str, guess: genre.Guess | None) -> dict:
    """A billed band nobody has enriched: its name and whatever the caches say
    it sounds like, so the "Sounds like" column has an answer and the app knows
    the rest is still to look up."""
    return {"name": name, "updated_at": None, "status": {}, "identifiers": {},
            "confidence": "pending", "why": "", "genre": guess_to_dict(guess)}


def enriched(rec: dict | None, names: tuple[str, ...] = ENRICHERS) -> bool:
    """Has every enricher in `names` answered for this record?"""
    st = (rec or {}).get("status") or {}
    return all(st.get(n) == "done" for n in names)


def from_record(rec: dict, *, pinned: Callable[[str], dict | None] | None = None,
                names: tuple[str, ...] = SEEDED) -> BandProfile:
    """The profile as the dataset knows it, ready to seed a `BandBook`.

    Every enricher that did not finish `done` is `idle` (the dataset never has
    `tracks` done: song lists are fetched for the band on screen): the app runs it, for
    that one band, when the band is put on screen. So is Spotify whenever this
    person pinned a different artist than the dataset's name-search picked
    (or pinned "not on Spotify" where it found one) -- the dataset is graded
    without pins, and the pin must win.
    """
    p = BandProfile(band=rec.get("name", ""))
    st = rec.get("status") or {}
    p.status = {n: "done" if st.get(n) == "done" else "idle" for n in names}
    p.info = info_from_dict(rec.get("info"))
    p.lookup_candidates = list(rec.get("lookup_candidates") or [])
    p.spotify_artist = rec.get("spotify_artist")
    p.spotify_candidates = list(rec.get("spotify_candidates") or [])
    p.tracks = list(rec.get("tracks") or [])
    p.bandcamp = rec.get("bandcamp")
    p.bc_tracks = list(rec.get("bc_tracks") or [])
    p.alias = rec.get("alias")
    p.searched = dict(rec.get("searched") or {})
    pin = pinned(p.band) if pinned else None
    if pin is not None and "spotify" in p.status:
        if (pin.get("spotify_id") or "") != (p.spotify_artist or {}).get("id", ""):
            # The dataset's pick is not who this person chose: forget it, so
            # nothing (TrackEnricher, the badge) works from the wrong artist
            # while the pinned one is looked up.
            p.status["spotify"] = "idle"
            p.spotify_artist, p.spotify_candidates, p.tracks = None, [], []
    assess(p)
    return p
