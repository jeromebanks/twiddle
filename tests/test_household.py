"""The household model: naming, coordinator routing, and snapshot/restore.

These encode facts that were *measured* against the real speakers, not
assumed. Two of them contradict the obvious implementation, so if one fails,
re-read what it asserts before changing it:

* a bonded stereo pair's follower has ``is_satellite=False`` but is still not
  addressable -- ``Invisible`` is the flag that matters;
* a group's ID prefix is not its coordinator's UUID.
"""
import json

import pytest

from twiddle import household, play, topology
from twiddle.devices import Device
from twiddle.household import Household, Snapshot

# Trimmed from this household's real GetZoneGroupState. Note the two shapes of
# bond: the Beam's surrounds are <Satellite> elements, while the Roam pair are
# plain ZoneGroupMembers distinguished only by ChannelMapSet + Invisible.
# Note also that the Roam group's ID names ...9FBA01400 while its Coordinator
# attribute names ...B9F401400. That is real, and it is why the coordinator
# must be read from the attribute.
SAMPLE = """<ZoneGroupState><ZoneGroups>
<ZoneGroup Coordinator="RINCON_BEAM" ID="RINCON_BEAM:1803405565">
 <ZoneGroupMember UUID="RINCON_BEAM" Location="http://192.168.1.2:1400/xml/device_description.xml"
   ZoneName="Living Room (LF,RF)" HTSatChanMapSet="RINCON_BEAM:LF,RF;RINCON_LR:LR;RINCON_RR:RR"
   BootSeq="72" EthLink="0">
  <Satellite UUID="RINCON_LR" Location="http://192.168.1.13:1400/xml/device_description.xml"
    ZoneName="Living Room (LR)" Invisible="1"
    HTSatChanMapSet="RINCON_BEAM:LF,RF;RINCON_LR:LR" BootSeq="69" EthLink="0"/>
 </ZoneGroupMember>
</ZoneGroup>
<ZoneGroup Coordinator="RINCON_ROAM_L" ID="RINCON_ROAM_R:2831555993">
 <ZoneGroupMember UUID="RINCON_ROAM_L" Location="http://192.168.1.4:1400/xml/device_description.xml"
   ZoneName="Sonos Roam" BootSeq="12" EthLink="0"
   ChannelMapSet="RINCON_ROAM_L:LF,LF;RINCON_ROAM_R:RF,RF"/>
 <ZoneGroupMember UUID="RINCON_ROAM_R" Location="http://192.168.1.8:1400/xml/device_description.xml"
   ZoneName="Sonos Roam" Invisible="1" BootSeq="9" EthLink="0"
   ChannelMapSet="RINCON_ROAM_L:LF,LF;RINCON_ROAM_R:RF,RF"/>
</ZoneGroup>
</ZoneGroups></ZoneGroupState>"""

DEVICES = {
    "192.168.1.2": Device(ip="192.168.1.2", uuid="RINCON_BEAM",
                          zone_name="Living Room (LF,RF)", room="Living Room",
                          model="Sonos Beam"),
    "192.168.1.13": Device(ip="192.168.1.13", uuid="RINCON_LR",
                           zone_name="Living Room (LR)", room="Living Room",
                           model="Sonos Play:1"),
    "192.168.1.4": Device(ip="192.168.1.4", uuid="RINCON_ROAM_L",
                          zone_name="Sonos Roam (L)", room="Sonos Roam",
                          model="Sonos Roam"),
    "192.168.1.8": Device(ip="192.168.1.8", uuid="RINCON_ROAM_R",
                          zone_name="Sonos Roam (R)", room="Sonos Roam",
                          model="Sonos Roam"),
}


@pytest.fixture
def house():
    return Household.build(topology.parse(SAMPLE), DEVICES)


def _group(house, name):
    return house.resolve(name).group


def test_bonded_follower_is_not_addressable_even_when_not_a_satellite(house):
    """The Roam pair's right unit is a ZoneGroupMember, not a <Satellite>.

    Any routing rule written against is_satellite sends commands to a speaker
    that accepts and ignores them -- which looks exactly like a dropout.
    """
    right = next(s for s in house.speakers if s.ip == "192.168.1.8")
    assert right.is_satellite is False
    assert right.invisible is True
    assert right.addressable is False


