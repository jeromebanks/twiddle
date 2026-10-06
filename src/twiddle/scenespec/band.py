"""One band as a dataset describes it: identity answers and a confidence grade.

A `BandProfile` is what every producer fills in and the client displays. The
logic that *produces* its answers (enrichers, grading) lives with the
producer; this module is only the shape, so it imports no network code.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import lookup

CORROBORATED, NAME_ONLY, UNCERTAIN, NONE, PENDING, UNLOOKED = (
    "corroborated", "name_only", "uncertain", "none", "pending", "unlooked")


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
