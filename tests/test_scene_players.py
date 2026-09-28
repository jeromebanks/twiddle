"""Playback from `scene`, and the relay experiment it must not quietly disturb.

A Spotify account plays on one device at a time, so a preview on the Mac
silences a live relay. That must be asked for twice and journalled, or it
reads as a real fault in `logs/relay.jsonl`.
"""
import json

import pytest

from twiddle import play, relay, spotify, spotify_ops
from twiddle.scene.local import LOCAL_LABEL, LocalSpeaker
from twiddle.scene.players import Device, NeedsConfirmation, SpotifyConnectPlayer

RELAY = "Sonos Roam (relay)"


class FakeSession:
    def __init__(self, active_device=RELAY, playing=True):
        self.calls = []
        self.state = {"is_playing": playing, "device": {"name": active_device, "id": "rid"},
                      "progress_ms": 61000,
                      "context": {"uri": "spotify:album:kindofblue"},
                      "item": {"name": "So What", "uri": "spotify:track:sowhat",
                               "artists": [{"name": "Miles Davis"}]}}

    def current(self):
        return self.state

    def devices(self):
        return [{"id": "mac", "name": "Sam's MacBook", "is_active": False},
                {"id": "rid", "name": RELAY, "is_active": True}]

    def play(self, uri="", device_id="", position_ms=0, *, uris=None, offset=0,
             offset_uri=""):
        if uri:
            self.calls.append(("play-context", device_id, uri, offset_uri, position_ms))
        else:
            self.calls.append(("play", device_id, uris, offset))

    def transfer(self, device_id, play=False):
        self.calls.append(("transfer", device_id, play))


class FakeGroup:
    name = "Roam"
    ip = "192.168.1.4"

    def __init__(self, uri):
        self.uri = uri
        self.repointed = []

    def now_playing(self):
        return {"uri": self.uri}

    def play_radio(self, url, title, art=None):
        self.repointed.append(url)


class FakeHouse:
    def __init__(self, group):
        self.group = group

    def resolve(self, _room):
        return type("R", (), {"group": self.group})()


MAC = Device(id="mac", name="Sam's MacBook")


def player(sess, group=None, **kw):
    group = group or FakeGroup("")
    kw.setdefault("relay_available", lambda: True)
    return SpotifyConnectPlayer(session_factory=lambda: sess, relay_name=RELAY,
                                household_factory=lambda: FakeHouse(group), **kw)


def journal():
    try:
        return [json.loads(line) for line in play.INTERVENTION_LOG.read_text().splitlines()]
    except FileNotFoundError:
        return []


@pytest.fixture(autouse=True)
def _no_real_relay(monkeypatch):
    monkeypatch.setattr(spotify_ops, "relay_argv_value", lambda flag: None)
    monkeypatch.setattr(spotify_ops, "relay_url", lambda g: "http://10.0.0.5:8765/stream.mp3")


def test_taking_spotify_off_a_live_relay_needs_confirmation():
    sess = FakeSession()
    with pytest.raises(NeedsConfirmation):
        player(sess).play(["spotify:track:a"], MAC)
    assert sess.calls == [] and journal() == []


def test_confirmed_it_plays_and_is_journalled_against_the_relay_room():
    sess = FakeSession()
    player(sess).play(["spotify:track:a", "spotify:track:b"], MAC, offset=1, confirmed=True)
    assert sess.calls == [("play", "mac", ["spotify:track:a", "spotify:track:b"], 1)]
    [rec] = journal()
    assert rec["action"] == "spotify_transfer_from_relay"
    assert rec["ip"] == "192.168.1.4" and rec["to_device"] == MAC.name


def test_no_confirmation_needed_when_the_relay_is_idle():
    sess = FakeSession(playing=False)
    player(sess).play(["spotify:track:a"], MAC)
    assert sess.calls and journal() == []


def test_playing_to_the_relay_does_not_repoint_a_room_already_on_it():
    sess = FakeSession()
    group = FakeGroup("x-rincon-mp3radio://10.0.0.5:8765/stream.mp3")
    relay = Device(id="rid", name=RELAY, relay=True)
    player(sess, group).play(["spotify:track:a"], relay)
    assert group.repointed == []
    assert sess.calls == [("play", "rid", ["spotify:track:a"], 0)]


