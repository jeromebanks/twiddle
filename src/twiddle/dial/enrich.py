"""Who is this artist? One lane for `lookup.identify`, newest request wins.

MusicBrainz allows one request a second and `lookup` is not thread-safe
(the same constraint `scene/bands.py` works around), so everything goes
through a single thread. Only the station on screen asks, and scrolling
past five stations must not queue five lookups: a request replaces any
that has not started yet.

Results are kept per (artist, album, song) for the session; `lookup`'s own
month-long disk cache makes a repeat across sessions cheap too.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import lookup
from ..stations import NowPlaying
from . import art


@dataclass
class ArtistCard:
    query: tuple
    info: lookup.ArtistInfo | None = None
    candidates: list[dict] = field(default_factory=list)
    photo_url: str | None = None
    cover_url: str | None = None     # from the album MusicBrainz named, if any
    error: str | None = None


def query_of(np: NowPlaying | None) -> tuple | None:
    if np is None or not np.artist:
        return None
    return (np.artist, np.album, np.song)


class Enricher:
    def __init__(self, on_done: Callable[[ArtistCard], None], *,
                 identify: Callable = lookup.identify,
                 photo: Callable[..., str | None] = art.artist_photo):
        self.on_done = on_done
        self._identify = identify
        self._photo = photo
        self.cards: dict[tuple, ArtistCard] = {}
        self._want: tuple[tuple, NowPlaying] | None = None
        self._cond = threading.Condition()
        self._closed = False
        self._thread: threading.Thread | None = None

    def get(self, np: NowPlaying | None) -> ArtistCard | None:
        """The card if known; otherwise ask for it and return None for now."""
        q = query_of(np)
        if q is None:
            return None
        if q in self.cards:
            return self.cards[q]
        with self._cond:
            self._want = (q, np)
            self._cond.notify()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="dial-lookup",
                                                daemon=True)
                self._thread.start()
        return None

    def run_one(self, q: tuple, np: NowPlaying) -> ArtistCard:
        card = ArtistCard(q)
        try:
            r = self._identify(np.artist, np.album, np.song,
                               mb_artist_id=np.mb_artist_id,
                               mb_release_group_id=np.mb_release_group_id)
            card.info, card.candidates = r.artist, r.candidates
        except Exception as exc:     # LookupFailed, or a network error beneath it
            card.error = str(exc)
        if card.info:
            card.photo_url = self._photo(card.info, np.artist)
            if card.info.album:
                card.cover_url = art.cover_art_archive(card.info.album.mbid)
        self.cards[q] = card
        return card

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._want is None and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                (q, np), self._want = self._want, None
            if q in self.cards:
                continue
            self.on_done(self.run_one(q, np))

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify()
