"""`dial`: the feed, the outputs, and the TUI driven by Textual's pilot.

No network, no speaker, no Spotify: stations, outputs and pictures are
fakes. The relay tests are the ones that matter most -- tuning a room off
the relay experiment must ask first, pause Spotify, and say so in the
journal.
"""
import asyncio
import json

import pytest

from twiddle import play, spotify_ops, stations
from twiddle.streaminfo import StreamInfo
from twiddle.dial import output as output_mod
from twiddle.dial import state
from twiddle.dial.app import DialApp, gauge
from twiddle.dial.enrich import ArtistCard, Enricher, query_of
from twiddle.dial.feed import SLOW_S, StationFeed
from twiddle.dial.output import (
    LocalOutput,
    OutputState,
    Outputs,
    SonosOutput,
    bare,
    station_for,
)
from twiddle.scene.players import NeedsConfirmation
from twiddle.stations import STATIONS, NowPlaying, Station


@pytest.fixture(autouse=True)
def _dial_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "CACHE_DIR", tmp_path / "dial")


def fake_station(key, songs):
    """A station whose now_playing walks through `songs` (Exception = fail)."""
    it = iter(songs)

    def fetch(station):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item
    return Station(key, key.upper(), f"http://example/{key}", "Town -- test radio", fetch)


# ---- feed ------------------------------------------------------------------------


def test_feed_emits_only_when_the_song_changes_and_remembers_what_played():
    a = NowPlaying("A", artist="Low", song="Words")
    b = NowPlaying("A", artist="Low", song="Lullaby")
    s = fake_station("a", [a, NowPlaying("A", artist="Low", song="Words"), b])
    seen = []
    feed = StationFeed([s], seen.append, clock=lambda: 1000.0)
    assert feed.poll("a") is True
    assert feed.poll("a") is False           # same song: no redraw
    assert feed.poll("a") is True
    assert seen == ["a", "a"]
    st = feed.states["a"]
    assert st.np.song == "Lullaby"
    assert [r["song"] for r in st.recent()] == ["Words"]
    feed.close()


def test_feed_prefers_the_stations_own_history():
    np = NowPlaying("K", artist="X", song="Y", recent=[{"artist": "P", "song": "Q"}])
    feed = StationFeed([fake_station("k", [np])], lambda k: None)
    feed.poll("k")
    assert feed.states["k"].recent() == [{"artist": "P", "song": "Q"}]
    feed.close()


def _song(n, show=None):
    return NowPlaying("W", artist=f"Band {n}", song=f"Song {n}", show=show)


def test_feed_keeps_a_shows_history_through_a_fetch_that_names_no_show():
    # Show A, then an ICY fallback naming no show, then Show A again.
    s = fake_station("w", [_song(1, "Show A"), _song(2, "Show A"), _song(3),
                           _song(4, "Show A")])
    feed = StationFeed([s], lambda k: None, clock=lambda: 1000.0)
    for _ in range(4):
        feed.poll("w")
    assert [r["song"] for r in feed.states["w"].recent()] == ["Song 3", "Song 2", "Song 1"]
    feed.close()


def test_feed_a_fallback_naming_the_same_song_is_not_a_song_change():
    # WFMU: live page (no raw title), one ICY fallback (raw title), live again.
    live = NowPlaying("W", artist="Band 1", song="Song 1", show="Show A")
    icy = NowPlaying("W", artist="Band 1", song="Song 1", show="Show A",
                     raw_title='"Song 1" by Band 1 on Show A on WFMU')
    s = fake_station("w", [live, icy, live])
    feed = StationFeed([s], lambda k: None, clock=lambda: 1000.0)
    for _ in range(3):
        feed.poll("w")
    assert feed.states["w"].recent() == []
    feed.close()


def test_feed_starts_its_history_over_when_a_new_show_is_named():
    # Show A, a fallback naming no show, then Show B: nothing of Show A stays,
    # not even the song that was on when the fallback came.
    s = fake_station("w", [_song(1, "Show A"), _song(2, "Show A"), _song(3),
                           _song(4, "Show B")])
    feed = StationFeed([s], lambda k: None, clock=lambda: 1000.0)
    for _ in range(4):
        feed.poll("w")
    st = feed.states["w"]
    assert st.np.song == "Song 4" and st.recent() == []
    feed.close()


def test_feed_failure_reports_once_and_backs_off_keeping_the_last_song():
    s = fake_station("a", [NowPlaying("A", artist="Low", song="Words"),
                           OSError("down"), OSError("down")])
    seen = []
    feed = StationFeed([s], seen.append, clock=lambda: 1000.0)
    feed.poll("a")
    feed.poll("a")
    feed.poll("a")
    st = feed.states["a"]
    assert seen == ["a", "a"]                   # the first failure only
    assert st.np.song == "Words" and "down" in st.error
    assert st.next_at == 1000.0 + 60
    feed.close()


def test_feed_polls_only_the_active_stations_and_the_hot_ones():
    now = [1000.0]
    polled = []

    def station(key):
        def fetch(s):
            polled.append(s.key)
            return NowPlaying(s.key)
        return Station(key, key.upper(), f"http://example/{key}", "Town -- test", fetch)
    feed = StationFeed([station(k) for k in "abc"], lambda k: None, clock=lambda: now[0])
    feed._pool.submit = lambda fn, key: fn(key)          # run polls inline
    feed.set_active(["a"])
    feed.set_hot(["c"])
    feed.tick()
    assert sorted(polled) == ["a", "c"]                  # b is filtered out and not hot
    feed.set_active(None)
    now[0] += SLOW_S
    feed.tick()
    assert "b" in polled                                 # widening polls it at once
    feed.close()


def test_hot_stations_are_pulled_forward():
    s = fake_station("a", [NowPlaying("A", artist="x", song="y")])
    feed = StationFeed([s], lambda k: None, clock=lambda: 1000.0)
    feed.poll("a")
    assert feed.states["a"].next_at == 1000.0 + SLOW_S
    feed.set_hot(["a", None])
    assert feed.states["a"].next_at == 1000.0 + 20
    feed.close()


# ---- enrich -----------------------------------------------------------------------


def test_enricher_carries_musicbrainz_ids_and_finds_the_photo_and_cover():
    from twiddle import lookup
    calls = []

    def identify(artist, album, song, **ids):
        calls.append((artist, album, song, ids))
        return lookup.Result(artist=lookup.ArtistInfo(
            name="Low", links={"wikipedia": "https://en.wikipedia.org/wiki/Low_(band)"},
            album=lookup.AlbumInfo(title="Things", mbid="rg-1")))
    en = Enricher(lambda c: None, identify=identify, photo=lambda info, heard: f"photo:{info.links['wikipedia']}")
    np = NowPlaying("KEXP", artist="Low", song="Words", album="Things",
                    mb_artist_id="a-1", mb_release_group_id="rg-1")
    assert en.get(np) is None or True        # starts the lane; answer below is synchronous
    card = en.run_one(("Low", "Things", "Words"), np)
    assert calls[-1][3] == {"mb_artist_id": "a-1", "mb_release_group_id": "rg-1"}
    assert card.photo_url == "photo:https://en.wikipedia.org/wiki/Low_(band)"
    assert card.cover_url.endswith("/release-group/rg-1/front-500")
    en.close()



def test_enricher_identifies_and_pictures_the_artist_without_a_with_credit():
    from twiddle import lookup
    calls, heard = [], []

    def identify(artist, album, song, **ids):
        calls.append(artist)
        return lookup.Result(artist=lookup.ArtistInfo(name="Butch Paulson"))
    en = Enricher(lambda c: None, identify=identify,
                  photo=lambda info, name: heard.append(name) or None)
    np = NowPlaying("WFMU", artist='Butch Paulson with "The Motations"', song="Man From Mars")
    q = query_of(np)
    assert q == ("Butch Paulson", None, "Man From Mars")
    en.run_one(q, np)
    assert calls == ["Butch Paulson"] and heard == ["Butch Paulson"]
    en.close()

# ---- outputs ----------------------------------------------------------------------


def test_sonos_and_our_urls_compare_without_their_schemes():
    kalx = STATIONS["kalx"]
    assert station_for("x-rincon-mp3radio://" + bare(kalx.url)) is kalx
    assert station_for("x-sonos-spotify:whatever") is None


