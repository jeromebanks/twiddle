"""`spotify play --room` must leave the room actually listening to the relay.

It used to only check Spotify's device list: with the Roam drifted to a radio
station, a play went into a relay nobody was reading from -- silence that
reported success. Fakes are shared with the discover tests; nothing here
reaches the LAN, the relay, or the Spotify API.
"""
from twiddle import cli, spotify_cli, stations
from tests.test_discover_cli import RELAY_URL, FakeGroup, FakeResolution, FakeSession


def _play(monkeypatch, argv, group, relay_device=None):
    sess = FakeSession(tracks={"so what": [{"type": "track", "name": "So What",
                                            "uri": "spotify:track:sowhat",
                                            "artists": [{"name": "Miles Davis"}]}]})
    monkeypatch.setattr(spotify_cli, "_session", lambda args: (sess, None))
    monkeypatch.setattr(spotify_cli, "_target", lambda args: (FakeResolution(group), None))
    monkeypatch.setattr(spotify_cli, "_relay_device", relay_device or
                        (lambda args, s: ({"id": "dev1", "name": "relay"}, None)))
    monkeypatch.setattr(spotify_cli, "_relay_url", lambda g: RELAY_URL)
    args = cli.build_parser().parse_args(["spotify", "play", *argv])
    return args.func(args), sess


def test_play_repoints_a_room_that_drifted_to_radio(monkeypatch):
    group = FakeGroup(uri="x-rincon-mp3radio://kexp-mp3-128.streamguys1.com/kexp128.mp3")
    rc, sess = _play(monkeypatch, ["so what", "--room", "roam"], group)
    assert rc == 0
    assert group.play_radio_calls == [(RELAY_URL, "Spotify (relay)")]
    assert [c for c in sess.calls if c[0] == "play"]
    assert stations.last_source() == stations.SPOTIFY


def test_play_leaves_a_room_already_on_the_relay_alone(monkeypatch):
    group = FakeGroup(uri="x-rincon-mp3radio://" + RELAY_URL.removeprefix("http://"))
    rc, _ = _play(monkeypatch, ["so what", "--room", "roam"], group)
    assert rc == 0
    assert group.play_radio_calls == []


def test_play_dry_run_neither_repoints_nor_starts_a_relay(monkeypatch):
    def boom(args, s):
        raise AssertionError("_relay_device can start a relay; not under --dry-run")

    group = FakeGroup(uri="x-rincon-mp3radio://kexp-mp3-128.streamguys1.com/kexp128.mp3")
    rc, sess = _play(monkeypatch, ["so what", "--room", "roam", "--dry-run"], group, boom)
    assert rc == 0
    assert group.play_radio_calls == []
    assert not [c for c in sess.calls if c[0] == "play"]
    assert stations.last_source() is None


def test_play_without_room_checks_the_room_the_relay_feeds(monkeypatch):
    """`spotify play "Spyro Gyra"`, bare, 2026-09-25: the Roam was still on
    FIP from `dial`, the relay had no listeners, and Spotify played to nobody."""
    from twiddle import spotify_ops
    monkeypatch.setattr(spotify_ops, "relay_argv_value",
                        lambda flag: "roam" if flag == "--room" else None)
    seen = []
    monkeypatch.setattr(spotify_cli, "_target",
                        lambda args: seen.append(args.room) or (FakeResolution(group), None))
    group = FakeGroup(uri="x-rincon-mp3radio://icecast.radiofrance.fr/fipelectro-hifi.aac")
    sess = FakeSession(tracks={"so what": [{"type": "track", "name": "So What",
                                            "uri": "spotify:track:sowhat",
                                            "artists": [{"name": "Miles Davis"}]}]})
    monkeypatch.setattr(spotify_cli, "_session", lambda args: (sess, None))
    monkeypatch.setattr(spotify_cli, "_relay_device",
                        lambda args, s: ({"id": "dev1", "name": "relay"}, None))
    monkeypatch.setattr(spotify_cli, "_relay_url", lambda g: RELAY_URL)
    args = cli.build_parser().parse_args(["spotify", "play", "so what"])
    assert args.func(args) == 0
    assert seen == ["roam"]
    assert group.play_radio_calls == [(RELAY_URL, "Spotify (relay)")]


def test_play_to_a_named_other_device_does_not_touch_the_relay_room(monkeypatch):
    from twiddle import spotify_ops
    monkeypatch.setattr(spotify_ops, "relay_argv_value", lambda flag: "roam")
    group = FakeGroup(uri="x-rincon-mp3radio://icecast.radiofrance.fr/fipelectro-hifi.aac")
    rc, _ = _play(monkeypatch, ["so what", "--device", "Phone"], group)
    assert rc == 0 and group.play_radio_calls == []
