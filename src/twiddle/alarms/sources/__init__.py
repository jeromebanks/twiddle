"""What an alarm plays, behind one interface.

A source is one module here with a `Source` subclass and a `register()` call
below: it lists or searches what it can play, builds the `ProgramURI` and
`ProgramMetaData` for one choice, says whether this Mac must be up when the
alarm fires (`needs_mac`) and what the room does if it can't play
(`fallback`), and recognises an alarm it built (`owns`). The CLI reads the
registry when it runs, so a new source needs no CLI edit: `alarm sources`
lists it and `--source <name>[:<choice>]` takes it.

A source spec is `<name>` or `<name>:<choice>`, split at the first colon so a
choice may hold one. `keep` is never a source: it is `alarm edit`'s word for
the source as it is.

Nothing here talks to a speaker; building a source writes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass

KEEP = "keep"


@dataclass(frozen=True)
class Choice:
    """One thing a source can play: what to put after `<name>:`, and what it is."""
    key: str
    title: str
    detail: str = ""


class Source:
    """A provider. Subclasses set the class attributes and override `build`;
    one that needs a choice (`takes_choice`) overrides `choices` too."""
    name: str = ""                  # the spec's word: `--source <name>[:...]`
    title: str = ""                 # how it is shown
    needs_mac: bool = False         # this Mac must be up when the alarm fires
    fallback: str = ""              # what the room does if it can't play
    takes_choice: bool = False      # `<name>:<choice>`, or the bare `<name>`

    def choices(self, query: str = "") -> list[Choice]:
        """What can be played, narrowed by `query` (empty: everything)."""
        return []

    def build(self, choice: str = "") -> tuple[str, str]:
        """`(ProgramURI, ProgramMetaData)` for one choice. ValueError says
        why it can't be built."""
        raise NotImplementedError

    def owns(self, uri: str, metadata: str) -> bool:
        """Whether an alarm's source is one this would build."""
        return False


_REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    """Add a source, or replace one of the same name."""
    if not source.name or source.name == KEEP or ":" in source.name:
        raise ValueError(f"can't register a source named {source.name!r}")
    _REGISTRY[source.name] = source
    return source


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def all_sources() -> list[Source]:
    """Every registered source, in the order registered."""
    return list(_REGISTRY.values())


def get(name: str) -> Source:
    try:
        return _REGISTRY[name.strip().lower()]
    except KeyError:
        raise ValueError(f"no source {name!r}: {', '.join(_REGISTRY)}"
                         " (`alarm sources` lists them)") from None


def build(spec: str) -> tuple[Source, str, str]:
    """`<name>[:<choice>]` -> the source, its ProgramURI and ProgramMetaData."""
    name, colon, choice = spec.strip().partition(":")
    source = get(name)
    if source.takes_choice and not choice.strip():
        raise ValueError(f"which {source.title}? --source {source.name}:<choice>"
                         f" (`alarm sources {source.name}` lists them)")
    if not source.takes_choice and colon:
        raise ValueError(f"{source.title} takes no choice: --source {source.name}")
    uri, metadata = source.build(choice.strip())
    return source, uri, metadata


def recognise(uri: str, metadata: str) -> Source | None:
    """The source that built an alarm's URI and metadata; None for one nobody
    here owns (iHeart, TuneIn, Sonos's own Spotify, ...)."""
    return next((s for s in _REGISTRY.values() if s.owns(uri, metadata)), None)


from .chime import Chime            # noqa: E402  (the stock sources register below)
from .station import Station        # noqa: E402

register(Chime())
register(Station())