class FakeGroup:
    name = "Roam"
    ip = "10.0.0.4"

    def __init__(self):
        self.radio = []
        self.files = []
        self.volumes = []
        self.mutes = []
        self.stopped = 0

    def play_radio(self, url, title, art=None):
        self.radio.append((url, title, art))

    def play_url(self, url, metadata=""):
        self.files.append((url, metadata))

    def set_volume(self, v):
        self.volumes.append(v)

    def set_mute(self, m):
        self.mutes.append(m)

    def stop(self):
        self.stopped += 1


class FakeSpotify:
    def __init__(self, live=True):
        self.live = live
        self.paused = 0

    def now(self):
        return {"playing": self.live, "device": "relay", "uri": "spotify:track:1",
                "track": "So What"}

    def relay_is_live(self, np):
        return self.live

    def pause(self):
        self.paused += 1


RELAY_URL = "http://10.0.0.9:8090/stream.mp3"


def sonos(monkeypatch, current_uri, live=True, dry_run=False):
    g = FakeGroup()
    sp = FakeSpotify(live)
    monkeypatch.setattr(play, "transport_info",
                        lambda ip: {"CurrentTransportState": "PLAYING"})
    monkeypatch.setattr(play, "media_info", lambda ip: {"CurrentURI": current_uri})
    monkeypatch.setattr(play, "get_volume", lambda ip: 35)
    monkeypatch.setattr(play, "get_mute", lambda ip: False)
    monkeypatch.setattr(play, "get_sleep_timer", lambda ip: None)
    out = SonosOutput("roam", lambda: g, dry_run=dry_run, spotify_player=sp,
                      relay_url=lambda group: RELAY_URL)
    return out, g, sp


def journal():
    p = play.INTERVENTION_LOG
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_sonos_state_names_the_station_or_the_relay(monkeypatch):
    out, _, _ = sonos(monkeypatch, "x-rincon-mp3radio://" + bare(STATIONS["kexp"].url))
    assert out.state() == OutputState(tuned="kexp", playing=True, volume=35, muted=False,
                                      uri=bare(STATIONS["kexp"].url))
    out, _, _ = sonos(monkeypatch, "x-rincon-mp3radio://10.0.0.9:8090/stream.mp3")
    assert out.state().tuned == output_mod.RELAY


def test_tuning_off_the_relay_asks_then_pauses_spotify_and_journals_it(monkeypatch):
    out, g, sp = sonos(monkeypatch, "x-rincon-mp3radio://10.0.0.9:8090/stream.mp3")
    with pytest.raises(NeedsConfirmation):
        out.tune(STATIONS["kalx"])
    assert g.radio == [] and sp.paused == 0 and journal() == []

    msg = out.tune(STATIONS["kalx"], confirmed=True)
    assert sp.paused == 1
    assert g.radio == [(STATIONS["kalx"].url, "KALX", STATIONS["kalx"].logo)]
    assert "Spotify paused" in msg
    acts = [r["action"] for r in journal()]
    assert "spotify_paused_for_radio" in acts
    # Not a span: radio on the Roam is organic evidence, not this Mac's doing.
    assert not any(r.get("span") for r in journal())
    assert state.get_state("left_relay") == {"room": "Roam", "station": "kalx"}


def test_a_silent_relay_is_tuned_over_at_once(monkeypatch):
    # The case that bit: the Roam on the relay, Spotify playing nothing, so the
    # Roam says PLAYING while emitting silence. Asking first only got in the way.
    out, g, sp = sonos(monkeypatch, "x-rincon-mp3radio://10.0.0.9:8090/stream.mp3",
                       live=False)
    out.tune(STATIONS["kalx"])
    assert len(g.radio) == 1 and sp.paused == 0
    assert "spotify_paused_for_radio" not in [r["action"] for r in journal()]


def test_tuning_from_a_station_never_asks_or_touches_spotify(monkeypatch):
    out, g, sp = sonos(monkeypatch, "x-rincon-mp3radio://" + bare(STATIONS["kexp"].url))
    out.tune(STATIONS["kalx"])
    assert sp.paused == 0 and len(g.radio) == 1


def test_dry_run_tunes_nothing(monkeypatch):
    out, g, sp = sonos(monkeypatch, "x-rincon-mp3radio://10.0.0.9:8090/stream.mp3",
                       dry_run=True)
    msg = out.tune(STATIONS["kalx"], confirmed=True)
    assert msg.startswith("[dry-run]") and g.radio == [] and sp.paused == 0
    assert out.set_volume(140) == 100 and g.volumes == []


def test_disconnect_stops_then_clears_the_transport_uri(monkeypatch):
    out, g, _ = sonos(monkeypatch, "x-rincon-mp3radio://" + bare(STATIONS["kexp"].url))
    cleared = []
    monkeypatch.setattr(output_mod.play, "set_uri", lambda ip, uri, md="": cleared.append((ip, uri)))
    out.disconnect()
    assert g.stopped == 1 and cleared == [(g.ip, "")]


def test_disconnect_survives_a_speaker_that_refuses_the_clear(monkeypatch):
    out, g, _ = sonos(monkeypatch, "x-rincon-mp3radio://" + bare(STATIONS["kexp"].url))
    def refuse(*a, **k):
        raise RuntimeError("714")
    monkeypatch.setattr(output_mod.play, "set_uri", refuse)
    out.disconnect()
    assert g.stopped == 1


def test_a_handoff_stops_a_room_but_never_clears_it():
    roam, mac = Spot("room:roam", "Roam"), Spot("mac", "This Mac")
    roam.disconnect = lambda: pytest.fail("a handoff must only stop")
    outs = SpotOutputs(roam, mac)
    outs.play("room:roam", KEXP)
    outs.play("mac", KALX)
    assert roam.stops == 1


def test_D_disconnects_through_outputs_and_forgets_it():
    roam = Spot("room:roam", "Roam")
    roam.disconnect = lambda: "disconnected"
    outs = SpotOutputs(roam)
    outs.play("room:roam", KEXP)
    assert outs.disconnect("room:roam") == "disconnected" and outs.owned() == []


def test_disconnect_never_silences_the_relay(monkeypatch):
    out, g, _ = sonos(monkeypatch, "x-rincon-mp3radio://10.0.0.9:8090/stream.mp3")
    with pytest.raises(spotify_ops.PlaybackError):
        out.disconnect()
    assert g.stopped == 0


def test_stop_refuses_to_silence_the_relay(monkeypatch):
    out, g, _ = sonos(monkeypatch, "x-rincon-mp3radio://10.0.0.9:8090/stream.mp3")
    with pytest.raises(spotify_ops.PlaybackError):
        out.stop()
    assert g.stopped == 0


class PipeProc:
    """A process with a stdin to listen on, as `_spawn` now gives."""
    def __init__(self):
        self.written = []
        self.stdin = self
        self.done = None

    def write(self, data):
        self.written.append(data.decode())

    def flush(self):
        pass

    def poll(self):
        return self.done

    def terminate(self):
        self.done = 0

    def wait(self, timeout=None):
        return 0


def until(cond, timeout=2.0):
    """The gain sender is a thread: wait for what it should have written."""
    import time as _t
    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        if cond():
            return True
        _t.sleep(0.005)
    return cond()


def test_local_output_plays_with_ffmpeg_and_keeps_its_own_volume():
    """Issue #1: volume and mute are dial's own, never the Mac's. Nothing
    here may reach osascript, and the change reaches the running process
    without a restart."""
    spawned, procs = [], []

    def spawn(argv, log):
        spawned.append(argv)
        procs.append(PipeProc())
        return procs[-1]
    out = LocalOutput(spawn=spawn, sink="audiotoolbox", ffmpeg="/bin/ffmpeg")
    out.gain_pace_s = 0.01
    out.tune(STATIONS["kexp"])
    argv = spawned[0]
    assert argv[0] == "/bin/ffmpeg" and STATIONS["kexp"].url in argv
    assert "-nostdin" not in argv
    assert argv[argv.index("-af") + 1] == "volume@v=1.0000"        # unity, as before
    assert out.state() == OutputState(tuned="kexp", playing=True, volume=100, muted=False,
                                      uri=bare(STATIONS["kexp"].url))

    assert out.set_volume(50) == 50
    assert until(lambda: procs[0].written == ["cvolume@v -1 volume 0.2500\n"])    # squared, live
    out.set_mute(True)
    assert until(lambda: procs[0].written[-1] == "cvolume@v -1 volume 0.0000\n")
    assert out.state().volume == 50 and out.state().muted      # unmute keeps the level
    out.set_mute(False)
    assert until(lambda: procs[0].written[-1] == "cvolume@v -1 volume 0.2500\n")

    out.set_volume(140)                                          # never a boost
    assert out.state().volume == 100
    assert until(lambda: procs[0].written[-1] == "cvolume@v -1 volume 1.0000\n")

    out.set_volume(30)
    out.tune(STATIONS["kalx"])                                   # the next station keeps it
    assert spawned[1][spawned[1].index("-af") + 1] == "volume@v=0.0900"
    out.close()


