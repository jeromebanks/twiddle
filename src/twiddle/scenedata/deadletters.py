"""The dead-letter queue: bands and venues a build could not identify, kept for later.

A build resolves most of what it meets deterministically (MusicBrainz, Bandcamp,
Spotify, a hand-kept venue table). What is left is the long tail -- a billing
nobody has heard of, a name six artists share, a room that is on no list -- and
that is where an AI, a human, or a better source earns its keep. Rather than
lose it in the log, every build writes the tail here with *what was already
tried*, so whoever picks it up does not repeat deterministic work.

    ~/.local/share/twiddle/scene/dead-letters.json   (next to the dataset)

    {"schema": "twiddle.scene.dead-letters", "version": 1, "updated_at": "...",
     "letters": {"band:girl-chow": {
        "kind": "band" | "venue", "name", "reason", "detail", "evidence": {...},
        "shows": [{"day", "venue", "billing"}...], "first_seen", "last_seen",
        "attempts": 0, "status": "pending" | "resolved" | "abandoned" | "expired",
        "resolution": null | {...}, "resolved_by": "build" | "ai" | "human", "notes": ""}}}

**Reasons.** `unfound`: MusicBrainz, Bandcamp and Spotify (when it ran) all came
back empty. `ambiguous`: several same-named candidates and nothing to choose
between them. `unwatched_venue`: a room that appears in listings but is on no
venue list, so it has no address, site or description. A band whose lookup
*errored* or was rate-limited is not a dead letter: the next build retries it.

**Life cycle.** A letter is `pending` until something resolves it. A later build
re-checks pending letters: found now -> `resolved` (by `build`); no longer billed
-> `expired`. A letter someone resolved or abandoned is never re-opened by a
build. `resolution` is free-form; these keys are understood on the next build:

  band:   `{"alias": "Mindi Abair"}` -- search under this name from now on
          (stored as the billing's alias, where a trimmed name would be);
          `{"mbid": "..."}` -- the MusicBrainz artist to use among same-named ones.
  venue:  `{"name", "address", "url", "about", "wikipedia", "instagram", "icon"}`
          -- kept and exported; promoting an unwatched room into the dataset's
          venue table needs the client to tell watched from merely known rooms,
          which it does not yet.

Everything else in a resolution is kept untouched for whoever reads it next.
"""
from __future__ import annotations

import fcntl
import re
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from .. import jsonstore
from ..lookup import norm
from ..scenespec import dataset
from ..scenespec import venue as venue_mod
from . import cache, geocode, venue_names
from .bands import SHOW_WORDS, bay_area_only, non_band

SCHEMA = "twiddle.scene.dead-letters"
VERSION = 1
FILE = "dead-letters.json"
SAMPLE_SHOWS = 5            # shows kept per letter, as context
PENDING, RESOLVED, ABANDONED, EXPIRED = "pending", "resolved", "abandoned", "expired"
_SEPARATORS = re.compile(r"\s*(?:,|;|/| & | \+ | w/ | with )\s*|\(")


def path(dataset_path: Path | None = None) -> Path:
    return (dataset_path or dataset.default_path()).with_name(FILE)


