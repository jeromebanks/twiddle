"""Station now-playing parsing, against captured page shapes -- no network."""
import html
import json

import pytest

from twiddle import stations
from twiddle.stations import titles
from twiddle.stations.fetchers import icy as icy_fetch
from twiddle.stations.fetchers import kqed as kqed_mod


def test_icy_title_keeps_apostrophes_inside_the_title():
    meta = "StreamTitle='Johnny's Theme - Band';StreamUrl='';"
    assert stations.parse_icy(meta) == "Johnny's Theme - Band"


def test_icy_blank_title_is_none():
    assert stations.parse_icy("StreamTitle='';") is None


def test_icy_title_stops_at_any_following_field():
    # NTS sends a json= field after the title, not StreamUrl=.
    assert stations.parse_icy("StreamTitle='NTS 2 - Rumpshakers';json='{}';") == "NTS 2 - Rumpshakers"


@pytest.mark.parametrize("title,expected", [
    ("Neutral Milk Hotel - Holland, 1945", ("Neutral Milk Hotel", "Holland, 1945")),
    # WFMU's filler text has no " - ", and must never become an "artist"
    ('Your DJ speaks over "X" on Bucci\'s show on WFMU', (None, None)),
    (None, (None, None)),
    ("Artist - ", ("Artist", None)),
])
def test_split_title_only_trusts_the_artist_dash_song_shape(title, expected):
    assert stations.split_title(title) == expected


SPINITRON_PAGE = """
<div class="spin" data-spin="{spin}">...</div>
<h3 class="show-title">
  <a href="/KALX/show/1">queen of the cowbell.</a></h3>
<p class="dj-name"><a href="/KALX/dj/1">alisa</a>, <a href="/KALX/dj/2">b&amp;c</a></p>
""".format(spin=html.escape(json.dumps(
    {"i": "X", "a": "(T-T)b", "s": "Sophie", "r": "Beautiful Extension Cord"})))


def test_spinitron_page_yields_song_album_show_and_every_dj():
    got = stations.parse_spinitron(SPINITRON_PAGE)
    assert got == {"artist": "(T-T)b", "song": "Sophie",
                   "album": "Beautiful Extension Cord",
                   "show": "queen of the cowbell.", "hosts": ["alisa", "b&c"]}


def test_spinitron_without_a_spin_falls_back_to_icy(monkeypatch):
    monkeypatch.setattr(stations.net, "get", lambda url, headers=None: b"<html></html>")
    monkeypatch.setattr(stations.icy, "icy_title", lambda url: "Band - Song")
    np = stations.STATIONS["kalx"].now_playing()
    assert (np.artist, np.song) == ("Band", "Song")


def test_kexp_passes_musicbrainz_ids_through(monkeypatch):
    play = {"results": [{"artist": "Björk", "song": "Immature", "album": "Homogenic",
                         "artist_ids": ["mb-artist"], "release_group_id": "mb-rg",
                         "show_uri": "https://api.kexp.org/v2/shows/1/",
                         "play_type": "trackplay"}]}
    show = {"program_name": "The Afternoon Show", "host_names": ["Larry"]}
    monkeypatch.setattr(stations.net, "get_json",
                        lambda url: show if "/shows/" in url else play)
    np = stations.STATIONS["kexp"].now_playing()
    assert (np.mb_artist_id, np.mb_release_group_id) == ("mb-artist", "mb-rg")
    assert "Björk - Immature  [Homogenic]" in np.render()
    assert "The Afternoon Show with Larry" in np.render()


def _kqed_page(slots: list[dict]) -> str:
    # The shape kqed.org/radio/schedule embeds in its initial state.
    state = {"blockName": "kqed/radio-schedule",
             "schedule": {"type": "radio-schedules", "id": "2026-09-25", "schedule": slots}}
    return f"<html><script>window.__INITIAL_STATE__ = {json.dumps(state, indent=2)}</script>"


KQED_SLOTS = [
    {"programTitle": "Morning Edition", "episodeTitle": "Xi Goes to Washington",
     "episodeHost": None, "startTime": 1000, "endTime": 2000},
    {"programTitle": "Forum", "episodeTitle": "How Gaza Policy Reverberates",
     "episodeHost": "Mina Kim", "startTime": 2000, "endTime": 3000,
     "imageSrc": "https://cdn.kqed.org/forum.jpg"},
    {"programTitle": "PBS NewsHour", "episodeTitle": None,
     "episodeHost": None, "startTime": 3000, "endTime": 4000},
]