def test_volume_before_anything_plays_is_kept_for_the_first_station():
    spawned = []
    out = LocalOutput(spawn=lambda argv, log: spawned.append(argv) or PipeProc(),
                      sink="audiotoolbox", ffmpeg="/bin/ffmpeg")
    out.set_volume(20)
    out.set_mute(True)
    out.tune(STATIONS["kexp"])
    assert spawned[0][spawned[0].index("-af") + 1] == "volume@v=0.0000"
    out.close()


def test_a_process_that_just_died_does_not_break_the_volume_key():
    proc = PipeProc()

    def broken(data):
        raise BrokenPipeError
    proc.write = broken
    out = LocalOutput(spawn=lambda argv, log: proc, sink="audiotoolbox", ffmpeg="/bin/ffmpeg")
    out.gain_pace_s = 0.01
    out.tune(STATIONS["kexp"])
    assert out.set_volume(10) == 10 and out.state().volume == 10
    out.close()


def test_dry_run_volume_writes_nothing():
    proc = PipeProc()
    out = LocalOutput(dry_run=True, spawn=lambda argv, log: proc, sink="audiotoolbox",
                      ffmpeg="/bin/ffmpeg")
    out.set_volume(10)
    assert proc.written == []


def test_this_computer_on_linux_uses_pactl_for_volume_and_mute():
    """No osascript off macOS (a Chromebook's Linux container): the default
    PulseAudio/PipeWire sink is the system volume instead."""
    calls = []

    def pactl(*args):
        calls.append(args)
        return {"get-sink-volume": "Volume: front-left: 45875 /  70% / -9.29 dB,   "
                                   "front-right: 45875 /  70% / -9.29 dB",
                "get-sink-mute": "Mute: yes"}.get(args[0], "")
    out = LocalOutput(sink="ffplay", pactl=pactl, ffplay="/bin/ffplay", spawn=lambda *a: None)
    assert out._volume() == (70, True)
    out.set_volume(55)
    out.set_mute(False)
    assert ("set-sink-volume", "@DEFAULT_SINK@", "55%") in calls
    assert ("set-sink-mute", "@DEFAULT_SINK@", "0") in calls


def test_play_url_on_the_roam_is_relay_safe_but_not_remembered_as_a_station(monkeypatch):
    """A Bandcamp track from `scene`: the same ask-first as tuning, but not
    written as the last station or dial's `left_relay` -- both are read
    back as station keys."""
    out, g, sp = sonos(monkeypatch, "x-rincon-mp3radio://10.0.0.9:8090/stream.mp3")
    remembered = []
    monkeypatch.setattr(stations, "remember", remembered.append)
    with pytest.raises(NeedsConfirmation, match="Press p again"):
        out.play_url("https://t4.bcbits.com/stream/x", "Forged in Steel – Sleepbomb")
    assert g.radio == [] and g.files == []
    out.play_url("https://t4.bcbits.com/stream/x", "Forged in Steel – Sleepbomb",
                 confirmed=True)
    # as a file at its https URL, not the radio scheme (which the speaker
    # fetches over plain http, and bcbits only redirects that)
    assert g.radio == [] and g.files[0][0] == "https://t4.bcbits.com/stream/x"
    assert "musicTrack" in g.files[0][1] and sp.paused == 1
    # the type stated outright: guessed from a Bandcamp path, it's error 714
    assert 'protocolInfo="http-get:*:audio/mpeg:*">https://t4.bcbits.com/stream/x<' \
        in g.files[0][1]
    assert remembered == [] and state.get_state("left_relay") is None
    assert journal()[-1]["source"] == "scene"


def test_local_play_url_exits_when_the_track_ends():
    spawned = []

    class Proc:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0
    out = LocalOutput(spawn=lambda argv, log: spawned.append(argv) or Proc(),
                      sink="ffplay", pactl=lambda *a: "", ffplay="/bin/ffplay")     # no PulseAudio in ffmpeg
    out.play_url("https://t4.bcbits.com/stream/x", "a track")
    assert "-autoexit" in spawned[0]
    out.tune(STATIONS["kexp"])
    assert "-autoexit" not in spawned[1]        # a station never "ends"


def test_gauge_shows_mute():
    assert "muted" in gauge(30, True).plain
    assert "?" in gauge(None, False).plain


# ---- the app ------------------------------------------------------------------------


class FakeOutput:
    id = "room:roam"
    label = "Roam"

    def __init__(self, tuned="relay", confirm_first=False):
        self.st = OutputState(tuned=tuned, playing=True, volume=30, muted=False)
        self.confirm_first = confirm_first
        self.tuned_to = []
        self.volumes = []
        self.mutes = []

    def state(self):
        return self.st

    def play(self, media, *, confirmed=False, source="dial"):
        if self.confirm_first and not confirmed:
            raise NeedsConfirmation("on the relay")
        self.tuned_to.append((media.station, confirmed))
        self.st = OutputState(tuned=media.station, playing=True, volume=self.st.volume,
                              uri=media.url)
        return f"▶ {media.name}"

    def set_volume(self, v):
        self.volumes.append(v)
        return v

    def set_mute(self, m):
        self.mutes.append(m)

    def stop(self):
        return "stopped"

    def back_to_relay(self):
        return "back"

    def close(self):
        pass


class FakeOutputs(Outputs):
    def __init__(self, out):
        super().__init__()
        self.out = out

    def get(self, oid):
        return self.out

    def choices(self):
        return [("room:roam", "Roam"), ("mac", "This Mac")]


SONGS = {
    "kexp": NowPlaying("KEXP", artist="Stereolab", song="Pink Floyd Called",
                       album="Instant Holograms", show="The Afternoon Show",
                       hosts=["Kevin Cole"], recent=[{"time": "21:38", "artist": "Broadcast",
                                                      "song": "Come On Let's Go"}]),
    "kalx": NowPlaying("KALX", artist="Low", song="Words", show="Freeform"),
    "kqed": NowPlaying("KQED", raw_title="Gaza Policy", show="Forum",
                       note="(talk/news: this is the segment)",
                       schedule=[{"time": "05:00", "show": "Morning Edition"},
                                 {"time": "10:00", "show": "Forum", "episode": "Gaza Policy",
                                  "on_now": True},
                                 {"time": "11:00", "show": "Science Friday"}]),
}


FORMATS = {STATIONS["kalx"].url: StreamInfo("mp3", 128, 48000, 2),
           STATIONS["kqed"].url: StreamInfo("mp3", 32, 22050, 1)}


def stations_for_app():
    out = []
    for key in ("kexp", "kalx", "kqed"):
        real = STATIONS[key]
        out.append(Station(real.key, real.name, real.url, real.blurb,
                           lambda s, k=key: SONGS[k], logo=None, tags=real.tags))
    return out


def make_app(out, enricher_factory=None, **kw):
    def feed_factory(stations, on_change):
        feed = StationFeed(stations, on_change)
        for s in stations:
            feed.poll(s.key)
        return feed

    class NoLookup(Enricher):
        def get(self, np):
            return None
    return DialApp(outputs=FakeOutputs(out), output_id="room:roam",
                   stations=stations_for_app(), feed_factory=feed_factory,
                   enricher_factory=enricher_factory or (lambda cb: NoLookup(cb)),
                   load_image=lambda url: None, stream_info=FORMATS.get, start_feed=False,
                   **{"splash": False, **kw})


def run(coro):
    return asyncio.run(coro)


def test_app_shows_every_station_and_previews_without_tuning():
    out = FakeOutput(tuned="kalx")

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            text = app.query_one("#hero-text").render().plain if hasattr(
                app.query_one("#hero-text").render(), "plain") else str(
                app.query_one("#hero-text").render())
            assert "Pink Floyd Called" in text
            await pilot.press("j")
            assert app.current == "kalx"
            await pilot.press("j")
            assert app.current == "kqed"
            assert out.tuned_to == []                      # browsing never tunes
            assert app.card("kalx").has_class("-tuned")
    run(go())


def test_enter_tunes_and_relay_needs_a_second_press():
    out = FakeOutput(tuned="relay", confirm_first=True)

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert out.tuned_to == []
            assert "⚠" in app._status_msg[0]
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert out.tuned_to == [("kexp", True)]
    run(go())


