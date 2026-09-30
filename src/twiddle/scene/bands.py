"""Who is this band, and what do they sound like?

A `BandProfile` is assembled by `Enricher`s, each adding what one source
knows. Three ship today:

  LookupEnricher     MusicBrainz -> Wikipedia / Discogs / Bandcamp (lookup.py)
  SpotifyEnricher    the Spotify artist, and a list of their tracks
  BandcampEnricher   their Bandcamp page: photo, tags (-> `genre`), songs
  TrackEnricher      song lists only, for a band known by identity alone: the
                     dataset builder skips tracks (2-4 more requests a band,
                     and only useful when you press play), so the app fetches
                     them for the band on screen

`BandBook` runs them off the UI thread and calls back as each finishes.

## Identity is the hard part, so it is never guessed silently

Local bands are often on no database at all, and when they are, the name
often belongs to someone bigger too ("Shape", "Eraser", "Sleeves" -- all on
one week's List). Development-mode Spotify strips popularity, followers and
genres from every artist, so a single same-named Spotify result says almost
nothing. `assess` therefore grades what was found:

  corroborated  pinned by hand; or MusicBrainz links this exact Spotify
                artist; or the name is unique on both Spotify and
                MusicBrainz *and* MusicBrainz/Bandcamp place the band in the
                Bay Area / California, where the venue is
  name_only     exactly one Spotify artist has this name, nothing more
  uncertain     several Spotify artists match -- press `m` to choose
  none          nothing on Spotify

The UI shows the grade and `why`, the same way `ArtistInfo.matched_by`
does for `np -i`.

## A billing is not always a band name

Venue calendars bill shows, not bands: "Mindi Abair Christmas Show",
"A Tribute To Grover Washington Jr.". When the billed name finds nothing,
`LookupEnricher` tries a few trimmed names (`name_candidates`) and keeps the
first one the music databases *uniquely* identify -- the catalog is the
judge, so a trim that finds nothing, or several artists, is never used.
That name becomes the profile's `alias`, which Spotify and Bandcamp then
search for; `why` says so. The answer is cached per billing (`aliases.json`).

## Threading

`lookup.py` throttles MusicBrainz with a module-global timestamp and keeps
its cache as a read-modify-write JSON file, so two concurrent `identify`
calls would break the 1 req/s limit and lose cache writes. Every lookup
therefore goes through one lane: a single thread on a priority queue, where
the band on screen jumps ahead of the lineup being prefetched. Spotify
calls are independent and run on a small pool.
"""
from __future__ import annotations

import itertools
import re
import queue
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

from .. import lookup, spotify_ops
from . import bandcamp, cache, genre

# Where the venues are. Origin strings come from MusicBrainz areas
# ("Oakland", "California") and Bandcamp locations ("Oakland, California").
REGION = ("california", "bay area", "oakland", "berkeley", "san francisco",
          "albany", "emeryville", "alameda", "richmond", "san jose", "santa cruz",
          "vallejo", "san leandro", "hayward", "fremont", "sacramento", ", ca")

CORROBORATED, NAME_ONLY, UNCERTAIN, NONE, PENDING, UNLOOKED = (
    "corroborated", "name_only", "uncertain", "none", "pending", "unlooked")

NOT_SIGNED_IN = ("not signed in -- run `twiddle spotify auth`; "
                 "Bandcamp tracks still play")
SEARCH_LIMIT = 10   # see spotify_cli.RESOLVE_LIMIT: small limits rank badly


