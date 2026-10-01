"""The client's book of bands: profiles seeded from the dataset, and the
background work for the band on screen (song lists today).

`on_update(profile)` is called from worker threads; a UI must marshal it onto
its own thread (Textual: `app.call_from_thread`).
"""
from __future__ import annotations

import itertools
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable

from .. import lookup, spotify_ops
from ..scenedata.bands import NOT_SIGNED_IN, Enricher, SpotifyEnricher, assess
from ..scenespec.band import BandProfile
from .. import bandcamp


class TrackEnricher:
    """Song lists for a band whose identity is already known.

    `scene build` records who a band is on Spotify and Bandcamp but not their
    songs: a list costs 2-4 more requests a band, across some 1,100 bands a
    build, and is only used when someone presses play -- which needs the
    network anyway. So the band on screen gets its lists here, as it did
    before the dataset existed. Reruns when identity lands later.
    """
    name = "tracks"
    serial = False
    self_rerun = True   # identity can land while this runs: check again when done

    def __init__(self, spotify: SpotifyEnricher, bc_tracks=bandcamp.tracks):
        self._spotify = spotify
        self._bc_tracks = bc_tracks

    @staticmethod
    def _key(p: BandProfile) -> str:
        return f"{(p.spotify_artist or {}).get('id', '')}|{(p.bandcamp or {}).get('item_url_root', '')}"

    def enrich(self, p: BandProfile) -> None:
        from ..discover_cli import _artist_tracks
        p.searched["tracks"] = self._key(p)
        failure: Exception | None = None
        if p.bandcamp and p.bandcamp.get("item_url_root") and not p.bc_tracks:
            root = p.bandcamp["item_url_root"]
            try:
                got = self._bc_tracks(root)
                if (p.bandcamp or {}).get("item_url_root") == root:   # not if it changed meanwhile
                    p.bc_tracks = got
            except Exception as exc:
                failure = exc
        if p.spotify_artist and not p.tracks:
            artist = p.spotify_artist.get("id")
            try:
                got = _artist_tracks(self._spotify.session(), p.spotify_artist)
                if (p.spotify_artist or {}).get("id") == artist:      # a corrected artist's stay
                    p.tracks = got
            except Exception as exc:
                failure = failure or (RuntimeError(NOT_SIGNED_IN)
                                      if spotify_ops.not_signed_in(exc) else exc)
        if failure is not None:
            raise failure

    def wants_rerun(self, p: BandProfile) -> bool:
        missing = (bool(p.spotify_artist) and not p.tracks) or \
            (bool(p.bandcamp) and not p.bc_tracks)
        return missing and self._key(p) != p.searched.get("tracks")


# ---- the book -------------------------------------------------------------------