@pytest.fixture
def kqed(monkeypatch):
    """KQED at a chosen moment, against a canned schedule page."""
    monkeypatch.setattr(kqed_mod, "_cache", {"at": 0.0, "slots": []})
    fetches = []

    def at(now, page=_kqed_page(KQED_SLOTS)):
        monkeypatch.setattr(kqed_mod.time, "time", lambda: now)
        monkeypatch.setattr(stations.net, "get", lambda url, headers=None: (
            fetches.append(url), page.encode())[1])
        return stations.STATIONS["kqed"].now_playing()
    at.fetches = fetches
    return at


def test_kqed_names_the_show_and_episode_from_its_schedule(kqed):
    np = kqed(2500)
    assert (np.show, np.raw_title, np.hosts) == ("Forum", "How Gaza Policy Reverberates",
                                                 ["Mina Kim"])
    assert np.artist is None and np.art_url == "https://cdn.kqed.org/forum.jpg"
    assert [(r["show"], r.get("on_now", False)) for r in np.schedule] == [
        ("Morning Edition", False), ("Forum", True), ("PBS NewsHour", False)]
    assert np.schedule[1]["host"] == "Mina Kim" and "episode" not in np.schedule[2]


def test_kqed_slot_without_an_episode_is_just_the_show(kqed):
    np = kqed(3500)
    assert (np.raw_title, np.show) == ("PBS NewsHour", None)


def test_kqed_schedule_is_fetched_once_per_ttl_not_per_poll(kqed):
    kqed(1990)
    assert kqed(2010).show == "Forum"  # a new slot, but inside the same day's page
    assert len(kqed.fetches) == 1


def test_kqed_without_a_schedule_falls_back_to_icy_and_never_an_artist(kqed, monkeypatch):
    monkeypatch.setattr(stations.icy, "icy_title", lambda url: "Forum - Housing Policy")
    np = kqed(2500, page="<html>redesigned</html>")
    assert np.artist is None
    assert "Forum - Housing Policy" in np.render()


def test_remember_round_trips_and_is_isolated_by_conftest():
    assert stations.last_source() is None
    stations.remember("kexp")
    assert stations.last_source() == "kexp"


def _spin_row(spin: dict, time: str, art: str) -> str:
    return (f'<tr class="spin-item" data-spin="{html.escape(json.dumps(spin))}">'
            f'<td class="spin-time"><a href="/x">{time}</a></td>'
            f'<td class="spin-art"><div><img class="a" src="{art}" alt="x"></div></td></tr>')


SPINS_PAGE = "".join([
    _spin_row({"a": "Low", "s": "Words", "r": "I Could Live"}, "11:17 PM",
              "https://is1-ssl.mzstatic.com/image/thumb/x/190295.jpg/150x150bb.jpg"),
    _spin_row({"a": "Broadcast", "s": "Echo's Answer"}, "11:10 PM",
              "https://spinitron.com/static/pictures/placeholders/loudspeaker.svg"),
])


def test_spinitron_spins_carry_time_and_a_drawable_cover():
    spins = stations.parse_spinitron_spins(SPINS_PAGE)
    assert spins[0] == {"time": "11:17 PM", "artist": "Low", "song": "Words",
                        "album": "I Could Live",
                        "art_url": "https://is1-ssl.mzstatic.com/image/thumb/x/190295.jpg/600x600bb.jpg"}
    assert "art_url" not in spins[1]            # the loudspeaker placeholder is no cover


def test_spinitron_cover_is_only_trusted_when_the_first_row_is_the_current_song(monkeypatch):
    current = '<div data-spin="{}"></div>'.format(html.escape(json.dumps(
        {"a": "Low", "s": "Words", "r": "I Could Live"})))
    monkeypatch.setattr(stations.net, "get", lambda url, headers=None: (current + SPINS_PAGE).encode())
    np = stations.STATIONS["kalx"].now_playing()
    assert np.art_url.endswith("/600x600bb.jpg")
    assert [r["artist"] for r in np.recent] == ["Broadcast"]

    other = current.replace("Words", "Lullaby")
    monkeypatch.setattr(stations.net, "get", lambda url, headers=None: (other + SPINS_PAGE).encode())
    np = stations.STATIONS["kalx"].now_playing()
    assert np.art_url is None
    assert [r["artist"] for r in np.recent] == ["Low", "Broadcast"]