@dataclass
class BandProfile:
    band: str
    # per enricher: "pending" | "done" | "error: ..."
    status: dict[str, str] = field(default_factory=dict)
    info: lookup.ArtistInfo | None = None
    lookup_candidates: list[dict] = field(default_factory=list)
    spotify_artist: dict | None = None      # the chosen artist, when there is one
    spotify_candidates: list[dict] = field(default_factory=list)
    tracks: list[dict] = field(default_factory=list)
    confidence: str = PENDING
    why: str = ""
    bandcamp: dict | None = None            # the chosen band (`bandcamp.choose`)
    bc_tracks: list[dict] = field(default_factory=list)
    alias: str | None = None                # a trimmed name the catalog knows
    searched: dict[str, str] = field(default_factory=dict)   # enricher -> name it used

    # Which fields each enricher fills: one answer can be carried to another
    # profile of the same band without dragging the others' along.
    FIELDS = {"lookup": ("info", "lookup_candidates", "alias"),
              "spotify": ("spotify_artist", "spotify_candidates", "tracks"),
              "bandcamp": ("bandcamp", "bc_tracks")}

    def adopt(self, name: str, other: "BandProfile", status: str = "done") -> None:
        """Take `other`'s answer from enricher `name`. `status` is what it now
        counts as: "done", or the builder's "kept" -- an old answer carried over
        that a later build should still try to refresh."""
        for attr in self.FIELDS[name]:
            setattr(self, attr, getattr(other, attr))
        if name in other.searched:
            self.searched[name] = other.searched[name]
        self.status[name] = status

    @property
    def search_name(self) -> str:
        return self.alias or self.band

    def genre(self) -> genre.Guess:
        """Sure when MusicBrainz tagged them, or the Bandcamp page is the one
        it links, or is local -- not when it was found by name alone."""
        bc = self.bandcamp or {}
        linked = self.info.links.get("bandcamp") if self.info else None
        sure = bool(self.info and self.info.genres) or near(bc.get("location")) or \
            (bool(linked) and bandcamp._host(linked) == bandcamp._host(bc.get("item_url_root")))
        return genre.for_band(self.bandcamp, self.info.genres if self.info else (), sure)

    def busy(self) -> bool:
        return any(v in ("pending", "running") for v in self.status.values())

    def links(self) -> dict[str, str]:
        out = dict(self.info.links) if self.info else {}
        if self.bandcamp and self.bandcamp.get("item_url_root"):
            out.setdefault("bandcamp", self.bandcamp["item_url_root"])
        if self.spotify_artist and self.spotify_artist.get("id"):
            out.setdefault("spotify",
                           f"https://open.spotify.com/artist/{self.spotify_artist['id']}")
        return out


def _spotify_id(url: str | None) -> str:
    return (url or "").rstrip("/").split("/artist/")[-1].split("?")[0] if url else ""


def linked_spotify_id(p: BandProfile) -> str:
    """The Spotify artist MusicBrainz links to this band, if it links one."""
    return _spotify_id(p.info.links.get("spotify")) if p.info else ""


def name_key(name: str | None) -> str:
    """A name reduced to letters and digits, for "is this the same name?".

    The List and Spotify punctuate differently -- measured: "Holybasil909"
    vs "HOLYBASIL_909", "M.D.C." vs "MDC" -- and those are the same name.
    """
    return re.sub(r"[^0-9a-z]", "", lookup.norm(name))


# Words that make a billing a show rather than a band. Only ever *removed*
# from a name that found nothing, and a trim only counts if the catalog
# confirms it, so a band really called "... Revue" loses nothing.
SHOW_WORDS = {"christmas", "xmas", "holiday", "holidays", "show", "tour", "live",
              "revue", "experience", "celebration", "tribute", "presents", "band",
              "trio", "quartet", "quintet", "sextet", "concert", "anniversary"}
_ABOUT = re.compile(r"^(?:an?\s+)?(?:tribute\s+to|salute\s+to|celebration\s+of|"
                    r"celebrating|the\s+music\s+of|the\s+songs\s+of)\s+(.+)$", re.IGNORECASE)
MAX_CANDIDATES = 3      # each is up to three database searches on the lookup lane


def name_candidates(billed: str) -> list[str]:
    """Trimmed names to try when a billing finds nothing, most likely first.

    "Mindi Abair Christmas Show" -> ["Mindi Abair"]
    "A Tribute To Grover Washington Jr." -> ["Grover Washington Jr."]
    "Cherronda G I'm Every Woman Show" -> [.. "Cherronda G"]
    A name with no show words gives nothing: it is already a band name.
    """
    out: list[str] = []
    about = _ABOUT.match(billed.strip())
    words = billed.split()
    showy = [i for i, w in enumerate(words) if w.lower().strip(".,:!") in SHOW_WORDS]
    if about:
        out.append(about.group(1))
    elif showy:
        out.append(" ".join(words[:showy[0]]))                           # up to the first
        out.append(" ".join(w for i, w in enumerate(words) if i not in showy))
        if len(words) >= 3:
            out.append(" ".join(words[:2]))
    keep: list[str] = []
    for c in out:
        c = c.strip(" -:,")
        ws = [w.lower().strip(".,:!") for w in c.split()]
        if not ws or all(w in SHOW_WORDS or w in ("a", "an", "the", "to", "of") for w in ws) \
                or any(w in SHOW_WORDS for w in ws) or _ABOUT.match(c):
            continue
        if lookup.norm(c) != lookup.norm(billed) and c not in keep:
            keep.append(c)
    return keep[:MAX_CANDIDATES]


def near(place: str | None) -> bool:
    place = (place or "").lower()
    return any(r in place for r in REGION)


def local(info: lookup.ArtistInfo | None) -> bool:
    return near(info.origin if info else None)