def test_playing_to_the_relay_repoints_a_room_that_drifted_to_radio():
    sess = FakeSession()
    group = FakeGroup("x-rincon-mp3radio://stream.kalx.berkeley.edu:8000/kalx-128.mp3")
    player(sess, group).play(["spotify:track:a"], Device(id="rid", name=RELAY, relay=True))
    assert group.repointed == ["http://10.0.0.5:8765/stream.mp3"]


def test_dry_run_touches_nothing():
    sess = FakeSession(playing=False)
    msg = player(sess, dry_run=True).play(["spotify:track:a"], MAC, label="So What")
    assert "would play So What" in msg
    assert sess.calls == [] and journal() == []


def test_handing_back_with_nothing_saved_just_transfers():
    sess = FakeSession(active_device="Sam's MacBook")
    player(sess).back_to_relay()
    assert sess.calls == [("transfer", "rid", True)]
    assert journal()[0]["action"] == "spotify_transfer_to_relay"


def test_R_puts_back_what_the_roams_were_playing_not_the_preview():
    """Kind Of Blue, So What, 61s in -- not the preview that replaced it."""
    sess = FakeSession()
    p = player(sess)
    p.play(["spotify:track:preview"], MAC, confirmed=True)
    sess.state["device"] = {"name": MAC.name, "id": "mac"}
    sess.calls.clear()
    msg = p.back_to_relay()
    assert sess.calls == [("play-context", "rid", "spotify:album:kindofblue",
                           "spotify:track:sowhat", 61000)]
    assert "So What" in msg
    assert [r["action"] for r in journal()] == ["spotify_transfer_from_relay",
                                                "spotify_transfer_to_relay"]
    sess.calls.clear()
    p.back_to_relay()                   # used once; after that, a plain transfer
    assert sess.calls == [("transfer", "rid", True)]


def test_the_relay_name_comes_from_the_running_relay(monkeypatch):
    """A relay started with a custom --device-name must still count as the
    relay, or the "you'll silence the Roams" check never fires."""
    monkeypatch.setattr(spotify_ops, "relay_argv_value",
                        lambda flag: "Custom Relay" if flag == "--device-name" else None)
    sess = FakeSession(active_device="Custom Relay")
    p = SpotifyConnectPlayer(session_factory=lambda: sess,
                             household_factory=lambda: FakeHouse(FakeGroup("")))
    assert p.relay_is_live()
    with pytest.raises(NeedsConfirmation):
        p.play(["spotify:track:a"], MAC)


def test_the_relay_is_always_offered_even_when_not_running():
    class NoRelay(FakeSession):
        def devices(self):
            return [{"id": "mac", "name": "Sam's MacBook"}]

    devs = player(NoRelay()).devices()
    assert devs[0].relay and devs[0].id == "" and "will start" in devs[0].label


def test_api_errors_become_playback_errors_with_hints():
    class Down(FakeSession):
        def devices(self):
            raise spotify.ApiError(404, "no active device")

    with pytest.raises(spotify_ops.PlaybackError) as exc:
        player(Down()).devices()
    assert "no active device" in exc.value.hint


def test_the_403_race_is_success_when_playback_is_under_way():
    """Unchanged from spotify_cli: 403 after a transfer usually means it worked."""
    sess = FakeSession(active_device="Sam's MacBook")

    def boom():
        raise spotify.ApiError(403, "Restriction violated")

    np = spotify_ops.play_and_check(sess, {"name": "Sam's MacBook"}, boom)
    assert np["track"] == "So What"
    with pytest.raises(spotify.ApiError):
        spotify_ops.play_and_check(sess, {"name": "Somewhere else"}, boom)


def test_resuming_a_context_sends_the_track_to_start_from():
    sent = {}
    sess = spotify.Session.__new__(spotify.Session)
    sess.request = lambda method, path, **kw: sent.update(kw)
    sess.play("spotify:album:kob", device_id="rid", position_ms=61000,
              offset_uri="spotify:track:sowhat")
    assert sent["json"] == {"context_uri": "spotify:album:kob",
                            "offset": {"uri": "spotify:track:sowhat"},
                            "position_ms": 61000}


