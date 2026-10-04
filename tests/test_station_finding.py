"""`stations search` and `stations probe`: parsing the directories, spotting
the platform, and drafting a catalog entry -- all against canned answers."""
import pytest

from twiddle import stations
from twiddle.stations import directory, probe, titles

# Trimmed from real answers, 2026-09-27.
RADIO_BROWSER = [
    {"name": "KALX 90.7FM Berkeley", "url": "http://stream.kalx.berkeley.edu:8000/kalx-320.aac.m3u",
     "url_resolved": "https://stream.kalx.berkeley.edu:8443/kalx-320.aac",
     "homepage": "https://www.kalx.berkeley.edu/", "favicon": "https://www.kalx.berkeley.edu/favicon.ico",
     "tags": "aac,berkeley,uc berkeley,university radio", "countrycode": "US",
     "votes": 221, "codec": "AAC+", "bitrate": 320, "lastcheckok": 1},
    {"name": "KEXP 90.3 Seattle, WA (AAC 160K)", "url": "https://kexp.streamguys1.com/kexp160.aac",
     "url_resolved": "https://kexp.streamguys1.com/kexp160.aac", "homepage": "https://www.kexp.org/",
     "favicon": "", "tags": "", "countrycode": "US", "votes": 631, "codec": "AAC",
     "bitrate": 162, "lastcheckok": 1},
    {"name": "Some Dub Station", "url": "http://dub.example/stream", "url_resolved": "",
     "homepage": "", "favicon": "", "tags": "dub, techno", "countrycode": "DE", "votes": 3,
     "codec": "MP3", "bitrate": 128, "lastcheckok": 0},
]

SOMAFM = {"channels": [
    {"id": "secretagent", "title": "Secret Agent", "genre": "lounge",
     "description": "The soundtrack for your stylish, mysterious, dangerous life.",
     "xlimage": "https://api.somafm.com/logos/512/secretagent512.png", "listeners": "300"},
    {"id": "groovesalad", "title": "Groove Salad", "genre": "ambient|electronica",
     "description": "A nicely chilled plate of ambient/downtempo beats and grooves.",
     "listeners": "1500"},
]}

TUNEIN_SEARCH = {"head": {"status": "200"}, "body": [
    {"element": "outline", "type": "audio", "text": "KXSF 102.5", "item": "station",
     "URL": "http://opml.radiotime.com/Tune.ashx?id=s151755", "bitrate": "128",
     "formats": "mp3", "guide_id": "s151755", "subtext": "Radio for the Bay Area Community",
     "image": "http://cdn-profiles.tunein.com/s151755/images/logoq.jpg"},
    {"element": "outline", "type": "link", "text": "More stations", "URL": "http://x"},
]}
TUNEIN_TUNE = {"body": [
    {"element": "audio", "url": "http://stream.sfcommunityradio.org:8000/", "reliability": 10},
    {"element": "audio", "url": "http://107.161.208.3:8000/sfcr", "reliability": 98},
]}


def test_radio_browser_results_carry_what_adding_needs():
    found = directory.parse_radio_browser(RADIO_BROWSER)
    kalx, kexp, dub = found
    assert kalx.url == "https://stream.kalx.berkeley.edu:8443/kalx-320.aac"   # resolved, not the .m3u
    assert kalx.tags == ["aac", "berkeley", "uc berkeley", "university radio"]
    assert (kalx.codec, kalx.bitrate, kalx.votes, kalx.ok) == ("AAC+", 320, 221, True)
    assert dub.url == "http://dub.example/stream" and dub.ok is False
    assert kexp.logo is None and kexp.tags == []


def test_results_already_in_the_catalog_are_marked():
    found = directory.mark_catalog(directory.parse_radio_browser(RADIO_BROWSER),
                                   stations.STATIONS)
    kalx, kexp, dub = found
    assert kexp.in_catalog == "kexp (same stream)"     # scheme aside
    assert kalx.in_catalog == "kalx (same name?)"
    assert dub.in_catalog is None
    assert "ALREADY IN CATALOG" in kexp.render()


