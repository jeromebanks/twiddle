"""`twiddle comedy` -- pick and play something to fall asleep to.

Spotify's own recommendation engine is dead for this app: `/recommendations`
returns null, `/artists/{id}/related-artists` and `/browse/categories` both
403 -- measured against the real account, not assumed from the deprecation
notice `discover_cli.py` already documents. None of that matters here.
`/me/top/artists` already skews heavily toward comedy, because that is what
you actually listen to. So this module never asks Spotify who you might
like -- it classifies who he already listens to (via `lookup.py`'s
MusicBrainz/Wikipedia identity, the same thing `np -i` uses) and picks
tonight's queue from that list plus whatever's genuinely new, found by a
plain-text catalog search (the `genre:` field filter is broken for this app
too, measured the same way).

Playback is one whole run of full albums, not a shuffle of tracks -- specials
are made to be heard start to finish, and Spotify's queue endpoint only ever
takes one track at a time, so several albums back-to-back means flattening
every track into a single ordered list up front (`flatten`), not looping
`queue()` -- wrapped in `spotify_ops.play_and_check`, the same tolerance for
the transfer-then-play race every other play path in this repo already has.

Stopping is the speaker's own sleep timer (`play.configure_sleep_timer`), not
a process kept alive overnight. Only the *tail* -- the stretch after the
queue's own last track ends, where the timer's buffer is padding silence
before it fires -- is journalled as an intervention span
(`play.journal_span`), and only when the timer actually armed. The 2-3 hours
of real comedy audio before that tail is not journalled at all: it travels
the same relay/Roam path music does, so its dropouts are real evidence for
the disconnect investigation, and a span covering the whole session would
have discounted every one of them, for the whole household, defeating the
point of that investigation. See `start_queue`.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import lookup, play, spotify, spotify_ops

CACHE_FILE = Path(os.environ.get(
    "TWIDDLE_COMEDY_ARTISTS",
    str(Path.home() / ".cache" / "twiddle" / "comedy_artists.json")))

# Every artist's catalog, cached: `sleep` and `new` both fetch it for the
# whole classified list just to build tonight's queue, and doing that with a
# live paginated call per artist on every single invocation is exactly the
# burst that tripped a ~24h quota lockout on this app on 2026-09-24 (the
# app's own Spotify quota). A comedian's back catalog does not change
# minute to minute.
ALBUMS_CACHE_FILE = Path(os.environ.get(
    "TWIDDLE_COMEDY_ALBUMS",
    str(Path.home() / ".cache" / "twiddle" / "comedy_albums.json")))
# Days, not hours: `snooze` runs roughly once every 24h, so anything shorter
# than a day is stale on every single nightly run and buys nothing for the
# case that actually matters. A few days' lag noticing a new release is
# harmless inside the 45-day `NEW_RELEASE_DAYS` window.
ALBUMS_CACHE_TTL_S = 5 * 86400
# A cache only helps once it is warm. The *first* `new`/`sleep` after the
# cache is empty (or a cold start after the 2026-09-24 lockout) is still a
# burst of one call per artist -- the same shape that tripped it. This
# spaces those calls out; it costs about 20s cold and nothing once warm.
# 0 in tests (see conftest.py) so the suite stays fast.
PACE_S = 0.3

# Its own file, not logs/observations.jsonl: that log is the dropout evidence
# for the disconnect investigation, and `--heard` there is specifically what
# gets cross-referenced as "I heard a drop" (CLAUDE.md -> *Recording new
# observations*). Mixing "loved this bit" ratings into the same stream risks
# a false match later.
_REPO = Path(__file__).resolve().parents[2]
PLAYED_LOG = Path(os.environ.get(
    "TWIDDLE_COMEDY_LOG", str(_REPO / "logs" / "comedy_played.jsonl")))

NEW_RELEASE_DAYS = 45     # older than this is "the rotation", not "new"
AVOID_REPEAT_DAYS = 14    # don't replay the same act two weeks running
DEFAULT_ALBUMS = 2
# Small on purpose: track durations from `album_tracks` are exact, so this
# only needs to cover buffering/handoff slack, not uncertainty about when the
# queue actually ends. A big buffer here means the Roam sits on padded
# silence after the queue finishes -- which `relay.jsonl`'s `silence_s` would
# then read as librespot starving, contaminating the dropout measurements
# the relay exists for.
SLEEP_BUFFER_MIN = 2
# Separate, and larger: purely how long *past* the timer's predicted fire
# time the journalled span is kept open. The monitor samples every ~30s and
# the on-speaker timer's own precision isn't guaranteed, so a sample landing
# a little after the exact predicted instant must still land inside the span
# -- otherwise `analyse` scores the sleep timer's own Stop as a real dropout.
# This never touches the actual sleep-timer duration.
JOURNAL_MARGIN_S = 180
MAX_QUEUE_TRACKS = 300    # a generous, unofficial cap on Session.play(uris=...)

COMEDY_KEYWORDS = ("comedian", "stand-up", "stand up comedy", "stand up comic")


@dataclass
class ComedyArtist:
    id: str
    name: str
    added_at: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "added_at": self.added_at}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- the classified list ----------------------------------------------------


def load_artists() -> list[ComedyArtist]:
    try:
        raw = json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return []
    return [ComedyArtist(**a) for a in raw]


def save_artists(artists: list[ComedyArtist]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps([a.to_dict() for a in artists], indent=2))


def classify(name: str) -> tuple[bool | None, str]:
    """Is this person a comedian? True/False/None (couldn't tell), with why.

    Keys off `lookup.py`'s structured fields (description, summary, genres)
    rather than parsing rendered text -- confirmed live against the real
    account: `twiddle info "Marc Maron"` returns a `comedy` tag and
    "American comedian" in its description.
    """
    try:
        result = lookup.identify(name, use_cache=True)
    except lookup.LookupFailed:
        return None, "lookup unreachable"
    a = result.artist
    if a is None:
        return None, "not found on MusicBrainz, Discogs or Bandcamp"
    text = " ".join(filter(None, [a.description, a.summary])).lower()
    tags = [g.lower() for g in a.genres]
    if any(k in text for k in COMEDY_KEYWORDS) or any("comedy" in t for t in tags):
        return True, a.description or (", ".join(a.genres) or "tagged comedy")
    return False, a.description or "no comedy signal in description, summary or genres"


def confirm(artist_id: str, name: str) -> ComedyArtist:
    """Add an artist the classifier couldn't call but a person confirmed."""
    known = load_artists()
    existing = next((a for a in known if a.id == artist_id), None)
    if existing:
        return existing
    entry = ComedyArtist(id=artist_id, name=name, added_at=_now_iso())
    known.append(entry)
    save_artists(known)
    return entry


# ---- finding candidates on Spotify ------------------------------------------


def _dedup_by_id(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for it in items:
        i = it.get("id")
        if i and i not in seen:
            seen.add(i)
            out.append(it)
    return out


def candidate_artists(sess: spotify.Session) -> list[dict]:
    """Every artist worth checking, from what you actually listen to.

    `/me/top/artists` and `/me/player/recently-played` both work fine and
    already skew toward comedy (measured); `/recommendations` and
    `/artists/{id}/related-artists` do not exist for this app, so this is
    the whole candidate pool, not a seed for something smarter.
    """
    out: list[dict] = []
    for time_range in ("long_term", "medium_term"):
        data = sess.request("GET", "/me/top/artists",
                             params={"limit": 50, "time_range": time_range}) or {}
        out += data.get("items", [])
    recent = sess.request("GET", "/me/player/recently-played",
                           params={"limit": 50}) or {}
    out += [a for item in recent.get("items", [])
            for a in (item.get("track") or {}).get("artists", [])]
    return _dedup_by_id(out)


def search_new_comedy(sess: spotify.Session, limit: int = 10) -> list[dict]:
    """Artists behind recent catalog releases tagged comedy, independent of
    your own history.

    `genre:comedy` returns nothing for this app (measured -- the field
    filter is broken, not just for this genre); plain `tag:new` combined
    with the word "comedy" works and returns real hits, though noisy (music
    whose title merely contains the word). Every artist that surfaces here
    still goes through `classify()` before being trusted.

    `limit` defaults low deliberately: `tag:new` 400s with "Invalid limit"
    above 10, undocumented and measured live. Whether a plain search
    without `tag:new` tolerates a higher limit was *not* actually tested --
    every plain-search probe run this session also used `limit<=10`, and
    `/artists/{id}/albums` caps at 10 for this app regardless of `tag:new`
    (see `artist_albums`), so the restriction may be broader than this one
    tag rather than specific to it. Treat 10 as the safe default against
    this app's endpoints until a higher limit is actually confirmed
    somewhere.
    """
    data = sess.request("GET", "/search", params={
        "q": "comedy tag:new", "type": "album", "limit": limit}) or {}
    items = data.get("albums", {}).get("items", [])
    return _dedup_by_id([a for it in items for a in it.get("artists", [])])


# ---- refresh: classify anyone new -------------------------------------------


@dataclass
class RefreshResult:
    added: list[ComedyArtist] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)  # {"id", "name", "why"}
    unchanged: int = 0


def refresh(sess: spotify.Session, notify=lambda m: None) -> RefreshResult:
    """Classify every not-yet-known candidate; nothing is added silently.

    A confident `True` is added, a confident `False` is dropped, and
    anything the heuristic can't call is handed back as `ambiguous` for a
    person to confirm -- never auto-added.
    """
    known = load_artists()
    known_ids = {a.id for a in known}
    result = RefreshResult()
    candidates = _dedup_by_id(candidate_artists(sess) + search_new_comedy(sess))
    for c in candidates:
        cid, name = c.get("id"), c.get("name", "")
        if not cid or cid in known_ids:
            result.unchanged += 1
            continue
        notify(f"checking {name}...")
        verdict, why = classify(name)
        if verdict is True:
            entry = ComedyArtist(id=cid, name=name, added_at=_now_iso())
            known.append(entry)
            known_ids.add(cid)
            result.added.append(entry)
        elif verdict is None:
            result.ambiguous.append({"id": cid, "name": name, "why": why})
        # verdict is False: not a comedian, silently skipped
    save_artists(known)
    return result


# ---- new releases ------------------------------------------------------------


def _load_albums_cache() -> dict:
    try:
        return json.loads(ALBUMS_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_albums_cache(cache: dict) -> None:
    ALBUMS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALBUMS_CACHE_FILE.write_text(json.dumps(cache))


def artist_albums(sess: spotify.Session, artist_id: str, *,
                   use_cache: bool = True) -> list[dict]:
    """This artist's albums and singles, newest first.

    Cached for `ALBUMS_CACHE_TTL_S` (see the module-level comment on it): a
    classified list of 40+ comedians means this many paginated fetches every
    time `new` or `sleep` runs, and that burst is very likely what tripped
    the 429 lockout measured on 2026-09-24.

    Sorted client-side: one sample coming back newest-first while probing
    the live API isn't a guarantee Spotify documents anywhere.
    `include_groups=album,single` because specials sometimes ship as a
    single "hour", not an "album".

    Paginated in pages of 10: `limit` above 10 is rejected here with
    "Invalid limit" for this restricted app -- measured live. `/me/top/
    artists`, `/me/player/recently-played` and `/albums/{id}/tracks` are
    each confirmed fine at 50 (`search_new_comedy`'s `tag:new` query is
    capped the same way as this endpoint, not an exception to it).
    Fetching one page and hoping the newest release landed on it would
    silently miss it for any comedian with more than 10 releases.
    """
    cache = _load_albums_cache() if use_cache else {}
    hit = cache.get(artist_id)
    if hit and time.time() - hit.get("at", 0) < ALBUMS_CACHE_TTL_S:
        return hit["albums"]
    items: list[dict] = []
    offset = 0
    while True:
        if PACE_S:
            time.sleep(PACE_S)
        data = sess.request("GET", f"/artists/{artist_id}/albums", params={
            "include_groups": "album,single", "limit": 10, "offset": offset}) or {}
        page = data.get("items", [])
        items += page
        if not page or not data.get("next"):
            break
        offset += len(page)
    albums = sorted(items, key=lambda a: a.get("release_date", ""), reverse=True)
    if use_cache:
        cache[artist_id] = {"at": time.time(), "albums": albums}
        _save_albums_cache(cache)
    return albums


def new_releases(sess: spotify.Session, artists: list[ComedyArtist],
                  days: int = NEW_RELEASE_DAYS) -> list[dict]:
    """Releases from the classified list within the last `days`, newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    out = []
    for artist in artists:
        for album in artist_albums(sess, artist.id):
            date = album.get("release_date", "")
            # Day precision only: a year- or month-only date can't be trusted
            # to actually fall inside the window, and lexicographic
            # comparison against a full-date cutoff would just as often
            # wrongly exclude it -- either way, treat it as "not new".
            if album.get("release_date_precision") == "day" and date >= cutoff:
                out.append(album | {"_artist": artist.name})
    return sorted(out, key=lambda a: a["release_date"], reverse=True)


# ---- the played/rated log ----------------------------------------------------


def load_played() -> list[dict]:
    if not PLAYED_LOG.exists():
        return []
    out = []
    for line in PLAYED_LOG.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _append_played(rec: dict) -> None:
    PLAYED_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PLAYED_LOG.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def record_played(album_id: str, name: str, artist: str) -> None:
    _append_played({"ts": _now_iso(), "kind": "played", "album_id": album_id,
                    "name": name, "artist": artist})


def rate(direction: str, album_id: str | None = None) -> dict:
    """Thumbs up/down an album -- the last one played, unless `album_id` is given.

    Its own verb rather than `tools/observe.py`: that log is cross-referenced
    for dropout evidence, and a rating has nothing to do with that. Can only
    rate something this tool actually played, so the artist attribution
    comes from the matching `played` record rather than being retyped.
    """
    played = [r for r in load_played() if r.get("kind") == "played"]
    if album_id is None:
        if not played:
            raise ValueError("nothing played yet to rate")
        match = played[-1]
    else:
        match = next((r for r in played if r.get("album_id") == album_id), None)
        if match is None:
            raise ValueError(f"no played record for album {album_id}")
    rec = {"ts": _now_iso(), "kind": "rated", "album_id": match["album_id"],
           "name": match.get("name", ""), "artist": match.get("artist", ""),
           "direction": direction}
    _append_played(rec)
    return rec


def _recent_played(days: int) -> list[dict]:
    cutoff = time.time() - days * 86400
    out = []
    for r in load_played():
        if r.get("kind") != "played":
            continue
        try:
            ts = datetime.fromisoformat(r["ts"]).timestamp()
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            out.append(r)
    return out


def _recent_album_ids(days: int) -> set[str]:
    return {r.get("album_id", "") for r in _recent_played(days)}


def _recent_artist_names(days: int) -> set[str]:
    """Artists heard within `days` -- excluded from the rotation *pool*
    entirely (see `pick_queue`), not merely deprioritized: a liked artist
    played last night must not outrank someone never played at all, which a
    plain sort against `_liked_artist_names` used to allow."""
    return {r["artist"] for r in _recent_played(days) if r.get("artist")}


def _liked_artist_names() -> set[str]:
    return {r["artist"] for r in load_played()
            if r.get("kind") == "rated" and r.get("direction") == "up" and r.get("artist")}


# ---- tonight's queue ---------------------------------------------------------


@dataclass
class QueuePick:
    album_id: str
    name: str
    artist: str
    uri: str
    track_uris: list[str]
    duration_ms: int


def album_tracks(sess: spotify.Session, album_id: str) -> tuple[list[str], int]:
    """(track URIs, total duration_ms) for one album, in track order.

    Spotify's queue endpoint only ever takes one track URI at a time, so
    playing several whole albums back-to-back means enumerating every track
    up front and handing the lot to `Session.play(uris=...)` as one ordered
    list -- see `flatten` -- rather than looping `queue()` once per track.
    """
    uris, total = [], 0
    data = sess.request("GET", f"/albums/{album_id}/tracks",
                         params={"limit": 50}) or {}
    for t in data.get("items", []):
        uris.append(t.get("uri", ""))
        total += t.get("duration_ms", 0) or 0
    return uris, total


def pick_queue(sess: spotify.Session, artists: list[ComedyArtist],
               n: int = DEFAULT_ALBUMS) -> list[QueuePick]:
    """Tonight's queue: a fresh release first, then a rotation, never a repeat.

    Order of preference for each slot: an unplayed release from the last
    `NEW_RELEASE_DAYS` (regardless of who -- a genuinely new special earns
    its slot even from someone heard last night), then the classified list
    rotated by least-recently-played, with anyone heard in the last
    `AVOID_REPEAT_DAYS` excluded from that rotation outright (not merely
    deprioritized -- a liked artist played last night must not still
    outrank someone never played), and artists rated up preferred among
    whoever remains eligible.
    """
    if not artists:
        raise ValueError("no classified comedians yet -- run `comedy refresh` first")
    played_recent = _recent_album_ids(AVOID_REPEAT_DAYS)
    recent_artists = _recent_artist_names(AVOID_REPEAT_DAYS)
    liked = _liked_artist_names()
    picks: list[QueuePick] = []
    chosen_ids: set[str] = set()

    def take(album: dict, artist_name: str) -> QueuePick | None:
        aid = album.get("id", "")
        if not aid or aid in chosen_ids or aid in played_recent:
            return None
        uris, dur = album_tracks(sess, aid)
        if not uris:
            return None
        chosen_ids.add(aid)
        return QueuePick(album_id=aid, name=album.get("name", ""),
                          artist=artist_name, uri=album.get("uri", ""),
                          track_uris=uris, duration_ms=dur)

    for album in new_releases(sess, artists):
        if len(picks) >= n:
            break
        pick = take(album, album.get("_artist", ""))
        if pick:
            picks.append(pick)

    if len(picks) < n:
        last_played = {r["artist"]: r["ts"] for r in load_played()
                       if r.get("kind") == "played"}
        eligible = [a for a in artists if a.name not in recent_artists]
        rotation = sorted(eligible, key=lambda a: (a.name not in liked,
                                                    last_played.get(a.name, "")))
        for artist in rotation:
            if len(picks) >= n:
                break
            for album in artist_albums(sess, artist.id):
                pick = take(album, artist.name)
                if pick:
                    picks.append(pick)
                    break

    total_tracks = sum(len(p.track_uris) for p in picks)
    while total_tracks > MAX_QUEUE_TRACKS and len(picks) > 1:
        dropped = picks.pop()
        total_tracks -= len(dropped.track_uris)
    return picks


def flatten(picks: list[QueuePick]) -> tuple[list[str], int]:
    """Every track from every pick, in order, as one list -- see `pick_queue`."""
    uris = [u for p in picks for u in p.track_uris]
    total_ms = sum(p.duration_ms for p in picks)
    return uris, total_ms


def start_queue(sess: spotify.Session, dev: dict, picks: list[QueuePick],
                room_ip: str, buffer_min: int = SLEEP_BUFFER_MIN) -> dict:
    """Play the queue, arm the speaker's own sleep timer, and journal only
    the tail where that timer's buffer is padding silence.

    `spotify_ops.play_and_check` wraps the play call: `sess.play` alone would
    report failure on the documented transfer-then-play race even when
    playback is, in fact, under way -- the same reason every other play path
    in this repo already wraps it.

    Order matters after that. `record_played` happens right after the play
    call succeeds, before the sleep timer is even attempted: if
    `configure_sleep_timer`'s SOAP call fails (it has never run against real
    hardware), the Roam is nonetheless playing, and that has to be on record
    regardless of what the timer did. A failed timer is reported
    (`timer_armed: False`) rather than raised, and -- critically -- nothing
    is journalled for it: an unmatched span-start is read by `analyse` as
    open until the *next* successful call, silently discounting every event
    on every speaker in between.

    When the timer *does* arm, only the tail is journalled: from just before
    the queue's own last track ends to the timer's predicted fire time plus
    `JOURNAL_MARGIN_S` slack (the monitor samples every ~30s and the timer's
    own precision isn't guaranteed, so a sample landing a little late still
    has to land inside the span). The 2-3 hours of real audio before that
    tail is deliberately left unjournalled -- see the module docstring.
    """
    uris, total_ms = flatten(picks)
    if not uris:
        raise ValueError("nothing to play -- empty queue")

    def go():
        sess.play(uris=uris, device_id=dev.get("id", ""))
    spotify_ops.play_and_check(sess, dev, go)

    started = datetime.now(timezone.utc)
    for p in picks:
        record_played(p.album_id, p.name, p.artist)

    queue_ends_at = started + timedelta(milliseconds=total_ms)
    duration_s = int(total_ms / 1000) + buffer_min * 60
    fires_at = started + timedelta(seconds=duration_s)
    timer_armed, timer_error = True, ""
    try:
        play.configure_sleep_timer(room_ip, duration_s)
    except Exception as exc:  # a SOAP failure here must not lose the play
        timer_armed, timer_error = False, f"{type(exc).__name__}: {exc}"
    if timer_armed:
        tail_start = queue_ends_at - timedelta(seconds=60)
        tail_end = fires_at + timedelta(seconds=JOURNAL_MARGIN_S)
        sid = play.new_span_id()
        bound = (tail_end - tail_start).total_seconds()
        play.journal_span("comedy_sleep_tail_start", room_ip, span_id=sid, max_s=bound,
                           ts=tail_start.isoformat(timespec="milliseconds"))
        play.journal_span("comedy_sleep_tail_end", room_ip, span_id=sid,
                           ts=tail_end.isoformat(timespec="milliseconds"))

    return {"albums": [{"name": p.name, "artist": p.artist} for p in picks],
            "duration_s": duration_s,
            "fires_at": fires_at.isoformat(timespec="seconds"),
            "timer_armed": timer_armed, "timer_error": timer_error}
