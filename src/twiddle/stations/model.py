"""What a station is, what it reports, and loading them from `catalog/`.

Each station is one TOML file in `catalog/`, named for its key:

    name  = "KEXP"
    url   = "http://kexp.streamguys1.com/kexp160.aac"
    blurb = "Seattle -- indie/alt, well-curated"      # "City -- what it is"
    tags  = ["public", "indie"]                        # from tags.TAGS only
    fetch = "kexp"                                     # fetchers.FETCHERS; default "icy"
    fetch_args = { rf_id = 74 }                        # optional, passed to the fetcher
                                                       # (`titles` must be in titles.SHAPES)
    logo  = "https://..."                              # optional, square
    order = 40                                         # optional; lower lists first

A bad file is skipped with a warning rather than failing the import: the
launchd daemon imports this module (via `cli.py`), and a half-written
station must not stop the evidence collector. `tests/test_catalog.py`
loads with `strict=True`, which is where mistakes are caught.
"""
from __future__ import annotations

import sys
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path

CATALOG_DIR = Path(__file__).parent / "catalog"
DEFAULT_ORDER = 1000
FIELDS = {"name", "url", "blurb", "tags", "fetch", "fetch_args", "logo", "order"}


@dataclass
class NowPlaying:
    source: str                      # station name, or "Spotify"
    artist: str | None = None
    song: str | None = None
    album: str | None = None
    show: str | None = None
    hosts: list[str] = field(default_factory=list)
    raw_title: str | None = None     # the unparsed ICY string, when that is all there is
    note: str | None = None
    # MusicBrainz ids, when the station hands them over (KEXP does). They
    # make an info lookup exact instead of a name search.
    mb_artist_id: str | None = None
    mb_release_group_id: str | None = None
    # Cover art for what is playing (KEXP, Spinitron and Spotify hand it
    # over), else None.
    art_url: str | None = None
    # What aired before this, newest first, where the station publishes it
    # (KEXP's API, a Spinitron page): dicts of time/artist/song/album/art_url.
    recent: list[dict] = field(default_factory=list)
    # A station that airs a fixed program schedule (KQED) rather than a
    # playlist: today's slots in air order, dicts of time/show/episode/host
    # plus `on_now` on the current one. Shown instead of "just played".
    schedule: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [])}

    def render(self) -> str:
        lines = [self.source]
        if self.artist or self.song:
            line = " - ".join(x for x in (self.artist, self.song) if x)
            lines.append(f"  now playing: {line}" + (f"  [{self.album}]" if self.album else ""))
        else:
            lines.append(f"  now playing: {self.raw_title or '(the station gave no title right now)'}")
        if self.show:
            lines.append(f"  show:        {self.show}"
                         + (f" with {', '.join(self.hosts)}" if self.hosts else ""))
        if self.note:
            lines.append(f"  {self.note}")
        return "\n".join(lines)


def _icy_fetch(station: Station) -> NowPlaying:
    from .fetchers.icy import icy   # fetchers import this module
    return icy(station)


@dataclass(frozen=True)
class Station:
    key: str
    name: str
    url: str
    blurb: str
    fetch: Callable[[Station], NowPlaying] = _icy_fetch
    # A square station logo: `tune` hands it to the speaker as album art (so
    # the Sonos app shows it), and `np` draws it when a song has no cover.
    # Taken from each station's own site, or its Spinitron page where the
    # site has none; the speaker fetches it itself, over the internet.
    logo: str | None = None
    tags: tuple[str, ...] = ()

    def now_playing(self) -> NowPlaying:
        return self.fetch(self)


class CatalogError(ValueError):
    pass


def station_from(key: str, data: dict) -> Station:
    """One catalog entry -> a Station, or CatalogError saying what's wrong."""
    from .fetchers import FETCHERS
    from .tags import TAGS
    from .titles import SHAPES

    unknown = set(data) - FIELDS
    if unknown:
        raise CatalogError(f"unknown field(s) {sorted(unknown)}")
    missing = [f for f in ("name", "url", "blurb") if not data.get(f)]
    if missing:
        raise CatalogError(f"missing {', '.join(missing)}")
    tags = tuple(data.get("tags") or ())
    bad = [t for t in tags if t not in TAGS]
    if bad:
        raise CatalogError(f"tag(s) {bad} not in stations/tags.py")
    name = data.get("fetch", "icy")
    if name not in FETCHERS:
        raise CatalogError(f"fetch = {name!r} is not a known fetcher "
                           f"({', '.join(sorted(FETCHERS))})")
    shape = (data.get("fetch_args") or {}).get("titles")
    if shape is not None and (not isinstance(shape, str) or shape not in SHAPES):
        raise CatalogError(f"titles = {shape!r} is not a known title shape "
                           f"({', '.join(sorted(SHAPES))})")
    fetch = FETCHERS[name]
    if data.get("fetch_args"):
        fetch = partial(fetch, **data["fetch_args"])
    return Station(key, data["name"], data["url"], data["blurb"], fetch,
                   logo=data.get("logo") or None, tags=tags)


def load_catalog(directory: Path = CATALOG_DIR, *, strict: bool = False) -> dict[str, Station]:
    """Every station in `directory`, in list order (`order`, then name)."""
    found: list[tuple[int, str, Station]] = []
    for path in sorted(directory.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text())
            found.append((int(data.get("order", DEFAULT_ORDER)), data.get("name", "").lower(),
                          station_from(path.stem, data)))
        except (OSError, tomllib.TOMLDecodeError, CatalogError, TypeError, ValueError) as exc:
            if strict:
                raise CatalogError(f"{path.name}: {exc}") from exc
            print(f"twiddle: skipping station {path.name}: {exc}", file=sys.stderr)
    found.sort(key=lambda x: x[:2])
    return {s.key: s for _, _, s in found}