def test_coordinator_comes_from_attribute_not_group_id_prefix(house):
    """The Roam group ID names the *other* unit. Trusting it inverts the pair."""
    group = _group(house, "Sonos Roam")
    assert group.gid.startswith("RINCON_ROAM_R")
    assert group.coordinator.uuid == "RINCON_ROAM_L"
    assert group.coordinator.ip == "192.168.1.4"


def test_naming_a_bonded_follower_redirects_and_says_so(house):
    res = house.resolve("Sonos Roam (R)")
    assert res.group.coordinator.ip == "192.168.1.4"
    assert res.redirected is True
    assert "not independently addressable" in res.reason
    assert res.to_dict()["redirected_from"].startswith("Sonos Roam (R)")


def test_addressing_by_ip_still_redirects(house):
    res = house.resolve("192.168.1.8")
    assert res.redirected is True
    assert res.group.ip == "192.168.1.4"


def test_coordinator_is_not_reported_as_redirected(house):
    assert house.resolve("Sonos Roam (L)").redirected is False


def test_partial_and_case_insensitive_names_resolve(house):
    for query in ("roam", "ROAM", "sonos roam", "Roam"):
        assert _group(house, query).coordinator.ip == "192.168.1.4"
    for query in ("living", "Living Room", "living room"):
        assert _group(house, query).coordinator.ip == "192.168.1.2"


def test_model_names_resolve_when_no_room_matches(house):
    """"beam" is a model here, not a room; an agent will still say it."""
    assert _group(house, "beam").coordinator.ip == "192.168.1.2"


def test_room_names_win_over_model_names(house):
    """A literal room name must never be shadowed by a model substring."""
    assert _group(house, "Living Room").coordinator.ip == "192.168.1.2"


def test_ambiguous_query_lists_candidates(house):
    with pytest.raises(household.Ambiguous) as exc:
        house.resolve("sonos ")   # matches both a Beam and a Roam by model
    assert len(exc.value.candidates) == 2


def test_unknown_name_lists_what_is_known(house):
    with pytest.raises(household.NotFound) as exc:
        house.resolve("kitchen")
    assert "Sonos Roam" in exc.value.known


def test_channel_is_read_for_this_unit_not_the_first_in_the_map(house):
    """Both halves of a bond carry the same map; find our own UUID in it."""
    by_ip = {s.ip: s for s in house.speakers}
    assert by_ip["192.168.1.4"].channel == "LF"
    assert by_ip["192.168.1.8"].channel == "RF"
    assert by_ip["192.168.1.13"].channel == "LR"


def test_channel_matches_the_documented_left_right_mapping(house):
    """In the fixture household .4 is LF/left. Listener reports depend on this."""
    by_ip = {s.ip: s for s in house.speakers}
    assert by_ip["192.168.1.4"].channel == "LF"
    assert "(L)" in by_ip["192.168.1.4"].name


# ---- snapshot / restore ----------------------------------------------------

