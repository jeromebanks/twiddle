"""The EventSource contract.

A source is anything that can say "these bands play this venue on this
date": a listings aggregator, a venue's own calendar, a ticketing API. Each
returns plain `Show`s and records where it got them, so several sources can
be merged (`model.dedupe`) without any of them knowing about the others.

The List is the first source: it covers most rooms, with age and price. It
does not cover Yoshi's (2026-09-25), whose own calendar is the second. The
Stork Club's site, Gilman's ShowSlinger page and Ivy Room's VenuePilot
calendar (2026-09-26) add what The
List lacks for rooms it does cover: flyers and ticket links. (An earlier
note here called the Stork Club's domain parked; by 2026-09-26 it served a
full calendar.) Order matters: `dedupe` keeps the first source's facts and
fills the gaps from the rest. Songkick or Bandsintown
would each be one more module here.
"""
from __future__ import annotations

from typing import Protocol

from ..model import Show, dedupe


class SourceError(RuntimeError):
    """A source could not be fetched or parsed."""


class EventSource(Protocol):
    name: str

    def fetch(self) -> list[Show]:
        """Every upcoming show this source knows about. Raises SourceError."""
        ...


def _registry() -> dict[str, EventSource]:
    from .gilman import Gilman
    from .grayarea import GrayArea
    from .makeoutroom import MakeOutRoom
    from .simplecal import VENUES as SIMPLECAL, SimpleCalendar
    from .squarespace import VENUES as SQUARESPACE, Squarespace
    from .venuepilot import VENUES as VENUEPILOT, VenuePilot
    from .seetickets import VENUES as SEETICKETS, SeeTickets
    from .ticketweb import VENUES as TICKETWEB, TicketWeb
    from .thelist import TheList
    from .yoshis import Yoshis
    return {"thelist": TheList(), "yoshis": Yoshis(),
            **{name: SeeTickets(name) for name in SEETICKETS},
            **{name: TicketWeb(name) for name in TICKETWEB},
            **{name: VenuePilot(name) for name in VENUEPILOT},
            **{name: Squarespace(name) for name in SQUARESPACE},
            **{name: SimpleCalendar(name) for name in SIMPLECAL},
            "gilman": Gilman(), "grayarea": GrayArea(), "makeoutroom": MakeOutRoom()}


SOURCES: dict[str, EventSource] = {}


def sources() -> dict[str, EventSource]:
    if not SOURCES:
        SOURCES.update(_registry())
    return SOURCES


def fetch_all(chosen: list[EventSource] | None = None,
              stale: list[Show] | None = None) -> tuple[list[Show], list[str]]:
    """Shows from every source, merged, plus an error line per source that failed.

    One broken source must not blank the whole app, so failures are reported
    alongside whatever the others returned. A failed source keeps its shows
    from `stale` (the cache), or a Yoshi's-only refresh would overwrite six
    hours of The List whenever foopee.com was briefly down.
    """
    shows: list[Show] = []
    errors: list[str] = []
    for src in chosen if chosen is not None else list(sources().values()):
        try:
            shows += src.fetch()
        except Exception as exc:
            # Not only SourceError: a venue site that changes its markup can
            # break a parser anywhere, and the app's worker would quit on it.
            what = str(exc) if isinstance(exc, SourceError) else \
                f"could not read it ({type(exc).__name__}: {exc})"
            errors.append(f"{src.name}: {what}")
            # Merged shows too: a Stork Club flyer lives on The List's row.
            shows += [s for s in stale or [] if src.name in (s.source, *s.also)]
    return dedupe(shows, _room()), errors


def _room():
    """A source's venue spelling -> the watched venue's name, if it is one."""
    from .. import venues
    try:
        watched = venues.watched()
    except ValueError:          # a broken config is reported elsewhere
        watched = venues.DEFAULT_VENUES

    def room(listed: str) -> str:
        v = venues.find(watched, listed)
        return v.name if v else listed
    return room
