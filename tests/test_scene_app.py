"""The TUI, driven by Textual's pilot over fake sources, bands and player.

No network, no Spotify, no speaker: the point is that the keys do what the
help screen says, and that playing sends the right tracks to the right
device.
"""
import asyncio
from datetime import date

from twiddle import lookup, spotify, spotify_ops
from twiddle.dial.output import Outputs
from twiddle.scene import cache
from twiddle.scenespec import dataset, genre as genre_mod, profiles
from twiddle.scene.app import SceneApp
from twiddle.scene.book import BandBook
from twiddle.scenedata.bands import BandcampEnricher, LookupEnricher
from twiddle.scenespec.model import Show
from twiddle.scene.players import Device, NeedsConfirmation, SpotifyConnectPlayer

TODAY = date(2026, 9, 22)
SHOWS = [
    Show(date(2026, 9, 23), "Thee Stork Club, Oakland", ["Girl Chow", "Wiseacre"]),
    Show(date(2026, 9, 24), "Ivy Room, Albany", ["Coup Dville"]),
    Show(date(2026, 9, 25), "Somewhere Else, S.F.", ["Nobody"]),
]
BC_TRACKS = [{"title": f"Demo {i}", "album": "Tape", "year": "2026", "duration": 120,
              "page": "https://x.bandcamp.com/album/tape", "id": i} for i in range(2)]
GIRL_CHOW_BC = [{"name": "Girl Chow", "item_url_root": "https://girlchow.bandcamp.com",
                 "location": "Oakland, California", "genre_name": "Reggae",
                 "tag_names": ["ska", "dub"]}]
TRACKS = [{"uri": f"spotify:track:{i}", "name": f"Song {i}", "album": {"name": "LP"}}
          for i in range(3)]


def _default_venue_rows():
    """The venue table a builder run with the stock Bay Area list would publish."""
    from twiddle.scenedata.builder import _venue_records
    from twiddle.scenedata.venues import DEFAULT_VENUES
    return _venue_records(DEFAULT_VENUES, None, None)


def publish_dataset(shows=None, bands=None, venues=None, complete=True, generated_at=None):
    """Put a dataset where the app looks (conftest redirects it to tmp_path).

    `bands`: {name: record} -- what the builder would have compiled. Bands
    billed but absent get a minimal record, as the builder writes.
    """
    shows = SHOWS if shows is None else shows
    recs = {}
    for s in shows:
        for b in s.bands:
            recs[dataset.band_id(b)] = profiles.minimal_record(b, None)
    for name, rec in (bands or {}).items():
        recs[dataset.band_id(name)] = rec
    dataset.publish(dataset.document(shows=shows, venues=_default_venue_rows() if venues is None else venues,
                                     bands=recs, sources={},
                                     enrichers={}, complete=complete, builder="test",
                                     generated_at=generated_at))


class FakeSpotify:
    name = "spotify"
    serial = False

    def enrich(self, p):
        p.spotify_artist = {"id": "a", "name": p.band}
        p.tracks = TRACKS

    def search(self, term):
        return [{"id": "a", "name": term}, {"id": "b", "name": term}]


class FakePlayer:
    def __init__(self, confirm_first=False):
        self.played = []
        self.confirm_first = confirm_first

    def devices(self):
        return [Device(id="rid", name="relay", relay=True), Device(id="mac", name="Mac")]

    def now(self):
        return {"playing": False}

    def play(self, uris, device, *, offset=0, confirmed=False, label=""):
        if self.confirm_first and not confirmed:
            raise NeedsConfirmation("takes Spotify away from the relay")
        self.played.append((uris, offset, device.id, confirmed))
        return "▶ ok"

    def back_to_relay(self):
        self.played.append("back")
        return "back"

    def pause(self):
        pass

    def resume(self):
        pass

    def next(self):
        pass


def _bandcamp_record(name, hit):
    """What a build that found `name` on Bandcamp publishes: the page, its songs,
    and the genre guess its tags give."""
    guess = profiles.guess_to_dict(genre_mod.for_band(hit, sure=True))
    return dict(profiles.minimal_record(name, None), bandcamp=hit, bc_tracks=BC_TRACKS,
                status={"bandcamp": "done"}, genre=guess)


def make_app(player=None, bandcamp=None, **kw):
    """`bandcamp`: {band: [search results]} -- the dataset says each band is on
    Bandcamp (the client never searches); a band without one knows nobody."""
    if kw.pop("publish", True):
        bands = dict(kw.pop("bands", None) or {})
        for name, results in (bandcamp or {}).items():
            if results and name not in bands:
                bands[name] = _bandcamp_record(name, results[0])
        publish_dataset(kw.pop("shows", None), bands, kw.pop("venues", None))

    def book_factory(on_update):
        return BandBook([FakeSpotify()], on_update=on_update)
    kw.setdefault("load_image", lambda url: None)      # no network for pictures
    kw.setdefault("band_photo", lambda p: (None, ""))
    kw.setdefault("instagram_picture", lambda handle: None)   # nor for venues
    kw.setdefault("bandcamp_stream", lambda t: f"https://t4.bcbits.com/stream/{t['title']}")
    kw.setdefault("bootstrap", False)
    kw.setdefault("run_build", lambda cancelled: (0, ""))
    return SceneApp(book_factory=book_factory, player=player or FakePlayer(),
                    today=TODAY, **kw)


async def settle(pilot, app, cond=lambda: True, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await pilot.pause(0.05)
        if cond():
            return
    raise AssertionError("condition never became true")


def run(coro):
    asyncio.run(coro)


def test_only_watched_venues_by_default_and_v_shows_all():
    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            await pilot.press("a")
            await settle(pilot, app, lambda: len(app._rows) == 3)
    run(go())


def test_enter_drills_into_the_lineup_then_tracks_and_plays():
    async def go():
        player = FakePlayer()
        app = make_app(player)
        cache.set_state("device", {"id": "mac", "name": "Mac", "relay": False})
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._rows)
            await pilot.press("enter")                  # show -> lineup
            assert app.focused.id == "lineup"
            await pilot.press("j")                      # second band
            assert app.current_band == "Wiseacre"
            await settle(pilot, app, lambda: app._profile() and app._profile().tracks)
            await pilot.press("enter")                  # lineup -> tracks
            assert app.focused.id == "tracks"
            await pilot.press("j", "j", "enter")        # third track -> play
            await settle(pilot, app, lambda: player.played)
            assert player.played == [([t["uri"] for t in TRACKS], 2, "mac", False)]
    run(go())


def test_play_without_a_device_opens_the_picker_first():
    async def go():
        player = FakePlayer()
        app = make_app(player)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: app.screen.__class__.__name__ == "ChoiceScreen")
            await pilot.press("j", "enter")             # the Mac
            await settle(pilot, app, lambda: player.played)
            assert player.played[0][2] == "mac"
            assert cache.get_state("device")["id"] == "mac"
    run(go())


def test_taking_spotify_from_the_relay_needs_a_second_p():
    async def go():
        player = FakePlayer(confirm_first=True)
        app = make_app(player)
        cache.set_state("device", {"id": "mac", "name": "Mac", "relay": False})
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: app._pending is not None)
            assert player.played == []
            await pilot.press("p")
            await settle(pilot, app, lambda: player.played)
            assert player.played[0][3] is True
            await pilot.press("R")
            await settle(pilot, app, lambda: "back" in player.played)
    run(go())


def test_typing_in_the_filter_does_not_trigger_shortcuts():
    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._rows)
            await pilot.press("slash")
            await pilot.press(*"quiet")                 # q would quit, t would cycle
            assert app.is_running and app.window == "month"
            assert app.filter_text == "quiet"
            await pilot.press("escape")
            await settle(pilot, app, lambda: len(app._rows) == 2)
    run(go())


def test_filter_narrows_by_band():
    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._rows)
            await pilot.press("slash", *"wise")
            await settle(pilot, app, lambda: len(app._rows) == 1)
    run(go())


def test_match_pins_the_chosen_artist():
    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().tracks)
            await pilot.press("m")
            await settle(pilot, app, lambda: app.screen.__class__.__name__ == "ChoiceScreen")
            await pilot.press("j", "enter")             # the second candidate
            await settle(pilot, app, lambda: cache.pinned("Girl Chow") is not None)
            assert cache.pinned("Girl Chow")["spotify_id"] == "b"
    run(go())


def test_narrow_terminal_hides_the_venue_column():
    async def go():
        app = make_app()
        async with app.run_test(size=(90, 40)) as pilot:
            await settle(pilot, app, lambda: app._rows)
            assert app.screen.has_class("-narrow")
    run(go())


def test_a_pane_left_open_past_midnight_moves_on():
    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            app._today = date(2026, 9, 24)      # two days later: Sep 23 is gone
            app._tick()
            await settle(pilot, app, lambda: len(app._rows) == 1)
    run(go())