def assess(p: BandProfile, use_pins: bool = True) -> None:
    """Grade the Spotify identity from everything gathered so far.

    `use_pins=False` grades on evidence alone: the dataset builder publishes
    that, so one person's hand-made pins never travel with the data.
    """
    if p.status.get("spotify") == "idle":
        # Seeded from the dataset with no (usable) Spotify answer: the band on
        # screen is looked up when it is selected.
        p.confidence, p.why = UNLOOKED, "not looked up on Spotify yet"
        return
    pin = cache.pinned(p.band) if use_pins else None
    if pin is not None:
        if pin.get("spotify_id"):
            p.confidence, p.why = CORROBORATED, "chosen by you"
        else:
            p.confidence, p.why = NONE, "marked by you as not on Spotify"
        return
    if p.status.get("spotify") in ("pending", "running"):
        p.confidence, p.why = PENDING, "searching Spotify…"
        return
    failed = p.status.get("spotify", "")
    if not p.spotify_artist and failed.startswith("error: "):
        # Not "no such artist": the search never ran, or never answered.
        p.confidence, p.why = NONE, "Spotify: " + failed.removeprefix("error: ")
        return
    if not p.spotify_artist:
        if p.spotify_candidates:
            p.confidence = UNCERTAIN
            p.why = (f"{len(p.spotify_candidates)} Spotify artists are called this "
                     "-- press m to choose")
        else:
            p.confidence = NONE
            p.why = "no Spotify artist by this name -- m to search by hand"
        if p.alias:
            p.why += f" (searched as \u201c{p.alias}\u201d)"
        return
    sid = p.spotify_artist.get("id", "")
    mb_sid = linked_spotify_id(p)
    if mb_sid and mb_sid == sid:
        p.confidence, p.why = CORROBORATED, "MusicBrainz links this Spotify artist"
    elif mb_sid and mb_sid != sid:
        # MusicBrainz knows *a* Spotify artist for this band, and it is not
        # the one the name search found: the name search is the wrong band.
        p.confidence = UNCERTAIN
        p.why = "MusicBrainz links a different Spotify artist -- press m"
    elif p.info and p.info.matched_by.startswith("unique name match") and local(p.info):
        where = p.info.matched_by.split(" on ")[1] if " on " in p.info.matched_by \
            else "MusicBrainz"
        p.confidence = CORROBORATED
        p.why = (f"unique name on Spotify and on {where}, which places them "
                 f"in {p.info.origin}")
    else:
        p.confidence = NAME_ONLY
        p.why = "the only Spotify artist with this exact name -- not confirmed"
    if p.alias:
        p.why += f" (searched as \u201c{p.alias}\u201d)"


# ---- enrichers ----------------------------------------------------------------


class Enricher(Protocol):
    name: str
    serial: bool    # True: must run on the single lookup lane

    def enrich(self, p: BandProfile) -> None:
        """Fill in this enricher's fields on `p`. Raise on failure."""
        ...


class LookupEnricher:
    name = "lookup"
    serial = True

    def __init__(self, identify=None, bandcamp_ok: Callable[[], bool] | None = None):
        self._identify = identify
        self._bandcamp_ok = bandcamp_ok     # False: leave Bandcamp alone (rate-limited)

    def enrich(self, p: BandProfile) -> None:
        ok = self._bandcamp_ok() if self._bandcamp_ok else True
        identify = self._identify or (
            lambda name: lookup.identify(name, bandcamp_fallback=ok))
        r = identify(p.band)
        if not r.artist and not r.candidates:
            r = self._trimmed(p, identify, record_miss=ok) or r
        p.info = r.artist
        p.lookup_candidates = r.candidates

    @staticmethod
    def _trimmed(p: BandProfile, identify, record_miss: bool = True) -> lookup.Result | None:
        """The first trimmed name the databases uniquely identify, if any.

        Only a single identified artist counts: "several candidates" for a
        fragment like "Cherronda" is a guess, not an answer.
        """
        known = cache.alias(p.band)
        if known is not None:           # asked before: "" means none worked
            if not known:
                return None
            r = identify(known)
            p.alias = known if r.artist else None
            return r if r.artist else None
        for name in name_candidates(p.band):
            r = identify(name)
            if r.artist:
                p.alias = name
                cache.save_alias(p.band, name)
                return r
        if record_miss:                 # not when we skipped a source: "none worked" would be false
            cache.save_alias(p.band, "")    # a network failure raises before here
        return None


