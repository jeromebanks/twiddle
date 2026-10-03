"""The alarm model: a recorded ListAlarms parses, and serialises back exactly.

The fixture is a real household's alarm list, anonymised (its header comment
says how). Nothing here touches a speaker: the model has no I/O.
"""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from twiddle.alarms import model
from twiddle.alarms.model import Alarm, Recurrence, parse_alarms

FIXTURE = Path(__file__).parent / "fixtures" / "alarms_listalarms.xml"
TEXT = FIXTURE.read_text()
ELEMENTS = list(ET.fromstring(TEXT).iter("Alarm"))
ALARMS = parse_alarms(TEXT)
BY_ID = {a.id: a for a in ALARMS}

# The fake forms the fixture's header documents.
ROAM_L = "RINCON_00000000000101400"   # the stereo pair's coordinator
ROAM_R = "RINCON_00000000000201400"   # its bonded follower
FAKE_SERIALS = {"91", "92", "93"}
FAKE_PLAYLIST = re.compile(r"FakePlaylist\d{10}")


def test_every_fixture_alarm_parses():
    assert len(ALARMS) == len(ELEMENTS) == 10
    assert [a.id for a in ALARMS] == [el.get("ID") for el in ELEMENTS]


def test_fixture_covers_every_kind_of_source():
    uris = [a.program_uri for a in ALARMS]
    assert any(u == model.CHIME_URI for u in uris)                          # chime
    assert any(u.startswith("x-sonosapi-stream:ihr%3a") for u in uris)     # iHeart
    assert any(u.startswith("x-sonosapi-radio:sonos%3a") for u in uris)    # Sonos Radio
    assert any("spotify%3aplaylist" in u and "sid=12&" in u for u in uris)  # Sonos Spotify
    assert any(a.children for a in ALARMS)                                  # a <Content> child


def test_fixture_has_an_alarm_on_a_bonded_follower():
    assert BY_ID["66"].room_uuid == ROAM_R
    assert {a.room_uuid for a in ALARMS} == {ROAM_L, ROAM_R}


@pytest.mark.parametrize("el", ELEMENTS, ids=lambda el: el.get("ID"))
def test_round_trip_is_exact(el):
    alarm = Alarm.from_element(el)
    assert alarm.to_attributes() == el.attrib
    assert alarm.children == tuple(
        ET.tostring(c, encoding="unicode").strip() for c in el)


@pytest.mark.parametrize("el", ELEMENTS, ids=lambda el: el.get("ID"))
def test_create_and_update_args_carry_the_same_values(el):
    alarm = Alarm.from_element(el)
    args = alarm.update_args()
    # soco's order (soco.alarms.Alarm.save): ID first on an update.
    assert [k for k, _ in args] == [
        "ID", "StartLocalTime", "Duration", "Recurrence", "Enabled", "RoomUUID",
        "ProgramURI", "ProgramMetaData", "PlayMode", "Volume", "IncludeLinkedZones"]
    expected = {("StartLocalTime" if k == "StartTime" else k): v
                for k, v in el.attrib.items()}
    assert dict(args) == expected
    assert alarm.create_args() == args[1:]


def test_metadata_is_kept_decoded_and_untouched():
    # The DIDL's own escapes must survive as text, not be decoded or re-escaped:
    # soco escapes on send, so any change here is a change on the wire.
    meta = dict(BY_ID["66"].create_args())["ProgramMetaData"]
    assert "i don&apos;t want to work" in meta
    assert meta.startswith("<DIDL-Lite ")
    for aid in ("2", "68"):
        meta = dict(BY_ID[aid].create_args())["ProgramMetaData"]
        assert "image?w=60&amp;image=https%3A" in meta
    assert dict(BY_ID["2"].create_args())["ProgramURI"].endswith("sid=303&flags=8232&sn=91")


def test_unknown_attributes_and_children_survive():
    el = ET.fromstring(
        '<Alarm ID="5" StartTime="06:00:00" Duration="" Recurrence="WEEKDAYS" Enabled="1" '
        'RoomUUID="RINCON_00000000000101400" ProgramURI="x-rincon-buzzer:0" '
        'ProgramMetaData="" PlayMode="NORMAL" Volume="0" IncludeLinkedZones="1" '
        'Future="yes"><Thing a="b"/></Alarm>')
    alarm = Alarm.from_element(el)
    assert alarm.extra == {"Future": "yes"}
    assert alarm.children == ('<Thing a="b" />',)
    assert alarm.to_attributes() == el.attrib
    assert ("Future", "yes") not in alarm.create_args()   # not a CreateAlarm argument


def test_typed_fields():
    a = BY_ID["2"]
    assert a.enabled is True and a.include_linked_zones is False
    assert a.volume == 32 and a.start_time == "08:20:00" and a.duration == "02:00:00"
    assert a.recurrence.days == {1, 2, 4, 5}
    assert BY_ID["94"].recurrence.once


def test_a_new_alarm_has_no_id_to_update():
    a = Alarm(start_time="07:00:00", recurrence=Recurrence.on({1, 2, 3, 4, 5}),
              room_uuid=ROAM_L)
    assert dict(a.create_args())["Recurrence"] == "WEEKDAYS"
    assert "ID" not in a.to_attributes()
    with pytest.raises(ValueError, match="no ID"):
        a.update_args()