def test_one_venue_spelled_several_ways_is_one_row():
    shows = [Show(date(2026, 9, 23), "924 Gilman Street, Berkeley", ["A"]),
             Show(date(2026, 9, 24), "924 Gilman St., Berkeley", ["B"]),
             Show(date(2026, 9, 25), "924 Gilman, Berkeley", ["C"])]

    async def go():
        app = make_app(shows=shows)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 3)
            venues = app.query_one("#venues")
            ids = [venues.get_option_at_index(i).id for i in range(venues.option_count)]
            assert ids.count("924 Gilman") == 1
            gilman = venues.get_option("924 Gilman")
            assert "3" in str(gilman.prompt)
            venues.highlighted = ids.index("924 Gilman")
            await settle(pilot, app, lambda: app.venue_choice == "924 Gilman")
            assert len(app._rows) == 3
    run(go())


def test_default_window_is_a_month_and_choices_are_remembered():
    """A week hid most of The List, which runs ~5 months ahead."""
    later = [Show(date(2026, 10, 12), "Ivy Room, Albany", ["Twenty Days Out"]),
             Show(date(2026, 12, 1), "Ivy Room, Albany", ["Ten Weeks Out"])]

    async def go():
        app = make_app(shows=SHOWS + later)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 3)   # 2 this week + 20 days out
            await pilot.press("t")                                  # All upcoming
            await settle(pilot, app, lambda: len(app._rows) == 4)
            await pilot.press("a")
            await settle(pilot, app, lambda: len(app._rows) == 5)
        assert cache.get_state("window") == "all"
        assert cache.get_state("all_venues") is True
        again = make_app(publish=False)
        assert again.window == "all" and again.all_venues
    run(go())


def _png():
    from PIL import Image
    return Image.new("RGBA", (8, 8), (200, 30, 30, 255))


def test_band_photo_answer_for_a_band_no_longer_on_screen_is_dropped():
    """A slow Bandcamp search for band A lands after the cursor moved to B."""
    import threading
    release = threading.Event()
    red, blue = _png(), _png()

    def band_photo(p):
        if p.band == "Girl Chow":
            release.wait(3)
            return "red.png", "Bandcamp"
        return "blue.png", "Wikipedia"

    def load(url):
        return {"red.png": red, "blue.png": blue}.get(url)

    async def go():
        app = make_app(load_image=load, band_photo=band_photo)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._rows)
            await pilot.press("enter", "j")             # Girl Chow -> Wiseacre
            photo = app.query_one("#photo")
            await settle(pilot, app, lambda: photo.image is blue)
            release.set()
            await pilot.pause(0.3)
            assert photo.image is blue
            assert "Wikipedia" in str(app.query_one("#photo-credit").render())
    run(go())


def test_venue_icon_is_the_venues_logo_else_a_tile():
    from twiddle.scenespec import venue as venues
    logo = _png()
    watched = (venues.Venue("Stork Club", ("stork club",)),
               venues.Venue("Ivy Room", ("ivy room",), "https://example.test/ivy.png"))

    async def go():
        app = make_app(load_image=lambda url: logo if url.endswith("ivy.png") else None,
                       watched=watched)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._rows)
            icon = app.query_one("#venue-icon")
            await settle(pilot, app, lambda: icon.image is not None)
            assert icon.image is not logo              # Stork Club: generated tile
            await pilot.press("j")                      # Ivy Room
            await settle(pilot, app, lambda: icon.image is logo)
    run(go())


def test_a_venue_logo_that_failed_once_is_tried_again():
    """scene lives in a tmux pane for days: one failed fetch isn't forever."""
    from twiddle.scenespec import venue as venues
    logo = _png()
    replies = [None, logo]
    watched = (venues.Venue("Stork Club", ("stork club",), "https://example.test/stork.png"),
               venues.Venue("Ivy Room", ("ivy room",)))

    async def go():
        app = make_app(load_image=lambda url: replies.pop(0) if replies else logo,
                       watched=watched)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._rows)
            icon = app.query_one("#venue-icon")
            await settle(pilot, app, lambda: icon.image is not None)
            assert icon.image is not logo              # the fetch failed: a tile
            await pilot.press("j", "k")                 # away to Ivy Room and back
            await settle(pilot, app, lambda: icon.image is logo)
    run(go())


class FakeUrlOutput:
    volume_interval_s = 0.0

    def __init__(self, label, confirm_first=False):
        self.label = label
        self.played = []
        self.stopped = 0
        self.playing = False
        self.confirm_first = confirm_first
        self.volume = 30
        self.volumes = []           # what set_volume was asked for
        self.found = None           # what `capture` reports the room was doing: a token, or None
        self.captured = 0
        self.restored = []          # (token, volume_only)
        self.elsewhere = ""         # a uri someone else put on the room

    def capture(self):
        self.captured += 1
        return self.found

    def restore(self, token, *, volume_only=False):
        self.restored.append((token, volume_only))
        self.playing = False
        return f"▶ {self.label} back as it was"

    def set_volume(self, volume):
        self.volumes.append(volume)
        self.volume = volume
        return volume

    def set_mute(self, muted):
        pass

    def play(self, media, *, confirmed=False, source="dial"):
        if self.confirm_first and not confirmed:
            raise NeedsConfirmation("Spotify is playing on the Roams through the relay")
        self.played.append((media.url, media.name, confirmed))
        self.playing = True
        return f"▶ {media.name}"

    def stop(self):
        self.stopped += 1
        self.playing = False
        return "stopped"

    def state(self):
        from twiddle.dial.output import OutputState
        uri = self.elsewhere or (self.played[-1][0] if self.played else "")
        return OutputState(playing=self.playing, volume=self.volume,
                           uri=uri, other=uri)

    def close(self):
        pass


class FakeUrlOutputs(Outputs):
    def __init__(self, **kw):
        super().__init__()
        self.mac = FakeUrlOutput("This Mac (speakers)")
        self.roam = FakeUrlOutput("Roam", **kw)
        self.asked = []

    def get(self, oid):
        self.asked.append(oid)
        return self.mac if oid == "mac" else self.roam

    def close(self):
        pass


def _bandcamp_app(device, outputs, player=None):
    cache.set_state("device", device)
    return make_app(player, bandcamp={"Girl Chow": GIRL_CHOW_BC}, outputs=outputs)


def test_bandcamp_songs_list_first_and_play_on_this_mac_without_spotify():
    async def go():
        player = FakePlayer()
        outs = FakeUrlOutputs()
        app = _bandcamp_app({"id": "mac", "name": "Mac", "relay": False, "local": True},
                            outs, player)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks
                         and app._profile().tracks)
            tracks = app.query_one("#tracks")
            ids = [tracks.get_option_at_index(i).id for i in range(tracks.option_count)]
            assert ids == [None, "bc:0", "bc:1", None, "sp:0", "sp:1", "sp:2"]
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.mac.played)
            assert outs.mac.played[0][0] == "https://t4.bcbits.com/stream/Demo 0"
            assert player.played == []                         # Spotify untouched
            await settle(pilot, app, lambda: "Bandcamp" in str(
                app.query_one("#nowbar").render()))
            await pilot.press("space")                         # stops, not Spotify pause
            await settle(pilot, app, lambda: outs.mac.stopped == 1)
            assert app.bc_now is None
    run(go())


class NoLocalPlayerYet:
    name = "Kenny's MacBook (scene)"

    def find(self, devices):
        return None

    def stop(self):
        pass


def test_a_bandcamp_track_plays_on_this_mac_with_no_spotify_sign_in():
    # A friend's Mac before `spotify auth`: the device picker used to fail
    # on Spotify's device list, so nothing could play at all.
    def signed_out():
        raise spotify_ops.PlaybackError("no Spotify tokens") from spotify.AuthError("x")

    async def go():
        player = SpotifyConnectPlayer(session_factory=signed_out, local=NoLocalPlayerYet(),
                                      relay_available=lambda: False)
        outs = FakeUrlOutputs()
        app = make_app(player, bandcamp={"Girl Chow": GIRL_CHOW_BC}, outputs=outs)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: app.screen.__class__.__name__ == "ChoiceScreen")
            await pilot.press("enter")                  # the only choice: this Mac
            await settle(pilot, app, lambda: outs.mac.played)
            assert outs.mac.played[0][0] == "https://t4.bcbits.com/stream/Demo 0"
    run(go())


def test_a_spotify_row_below_the_bandcamp_ones_plays_the_right_spotify_track():
    async def go():
        player = FakePlayer()
        app = _bandcamp_app({"id": "mac", "name": "Mac", "relay": False, "local": True},
                            FakeUrlOutputs(), player)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks
                         and app._profile().tracks)
            tracks = app.query_one("#tracks")
            tracks.focus()
            tracks.highlighted = tracks.get_option_index("sp:1")
            await pilot.press("p")
            await settle(pilot, app, lambda: player.played)
            uris, offset, _dev, _c = player.played[0]
            assert uris[offset] == "spotify:track:1"
    run(go())