def test_a_generic_first_word_is_not_a_catalog_match():
    # R/a/dio reduces to "radio"; "Radio Paradise" is not it (seen 2026-09-27).
    found = directory.mark_catalog(
        [directory.Found("radio-browser", "Radio Paradise Main Mix", "http://rp.example/aac")],
        stations.STATIONS)
    assert found[0].in_catalog is None


def test_somafm_channels_map_onto_the_somafm_fetcher():
    found = directory.parse_somafm(SOMAFM, "lounge")
    assert [f.name for f in found] == ["Secret Agent"]
    f = found[0]
    assert f.url == "http://ice1.somafm.com/secretagent-128-mp3" and f.fetch == "somafm"
    # ...and that URL is the shape the fetcher reads its channel from.
    from twiddle.stations.fetchers.somafm import channel_of
    assert channel_of(f.url) == "secretagent"
    assert [f.name for f in directory.parse_somafm(SOMAFM, "")] == ["Groove Salad", "Secret Agent"]
    assert directory.parse_somafm(SOMAFM, "", genre="electronica")[0].name == "Groove Salad"


def test_tunein_search_then_tune_gives_the_most_reliable_stream(monkeypatch):
    assert [f.name for f in directory.parse_tunein(TUNEIN_SEARCH)] == ["KXSF 102.5"]
    assert directory.parse_tunein_streams(TUNEIN_TUNE)[0] == "http://107.161.208.3:8000/sfcr"
    monkeypatch.setattr(stations.net, "get_json",
                        lambda url: TUNEIN_TUNE if "Tune.ashx" in url else TUNEIN_SEARCH)
    found = directory.search_tunein("kxsf")
    assert found[0].url == "http://107.161.208.3:8000/sfcr" and found[0].tags == []


def test_search_reports_a_dead_directory_and_keeps_the_rest(monkeypatch):
    def get_json(url):
        if "somafm" in url:
            return SOMAFM
        raise OSError("down")
    monkeypatch.setattr(stations.net, "get_json", get_json)
    found, errors = directory.search("secret")
    assert [f.name for f in found] == ["Secret Agent"]
    assert errors and errors[0].startswith("radio-browser")


# ---- probe -------------------------------------------------------------------


def test_playlists_give_their_first_stream():
    assert probe.parse_playlist("[playlist]\nNumberOfEntries=1\nFile1=http://a/b\n") == "http://a/b"
    assert probe.parse_playlist("#EXTM3U\n#EXTINF:-1,x\nhttp://c/d\n") == "http://c/d"
    assert probe.parse_playlist("nothing") is None


# Read off stream0.wfmu.org/freeform-128k by `stations probe`, 2026-10-03.
WFMU_TITLES = ['"At War With Satan" by Venom on Marty McSorley\'s show on WFMU',
               '"Orgies - A Tool Of Witchcraft" by Louise Huebner with Louis and Bebe Barron '
               'on Marty McSorley\'s show on WFMU']


def test_title_shapes():
    assert probe.title_shape(["Low - Words", "Broadcast - Tears"]) == "artist-song"
    assert probe.title_shape(['title="Plane",artist="JOHN MULANEY",url=""']) == "iheart-attrs"
    assert probe.title_shape([None, None]) == "blank"
    assert probe.title_shape(["Morning Edition"]) == "show-like"
    # The stream's own name, split on its dash, is not an artist.
    assert probe.title_shape(["90s90s - DIGITAL WEB"] * 2, "90s90s - DIGITAL WEB") == "show-like"
    assert probe.guess_callsign("90s90s - DIGITAL WEB") is None