@contextmanager
def _txn(dataset_path: Path | None = None):
    """One writer at a time for a load-modify-save of the queue. A build, `scene dlq
    resolve` and an AI resolver each rewrite the whole file; without this, the last
    one to save would silently undo the others' changes."""
    p = path(dataset_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_name(p.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def load(dataset_path: Path | None = None) -> dict:
    doc = jsonstore.read(path(dataset_path))
    if doc.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "version": VERSION, "updated_at": None, "letters": {}}
    doc.setdefault("letters", {})
    return doc


def save(doc: dict, dataset_path: Path | None = None) -> None:
    doc["updated_at"] = dataset.iso()
    jsonstore.write(path(dataset_path), doc)


# ---- what is a dead letter --------------------------------------------------


def _hints(name: str) -> dict:
    """Cheap, deterministic clues about *why* a billing might not be a band, for
    whoever picks it up (an AI should not have to rediscover these)."""
    words = {w.lower().strip(".,:!()") for w in name.split()}
    return {k: v for k, v in {
        "several_names_joined": bool(_SEPARATORS.search(name)) or None,
        "show_words": sorted(words & SHOW_WORDS) or None,
        "unbalanced_parenthesis": name.count("(") != name.count(")") or None,
        "looks_like_a_night": bool(re.search(r"\b(nights?|nightz|party|showcase|open mic|karaoke|"
                                             r"jam|social|dj|nite)\b", name, re.I)) or None,
    }.items() if v}


def classify_band(rec: dict) -> tuple[str, str, dict] | None:
    """(reason, detail, evidence) if this enriched record is a dead letter, else None."""
    st = rec.get("status") or {}
    if not rec.get("updated_at") or any(str(v).startswith("error") for v in st.values()):
        return None                                  # not attempted, or will be retried
    if st.get("lookup") != "done" or st.get("bandcamp") != "done":
        return None
    if non_band(rec.get("name", "")):
        return None                                  # an event, not an act: nothing to find
    found = rec.get("info") or rec.get("bandcamp") or rec.get("spotify_artist")
    if found:
        return None
    spotify_ran = str(st.get("spotify", "")).startswith(("done", "kept"))
    ev = {"searched": rec.get("searched") or {}, "alias": rec.get("alias"),
          "spotify_checked": spotify_ran,
          "lookup_candidates": [{k: c.get(k) for k in ("name", "disambiguation", "mbid")}
                                for c in (rec.get("lookup_candidates") or [])[:8]],
          "spotify_candidates": [{"id": c.get("id"), "name": c.get("name"),
                                  "genres": c.get("genres")}
                                 for c in (rec.get("spotify_candidates") or [])[:8]],
          "hints": _hints(rec.get("name", ""))}
    if len(rec.get("spotify_candidates") or []) > 1 or len(rec.get("lookup_candidates") or []) > 1:
        return "ambiguous", "several same-named candidates and nothing to choose between them", ev
    return "unfound", "not on MusicBrainz, Bandcamp" + (", or Spotify" if spotify_ran else ""), ev


def unanswered_ids(bands: dict[str, dict]) -> set[str]:
    """Band records with no real answer yet: not attempted, or a source errored or was
    rate-limited. Such a band is not a dead letter this time, but a pending letter for
    it must stay pending -- an unanswered lookup is not a found one."""
    out = set()
    for key, rec in bands.items():
        st = rec.get("status") or {}
        if not rec.get("updated_at") or any(str(v).startswith("error") for v in st.values()) \
                or st.get("lookup") != "done" or st.get("bandcamp") != "done":
            out.add(f"band:{key}")
    return out


def band_letters(bands: dict[str, dict], shows: list) -> dict[str, dict]:
    """Every band record that is a dead letter, as a letter (no bookkeeping yet)."""
    by_band: dict[str, list] = defaultdict(list)
    for s in shows:
        for b in s.bands:
            by_band[dataset.band_id(b)].append(s)
    out = {}
    for key, rec in bands.items():
        hit = classify_band(rec)
        if hit is None:
            continue
        reason, detail, ev = hit
        out[f"band:{key}"] = {
            "kind": "band", "name": rec.get("name", key), "reason": reason, "detail": detail,
            "evidence": ev,
            "shows": [{"day": s.day.isoformat(), "venue": s.venue, "billing": s.billing}
                      for s in sorted(by_band.get(key, []), key=lambda s: s.day)[:SAMPLE_SHOWS]],
            "show_count": len(by_band.get(key, []))}
    return out


def _rooms(shows: list, watched) -> list[tuple[venue_names.Room, list]]:
    """Each unwatched room (its spellings merged) with the shows played there."""
    spelled: dict[str, list] = defaultdict(list)
    for s in shows:
        if venue_mod.find(watched, s.venue) is None:
            spelled[s.venue].append(s)
    rooms = venue_names.cluster({k: len(v) for k, v in spelled.items()})
    return [(r, sorted((s for v in r.variants for s in spelled[v]), key=lambda s: s.day))
            for r in rooms]


def venue_letters(shows: list, watched) -> dict[str, dict]:
    """Rooms that appear in listings but are on no venue list: no address, site or
    description. The spellings of one room are one letter (`venue_names.cluster`):
    "Hopmonk, Novato" and "Hopmonk Tavern, Novato" are the same place."""
    out = {}
    for room, ss in _rooms(shows, watched):
        out[f"venue:{room.key}"] = {
            "kind": "venue", "name": room.label, "reason": "unwatched_venue",
            "detail": "in the listings, on no venue list: no address, site or description",
            "evidence": {"listed_as": room.variants, "name": room.name, "city": room.city,
                         "state": room.state, "address": room.address,
                         "sources": sorted({s.source for s in ss}),
                         "headliners": [s.headliner for s in ss[:5] if s.bands]},
            "shows": [{"day": s.day.isoformat(), "venue": s.venue, "billing": s.billing}
                      for s in ss[:SAMPLE_SHOWS]],
            "show_count": len(ss)}
    return out


KNOWN_FIELDS = ("name", "city", "state", "address", "url", "about", "wikipedia", "instagram",
                "icon", "lat", "lon")
GEOCODED_FIELDS = ("city", "state", "address", "lat", "lon", "attribution", "osm", "matched")


def unwatched_rooms(shows: list, watched) -> list[venue_names.Room]:
    """The rooms the listings name that no venue list watches, busiest first."""
    return sorted((r for r, _ in _rooms(shows, watched)), key=lambda r: (-r.shows, r.key))


def known_venue_rows(shows: list, watched, dataset_path: Path | None = None) -> list[dict]:
    """The dataset's `known_venues`: every unwatched room the listings name, as
    far as it is known. What the listing itself says (name, city, state, a street
    address) is always there; a resolved venue letter adds what someone found
    (`KNOWN_FIELDS`). `match` holds every spelling, so the client can tell which
    shows are at the room. Never watched: these rooms do not widen anyone's view."""
    letters = load(dataset_path)["letters"]
    rows = []
    found = geocode.known([r for r, _ in _rooms(shows, watched)])
    for room, _ in _rooms(shows, watched):
        l = letters.get(f"venue:{room.key}") or {}
        res = l.get("resolution") if l.get("status") == RESOLVED and isinstance(l.get("resolution"), dict) else {}
        row = {"id": room.key, "name": room.name, "city": room.city, "state": room.state,
               "address": room.address, "match": sorted({v.lower() for v in room.variants})}
        hit = found.get(room.key) or {}
        row.update({k: hit[k] for k in GEOCODED_FIELDS if hit.get(k) not in (None, "")})
        row.update({k: res[k] for k in KNOWN_FIELDS if res.get(k) not in (None, "")})
        # what a person or an AI found beats a map's guess; the OSM credit stays only
        # while some OSM-derived value is still in the row
        if hit and not any(row.get(k) == hit.get(k) for k in ("address", "lat", "lon") if hit.get(k)):
            row.pop("attribution", None)
            row.pop("osm", None)
        rows.append(row)
    return sorted(rows, key=lambda r: r["id"])


def billed_ids(bands: dict, shows: list, watched) -> set[str]:
    """Every letter id that is billed right now (see `sync`): each band, each
    watched room as the source spells it, each unwatched room by its merged key."""
    ids = {f"band:{k}" for k in bands}
    ids |= {f"venue:{dataset.venue_id(s.venue)}" for s in shows
            if venue_mod.find(watched, s.venue) is not None}
    return ids | {f"venue:{r.key}" for r, _ in _rooms(shows, watched)}


# ---- keeping the queue ------------------------------------------------------


def sync(current: dict[str, dict], dataset_path: Path | None = None, *,
         now: float | None = None, billed: set[str] | None = None,
         unanswered: set[str] | None = None) -> dict:
    """Merge this build's dead letters into the queue and save it.

    `current`: this build's letters (`band_letters` | `venue_letters`).
    `billed`: the letter ids (`band:<key>`, `venue:<slug>`) of everything in the
    listings now, so a pending letter for one that is gone can expire. `unanswered`:
    ids whose lookup has not given an answer (see `unanswered_ids`): they stay pending.
    Returns counts."""
    with _txn(dataset_path):
        return _sync(current, dataset_path, now, billed, unanswered or set())


def _sync(current, dataset_path, now, billed, unanswered) -> dict:
    stamp = dataset.iso(now)
    doc = load(dataset_path)
    letters = doc["letters"]
    counts = {"new": 0, "still": 0, "resolved": 0, "expired": 0}
    for lid, new in current.items():
        old = letters.get(lid)
        if old is None:
            letters[lid] = dict(new, first_seen=stamp, last_seen=stamp, attempts=0,
                                status=PENDING, resolution=None, resolved_by=None, notes="")
            counts["new"] += 1
        elif old["status"] == PENDING or old["status"] == EXPIRED:
            letters[lid] = dict(old, **{k: v for k, v in new.items()},
                                last_seen=stamp, status=PENDING)
            counts["still"] += 1
        # resolved / abandoned: a build never re-opens what someone closed
    for lid, old in letters.items():
        if old["status"] != PENDING or lid in current:
            continue
        if lid in unanswered and (billed is None or lid in billed):
            old["last_seen"] = stamp            # asked, not answered: still pending
            continue
        if billed is not None and lid not in billed:        # nobody is billed there any more
            old["status"], old["last_seen"] = EXPIRED, stamp
            counts["expired"] += 1
        else:                                                # billed, and no longer unresolved
            why = {"not_a_band": "reads as an event, not an act"} \
                if old["kind"] == "band" and non_band(old["name"]) else {"found": "by a later build"}
            old.update(status=RESOLVED, resolved_by="build", last_seen=stamp,
                       resolution=old.get("resolution") or why)
            counts["resolved"] += 1
    save(doc, dataset_path)
    return counts


def settle_ambiguous(dataset_path: Path | None = None) -> int:
    """Settle, by rule, the ambiguous bands where exactly one same-named candidate
    is described as local ("pop duo from Oakland, CA", "Bay Area post-hardcore
    band"): billed at a Bay Area room, that is the one. Resolved `by="rule"`, so a
    person can still `reopen` it. Returns how many it settled."""
    with _txn(dataset_path):
        return _settle(dataset_path)


def _settle(dataset_path) -> int:
    doc = load(dataset_path)
    n = 0
    for lid, l in doc["letters"].items():
        if l["kind"] != "band" or l["reason"] != "ambiguous" or l["status"] != PENDING:
            continue
        local = [c for c in l["evidence"].get("lookup_candidates", [])
                 if c.get("mbid") and bay_area_only(c.get("disambiguation"))]
        if len(local) == 1:
            l.update(status=RESOLVED, resolved_by="rule", applied=False, attempts=l.get("attempts", 0) + 1,
                     resolution={"mbid": local[0]["mbid"], "why": "the only candidate described as local: "
                                 + str(local[0].get("disambiguation"))})
            n += 1
    if n:
        save(doc, dataset_path)
    return n


def apply_resolutions(dataset_path: Path | None = None) -> list[str]:
    """Hand what an AI or a human resolved to the next build; returns the band keys
    it touched so the build can look those bands up again.

    A band resolution with an `alias` is stored as that billing's alias, so the
    enrichers search for the alias instead of the billing. Each is applied once. A letter
    that was `reopen`ed has had its override removed and is looked up afresh."""
    with _txn(dataset_path):
        return _apply(dataset_path)


def _apply(dataset_path) -> list[str]:
    doc = load(dataset_path)
    keys = []
    for l in doc["letters"].values():
        if l["kind"] == "band" and l.pop("relookup", False):
            keys.append(dataset.band_id(l["name"]))
        res = l.get("resolution") or {}
        if l["kind"] == "band" and l["status"] == RESOLVED and l.get("resolved_by") != "build" \
                and (res.get("alias") or res.get("mbid")) and not l.get("applied"):
            cache.save_override(l["name"], mbid=str(res.get("mbid") or ""), alias=str(res.get("alias") or ""))
            l["applied"] = True
            keys.append(dataset.band_id(l["name"]))
    if keys:
        save(doc, dataset_path)
    return list(dict.fromkeys(keys))


# ---- what people and AIs do with it ----------------------------------------


def resolve(lid: str, resolution: dict, by: str = "human", dataset_path: Path | None = None) -> dict:
    with _txn(dataset_path):
        doc = load(dataset_path)
        l = doc["letters"].get(lid)
        if l is None:
            raise KeyError(lid)
        old = l.get("resolution") or {}
        if l["kind"] == "band" and l.get("applied"):      # a replaced answer must not linger in the cache
            cache.drop_override(l["name"], mbid=str(old.get("mbid") or ""), alias=str(old.get("alias") or ""))
        l.update(status=RESOLVED, resolution=resolution, resolved_by=by,
                 attempts=l.get("attempts", 0) + 1, applied=False, relookup=l["kind"] == "band" and bool(l.get("applied")))
        save(doc, dataset_path)
        return l


def abandon(lid: str, note: str = "", dataset_path: Path | None = None) -> dict:
    with _txn(dataset_path):
        doc = load(dataset_path)
        l = doc["letters"].get(lid)
        if l is None:
            raise KeyError(lid)
        l.update(status=ABANDONED, attempts=l.get("attempts", 0) + 1,
                 notes=(l.get("notes", "") + " " + note).strip())
        save(doc, dataset_path)
        return l


def reopen(lid: str, dataset_path: Path | None = None) -> dict:
    """Back to pending. A band resolution that was already applied is undone too: the
    alias or chosen artist it left in the producer's cache is removed and the band is
    looked up afresh by the next build, so reopening really does undo a wrong choice."""
    with _txn(dataset_path):
        doc = load(dataset_path)
        l = doc["letters"].get(lid)
        if l is None:
            raise KeyError(lid)
        res = l.get("resolution") or {}
        if l["kind"] == "band" and l.get("applied"):
            cache.drop_override(l["name"], mbid=str(res.get("mbid") or ""), alias=str(res.get("alias") or ""))
            l["relookup"] = True
        l.update(status=PENDING, resolution=None, resolved_by=None, applied=False)
        save(doc, dataset_path)
        return l


def select(doc: dict, *, kind: str | None = None, status: str | None = PENDING,
           limit: int | None = None) -> list[tuple[str, dict]]:
    """Letters, busiest first (most shows), optionally by kind and status."""
    rows = [(lid, l) for lid, l in doc["letters"].items()
            if (kind is None or l["kind"] == kind) and (status is None or l["status"] == status)]
    rows.sort(key=lambda r: (-r[1].get("show_count", 0), r[0]))
    return rows[:limit] if limit else rows