def test_a_podcast_on_the_phone_is_not_cut_off_without_asking():
    """Spotify on the Pixel, relay idle: playing to the Mac still stops it."""
    sess = FakeSession(active_device="Pixel 9a")
    with pytest.raises(NeedsConfirmation) as exc:
        player(sess).play(["spotify:track:a"], MAC)
    assert "Pixel 9a" in str(exc.value)
    assert sess.calls == []
    player(sess).play(["spotify:track:a"], MAC, confirmed=True)
    assert sess.calls and journal() == []       # not the relay: nothing to journal


def test_playing_on_the_device_already_playing_needs_no_confirmation():
    sess = FakeSession(active_device=MAC.name)
    player(sess).play(["spotify:track:a"], MAC)
    assert sess.calls


# -- this Mac's speakers, for anyone without the Roams -----------------------

LOCAL = "Friend's MacBook (scene)"


class FakeProc:
    def __init__(self):
        self.terminated = False
        self.exit = None

    def poll(self):
        return self.exit

    def terminate(self):
        self.terminated = True
        self.exit = 0

    def wait(self, timeout=None):
        return self.exit


class LocalSession(FakeSession):
    """Spotify, where the local librespot registers once it has been spawned."""

    def __init__(self, registered=False, **kw):
        super().__init__(**kw)
        self.registered = registered

    def devices(self):
        return super().devices() + ([{"id": "lid", "name": LOCAL}] if self.registered else [])


def speaker(tmp_path, sess, relay_creds=True):
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir()
    if relay_creds:
        (relay_dir / "credentials.json").write_text("{}")
    spawned = []

    def spawn(argv, log):
        spawned.append(argv)
        sess.registered = True
        return FakeProc()
    return LocalSpeaker(name=LOCAL, cache_dir=tmp_path / "local", relay_cache=relay_dir,
                        spawn=spawn, sleep=lambda s: None), spawned


def test_the_local_speaker_is_offered_before_it_runs(tmp_path):
    sess = LocalSession()
    local, spawned = speaker(tmp_path, sess)
    devs = player(sess, local=local).devices()
    [mine] = [d for d in devs if d.local]
    assert mine.id == "" and mine.label == LOCAL_LABEL + " -- will start"
    assert spawned == []                 # listing devices starts nothing


def test_playing_locally_starts_librespot_once_and_plays_on_it(tmp_path):
    sess = LocalSession(playing=False)
    local, spawned = speaker(tmp_path, sess)
    p = player(sess, local=local)
    dev = next(d for d in p.devices() if d.local)
    p.play(["spotify:track:a"], dev)
    p.play(["spotify:track:b"], dev)
    assert len(spawned) == 1 and "rodio" in spawned[0] and LOCAL in spawned[0]
    assert [c[1] for c in sess.calls] == ["lid", "lid"]
    # The relay's sign-in is reused, so nobody needs no second login.
    assert (tmp_path / "local" / "credentials.json").exists()


def test_a_local_speaker_already_registered_is_reused_not_respawned(tmp_path):
    sess = LocalSession(registered=True, playing=False)
    local, spawned = speaker(tmp_path, sess)
    p = player(sess, local=local)
    dev = next(d for d in p.devices() if d.local)
    assert dev.id == "lid"
    p.play(["spotify:track:a"], dev)
    p.close()
    assert spawned == [] and sess.calls[0][1] == "lid"


def test_quitting_stops_only_the_librespot_this_app_started(tmp_path):
    sess = LocalSession(playing=False)
    local, _ = speaker(tmp_path, sess)
    p = player(sess, local=local)
    p.play(["spotify:track:a"], next(d for d in p.devices() if d.local))
    proc = local._proc
    p.close()
    assert proc.terminated


def test_no_sign_in_anywhere_says_how_to_sign_in(tmp_path):
    sess = LocalSession(playing=False)
    local, spawned = speaker(tmp_path, sess, relay_creds=False)
    with pytest.raises(spotify_ops.PlaybackError) as exc:
        player(sess, local=local).play(["spotify:track:a"], Device("", LOCAL, local=True))
    assert "scene login" in exc.value.hint and spawned == []