def test_titles_in_a_registered_shape_are_named():
    assert probe.title_shape(WFMU_TITLES, "WFMU Freeform Radio") == "wfmu"
    # A show change between songs is still WFMU's shape...
    assert probe.title_shape([WFMU_TITLES[0], "Fool's Paradise with Rex"]) == "wfmu"
    # ...but its filler isn't, and one of those means no shape is certain.
    assert probe.title_shape([WFMU_TITLES[0],
                              'Your DJ speaks over "X" on Bucci\'s show on WFMU']) == "show-like"
    space = ('BOBCAT GOLDTHWAIT - text="The Game Of Love" song_spot="M" '
             'amgArtworkURL="https://i.iheart.com/x.jpg" length="00:03:12"')
    assert probe.title_shape([space]) == "iheart-space"
    # iHeart's own comma attributes and a plain title: the default reads both.
    assert probe.title_shape(['title="Plane",artist="JOHN MULANEY",url=""',
                              "Low - Words"]) == "artist-song"
    # A spot or show name beside an iHeart song doesn't lose it the shape.
    spot = 'title="",artist="",url="song_spot=\\"T\\""'
    assert probe.title_shape(['title="Plane",artist="JOHN MULANEY",url=""', spot]) \
        == "iheart-attrs"
    assert probe.title_shape(['title="Plane",artist="JOHN MULANEY",url=""',
                              "The Breakfast Club"]) == "iheart-attrs"


def test_only_specific_shapes_are_ever_guessed():
    assert set(probe.DETECTABLE) <= set(titles.SHAPES)
    assert not {"artist-song", "artist-dot-song", "kcrw"} & set(probe.DETECTABLE)
    # The most specific first: a comma-attribute title is iheart-attrs, never
    # iheart-space (which reads those too).
    assert probe.DETECTABLE.index("iheart-attrs") < probe.DETECTABLE.index("iheart-space")


@pytest.mark.parametrize("samples", [
    ["Good Food-Evan Kleiman-join.kcrw.com"] * 2,        # KCRW's, and only KCRW's
    ["Namee · Scarecrow (1985)", "Low · Words"],            # artist-dot-song is permissive
    ["Morning Edition with Steve Inskeep"] * 2,             # WFMU's show line, no song
])
def test_a_permissive_or_station_anchored_shape_is_never_guessed(samples):
    assert probe.title_shape(samples) == "show-like"


def test_platform_spotting_on_homepages():
    kalx = '<script src="https://spinitron.com/static/js/widget.js"></script>' \
           '<a href="https://spinitron.com/KALX/">playlist</a>'
    assert probe.detect_platform(kalx)[:3] == ("spinitron", "KALX", "spinitron")
    widget = '<iframe src="https://widgets.spinitron.com/widget/now-playing-v2?station=kzsc">'
    assert probe.detect_platform(widget)[1] == "kzsc"
    assert probe.detect_platform('<script src="https://spinitron.com/static/x.js">') is None
    airtime = '<script>var src = "https://dublab.airtime.pro/api/live-info"</script>'
    name, match, fetch, _ = probe.detect_platform(airtime)
    assert (name, match, fetch) == ("airtime-pro", "dublab", None)    # known, no fetcher yet


def test_callsign_guesses():
    assert probe.guess_callsign("KALX 90.7 Berkeley") == "KALX"
    assert probe.guess_callsign("kalx-128-mp3", "http://stream.kalx.berkeley.edu:8000/x") == "KALX"
    assert probe.guess_callsign("Groove Salad", "http://www.example.com/x") is None


def _fake_stream(monkeypatch, *, final, headers, titles, asked=None):
    monkeypatch.setattr(probe, "resolve", lambda url: (final, False, headers, asked or url))
    monkeypatch.setattr(probe, "plain_http", lambda url: (None, "http redirects to https"))
    monkeypatch.setattr(probe.streaminfo, "info", lambda url: None)
    it = iter(titles)
    monkeypatch.setattr(probe.icy, "icy_title", lambda url: next(it))
    monkeypatch.setattr(probe, "measure_logo", lambda url: {"url": url, "width": 512,
                                                            "height": 512, "square": True})
    monkeypatch.setattr(probe, "spinitron_has", lambda call: (False, None))