def test_bandcamp_on_the_roams_asks_first_when_the_relay_is_playing():
    async def go():
        outs = FakeUrlOutputs(confirm_first=True)
        app = _bandcamp_app({"id": "rid", "name": "relay", "relay": True, "local": False},
                            outs)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: app._pending)
            assert outs.roam.played == [] and set(outs.asked) == {"room:roam"}
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            assert outs.roam.played[0][2] is True
    run(go())


def test_bandcamp_wont_go_to_a_phone():
    async def go():
        outs = FakeUrlOutputs()
        app = _bandcamp_app({"id": "ph", "name": "Phone", "relay": False, "local": False},
                            outs)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await pilot.pause(0.2)
            assert outs.asked == []
    run(go())


def test_the_genre_column_comes_from_the_dataset_and_browsing_asks_no_network(monkeypatch):
    """The table's "Sounds like" was a Bandcamp scan run by the TUI. It is the
    builder's now: the app reads each band's stored guess and asks nobody."""
    from twiddle.scenedata import bandcamp
    monkeypatch.setattr(bandcamp, "search", lambda *a, **k: 1 / 0)
    guess = profiles.guess_to_dict(genre_mod.for_band(GIRL_CHOW_BC[0], sure=True))
    known = {"Girl Chow": dict(profiles.minimal_record("Girl Chow", None), genre=guess)}

    async def go():
        app = make_app(bands=known)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            cell = str(app.query_one("#shows").get_row_at(0)[2])
            assert "reggae" in cell.lower()
            await pilot.press("a")                      # every venue: still no network
            await settle(pilot, app, lambda: len(app._rows) == 3)
    run(go())


# ---- one stream at a time ---------------------------------------------------------------

MAC_DEV = {"id": "mac", "name": "Mac", "relay": False, "local": True}


async def _bandcamp_playing(pilot, app, outs, where):
    await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks
                 and app._profile().tracks)
    await pilot.press("p")
    await settle(pilot, app, lambda: where.played and app.bc_now)


def _spotify_row(app, row="sp:0"):
    tracks = app.query_one("#tracks")
    tracks.focus()
    tracks.highlighted = tracks.get_option_index(row)


def test_spotify_on_another_device_stops_a_bandcamp_track_left_on_the_mac():
    """Bandcamp on This Mac, then Spotify to a phone: ffplay used to play on."""
    async def go():
        player, outs = FakePlayer(), FakeUrlOutputs()
        app = _bandcamp_app(MAC_DEV, outs, player)
        async with app.run_test(size=(160, 45)) as pilot:
            await _bandcamp_playing(pilot, app, outs, outs.mac)
            app.device = Device(id="ph", name="Phone")
            _spotify_row(app)
            await pilot.press("p")
            await settle(pilot, app, lambda: player.played and outs.mac.stopped)
            assert app.bc_now is None and outs.roam.stopped == 0
    run(go())


def test_spotify_on_the_mac_stops_a_bandcamp_track_left_on_the_roam():
    async def go():
        player, outs = FakePlayer(), FakeUrlOutputs()
        app = _bandcamp_app({"id": "rid", "name": "relay", "relay": True, "local": False},
                            outs, player)
        async with app.run_test(size=(160, 45)) as pilot:
            await _bandcamp_playing(pilot, app, outs, outs.roam)
            app.device = Device(id="mac", name="Mac", local=True)
            _spotify_row(app)
            await pilot.press("p")
            await settle(pilot, app, lambda: player.played and outs.roam.stopped)
            assert app.bc_now is None
    run(go())


def test_spotify_to_the_relay_leaves_the_roam_to_be_repointed_not_stopped():
    async def go():
        player, outs = FakePlayer(), FakeUrlOutputs()
        app = _bandcamp_app({"id": "rid", "name": "relay", "relay": True, "local": False},
                            outs, player)
        async with app.run_test(size=(160, 45)) as pilot:
            await _bandcamp_playing(pilot, app, outs, outs.roam)
            _spotify_row(app)
            await pilot.press("p")
            await settle(pilot, app, lambda: player.played and app.bc_now is None)
            assert outs.roam.stopped == 0          # ensure_room_on_relay replaces it
    run(go())


def test_switching_device_moves_the_bandcamp_track_and_stops_the_old_output():
    async def go():
        player, outs = FakePlayer(), FakeUrlOutputs()
        app = _bandcamp_app(MAC_DEV, outs, player)
        async with app.run_test(size=(160, 45)) as pilot:
            await _bandcamp_playing(pilot, app, outs, outs.mac)
            app.query_one("#shows").focus()
            await pilot.press("d")
            await settle(pilot, app, lambda: len(app.screen_stack) > 1)
            await pilot.press("enter")                 # Roams (relay): listed first
            await settle(pilot, app, lambda: outs.roam.played and outs.mac.stopped)
            assert app.bc_now["output"] == "room:roam"
            assert outs.roam.played[0][0] == outs.mac.played[0][0]
    run(go())


def test_bandcamp_anywhere_pauses_spotify_on_the_macs_own_player():
    """Spotify on This Mac's librespot, then a Bandcamp track on the Roam:
    only the Mac case used to pause it."""
    class LocalSpotify(FakePlayer):
        local = type("L", (), {"name": "Mac"})()

        def __init__(self):
            super().__init__()
            self.paused = 0

        def now(self):
            return {"playing": True, "device": "Mac"}

        def pause(self):
            self.paused += 1

    async def go():
        player, outs = LocalSpotify(), FakeUrlOutputs()
        app = _bandcamp_app({"id": "rid", "name": "relay", "relay": True, "local": False},
                            outs, player)
        async with app.run_test(size=(160, 45)) as pilot:
            await _bandcamp_playing(pilot, app, outs, outs.roam)
            await settle(pilot, app, lambda: player.paused == 1)
    run(go())


def test_i_shows_the_venue_and_w_g_open_its_site_and_map(monkeypatch):
    opened = []
    monkeypatch.setattr("twiddle.scene.app.webbrowser.open", opened.append)

    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app.current_show)
            await pilot.press("i")
            await settle(pilot, app, lambda: app.screen.__class__.__name__ == "VenueScreen")
            assert app.screen.venue_name == "Stork Club"
            assert app.screen.info.address.startswith("2330 Telegraph Ave")
            await pilot.press("w", "g")
            assert opened[0] == "https://theestorkclub.com/"
            assert "2330%20Telegraph" in opened[1]
            await pilot.press("escape")
            await settle(pilot, app, lambda: app.screen.__class__.__name__ != "VenueScreen")
    run(go())


def test_an_unwatched_venue_still_gets_a_map_search(monkeypatch):
    opened = []
    monkeypatch.setattr("twiddle.scene.app.webbrowser.open", opened.append)

    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("a")
            await settle(pilot, app, lambda: len(app._rows) == 3)
            app.current_show = SHOWS[2]
            await pilot.press("i")
            await settle(pilot, app, lambda: app.screen.__class__.__name__ == "VenueScreen")
            assert not app.screen.info
            await pilot.press("w", "g")
            assert len(opened) == 1 and "Somewhere%20Else" in opened[0]
    run(go())


def test_a_room_the_dataset_knows_but_does_not_watch_is_described_never_watched():
    from twiddle.scenespec import venue as venue_mod

    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("a")
            await settle(pilot, app, lambda: len(app._rows) == 3)
            app.known = venue_mod.from_rows([{"id": "somewhere-else", "name": "Somewhere Else",
                                              "city": "Oakland", "address": "1 Main St",
                                              "match": [SHOWS[2].venue.lower()]}])
            app.current_show = SHOWS[2]
            await pilot.press("i")
            await settle(pilot, app, lambda: app.screen.__class__.__name__ == "VenueScreen")
            assert app.screen.info.address == "1 Main St" and "1%20Main%20St" in app.screen.map_url
            assert not any(w.name == "Somewhere Else" for w in app.watched)    # described, not watched
    run(go())


def test_a_venue_with_an_article_shows_its_stored_summary(monkeypatch):
    """The summary comes out of the dataset: opening the card fetches nothing."""
    import twiddle.scenedata.venue_info as vi
    monkeypatch.setattr(vi, "wiki_summary", lambda *a, **k: 1 / 0)
    fox = [Show(date(2026, 9, 23), "Fox Theater, Oakland", ["Somebody"])]
    venue = {"id": "fox-theater", "name": "Fox Theater",
             "wikipedia_summary": {"extract": "About Fox Oakland Theatre.", "url": "u"}}

    async def go():
        app = make_app(shows=fox, venues=[venue])
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app.current_show)
            await pilot.press("i")
            await settle(pilot, app, lambda: "About Fox Oakland Theatre."
                         in str(app.screen.query_one("#venue-wiki").render()))
    run(go())