class FakePlay:
    """Records writes instead of making them."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*a, **kw):
            self.calls.append((name, a, kw))
            if name == "get_volume":
                return 40
            if name == "get_mute":
                return False
            return None
        return record

    def verbs(self):
        return [c[0] for c in self.calls]


def test_restoring_a_stream_does_not_seek(monkeypatch, house):
    """Seek is a UPnP error on a live stream, not a harmless no-op."""
    fake = FakePlay()
    monkeypatch.setattr(household, "play", fake)
    snap = Snapshot(uri="x-sonosapi-stream:thetrip?sid=516&flags=8232",
                    is_stream=True, state="PLAYING", position="0:08:43",
                    volumes={"192.168.1.4": 43})
    snap.restore(_group(house, "roam"))
    assert "seek" not in fake.verbs()
    assert "set_uri" in fake.verbs()
    assert "play" in fake.verbs()


def test_restoring_a_queued_track_seeks_back_to_position(monkeypatch, house):
    fake = FakePlay()
    monkeypatch.setattr(household, "play", fake)
    snap = Snapshot(uri="http://192.168.1.50:8000/track.mp3", is_stream=False,
                    state="PLAYING", position="0:01:30",
                    volumes={"192.168.1.4": 43})
    snap.restore(_group(house, "roam"))
    assert "seek" in fake.verbs()


def test_restore_puts_volume_back(monkeypatch, house):
    """The documented normal is 43; the last session left the Roams at 32."""
    fake = FakePlay()
    monkeypatch.setattr(household, "play", fake)
    snap = Snapshot(volumes={"192.168.1.4": 43, "192.168.1.8": 43}, uri="")
    result = snap.restore(_group(house, "roam"))
    sets = [c for c in fake.calls if c[0] == "set_volume"]
    assert {c[1][1] for c in sets} == {43}
    assert not result["problems"]


def test_restore_puts_eq_back(monkeypatch, house):
    """Bass/treble/loudness/balance ride along with volume/mute, and apply
    even with no source to restore -- they're speaker state, not queue state."""
    fake = FakePlay()
    monkeypatch.setattr(household, "play", fake)
    snap = Snapshot(bass={"192.168.1.4": -2}, treble={"192.168.1.4": 3},
                    loudness={"192.168.1.4": False}, balance={"192.168.1.4": -5},
                    uri="")
    snap.restore(_group(house, "roam"))
    verbs = fake.verbs()
    for verb in ("set_bass", "set_treble", "set_loudness", "set_balance"):
        assert verb in verbs


def test_restore_leaves_a_stopped_group_stopped(monkeypatch, house):
    fake = FakePlay()
    monkeypatch.setattr(household, "play", fake)
    snap = Snapshot(uri="x-rincon-mp3radio://example/s", is_stream=True,
                    state="STOPPED")
    snap.restore(_group(house, "roam"))
    assert "play" not in fake.verbs()


def test_snapshot_survives_a_json_round_trip():
    snap = Snapshot(group="Sonos Roam", volumes={"192.168.1.4": 43},
                    bass={"192.168.1.4": -2}, treble={"192.168.1.4": 3},
                    loudness={"192.168.1.4": True}, balance={"192.168.1.4": -5},
                    uri="x-sonosapi-stream:thetrip?sid=516&flags=8232",
                    is_stream=True, state="PLAYING")
    again = Snapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
    assert again == snap


@pytest.mark.parametrize("shuffle,repeat,mode", [
    (False, "off", "NORMAL"),
    (False, "all", "REPEAT_ALL"),
    (False, "one", "REPEAT_ONE"),
    (True, "off", "SHUFFLE_NOREPEAT"),
    (True, "all", "SHUFFLE"),
    (True, "one", "SHUFFLE_REPEAT_ONE"),
])
def test_play_mode_encode_decode_round_trip(shuffle, repeat, mode):
    assert play.encode_play_mode(shuffle, repeat) == mode
    assert play.decode_play_mode(mode) == (shuffle, repeat)


def test_snapshot_ignores_unknown_fields_when_loading():
    """A snapshot written by a later version must not crash an earlier one."""
    snap = Snapshot.from_dict({"group": "Sonos Roam", "invented_later": True})
    assert snap.group == "Sonos Roam"


def test_stream_uris_are_detected_by_scheme():
    for uri in ("x-rincon-mp3radio://stream.kalx.berkeley.edu:8000/kalx-128.mp3",
                "x-sonosapi-stream:thetrip?sid=516"):
        assert household.is_stream(uri, {"TrackDuration": "0:00:00"})
    assert not household.is_stream(
        "http://192.168.1.50:8000/a.mp3", {"TrackDuration": "0:03:21"})


def test_anchor_comes_from_the_environment_when_not_passed(monkeypatch):
    """A Chromebook's Linux container never hears SSDP, but can reach a
    speaker directly: TWIDDLE_ANCHOR stands in for --anchor everywhere."""
    fetched = []

    def fetch(ip):
        fetched.append(ip)
        raise OSError("stop here")
    monkeypatch.setenv("TWIDDLE_ANCHOR", "192.168.1.2")
    monkeypatch.setattr(household, "discover", lambda: pytest.fail("SSDP should be skipped"))
    monkeypatch.setattr(topology, "fetch", fetch)
    with pytest.raises(Exception):
        Household.load()
    assert fetched == ["192.168.1.2"]