def test_radiofrance_picks_the_song_on_air_and_lists_the_ones_before():
    data = {"steps": {
        "a": {"embedType": "song", "start": 100, "end": 200, "title": "Old", "authors": "A",
              "titreAlbum": "LP1", "visual": "http://img/a.jpg"},
        "b": {"embedType": "song", "start": 200, "end": 300, "title": "Now", "authors": "",
              "highlightedArtists": ["B"], "visual": "http://img/b.jpg"},
        "c": {"embedType": "song", "start": 300, "end": 400, "title": "Next", "authors": "C"},
        "show": {"embedType": "expression", "start": 0, "end": 999, "title": "a show"}}}
    np = stations.parse_radiofrance(data, now=250)
    assert (np.artist, np.song, np.art_url) == ("B", "Now", "http://img/b.jpg")
    assert [r["song"] for r in np.recent] == ["Old"]          # never the one still to come
    between = stations.parse_radiofrance(data, now=450)       # after the last: latest started
    assert between.song == "Next"


def test_somafm_history_gives_album_and_recent(monkeypatch):
    songs = {"songs": [{"title": "Eva", "artist": "Heights of Abraham", "album": "Electric Hush",
                        "albumArt": "", "date": "1790322467"},
                       {"title": "My Face", "artist": "Freshmoods", "album": "Exhale",
                        "albumArt": "", "date": "1790322111"}]}
    monkeypatch.setattr(stations.net, "get_json",
                        lambda url: songs if url.endswith("/songs/groovesalad.json") else {})
    np = stations.STATIONS["groovesalad"].now_playing()
    assert (np.artist, np.song, np.album, np.art_url) == \
        ("Heights of Abraham", "Eva", "Electric Hush", None)
    assert np.recent[0]["artist"] == "Freshmoods" and "art_url" not in np.recent[0]


def test_nts_names_the_show_never_an_artist(monkeypatch):
    live = {"results": [{"channel_name": "2", "now": {"broadcast_title": "Wrong channel"}},
                        {"channel_name": "1", "now": {
                            "broadcast_title": "The Early Bird Show W/ Jack &amp; Jill",
                            "embeds": {"details": {"genres": [{"value": "Folk"}, {"value": "Dub"}],
                                                   "media": {"picture_large": "http://p.jpg"}}}}}]}
    monkeypatch.setattr(stations.net, "get_json", lambda url: live)
    np = stations.STATIONS["nts"].now_playing()
    assert np.artist is None
    assert np.raw_title == "The Early Bird Show W/ Jack & Jill"
    assert (np.note, np.art_url) == ("(Folk, Dub)", "http://p.jpg")


def test_wmbr_show_and_host_from_its_dynamic_xml(monkeypatch):
    inner = ('<b><a href="/cgi-bin/show?id=1" target="_blank">Dust Bin</a></b>'
             '<div><br>with Adam Y</div>')
    page = f"<wmbr_dynamic><wmbr_show>{html.escape(inner)}</wmbr_show></wmbr_dynamic>"
    monkeypatch.setattr(stations.net, "get", lambda url, headers=None: page.encode())
    np = stations.STATIONS["wmbr"].now_playing()
    assert (np.artist, np.raw_title, np.note) == (None, "Dust Bin", "(with Adam Y)")


def test_stations_added_for_sonos_are_plain_http():
    # play_radio turns a URL into x-rincon-mp3radio://, which a Sonos fetches
    # over http -- an https-only stream would play on the Mac and not the Roam.
    for key in ("kzsc", "whrb", "wmbr", "groovesalad", "beatblender", "thetrip",
                "fip", "dublab", "nts", "bbc", "wnyc", "shonanbeach", "ottava", "fmsetagaya",
                "rad", "rainwavegame", "slay", "retropc", "bigbkpop"):
        assert stations.STATIONS[key].url.startswith("http://"), key


