"""The published local event dataset: what `scene build` writes, what readers read.

This module is the whole boundary between the producer (`builder.py`: network,
scraping, enrichment) and every consumer (the TUI, `scene list`, a second
client). It knows the file's shape and nothing else: no collectors, no
Textual, no network. `tests/test_scene_dataset.py` holds it to that.

One JSON file, written whole to a temp file and renamed into place, so a
reader sees the old dataset or the new one and never a half-written mix, and
needs no lock. Default `~/.local/share/twiddle/scene/dataset.json`, or
`$TWIDDLE_SCENE_DATASET`.

    {"schema": "twiddle.scene.dataset", "version": 1,
     "id": "bay-area-music", "name": "Bay Area live music",     # which dataset this is:
     "region": "San Francisco Bay Area, CA", "kind": "music",   # all four optional
     "generated_at": "2026-09-29T18:00:00+00:00", "builder": "twiddle 0.1",
     "complete": true,            # false while a build is still enriching bands
     "sources":  {name: {"ok", "fetched_at", "count", "error"}},
     "enrichers": {"lookup": "ok", "spotify": "skipped: not signed in", ...},
     "venues": [{"id", "name", "match", "address", "url", "about", "wikipedia",
                 "wikipedia_summary", "instagram", "map_url"}],
     "shows":  [{"id", "venue_id", ...Show.to_dict(): source, also, source_url,
                 tickets, flyer -- where each fact came from}],
     "bands":  {<lookup.norm(name)>: {"name", "updated_at", "status",
                 "identifiers", "genre", "confidence", "why", ...}}}

Identities are stable across builds: a show's `id` is a hash of its night,
room and headliner (or its title), a venue's is its slug, a band's key is its
normalised name, and a band record carries its MusicBrainz / Spotify /
Bandcamp ids. A band's `confidence` is graded *without* anyone's hand-made
Spotify pins -- those are one person's choices and stay in `cache.py`.

Compatibility: unknown keys are ignored, a malformed row is skipped, and a
file whose `version` is newer than `VERSION` is refused with a message rather
than half-understood. Adding a key does not bump the version; changing what
one means does. Lookup indexes (by day, by venue) are built in memory by the
reader; nothing derived is stored, so nothing needs rebuilding.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from ..lookup import norm
from .model import Show

SCHEMA = "twiddle.scene.dataset"
VERSION = 1
IDENTITY_KEYS = ("id", "name", "region", "kind")      # the optional header: which dataset
STALE_S = 24 * 3600         # a scheduled build that has missed a day is worth saying so
DATASET_PATH = Path(os.environ.get("TWIDDLE_SCENE_DATASET",
                                   Path.home() / ".local" / "share" / "twiddle" / "scene"
                                   / "dataset.json"))


class DatasetError(RuntimeError):
    """The file exists but cannot be used (unreadable, or a newer version)."""


class DatasetCorrupt(DatasetError):
    """Not valid JSON: a torn or damaged file, safe for a build to replace.
    Any other DatasetError (a newer version, another schema) is not: replacing
    it would destroy data this code cannot read."""


def default_path() -> Path:
    return DATASET_PATH


# ---- identities -------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "-", norm(text)).strip("-")


def venue_id(name: str) -> str:
    return _slug(name)


def band_id(name: str) -> str:
    """A band's key in `bands`: the name as `lookup.norm` spells it."""
    return norm(name)


def show_id(s: Show, room: str | None = None) -> str:
    """Stable across builds for the same night, room and headliner.

    A night that bills no bands is told apart by its title and times, or two
    differently named events in one room on one day would share an id.
    """
    who = s.headliner if s.bands else f"{s.title}|{s.times or ''}"
    raw = "|".join((s.day.isoformat(), _slug(room or s.venue), _slug(who)))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def unique_ids(pairs: list[tuple[Show, str | None]]) -> list[str]:
    """`show_id` for each (show, room), with `-2`, `-3` for true repeats."""
    seen: dict[str, int] = {}
    out = []
    for s, room in pairs:
        i = show_id(s, room)
        seen[i] = seen.get(i, 0) + 1
        out.append(i if seen[i] == 1 else f"{i}-{seen[i]}")
    return out


# ---- the reader -------------------------------------------------------------


def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


def iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch,
                                  timezone.utc).isoformat(timespec="seconds")