def test_c_copies_the_show_to_send_and_l_opens_its_listing(monkeypatch):
    copied, opened = [], []
    monkeypatch.setattr("twiddle.scene.app.shutil.which", lambda name: "/usr/bin/pbcopy")
    monkeypatch.setattr("twiddle.scene.app.subprocess.run",
                        lambda cmd, input=None, check=False: copied.append(input.decode()))
    monkeypatch.setattr("twiddle.scene.app.webbrowser.open", opened.append)
    listed = [Show(date(2026, 9, 23), "Thee Stork Club, Oakland", ["Girl Chow"], times="8pm",
                   source_url="http://www.foopee.com/punk/the-list/by-club.2.html#Thee_Stork")]

    async def go():
        app = make_app(shows=listed)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app.current_show)
            await pilot.press("c", "l")
            assert copied == ["Girl Chow\nWed Sep 23, 8pm\nStork Club · 2330 Telegraph Ave, "
                              "Oakland\nhttp://www.foopee.com/punk/the-list/by-club.2.html"
                              "#Thee_Stork"]
            assert opened == [listed[0].source_url]
    run(go())


def test_a_shows_flyer_replaces_the_venue_icon_and_f_opens_it(monkeypatch):
    """Keyed per show: two Stork nights in a row have two flyers, although
    the venue card itself has nothing to redraw."""
    opened = []
    monkeypatch.setattr("twiddle.scene.app.webbrowser.open", opened.append)
    from PIL import Image
    a = Image.new("RGB", (1200, 1800), (200, 30, 30))      # a full-size poster
    b = Image.new("RGB", (600, 600), (30, 30, 200))

    def colour(w):
        return w.image.getpixel((0, 0)) if w.image is not None else None
    nights = [Show(date(2026, 9, 23), "Thee Stork Club, Oakland", ["X"], flyer="https://f/a"),
              Show(date(2026, 9, 24), "Thee Stork Club, Oakland", ["Y"], flyer="https://f/b"),
              Show(date(2026, 9, 25), "Thee Stork Club, Oakland", ["Z"])]

    async def go():
        app = make_app(shows=nights,
                       load_image=lambda url: {"https://f/a": a, "https://f/b": b}.get(url))
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._rows)
            flyer, icon = app.query_one("#flyer"), app.query_one("#venue-icon")
            await settle(pilot, app, lambda: colour(flyer) == (200, 30, 30))
            assert flyer.display and not icon.display
            assert max(flyer.image.size) <= 480               # kept small, not the poster
            await pilot.press("j")
            await settle(pilot, app, lambda: colour(flyer) == (30, 30, 200))
            await pilot.press("f")
            assert opened == ["https://f/b"]
            await pilot.press("j")                      # no flyer: the icon is back
            await settle(pilot, app, lambda: not flyer.display)
            assert icon.display
            await pilot.press("f")
            assert opened == ["https://f/b"]
    run(go())


def test_a_night_with_no_bands_shows_its_name_and_looks_nothing_up(monkeypatch):
    night = [Show(date(2026, 9, 23), "Thee Stork Club, Oakland", [], title="Freakyoke",
                  notes=["Karaoke"])]

    async def go():
        app = make_app(shows=night)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app.current_show)
            table = app.query_one("#shows")
            assert "Freakyoke" in str(table.get_row_at(0)[-1])
            assert "Freakyoke" in str(app.query_one("#profile").render())
            assert app.current_band is None and not app.book.profiles
    run(go())


def test_a_venue_without_a_logo_gets_its_instagram_picture(monkeypatch):
    from twiddle.scenespec import venue as venues
    from twiddle.scenespec.venue import VenueInfo
    pic = _png()
    asked = []
    watched = (venues.Venue("Stork Club", ("stork club",),
                            info=VenueInfo(instagram="theestorkcluboakland")),)

    async def go():
        app = make_app(watched=watched,
                       instagram_picture=lambda h: asked.append(h) or pic)
        async with app.run_test(size=(160, 45)) as pilot:
            icon = app.query_one("#venue-icon")
            await settle(pilot, app, lambda: icon.image is pic)
            assert asked == ["theestorkcluboakland"]
    run(go())


def test_double_clicking_the_venue_card_opens_its_instagram(monkeypatch):
    from twiddle.scenespec import venue as venues
    from twiddle.scenespec.venue import VenueInfo
    opened = []
    monkeypatch.setattr("twiddle.scene.app.webbrowser.open", opened.append)
    watched = (venues.Venue("Stork Club", ("stork club",),
                            info=VenueInfo(instagram="theestorkcluboakland")),)

    async def go():
        app = make_app(watched=watched)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app.current_show)
            await pilot.click("#venue-name")
            assert opened == []                       # one click does nothing
            await pilot.click("#venue-name", times=2)
            assert opened == ["https://www.instagram.com/theestorkcluboakland/"]
    run(go())


def test_v_visualizes_and_says_why_when_spotify_has_no_stream_here():
    """`v` is the visualizer now; every venue moved to `a`. Spotify on a
    phone has nothing to tap, so the screen says so and starts no tap."""
    from twiddle.viz.screen import VizCanvas, VizScreen
    from tests.test_viz_screen import RecordingTap, options

    _store, kw = options()

    async def go():
        calls = []

        class Recording(FakePlayer):
            def pause(self):
                calls.append("pause")

            def resume(self):
                calls.append("resume")

            def next(self):
                calls.append("next")
        player = Recording()
        app = make_app(player)
        app.viz_options = kw
        async with app.run_test(size=(160, 45)) as pilot:
            app.device = Device(id="ph", name="Phone")
            await pilot.press("v")
            await pilot.pause(0.3)
            assert isinstance(app.screen, VizScreen)
            text = "\n".join(s.text for s in app.screen.query_one(VizCanvas).strips)
            assert "Phone" in text and "no signal" in text
            assert RecordingTap.made == []
            # scene's own keys stay out while only the picture shows.
            before = app.all_venues
            await pilot.press("space", "R", "p", "n", "a", "d")
            await pilot.pause(0.5)
            assert isinstance(app.screen, VizScreen) and app.all_venues == before
            assert player.played == [] and calls == []
            await pilot.press("v")
            await pilot.pause()
            assert not isinstance(app.screen, VizScreen)
    run(go())


# ---- the dataset boundary -----------------------------------------------------------------


def _no_collection(monkeypatch):
    """Anything that scrapes, searches or looks up now explodes."""
    import twiddle.scenedata.sources as srcs
    from twiddle import bandcamp as site
    from twiddle.scenedata import venue_info
    from twiddle.scenedata import bandcamp
    boom = lambda *a, **k: 1 / 0            # noqa: E731
    monkeypatch.setattr(srcs, "fetch_all", boom)
    monkeypatch.setattr(bandcamp, "search", boom)
    monkeypatch.setattr(site, "_get", boom)
    monkeypatch.setattr(lookup, "identify", boom)
    monkeypatch.setattr(venue_info, "wiki_summary", boom)


def _compiled(name, **kw):
    """A band the builder fully enriched."""
    from twiddle.scenespec.band import BandProfile
    p = BandProfile(band=name)
    p.status = {"lookup": "done", "spotify": "done", "bandcamp": "done"}
    p.spotify_artist = {"id": "a", "name": name}
    p.tracks = TRACKS
    p.bandcamp = {"name": name, "item_url_root": "https://x.bandcamp.com"}
    p.bc_tracks = BC_TRACKS
    for k, v in kw.items():
        setattr(p, k, v)
    from twiddle.scenedata.bands import assess
    assess(p)
    return profiles.to_record(p, updated_at=1.0, guess=None)


def _identity_only(name):
    """What the builder writes now: who the band is, no song lists."""
    return dict(_compiled(name), tracks=[], bc_tracks=[],
                status={"lookup": "done", "spotify": "done", "bandcamp": "done"})


def _production_book(on_update):
    """`cli.tui_book`, the production book, with the network dead: every fetch explodes."""
    from twiddle.scene.cli import tui_book

    def dead_session():
        raise ConnectionError("network is down")

    def dead(url):
        raise ConnectionError("network is down")
    return tui_book(on_update, session_factory=dead_session, bc_tracks=dead)