@pytest.mark.parametrize("bad, field", [
    ({"Enabled": "yes"}, "0 or 1"),
    ({"Volume": "101"}, "volume"),
    ({"Volume": "067"}, "volume"),
    ({"Recurrence": "SOMETIMES"}, "recurrence"),
])
def test_a_malformed_alarm_names_itself(bad, field):
    el = ET.fromstring(ET.tostring(ELEMENTS[0]))
    for k, v in bad.items():
        el.set(k, v)
    with pytest.raises(ValueError, match=rf"alarm 1: .*{field}"):
        Alarm.from_element(el)


def test_a_missing_field_is_named():
    el = ET.fromstring(ET.tostring(ELEMENTS[0]))
    del el.attrib["RoomUUID"]
    with pytest.raises(ValueError, match="alarm 1: missing RoomUUID"):
        Alarm.from_element(el)


def test_not_an_alarm_list():
    with pytest.raises(ValueError, match="<Alarms>"):
        parse_alarms("<Nope/>")


# -- recurrence --------------------------------------------------------------

@pytest.mark.parametrize("text, days", [
    ("ONCE", set()),
    ("DAILY", set(range(7))),
    ("WEEKDAYS", {1, 2, 3, 4, 5}),
    ("WEEKENDS", {0, 6}),
    ("ON_0", {0}),
    ("ON_6", {6}),
    ("ON_135", {1, 3, 5}),
    ("ON_3421", {1, 2, 3, 4}),   # out of order: soco accepts it, so must we
    ("ON_666", {6}),             # repeated, likewise
    ("ON_0123456", set(range(7))),
])
def test_each_recurrence_form_round_trips(text, days):
    r = Recurrence.parse(text)
    assert r.days == days
    assert str(r) == text
    assert Recurrence.parse(str(r)) == r


def test_recurrence_compares_by_days():
    assert Recurrence.parse("DAILY") == Recurrence.parse("ON_0123456")
    assert Recurrence.parse("WEEKENDS") == Recurrence.parse("ON_60")
    assert Recurrence.parse("ONCE") != Recurrence.parse("ON_0")


@pytest.mark.parametrize("days, text", [
    (set(), "ONCE"),
    (range(7), "DAILY"),
    ({5, 1, 2, 3, 4}, "WEEKDAYS"),
    ({6, 0}, "WEEKENDS"),
    ({4, 2}, "ON_24"),
])
def test_recurrence_from_days_uses_the_speakers_name(days, text):
    assert str(Recurrence.on(days)) == text


@pytest.mark.parametrize("text", [
    "", "ON_", "ON_7", "ON_01234560", "on_123", "daily", "Weekdays", "ON_1a",
    " ONCE", "ONCE ", "ON_-1", "EVERY_DAY",
])
def test_an_invalid_recurrence_says_what_is_allowed(text):
    with pytest.raises(ValueError) as e:
        Recurrence.parse(text)
    assert repr(text) in str(e.value)
    assert "WEEKDAYS" in str(e.value) and "ON_<days>" in str(e.value)


def test_invalid_days_are_refused():
    with pytest.raises(ValueError, match="0-6"):
        Recurrence.on({7})


def test_every_fixture_recurrence_parses():
    for el in ELEMENTS:
        assert str(Recurrence.parse(el.get("Recurrence"))) == el.get("Recurrence")


# -- the fixture is anonymised -----------------------------------------------
# An allowlist of the fake forms: never name the real ids here, that would
# commit them.

def test_fixture_has_only_fake_rincon_ids():
    found = set(re.findall(r"RINCON_[0-9A-Za-z]+", TEXT))
    assert found and found <= {ROAM_L, ROAM_R}, found


def test_fixture_has_only_fake_account_serials():
    serials = re.findall(r"\bsn[=_](\w+)", TEXT)
    assert serials and set(serials) <= FAKE_SERIALS, serials
    accounts = re.findall(r'accountId="([^"]*)"', TEXT)
    assert accounts and all(a in {f"sn_{s}" for s in FAKE_SERIALS} for a in accounts)


def test_fixture_has_only_fake_playlist_ids():
    ids = re.findall(r"spotify(?::|%3a)playlist(?::|%3a)(\w+)", TEXT, re.I)
    assert ids and all(FAKE_PLAYLIST.fullmatch(i) for i in ids), ids


def test_fixture_tokens_are_sonos_placeholders():
    # Service descs are SA_RINCON<type>_X_#Svc<type>-0-Token literally; a
    # re-record that carried a real account token in that slot fails here.
    metadata = "".join(a.program_metadata for a in ALARMS)
    descs = re.findall(r"<desc [^>]*>([^<]*)</desc>", metadata)
    assert descs and all(re.fullmatch(r"SA_RINCON\d+_X_#Svc\d+-0-Token", d) for d in descs)


def test_model_does_no_io():
    # The model is pure: no soco, no sockets, no HTTP.
    src = Path(model.__file__).read_text()
    for mod in ("soco", "socket", "requests", "urllib", "http"):
        assert not re.search(rf"^\s*(import|from)\s+{mod}\b", src, re.M), mod