def test_a_burst_of_volume_keys_is_a_leading_write_and_a_trailing_one():
    out = FakeOutput(tuned="kexp")

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            for _ in range(5):
                await pilot.press("plus")
            assert app.volume == 40
            await asyncio.sleep(0.6)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert out.volumes[0] == 32 and out.volumes[-1] == 40   # not one per press
            assert len(out.volumes) == 2
            await pilot.press("m")
            await app.workers.wait_for_complete()
            assert out.mutes == [True] and app.muted
    run(go())


def test_clicking_the_gauge_sets_volume_and_the_icon_mutes():
    out = FakeOutput(tuned="kexp")

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            line = app.query_one("#nowbar").render().plain.split("\n")[0]
            icon = line.index("♪") + 1                  # +1: the bar's padding
            bar = icon + len("♪ ") + len(" 30 ")
            await pilot.click("#nowbar", offset=(bar + 16, 0))  # 16/20 of the way
            assert app.volume == 80
            await pilot.click("#nowbar", offset=(bar, 0))       # the far left
            assert app.volume == 0
            await asyncio.sleep(0.6)
            await app.workers.wait_for_complete()
            assert out.volumes[-1] == 0 and len(out.volumes) == 2   # first click, then the last
            await pilot.click("#nowbar", offset=(icon, 0))
            await app.workers.wait_for_complete()
            assert out.mutes == [True] and app.muted
    run(go())


def test_number_keys_jump_and_the_choice_is_remembered():
    async def go():
        app = make_app(FakeOutput(tuned=None))
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.press("3")
            assert app.current == "kqed"
    run(go())
    assert state.get_state("station") == "kqed"


def test_narrow_terminal_stacks():
    async def go():
        app = make_app(FakeOutput())
        async with app.run_test(size=(90, 40)) as pilot:
            await pilot.pause()
            assert app.screen.has_class("-narrow")
    run(go())


def test_one_click_previews_and_a_double_click_tunes():
    out = FakeOutput(tuned=None)

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#card-kalx")
            await app.workers.wait_for_complete()
            assert app.current == "kalx" and out.tuned_to == []
            await pilot.click("#card-kalx", times=2)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert out.tuned_to == [("kalx", False)]
    run(go())


def test_the_tuned_meter_survives_a_long_name_and_city():
    out = FakeOutput(tuned="kqed")

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            card = app.card("kqed")
            card.st.station = Station("kqed", "Beat Blender", "http://x",
                                      "SomaFM, San Francisco -- deep house")
            card.render_text(frame=3)
            first = str(card.query_one(".text").render()).splitlines()[0]
            assert first.index("Beat Blender") < min(first.index(c) for c in "▁▂▃▄▅▆▇"
                                                    if c in first)
            assert first.index("▁" if "▁" in first else next(
                c for c in "▂▃▄▅▆▇" if c in first)) < first.index("SomaFM")
    run(go())


def test_a_program_station_shows_its_schedule_not_just_played():
    async def go():
        app = make_app(FakeOutput(tuned="kalx"))
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "j")
            await pilot.pause()
            box = app.query_one("#recent-scroll")
            text = str(app.query_one("#recent").render())
            assert box.border_title == "Today on KQED"
            assert "▶ Forum — Gaza Policy" in text
            assert "Science Friday" in text and "Morning Edition" in text
            await pilot.press("k", "k")                  # a playlist station is unchanged
            await pilot.pause()
            assert box.border_title == "Just played on KEXP"
    run(go())


def test_just_played_shows_a_whole_published_show_from_its_first_song(monkeypatch):
    rows = [{"artist": f"Band {n}", "song": f"Song {n}", "album": f"Album {n}",
             "label": "Label", "year": "1981", "format": "LP", "comment": "a request",
             "playlist_url": "https://wfmu.org/playlists/shows/1", "song_id": str(n)}
            for n in range(20, 0, -1)]                   # newest first, Song 1 the show's first
    monkeypatch.setitem(SONGS, "kexp", NowPlaying("KEXP", artist="Low", song="Words",
                                                  recent=rows))

    async def go():
        app = make_app(FakeOutput(tuned="kalx"))
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            text = str(app.query_one("#recent").render())
            assert "Band 20 — Song 20" in text and "Band 1 — Song 1" in text
            assert "Album 1" in text
    run(go())


def test_stream_format_shows_on_the_station_and_by_the_volume():
    async def go():
        app = make_app(FakeOutput(tuned="kqed"))
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "MP3 32k · 22.05kHz mono" in str(app.query_one("#nowbar").render())
            await pilot.press("j")                              # preview KALX
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#hero").border_subtitle.endswith("MP3 128k · 48kHz stereo")
            assert "MP3 32k" in str(app.query_one("#nowbar").render())   # still what's tuned
    run(go())


def _png_bytes():
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_a_failed_cover_is_tried_again_later(monkeypatch):
    from twiddle import termimage
    from twiddle.dial import art
    monkeypatch.setattr(art, "_failed", {})
    clock = [1000.0]
    monkeypatch.setattr(art.time, "monotonic", lambda: clock[0])
    replies = [TimeoutError("slow"), _png_bytes()]

    def fetch(url):
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(termimage, "fetch", fetch)
    url = "https://example.test/cover.jpg"
    assert art.load(url) is None                 # a timeout
    assert art.load(url) is None and replies     # not hammered straight away
    clock[0] += art.RETRY_FAILED_S + 1
    assert art.load(url) is not None             # but not given up on for good


def test_cover_cache_is_never_seen_half_written(monkeypatch):
    """Two loads of one cover at once: the reader must see all or nothing."""
    from twiddle import termimage
    from twiddle.dial import art
    monkeypatch.setattr(art, "_failed", {})
    monkeypatch.setattr(termimage, "fetch", lambda url: _png_bytes())
    url = "https://example.test/race.jpg"
    art.load(url)
    assert [p.name for p in art.art_dir().iterdir() if p.suffix == ".tmp"] == []
    assert len(list(art.art_dir().glob("*.png"))) == 1
    assert art.load(url) is not None             # served from the cache


def test_a_lookup_with_no_photo_still_draws_the_monogram():
    """The lookup lands after the station is drawn and finds no photo: the
    artist box must fill with initials, not stay empty until revisited."""
    class Later(Enricher):
        def get(self, np):
            return self.cards.get(query_of(np))

    async def go():
        app = make_app(FakeOutput(tuned=None), enricher_factory=lambda cb: Later(cb))
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not app.query_one("#photo").display     # lookup still running
            card = ArtistCard(query_of(SONGS["kexp"]))       # found nobody's photo
            app.enricher.cards[card.query] = card
            app._card_ready(card)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#photo").display
    run(go())


def test_bandcamp_photo_never_borrows_a_namesakes_face(monkeypatch):
    from twiddle import lookup
    from twiddle.dial import art
    monkeypatch.setattr(art, "_bandcamp", {})
    bands = {
        "Solo": [{"item_url_root": "https://solo.bandcamp.com",
                  "img": "https://f4.bcbits.com/img/0000000001_23.jpg"}],
        "Twins": [{"item_url_root": "https://twins-a.bandcamp.com",
                   "img": "https://f4.bcbits.com/img/0000000002_23.jpg"},
                  {"item_url_root": "https://twins-b.bandcamp.com",
                   "img": "https://f4.bcbits.com/img/0000000003_23.jpg"}],
        "Faceless": [{"item_url_root": "https://faceless.bandcamp.com", "img": None}],
    }
    monkeypatch.setattr(lookup, "bandcamp_bands", lambda name: bands.get(name, []))
    assert art.bandcamp_photo("Solo") == "https://f4.bcbits.com/img/0000000001_16.jpg"
    assert art.bandcamp_photo("Twins") is None                    # which one? don't guess
    assert art.bandcamp_photo("Twins", "https://twins-b.bandcamp.com/") \
        == "https://f4.bcbits.com/img/0000000003_16.jpg"          # the linked one
    assert art.bandcamp_photo("Faceless") is None
    assert art.bandcamp_photo("Nobody") is None