def test_iheart_titles_become_artist_dash_song():
    raw = 'title="Ted And The 20 Person Plane",artist="JOHN MULANEY",url="song_spot=\\"T\\""'
    assert stations.tidy_title(raw) == "JOHN MULANEY - Ted And The 20 Person Plane"
    assert stations.split_title(stations.tidy_title(raw)) == (
        "JOHN MULANEY", "Ted And The 20 Person Plane")
    assert stations.tidy_title("George Carlin - Track 12") == "George Carlin - Track 12"
    assert stations.tidy_title(None) is None


def test_rain_is_never_taken_for_an_artist(monkeypatch):
    monkeypatch.setattr(stations.icy, "icy_title",
                        lambda url: "Rain Sounds & White Noise - Loopable Rain")
    np = stations.STATIONS["rain"].now_playing()
    assert np.artist is None and np.raw_title == "Rain Sounds & White Noise - Loopable Rain"



# ---- title shapes ----------------------------------------------------------

IHEART = 'title="Ted And The 20 Person Plane",artist="JOHN MULANEY",url="song_spot=\\"T\\""'


def test_artist_song_shape_needs_the_dash():
    assert titles.parse("artist-song", "Neutral Milk Hotel - Holland, 1945") == {
        "artist": "Neutral Milk Hotel", "song": "Holland, 1945"}
    assert titles.parse("artist-song", "Morning Becomes Eclectic") == {}
    # Named outright, artist-song doesn't read iHeart attributes.
    assert titles.parse("artist-song", IHEART) == {}


def test_iheart_shape_is_only_for_iheart_attributes():
    assert titles.parse("iheart-attrs", IHEART) == {
        "artist": "JOHN MULANEY", "song": "Ted And The 20 Person Plane"}
    assert titles.parse("iheart-attrs", "George Carlin - Track 12") == {}


def test_iheart_station_still_gets_comedian_dash_track(monkeypatch):
    monkeypatch.setattr(stations.icy, "icy_title", lambda url: IHEART)
    np = stations.STATIONS["comedy247"].now_playing()
    assert (np.artist, np.song) == ("JOHN MULANEY", "Ted And The 20 Person Plane")
    assert np.raw_title == "JOHN MULANEY - Ted And The 20 Person Plane"


def _old_icy(title, music):
    """What the icy fetcher did before title shapes, frozen here so the
    new path can be held to it: tidy_title, then split_title."""
    import re
    if title:
        fields = dict(re.findall(r'(\w+)="(.*?)"(?:,|$)', title))
        if fields.get("artist") and fields.get("title"):
            title = f"{fields['artist']} - {fields['title']}"
    artist = song = None
    if music and title and " - " in title:
        artist, song = (s.strip() or None for s in title.split(" - ", 1))
    return artist, song, title


# Fetchers that read the ICY title, with the default shape, when their own
# feed has nothing, and the ones of them (with talk) that never take it for
# an artist. wfmu reads ICY with its own shape (`titles.wfmu`, tested below);
# every other fetcher never looks at ICY.
ICY_FALLBACKS = ("talk", "spinitron", "wmbr", "kqed")
NEVER_MUSIC = ("talk", "wmbr", "kqed")


def _icy_stations():
    from pathlib import Path
    import tomllib
    for path in sorted((Path(stations.__file__).parent / "catalog").glob("*.toml")):
        data = tomllib.loads(path.read_text())
        if data.get("fetch", "icy") in ("icy", *ICY_FALLBACKS):
            yield path.stem, data


ICY_STATIONS = list(_icy_stations())


@pytest.mark.parametrize("title", [
    "Neutral Milk Hotel - Holland, 1945",
    IHEART,
    # An iHeart artist with a dash in it has always split at the first dash.
    'title="Song",artist="A - B",url=""',
    # Blank attributes split to nothing, never to a split of the url.
    'title=" ",artist=" ",url="Promo - Break"',
    'Your DJ speaks over "X" on Bucci\'s show on WFMU',
    None,
])
@pytest.mark.parametrize("key,data", ICY_STATIONS, ids=[k for k, _ in ICY_STATIONS])
def test_every_icy_station_reads_its_title_as_before(monkeypatch, key, data, title):
    monkeypatch.setattr(stations.icy, "icy_title", lambda url, encoding="utf-8": title)
    # Their own feeds come back empty, so each falls back to the ICY title.
    monkeypatch.setattr(stations.net, "get", lambda url, headers=None: b"")
    monkeypatch.setattr(kqed_mod, "_cache", {"at": 0.0, "slots": []})
    np = stations.STATIONS[key].now_playing()
    music = (data.get("fetch") not in NEVER_MUSIC
             and (data.get("fetch_args") or {}).get("music", True))
    assert (np.artist, np.song, np.raw_title) == _old_icy(title, music)


