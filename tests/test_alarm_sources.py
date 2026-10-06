"""What an alarm plays: `alarms/sources/`, its registry, and the CLI reading it.

A station alarm must play with this Mac off the network, so its URI is
checked to name the station's own stream host and nothing here. A fake
podcast provider shows a new source needs no CLI edit: registered through the
same `register()` the stock ones use, it is listed, built and marked.
"""
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from twiddle import alarm_cli, devices, play
from twiddle.alarms import sources
from twiddle.alarms.sources import spotify as spotify_source
from twiddle.alarms.model import CHIME_URI, Alarm, Recurrence
from twiddle.stations import load_catalog

from tests.test_alarm_cli import (ALARMS, ALARMS_XML, LIVING, ROAM_L, SAT_AFTERNOON, alarm_after,  # noqa: F401
                                  clockfake, household, journal, rows, run)

CATALOG = load_catalog(strict=True)


class Podcast(sources.Source):
    """A provider no UI or CLI code knows about."""
    name = "podcast"
    title = "podcast episode"
    takes_choice = True
    fallback = "the Sonos chime"
    EPISODES = {"ep1": "Episode One", "ep2": "Episode Two"}

    def __init__(self, needs_mac=False):
        self.needs_mac = needs_mac

    def choices(self, query=""):
        return [sources.Choice(k, t) for k, t in self.EPISODES.items()
                if query.lower() in t.lower()]

    def build(self, choice=""):
        if choice not in self.EPISODES:
            raise ValueError(f"no episode {choice!r}")
        return f"x-twiddle-podcast:{choice}", play.radio_didl(self.EPISODES[choice])

    def owns(self, uri, metadata):
        return uri.startswith("x-twiddle-podcast:")


@pytest.fixture
def podcast():
    made = []

    def add(needs_mac=False):
        made.append(sources.register(Podcast(needs_mac)))
        return made[-1]
    yield add
    sources.unregister("podcast")


# ---- station: the speaker fetches the stream itself ------------------------

@pytest.mark.parametrize("key", sorted(CATALOG))
def test_a_station_alarm_names_the_stations_own_stream_and_nothing_here(key, monkeypatch):
    st = CATALOG[key]
    # Anything that would put this Mac in the alarm's path would ask for its address.
    monkeypatch.setattr(play, "local_ip_for",
                        lambda peer: pytest.fail("a station alarm asked for this Mac's address"))
    source, uri, didl = sources.build(f"station:{key}")
    want = urlsplit(st.url)
    assert source.name == "station"
    assert uri.startswith("x-rincon-mp3radio://")
    assert uri == "x-rincon-mp3radio://" + want.netloc + st.url.split(want.netloc, 1)[1]
    host = urlsplit("http://" + uri[len("x-rincon-mp3radio://"):]).hostname
    assert host == want.hostname
    assert host not in ("localhost", "0.0.0.0") and not host.startswith("127.")
    for text in (uri, didl):
        assert "localhost" not in text and "127.0.0.1" not in text
    assert uri == play.radio_uri(st.url)           # exactly what `tune` hands a speaker


def test_a_station_alarms_title_is_the_stations_name():
    _, uri, didl = sources.build("station:kalx")
    alarm = Alarm(start_time="07:00:00", recurrence=Recurrence.parse("WEEKDAYS"),
                  room_uuid=ROAM_L, program_uri=uri, program_metadata=didl)
    assert alarm_cli.source_title(alarm) == "KALX"
    assert sources.recognise(uri, didl).name == "station"


def test_a_station_alarm_comes_from_twiddle_through_the_station_provider(monkeypatch):
    _, uri, didl = sources.build("station:kalx")
    alarm = Alarm(id="90", start_time="07:00:00", recurrence=Recurrence.parse("WEEKDAYS"),
                  room_uuid=ROAM_L, program_uri=uri, program_metadata=didl)
    r = alarm_cli.row(household(), alarm, SAT_AFTERNOON)
    assert (r["sound_source"], r["sound_source_text"]) == (
        "twiddle:station", "twiddle station (direct stream)")
    # It is the provider that knows: one that stops owning it leaves the URI unknown.
    monkeypatch.setattr(type(sources.get("station")), "owns", lambda self, uri, meta: False)
    r = alarm_cli.row(household(), alarm, SAT_AFTERNOON)
    assert (r["sound_source"], r["sound_source_text"]) == ("unknown", "unknown (x-rincon-mp3radio)")