def test_probe_a_somafm_channel_is_data_only(monkeypatch):
    _fake_stream(monkeypatch, final="http://ice1.somafm.com/secretagent-128-mp3",
                 headers={"icy-metaint": "16000",
                          "icy-name": "Secret Agent: The soundtrack for your life [SomaFM]"},
                 titles=["A - B", "C - D"])
    p = probe.probe("http://ice1.somafm.com/secretagent-128-mp3", sleep=lambda s: None)
    assert p.fetch == "somafm" and p.verdict.startswith("known platform (somafm): data only")
    draft = probe.draft_toml(p)
    assert "# secretagent.toml" in draft and 'name  = "Secret Agent"' in draft
    assert 'url   = "http://ice1.somafm.com/secretagent-128-mp3"' in draft
    assert 'logo  = "https://api.somafm.com/logos/512/secretagent512.png"' in draft
    # and the draft, with the TODOs filled in, is a valid catalog entry
    import tomllib
    data = tomllib.loads(draft.replace('"City -- what it is"', '"SF -- x"')
                         .replace("tags  = []", 'tags = ["electronic"]'))
    assert stations.model.station_from("secretagent", data).name == "Secret Agent"


def test_probe_a_college_station_finds_spinitron(monkeypatch):
    _fake_stream(monkeypatch, final="http://stream.kxyz.example:8000/live",
                 headers={"icy-metaint": "16000", "icy-name": "kxyz-live"}, titles=[None, None])
    monkeypatch.setattr(probe, "spinitron_has", lambda call: (call == "KXYZ", None))
    p = probe.probe("http://stream.kxyz.example:8000/live", sleep=lambda s: None)
    assert (p.callsign, p.fetch) == ("KXYZ", "spinitron")
    draft = probe.draft_toml(p)
    assert 'name  = "KXYZ"' in draft and "callsign" not in draft   # name is the call sign


def test_probe_blank_icy_and_no_platform_needs_a_fetcher(monkeypatch):
    _fake_stream(monkeypatch, final="https://edge7.cdn.example/live",
                 asked="http://radio.example/live",
                 headers={"icy-metaint": "16000", "icy-name": "Mystery Radio"}, titles=[None])
    p = probe.probe("http://radio.example/live", samples=1, sleep=lambda s: None)
    assert p.fetch is None and p.verdict.startswith("needs a custom fetcher")
    assert any("redirects to edge7.cdn.example" in x for x in p.problems)
    assert 'url   = "http://radio.example/live"' in probe.draft_toml(p)   # not the edge


def test_probe_show_titles_are_never_taken_for_artists(monkeypatch):
    _fake_stream(monkeypatch, final="http://talk.example/live",
                 headers={"icy-metaint": "16000", "icy-name": "Talk Example"},
                 titles=["Morning Show", "Morning Show"])
    p = probe.probe("http://talk.example/live", sleep=lambda s: None)
    draft = probe.draft_toml(p)
    assert "music = false" in draft and "titles =" not in draft
    # Write a shape first; music = false only when there's no artist in them.
    assert "write a title shape" in p.verdict
    assert p.verdict.index("title shape") < p.verdict.index("music = false")


def test_probe_wfmu_names_its_shape_and_drafts_it(monkeypatch):
    _fake_stream(monkeypatch, final="http://stream0.wfmu.org/freeform-128k",
                 headers={"icy-metaint": "8192", "icy-name": "WFMU Freeform Radio",
                          "icy-url": "http://wfmu.org"}, titles=WFMU_TITLES)
    monkeypatch.setattr(probe, "fetch_capped", lambda url, kinds, cap=0: b"<html></html>")
    p = probe.probe("http://stream0.wfmu.org/freeform-128k", sleep=lambda s: None)
    assert (p.title_shape, p.fetch) == ("wfmu", "icy")
    assert p.verdict.startswith("ICY titles in the 'wfmu' shape: data only")
    draft = probe.draft_toml(p, "wfmu")
    assert 'fetch_args = { titles = "wfmu" }' in draft and "music" not in draft
    assert "fetch =" not in draft                       # icy is the default
    import tomllib
    data = tomllib.loads(draft.replace('"City -- what it is"', '"Jersey City -- x"')
                         .replace("tags  = []", 'tags = ["freeform"]'))
    assert stations.model.station_from("wfmu", data).name == "WFMU Freeform Radio"


def test_a_stalled_read_times_out_instead_of_hanging():
    import threading
    import pytest
    never = threading.Event()
    with pytest.raises(TimeoutError):
        probe.bounded(never.wait, 0.05)
    assert probe.bounded(lambda: 7, 1) == 7
