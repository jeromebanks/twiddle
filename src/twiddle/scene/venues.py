"""Which venues to watch, and how to recognise them in a source's spelling.

Sources name venues their own way ("Thee Stork Club, Oakland"), so a watched
venue is matched on lowercase substrings, not equality. The defaults are
Bay Area rooms. Add more in `~/.config/twiddle/scene.toml`:

    [[venue]]
    name  = "Cornerstone"
    match = ["cornerstone, berkeley"]
    icon  = "https://example.com/logo.png"    # optional; a tile if absent
    address = "2367 Shattuck Ave, Berkeley"   # optional, as are url, about,
    url     = "https://..."                   # and wikipedia (an article title)

A venue named like a built-in one gets its details (`venue_info.INFO`)
unless the file gives its own.

A `[[venue]]` list in the file replaces the defaults entirely, so that
removing one is possible too.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .venue_info import INFO, VenueInfo

CONFIG_PATH = Path(os.environ.get("TWIDDLE_SCENE_CONFIG",
                                  Path.home() / ".config" / "twiddle" / "scene.toml"))


@dataclass(frozen=True)
class Venue:
    name: str                                   # what the app shows
    match: tuple[str, ...] = field(default=())  # lowercase substrings of a source's name
    icon: str | None = None                     # a logo URL, hand-picked (see below)
    info: VenueInfo = field(default=VenueInfo(), compare=False)   # address, site, about

    def matches(self, listed: str) -> bool:
        low = listed.lower()
        return any(m in low for m in self.match)


# Icons are picked by hand, not scraped: measured 2026-09-25, a venue's own
# favicon is too often wrong -- staygolddeli.com redirects to a running club
# and Gilman's is Wix's generic one, so those get a generated tile. (That
# note also said the Stork Club's site offered none; by 2026-09-26 it served
# the stork logo below.)
_SQSP = "https://static1.squarespace.com/static/61158978c9078a1340cf6e25/t/"
_WIX = "https://static.wixstatic.com/media/"
DEFAULT_VENUES = (
    Venue("Stork Club", ("stork club",),
          "https://theestorkclub.com/wp-content/uploads/2022/03/Stork-Black-300x300.png"),
    Venue("Ivy Room", ("ivy room",),
          _SQSP + "6a0e8a46ed628551dadd6830/1779337798830/lips_bw+%282%29.png?format=300w"),
    Venue("Stay Gold Deli", ("stay gold deli",)),
    Venue("Eli's Mile High", ("mile high club",),
          _WIX + "4a4730_e461637cb44c4cb98f50efa930ae0bfd%7Emv2.jpg/v1/fill/"
          "w_192%2Ch_192%2Clg_1%2Cusm_0.66_1.00_0.01/"
          "4a4730_e461637cb44c4cb98f50efa930ae0bfd%7Emv2.jpg"),
    # The List spells Gilman at least seven ways; "924 gilman" catches them
    # all and not Gilman Brewing's own events at 912.
    Venue("924 Gilman", ("924 gilman",),
          _WIX + "b031ff_90d65963a776454eb03f6d82e1522a19%7Emv2.jpg/v1/fit/"
          "w_600,h_320,al_c/b031ff_90d65963a776454eb03f6d82e1522a19%7Emv2.jpg"),
    Venue("Great American", ("great american music hall",),
          "https://gamh.com/wp-content/uploads/2021/08/cropped-favicon-1-192x192.png"),
    # "yoshi", not "yoshi's": survives a curly apostrophe. The List carries no
    # Yoshi's (2026-09-25); its shows come from `sources/yoshis.py`.
    Venue("Yoshi's", ("yoshi",)),
    # The List's spellings, 2026-09-25. It also lists a "Fox Theater, 2215
    # Broadwa, Redwood City", which the Oakland match leaves out.
    Venue("Greek Theatre", ("greek theater, uc berkeley", "greek theatre, uc berkeley",
                            "greek theater, berkeley", "greek theatre, berkeley")),
    Venue("Fox Theater", ("fox theater, oakland", "fox theatre, oakland")),
    Venue("Paramount", ("paramount theatre, oakland", "paramount theater, oakland")),
    Venue("The Midway", ("midway, s.f.",)),
    # ---- more East Bay and SF rooms, added 2026-09-25 -------------------------
    # Chosen from The List's own venue counts (shows in the next ~5 months)
    # plus a web check that each is still open. Match strings are The List's
    # spellings; `shows list --all-venues` shows them.
    # East Bay
    Venue("UC Theatre", ("uc theater, berkeley", "uc theatre, berkeley")),
    Venue("Cornerstone", ("cornerstone, berkeley",)),
    Venue("Freight", ("freight, berkeley", "freight & salvage", "freight and salvage")),
    Venue("Starry Plough", ("starry plough",)),
    Venue("Crybaby", ("crybaby, oakland",)),
    Venue("Buzzard", ("first church of the buzzard",)),
    # SF. Bottom of the Hill closes after New Year's Eve 2026 (SF Chronicle,
    # 2026-01-02); drop it then.
    Venue("Bottom of the Hill", ("bottom of the hill",)),
    Venue("Independent", ("independent, s.f.",)),
    Venue("Rickshaw Stop", ("rickshaw stop",)),
    Venue("Fillmore", ("fillmore, s.f.",)),           # not "Fillmore West"
    Venue("Kilowatt", ("kilowatt, s.f.",)),
    Venue("Chapel", ("chapel, s.f.", "chapel outdoor")),
    Venue("Regency", ("regency ballroom",)),
    Venue("August Hall", ("august hall",)),
    Venue("Warfield", ("warfield",)),
    Venue("Masonic", ("masonic, s.f.",)),
    Venue("Bimbo's", ("bimbo's", "bimbos")),
    Venue("Brick & Mortar", ("brick and mortar", "brick & mortar")),
    Venue("DNA Lounge", ("dna lounge",)),
    Venue("Castro Theatre", ("castro, s.f.", "castro theater, s.f.", "castro theatre, s.f.")),
    Venue("Great Northern", ("great northern, s.f.",)),
    Venue("Cafe du Nord", ("cafe du nord", "café du nord")),
    Venue("Swedish Am. Hall", ("swedish american hall",)),
    Venue("Knockout", ("knockout, s.f.",)),
    Venue("Hotel Utah", ("hotel utah",)),
    Venue("Neck of the Woods", ("neck of the woods",)),
    Venue("4 Star", ("4 star theater", "4 star theatre")),
    Venue("Black Cat", ("black cat, s.f.",)),
    Venue("Civic", ("civic auditorium, s.f.",)),
    # Neither was on The List on 2026-09-25 (Pussy Palace has been before), and
    # no other source has a calendar the app can read; see venue_info.py.
    Venue("Oakland Secret", ("oakland secret", "oakland.secret")),
    Venue("Pussy Palace", ("pussy palace",)),
    # ---- added 2026-09-26 on request; all but Gray Area are missing from The
    # List, so their own calendars (sources/) are what fills them -----------
    Venue("Ashkenaz", ("ashkenaz",)),
    Venue("Sound Room", ("sound room",)),
    Venue("Spats", ("spats, berkeley", "spatz")),
    Venue("The DeLuxe", ("deluxe, s.f.",)),
    Venue("Gray Area", ("gray area", "grey area")),
    Venue("Make-Out Room", ("make-out room", "make out room", "makeout room")),
    # ---- added 2026-09-29 (issue #2). Neither is on The List; KALX's weekly
    # calendar (sources/kalx.py) carries both. ---------------------------
    Venue("Hillside Club", ("hillside club",)),
    Venue("Sweetwater", ("sweetwater music hall",)),
)
DEFAULT_VENUES = tuple(replace(v, info=INFO.get(v.name, VenueInfo())) for v in DEFAULT_VENUES)


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def watched(config: dict | None = None) -> tuple[Venue, ...]:
    config = load_config() if config is None else config
    listed = config.get("venue")
    if not listed:
        return DEFAULT_VENUES
    return tuple(Venue(v["name"], tuple(m.lower() for m in v.get("match", [v["name"]])),
                       v.get("icon"), _info(v))
                 for v in listed)


def _info(v: dict) -> VenueInfo:
    """The file's details, falling back field by field to the built-in ones."""
    known = INFO.get(v["name"], VenueInfo())
    return VenueInfo(**{f: v.get(f) or getattr(known, f)
                        for f in ("address", "url", "about", "wikipedia")})


def find(venues: tuple[Venue, ...], listed: str) -> Venue | None:
    """The watched venue a source's venue name refers to, if any."""
    return next((v for v in venues if v.matches(listed)), None)


def display_name(venues: tuple[Venue, ...], listed: str) -> str:
    """A watched venue's short name, else the source's name without its city."""
    v = find(venues, listed)
    return v.name if v else listed.rsplit(",", 1)[0].strip()


def resolve(venues: tuple[Venue, ...], query: str) -> Venue | None:
    """`--venue stork` -> the Stork Club: a partial, case-insensitive name.

    A match at the start of a word wins over one inside a word, or "eli"
    would find Stay Gold D*eli* before Eli's; and the venue's own name wins
    over its match strings, or "uc" would find the Greek ("... UC Berkeley
    Campus") before the UC Theatre.
    """
    q = query.lower()

    def starts(text: str) -> bool:
        words = text.replace(",", " ").split()
        return any(" ".join(words[i:]).startswith(q) for i in range(len(words)))

    return (next((v for v in venues if starts(v.name.lower())), None)
            or next((v for v in venues if any(starts(m) for m in v.match)), None)
            or next((v for v in venues if q in v.name.lower()
                     or any(q in m for m in v.match)), None))