def test_artist_photo_prefers_wikipedia_then_bandcamp(monkeypatch):
    from twiddle import lookup
    from twiddle.dial import art
    monkeypatch.setattr(art, "wikipedia_image", lambda url: "wiki.jpg" if url else None)
    monkeypatch.setattr(art, "bandcamp_photo", lambda name, url=None: f"bc:{name}")
    famous = lookup.ArtistInfo(name="Low", links={"wikipedia": "https://en.wikipedia.org/wiki/Low"})
    assert art.artist_photo(famous) == "wiki.jpg"
    assert art.artist_photo(lookup.ArtistInfo(name="Laenz")) == "bc:Laenz"
    monkeypatch.setattr(art, "bandcamp_photo",
                        lambda name, url=None: "bc.jpg" if name == "Laenz" else None)
    assert art.artist_photo(lookup.ArtistInfo(name="Lænz"), "Laenz") == "bc.jpg"


# ---- one stream at a time: switching outputs ------------------------------------------


class Spot:
    """An output that remembers what it plays and can be changed behind our back."""

    def __init__(self, oid, label, tuned=None, confirm_first=False, unreachable=False):
        self.id, self.label = oid, label
        url = STATIONS[tuned].url if tuned in STATIONS else ""
        self.st = OutputState(tuned=tuned, playing=tuned is not None, uri=bare(url))
        self.confirm_first = confirm_first
        self.unreachable = unreachable
        self.played, self.stops = [], 0

    def state(self):
        if self.unreachable:
            raise spotify_ops.PlaybackError(f"{self.label} didn't answer")
        return self.st

    def play(self, media, *, confirmed=False, source="dial"):
        if self.confirm_first and not confirmed:
            raise NeedsConfirmation("on the relay")
        self.played.append(media.station or media.url)
        self.st = OutputState(tuned=media.station, playing=True, uri=bare(media.url))
        return f"▶ {media.name} on {self.label}"

    def stop(self):
        if self.st.tuned == output_mod.RELAY:
            raise spotify_ops.PlaybackError("on the relay")
        self.stops += 1
        self.st = OutputState(tuned=self.st.tuned, playing=False, uri=self.st.uri)
        return "stopped"

    def close(self):
        pass


class SpotOutputs(Outputs):
    def __init__(self, *spots, dry_run=False):
        super().__init__(dry_run=dry_run)
        self.spots = {s.id: s for s in spots}

    def get(self, oid):
        return self.spots[oid]

    def choices(self):
        return [(s.id, s.label) for s in self.spots.values()]


KEXP = output_mod.Media.of(STATIONS["kexp"])
KALX = output_mod.Media.of(STATIONS["kalx"])


def test_playing_on_a_second_output_stops_the_first():
    roam, mac = Spot("room:roam", "Roam"), Spot("mac", "This Mac")
    outs = SpotOutputs(roam, mac)
    outs.play("room:roam", KEXP)
    msg = outs.play("mac", KALX)
    assert roam.stops == 1 and mac.played == ["kalx"]
    assert "stopped Roam" in msg
    outs.play("mac", KEXP)                       # same output: nothing else to stop
    assert roam.stops == 1 and mac.stops == 0


def test_a_move_carries_the_station_and_stops_the_old_output():
    roam, mac = Spot("room:roam", "Roam", tuned="kexp"), Spot("mac", "This Mac")
    outs = SpotOutputs(roam, mac)
    media = outs.movable("room:roam")            # playing before dial ever touched it
    assert media == KEXP
    outs.play("mac", media)
    assert mac.played == ["kexp"] and roam.stops == 1


def test_nothing_moves_off_the_relay_or_an_idle_room():
    relay = Spot("room:roam", "Roam", tuned=output_mod.RELAY)
    idle = Spot("room:lr", "Living Room")
    outs = SpotOutputs(relay, idle, Spot("mac", "This Mac"))
    assert outs.movable("room:roam") is None
    assert outs.movable("room:lr") is None
    outs.play("mac", KEXP)
    assert relay.stops == 0 and idle.stops == 0


def test_an_output_someone_else_changed_is_not_ours_to_stop():
    roam, mac = Spot("room:roam", "Roam"), Spot("mac", "This Mac")
    outs = SpotOutputs(roam, mac)
    outs.play("room:roam", KEXP)
    roam.st = OutputState(tuned=output_mod.RELAY, playing=True, uri="10.0.0.9:8090/stream.mp3")
    outs.play("mac", KALX)
    assert roam.stops == 0
    outs.play("room:roam", KALX)                 # and it is forgotten, not retried
    assert mac.stops == 1 and roam.stops == 0


def test_a_refusal_keeps_the_old_output_playing_until_confirmed():
    mac = Spot("mac", "This Mac", tuned="kexp")
    roam = Spot("room:roam", "Roam", confirm_first=True)
    outs = SpotOutputs(mac, roam)
    media = outs.movable("mac")
    with pytest.raises(NeedsConfirmation):
        outs.play("room:roam", media)
    assert mac.stops == 0 and mac.st.playing        # not silence
    outs.play("room:roam", media, confirmed=True)
    assert mac.stops == 1 and roam.played == ["kexp"]


def test_an_unreachable_old_output_is_reported_not_forgotten():
    roam, mac = Spot("room:roam", "Roam"), Spot("mac", "This Mac")
    outs = SpotOutputs(roam, mac)
    outs.play("room:roam", KEXP)
    roam.unreachable = True
    assert "couldn't check Roam" in outs.play("mac", KALX)
    roam.unreachable = False
    outs.play("mac", KEXP)                       # the next play tries again
    assert roam.stops == 1


def test_a_bandcamp_track_matches_despite_its_token_coming_back_changed():
    assert output_mod.same_stream("https://t4.bcbits.com/stream/abc/mp3-128/1?p=0&ts=1&t=x",
                                  "t4.bcbits.com/stream/abc/mp3-128/1?p=0&amp;ts=1&amp;t=x")
    assert not output_mod.same_stream("", "")
    assert not output_mod.same_stream(STATIONS["kexp"].url, STATIONS["kalx"].url)


def test_dry_run_stops_nothing_and_says_what_it_would():
    roam, mac = Spot("room:roam", "Roam"), Spot("mac", "This Mac")
    outs = SpotOutputs(roam, mac, dry_run=True)
    outs._owned["room:roam"] = KEXP.url
    assert "would stop Roam" in outs.play("mac", KALX)
    assert roam.stops == 0


def test_a_registered_output_is_offered_and_made_on_demand():
    outs = Outputs(household_factory=lambda: type("H", (), {"groups": []})(),
                   bluetooth=lambda: [])
    made = []
    outs.register("sink:bt", "Headphones", lambda dry_run: made.append(dry_run) or Spot(
        "sink:bt", "Headphones"))
    assert ("sink:bt", "Headphones") in outs.choices()
    assert outs.get("sink:bt").label == "Headphones" and made == [False]


def _dial_on(outs, output_id="room:roam"):
    def feed_factory(stations, on_change):
        feed = StationFeed(stations, on_change)
        for s in stations:
            feed.poll(s.key)
        return feed

    class NoLookup(Enricher):
        def get(self, np):
            return None
    return DialApp(outputs=outs, output_id=output_id, stations=stations_for_app(),
                   feed_factory=feed_factory, enricher_factory=lambda cb: NoLookup(cb),
                   load_image=lambda url: None, stream_info=FORMATS.get, start_feed=False,
                   splash=False)


def test_choosing_another_output_moves_the_station_there():
    """The bug: `d` used to leave the old room playing, so streams piled up."""
    roam, mac = Spot("room:roam", "Roam", tuned="kalx"), Spot("mac", "This Mac")

    async def go():
        app = _dial_on(SpotOutputs(roam, mac))
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("down", "enter")          # This Mac
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.output is mac
            assert mac.played == ["kalx"] and roam.stops == 1
            assert app.current == "kalx"
    run(go())


def test_moving_onto_the_relay_asks_and_the_old_output_plays_until_enter():
    mac = Spot("mac", "This Mac", tuned="kexp")
    roam = Spot("room:roam", "Roam", confirm_first=True)

    async def go():
        app = _dial_on(SpotOutputs(mac, roam), output_id="mac")
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("down", "enter")          # Roam
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert roam.played == [] and mac.stops == 0 and "⚠" in app._status_msg[0]
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert roam.played == ["kexp"] and mac.stops == 1
    run(go())


def test_tuning_after_a_switch_from_the_relay_writes_nothing_to_the_relay():
    roam = Spot("room:roam", "Roam", tuned=output_mod.RELAY)
    mac = Spot("mac", "This Mac")

    async def go():
        app = _dial_on(SpotOutputs(roam, mac))
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("down", "enter")
            await app.workers.wait_for_complete()
            assert mac.played == [] and roam.stops == 0
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert mac.played == ["kexp"] and roam.stops == 0
    run(go())


