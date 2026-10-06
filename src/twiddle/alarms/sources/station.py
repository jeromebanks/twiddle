"""Any station in dial's catalog (`stations/catalog/`), played the way `tune`
plays it: an `x-rincon-mp3radio://` URI the speaker fetches itself, so this
Mac has no part in it once the alarm is set."""
from __future__ import annotations

from difflib import get_close_matches

from ... import play
from ...stations.model import load_catalog
from . import Choice, Source


class Station(Source):
    name = "station"
    title = "dial station"
    needs_mac = False
    fallback = "the Sonos chime (assumed, not yet verified on a speaker)"
    takes_choice = True

    _stations: dict | None = None

    def _catalog(self):
        """Read once a process: `alarm list` asks about every alarm."""
        if self._stations is None:
            self._stations = load_catalog()
        return self._stations

    def choices(self, query: str = "") -> list[Choice]:
        q = query.strip().lower()
        return [Choice(key, st.name, st.blurb) for key, st in self._catalog().items()
                if not q or q in key or q in st.name.lower() or q in st.blurb.lower()
                or q in st.tags]

    def build(self, choice: str = "") -> tuple[str, str]:
        catalog = self._catalog()
        st = catalog.get(choice.strip().lower())
        if st is None:
            near = get_close_matches(choice.strip().lower(), list(catalog), n=3)
            raise ValueError(f"no station {choice!r} in the catalog"
                             + (f" (did you mean {', '.join(near)}?)" if near else "")
                             + ": `alarm sources station` lists them")
        return play.radio_uri(st.url), play.radio_didl(st.name, st.logo)

    def owns(self, uri: str, metadata: str) -> bool:
        return uri.startswith(play.RADIO_SCHEME) and any(
            uri == play.radio_uri(st.url) for st in self._catalog().values())
