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
from twiddle.scene.app import SceneApp
from twiddle.scene.bands import BandBook, BandcampEnricher, LookupEnricher
from twiddle.scene.model import Show
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


class FakeSource:
    name = "fake"

    def fetch(self):
        return SHOWS


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


def make_app(player=None, bandcamp=None, **kw):
    """`bandcamp`: {band: [search results]} -- enables the Bandcamp enricher
    and the genre scan over those; without it, Bandcamp knows nobody."""
    known = bandcamp or {}

    def search(name, offline=False):
        return known.get(name, [])

    def book_factory(on_update):
        enrichers = [LookupEnricher(lambda n: lookup.Result()), FakeSpotify()]
        if bandcamp is not None:
            enrichers.append(BandcampEnricher(search=search, tracks=lambda url: BC_TRACKS))
        return BandBook(enrichers, on_update=on_update)
    kw.setdefault("load_image", lambda url: None)      # no network for pictures
    kw.setdefault("band_photo", lambda p: (None, ""))
    kw.setdefault("instagram_picture", lambda handle: None)   # nor for venues
    kw.setdefault("genre_search", search)
    kw.setdefault("bandcamp_stream", lambda t: f"https://t4.bcbits.com/stream/{t['title']}")
    return SceneApp(sources=[FakeSource()], book_factory=book_factory,
                    player=player or FakePlayer(), fetch_on_mount=True, today=TODAY, **kw)


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

    class Src:
        name = "src"

        def fetch(self):
            return shows

    async def go():
        app = make_app()
        app.sources = [Src()]
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

    class Src:
        name = "src"

        def fetch(self):
            return SHOWS + later

    async def go():
        app = make_app()
        app.sources = [Src()]
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: len(app._rows) == 3)   # 2 this week + 20 days out
            await pilot.press("t")                                  # All upcoming
            await settle(pilot, app, lambda: len(app._rows) == 4)
            await pilot.press("a")
            await settle(pilot, app, lambda: len(app._rows) == 5)
        assert cache.get_state("window") == "all"
        assert cache.get_state("all_venues") is True
        again = make_app()
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
    from twiddle.scene import venues
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
    from twiddle.scene import venues
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
    def __init__(self, label, confirm_first=False):
        self.label = label
        self.played = []
        self.stopped = 0
        self.playing = False
        self.confirm_first = confirm_first

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
        return OutputState(playing=self.playing,
                           uri=self.played[-1][0] if self.played else "")

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


def test_genre_column_fills_from_bandcamp_and_slash_filters_by_it():
    async def go():
        app = make_app(bandcamp={"Girl Chow": GIRL_CHOW_BC})
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app._rows)
            table = app.query_one("#shows")
            await settle(pilot, app, lambda: "reggae" in str(table.get_cell("r0", "genre")))
            assert str(table.get_cell("r1", "genre")) == ""     # nobody knows Coup Dville
            await pilot.press("slash", *"reggae")
            await settle(pilot, app, lambda: len(app._rows) == 1)
            assert app._rows["r0"].headliner == "Girl Chow"
    run(go())


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


def test_genre_scan_asks_the_network_only_about_your_venues_this_month():
    """`v` shows ~150 rooms and `t` five months: thousands of requests to
    an endpoint that isn't ours. Those come from the cache only."""
    asked = []

    def search(name, offline=False):
        if offline:
            return None
        asked.append(name)
        return []

    async def go():
        app = make_app(genre_search=search)
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: set(asked) >= {"Girl Chow", "Coup Dville"})
            await pilot.press("a")                      # + "Somewhere Else"
            await settle(pilot, app, lambda: len(app._rows) == 3)
            await pilot.pause(0.3)
            assert "Nobody" not in asked
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


def test_a_venue_with_an_article_shows_its_summary(monkeypatch):
    monkeypatch.setattr("twiddle.scene.app.wiki_summary",
                        lambda title: {"extract": f"About {title}.", "url": "u"})
    fox = [Show(date(2026, 9, 23), "Fox Theater, Oakland", ["Somebody"])]
    monkeypatch.setattr(FakeSource, "fetch", lambda self: fox)

    async def go():
        app = make_app()
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
    monkeypatch.setattr(FakeSource, "fetch", lambda self: listed)

    async def go():
        app = make_app()
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
    monkeypatch.setattr(FakeSource, "fetch", lambda self: nights)

    async def go():
        app = make_app(load_image=lambda url: {"https://f/a": a, "https://f/b": b}.get(url))
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
    monkeypatch.setattr(FakeSource, "fetch", lambda self: night)

    async def go():
        app = make_app()
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app, lambda: app.current_show)
            table = app.query_one("#shows")
            assert "Freakyoke" in str(table.get_row_at(0)[-1])
            assert "Freakyoke" in str(app.query_one("#profile").render())
            assert app.current_band is None and not app.book.profiles
    run(go())


def test_a_venue_without_a_logo_gets_its_instagram_picture(monkeypatch):
    from twiddle.scene import venues
    from twiddle.scene.venue_info import VenueInfo
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
    from twiddle.scene import venues
    from twiddle.scene.venue_info import VenueInfo
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