def test_a_room_still_buffering_our_station_is_moved_and_stopped():
    """TRANSITIONING, not PLAYING, for seconds after a tune: still ours."""
    roam, mac = Spot("room:roam", "Roam"), Spot("mac", "This Mac")
    outs = SpotOutputs(roam, mac)
    outs.play("room:roam", KEXP)
    roam.st = OutputState(tuned="kexp", playing=False, uri=roam.st.uri)
    assert outs.movable("room:roam") == KEXP
    outs.play("mac", KEXP)
    assert roam.stops == 1


def test_s_stops_outright_and_forgets_the_output():
    roam, mac = Spot("room:roam", "Roam"), Spot("mac", "This Mac")
    outs = SpotOutputs(roam, mac)
    outs.play("room:roam", KEXP)
    outs.stop("room:roam")
    outs.play("mac", KALX)
    assert roam.stops == 1 and outs.owned() == ["mac"]


# ---- tags --------------------------------------------------------------------------


def test_a_tag_hides_the_other_cards_and_numbers_what_is_left():
    out = FakeOutput(tuned="kalx")

    async def go():
        app = make_app(out, tag="public")                # kexp and kqed; kalx is college
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            assert [s.key for s in app.listed] == ["kexp", "kqed"]
            assert not app.card("kalx").display and app.card("kqed").display
            assert app.card("kqed").index == 1
            assert "public (2)" in app.query_one("#stations").border_title
            await pilot.press("2")
            assert app.current == "kqed"
            await pilot.press("j")
            assert app.current == "kexp"                 # wraps within the tag, skips kalx
            # kalx is tuned but hidden: its card still exists and still updates
            app._station_updated("kalx")
            assert app.feed._active == {"kexp", "kqed"}
            assert state.get_state("tag") is None        # a --tag is not a choice to save
    run(go())


def test_choosing_a_tag_moves_the_selection_into_it_and_is_remembered():
    async def go():
        app = make_app(FakeOutput(), tag="all")
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            assert len(app.listed) == 3 and app.feed._active is None
            app.select("kexp")
            app.set_tag("college")
            assert app.current == "kalx" and app.card("kalx").index == 0
            assert state.get_state("tag") == "college"
            app.set_tag(None)
            assert app.current == "kalx" and len(app.listed) == 3
            assert all(app.card(k).display for k in ("kexp", "kalx", "kqed"))
    run(go())


def test_a_saved_tag_is_used_unless_it_no_longer_matches_anything():
    state.set_state("tag", "news")
    assert [s.key for s in make_app(FakeOutput()).listed] == ["kqed"]
    state.set_state("tag", "comedy")                     # none of these three
    assert len(make_app(FakeOutput()).listed) == 3
    state.set_state("tag", "no-such-tag")
    assert len(make_app(FakeOutput()).listed) == 3


def test_a_long_choice_list_scrolls_to_follow_j_on_a_short_terminal():
    # The list's height was auto, so it asked for every option and the dialog's
    # max-height clipped the bottom instead: the list never scrolled, and `j`
    # walked the highlight off screen. The tag picker hid "somafm" that way.
    from textual.widgets import OptionList

    from twiddle.scene.app import ChoiceScreen

    async def go():
        app = make_app(FakeOutput())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(ChoiceScreen("Pick", [(f"t{i}", f"tag {i}") for i in range(40)]))
            await pilot.pause()
            options = app.screen.query_one("#dialog-list", OptionList)
            dialog = app.screen.query_one("#dialog")
            inside = dialog.region.bottom - dialog.styles.gutter.bottom
            assert options.region.bottom <= inside
            for _ in range(39):
                await pilot.press("j")
            await pilot.pause()
            assert options.highlighted == 39
            assert options.scroll_y == options.max_scroll_y > 0
            short = options.region.height
            await pilot.resize_terminal(130, 40)
            await pilot.pause()
            await pilot.pause()
            assert options.region.height > short              # the room comes back
            assert options.region.bottom <= dialog.region.bottom - dialog.styles.gutter.bottom
    run(go())


def test_help_opens_and_its_markup_parses():
    # A bare "[" in HELP ("] / [  volume ±5") once swallowed the next "[b]",
    # and `?` crashed the app with a MarkupError.
    from textual.content import Content

    from twiddle.dial.app import HELP, DialHelp
    assert "] / [" in Content.from_markup(HELP).plain
    assert "D           disconnect" in Content.from_markup(HELP).plain

    async def go():
        app = make_app(FakeOutput())
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, DialHelp)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, DialHelp)
    run(go())


def test_v_visualizes_the_tuned_station_and_leaves_no_tap_behind():
    from twiddle.viz.screen import VizScreen
    from tests.test_viz_screen import RecordingTap, options

    out = FakeOutput(tuned="kalx")
    out.st.uri = STATIONS["kalx"].url
    _store, kw = options()

    async def run():
        app = make_app(out)
        app.viz_options = kw
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.press("v")
            await pilot.pause(0.3)
            assert isinstance(app.screen, VizScreen)
            assert RecordingTap.made[0].url == STATIONS["kalx"].url
            # Only the picture is showing: dial's own keys must not reach a
            # speaker unseen (the relay's ask-twice lives on the hidden screen).
            await pilot.press("enter", "enter", "s", "D", "R", "z", "Z", "d", "1")
            await pilot.pause(0.5)
            assert isinstance(app.screen, VizScreen)
            assert out.tuned_to == [] and out.volumes == [] and out.mutes == []
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, VizScreen)
            assert RecordingTap.made[0].stopped
    asyncio.run(run())


def test_volume_and_mute_work_inside_the_visualizer():
    """Issue #1, Jerome's observation: the modal screen swallowed them. Only
    those keys are forwarded; the overlay shows the level, since the bar is hidden."""
    from tests.test_viz_screen import options
    from twiddle.viz.screen import VizScreen

    out = FakeOutput(tuned="kalx")
    out.st.uri = STATIONS["kalx"].url
    _store, kw = options()

    async def run():
        app = make_app(out)
        app.viz_options = kw
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause(0.3)
            assert isinstance(app.screen, VizScreen)
            await pilot.press("minus")
            await pilot.pause(0.2)
            assert app.volume == 28 and out.volumes == [28]
            assert "volume 28" in "\n".join(app.screen._overlay_lines())
            await pilot.press("m")
            await pilot.pause(0.2)
            assert out.mutes == [True]
            assert "muted" in "\n".join(app.screen._overlay_lines())
            assert isinstance(app.screen, VizScreen)
    asyncio.run(run())


# ---- the splash --------------------------------------------------------------


def _splash_text(app) -> str:
    from twiddle.dial.splash import SplashCanvas
    return "\n".join(s.text for s in app.screen.query_one(SplashCanvas).strips)