def test_with_the_network_dead_scene_browses_a_compiled_dataset(monkeypatch):
    """Acceptance: launch offline and browse events, venues and lineup data --
    with the production-shaped book, over records shaped as the builder writes
    them (identity, genre, no song lists)."""
    _no_collection(monkeypatch)
    guess = profiles.guess_to_dict(genre_mod.for_band(GIRL_CHOW_BC[0], sure=True))
    bands = {"Girl Chow": dict(_identity_only("Girl Chow"), genre=guess),
             "Wiseacre": _identity_only("Wiseacre")}

    async def go():
        app = make_app(bands=bands)
        app.book = _production_book(app._from_worker_band)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            assert "reggae" in str(app.query_one("#shows").get_row_at(0)[2]).lower()
            await settle(pilot, app, lambda: app._profile() is not None
                         and app._profile().status.get("tracks", "").startswith("error"))
            p = app._profile()
            assert p.confidence == "name_only" and not p.busy()        # not stuck "looking"
            assert "Girl Chow" in str(app.query_one("#profile").render())
            assert "searching" not in (app.query_one("#tracks").border_subtitle or "")
            await pilot.press("enter", "j")                            # the other band
            assert app.current_band == "Wiseacre"
            await settle(pilot, app, lambda: app._profile().status.get("tracks", "")
                         .startswith("error"))
            assert app.is_running
    run(go())


def test_the_app_only_looks_up_the_band_on_screen_and_only_what_the_dataset_lacks():
    """No prefetch of the lineup: Wiseacre is not asked about until selected."""
    asked = []

    class Spy(FakeSpotify):
        def enrich(self, p):
            asked.append(p.band)
            super().enrich(p)

    async def go():
        app = make_app()
        app.book = BandBook([Spy()], on_update=app._from_worker_band)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().tracks)
            await pilot.pause(1.2)                       # the old prefetch fired at 0.8s
            assert asked == ["Girl Chow"]
            await pilot.press("enter", "j")
            await settle(pilot, app, lambda: "Wiseacre" in asked)
            assert asked == ["Girl Chow", "Wiseacre"]
    run(go())


def test_a_hand_pin_beats_the_datasets_name_search_pick(monkeypatch):
    cache.pin("Girl Chow", "b", "Girl Chow (the other one)")
    asked = []

    class Spy(FakeSpotify):
        def enrich(self, p):
            asked.append(p.band)
            p.spotify_artist = {"id": "b", "name": p.band}
            p.tracks = TRACKS[:1]

    async def go():
        app = make_app(bands={"Girl Chow": _compiled("Girl Chow")})   # dataset picked "a"
        app.book = BandBook([Spy()], on_update=app._from_worker_band)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: asked == ["Girl Chow"])
            await settle(pilot, app, lambda: len(app._profile().tracks) == 1)
            assert app._profile().spotify_artist["id"] == "b"
    run(go())


def test_not_on_spotify_pin_clears_the_datasets_tracks(monkeypatch):
    cache.pin("Girl Chow", None)

    class Spy(FakeSpotify):
        def enrich(self, p):
            p.spotify_artist, p.tracks = None, []

    async def go():
        app = make_app(bands={"Girl Chow": _compiled("Girl Chow")})
        app.book = BandBook([Spy()], on_update=app._from_worker_band)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().status["spotify"] == "done")
            assert app._profile().tracks == [] and app._profile().confidence == "none"
    run(go())


def test_r_runs_the_builder_then_shows_what_it_published():
    runs = []

    def builder_run(cancelled):
        runs.append(1)
        publish_dataset(SHOWS + [Show(date(2026, 9, 26), "Ivy Room, Albany", ["Fresh Blood"])])
        return 0, ""

    async def go():
        app = make_app(run_build=builder_run)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            await pilot.press("r")
            await settle(pilot, app, lambda: len(app._rows) == 3)
            assert runs == [1] and not app._building
    run(go())


def test_a_build_published_by_someone_else_appears_without_a_keypress():
    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            import os
            publish_dataset(SHOWS + [Show(date(2026, 9, 26), "Ivy Room, Albany", ["Fresh"])])
            os.utime(dataset.default_path(), (1, 1_900_000_000))   # a distinct mtime
            app._watch_dataset()
            await settle(pilot, app, lambda: len(app._rows) == 3)
    run(go())


def test_no_dataset_yet_bootstraps_one_build_and_says_so_meanwhile():
    calls = []

    def first_build(cancelled):
        calls.append(1)
        publish_dataset()
        return 0, ""

    async def go():
        app = make_app(publish=False, bootstrap=True, run_build=first_build)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            assert calls == [1]
    run(go())


def test_no_dataset_without_bootstrap_waits_and_tells_you_how_to_build_it():
    async def go():
        app = make_app(publish=False, bootstrap=False)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.2)
            assert app._rows == {} and "no dataset yet" in app.sub_title
            assert app.snapshot is None
    run(go())


def test_a_stale_dataset_is_flagged_not_rebuilt_behind_your_back():
    import time
    builds = []

    async def go():
        publish_dataset(generated_at=time.time() - 3 * 86400)
        app = make_app(publish=False, run_build=lambda cancelled: (builds.append(1), (0, ""))[1])
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            assert "stale" in app.sub_title and builds == []
    run(go())


def test_freshness_shows_and_an_unfinished_build_says_it_is_still_enriching():
    import time
    async def go():
        publish_dataset(generated_at=time.time() - 3 * 3600, complete=False)
        app = make_app(publish=False)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            assert "built 3h ago" in app.sub_title and "enriching" in app.sub_title
    run(go())


def test_a_build_already_running_is_not_an_error():
    async def go():
        app = make_app(run_build=lambda cancelled: (75, "another `scene build` is running"))
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            await pilot.press("r")
            await settle(pilot, app, lambda: not app._building)
            assert app.is_running
    run(go())


def test_an_unreadable_dataset_reports_and_keeps_what_is_on_screen():
    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            dataset.default_path().write_text("{broken")
            app._reload()
            await pilot.pause(0.1)
            assert len(app._rows) == 2
    run(go())


def test_a_checkpoint_of_a_running_build_does_not_move_your_cursor_or_focus():
    """The builder republishes every 25 bands. Only band records change; the
    app must not throw the user back to band 0 of the lineup."""
    import os
    guess = profiles.guess_to_dict(genre_mod.for_band(GIRL_CHOW_BC[0], sure=True))

    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().tracks)
            await pilot.press("enter", "j")                # lineup -> Wiseacre
            await settle(pilot, app, lambda: app.current_band == "Wiseacre"
                         and app._profile().tracks)
            await pilot.press("enter")                     # focus the tracks
            await pilot.press("j")
            assert app.focused.id == "tracks"
            shown = app.query_one("#tracks").highlighted
            picked = app.query_one("#tracks").get_option_at_index(shown).id
            publish_dataset(bands={"Girl Chow": dict(profiles.minimal_record("Girl Chow", None),
                                                     genre=guess)}, complete=False)
            os.utime(dataset.default_path(), (1, 1_900_000_001))
            app._watch_dataset()
            await pilot.pause(0.2)
            assert app.current_band == "Wiseacre" and app.focused.id == "tracks"
            tracks = app.query_one("#tracks")
            assert tracks.get_option_at_index(tracks.highlighted).id == picked
            assert "reggae" in str(app.query_one("#shows").get_row_at(0)[2]).lower()
            assert "enriching" in app.sub_title
    run(go())


def test_reloading_never_replaces_a_profile_whose_lookup_is_running():
    from twiddle.scenespec.band import BandProfile
    book = BandBook([])
    running = BandProfile(band="Girl Chow")
    running.status = {"spotify": "running"}
    book.seed([running])
    fresh = BandProfile(band="Girl Chow")
    fresh.status = {"spotify": "idle"}
    book.seed([fresh])
    assert book.profiles["girl chow"] is running
    looked_up = BandProfile(band="Wiseacre")
    looked_up.status = {"spotify": "done", "bandcamp": "done"}
    looked_up.bandcamp = {"item_url_root": "https://w.bandcamp.com"}
    book.seed([looked_up])
    thinner = BandProfile(band="Wiseacre")
    thinner.status = {"spotify": "done", "bandcamp": "idle"}
    book.seed([thinner])
    merged = book.profiles["wiseacre"]                   # an identity found here is kept ...
    assert merged.bandcamp == {"item_url_root": "https://w.bandcamp.com"}
    assert merged.status["bandcamp"] == "done" and merged.status["spotify"] == "done"
    book.close()


def _viewed(spotify_id, tracks=True):
    from twiddle.scenespec.band import BandProfile
    p = BandProfile(band="Girl Chow")
    p.status = {"lookup": "done", "spotify": "done", "bandcamp": "done",
                "tracks": "done" if tracks else "idle"}
    p.spotify_artist = {"id": spotify_id}
    p.bandcamp = {"item_url_root": "https://x.bandcamp.com"}
    if tracks:
        p.tracks, p.bc_tracks = [{"uri": "u"}], [{"title": "t"}]
    return p