MARS = '"Man From Mars" by Butch Paulson with "The Motations" on Fool\'s Paradise on WFMU'


@pytest.mark.parametrize("shape,title", [
    (None, IHEART), ("iheart-attrs", IHEART), ("artist-song", "Band - Song"), ("wfmu", MARS)])
def test_music_false_never_yields_an_artist_whatever_the_shape(monkeypatch, shape, title):
    assert set(titles.SHAPES) == {"artist-song", "iheart-attrs", "wfmu"}   # a new shape joins this list
    monkeypatch.setattr(stations.icy, "icy_title", lambda url: title)
    s = stations.Station("x", "X", "http://x", "Town -- x")
    assert icy_fetch.icy(s, titles=shape).artist                 # the shape does match
    np = icy_fetch.icy(s, music=False, titles=shape)
    assert np.artist is None and np.song is None and np.raw_title


@pytest.mark.parametrize("title,expected", [
    (MARS, {"song": "Man From Mars", "artist": 'Butch Paulson with "The Motations"',
            "show": "Fool's Paradise"}),
    # The song's own " by ", " on ", dash and parentheses stay inside its quotes.
    ('"Stand By Me (Live on Air) — Take 2" by Ben E. King on Fool\'s Paradise on WFMU',
     {"song": "Stand By Me (Live on Air) — Take 2", "artist": "Ben E. King",
      "show": "Fool's Paradise"}),
    ('"Written by Him" by Cowboy Copas on Rock\'n\'Soul Radio on WFMU',
     {"song": "Written by Him", "artist": "Cowboy Copas", "show": "Rock'n'Soul Radio"}),
    ('"Brass in Pocket" by Pretenders, The on Fool\'s Paradise on WFMU',
     {"song": "Brass in Pocket", "artist": "The Pretenders", "show": "Fool's Paradise"}),
    # A credit is kept, and an " on " inside its quotes is not the show.
    ('"Song" by Sam with "Live on Mars" on Bodega Pop on WFMU',
     {"song": "Song", "artist": 'Sam with "Live on Mars"', "show": "Bodega Pop"}),
    # The last " on " splits artist from show: an artist keeps its own.
    ('"Song" by Hot on the Heels on Fool\'s Paradise on WFMU',
     {"song": "Song", "artist": "Hot on the Heels", "show": "Fool's Paradise"}),
    # No show named: artist and song still.
    ('"Song" by Cowboy Copas', {"song": "Song", "artist": "Cowboy Copas"}),
    # A show change: the show and its host, never an artist.
    ("Fool's Paradise with Rex", {"show": "Fool's Paradise", "hosts": ["Rex"]}),
    ('Your DJ speaks over "X" on Bucci\'s show on WFMU', {}),
    ("Neutral Milk Hotel - Holland, 1945", {}),
])
def test_wfmu_titles(title, expected):
    assert titles.parse("wfmu", title) == expected


@pytest.mark.parametrize("title,artist,song,show,hosts", [
    (MARS, 'Butch Paulson with "The Motations"', "Man From Mars", "Fool's Paradise", []),
    ("Fool's Paradise with Rex", None, None, "Fool's Paradise", ["Rex"]),
    # Neither a song nor a show: shown raw, and the RSS names the show.
    ('Your DJ speaks over "X" on Bucci\'s show on WFMU', None, None,
     "Rex's show from Oct 3 (latest published)", []),
])
def test_wfmu_station_takes_song_and_show_from_its_title(monkeypatch, title, artist, song,
                                                          show, hosts):
    monkeypatch.setattr(stations.icy, "icy_title", lambda url: title)
    rss = (b"<rss><channel><item><title>WFMU Playlist: Rex's show from Oct 3"
           b"</title></item></channel></rss>")
    monkeypatch.setattr(stations.net, "get", lambda url, headers=None: rss)
    np = stations.STATIONS["wfmu"].now_playing()
    assert (np.artist, np.song, np.show, np.hosts) == (artist, song, show, hosts)
    assert np.raw_title == title


