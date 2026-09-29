"""`twiddle scene build`: collect, enrich and publish the local event dataset.

This is the only place `scene` goes out to collect events. It reuses the
existing pieces -- `sources.fetch_all` (+ `model.dedupe`), the `bands`
enrichers, `bandcamp`'s throttled cached search, `venue_info.wiki_summary` --
and writes what they found to `dataset.py`'s file. The TUI and `scene list`
only read it.

A run, in order:

  1. take `build.lock` (a scheduled run and a manual one never overlap);
  2. fetch every source; one that fails keeps its rows from the last dataset;
  3. **publish** shows + venues (band records carried over) -- seconds, so
     `r` and a first run show listings before the slow part;
  4. enrich the bands playing watched venues in the next `days`: MusicBrainz,
     Bandcamp and (only if this Mac is signed in) Spotify *identity*, one band
     at a time, in the same lookup-first order the app used. Song lists are
     left out (`TrackEnricher`): measured 2026-09-29, a default build is ~1,100
     bands at ~2 s each cold (MusicBrainz's 1 req/s), and song lists
     would add 2-4 requests a band that only matter on play. A band enriched within
     `PROFILE_TTL_S` is reused, so a re-run costs almost nothing. Publishes
     every `CHECKPOINT` bands, so a killed run keeps its work;
  5. publish the finished dataset (`complete: true`).

Every billed band gets a record: unenriched ones carry only what the caches
say they sound like. Idempotent: the same inputs give the same file but for
its timestamps. Never interactive -- the Spotify session is only ever loaded
from an existing sign-in -- so launchd can run it.
"""
from __future__ import annotations

import fcntl
import time
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .. import spotify, spotify_ops
from . import bandcamp, dataset, genre, profiles
from . import venue_info
from . import venues as venues_mod
from .bands import (BandProfile, BandcampEnricher, LookupEnricher, SpotifyEnricher,
                    assess, near)
from .model import Show
from .sources import fetch_all

DEFAULT_DAYS = 31           # how far ahead bands are enriched (and asked of Bandcamp)
PROFILE_TTL_S = 3 * 86400   # an enriched band is reused this long (plus up to half again, see `_ttl`)
CHECKPOINT = 25             # bands between intermediate publishes
SPOTIFY_GAP_S = 0.5         # between bands' Spotify lookups: they share the user's quota


class BuildError(RuntimeError):
    """Nothing could be built, and nothing was published."""


class BuildBusy(BuildError):
    """Another build holds the lock."""


@dataclass
class Result:
    path: Path
    shows: int = 0
    bands: int = 0
    enriched: int = 0           # bands looked up this run
    reused: int = 0             # bands whose last record was still good
    errors: list[str] = field(default_factory=list)
    enrichers: dict[str, str] = field(default_factory=dict)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / "build.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BuildBusy("another `scene build` is running") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _ttl(key: str) -> float:
    """How long a band's record is trusted: 3 to 4½ days, by band, so a first
    build's few hundred bands do not all expire -- and re-enrich -- together.
    Deterministic, so the same inputs still publish the same file."""
    return PROFILE_TTL_S * (1 + (zlib.crc32(key.encode()) % 1000) / 2000)


def _rate_limited(exc: Exception) -> bool:
    """Bandcamp's own back-off, or a Spotify 429: stop asking for the run."""
    return isinstance(exc, bandcamp.BlockedError) or \
        (isinstance(exc, spotify.ApiError) and exc.status == 429)


def guess_from(hits: list[dict] | None) -> genre.Guess | None:
    """What a Bandcamp search says a band sounds like (the table's column)."""
    band = bandcamp.choose(hits or [], is_local=near)
    if band is None:
        return genre.for_candidates(hits or [])
    return genre.for_band(band, sure=near(band.get("location")))


def _source_status(names: list[str], shows: list[Show], errors: list[str],
                   prev: dataset.Snapshot | None, now: float) -> dict[str, dict]:
    out = {}
    for name in names:
        err = next((e.split(": ", 1)[1] for e in errors if e.startswith(f"{name}: ")), None)
        was = (prev.sources.get(name) if prev else None) or {}
        out[name] = {"ok": err is None,
                     "fetched_at": dataset.iso(now) if err is None else was.get("fetched_at"),
                     "count": sum(1 for s in shows if name in (s.source, *s.also)),
                     "error": err}
    return out


