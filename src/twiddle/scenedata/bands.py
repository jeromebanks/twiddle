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

The client's `BandBook` (scene/book.py) runs them off the UI thread.

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

import re
import threading
from collections.abc import Callable
from typing import Protocol

from .. import bandcamp as site, lookup, spotify_ops
from . import bandcamp, cache
from ..scenespec import genre
from ..scenespec.band import (CORROBORATED, NAME_ONLY, NONE, PENDING, UNCERTAIN, UNLOOKED,
                              BandProfile)

# Where the venues are. Origin strings come from MusicBrainz areas
# ("Oakland", "California") and Bandcamp locations ("Oakland, California").
REGION = ("california", "bay area", "oakland", "berkeley", "san francisco",
          "albany", "emeryville", "alameda", "richmond", "san jose", "santa cruz",
          "vallejo", "san leandro", "hayward", "fremont", "sacramento", ", ca")

NOT_SIGNED_IN = ("not signed in -- run `twiddle spotify auth`; "
                 "Bandcamp tracks still play")
SEARCH_LIMIT = 10   # see spotify_cli.RESOLVE_LIMIT: small limits rank badly


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
# Billings that are an event, not an act: nothing to look up, so never a dead letter.
# Measured against the real queue (2026-10): "Private Event", "Membership Meeting",
# "Karaoke Tuesday", "Bachata Nightz", "Salsa Crazy Mondays". Only consulted for a
# billing that found nothing, so a band really called "Open Mic" costs us nothing.
EVENT = re.compile(r"\b(?:private (?:event|party)|closed for|membership meeting|open mic|karaoke|"
                   r"trivia|bingo|jam session|storytelling|workshop|nights?|nightz|nite|"
                   r"(?:mon|tues|wednes|thurs|fri|satur|sun)days)\b|\bvs\.?\s", re.IGNORECASE)


def non_band(name: str) -> bool:
    """True when this billing reads as an event (a night, a meeting), not a band."""
    return bool(EVENT.search(name))


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


def assess(p: BandProfile) -> None:
    """Grade the Spotify identity from everything gathered so far, on evidence
    alone: one person's hand-made pins never travel with the data (the client
    overlays its own, `scene/book.py`)."""
    if p.status.get("spotify") == "idle":
        # Seeded from the dataset with no (usable) Spotify answer: the band on
        # screen is looked up when it is selected.
        p.confidence, p.why = UNLOOKED, "not looked up on Spotify yet"
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


def genre_of(p: BandProfile) -> genre.Guess:
    """Sure when MusicBrainz tagged them, or the Bandcamp page is the one
    it links, or is local -- not when it was found by name alone."""
    bc = p.bandcamp or {}
    linked = p.info.links.get("bandcamp") if p.info else None
    sure = bool(p.info and p.info.genres) or near(bc.get("location")) or \
        (bool(linked) and site._host(linked) == site._host(bc.get("item_url_root")))
    return genre.for_band(p.bandcamp, p.info.genres if p.info else (), sure)


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
        ov = cache.override(p.band)      # someone decided: a chosen artist and/or the name to search
        chosen, alias = ov.get("mbid", ""), ov.get("alias", "")
        r = lookup.identify(alias or p.band, mb_artist_id=chosen, bandcamp_fallback=ok) \
            if chosen and self._identify is None else identify(alias or p.band)
        if not r.artist and not r.candidates:
            r = self._trimmed(p, identify, record_miss=ok) or r
        p.info = r.artist
        p.lookup_candidates = r.candidates
        if alias and r.artist:
            p.alias = alias              # Spotify and Bandcamp search by it too

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

    def __init__(self, session_factory: Callable[[], object], tracks: bool = True):
        self._session_factory = session_factory
        self._tracks = tracks           # the builder's are off: see `TrackEnricher`
        self._sess = None
        self._lock = threading.Lock()

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

    def __init__(self, search=bandcamp.search, tracks=site.tracks,
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
        return (bool(linked) and site._host(linked) != site._host(have)) or \
            bool(p.alias and p.searched.get("bandcamp") != p.alias)