@pytest.mark.parametrize("artist,expected", [
    ('Butch Paulson with "The Motations"', "Butch Paulson"),
    ("Butch Paulson", "Butch Paulson"),
    ("Earth, Wind & Fire with the Emotions", "Earth, Wind & Fire with the Emotions"),
    (None, None),
])
def test_lookup_name_drops_only_a_quoted_with_credit(artist, expected):
    assert stations.lookup_name(artist) == expected

def test_every_station_has_a_shell_word():
    """`scripts/radio.zsh` makes one word per catalog file, so a new station
    needs no edit there -- but only if it still reads the catalog."""
    from pathlib import Path
    zsh = (Path(__file__).parent.parent / "scripts" / "radio.zsh").read_text()
    assert "src/twiddle/stations/catalog" in zsh
    assert set(stations.STATIONS) == {
        p.stem for p in (Path(stations.__file__).parent / "catalog").glob("*.toml")}


# Rainwave's api4/info, 2026-09-27 (OC ReMix channel), trimmed.
RAINWAVE_INFO = {
    "sched_current": {"start_actual": 1790554444, "songs": [{
        "title": "BTMNTBAMLOL",
        "artists": [{"id": 10880, "name": "Danimal Cannon"}, {"id": 35409, "name": "KBart"}],
        "albums": [{"id": 2088, "art": "/album_art/2_2088",
                    "name": "Teenage Mutant Ninja Turtles III: The Manhattan Project"}]}]},
    "sched_history": [{"start_actual": 1790554299, "songs": [{
        "title": "To Endor", "artists": [{"id": 34992, "name": "Nate Tronerud"}],
        "albums": [{"id": 332, "art": "/album_art/2_332", "name": "Dragon Quest IV"}]}]}],
}


def test_rainwave_gives_song_composers_and_game(monkeypatch):
    urls = []
    monkeypatch.setattr(stations.net, "get_json", lambda url: urls.append(url) or RAINWAVE_INFO)
    np = stations.STATIONS["rainwaveocr"].now_playing()
    assert urls == ["https://rainwave.cc/api4/info?sid=2"]     # channel read from the URL
    # Only the first composer is looked up; the rest are shown alongside.
    assert (np.source, np.artist, np.song) == ("Rainwave OC ReMix", "Danimal Cannon",
                                               "BTMNTBAMLOL")
    assert np.note == "(with KBart)"
    assert np.album == "Teenage Mutant Ninja Turtles III: The Manhattan Project"
    assert np.art_url == "https://rainwave.cc/album_art/2_2088_320.jpg"
    assert np.recent[0]["song"] == "To Endor" and np.recent[0]["album"] == "Dragon Quest IV"


# streamabc's channel metadata, 2026-09-27, trimmed (Sunshine Live's 90s channel).
STREAMABC_NOW = {
    "channel": "SUNSHINE LIVE - 90s", "channelkey": "sunsl_90er", "artist": "Legend B",
    "song": "Lost In Love", "station": "sunshine-live", "duration": 362, "type": "now",
    "album": "", "cover": "https://is1-ssl.mzstatic.com/x/source/600x600bb.jpg",
    "images": {"large": {"url": "https://is1-ssl.mzstatic.com/x/source/1200x1200bb.jpg"},
               "medium": {"url": "https://is1-ssl.mzstatic.com/x/source/600x600bb.jpg"}},
}


def test_streamabc_gives_the_song_the_icy_title_never_names(monkeypatch):
    urls = []
    monkeypatch.setattr(stations.net, "get_json", lambda url: urls.append(url) or STREAMABC_NOW)
    np = stations.STATIONS["sunshine90s"].now_playing()
    assert urls == ["https://api.streamabc.net/metadata/channel/sunsl_90er.json"]
    assert (np.source, np.artist, np.song) == ("Sunshine Live 90s", "Legend B", "Lost In Love")
    assert np.album is None                                   # "" is not an album
    assert np.art_url == "https://is1-ssl.mzstatic.com/x/source/600x600bb.jpg"


