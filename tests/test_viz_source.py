"""What the visualizer taps for what's playing -- and that it says why when
there's nothing, instead of pretending."""
from types import SimpleNamespace

import pytest

from twiddle import relay, stations
from twiddle.dial.output import RELAY, OutputState, bare
from twiddle.scene.players import Device
from twiddle.viz import source
from twiddle.viz.source import TapSource

STATION = next(iter(stations.STATIONS.values()))


class FakeOutputs:
    def __init__(self, owned=None):
        self.owned = owned or {}

    def owned_url(self, oid):
        return self.owned.get(oid)


ROOM = SimpleNamespace(id="room:Roam", label="Roam")


def _relay(label, key):
    return TapSource("http://relay?observer", label, key)


def test_a_station_on_a_room_taps_the_station_itself():
    st = OutputState(tuned=STATION.key, playing=True, uri=bare(STATION.url))
    src = source.for_dial(FakeOutputs(), ROOM, st)
    assert src.url == STATION.url and STATION.name in src.label and src.key == "room:Roam"


def test_what_we_started_is_tapped_by_its_full_url_token_and_all():
    url = "https://t4.bcbits.com/stream/abc/mp3-128/1?p=0&ts=9&token=xyz"
    st = OutputState(playing=True, uri=bare(url), other="t4.bcbits.com/…")
    src = source.for_dial(FakeOutputs({"room:Roam": url}), ROOM, st)
    assert src.url == url


def test_the_relay_goes_through_the_relay_lookup():
    st = OutputState(tuned=RELAY, playing=True)
    src = source.for_dial(FakeOutputs(), ROOM, st, relay_lookup=_relay)
    assert src.url == "http://relay?observer"


@pytest.mark.parametrize("st, why", [
    (OutputState(tuned=None, playing=False), "isn't playing"),
    (OutputState(playing=True, uri="x-sonos-spotify:spotify%3atrack", other="Spotify"),
     "no stream here to tap"),
])
def test_nothing_to_tap_says_why(st, why):
    src = source.for_dial(FakeOutputs(), ROOM, st)
    assert src.url is None and why in src.reason


def test_an_unknown_plain_stream_is_fetched_over_http():
    st = OutputState(playing=True, uri="radio.example.com/live.mp3", other="radio.example.com")
    assert source.for_dial(FakeOutputs(), ROOM, st).url == "http://radio.example.com/live.mp3"


# ---- the relay: never counted as a speaker ------------------------------------


def _status(running=True, argv=None):
    return lambda: SimpleNamespace(running=running, record={"argv": argv or []})


def test_a_relay_that_knows_observers_is_tapped_as_one():
    seen = []

    def head(url):
        seen.append(url)
        return {relay.OBSERVER_HEADER: "1"}
    src = source.relay_source("x", "k", status=_status(argv=["relay", "start", "--port", "9001"]),
                              head=head)
    assert seen == [f"http://127.0.0.1:9001{relay.STREAM_PATH}"]
    assert src.url == f"http://127.0.0.1:9001{relay.STREAM_PATH}?{relay.OBSERVER_QUERY}"


def test_an_old_relay_is_left_alone_and_told_to_be_restarted():
    """It would count the visualizer in `listeners` -- the experiment's evidence."""
    src = source.relay_source("x", "k", status=_status(), head=lambda url: {"Content-Type": "audio/mpeg"})
    assert src.url is None and "restart" in src.reason.lower()


def test_no_relay_no_tap():
    src = source.relay_source("x", "k", status=_status(running=False), head=lambda u: 1 / 0)
    assert src.url is None and "isn't running" in src.reason


def test_a_relay_that_does_not_answer_is_no_tap():
    def head(url):
        raise ConnectionError("refused")
    src = source.relay_source("x", "k", status=_status(), head=head)
    assert src.url is None and "didn't answer" in src.reason


# ---- scene ----------------------------------------------------------------------


def test_scene_bandcamp_wins_and_uses_the_url_we_started():
    bc = {"output": "mac", "track": "Song", "band": "Band", "label": "This Mac"}
    src = source.for_scene(Device("x", "Roams", relay=True), bc, {}, FakeOutputs({"mac": "https://bc/1"}))
    assert src.url == "https://bc/1" and "Song" in src.label


def test_scene_spotify_through_the_relay_taps_the_relay():
    now = {"track": "So What", "artists": ["Miles Davis"]}
    src = source.for_scene(Device("id", "Roams", relay=True), None, now, FakeOutputs(),
                           relay_lookup=_relay)
    assert src.ok and "So What" in src.label


@pytest.mark.parametrize("dev, why", [
    (Device("id", "Mac (scene)", local=True), "librespot"),
    (Device("id", "Phone"), "Phone"),
    (None, "press d"),
])
def test_scene_spotify_elsewhere_has_no_stream_and_says_so(dev, why):
    src = source.for_scene(dev, None, {"track": "t"}, FakeOutputs())
    assert src.url is None and why in src.reason