def _venue_records(watched, prev: dataset.Snapshot | None,
                   wiki: Callable[[str], dict | None] | None) -> list[dict]:
    """`wiki=None`: carry last time's summaries, fetch nothing (the fast first publish)."""
    old = {v["id"]: v for v in (prev.venues if prev else []) if "id" in v}
    out = []
    for v in watched:
        vid = dataset.venue_id(v.name)
        summary = None
        if v.info.wikipedia:
            try:
                summary = wiki(v.info.wikipedia) if wiki else None
            except Exception:
                summary = None
            summary = summary or (old.get(vid) or {}).get("wikipedia_summary")
        out.append({"id": vid, "name": v.name, "match": list(v.match),
                    "address": v.info.address, "url": v.info.url, "about": v.info.about,
                    "wikipedia": v.info.wikipedia, "wikipedia_summary": summary,
                    "instagram": v.info.instagram, "map_url": v.info.map_url})
    return out


def _show_rows(shows: list[Show], watched) -> list[dict]:
    rooms = []
    for s in shows:
        v = venues_mod.find(watched, s.venue)
        rooms.append((s, v.name if v else None))
    ids = dataset.unique_ids(rooms)
    return [dict(s.to_dict(), id=i, venue_id=dataset.venue_id(room) if room else None)
            for (s, room), i in zip(rooms, ids)]


def _default_enrichers(use_spotify: bool, notes: dict[str, str]) -> list:
    enrichers: list = [LookupEnricher()]
    if use_spotify:
        try:
            spotify_ops.session()       # loads an existing sign-in; never opens a browser
        except Exception as exc:
            notes["spotify"] = "skipped: " + ("not signed in -- run `twiddle spotify auth`"
                                              if spotify_ops.not_signed_in(exc) else str(exc))
        else:
            enrichers.append(SpotifyEnricher(spotify_ops.session, use_pins=False, tracks=False))
    else:
        notes["spotify"] = "skipped: --no-spotify"
    enrichers.append(BandcampEnricher(fetch_tracks=False))
    return enrichers


def _keep_prior_answers(p: BandProfile, old: dict | None) -> None:
    """An enricher that did not answer this run (skipped, signed out, errored)
    must not erase what the last build learned from it."""
    if not old or not any(v == "done" for v in (old.get("status") or {}).values()):
        return
    before = profiles.from_record(old, names=profiles.ENRICHERS)
    fields = {"lookup": ("info", "lookup_candidates", "alias"),
              "spotify": ("spotify_artist", "spotify_candidates", "tracks"),
              "bandcamp": ("bandcamp", "bc_tracks")}
    kept = False
    for name, attrs in fields.items():
        if name in p.status and p.status[name] == "done":
            continue
        if before.status.get(name) != "done":
            continue
        for a in attrs:
            setattr(p, a, getattr(before, a))
        p.searched.update({k: v for k, v in before.searched.items() if k == name})
        p.status[name] = "done"
        kept = True
    if kept:
        assess(p, use_pins=False)


def enrich_one(band: str, enrichers: list, skip: set[str]) -> BandProfile:
    """One band through every enricher, lookup first, as `BandBook` does but in
    order and to completion. `skip` names enrichers switched off for the run."""
    p = BandProfile(band=band)
    for e in enrichers:
        p.status[e.name] = "pending"
    order = list(enrichers)
    for e in order:
        if e.name in skip:
            p.status[e.name] = "error: paused"
            continue
        try:
            e.enrich(p)
            p.status[e.name] = "done"
        except Exception as exc:
            if _rate_limited(exc):
                skip.add(e.name)
            p.status[e.name] = f"error: {getattr(exc, 'message', None) or exc}"
    for e in order:     # one answer can change another's (MusicBrainz names the Spotify artist)
        rerun = getattr(e, "wants_rerun", None)
        if e.name in skip or p.status.get(e.name) != "done" or not (rerun and rerun(p)):
            continue
        try:
            e.enrich(p)
        except Exception as exc:
            if _rate_limited(exc):
                skip.add(e.name)
            p.status[e.name] = f"error: {getattr(exc, 'message', None) or exc}"
    assess(p, use_pins=False)
    return p


def build(*, path: Path | None = None, days: int = DEFAULT_DAYS, all_venues: bool = False,
          use_spotify: bool = True, dry_run: bool = False,
          sources: list | None = None, enrichers: list | None = None,
          genre_search: Callable = bandcamp.search,
          wiki: Callable[[str], dict | None] = venue_info.wiki_summary,
          watched=None, today: date | None = None,
          spotify_gap: float = SPOTIFY_GAP_S, pace: Callable[[float], None] = time.sleep,
          log: Callable[[str], None] = lambda _m: None,
          now: Callable[[], float] = time.time) -> Result:
    path = path or dataset.default_path()
    today = today or date.today()
    with _locked(path):
        return _build(path, days, all_venues, use_spotify, dry_run, sources, enrichers,
                      genre_search, wiki, watched, today, spotify_gap, pace, log, now)