def test_a_viewed_band_takes_a_corrected_record_and_drops_lists_of_the_old_artist():
    """Codex: four done enrichers (with tracks) must not veto a three-done record."""
    book = BandBook([])
    book.seed([_viewed("old", tracks=True)])
    book.seed([_viewed("corrected", tracks=False)])
    p = book.profiles["girl chow"]
    assert p.spotify_artist == {"id": "corrected"}
    assert p.tracks == [] and p.status["tracks"] == "idle"     # the old artist's lists are gone
    assert p.bc_tracks == [{"title": "t"}]                     # same Bandcamp page: kept
    book.close()


def test_a_viewed_band_keeps_its_lists_when_the_record_agrees_on_who_it_is():
    book = BandBook([])
    book.seed([_viewed("same", tracks=True)])
    book.seed([_viewed("same", tracks=False)])
    p = book.profiles["girl chow"]
    assert p.tracks == [{"uri": "u"}] and p.bc_tracks and p.status["tracks"] == "done"
    book.close()


def test_a_band_nobody_has_looked_up_is_badged_as_such_not_as_looking():
    from twiddle.scene.app import BADGE
    from twiddle.scenespec.band import UNLOOKED
    p = profiles.from_record(profiles.minimal_record("Nobody", None))
    assert p.confidence == UNLOOKED and BADGE[UNLOOKED][2] == "not looked up yet"


def test_quitting_during_a_long_build_does_not_wait_for_it():
    import time
    started = []

    def long_build(cancelled):
        started.append(1)
        end = time.time() + 20
        while time.time() < end and not cancelled():
            time.sleep(0.05)
        return (-1 if cancelled() else 0), ""

    async def go():
        app = make_app(run_build=long_build)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            await pilot.press("r")
            await settle(pilot, app, lambda: started)
            t0 = time.time()
            await pilot.press("q")
            await pilot.pause(0.1)
        return time.time() - t0
    t0 = time.time()
    asyncio.run(go())
    assert time.time() - t0 < 10


def test_run_builder_leaves_a_build_running_when_cancelled_and_reports_a_failure(tmp_path):
    import os
    import signal
    import sys
    from twiddle.scene.app import BUILD_CANCELLED, run_builder
    pidfile = tmp_path / "pid"
    code = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    calls = []

    def cancelled():
        calls.append(1)
        return pidfile.exists()
    try:
        assert run_builder(cancelled, [sys.executable, "-c", code]) == (BUILD_CANCELLED, "")
        pid = int(pidfile.read_text())
        os.kill(pid, 0)                               # still alive: detached, keeps building
        assert os.getpgid(pid) != os.getpgid(0)       # in its own session
    finally:
        os.kill(int(pidfile.read_text()), signal.SIGKILL)
    bad = "import sys; print('progress'); print('error: no listings', file=sys.stderr); " \
          "print('hint: x', file=sys.stderr); sys.exit(1)"
    assert run_builder(lambda: False, [sys.executable, "-c", bad]) == (1, "error: no listings")
    assert run_builder(lambda: False, [sys.executable, "-c", "print('ok')"]) == (0, "ok")


def test_a_broken_dataset_is_reported_once_not_every_check():
    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            told = []
            app.notify = lambda *a, **k: told.append(a)
            dataset.default_path().write_text("{broken")
            for _ in range(3):
                app._watch_dataset()
            assert len(told) == 1
    run(go())


def test_the_band_on_screen_gets_its_song_lists_and_the_rest_of_the_lineup_does_not(monkeypatch):
    """The dataset knows who each band is but carries no song lists: only the
    band you are looking at pays for them."""
    from twiddle import discover_cli
    from twiddle.scene.book import TrackEnricher
    from twiddle.scenedata.bands import SpotifyEnricher
    asked = []
    monkeypatch.setattr(discover_cli, "_artist_tracks",
                        lambda sess, artist: asked.append(artist["name"]) or TRACKS)
    identity_only = {n: dict(_compiled(n), tracks=[], bc_tracks=[],
                             status={"lookup": "done", "spotify": "done", "bandcamp": "done"})
                     for n in ("Girl Chow", "Wiseacre")}

    async def go():
        app = make_app(bands=identity_only)
        spot = SpotifyEnricher(lambda: object(), tracks=False)
        app.book = BandBook([TrackEnricher(spot, bc_tracks=lambda url: BC_TRACKS)],
                            on_update=app._from_worker_band)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().tracks)
            assert app._profile().bc_tracks == BC_TRACKS and asked == ["Girl Chow"]
            await pilot.pause(0.5)
            assert asked == ["Girl Chow"]                    # Wiseacre: not until selected
            await pilot.press("enter", "j")
            await settle(pilot, app, lambda: "Wiseacre" in asked)
    run(go())


def test_a_checkpoint_that_enriches_the_band_on_screen_brings_its_song_lists(monkeypatch):
    """The swapped-in record has no song lists; nothing waits for you to move
    away and back to fetch them."""
    import os
    from twiddle import discover_cli
    from twiddle.scene.book import TrackEnricher
    from twiddle.scenedata.bands import SpotifyEnricher
    monkeypatch.setattr(discover_cli, "_artist_tracks", lambda sess, a: TRACKS)

    async def go():
        app = make_app()                      # every band starts unenriched
        spot = SpotifyEnricher(lambda: object(), tracks=False)
        app.book = BandBook([TrackEnricher(spot, bc_tracks=lambda url: BC_TRACKS)],
                            on_update=app._from_worker_band)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app.current_band == "Girl Chow")
            assert app._profile().tracks == []
            publish_dataset(bands={"Girl Chow": _identity_only("Girl Chow")}, complete=False)
            os.utime(dataset.default_path(), (1, 1_900_000_002))
            app._watch_dataset()
            await settle(pilot, app, lambda: app._profile().tracks)
            assert app._profile().bc_tracks == BC_TRACKS
            assert app.query_one("#tracks").option_count > 0
    run(go())


def test_a_rebuild_that_changes_only_the_bands_details_redraws_the_open_card():
    """Codex: same status and identity, new bio/genre/links -- the card must follow."""
    import os
    from twiddle import lookup as lk
    base = _identity_only("Girl Chow")
    base["info"] = lk.ArtistInfo(name="Girl Chow", summary="The old bio.").to_dict()

    async def go():
        app = make_app(bands={"Girl Chow": base})
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: "The old bio." in str(app.query_one("#profile").render()))
            newer = dict(base, info=lk.ArtistInfo(name="Girl Chow", summary="A corrected bio.").to_dict())
            publish_dataset(bands={"Girl Chow": newer})
            os.utime(dataset.default_path(), (1, 1_900_000_003))
            app._watch_dataset()
            await settle(pilot, app, lambda: "A corrected bio." in str(app.query_one("#profile").render()))
    run(go())


def test_a_partial_checkpoint_still_corrects_the_artist_it_does_have():
    """Codex: Bandcamp done locally must not veto a record's corrected Spotify
    artist just because the record has not finished Bandcamp yet."""
    from twiddle.scenespec.band import BandProfile
    book = BandBook([])
    viewed = BandProfile(band="Girl Chow")
    viewed.status = {"lookup": "done", "spotify": "done", "bandcamp": "done", "tracks": "done"}
    viewed.spotify_artist, viewed.tracks = {"id": "old"}, [{"uri": "u"}]
    viewed.bandcamp, viewed.bc_tracks = {"item_url_root": "https://x.bandcamp.com"}, [{"title": "t"}]
    book.seed([viewed])
    partial = BandProfile(band="Girl Chow")
    partial.status = {"lookup": "done", "spotify": "done", "bandcamp": "idle", "tracks": "idle"}
    partial.spotify_artist = {"id": "corrected"}
    book.seed([partial])
    p = book.profiles["girl chow"]
    assert p.spotify_artist == {"id": "corrected"} and p.tracks == []      # corrected, old lists gone
    assert p.bandcamp == {"item_url_root": "https://x.bandcamp.com"}       # what only we knew stays
    assert p.bc_tracks == [{"title": "t"}] and p.status["bandcamp"] == "done"
    book.close()


def test_a_failed_song_list_fetch_is_shown_not_hidden(monkeypatch):
    """Codex: identity from the dataset, an expired Spotify sign-in for the songs."""
    from twiddle.scene.book import TrackEnricher
    from twiddle.scenedata.bands import SpotifyEnricher

    def expired():
        raise ConnectionError("network is down")
    spot = SpotifyEnricher(expired, tracks=False)

    async def go():
        app = make_app(bands={"Girl Chow": _identity_only("Girl Chow")})
        app.book = BandBook([TrackEnricher(spot, bc_tracks=lambda u: (_ for _ in ()).throw(
            ConnectionError("network is down")))], on_update=app._from_worker_band)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and
                         app._profile().status.get("tracks", "").startswith("error"))
            await pilot.pause(0.1)
            assert "song lists: network is down" in str(app.query_one("#profile").render())
            assert "song lists failed" in app.query_one("#tracks").border_subtitle
    run(go())