def test_the_splash_shows_while_the_app_starts_underneath():
    from twiddle.dial.splash import SplashScreen
    out = FakeOutput(tuned="kalx")

    async def go():
        app = make_app(out, splash=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            assert isinstance(app.screen, SplashScreen)
            text = _splash_text(app)
            assert "none of the static" in text and "press any key" in text
            assert "stations tuned in" in text          # the fake feed has answered
            assert app.screen.frames > 2
            assert app.out_ready                        # poll_output ran behind it
    run(go())


def test_the_key_that_closes_the_splash_reaches_nothing_behind_it():
    """enter tunes, + writes volume, q quits: none of that from a hello."""
    from twiddle.dial.splash import SplashScreen
    out = FakeOutput(tuned="kalx")

    async def go():
        app = make_app(out)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            for key in ("enter", "plus", "q"):
                app.push_screen(SplashScreen())
                await pilot.pause(0.05)
                await pilot.press(key)
                await pilot.pause(0.1)
                assert not isinstance(app.screen, SplashScreen), key
            await pilot.pause(0.5)         # past the volume settle: a write would have landed
            assert out.tuned_to == [] and out.volumes == []
            assert app.is_running
    run(go())


def test_the_splash_goes_by_itself_and_leaves_the_app_ready(monkeypatch):
    from twiddle.dial import splash
    monkeypatch.setattr(splash, "TIMEOUT_S", 0.3)
    out = FakeOutput(tuned="kalx")

    async def go():
        app = make_app(out, splash=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.05)
            assert isinstance(app.screen, splash.SplashScreen)
            await pilot.resize_terminal(140, 30)
            await pilot.pause(0.4)
            assert not isinstance(app.screen, splash.SplashScreen)
            assert not app.screen.has_class("-narrow")     # the resize reached the app
            assert app.focused is app.query_one("#stations")
            before = app.current
            await pilot.press("j")
            assert app.current != before                   # the list is live at once
    run(go())


def test_a_small_terminal_still_gets_a_readable_splash():
    from twiddle.viz.splash import SplashArt
    art = SplashArt()
    for w, h in ((100, 30), (60, 20), (30, 8), (12, 4), (1, 1)):
        strips = art.draw(w, h, 0.5, 1 / 30, "tuning in")
        assert len(strips) == h and all(s.cell_length == w for s in strips)
    assert "dial" in "\n".join(s.text for s in art.draw(30, 12, 0.5, 1 / 30))


def test_twiddle_waves_but_never_dances_on_the_splash():
    """Nothing is playing yet: he may wave and blink, never hop or sing."""
    from twiddle.viz.splash import SplashArt
    art = SplashArt()
    for i in range(90):
        art.draw(100, 30, i / 30, 1 / 30)
    assert art.twiddle.beat_t < 0 and not art.twiddle.notes and art.twiddle.mouth == 0


def test_ctrl_w_brings_the_splash_back_and_any_key_leaves_it():
    from twiddle.dial.splash import SplashScreen
    out = FakeOutput(tuned="kalx")

    async def go():
        app = make_app(out)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            assert not isinstance(app.screen, SplashScreen)
            await pilot.press("ctrl+w")
            await pilot.pause(0.1)
            assert isinstance(app.screen, SplashScreen)
            await pilot.press("ctrl+w")                  # doesn't stack a second one
            await pilot.pause(0.1)
            assert not isinstance(app.screen, SplashScreen)
            assert out.tuned_to == [] and app.is_running
    run(go())


def test_a_held_volume_key_is_applied_as_it_goes_not_when_released():
    """Issue #1, point 1: the bar ran ahead of the sound because every press
    reset a 0.35s timer. The first press is written at once, a held key
    is written every interval, and the last value always lands."""
    out = FakeOutput()
    out.volume_interval_s = 0.05

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            assert app.volume == 30
            await pilot.press("minus")
            await pilot.pause()
            assert out.volumes == [28]                      # no waiting for a pause
            for _ in range(10):                             # a held key
                await pilot.press("minus")
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert len(out.volumes) > 2                     # applied during the hold
            assert out.volumes[-1] == app.volume == 8       # and the last one lands
            assert not app._vol_dirty
    run(go())


def test_the_bar_is_not_snapped_back_by_a_poll_mid_hold():
    out = FakeOutput()
    out.volume_interval_s = 0.4

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.press("minus")
            await pilot.press("minus")                      # second is pending: bar is ahead
            assert app._vol_dirty and app.volume == 26
            app._set_output_state(out, OutputState(tuned="relay", playing=True,
                                                   volume=28, muted=False))
            assert app.volume == 26                         # a stale read didn't win
            await asyncio.sleep(0.7)
            await pilot.pause()
            assert out.volumes[-1] == 26 and not app._vol_dirty
    run(go())


def test_linux_with_pulse_in_ffmpeg_gets_dials_own_volume_too(monkeypatch):
    """Chromebook parity: `-f pulse default` takes the same live gain, and
    the system volume (pactl) is not touched."""
    import subprocess as sp
    monkeypatch.setattr(output_mod.sys, "platform", "linux")
    devices = (" D. = Demuxing supported\n .E = Muxing supported\n --\n"
               "  E alsa            ALSA audio output\n  E pulse           Pulse audio output\n")
    monkeypatch.setattr(output_mod.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a, 0, stdout=devices, stderr=""))
    spawned, procs = [], []
    calls = []

    def spawn(argv, log):
        spawned.append(argv)
        procs.append(PipeProc())
        return procs[-1]
    out = LocalOutput(spawn=spawn, ffmpeg="/usr/bin/ffmpeg", pactl=lambda *a: calls.append(a))
    assert out.sink == "pulse"
    out.tune(STATIONS["kexp"])
    argv = spawned[0]
    assert argv[argv.index("-f") + 1] == "pulse" and argv[-1] == "default"
    out.gain_pace_s = 0.01
    out.set_volume(50)
    assert until(lambda: procs[0].written == ["cvolume@v -1 volume 0.2500\n"]) and calls == []
    out.close()


def test_linux_without_pulse_in_ffmpeg_falls_back_to_ffplay_and_the_sink_volume(monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(output_mod.sys, "platform", "linux")
    monkeypatch.setattr(output_mod.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a, 0, stdout="  E alsa  ALSA\n", stderr=""))
    out = LocalOutput(ffmpeg="/usr/bin/ffmpeg", ffplay="/usr/bin/ffplay", pactl=lambda *a: "")
    assert out.sink == "ffplay" and out.argv(output_mod.Media.of(STATIONS["kexp"]))[0] == "/usr/bin/ffplay"


def test_a_held_key_sends_only_the_latest_level_not_a_queue_of_stale_ones():
    """Codex finding 1: ffmpeg applies about one command per wake, so sending
    one per key press queued stale levels and mute waited behind them."""
    proc = PipeProc()
    out = LocalOutput(spawn=lambda argv, log: proc, sink="audiotoolbox", ffmpeg="/bin/ffmpeg")
    out.gain_pace_s = 0.05
    out.tune(STATIONS["kexp"])
    for v in range(99, 59, -1):                  # 40 presses in a burst
        out.set_volume(v)
    out.set_mute(True)                           # and mute right after
    assert until(lambda: proc.written and proc.written[-1].endswith("volume 0.0000\n"))
    assert len(proc.written) <= 4                # coalesced, and the last one lands
    out.close()


def test_a_blocked_pipe_never_blocks_stop_or_close():
    """Codex finding 4: a stalled ffmpeg that stops reading stdin must not
    hold the lock stop/respawn/close need."""
    import threading
    release = threading.Event()

    class Stuck(PipeProc):
        def write(self, data):
            release.wait(5)                      # the pipe is full
            raise BrokenPipeError
    proc = Stuck()
    out = LocalOutput(spawn=lambda argv, log: proc, sink="audiotoolbox", ffmpeg="/bin/ffmpeg")
    out.gain_pace_s = 0.01
    out.tune(STATIONS["kexp"])
    out.set_volume(20)
    import time as _t
    _t.sleep(0.1)                                # the sender is now stuck in write
    t0 = _t.monotonic()
    out.stop()
    out.tune(STATIONS["kalx"])                   # respawn
    out.close()
    assert _t.monotonic() - t0 < 1.0 and proc.done == 0
    release.set()


class SlowOutput(FakeOutput):
    """Each set_volume takes a while, as a Sonos SOAP call can."""
    def __init__(self, delay, **kw):
        super().__init__(**kw)
        self.delay = delay
        self.landed = []

    def set_volume(self, v):
        import time as _t
        _t.sleep(self.delay)
        self.landed.append(v)
        return super().set_volume(v)


def test_overlapping_slow_volume_writes_never_land_out_of_order():
    """Codex finding 2: exclusive=True cancels a worker task, not its thread,
    so an older slow write could land after the newest and win."""
    out = SlowOutput(0.15)
    out.volume_interval_s = 0.02

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            for _ in range(6):
                await pilot.press("minus")
                await asyncio.sleep(0.04)
            await asyncio.sleep(1.2)
            await pilot.pause()
            assert out.landed == sorted(out.landed, reverse=True)    # 28, 26, ... never back up
            assert out.landed[-1] == app.volume == 18
            assert not app._vol_dirty
    run(go())


def test_switching_output_during_a_volume_write_does_not_wedge_the_keys():
    """Codex finding 3: a write still in flight when the output changed left
    _vol_dirty set, so the new output's volume was never shown or settable."""
    out = SlowOutput(0.3)
    out.volume_interval_s = 0.02

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.press("minus")                 # write now in flight
            await asyncio.sleep(0.05)
            assert app._vol_busy
            app._reset_volume_state()                  # what choosing another output does
            app.volume = None
            await asyncio.sleep(0.6)                   # the old write lands
            await pilot.pause()
            assert not app._vol_dirty and not app._vol_busy
            app._set_output_state(app.output, OutputState(tuned="relay", playing=True,
                                                          volume=55, muted=False))
            assert app.volume == 55                    # the new output's volume shows
            await pilot.press("minus")
            await asyncio.sleep(0.7)
            assert out.landed[-1] == 53                # and the keys work
    run(go())


