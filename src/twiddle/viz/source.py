"""What to tap for whatever is playing -- or, as honestly, why there is nothing.

| playing | tapped |
|---|---|
| a station on a Sonos room, this Mac or Bluetooth | the station's URL, a second connection |
| a Bandcamp track | the URL we started, token and all (`Outputs.owned_url`) |
| Spotify through the relay | the relay's stream, as an *observer* |
| Spotify on this Mac's librespot, a phone, a Sonos's own Spotify | nothing: no stream exists outside the player |

The relay is tapped only when it says it keeps observers apart from its
speaker counters (`relay.OBSERVER_HEADER` on a HEAD, which subscribes no
one). An older relay would count the visualizer as a Roam in `listeners`
and, if it stalled, in `dropped_chunks` -- the experiment's evidence -- so
then it isn't tapped at all, and never restarted from here.

Every function here may touch the network (the relay's HEAD): call from a
worker thread.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import requests

from .. import relay, stations, supervisor


@dataclass(frozen=True)
class TapSource:
    url: str | None          # None: nothing to tap, and `reason` says why
    label: str               # what is playing where, for the overlay
    key: str = ""            # the output, to remember its delay by
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.url is not None


def _head(url: str) -> dict:
    return dict(requests.head(url, timeout=2).headers)


def relay_source(label: str, key: str, *, status: Callable = supervisor.status,
                 head: Callable[[str], dict] = _head) -> TapSource:
    st = status()
    if not st.running:
        return TapSource(None, label, key, "the relay isn't running")
    argv = (st.record or {}).get("argv", [])
    port = argv[argv.index("--port") + 1] if "--port" in argv[:-1] else relay.DEFAULT_PORT
    url = f"http://127.0.0.1:{port}{relay.STREAM_PATH}"
    try:
        headers = {k.lower(): v for k, v in head(url).items()}
    except Exception as exc:
        return TapSource(None, label, key, f"the relay didn't answer: {type(exc).__name__}")
    if headers.get(relay.OBSERVER_HEADER.lower()) != "1":
        return TapSource(None, label, key,
                         "this relay predates the visualizer: it would count it as a speaker. "
                         "Restart the relay when it suits you (relay down, relay up)")
    return TapSource(f"{url}?{relay.OBSERVER_QUERY}", label, key)


def _fetchable(uri: str) -> str | None:
    """A speaker's reported URI (scheme stripped by `bare`) back to a URL,
    when it is a plain stream; None for a Sonos-native source."""
    if not uri or uri.startswith("x-") or ":" in uri.split("/", 1)[0]:
        return None
    return uri if uri.startswith(("http://", "https://")) else f"http://{uri}"


def for_dial(outputs, output, st, *, relay_lookup: Callable = relay_source) -> TapSource:
    """dial: `output` is the chosen `Output`, `st` its latest `OutputState`."""
    from ..dial.output import RELAY, same_stream
    where = output.label
    if st.tuned == RELAY:
        return relay_lookup(f"Spotify via the relay, on {where}", output.id)
    owned = outputs.owned_url(output.id)
    station = stations.STATIONS.get(st.tuned or "")
    name = station.name if station else (st.other or "")
    label = f"{name} on {where}" if name else where
    if not st.playing:
        return TapSource(None, label, output.id, f"{where} isn't playing")
    if owned and same_stream(owned, st.uri):
        return TapSource(owned, label, output.id)
    if station is not None:
        return TapSource(station.url, label, output.id)
    url = _fetchable(st.uri)
    if url is None:
        return TapSource(None, label, output.id,
                         f"{where} is playing {st.other or 'something'} from its own source: "
                         "there's no stream here to tap")
    return TapSource(url, label, output.id)


def for_scene(device, bc_now: dict | None, now: dict, outputs, *,
              relay_lookup: Callable = relay_source) -> TapSource:
    """scene: the chosen Spotify `device`, the Bandcamp track if one is
    playing, and Spotify's now-playing dict."""
    if bc_now:
        label = f"{bc_now.get('track', '?')} – {bc_now.get('band', '?')} on {bc_now.get('label', '?')}"
        url = outputs.owned_url(bc_now["output"]) if outputs is not None else None
        if url:
            return TapSource(url, label, bc_now["output"])
        return TapSource(None, label, bc_now["output"], "lost track of the Bandcamp stream")
    track = now.get("track")
    what = f"{track} – {', '.join(now.get('artists') or [])}" if track else "Spotify"
    if device is None:
        return TapSource(None, what, "", "no device chosen: press d")
    if device.relay:
        return relay_lookup(f"{what} via the relay", "relay")
    if device.local:
        return TapSource(None, what, "local",
                         "Spotify on this Mac plays through librespot straight to the speakers: "
                         "there's no stream to tap")
    return TapSource(None, what, device.id,
                     f"Spotify plays on {device.name} itself: there's no stream here to tap")