def test_streamabc_between_songs_names_no_artist():
    np = stations.fetchers.streamabc.parse_streamabc({"channel": "80s80s Digital Web",
                                                      "artist": "", "song": ""}, "80s80s")
    assert (np.artist, np.song, np.art_url) == (None, None, None)


# Airtime Pro's live-info-v2, 2026-09-27, trimmed: Ottava names its shows
# and feeds a live source; Shonan Beach FM publishes nothing at all.
OTTAVA_INFO = {
    "station": {"timezone": "Asia/Tokyo"},
    "tracks": {"previous": {"type": "track", "metadata": {"track_title": "60min_0239.mp3"}},
               "current": {"type": "livestream", "name": ""}, "next": None},
    "shows": {"current": {"name": "OTTAVA Fresca", "starts": "2026-09-28 09:00:00",
                          "image_path": ""},
              "next": [{"name": "OTTAVA Celeste", "starts": "2026-09-28 12:00:00"}]},
}
SHONAN_INFO = {
    "station": {"timezone": "Asia/Tokyo"},
    "tracks": {"current": {"type": "livestream", "name": ""}},
    "shows": {"current": None, "next": []},
}


def test_airtime_names_the_show_never_an_artist(monkeypatch):
    urls = []
    monkeypatch.setattr(stations.net, "get_json", lambda url: urls.append(url) or OTTAVA_INFO)
    np = stations.STATIONS["ottava"].now_playing()
    assert urls == ["https://ottava2.airtime.pro/api/live-info-v2"]
    assert (np.artist, np.song, np.raw_title) == (None, None, "OTTAVA Fresca")
    assert [s["show"] for s in np.schedule] == ["OTTAVA Fresca", "OTTAVA Celeste"]
    assert np.schedule[0].get("on_now") and not np.schedule[1].get("on_now")


def test_airtime_with_nothing_published_never_falls_back_to_icy(monkeypatch):
    # Its ICY title is "Shonan Beach FM - offline": that must not become an artist.
    monkeypatch.setattr(stations.net, "get_json", lambda url: SHONAN_INFO)
    monkeypatch.setattr(stations.icy, "icy_title", lambda url, **kw: "Shonan Beach FM - offline")
    np = stations.STATIONS["shonanbeach"].now_playing()
    assert (np.artist, np.raw_title, np.schedule) == (None, None, [])


def test_airtime_takes_a_library_track_but_not_a_filename():
    from twiddle.stations.fetchers.airtime import parse_airtime
    track = {"type": "track", "metadata": {"artist_name": "Satie", "track_title": "Gymnopédie 1",
                                           "album_title": "Piano"}}
    np = parse_airtime(OTTAVA_INFO | {"tracks": {"current": track}})
    assert (np.artist, np.song, np.album, np.show) == ("Satie", "Gymnopédie 1", "Piano",
                                                       "OTTAVA Fresca")
    track["metadata"] |= {"track_title": "BFM2020-01-11-18-00-01.MP3"}
    assert parse_airtime(OTTAVA_INFO | {"tracks": {"current": track}}).artist is None


# gyusyabu.ddo.jp's ICY block, 2026-09-27: Shift-JIS, not UTF-8.
GYUSYABU_META = (b"StreamTitle='\x83X\x83^\x81[\x83N\x83\x8b\x81[\x83U\x81[II for PC98 "
                 b"[MIDI MT-32]   38. \x90_\x93a (C)1993 Arsys ';StreamUrl='';\x00\x00")


def test_shift_jis_titles_decode_only_when_the_station_says_so(monkeypatch):
    title = stations.parse_icy(stations.icy.decode_meta(GYUSYABU_META, "cp932"))
    assert title.startswith("スタークルーザーII for PC98") and "神殿" in title
    assert "�" in stations.parse_icy(stations.icy.decode_meta(GYUSYABU_META))
    asked = []
    monkeypatch.setattr(stations.icy, "icy_title",
                        lambda url, **kw: asked.append(kw) or title)
    np = stations.STATIONS["retropc"].now_playing()
    assert asked == [{"encoding": "cp932"}]
    assert np.artist is None and np.raw_title == title        # a soundtrack line, not a song