@dataclass
class Snapshot:
    """One read of the dataset. Immutable in spirit: read again to see a newer one."""
    path: Path
    version: int
    generated_at: float | None
    builder: str = ""
    id: str = ""            # which dataset this is; "" in a file that predates the header
    name: str = ""          # a human label ("Bay Area live music")
    region: str = ""        # where it covers
    kind: str = ""          # what it covers: "music", "comedy", ...
    complete: bool = True
    sources: dict[str, dict] = field(default_factory=dict)
    enrichers: dict[str, str] = field(default_factory=dict)
    venues: list[dict] = field(default_factory=list)
    shows: list[Show] = field(default_factory=list)
    show_ids: list[str] = field(default_factory=list)      # parallel to `shows`
    venue_ids: list[str | None] = field(default_factory=list)   # ditto: the watched room
    bands: dict[str, dict] = field(default_factory=dict)
    mtime: float = 0.0

    @property
    def label(self) -> str:
        """What to call this dataset: its name, else its id, else its file."""
        return self.name or self.id or self.path.stem

    def age_s(self, now: float | None = None) -> float | None:
        if self.generated_at is None:
            return None
        return (time.time() if now is None else now) - self.generated_at

    def stale(self, now: float | None = None) -> bool:
        age = self.age_s(now)
        return age is None or age > STALE_S

    def band(self, name: str) -> dict | None:
        return self.bands.get(band_id(name))

    def venue(self, name_or_id: str) -> dict | None:
        want = _slug(name_or_id)
        return next((v for v in self.venues
                     if v.get("id") == want or _slug(v.get("name", "")) == want), None)

    def shows_on(self, day: date) -> list[Show]:
        return [s for s in self.shows if s.day == day]

    def shows_at(self, venue: str) -> list[Show]:
        """Shows whose room is this venue (by name or id)."""
        v = self.venue(venue)
        vid = v["id"] if v else _slug(venue)
        want = _slug(venue)
        out = []
        for s, i in zip(self.shows, self.venue_ids):
            if i == vid or (i is None and want and want in _slug(s.venue)):   # older files: no id
                out.append(s)
        return out


def _show_from(row: dict) -> Show | None:
    try:
        d = {k: v for k, v in row.items() if k in Show.__dataclass_fields__}
        d["day"] = date.fromisoformat(d["day"])
        return Show(**d)
    except (KeyError, TypeError, ValueError):
        return None


def load(path: Path | None = None) -> Snapshot | None:
    """The dataset at `path`, or None if there is none yet.

    Raises DatasetError for a file that is not this schema, is unreadable, or
    was written by a newer version -- "run the builder" would not fix those.
    """
    path = path or default_path()
    try:
        with open(path) as f:
            raw = f.read()
            mtime = os.fstat(f.fileno()).st_mtime   # of what was read, not of a later rename
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatasetError(f"cannot read {path}: {exc}") from exc
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatasetCorrupt(f"{path} is not valid JSON ({exc}); "
                             "`twiddle scene build` will replace it") from exc
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise DatasetError(f"{path} is not a {SCHEMA} file")
    version = doc.get("version")
    if not isinstance(version, int) or version > VERSION:
        raise DatasetError(f"{path} is dataset version {version}; this twiddle reads up to "
                           f"{VERSION} -- update twiddle")
    shows, ids, venues_of = [], [], []
    for row in doc.get("shows", []):
        s = _show_from(row) if isinstance(row, dict) else None
        if s is None:
            continue
        shows.append(s)
        ids.append(str(row.get("id") or show_id(s)))
        venues_of.append(row.get("venue_id"))
    bands = {k: v for k, v in (doc.get("bands") or {}).items() if isinstance(v, dict)}
    return Snapshot(
        path=path, version=version, generated_at=_epoch(doc.get("generated_at")),
        builder=str(doc.get("builder", "")), complete=bool(doc.get("complete", True)),
        id=str(doc.get("id") or ""), name=str(doc.get("name") or ""),
        region=str(doc.get("region") or ""), kind=str(doc.get("kind") or ""),
        sources=doc.get("sources") or {}, enrichers=doc.get("enrichers") or {},
        venues=[v for v in doc.get("venues", []) if isinstance(v, dict)],
        shows=shows, show_ids=ids, bands=bands, mtime=mtime, venue_ids=venues_of)


def load_all(paths: list[Path] | None = None) -> list[Snapshot]:
    """Every dataset that exists at `paths` (default: the one dataset there is
    today), in order. A missing file is skipped, as `load` answers None; an
    unusable one raises, naming its path. A client that follows several
    datasets (another city, comedy) reads them through this."""
    out = []
    for path in paths if paths is not None else [default_path()]:
        snap = load(path)
        if snap is not None:
            out.append(snap)
    return out


def mtime(path: Path | None = None) -> float | None:
    """When the file last changed, for "is there a newer dataset?" without parsing it."""
    try:
        return (path or default_path()).stat().st_mtime
    except OSError:
        return None


# ---- the writer -------------------------------------------------------------


def document(*, shows: list[Show], show_rows: list[dict] | None = None, venues: list[dict],
             bands: dict[str, dict], sources: dict[str, dict], enrichers: dict[str, str],
             complete: bool, builder: str, generated_at: float | None = None,
             identity: dict[str, str] | None = None) -> dict:
    """`identity`: any of id / name / region / kind, written when given."""
    head = {k: v for k, v in (identity or {}).items() if k in IDENTITY_KEYS and v}
    return {"schema": SCHEMA, "version": VERSION, **head, "generated_at": iso(generated_at),
            "builder": builder, "complete": complete, "sources": sources,
            "enrichers": enrichers, "venues": venues,
            "shows": show_rows if show_rows is not None else [s.to_dict() for s in shows],
            "bands": bands}


def publish(doc: dict, path: Path | None = None) -> Path:
    """Write `doc` so that readers never see a partial file.

    Temp file in the same directory (a rename across filesystems is not
    atomic), flushed to disk, then `os.replace`d over the old one.
    """
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(doc, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path