def test_play_radio_still_sends_the_same_uri(monkeypatch):
    sent = []
    monkeypatch.setattr(play, "set_uri", lambda ip, uri, meta: sent.append(uri))
    monkeypatch.setattr(play, "play", lambda ip: None)
    play.play_radio("10.0.0.1", "https://streams.example.org/live", "X")
    play.play_radio("10.0.0.1", "http://example.org:8000/a.mp3", "X")
    assert sent == ["x-rincon-mp3radio://streams.example.org/live",
                    "x-rincon-mp3radio://example.org:8000/a.mp3"]


@pytest.mark.parametrize("spec, says", [
    ("station", "which dial station?"),
    ("station:", "which dial station?"),
    ("station:kalz", "did you mean kalx"),
    ("chime:loud", "takes no choice"),
    ("tunein:s1", "no source 'tunein'"),
    ("keep", "no source 'keep'"),
])
def test_a_bad_spec_says_what_is_wrong(spec, says):
    with pytest.raises(ValueError, match=says):
        sources.build(spec)


def test_keep_and_colons_cant_be_registered():
    for name in ("keep", "a:b", ""):
        bad = Podcast()
        bad.name = name
        with pytest.raises(ValueError):
            sources.register(bad)


# ---- needs this Mac --------------------------------------------------------

def test_chime_and_station_dont_need_this_mac():
    assert sources.get("chime").needs_mac is False
    assert sources.get("station").needs_mac is False
    assert sources.build("chime")[1:] == (CHIME_URI, "")


def test_nothing_in_the_fixture_is_claimed_but_the_chimes():
    """iHeart, TuneIn, Sonos Radio and Spotify alarms belong to no source here."""
    got = {a.id: sources.recognise(a.program_uri, a.program_metadata) for a in ALARMS}
    for a in ALARMS:
        want = "chime" if a.program_uri == CHIME_URI else None
        assert (got[a.id].name if got[a.id] else None) == want, a.program_uri
    assert all(not r["needs_mac"] for r in rows().values())


def test_list_marks_an_alarm_whose_source_needs_this_mac(podcast):
    podcast(needs_mac=True)
    uri, didl = sources.get("podcast").build("ep1")
    mac = Alarm(id="90", start_time="06:00:00", recurrence=Recurrence.parse("DAILY"),
                room_uuid=LIVING, program_uri=uri, program_metadata=didl)
    listed = alarm_cli.listing(household(), [*ALARMS, mac], SAT_AFTERNOON)
    by_id = {r["id"]: r for r in listed}
    assert by_id["90"]["source"] == "podcast" and by_id["90"]["needs_mac"] is True
    assert by_id["1"]["source"] == "chime" and by_id["1"]["needs_mac"] is False
    text = alarm_cli.human(listed, SAT_AFTERNOON)
    [line] = [l for l in text.splitlines() if l.lstrip().startswith("#90 ")]
    assert line.endswith("⌁ Episode One")
    assert "⌁ needs this Mac when it goes off" in text
    assert sum("⌁" in l for l in text.splitlines()) == 2


def test_a_source_names_its_own_alarms(podcast, monkeypatch):
    """A source whose metadata says nothing useful still titles its alarms."""
    podcast()
    monkeypatch.setattr(Podcast, "describe", lambda self, uri, meta: "Podcast: " + uri[-3:])
    uri, _ = sources.get("podcast").build("ep1")
    mac = Alarm(id="90", start_time="06:00:00", recurrence=Recurrence.parse("DAILY"),
                room_uuid=LIVING, program_uri=uri, program_metadata="")
    by_id = {r["id"]: r for r in alarm_cli.listing(household(), [*ALARMS, mac], SAT_AFTERNOON)}
    assert by_id["90"]["source_title"] == "Podcast: ep1"
    assert by_id["1"]["source_title"] == "Sonos chime"


def test_a_new_provider_is_a_sound_source_with_no_classifier_edit(podcast):
    uri, didl = Podcast().build("ep1")
    alarm = Alarm(id="90", start_time="06:00:00", recurrence=Recurrence.parse("DAILY"),
                  room_uuid=LIVING, program_uri=uri, program_metadata=didl)
    before = alarm_cli.row(household(), alarm, SAT_AFTERNOON)
    assert (before["sound_source"], before["sound_source_text"]) == (
        "unknown", "unknown (x-twiddle-podcast)")
    podcast()
    r = alarm_cli.row(household(), alarm, SAT_AFTERNOON)
    assert (r["sound_source"], r["sound_source_text"]) == ("twiddle:podcast",
                                                           "twiddle podcast episode")
    text = alarm_cli.human([r], SAT_AFTERNOON)
    assert "twiddle podcast episode  Episode One" in text