class BandBook:
    """Profiles by band, enriched in the background, reported via `on_update`.

    `on_update(profile)` is called from worker threads; a UI must marshal it
    onto its own thread (Textual: `app.call_from_thread`).
    """

    def __init__(self, enrichers: list[Enricher],
                 on_update: Callable[[BandProfile], None] = lambda p: None,
                 spotify_workers: int = 3):
        self.enrichers = enrichers
        self.on_update = on_update
        # band -> its profile as the dataset has it (`refresh` uses this)
        self.reseed: Callable[[str], BandProfile | None] | None = None
        self._deferred: dict[str, BandProfile] = {}
        self.profiles: dict[str, BandProfile] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=spotify_workers,
                                        thread_name_prefix="scene-spotify")
        self._lane: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._lane_thread = threading.Thread(target=self._run_lane, daemon=True,
                                             name="scene-lookup")
        self._lane_thread.start()

    def seed(self, profiles: list[BandProfile]) -> None:
        """Register profiles already known (the dataset's) without scheduling
        anything. An enricher marked `idle` on one runs only when that band is
        fetched `urgent`ly -- the one on screen -- never for the lot."""
        identity = ("lookup", "spotify", "bandcamp")
        with self._lock:
            for p in profiles:
                key = lookup.norm(p.band)
                old = self.profiles.get(key)
                if old is not None:
                    # A lookup in flight finishes on its own object. And an
                    # identity answered here that the record lacks (or that a
                    # pin overrode) is worth more than the record.
                    if old.busy():
                        self._deferred[key] = p          # applied when its lookup ends
                        continue
                    # Keep what was looked up here that the record lacks, but
                    # take the record's other answers: a partial checkpoint
                    # with a corrected Spotify artist still corrects it.
                    for n in identity:
                        if old.status.get(n) == "done" and p.status.get(n) != "done":
                            p.adopt(n, old)
                    assess(p)
                    self._carry_tracks(old, p)
                self.profiles[key] = p

    @staticmethod
    def _carry_tracks(old: BandProfile, new: BandProfile) -> None:
        """Song lists fetched here stay, but only for the identity they were
        fetched for: a corrected artist in the new record drops the old lists."""
        same_spotify = (old.spotify_artist or {}).get("id") == (new.spotify_artist or {}).get("id")
        same_bandcamp = (old.bandcamp or {}).get("item_url_root") == \
            (new.bandcamp or {}).get("item_url_root")
        if old.tracks and same_spotify and new.spotify_artist:
            new.tracks = old.tracks
        if old.bc_tracks and same_bandcamp and new.bandcamp:
            new.bc_tracks = old.bc_tracks
        if old.status.get("tracks") == "done" and same_spotify and same_bandcamp:
            new.status["tracks"] = "done"

    def get(self, band: str, urgent: bool = True) -> BandProfile:
        """The profile as known now; missing enrichments are scheduled.

        `urgent` is for the band on screen: it jumps the lookup lane ahead of
        prefetched lineup members, and wakes any `idle` enricher on a seeded
        profile.
        """
        key = lookup.norm(band)
        with self._lock:
            p = self.profiles.get(key)
            fresh = p is None
            if fresh:
                p = BandProfile(band=band)
                for e in self.enrichers:
                    p.status[e.name] = "pending"
                self.profiles[key] = p
        if fresh:
            assess(p)
            for e in self.enrichers:
                self._schedule(e, p, urgent)
        elif urgent:
            self._wake(p)
            self._bump(p)
        return p

    def refresh(self, band: str) -> BandProfile:
        """Forget and re-enrich -- after a pin changes, say.

        A seeded band is re-seeded (its Spotify answer went stale with the
        pin, the rest is still the dataset's) rather than looked up afresh.
        """
        key = lookup.norm(band)
        with self._lock:
            old = self.profiles.pop(key, None)
            self._deferred.pop(key, None)       # captured before the pin changed
            if old is not None and self.reseed is not None:
                fresh = self.reseed(old.band)
                if fresh is not None:
                    self.profiles[key] = fresh
        return self.get(band)

    def _wake(self, p: BandProfile) -> None:
        for e in self.enrichers:
            with self._lock:
                if p.status.get(e.name) != "idle":
                    continue
                p.status[e.name] = "pending"
            assess(p)
            self._schedule(e, p, urgent=True)

    def _schedule(self, e: Enricher, p: BandProfile, urgent: bool) -> None:
        if e.serial:
            self._lane.put((0 if urgent else 1, next(self._seq), e, p))
        else:
            self._pool.submit(self._run, e, p)

    def _bump(self, p: BandProfile) -> None:
        # Re-queue at high priority; `_run` skips work already done.
        for e in self.enrichers:
            if e.serial and p.status.get(e.name) == "pending":
                self._lane.put((0, next(self._seq), e, p))

    def _run_lane(self) -> None:
        while True:
            _prio, _n, e, p = self._lane.get()
            if e is None:
                return
            self._run(e, p)

    def _run(self, e: Enricher, p: BandProfile) -> None:
        with self._lock:
            if p.status.get(e.name) != "pending":
                return
            p.status[e.name] = "running"
        try:
            e.enrich(p)
            p.status[e.name] = "done"
        except Exception as exc:   # one source failing must not break the rest
            p.status[e.name] = f"error: {getattr(exc, 'message', None) or exc}"
        # One enricher's answer can change another's: MusicBrainz landing
        # after Spotify may name a different Spotify artist. Redo that one.
        for other in self.enrichers:
            rerun = getattr(other, "wants_rerun", None)
            with self._lock:
                state = p.status.get(other.name, "")
                # A song-list fetch that failed for one provider may still have
                # a newly identified one to try; `wants_rerun` says when the
                # identity changed, so an unchanged one is never retried.
                retry_failed = getattr(other, "self_rerun", False) and state.startswith("error")
                if (other is e and not getattr(e, "self_rerun", False)) \
                        or (state != "done" and not retry_failed) or not (rerun and rerun(p)):
                    continue
                p.status[other.name] = "pending"
            self._schedule(other, p, urgent=True)
        assess(p)
        newer = self._take_deferred(p)
        try:
            self.on_update(newer or p)
        except Exception:
            pass

    def _take_deferred(self, p: BandProfile) -> BandProfile | None:
        """A dataset record that arrived while `p` was being looked up waits
        for the lookup to finish, then goes through `seed` like any other."""
        key = lookup.norm(p.band)
        with self._lock:
            if p.busy() or self.profiles.get(key) is not p:
                return None
            waiting = self._deferred.pop(key, None)
        if waiting is None:
            return None
        self.seed([waiting])
        fresh = self.profiles.get(key)
        if fresh is not None and fresh is not p:
            self._wake(fresh)       # what the new record lacks (song lists), as selecting it would
        return fresh

    def close(self) -> None:
        self._lane.put((-1, -1, None, None))
        self._pool.shutdown(wait=False, cancel_futures=True)