class SpotifyEnricher:
    """The Spotify artist for a band, and their tracks.

    Tracks come from `discover_cli._artist_tracks`, which filters by artist
    *id*: a plain search for a common word returns other artists' songs.
    """
    name = "spotify"
    serial = False

    def __init__(self, session_factory: Callable[[], object], use_pins: bool = True,
                 tracks: bool = True):
        self._session_factory = session_factory
        self._use_pins = use_pins       # the builder's are off: see `assess`
        self._tracks = tracks           # the builder's are off: see `TrackEnricher`
        self._sess = None
        self._lock = threading.Lock()

    def _pin(self, band: str) -> dict | None:
        return cache.pinned(band) if self._use_pins else None

    def session(self):
        with self._lock:
            if self._sess is None:
                self._sess = self._session_factory()
            return self._sess

    def enrich(self, p: BandProfile) -> None:
        try:
            self._enrich(p)
        except Exception as exc:
            if spotify_ops.not_signed_in(exc):
                raise RuntimeError(NOT_SIGNED_IN) from exc
            raise

    def _enrich(self, p: BandProfile) -> None:
        from ..discover_cli import _artist_tracks
        sess = self.session()
        pin = self._pin(p.band)
        if pin is not None:
            p.spotify_candidates = []
            if not pin.get("spotify_id"):
                p.spotify_artist, p.tracks = None, []
                return
            p.spotify_artist = sess.request("GET", f"/artists/{pin['spotify_id']}")
        else:
            term = p.searched["spotify"] = p.search_name
            hits = sess.search(term, "artist", limit=SEARCH_LIMIT)
            want = name_key(term)
            exact = [a for a in hits if name_key(a.get("name")) == want]
            linked = linked_spotify_id(p)
            chosen = None
            if linked:
                # MusicBrainz names the exact Spotify artist: that settles
                # it, even among several same-named ones ("Chuck Johnson").
                chosen = next((a for a in hits if a.get("id") == linked), None) \
                    or sess.request("GET", f"/artists/{linked}")
            elif len(exact) == 1:
                chosen = exact[0]
            p.spotify_artist = chosen
            # Only *same-named* artists are candidates. Loose hits for a band
            # Spotify doesn't have are noise (measured: "Girl Chow" returns
            # Girlschool and a Maori girls' choir); `m` searches by hand.
            p.spotify_candidates = exact if (chosen is None and len(exact) > 1) else []
        p.tracks = _artist_tracks(sess, p.spotify_artist) \
            if p.spotify_artist and self._tracks else []

    def wants_rerun(self, p: BandProfile) -> bool:
        """After the lookup lands: does MusicBrainz name a different artist,
        or did it find the band under a trimmed name we haven't searched?"""
        if self._pin(p.band) is not None:
            return False
        linked = linked_spotify_id(p)
        current = (p.spotify_artist or {}).get("id", "")
        return (bool(linked) and linked != current) or \
            bool(p.alias and p.searched.get("spotify") != p.alias)

    def search(self, term: str) -> list[dict]:
        """Artist candidates for the manual picker."""
        return self.session().search(term, "artist", limit=SEARCH_LIMIT)


class BandcampEnricher:
    """Their Bandcamp page -- where most bands on The List actually are.

    Chosen as `bandcamp.choose` says; a page MusicBrainz links wins, so this
    runs again if the lookup lands later with a different one.
    """
    name = "bandcamp"
    serial = False

    def __init__(self, search=bandcamp.search, tracks=bandcamp.tracks,
                 fetch_tracks: bool = True):
        self._search = search
        self._tracks = tracks
        self._fetch_tracks = fetch_tracks       # the builder's is off: see `TrackEnricher`

    @staticmethod
    def _linked(p: BandProfile) -> str | None:
        return p.info.links.get("bandcamp") if p.info else None

    def enrich(self, p: BandProfile) -> None:
        linked = self._linked(p)
        band = None
        # MusicBrainz's spelling too: its "Lænz" is Bandcamp's "Laenz".
        names = [p.alias, p.band, p.info.name if p.info else None]
        p.searched["bandcamp"] = p.search_name
        for name in dict.fromkeys(n for n in names if n):
            band = bandcamp.choose(self._search(name) or [], linked, is_local=near)
            if band:
                break
        if band is None and linked:
            band = {"name": p.band, "item_url_root": linked}     # linked, but named otherwise
        p.bandcamp = band
        p.bc_tracks = self._tracks(band["item_url_root"]) if band and self._fetch_tracks else []

    def wants_rerun(self, p: BandProfile) -> bool:
        linked = self._linked(p)
        have = (p.bandcamp or {}).get("item_url_root")
        return (bool(linked) and bandcamp._host(linked) != bandcamp._host(have)) or \
            bool(p.alias and p.searched.get("bandcamp") != p.alias)


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