def test_list_shows_no_mark_when_nothing_needs_this_mac(podcast):
    podcast(needs_mac=False)
    uri, didl = sources.get("podcast").build("ep1")
    mac = Alarm(id="90", start_time="06:00:00", recurrence=Recurrence.parse("DAILY"),
                room_uuid=LIVING, program_uri=uri, program_metadata=didl)
    text = alarm_cli.human(alarm_cli.listing(household(), [*ALARMS, mac], SAT_AFTERNOON),
                           SAT_AFTERNOON)
    assert "⌁" not in text


# ---- the CLI reads the registry ----------------------------------------------

@pytest.fixture
def offline(monkeypatch):
    """`alarm sources` asks no speaker: finding the household fails the test."""
    monkeypatch.setattr(alarm_cli, "_household",
                        lambda args: pytest.fail("alarm sources looked for speakers"))


def test_sources_lists_every_source(offline, capsys):
    code, out, _ = run(["alarm", "sources", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert [s["name"] for s in payload["sources"]][:2] == ["chime", "station"]
    assert all(s["needs_mac"] is False for s in payload["sources"])
    assert journal() == []


def test_a_registered_fake_provider_is_listed_with_no_cli_edit(offline, podcast, capsys):
    podcast(needs_mac=True)
    code, out, _ = run(["alarm", "sources", "--json"], capsys)
    assert code == 0
    [row] = [s for s in json.loads(out)["sources"] if s["name"] == "podcast"]
    assert row == {"name": "podcast", "title": "podcast episode", "needs_mac": True,
                   "takes_choice": True, "fallback": "the Sonos chime"}
    code, out, _ = run(["alarm", "sources"], capsys)
    assert "podcast:<key>" in out and "⌁ podcast episode" in out
    code, out, _ = run(["alarm", "sources", "podcast", "two", "--json"], capsys)
    assert json.loads(out)["choices"] == [{"key": "ep2", "title": "Episode Two", "detail": ""}]


def test_sources_station_lists_and_narrows_the_catalog(offline, capsys):
    code, out, _ = run(["alarm", "sources", "station", "--json"], capsys)
    assert code == 0
    assert {c["key"] for c in json.loads(out)["choices"]} == set(CATALOG)
    code, out, _ = run(["alarm", "sources", "station", "kalx"], capsys)
    assert out.splitlines()[0] == "dial station, --source station:<key>"
    assert out.splitlines()[1].split()[:2] == ["kalx", "KALX"]


def test_sources_with_an_unknown_name_fails(offline, capsys):
    code, out, _ = run(["alarm", "sources", "tunein", "--json"], capsys)
    assert code == 1 and "no source 'tunein'" in json.loads(out)["error"]


def test_add_a_station_alarm_dry_run(clockfake, capsys):
    """The demo's command, offline: a KALX weekday alarm."""
    code, out, _ = run(["alarm", "add", "--room", "roam", "--time", "07:00", "--days",
                        "weekdays", "--source", "station:kalx", "--dry-run", "--json"], capsys)
    assert code == 0
    alarm = json.loads(out)["alarm"]
    assert alarm["ProgramURI"] == play.radio_uri(CATALOG["kalx"].url)
    assert alarm["Recurrence"] == "WEEKDAYS"
    assert clockfake.writes == [] and journal() == []


def test_add_a_station_alarm_writes_it(clockfake, capsys):
    code, out, _ = run(["alarm", "add", "--room", "roam", "--time", "07:00",
                        "--source", "station:kalx"], capsys)
    assert code == 0 and "KALX" in out
    aid = max(clockfake.alarms, key=int)
    after = alarm_after(clockfake, aid)
    assert after.program_uri == play.radio_uri(CATALOG["kalx"].url)
    assert alarm_cli.source_title(after) == "KALX"
    assert [e["action"] for e in journal()] == ["alarm_create"]


def test_add_with_a_fake_providers_source(clockfake, podcast, capsys):
    podcast()
    code, out, _ = run(["alarm", "add", "--room", "roam", "--time", "07:00",
                        "--source", "podcast:ep2", "--dry-run", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["alarm"]["ProgramURI"] == "x-twiddle-podcast:ep2"


def test_edit_to_a_station(clockfake, capsys):
    code, out, _ = run(["alarm", "edit", "2", "--source", "station:kexp", "--json"], capsys)
    assert code == 0
    assert clockfake.alarms["2"]["ProgramURI"] == play.radio_uri(CATALOG["kexp"].url)
    assert json.loads(out)["fields"] == ["program_uri", "program_metadata"]


@pytest.mark.parametrize("argv, says", [
    (["add", "--room", "roam", "--time", "07:00", "--source", "station:nope"], "no station"),
    (["add", "--room", "roam", "--time", "07:00", "--source", "keep"], "no source 'keep'"),
    (["edit", "2", "--source", "station"], "which dial station?"),
])
def test_a_bad_source_writes_nothing(clockfake, capsys, argv, says):
    code, out, _ = run(["alarm", *argv, "--json"], capsys)
    assert code == 1 and says in json.loads(out)["error"]
    assert clockfake.writes == [] and journal() == []


# ---- spotify: Sonos's own link, the account read from the household ---------

FAV_FIXTURE = Path(__file__).parent / "fixtures" / "favorites_spotify.xml"


def _fixture_alarm(alarm_id):
    return next(a for a in ALARMS if a.id == alarm_id)


def _shape(doc):
    return ET.canonicalize(doc, strip_text=True)


def fake_spotify(sn="92", search=None):
    return sources.Spotify(account=lambda anchor: sn, search=search or (lambda q: []),
                           lookup=lambda kind, ident: ("", ""))


@pytest.fixture(autouse=True)
def _no_spotify_or_lan(monkeypatch):
    """The registered Spotify source reaches the household and Spotify: a test that
    builds with it must say how (an override below), never quietly use the real ones."""
    def refuse(*a, **kw):
        pytest.fail("the registered Spotify source reached for the LAN or Spotify")
    for name in ("account", "_search", "_lookup"):
        monkeypatch.setattr(sources.get("spotify"), name, refuse)


def test_a_playlist_alarm_has_the_shape_of_the_households_spotify_alarms():
    real = _fixture_alarm("66")
    meta = ET.fromstring(real.program_metadata)
    ns = {"dc": "http://purl.org/dc/elements/1.1/",
          "u": "urn:schemas-upnp-org:metadata-1-0/upnp/"}
    uri, didl = spotify_source.program(
        "playlist", "FakePlaylist0000000001", "92", meta.find(".//dc:title", ns).text,
        meta.find(".//u:albumArtURI", ns).text)
    assert uri == real.program_uri
    assert _shape(didl) == _shape(real.program_metadata)


def test_the_built_playlist_is_still_a_sonos_spotify_alarm_to_the_classifier():
    uri, didl = fake_spotify().build("spotify:playlist:FakePlaylist0000000001")
    alarm = Alarm(id="90", start_time="07:00:00", recurrence=Recurrence.parse("DAILY"),
                  room_uuid=LIVING, program_uri=uri, program_metadata=didl)
    r = alarm_cli.row(household(), alarm, SAT_AFTERNOON)
    assert (r["sound_source"], r["source"], r["needs_mac"]) == ("spotify_sonos", None, False)


@pytest.mark.parametrize("choice, uri, klass", [
    ("spotify:album:FakeAlbum00000000001",
     "x-rincon-cpcontainer:00040000spotify%3aalbum%3aFakeAlbum00000000001?sid=12&flags=0&sn=92",
     "object.container.album.musicAlbum"),
    ("spotify:track:FakeTrack0000000001",
     "x-sonos-spotify:spotify%3atrack%3aFakeTrack0000000001?sid=12&flags=8224&sn=92",
     "object.item.audioItem.musicTrack"),
])
def test_album_and_track_forms_are_built(choice, uri, klass):
    got_uri, didl = fake_spotify().build(choice)
    assert got_uri == uri
    assert f"<upnp:class>{klass}</upnp:class>" in didl
    assert "Svc3079-0-Token" in didl


@pytest.mark.parametrize("tail", ["", "/", "?si=abc", "/?si=abc#frag"])
def test_a_whole_spotify_link_with_a_query_or_fragment_is_a_choice(tail):
    link = "https://open.spotify.com/playlist/FakePlaylist0000000001" + tail
    assert spotify_source.parse_choice(link) == ("playlist", "FakePlaylist0000000001")


def test_a_spotify_link_is_a_choice_and_the_ids_case_survives():
    link = "https://open.spotify.com/intl-de/playlist/FakePlaylist0000000001?si=abc"
    assert fake_spotify().build(link) == fake_spotify().build("spotify:playlist:FakePlaylist0000000001")
    assert "FakePlaylist0000000001" in fake_spotify().build(link)[0]


@pytest.mark.parametrize("bad", ["", "spotify:artist:abc", "spotify:playlist:", "kalx",
                                 "https://example.com/playlist/abc",
                                 "https://open.spotify.com/playlist/FakePlaylist0000000001-invalid",
                                 "https://open.spotify.com/playlist/FakePlaylist0000000001/extra"])
def test_anything_that_is_not_a_spotify_item_is_refused(bad):
    with pytest.raises(ValueError, match="isn't a Spotify"):
        fake_spotify().build(bad)


def test_a_search_result_gives_the_alarm_its_title_and_cover():
    found = [{"_kind": "playlist", "id": "FakePlaylist0000000002", "name": "Todd Rundgren Mix",
              "owner": {"display_name": "Someone"},
              "images": [{"url": "https://example.test/c.jpg"}]}]
    src = fake_spotify(search=lambda q: found)
    [choice] = src.choices("todd")
    assert choice.key == "playlist:FakePlaylist0000000002"
    assert choice.title == "Todd Rundgren Mix" and "Someone" in choice.detail
    _, didl = src.build(choice.key)
    assert "<dc:title>Todd Rundgren Mix</dc:title>" in didl
    assert "https://example.test/c.jpg" in didl


def test_no_linked_account_says_so_instead_of_building_a_broken_alarm():
    with pytest.raises(ValueError, match="no Spotify account is linked"):
        fake_spotify(sn=None).build("spotify:playlist:FakePlaylist0000000001")


def test_a_linked_account_with_no_spotify_alarm_can_still_make_one(monkeypatch):
    """The household has the Spotify favourites and no Spotify alarm: the account
    comes from the favourites, read from the speaker, not from an alarm."""
    seen = []

    def soap(ip, service, action, body="", path=None):
        seen.append((service, action))
        return FAV_FIXTURE.read_text()

    monkeypatch.setattr(spotify_source.devices, "soap", soap)
    monkeypatch.setattr(spotify_source, "alarms_account",
                        lambda alarms: pytest.fail("looked at the alarms"))
    assert spotify_source.favourites_account("192.0.2.1") == "92"
    assert seen == [("ContentDirectory", "Browse")]
    src = fake_spotify()
    src.account = lambda anchor: spotify_source.favourites_account("192.0.2.1")
    uri, _ = src.build("spotify:playlist:FakePlaylist0000000009")
    assert uri.endswith("?sid=12&flags=0&sn=92")


def test_the_account_falls_back_to_an_existing_spotify_alarm_last():
    assert spotify_source.alarms_account(ALARMS) == "92"
    assert spotify_source.alarms_account([a for a in ALARMS if "spotify" not in a.program_uri]) is None


def test_favourites_with_no_spotify_item_name_no_account():
    assert spotify_source.account_from_text(
        "x-sonosapi-stream:s00000?sid=254&amp;flags=8224&amp;sn=0") is None


def test_the_default_household_lookup_prefers_favourites(monkeypatch):
    monkeypatch.setattr(spotify_source, "favourites_account", lambda ip: "4")
    monkeypatch.setattr(spotify_source.Household, "load", classmethod(lambda cls, anchor=None: household()))
    assert spotify_source._household_account() == "4"


def test_the_default_lookup_uses_the_alarms_when_the_favourites_fail(monkeypatch):
    def boom(ip):
        raise OSError("unreachable")

    monkeypatch.setattr(spotify_source, "favourites_account", boom)
    monkeypatch.setattr(spotify_source.Household, "load", classmethod(lambda cls, anchor=None: household()))
    monkeypatch.setattr(spotify_source.clock, "list_alarms",
                        lambda ip, tolerant=False: type("L", (), {"alarms": ALARMS})())
    assert spotify_source._household_account() == "92"


def test_a_sid_that_only_starts_with_12_is_another_service():
    assert spotify_source.account_from_text("x-sonosapi-stream:s1?sid=120&flags=0&sn=7") is None
    assert spotify_source.account_from_text("x-sonosapi-stream:s1?sid=12&flags=0&sn=7") == "7"


def test_a_search_failure_is_a_plain_error_not_a_traceback(monkeypatch):
    from twiddle import spotify_ops
    monkeypatch.setattr(spotify_ops, "session", lambda: (_ for _ in ()).throw(
        spotify_ops.PlaybackError("not signed in", "run `spotify auth`")))
    with pytest.raises(ValueError, match="not signed in: run `spotify auth`"):
        spotify_source._search("jazz")


def test_an_unreachable_household_is_a_plain_error(monkeypatch):
    def boom(cls, anchor=None):
        raise OSError("no route")
    monkeypatch.setattr(spotify_source.Household, "load", classmethod(boom))
    with pytest.raises(ValueError, match="--anchor <ip>"):
        spotify_source._household_account()


def test_the_cli_anchor_reaches_the_account_lookup(monkeypatch):
    seen = []
    monkeypatch.setattr(spotify_source.Household, "load",
                        classmethod(lambda cls, anchor=None: seen.append(anchor) or household()))
    monkeypatch.setattr(spotify_source, "favourites_account", lambda ip: "92")
    src = sources.Spotify(account=spotify_source._household_account,
                          lookup=lambda kind, ident: ("", ""))
    monkeypatch.setitem(sources._REGISTRY, "spotify", src)
    sources.build("spotify:playlist:FakePlaylist0000000001", anchor="192.0.2.9")
    assert seen == ["192.0.2.9"]


def test_a_link_with_no_search_gets_its_real_name_when_spotify_can_say():
    src = sources.Spotify(account=lambda anchor: "92",
                          lookup=lambda kind, ident: ("Real Name", "https://example.test/a.jpg"))
    _, didl = src.build("playlist:FakePlaylist0000000001")
    assert "<dc:title>Real Name</dc:title>" in didl and "a.jpg" in didl


# ---- spotify end to end: `alarm add` through a fake speaker --------------------

@pytest.fixture
def spotify_free(monkeypatch, clockfake):
    """A household whose only Spotify trace is a favourite: no Spotify alarm."""
    from tests.test_alarm_clock import FakeClock
    root = ET.fromstring(ALARMS_XML)
    for alarm in [a for a in root if "spotify" in a.get("ProgramURI", "")]:
        root.remove(alarm)
    spotify_free_xml = ET.tostring(root, encoding="unicode")
    assert "spotify" not in spotify_free_xml
    f = FakeClock(alarms_xml=spotify_free_xml)
    real_post = f.post
    seen = []

    def post(url, data=None, headers=None, timeout=None):
        if headers["SOAPACTION"].strip('"').endswith("#Browse"):
            seen.append("Browse")
            return type("R", (), {"text": FAV_FIXTURE.read_text(),
                                  "raise_for_status": lambda self: None})()
        return real_post(url, data=data, headers=headers, timeout=timeout)

    monkeypatch.setattr(devices.requests, "post", post)
    monkeypatch.setattr(spotify_source.Household, "load",
                        classmethod(lambda cls, anchor=None: household()))
    f.browsed = seen
    return f


def _real_spotify(monkeypatch):
    src = sources.Spotify(lookup=lambda kind, ident: ("", ""))
    monkeypatch.setitem(sources._REGISTRY, "spotify", src)


def test_a_household_with_a_linked_account_and_no_spotify_alarm_can_add_one(
        spotify_free, monkeypatch, capsys):
    _real_spotify(monkeypatch)
    code, out, _ = run(["alarm", "add", "--room", "roam", "--time", "07:00", "--days",
                        "weekdays", "--source", "spotify:playlist:FakePlaylist0000000009",
                        "--dry-run", "--json"], capsys)
    assert code == 0, out
    assert "FakePlaylist0000000009?sid=12&flags=0&sn=92" in out
    assert spotify_free.browsed == ["Browse"]
    assert spotify_free.writes == [] and journal() == []


def test_add_with_no_linked_account_writes_nothing_and_says_so(
        spotify_free, monkeypatch, capsys):
    _real_spotify(monkeypatch)
    monkeypatch.setattr(spotify_source, "favourites_account", lambda ip: None)
    code, out, _ = run(["alarm", "add", "--room", "roam", "--time", "07:00", "--days",
                        "weekdays", "--source", "spotify:playlist:FakePlaylist0000000009",
                        "--json"], capsys)
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False
    assert "no Spotify account is linked" in payload["error"]
    assert spotify_free.writes == [] and journal() == []