class AckingProc(PipeProc):
    """Answers each command line after a delay, as ffmpeg does: it prints
    "Command reply" to its log when it applies one."""
    def __init__(self, log, delay):
        super().__init__()
        self.log, self.delay, self.applied = log, delay, []
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()                              # as `_spawn` does

    def write(self, data):
        import threading as _th
        super().write(data)

        def reply():
            import time as _t
            _t.sleep(self.delay)
            with open(self.log, "ab") as f:
                f.write(b"Command reply for stream -1: ret:0 res:\n")
            self.applied.append(data.decode())
        _th.Thread(target=reply, daemon=True).start()


def test_commands_wait_for_ffmpegs_ack_so_none_pile_up(tmp_path):
    """Codex round 2, finding 1: a rate limit only guesses at ffmpeg's speed.
    With one outstanding command at a time, the backlog is bounded and the
    final mute is the very next command."""
    out = LocalOutput(sink="audiotoolbox", ffmpeg="/bin/ffmpeg",
                      spawn=lambda argv, log: setattr(out, "_p", AckingProc(log, 0.15)) or out._p)
    out.gain_pace_s = 0.01
    out.tune(STATIONS["kexp"])
    proc = out._p
    for v in range(99, 59, -1):                  # 40 changes, 20 ms apart
        out.set_volume(v)
        import time as _t
        _t.sleep(0.02)
    out.set_mute(True)
    assert until(lambda: proc.applied and proc.applied[-1].endswith("volume 0.0000\n"), 3.0)
    assert len(proc.written) <= 8                # ~ one per 150 ms ack, not 41
    out.close()


def test_a_mute_set_while_the_process_is_starting_is_not_lost():
    """Codex round 2, finding 2: argv had the old level, `_proc` was still
    None when the sender looked, and nothing woke it again."""
    holder = {}

    def spawn(argv, log):
        holder["out"].set_mute(True)             # the key arrives mid-spawn
        holder["proc"] = PipeProc()
        return holder["proc"]
    out = LocalOutput(sink="audiotoolbox", ffmpeg="/bin/ffmpeg", spawn=spawn)
    holder["out"] = out
    out.gain_pace_s = 0.01
    out.tune(STATIONS["kexp"])
    assert until(lambda: holder["proc"].written == ["cvolume@v -1 volume 0.0000\n"])
    assert out.state().muted
    out.close()


def test_only_one_sender_thread_ever_exists():
    """Codex round 2, finding 3: two senders could write mute then an older
    volume, leaving it audible while the model said muted."""
    import threading
    started, release = [], threading.Event()
    out = LocalOutput(sink="audiotoolbox", ffmpeg="/bin/ffmpeg", spawn=lambda a, l: PipeProc())
    out._gain_loop = lambda: (started.append(1), release.wait(5))   # counts this output's senders
    out.gain_pace_s = 0.05
    out.tune(STATIONS["kexp"])
    ts = [threading.Thread(target=out._push_gain) for _ in range(60)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    release.set()
    assert len(started) == 1


def test_returning_to_an_output_does_not_overlap_its_old_volume_write():
    """Codex round 2, finding 4: A -> B -> A reset the in-flight flag, so a
    new write could overtake the old one still running on A."""
    out = SlowOutput(0.3)
    out.volume_interval_s = 0.02

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.press("minus")                 # 28 in flight on A
            await asyncio.sleep(0.05)
            app._reset_volume_state()                  # away to B ...
            app._reset_volume_state()                  # ... and back to A
            app.volume = 30
            app.action_set_volume(20)                  # must wait for the 28
            await asyncio.sleep(1.0)
            await pilot.pause()
            assert out.landed == [28, 20]
            assert not app._vol_dirty
    run(go())


def test_an_unanswered_command_is_never_followed_by_more():
    """Codex round 3, finding 1: after a timeout the sender released more
    commands and rebuilt the backlog. One stays outstanding until answered."""
    proc = PipeProc()

    def spawn(argv, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()                              # readable, but ffmpeg never replies
        return proc
    out = LocalOutput(sink="audiotoolbox", ffmpeg="/bin/ffmpeg", spawn=spawn)
    out.gain_pace_s = 0.01
    out.tune(STATIONS["kexp"])
    import time as _t
    for v in (90, 80, 70, 60, 50):
        out.set_volume(v)
        _t.sleep(0.15)
    out.set_mute(True)
    _t.sleep(0.3)
    assert len(proc.written) == 1                # still waiting for the first ack
    with open(out.log, "ab") as f:               # now it answers
        f.write(b"Command reply for stream -1: ret:0 res:\n")
    assert until(lambda: len(proc.written) == 2 and proc.written[-1].endswith("0.0000\n"))
    out.close()


def test_each_process_has_its_own_log_so_replies_are_not_shared(tmp_path):
    """Codex round 3, finding 3: one shared log let another process's reply
    release this one's wait."""
    paths = []
    out_a = LocalOutput(sink="audiotoolbox", ffmpeg="/bin/ffmpeg",
                        spawn=lambda argv, log: paths.append(log) or PipeProc())
    out_b = LocalOutput(sink="audiotoolbox", ffmpeg="/bin/ffmpeg",
                        spawn=lambda argv, log: paths.append(log) or PipeProc())
    out_a.tune(STATIONS["kexp"])
    out_b.tune(STATIONS["kexp"])
    out_a.tune(STATIONS["kalx"])                 # a respawn
    assert len(set(paths)) == 3
    for o in (out_a, out_b):
        o.close()


def test_the_filter_and_the_changed_check_use_one_snapshot():
    """Codex round 3, finding 2."""
    out = LocalOutput(sink="audiotoolbox", ffmpeg="/bin/ffmpeg", spawn=lambda a, l: PipeProc())
    out.set_volume(40)
    out.set_mute(True)
    args = out.gain_args()
    assert args[-1] == "volume@v=0.0000" and out._argv_gain == (40, True)
    out.close()


def test_pruning_never_removes_a_log_a_live_process_is_using(tmp_path):
    """Codex round 4: a pruned live log made the ack unreadable (-1), which
    ended or wedged the wait, and stale commands could pile up again."""
    import os
    from twiddle.dial import state as dstate
    d = dstate.CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    import time as _t
    other_live = d / f"ffmpeg.{os.getppid()}.1.log"      # another session, alive
    dead = [d / f"ffmpeg.99999{i}.1.log" for i in range(9)]  # pids that are gone
    for i, p in enumerate([other_live, *dead]):
        p.write_text("x")
        os.utime(p, (_t.time() - 1000 + i, _t.time() - 1000 + i))   # all older than new ones
    out = LocalOutput(sink="audiotoolbox", ffmpeg="/bin/ffmpeg", spawn=lambda a, l: PipeProc())
    out.tune(STATIONS["kexp"])                          # live log of our own
    mine = out.log
    for _ in range(10):                                 # churn: respawns prune older logs
        out.tune(STATIONS["kalx"])
    live_now = out.log
    assert other_live.exists()                          # a live session's log survives
    assert live_now in out._live_logs
    assert not mine.exists() or mine != live_now        # old own logs may go
    assert sum(1 for p in dead if p.exists()) < len(dead)   # dead sessions' logs are pruned
    out.close()


def test_a_log_lost_mid_wait_stops_sending_and_reports_instead_of_queueing(tmp_path):
    """Codex round 5: releasing the outstanding command when the log vanished
    rebuilt the stale queue (21 commands, 0 acks). Now: stop, report, recover
    on the next play."""
    proc = PipeProc()
    procs = [proc]

    def spawn(argv, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()
        return procs[-1]
    out = LocalOutput(sink="audiotoolbox", ffmpeg="/bin/ffmpeg", spawn=spawn)
    out.gain_pace_s = 0.02
    out.tune(STATIONS["kexp"])
    out.set_volume(50)
    assert until(lambda: len(proc.written) == 1)
    out.log.unlink()                                    # cleanup removes it mid-wait
    import time as _t
    assert until(lambda: out._gain_broken is not None)
    for v in range(40, 20, -1):                         # a held key afterwards
        try:
            out.set_volume(v)
        except spotify_ops.PlaybackError as exc:
            assert "lost contact" in exc.message and "tune again" in exc.hint
        _t.sleep(0.01)
    _t.sleep(0.2)
    assert len(proc.written) == 1                       # nothing more sent blind
    assert out.state().volume == 21                     # the level is still kept
    procs.append(PipeProc())                            # tuning again: fresh process + log
    out.tune(STATIONS["kalx"])
    assert out._gain_broken is None
    out.set_volume(30)
    assert until(lambda: len(procs[-1].written) >= 1)
    out.close()