def test_the_tuis_book_does_no_identity_lookup_and_has_one_song_list_fetcher():
    """The client shows who a band is from the dataset: no MusicBrainz, Spotify or
    Bandcamp identity search, only your pin and the song lists."""
    from twiddle.scene import cli

    import inspect
    assert "tui_book" in inspect.getsource(cli.cmd_scene)
    src = inspect.getsource(cli.tui_book)
    assert "PinEnricher" in src and "TrackEnricher" in src
    for identity in ("LookupEnricher", "SpotifyEnricher", "BandcampEnricher", "scenedata"):
        assert identity not in src


def test_a_pin_through_the_production_book_fetches_the_chosen_artist_and_its_songs(monkeypatch):
    """The whole client-side pin flow: the dataset picked artist `a`, you pinned
    `b`; opening the band fetches `b` by id, then its songs, graded by your choice."""
    from twiddle import discover_cli
    from twiddle.scene.cli import tui_book
    cache.pin("Girl Chow", "b", "Girl Chow (the other one)")
    fetched = []

    class Sess:
        def request(self, method, path):
            fetched.append(path)
            return {"id": path.rsplit("/", 1)[-1], "name": "Girl Chow"}

    monkeypatch.setattr(discover_cli, "_artist_tracks",
                        lambda sess, artist: [dict(TRACKS[0], uri=f"spotify:track:{artist['id']}")])

    async def go():
        app = make_app(bands={"Girl Chow": _identity_only("Girl Chow")})      # dataset's pick: "a"
        app.book = tui_book(app._from_worker_band, session_factory=Sess, bc_tracks=lambda u: BC_TRACKS)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().tracks)
            p = app._profile()
            assert fetched == ["/artists/b"]
            assert p.spotify_artist["id"] == "b" and p.tracks[0]["uri"] == "spotify:track:b"
            assert p.confidence == "corroborated" and p.why == "chosen by you"
    run(go())


def test_a_band_nobody_has_looked_up_does_not_claim_the_databases_lack_them():
    async def go():
        app = make_app(bands={"Girl Chow": profiles.minimal_record("Girl Chow", None)})
        app.book = BandBook([], on_update=app._from_worker_band)      # nothing runs: dataset only
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() is not None)
            card = str(app.query_one("#profile").render())
            assert "not in MusicBrainz" not in card
            assert "next build looks them up" in card
    run(go())


def test_a_checkpoint_that_changes_genres_updates_which_shows_a_genre_filter_matches():
    """Codex: same shows, new genre records -- the filtered rows must follow."""
    import os
    guess = profiles.guess_to_dict(genre_mod.for_band(GIRL_CHOW_BC[0], sure=True))
    coup = profiles.guess_to_dict(genre_mod.for_band({"genre_name": "Metal", "tag_names": ["metal"],
                                                      "location": "Oakland, California"}, sure=True))

    async def go():
        app = make_app(bands={"Girl Chow": dict(profiles.minimal_record("Girl Chow", None), genre=coup)})
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 2)
            app.filter_text = "reggae"
            app._refresh_shows()
            await settle(pilot, app, lambda: len(app._rows) == 0)
            publish_dataset(bands={"Girl Chow": dict(profiles.minimal_record("Girl Chow", None),
                                                     genre=guess)}, complete=False)
            os.utime(dataset.default_path(), (1, 1_900_000_004))
            app._watch_dataset()
            await settle(pilot, app, lambda: len(app._rows) == 1)     # Girl Chow is reggae now
    run(go())


# ---- volume, and back to what was playing -------------------------------------

RELAY_DEV = {"id": "rid", "name": "relay", "relay": True, "local": False}
STATION = {"relay": False, "snapshot": "radio, 43"}          # what a room on a station captures
ON_RELAY = {"relay": True, "snapshot": "spotify via the relay, 43"}


def _preview_on_the_roam(outs, player=None):
    """An app on the relay device with a Bandcamp track playing on the Roam."""
    return _bandcamp_app(RELAY_DEV, outs, player)


def test_volume_keys_steer_the_output_a_bandcamp_track_plays_on():
    async def go():
        outs = FakeUrlOutputs()
        app = _preview_on_the_roam(outs)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            await pilot.press("plus")
            await settle(pilot, app, lambda: outs.roam.volumes)
            assert outs.roam.volumes == [32]                       # read 30, then +2
            await pilot.press("right_square_bracket")
            await settle(pilot, app, lambda: len(outs.roam.volumes) == 2)
            assert outs.roam.volumes[-1] == 37 and app.volume == 37
            await settle(pilot, app, lambda: "37" in app.query_one("#nowbar").render().plain)
    run(go())


def test_the_volume_keys_say_so_when_spotify_plays_on_this_mac():
    async def go():
        outs = FakeUrlOutputs()
        app = _bandcamp_app(MAC_DEV, outs, FakePlayer())
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("minus")
            await pilot.pause(0.2)
            assert outs.mac.volumes == [] and outs.roam.volumes == [] and app.volume is None
    run(go())


def test_R_puts_the_roam_back_on_the_station_it_was_on_and_stops_the_preview():
    async def go():
        outs = FakeUrlOutputs()
        outs.roam.found = STATION
        app = _preview_on_the_roam(outs, FakePlayer())
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            assert outs.roam.captured == 1
            await pilot.press("n")                              # a second preview: not "before"
            await settle(pilot, app, lambda: len(outs.roam.played) == 2)
            assert outs.roam.captured == 1 and app._before["room:roam"]["token"] is STATION
            await pilot.press("R")
            await settle(pilot, app, lambda: outs.roam.restored)
            assert outs.roam.restored == [(STATION, False)] and outs.roam.stopped >= 1
            assert not app._before and app.bc_now is None
    run(go())


def test_R_leaves_a_room_alone_that_someone_retuned_meanwhile():
    async def go():
        outs = FakeUrlOutputs()
        outs.roam.found = STATION
        app = _preview_on_the_roam(outs, FakePlayer())
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            outs.roam.playing, outs.roam.elsewhere = False, "http://kexp.example/stream"   # theirs, paused
            await pilot.press("R")
            await settle(pilot, app, lambda: not app._before)
            assert outs.roam.restored == []
    run(go())


def test_R_on_a_relay_room_still_hands_back_through_spotify_and_restores_the_volume():
    async def go():
        outs = FakeUrlOutputs()
        outs.roam.found = ON_RELAY
        player = FakePlayer()
        app = _preview_on_the_roam(outs, player)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            await pilot.press("R")
            await settle(pilot, app, lambda: outs.roam.restored)
            assert outs.roam.restored == [(ON_RELAY, True)]       # volume only
            assert "back" in player.played                         # Spotify's handback did the stream
    run(go())


def test_a_preview_on_the_mac_does_not_stop_the_roam_being_remembered():
    # Codex: one session-wide "before" meant a Mac preview hid the Roam's station
    async def go():
        outs = FakeUrlOutputs()
        outs.roam.found = STATION
        app = _bandcamp_app(MAC_DEV, outs, FakePlayer())
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")                                  # a preview on this Mac
            await settle(pilot, app, lambda: outs.mac.played)
            assert outs.roam.captured == 0 and app._before["mac"]["token"] is None
            app.device = Device(id="rid", name="relay", relay=True)  # now the Roam
            app._play_bandcamp(app._profile(), 1, confirmed=True)   # a handoff while a track plays
            await settle(pilot, app, lambda: outs.roam.played)
            assert outs.roam.captured == 1 and app._before["room:roam"]["token"] is STATION
    run(go())


def test_a_known_room_s_map_credit_is_shown_with_its_address():
    from twiddle.scene.app import VenueScreen
    from twiddle.scenespec import venue as venue_mod
    from textual.widgets import Static
    (v,) = venue_mod.from_rows([{"id": "x", "name": "Hopmonk", "address": "224 Vintage Way, Novato",
                                 "city": "Novato", "attribution": "Location © OpenStreetMap contributors (ODbL)"}])

    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            app.push_screen(VenueScreen("Hopmonk", v.info))
            await settle(pilot, app, lambda: app.screen.__class__.__name__ == "VenueScreen")
            text = " ".join(str(w.render()) for w in app.screen.query(Static))
            assert "224 Vintage Way" in text and "OpenStreetMap contributors" in text
    run(go())