def _build(path, days, all_venues, use_spotify, dry_run, sources, enrichers,
           genre_search, wiki, watched, today, spotify_gap, pace, log, now) -> Result:
    try:
        prev = dataset.load(path)
    except dataset.DatasetCorrupt as exc:
        log(f"ignoring the old dataset: {exc}")
        prev = None
    except dataset.DatasetError as exc:
        # A newer version's file, or someone else's: never overwrite what we cannot read.
        raise BuildError(f"not replacing {path}: {exc}") from exc
    watched = watched if watched is not None else venues_mod.watched()
    started = now()
    result = Result(path=path)

    # 1. collect
    from .sources import sources as all_sources
    chosen = sources if sources is not None else list(all_sources().values())
    log(f"fetching {len(chosen)} sources…")
    shows, errors = fetch_all(chosen, stale=prev.shows if prev else None)
    result.errors = errors
    for e in errors:
        log(f"warning: {e}")
    if not shows:
        raise BuildError("no listings from any source" + (": " + "; ".join(errors) if errors else ""))
    result.shows = len(shows)
    source_status = _source_status([s.name for s in chosen], shows, errors, prev, started)
    venue_records = _venue_records(watched, prev, None)   # summaries come after listings
    rows = _show_rows(shows, watched)

    # 2. every billed band gets a record; last time's enrichment carries over
    prev_bands = prev.bands if prev else {}
    bands: dict[str, dict] = {}
    for s in shows:
        for b in s.bands:
            key = dataset.band_id(b)
            if key in bands:
                continue
            old = prev_bands.get(key)
            bands[key] = old if old else profiles.minimal_record(
                b, guess_from(genre_search(b, offline=True)))
    result.bands = len(bands)

    notes: dict[str, str] = {}
    active = enrichers if enrichers is not None else _default_enrichers(use_spotify, notes)
    names = tuple(e.name for e in active)
    enricher_status = {n: "ok" for n in names} | notes

    def publish(complete: bool) -> None:
        if dry_run:
            return
        dataset.publish(dataset.document(
            shows=shows, show_rows=rows, venues=venue_records, bands=bands,
            sources=source_status, enrichers=enricher_status, complete=complete,
            builder=_builder_name(), generated_at=started), path)

    publish(complete=False)
    log(f"{len(shows)} shows, {len(bands)} bands; venue summaries, then enriching…")
    # Wikipedia can take seconds a venue when cold: after the listings are out.
    venue_records[:] = _venue_records(watched, prev, wiki)

    # 3. enrich the bands that matter now
    horizon = today + timedelta(days=days)
    wanted: dict[str, str] = {}
    for s in sorted(shows, key=lambda s: s.day):
        if s.day < today or s.day >= horizon:
            continue
        if not all_venues and venues_mod.find(watched, s.venue) is None:
            continue
        for b in s.bands:
            wanted.setdefault(dataset.band_id(b), b)
    skip: set[str] = set()
    todo = []
    for key, name in wanted.items():
        rec = bands.get(key)
        fresh = rec and rec.get("updated_at") and \
            now() - (dataset._epoch(rec["updated_at"]) or 0) < _ttl(key)
        if fresh and profiles.enriched(rec, names):
            result.reused += 1
        else:
            todo.append((key, name))
    for i, (key, name) in enumerate(todo, 1):
        p = enrich_one(name, active, skip)
        _keep_prior_answers(p, bands.get(key))
        hits = None if "bandcamp" in skip else _safe(genre_search, name, offline=True)
        guess = p.genre() or guess_from(hits)
        bands[key] = profiles.to_record(p, updated_at=now(), guess=guess)
        result.enriched += 1
        if "spotify" in names and "spotify" not in skip and spotify_gap:
            pace(spotify_gap)
        if i % CHECKPOINT == 0:
            publish(complete=False)
            log(f"  {i}/{len(todo)} bands")
    for n in skip:
        enricher_status[n] = "paused: rate-limited"
    result.enrichers = enricher_status
    publish(complete=True)
    log(f"published {path}" if not dry_run else "dry run: nothing written")
    return result


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:
        return None


def _builder_name() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return f"twiddle {version('twiddle')}"
    except PackageNotFoundError:
        return "twiddle"