def test_playing_locally_over_a_live_relay_still_asks_and_journals(tmp_path):
    sess = LocalSession()                        # the relay is playing
    local, _ = speaker(tmp_path, sess)
    p = player(sess, local=local)
    dev = Device("", LOCAL, local=True)
    with pytest.raises(NeedsConfirmation):
        p.play(["spotify:track:a"], dev)
    p.play(["spotify:track:a"], dev, confirmed=True)
    assert [r["action"] for r in journal()] == ["spotify_transfer_from_relay"]


def test_dry_run_does_not_start_a_local_player(tmp_path):
    sess = LocalSession(playing=False)
    local, spawned = speaker(tmp_path, sess)
    msg = player(sess, local=local, dry_run=True).play(["spotify:track:a"],
                                                       Device("", LOCAL, local=True))
    assert "would play" in msg and spawned == [] and sess.calls == []


def test_no_roams_are_offered_on_a_mac_that_never_ran_the_relay(tmp_path):
    class NoRelay(FakeSession):
        def devices(self):
            return [{"id": "pixel", "name": "Pixel 9a"}]

    local, _ = speaker(tmp_path, LocalSession())
    devs = player(NoRelay(), local=local, relay_available=lambda: False).devices()
    assert [d.label for d in devs] == [LOCAL_LABEL + " -- will start", "Pixel 9a"]


def test_the_local_argv_plays_to_coreaudio_and_stays_off_the_lan(monkeypatch):
    monkeypatch.setattr(relay, "librespot_bin", lambda: "librespot")
    argv = relay.local_argv("X (scene)", "/c")
    assert argv[argv.index("--backend") + 1] == "rodio"
    assert "--disable-discovery" in argv and "fixed" not in argv


def test_resume_on_the_relay_repoints_a_roam_a_bandcamp_track_took_away(monkeypatch):
    """space after a Bandcamp track on the Roam: Spotify must not resume into
    a relay the room has stopped reading."""
    monkeypatch.setattr(spotify_ops, "relay_url", lambda g: "http://10.0.0.5:8765/stream.mp3")
    sess = FakeSession(playing=False)
    sess.pause = lambda: None
    sess.next_track = lambda: sess.calls.append(("next",))
    group = FakeGroup("x-rincon-mp3radio://t4.bcbits.com/stream/abc")
    p = player(sess, group)
    p.resume()
    assert group.repointed == ["http://10.0.0.5:8765/stream.mp3"]
    group.uri = "x-rincon-mp3radio://10.0.0.5:8765/stream.mp3"
    p.next()
    assert group.repointed == ["http://10.0.0.5:8765/stream.mp3"]     # already there: no write


def test_resume_elsewhere_leaves_the_roam_alone(monkeypatch):
    monkeypatch.setattr(spotify_ops, "relay_url", lambda g: "http://10.0.0.5:8765/stream.mp3")
    sess = FakeSession(active_device="Sam's MacBook", playing=False)
    group = FakeGroup("x-rincon-mp3radio://t4.bcbits.com/stream/abc")
    player(sess, group).resume()
    assert group.repointed == []


def test_not_signed_in_to_spotify_still_offers_this_mac(tmp_path):
    # Bandcamp tracks play there with no Spotify at all (ffplay); without
    # this, the device picker failed and nothing could play.
    def signed_out():
        raise spotify_ops.PlaybackError("no Spotify tokens") from spotify.AuthError("x")

    local, spawned = speaker(tmp_path, LocalSession(), relay_creds=False)
    p = SpotifyConnectPlayer(session_factory=signed_out, local=local,
                             relay_available=lambda: False)
    assert [d.label for d in p.devices()] == [LOCAL_LABEL + " -- will start"]
    assert spawned == []


def test_other_spotify_failures_still_raise_from_devices(tmp_path):
    class Limited(FakeSession):
        def devices(self):
            raise spotify.ApiError(429, "API rate limit exceeded")

    local, _ = speaker(tmp_path, LocalSession())
    with pytest.raises(spotify_ops.PlaybackError):
        player(Limited(), local=local).devices()