def test_a_preview_that_was_refused_leaves_nothing_captured_for_the_next_one():
    # Codex: the capture outlived a cancelled first preview, so after the user re-tuned the
    # room, `R` put back the old station instead of what the real preview interrupted
    async def go():
        outs = FakeUrlOutputs(confirm_first=True)
        outs.roam.found = STATION
        app = _preview_on_the_roam(outs, FakePlayer())
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")                             # asks first (Spotify on the relay)
            await settle(pilot, app, lambda: app._pending)
            assert outs.roam.captured == 1 and not app._before    # refused: not kept
            outs.roam.found = {"relay": False, "snapshot": "the station it is on now"}
            await pilot.press("p")                             # confirmed
            await settle(pilot, app, lambda: outs.roam.played)
            assert outs.roam.captured == 2
            assert app._before["room:roam"]["token"]["snapshot"] == "the station it is on now"
    run(go())


def test_the_volume_keys_and_the_snapshot_target_the_room_the_relay_really_feeds():
    # Codex: a relay started with --room "Living Room" while scene's default room is roam
    class LivingRoomPlayer(FakePlayer):
        relay_room = "Living Room"
        room = "roam"

    async def go():
        outs = FakeUrlOutputs()
        app = _bandcamp_app(RELAY_DEV, outs, LivingRoomPlayer())
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            assert app._bandcamp_output(app.device) == "room:Living Room"
    run(go())


def test_a_rebuild_that_changes_the_venue_table_but_not_the_shows_rebuilds_the_sidebar():
    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.3)
            app._watched_given = False
            snap = app.snapshot
            same = dataset.Snapshot(path=snap.path, version=snap.version, generated_at=snap.generated_at,
                                    shows=list(app.shows), show_ids=snap.show_ids, bands=snap.bands,
                                    venues=[{"id": "a", "name": "Room A", "match": ["room a"]}])
            calls = []
            real = app._set_shows
            app._set_shows = lambda *a, **k: calls.append(a) or real(*a, **k)
            app.load_dataset = lambda: same
            app._reload()                       # a first, different table: the sidebar is rebuilt
            assert len(calls) == 1
            app._reload()                       # nothing changed: only band records are refreshed
            assert len(calls) == 1
            app.load_dataset = lambda: dataset.Snapshot(
                path=snap.path, version=snap.version, generated_at=snap.generated_at, shows=list(app.shows),
                show_ids=snap.show_ids, bands=snap.bands,
                venues=[{"id": "b", "name": "Room B", "match": ["room b"]}])
            app._reload()                       # A replaced by B with identical shows
            assert len(calls) == 2
    run(go())


def test_R_leaves_alone_a_different_spotify_track_or_bandcamp_url_on_the_room():
    # Codex: "the relay" or "a bcbits URL" is not "the preview scene started"
    from twiddle.dial.output import RELAY, OutputState

    def left_alone(tuned, other, np_playing, np_uri):
        async def go():
            outs = FakeUrlOutputs()
            outs.roam.found = STATION

            class Player(FakePlayer):
                def now(self):
                    return {"playing": np_playing, "uri": np_uri}
            app = _preview_on_the_roam(outs, Player())
            async with app.run_test(size=(160, 45)) as pilot:
                await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
                await pilot.press("p")
                await settle(pilot, app, lambda: outs.roam.played)
                outs.roam.state = lambda: OutputState(tuned=tuned, playing=True, uri=other or "x", other=other)
                await pilot.press("R")
                await settle(pilot, app, lambda: not app._before)
                assert outs.roam.restored == []
        run(go())
    left_alone(RELAY, "Spotify (relay)", True, "spotify:track:THEIRS")          # someone else's Spotify track
    left_alone(None, "https://t4.bcbits.com/stream/someone-elses", False, "")   # another Bandcamp stream


def test_R_after_switching_the_device_away_still_restores_the_room_that_was_previewed():
    # Codex: with This Mac or a phone chosen, `_volume_oid` is None and R fell back to Spotify's handback
    async def go():
        outs = FakeUrlOutputs()
        outs.roam.found = STATION
        app = _preview_on_the_roam(outs, FakePlayer())
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            app.bc_now = None
            app.device = Device(id="mac", name="Mac", local=True)      # the user picked This Mac
            assert app._volume_oid() is None
            await pilot.press("R")
            await settle(pilot, app, lambda: outs.roam.restored)
            assert outs.roam.restored == [(STATION, False)]
    run(go())


def test_a_different_paused_spotify_track_on_the_relay_is_not_ours():
    # Codex: "not playing" was read as idle; Spotify keeps the paused track's URI
    from twiddle.dial.output import RELAY, OutputState

    async def go():
        outs = FakeUrlOutputs()
        outs.roam.found = STATION

        class Player(FakePlayer):
            def now(self):
                return {"playing": False, "uri": "spotify:track:THEIRS"}     # paused, and not ours
        app = _preview_on_the_roam(outs, Player())
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            outs.roam.state = lambda: OutputState(tuned=RELAY, playing=False, uri="relay", other="Spotify (relay)")
            await pilot.press("R")
            await settle(pilot, app, lambda: not app._before)
            assert outs.roam.restored == []
    run(go())


def test_the_next_track_of_scene_s_own_spotify_preview_is_still_ours():
    # Codex: only the first URI was remembered, so `n` or the queue advancing made R give up the room
    from twiddle.dial.output import RELAY, OutputState

    async def go():
        outs = FakeUrlOutputs()
        outs.roam.found = STATION
        app = _bandcamp_app(RELAY_DEV, outs, None)

        class Player(FakePlayer):
            def now(self):
                return {"playing": True, "uri": "spotify:track:1"}           # the second of ours
        app.player = Player()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().tracks)
            tracks = app.query_one("#tracks")
            tracks.focus()
            tracks.highlighted = tracks.get_option_index("sp:0")
            await pilot.press("p")
            await settle(pilot, app, lambda: app.player.played)
            assert {"spotify:track:0", "spotify:track:1", "spotify:track:2"} <= app._last_preview["spotify"]
            outs.roam.state = lambda: OutputState(tuned=RELAY, playing=True, uri="relay", other="Spotify (relay)")
            await pilot.press("R")
            await settle(pilot, app, lambda: outs.roam.restored)
            assert outs.roam.restored == [(STATION, False)]
    run(go())


def test_R_after_switching_away_still_restores_the_volume_of_a_room_that_was_on_the_relay():
    async def go():
        outs = FakeUrlOutputs()
        outs.roam.found = ON_RELAY
        player = FakePlayer()
        app = _preview_on_the_roam(outs, player)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            app.bc_now = None
            app.device = Device(id="mac", name="Mac", local=True)
            await pilot.press("R")
            await settle(pilot, app, lambda: outs.roam.restored)
            assert outs.roam.restored == [(ON_RELAY, True)] and "back" in player.played
    run(go())


def test_the_volume_is_read_again_when_it_may_have_changed_underneath_us():
    # Codex: a cached 20 followed by R restoring 43 made `+` write 22 instead of 45
    import twiddle.scene.app as scene_app

    async def go():
        outs = FakeUrlOutputs()
        app = _preview_on_the_roam(outs)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            outs.roam.volume = 20
            await pilot.press("plus")
            await settle(pilot, app, lambda: outs.roam.volumes == [22])
            outs.roam.volume = 43                       # R, or the Sonos app, changed it
            app._vol_read_at -= scene_app.VOLUME_STALE_S + 1
            await pilot.press("plus")
            await settle(pilot, app, lambda: len(outs.roam.volumes) == 2)
            assert outs.roam.volumes[-1] == 45          # from what it is now, not from the stale 22
    run(go())


def test_R_leaves_the_room_ours_when_scene_moved_Spotify_to_the_mac_after_previewing_on_the_roam():
    from twiddle.dial.output import RELAY, OutputState

    async def go():
        outs = FakeUrlOutputs()
        outs.roam.found = STATION

        class Player(FakePlayer):
            relay_name = "Sonos Roam Relay"

            def now(self):
                return {"playing": True, "uri": "spotify:track:MAC", "device": "Kenny's MacBook"}
        app = _preview_on_the_roam(outs, Player())
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            outs.roam.state = lambda: OutputState(tuned=RELAY, playing=False, uri="relay", other="Spotify (relay)")
            await pilot.press("R")
            await settle(pilot, app, lambda: outs.roam.restored)
            assert outs.roam.restored == [(STATION, False)]
    run(go())


def test_volume_presses_made_while_the_first_read_is_out_all_count():
    # Codex: each press started its own read with only its own step, and all but one were dropped
    async def go():
        outs = FakeUrlOutputs()
        app = _preview_on_the_roam(outs)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._profile() and app._profile().bc_tracks)
            await pilot.press("p")
            await settle(pilot, app, lambda: outs.roam.played)
            outs.roam.volume = 30
            await pilot.press("plus", "plus", "right_square_bracket")        # 2 + 2 + 5, before the read lands
            await settle(pilot, app, lambda: outs.roam.volumes and outs.roam.volumes[-1] == 39)
            assert outs.roam.volumes[-1] == 39 and app.volume == 39
    run(go())
