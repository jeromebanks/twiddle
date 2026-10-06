"""`twiddle alarm list`: every alarm, under its room, and nothing written.

The alarms are T1.1's recorded (anonymised) ListAlarms; the household around
them is built here. The end-to-end tests answer at the HTTP layer, so the real
SOAP envelopes are built and every action the command sends is seen.
"""
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
import requests

from twiddle import alarm_cli, cli, devices, play
from twiddle.alarms import clock
from twiddle.alarms.model import Alarm, Recurrence, parse_alarms
from twiddle.household import Group, Household, Speaker
from twiddle.topology import Member, Topology, Vanished

FIXTURE = Path(__file__).parent / "fixtures" / "alarms_listalarms.xml"
ALARMS_XML = FIXTURE.read_text()[FIXTURE.read_text().index("<Alarms>"):]
ROAM_L = "RINCON_00000000000101400"
ROAM_R = "RINCON_00000000000201400"
LIVING = "RINCON_00000000000301400"
SURROUND = "RINCON_00000000000401400"
GONE = "RINCON_00000000000501400"
STRANGER = "RINCON_00000000000601400"
PAIR_MAP = f"{ROAM_L}:LF,LF;{ROAM_R}:RF,RF"

# 2026-10-03 is a Saturday.
SAT_AFTERNOON = clock.HouseholdTime(datetime(2026, 10, 3, 13, 20, 1),
                                    datetime(2026, 10, 3, 20, 20, 1), "INV", "INV")


def household(roam_coordinator=ROAM_L) -> Household:
    l = Speaker(ip="10.0.0.11", uuid=ROAM_L, name="Sonos Roam (L)", room="Sonos Roam",
                group_id="g1", is_coordinator=roam_coordinator == ROAM_L, channel="LF")
    r = Speaker(ip="10.0.0.12", uuid=ROAM_R, name="Sonos Roam (R)", room="Sonos Roam",
                group_id="g1", is_coordinator=roam_coordinator == ROAM_R, channel="RF")
    lr = Speaker(ip="10.0.0.13", uuid=LIVING, name="Living Room", room="Living Room",
                 group_id="g2", is_coordinator=True)
    sur = Speaker(ip="10.0.0.14", uuid=SURROUND, name="Living Room (LR)",
                  room="Living Room", group_id="g2", invisible=True, is_satellite=True,
                  channel="LR")
    roam_coord = l if roam_coordinator == ROAM_L else r
    groups = [Group("g1", roam_coord, [l, r]), Group("g2", lr, [lr, sur])]
    topo = Topology(
        groups={"g1": [Member(uuid=ROAM_L, ip=l.ip, zone_name="Sonos Roam", software="",
                              chan_map=PAIR_MAP),
                       Member(uuid=ROAM_R, ip=r.ip, zone_name="Sonos Roam", software="",
                              chan_map=PAIR_MAP)]},
        vanished=[Vanished(uuid=GONE, zone_name="Kitchen", reason="powered off",
                           model="One", mac="", last_known_ip="", last_seen_utc="")])
    return Household([l, r, lr, sur], groups, topo)


def extra(uuid, aid):
    return Alarm(id=aid, start_time="06:00:00", recurrence=Recurrence.parse("DAILY"),
                 room_uuid=uuid)


ALARMS = parse_alarms(ALARMS_XML)


def rows(alarms=None, house=None, hh=SAT_AFTERNOON):
    return {r["id"]: r for r in alarm_cli.listing(house or household(),
                                                   alarms or ALARMS, hh)}


# ---- every alarm, under its room -------------------------------------------

def test_every_fixture_alarm_is_listed_under_its_room():
    got = rows()
    assert set(got) == {a.id for a in ALARMS}
    assert {r["room"] for r in got.values()} == {"Sonos Roam"}
    assert got["66"]["status"] == "bonded_follower"
    assert got["66"]["speaker"] == "Sonos Roam (R)"
    assert all(r["status"] == "ok" for i, r in got.items() if i != "66")


def test_every_field_the_criteria_name_is_present():
    r = rows()["2"]
    want = {
        "room": "Sonos Roam", "time": "08:20:00", "time_text": "08:20",
        "recurrence": "ON_1245", "days": [1, 2, 4, 5], "days_text": "Mon Tue Thu Fri",
        "enabled": True, "volume": 32, "duration": "02:00:00", "duration_text": "2h00",
        "play_mode": "REPEAT_ALL", "source_title": "KQED"}
    assert {k: r[k] for k in want} == want


def test_source_titles_come_from_the_alarms_own_metadata():
    got = rows()
    assert got["1"]["source_title"] == "Sonos chime"
    assert got["11"]["source_title"] == "Pindrop Electronic"                # Sonos Radio
    assert got["34"]["source_title"] == "88.5 | KQED-FM (US News)"          # TuneIn
    assert got["66"]["source_title"].startswith("i don't want to work")    # Spotify


def test_unreadable_metadata_falls_back_to_the_uri_scheme():
    a = extra(ROAM_L, "90")
    a.program_uri, a.program_metadata = "x-sonosapi-stream:foo", "<DIDL-Lite"
    assert alarm_cli.source_title(a) == "x-sonosapi-stream"


def test_a_vanished_or_unknown_speaker_is_labelled_not_hidden():
    got = rows(ALARMS + [extra(GONE, "91"), extra(STRANGER, "92"), extra(SURROUND, "93")])
    assert got["91"] | {"room": "Kitchen", "status": "vanished"} == got["91"]
    assert got["92"]["status"] == "unknown"
    assert got["93"] | {"room": "Living Room", "status": "bonded_follower"} == got["93"]
    text = alarm_cli.human(list(got.values()), SAT_AFTERNOON)
    assert "no longer in the household" in text
    assert f"set on {STRANGER}, not a speaker in this household" in text


def test_a_household_with_no_topology_still_lists():
    house = household()
    house.topology = None
    got = rows(ALARMS + [extra(GONE, "91")], house=house)
    assert got["91"]["status"] == "unknown"
    assert got["66"]["status"] == "bonded_follower"


def test_the_pair_primary_comes_from_the_channel_map_when_grouped_elsewhere():
    # The pair grouped under another room: neither Roam is the coordinator,
    # so the first unit in ChannelMapSet is the room.
    house = household()
    lr = house.groups[1].coordinator
    roams = house.groups[0].members
    house.groups = [Group("g", lr, [lr, *roams])]
    got = rows(house=house)
    assert got["66"]["status"] == "bonded_follower"
    assert got["2"]["status"] == "ok"


# ---- next fire, in the household's own time --------------------------------

def test_next_fire_dates_from_a_saturday_afternoon():
    got = rows()
    assert got["11"]["next_fire"] == "2026-10-10T08:00:00"   # ON_6: 08:00 has passed
    assert got["68"]["next_fire"] == "2026-10-04T09:00:00"   # ON_01: Sunday
    assert got["2"]["next_fire"] == "2026-10-05T08:20:00"    # ON_1245: Monday
    assert got["68"]["next_fire_text"] == "tomorrow 09:00"
    assert got["2"]["next_fire_text"] == "Mon 10-05 08:20"
    disabled = [r for r in got.values() if not r["enabled"]]
    assert len(disabled) == 7 and all(r["next_fire"] is None for r in disabled)


def test_next_fire_later_the_same_day():
    morning = clock.HouseholdTime(datetime(2026, 10, 3, 7, 0), datetime(2026, 10, 3, 14, 0))
    assert rows(hh=morning)["11"]["next_fire_text"] == "today 08:00"


def test_a_once_alarm_fires_at_the_next_occurrence_of_its_time():
    a = extra(ROAM_L, "95")
    a.recurrence = Recurrence.parse("ONCE")
    a.start_time = "02:45:00"
    assert alarm_cli.next_fire(a, SAT_AFTERNOON.local) == datetime(2026, 10, 4, 2, 45)


def test_household_formats():
    from datetime import time
    assert alarm_cli.clock_text(time(17, 5), "12H") == "5:05 PM"
    assert alarm_cli.clock_text(time(0, 30), "12H") == "12:30 AM"
    assert alarm_cli.clock_text(time(17, 5), "INV") == "17:05"
    assert alarm_cli.clock_text(time(17, 5), "ZZZ") == "17:05"
    assert alarm_cli.clock_text(time(6, 0, 45), "INV") == "06:00:45"
    assert alarm_cli.clock_text(time(6, 0, 45), "12H") == "6:00:45 AM"
    hh = clock.HouseholdTime(SAT_AFTERNOON.local, SAT_AFTERNOON.utc, "12H", "DMY")
    assert rows(hh=hh)["2"]["next_fire_text"] == "Mon 05/10 8:20 AM"


@pytest.mark.parametrize("fmt, shown", [("INV", "06:00:45"), ("12H", "6:00:45 AM")])
def test_seconds_are_never_dropped(fmt, shown):
    a = extra(ROAM_L, "96")
    a.start_time = "06:00:45"
    hh = clock.HouseholdTime(SAT_AFTERNOON.local, SAT_AFTERNOON.utc, fmt, "INV")
    r = rows(ALARMS + [a], hh=hh)["96"]
    assert r["time_text"] == shown
    assert r["next_fire_text"] == f"tomorrow {shown}"


@pytest.mark.parametrize("text, shown", [
    ("ONCE", "once"), ("DAILY", "daily"), ("ON_0123456", "daily"),
    ("ON_12345", "weekdays"), ("ON_60", "weekends"), ("ON_01", "Mon Sun"),
])
def test_days_text(text, shown):
    assert alarm_cli.days_text(Recurrence.parse(text)) == shown


@pytest.mark.parametrize("d, shown", [
    ("02:00:00", "2h00"), ("00:30:00", "30m"), ("01:15:00", "1h15"), ("", "no auto-stop"),
    ("01:00:30", "1h00m30s"), ("00:30:15", "30m15s"), ("00:00:45", "0m45s"),
])
def test_duration_text(d, shown):
    assert alarm_cli.duration_text(d) == shown


# ---- end to end, at the HTTP layer -----------------------------------------

def _envelope(action, inner):
    return ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:AlarmClock:1">'
            f"{inner}</u:{action}Response></s:Body></s:Envelope>")


ANSWERS = {
    "ListAlarms": _envelope("ListAlarms",
                            f"<CurrentAlarmList>{escape(ALARMS_XML)}</CurrentAlarmList>"
                            f"<CurrentAlarmListVersion>{ROAM_L}:108</CurrentAlarmListVersion>"),
    "GetTimeNow": _envelope("GetTimeNow",
                            "<CurrentUTCTime>2026-10-03 20:20:01</CurrentUTCTime>"
                            "<CurrentLocalTime>2026-10-03 13:20:01</CurrentLocalTime>"
                            "<CurrentTimeZone>x</CurrentTimeZone>"
                            "<CurrentTimeGeneration>1</CurrentTimeGeneration>"),
    "GetFormat": _envelope("GetFormat", "<CurrentTimeFormat>INV</CurrentTimeFormat>"
                                        "<CurrentDateFormat>INV</CurrentDateFormat>"),
}


@pytest.fixture
def speaker(monkeypatch):
    """A fake household and a fake speaker that records every SOAP action."""
    sent = []

    class Reply:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    def post(url, data=None, headers=None, timeout=None):
        action = headers["SOAPACTION"].strip('"').rsplit("#", 1)[-1]
        sent.append((url, action))
        return Reply(ANSWERS[action])

    monkeypatch.setattr(devices.requests, "post", post)
    monkeypatch.setattr(alarm_cli, "_household", lambda args: household())
    return sent


def run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_list_json_is_the_ok_envelope_with_every_alarm(speaker, capsys):
    code, out, _ = run(["alarm", "list", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["version"] == f"{ROAM_L}:108"
    assert payload["household_time"]["local"] == "2026-10-03T13:20:01"
    assert [r["id"] for r in payload["alarms"]] == [r["id"] for r in alarm_cli.listing(
        household(), ALARMS, SAT_AFTERNOON)]
    assert payload["alarms"] == alarm_cli.listing(household(), ALARMS, SAT_AFTERNOON)


def test_list_prints_every_alarm_under_its_room(speaker, capsys):
    code, out, _ = run(["alarm", "list"], capsys)
    assert code == 0
    assert out.startswith("Sonos Roam\n")
    assert out.count(" vol ") == len(ALARMS)
    assert "set on Sonos Roam (R), a bonded follower" in out
    assert "next: Sat 10-10 08:00" in out


def test_list_writes_nothing(speaker, capsys):
    code, _, _ = run(["alarm", "list", "--json"], capsys)
    assert code == 0
    actions = {a for _, a in speaker}
    assert "ListAlarms" in actions
    assert actions <= {"ListAlarms", "GetTimeNow", "GetFormat"}, actions
    assert all("/AlarmClock/Control" in url for url, _ in speaker)   # no AVTransport
    assert not play.INTERVENTION_LOG.exists()


def test_an_unreachable_household_is_an_error_envelope(monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("no speakers answered discovery")
    monkeypatch.setattr(alarm_cli, "_household", boom)
    code, out, _ = run(["alarm", "list", "--json"], capsys)
    assert code == 1
    payload = json.loads(out)
    assert payload["ok"] is False and "could not reach" in payload["error"]


def _list_answer(doc: str) -> str:
    return _envelope("ListAlarms", f"<CurrentAlarmList>{escape(doc)}</CurrentAlarmList>"
                                   f"<CurrentAlarmListVersion>{ROAM_L}:108"
                                   "</CurrentAlarmListVersion>")


def test_a_malformed_alarm_list_is_an_error_not_a_traceback(speaker, monkeypatch, capsys):
    # `alarm list` lists around one bad alarm (below); a verb that needs the
    # whole list, like `snapshot`, still says so in an error envelope.
    monkeypatch.setitem(ANSWERS, "ListAlarms", _list_answer('<Alarms><Alarm ID="1"/></Alarms>'))
    code, out, _ = run(["alarm", "snapshot", "--json"], capsys)
    assert code == 1
    assert "alarm 1 (missing" in json.loads(out)["error"]


@pytest.mark.parametrize("doc", ["<Alarms><Alarm", "<Nope/>"])
def test_a_list_that_isnt_an_alarm_list_is_still_an_error(speaker, monkeypatch, capsys, doc):
    monkeypatch.setitem(ANSWERS, "ListAlarms", _list_answer(doc))
    code, out, _ = run(["alarm", "list", "--json"], capsys)
    assert code == 1
    assert json.loads(out)["ok"] is False


# ---- one alarm twiddle can't read --------------------------------------------

def _bad(aid, **attrs):
    base = {"ID": aid, "StartTime": "06:00:00", "Duration": "01:00:00", "Recurrence": "DAILY",
            "Enabled": "1", "RoomUUID": ROAM_L, "ProgramURI": "x-rincon-buzzer:0",
            "ProgramMetaData": "", "PlayMode": "NORMAL", "Volume": "20",
            "IncludeLinkedZones": "0"}
    return {k: v for k, v in (base | attrs).items() if v is not None}


# One of each way the model refuses an alarm.
BAD = {"80": _bad("80", Recurrence="EVERY_OTHER_DAY"),
       "81": _bad("81", Volume="101"),
       "82": _bad("82", Enabled="2"),
       "83": _bad("83", StartTime=None, RoomUUID=GONE)}
BAD_REASONS = {"80": "invalid alarm recurrence 'EVERY_OTHER_DAY'",
               "81": "expected a volume 0-100, got '101'",
               "82": "expected 0 or 1, got '2'",
               "83": "missing StartTime"}


def with_bad(doc: str = ALARMS_XML, bad=BAD) -> str:
    els = "".join(ET.tostring(ET.Element("Alarm", a), encoding="unicode") for a in bad.values())
    return doc.replace("</Alarms>", els + "</Alarms>")


def test_list_shows_every_readable_alarm_and_labels_the_unreadable(speaker, monkeypatch,
                                                                   capsys):
    monkeypatch.setitem(ANSWERS, "ListAlarms", _list_answer(with_bad()))
    code, out, _ = run(["alarm", "list"], capsys)
    assert code == 0
    assert out.count(" vol ") == len(ALARMS)
    assert "can't read 4 alarms (fix or delete them in the Sonos app):" in out
    for aid, why in BAD_REASONS.items():
        room = "Kitchen" if aid == "83" else "Sonos Roam"
        assert f"#{aid} {room}: {why}" in out


def test_list_json_keeps_every_row_and_adds_the_unreadable(speaker, monkeypatch, capsys):
    monkeypatch.setitem(ANSWERS, "ListAlarms", _list_answer(with_bad()))
    code, out, _ = run(["alarm", "list", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["alarms"] == alarm_cli.listing(household(), ALARMS, SAT_AFTERNOON)
    got = {u["id"]: u for u in payload["unreadable"]}
    assert set(got) == set(BAD)
    assert all(BAD_REASONS[i] in u["reason"] for i, u in got.items())
    assert got["83"] | {"room": "Kitchen", "status": "vanished", "room_uuid": GONE} == got["83"]
    assert got["80"]["room"] == "Sonos Roam" and got["80"]["status"] == "ok"
    actions = {a for _, a in speaker}
    assert actions <= {"ListAlarms", "GetTimeNow", "GetFormat"}, actions
    assert not play.INTERVENTION_LOG.exists()


def test_a_readable_list_has_no_unreadable(speaker, capsys):
    code, out, _ = run(["alarm", "list", "--json"], capsys)
    assert json.loads(out)["unreadable"] == []
    code, out, _ = run(["alarm", "list"], capsys)
    assert "can't read" not in out


def test_only_unreadable_alarms_still_says_which(speaker, monkeypatch, capsys):
    monkeypatch.setitem(ANSWERS, "ListAlarms", _list_answer(
        with_bad("<Alarms></Alarms>", {"x": {"Volume": "7"}})))
    code, out, _ = run(["alarm", "list"], capsys)
    assert code == 0
    assert out.startswith("no alarms\n")
    assert "an alarm with no ID no room: missing ID" in out


def _calls_with_tolerant(module) -> set[str]:
    """The functions in `module` that read the alarm list tolerantly."""
    import ast
    import inspect
    found = set()
    for fn in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and any(
                        k.arg == "tolerant" and getattr(k.value, "value", None) is True
                        for k in node.keywords):
                    found.add(fn.name)
    return found


def test_only_the_views_read_the_list_tolerantly():
    # Anything that writes, or compares lists to decide a write, must see
    # every alarm: an alarm left out of the comparison could be changed (or
    # destroyed) unseen.
    from twiddle import monitor
    from twiddle.alarms import baseline
    assert _calls_with_tolerant(alarm_cli) == {"cmd_list", "cmd_status"}
    assert _calls_with_tolerant(monitor) == {"_check_alarms"}
    assert _calls_with_tolerant(clock) == set()
    assert _calls_with_tolerant(baseline) == set()


def test_the_clock_refuses_anything_but_a_read():
    with pytest.raises(ValueError, match="not an AlarmClock read"):
        clock._read("10.0.0.11", "DestroyAlarm")


def test_no_alarms(speaker, monkeypatch, capsys):
    monkeypatch.setitem(ANSWERS, "ListAlarms", _envelope(
        "ListAlarms", "<CurrentAlarmList>&lt;Alarms/&gt;</CurrentAlarmList>"
                      "<CurrentAlarmListVersion>x:1</CurrentAlarmListVersion>"))
    code, out, _ = run(["alarm", "list"], capsys)
    assert code == 0 and "no alarms" in out


# ---- alarm snapshot / alarm restore ------------------------------------------

@pytest.fixture
def clockfake(monkeypatch, tmp_path):
    """The household above, and a fake AlarmClock that takes writes."""
    from tests.test_alarm_clock import FakeClock
    f = FakeClock()
    monkeypatch.setattr(devices.requests, "post", f.post)
    monkeypatch.setattr(alarm_cli, "_household", lambda args: household())
    monkeypatch.setattr(alarm_cli, "SNAPSHOT_FILE", tmp_path / "snapshots" / "alarms.json")
    return f


def test_snapshot_saves_the_alarm_list_and_writes_nothing(clockfake, capsys):
    code, out, _ = run(["alarm", "snapshot", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["alarms"] == len(ALARMS)
    saved = json.loads(alarm_cli.SNAPSHOT_FILE.read_text())
    assert saved["version"] == clockfake.version
    assert parse_alarms(saved["alarm_list"]) == ALARMS
    assert clockfake.sent == ["ListAlarms"]
    assert not play.INTERVENTION_LOG.exists()


def test_restore_on_an_unchanged_household_does_nothing(clockfake, capsys):
    run(["alarm", "snapshot"], capsys)
    for argv in (["alarm", "restore", "--dry-run"], ["alarm", "restore"]):
        code, out, _ = run(argv, capsys)
        assert code == 0 and out.startswith("nothing to do")
    assert clockfake.writes == []
    assert not play.INTERVENTION_LOG.exists()


def test_restore_dry_run_lists_every_change_and_writes_nothing(clockfake, capsys):
    run(["alarm", "snapshot"], capsys)
    v = clockfake.version
    clockfake.alarms.pop("2"), clockfake.children.pop("2")
    clockfake.edit_in_app("11", Volume="4")
    clockfake.alarms["300"] = dict(clockfake.alarms["1"], ID="300", StartTime="05:00:00")
    clockfake.children["300"] = []
    assert clockfake.version != v
    code, out, _ = run(["alarm", "restore", "--dry-run"], capsys)
    assert code == 0
    assert out.startswith("[dry-run] would restore")
    assert "update  alarm 11: Sonos Roam" in out and "(volume)" in out
    assert "create  Sonos Roam 08:20:00" in out and "(was alarm 2)" in out
    assert "destroy alarm 300: Sonos Roam 05:00:00" in out
    code, out, _ = run(["alarm", "restore", "--dry-run", "--json"], capsys)
    would = json.loads(out)["would"]
    assert [(w["op"], w["baseline_id"], w["current_id"]) for w in would] == [
        ("create", "2", None), ("update", "11", "11"), ("destroy", None, "300")]
    assert clockfake.writes == []
    assert not play.INTERVENTION_LOG.exists()


def test_restore_reports_and_journals_the_id_map(clockfake, capsys):
    run(["alarm", "snapshot"], capsys)
    clockfake.alarms.pop("2"), clockfake.children.pop("2")
    clockfake.n += 1
    code, out, _ = run(["alarm", "restore", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["ok"] is True and payload["left"] == []
    assert payload["id_map"] == {"2": "200"}
    log = [json.loads(l) for l in play.INTERVENTION_LOG.read_text().splitlines()]
    assert [r["action"] for r in log] == ["alarm_create", "alarm_restore"]
    assert log[-1]["id_map"] == {"2": "200"}
    code, out, _ = run(["alarm", "restore"], capsys)
    assert code == 0 and out.startswith("nothing to do")


def test_a_difference_restore_cannot_make_is_still_reported(clockfake, capsys):
    run(["alarm", "snapshot"], capsys)
    clockfake.children["66"] = []           # an UpdateAlarm that dropped <Content>
    clockfake.n += 1
    code, out, _ = run(["alarm", "restore"], capsys)
    assert code == 0 and out.startswith("nothing to do")
    assert "note: alarm 66 (baseline 66): its child elements differ" in out
    assert clockfake.writes == []


def test_restore_that_leaves_differences_says_so_and_fails(clockfake, capsys):
    run(["alarm", "snapshot"], capsys)
    clockfake.alarms.pop("2"), clockfake.children.pop("2")
    clockfake.n += 1
    clockfake.refuse_rooms.add(ROAM_L)
    code, out, err = run(["alarm", "restore"], capsys)
    assert code == 1
    assert "INCOMPLETE" in out
    assert "stopped: AlarmWriteError: CreateAlarm: HTTPError" in out
    assert "STILL DIFFERENT: create  Sonos Roam 08:20:00" in out


def test_restore_without_a_snapshot_is_an_error(clockfake, capsys):
    code, out, _ = run(["alarm", "restore", "--json"], capsys)
    assert code == 1 and "no alarm snapshot" in json.loads(out)["error"]
    assert clockfake.sent == []


# ---- alarm enable / disable / rm ---------------------------------------------
# Alarm 2 is iHeart (not a source twiddle knows), enabled, an `&` in its URI and
# `&amp;` in its metadata; 66 is Sonos Spotify, disabled, with `&apos;` in its
# metadata and a <Content> child.

def journal():
    if not play.INTERVENTION_LOG.exists():
        return []
    return [json.loads(l) for l in play.INTERVENTION_LOG.read_text().splitlines()]


class Terminal:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def answers(monkeypatch, *said, tty=True):
    """A person at a terminal types these at the prompts; record which were shown."""
    said, asked = list(said), []
    monkeypatch.setattr(alarm_cli.sys, "stdin", Terminal(tty))

    def fake_input(prompt=""):
        asked.append(prompt)
        if not said:
            raise EOFError
        return said.pop(0)
    monkeypatch.setattr("builtins.input", fake_input)
    return asked


@pytest.mark.parametrize("verb, aid, on", [("disable", "2", "0"), ("enable", "66", "1")])
def test_enable_disable_leaves_an_unrecognised_source_byte_for_byte(
        clockfake, capsys, verb, aid, on):
    before = dict(clockfake.alarms[aid])
    children = list(clockfake.children[aid])
    code, out, _ = run(["alarm", verb, aid], capsys)
    assert code == 0 and out.startswith(f"{verb}d alarm {aid}: Sonos Roam")
    got = clockfake.alarms[aid]
    assert got["Enabled"] == on
    assert got["ProgramURI"] == before["ProgramURI"]
    assert got["ProgramMetaData"] == before["ProgramMetaData"]
    assert {k: v for k, v in got.items() if k != "Enabled"} == \
        {k: v for k, v in before.items() if k != "Enabled"}
    assert clockfake.children[aid] == children       # the fake keeps them on update
    old = next(a for a in ALARMS if a.id == aid)
    new = parse_alarms(clockfake.document())
    assert next(a for a in new if a.id == aid) == replace(old, enabled=on == "1")
    assert clockfake.writes == ["UpdateAlarm"]
    [entry] = journal()
    assert entry["action"] == "alarm_update" and entry["written"] is True
    assert entry["before"]["attributes"]["Enabled"] != on
    assert entry["after"]["attributes"]["Enabled"] == on


@pytest.mark.parametrize("verb, aid", [("enable", "2"), ("disable", "66")])
def test_already_so_writes_nothing(clockfake, capsys, verb, aid):
    code, out, _ = run(["alarm", verb, aid, "--json"], capsys)
    assert code == 0 and json.loads(out)["performed"] is False
    assert clockfake.writes == [] and journal() == []


@pytest.mark.parametrize("argv", [["disable", "2"], ["enable", "66"], ["rm", "66"]])
def test_dry_run_prints_the_room_and_alarm_and_writes_nothing(
        clockfake, capsys, monkeypatch, argv):
    asked = answers(monkeypatch)
    code, out, _ = run(["alarm", *argv, "--dry-run"], capsys)
    assert code == 0
    assert out.startswith(f"[dry-run] would {'delete' if argv[0] == 'rm' else argv[0]} "
                          f"alarm {argv[1]}: Sonos Roam ")
    code, out, _ = run(["alarm", *argv, "--dry-run", "--json"], capsys)
    payload = json.loads(out)
    assert payload["room"] == "Sonos Roam" and payload["id"] == argv[1]
    assert payload["alarm"] == clockfake.alarms[argv[1]]
    assert payload["performed"] is False
    assert asked == []
    assert clockfake.sent == ["ListAlarms"] * 2
    assert not play.INTERVENTION_LOG.exists()


def test_an_unknown_alarm_is_an_error(clockfake, capsys):
    code, out, _ = run(["alarm", "disable", "999", "--json"], capsys)
    assert code == 1 and json.loads(out)["error"] == "no alarm 999"
    assert clockfake.writes == []


def test_rm_asks_twice_then_journals_the_whole_alarm(clockfake, capsys, monkeypatch):
    asked = answers(monkeypatch, "y", "66")
    code, out, err = run(["alarm", "rm", "66", "--json"], capsys)
    assert code == 0 and json.loads(out)["performed"] is True     # stdout: the envelope only
    assert len(asked) == 2                     # the prompts are on stderr, and differ:
    assert err.startswith("delete alarm 66: Sonos Roam") and "? [y/N] " in err
    assert err.endswith("type its ID (66) to delete it: ")
    assert "66" not in clockfake.alarms
    [entry] = journal()
    assert entry["action"] == "alarm_destroy" and entry["written"] is True
    assert entry["before"]["attributes"] == clockfake_before("66")
    assert entry["before"]["children"] == list(next(a for a in ALARMS if a.id == "66").children)


def clockfake_before(aid):
    from tests.test_alarm_clock import FakeClock
    return FakeClock().alarms[aid]


@pytest.mark.parametrize("said, prompts", [
    (["n"], 1), ([""], 1), (["y", "n"], 2), (["y", "y"], 2), (["y", "6"], 2), ([], 1)])
def test_rm_not_confirmed_twice_deletes_nothing(clockfake, capsys, monkeypatch, said, prompts):
    asked = answers(monkeypatch, *said)
    code, out, _ = run(["alarm", "rm", "66", "--json"], capsys)
    assert code == 1 and json.loads(out)["error"] == "not deleted: alarm 66"
    assert len(asked) == prompts
    assert "66" in clockfake.alarms
    assert clockfake.writes == [] and journal() == []


def test_rm_with_answers_piped_in_never_asks_and_deletes_nothing(
        clockfake, capsys, monkeypatch):
    asked = answers(monkeypatch, "y", "66", tty=False)
    code, out, err = run(["alarm", "rm", "66", "--json"], capsys)
    payload = json.loads(out)
    assert code == 1 and payload["error"] == "not deleted: alarm 66"
    assert "needs a terminal" in payload["hint"]
    assert asked == [] and err == ""
    assert "66" in clockfake.alarms
    assert clockfake.writes == [] and journal() == []


def test_a_delete_that_landed_while_another_alarm_moved_is_reported_done(
        clockfake, capsys, monkeypatch):
    answers(monkeypatch, "y", "66")
    clockfake.after_write = lambda f: f.edit_in_app("11", Volume="4")
    code, out, _ = run(["alarm", "rm", "66", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0 and payload["ok"] is True and payload["performed"] is True
    assert "alarms 11 changed meanwhile" in payload["warning"]
    assert "66" not in clockfake.alarms
    [entry] = journal()
    assert entry["written"] is True and entry["others_changed"] == ["11"]


def test_an_enable_that_landed_while_another_alarm_moved_shows_the_alarm_after(
        clockfake, capsys):
    clockfake.after_write = lambda f: f.edit_in_app("11", Volume="4")
    code, out, _ = run(["alarm", "enable", "66", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0 and payload["performed"] is True and "warning" in payload
    assert clockfake.alarms["66"]["Enabled"] == "1"
    assert payload["alarm"] == clockfake.alarms["66"]
    assert payload["version"] == clockfake.version
    clockfake.after_write = lambda f: f.edit_in_app("11", Volume="5")
    code, out, _ = run(["alarm", "disable", "66"], capsys)
    assert code == 0 and clockfake.alarms["66"]["Enabled"] == "0"
    assert out.startswith("disabled alarm 66: Sonos Roam ") and "(off, vol" in out
    assert "warning: alarm 66 was written, but alarms 11 changed meanwhile" in out


def test_an_enable_whose_list_couldnt_be_read_back_says_so(clockfake, capsys):
    def quiet(f):                       # the write answered; the read-back times out
        def ListAlarms(args):
            raise requests.Timeout("read timed out")
        f.ListAlarms = ListAlarms
    clockfake.after_write = quiet
    before = dict(clockfake.alarms["66"])
    code, out, _ = run(["alarm", "enable", "66", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0 and payload["performed"] is True
    assert clockfake.alarms["66"]["Enabled"] == "1"
    assert payload["alarm"] is None and payload["version"] is None
    assert payload["before"] == before
    assert "Timeout" in payload["warning"]
    clockfake.after_write = None
    clockfake.alarms["66"]["Enabled"] = "0"
    del clockfake.ListAlarms
    clockfake.after_write = quiet
    code, out, _ = run(["alarm", "enable", "66"], capsys)
    assert out.startswith("enabled alarm 66 (the list couldn't be read back to show it)")
    assert "isn't in the list" not in out


def test_rm_refuses_if_the_list_moved_while_asking(clockfake, capsys, monkeypatch):
    answers(monkeypatch)
    said = iter(["y", "66"])

    def meanwhile(prompt=""):
        clockfake.edit_in_app("11", Volume="4")
        return next(said)
    monkeypatch.setattr("builtins.input", meanwhile)
    code, out, _ = run(["alarm", "rm", "66", "--json"], capsys)
    payload = json.loads(out)
    assert code == 1 and "nothing written" in payload["error"]
    assert payload["written"] is False and "Sonos app" in payload["hint"]
    assert "66" in clockfake.alarms
    assert clockfake.writes == [] and journal() == []


@pytest.mark.parametrize("aid", ["2", "66"])
def test_a_deleted_alarm_is_recreated_from_its_journal_entry(
        clockfake, capsys, monkeypatch, aid):
    answers(monkeypatch, "y", aid)
    assert run(["alarm", "rm", aid], capsys)[0] == 0
    old = next(a for a in ALARMS if a.id == aid)
    # From the journal on disk, not from anything still in memory.
    entry = json.loads(play.INTERVENTION_LOG.read_text().splitlines()[-1])
    assert clock.from_record(entry["before"]) == old
    made, now, lost = clock.recreate("10.0.0.11", aid, clockfake.version)
    assert lost == ([] if aid == "2" else ["its child elements (1)"])
    assert made.id != aid and made.id in clockfake.alarms
    assert made.program_uri == old.program_uri
    assert made.program_metadata == old.program_metadata
    assert clockfake.alarms[made.id]["ProgramMetaData"] == clockfake_before(aid)["ProgramMetaData"]
    # Every field equal but the ID. A Spotify alarm's <Content> child is the
    # exception: CreateAlarm has no argument for it (see model.py), and
    # `lost` says so.
    assert replace(made, id=aid, children=old.children) == old
    assert made.children == ()
    assert [r["action"] for r in journal()] == ["alarm_destroy", "alarm_create"]
    with pytest.raises(ValueError, match=f"alarm {made.id} is already the same"):
        clock.recreate("10.0.0.11", aid, clockfake.version)
    assert clockfake.writes == ["DestroyAlarm", "CreateAlarm"]


def test_an_attribute_the_model_doesnt_know_is_journalled_but_cant_be_recreated(
        clockfake, capsys, monkeypatch):
    clockfake.alarms["300"] = dict(clockfake.alarms["2"], ID="300", StartTime="05:00:00",
                                   FutureField="keep-me")
    clockfake.children["300"] = []
    clockfake.n += 1
    answers(monkeypatch, "y", "300")
    assert run(["alarm", "rm", "300"], capsys)[0] == 0
    entry = json.loads(play.INTERVENTION_LOG.read_text().splitlines()[-1])
    assert entry["before"]["attributes"]["FutureField"] == "keep-me"
    old = clock.from_record(entry["before"])
    assert old.extra == {"FutureField": "keep-me"}
    made, _, lost = clock.recreate("10.0.0.11", "300", clockfake.version)
    assert lost == ["attributes FutureField"]
    assert replace(made, id="300", extra=old.extra) == old
    assert "FutureField" not in clockfake.alarms[made.id]


def test_only_a_delete_that_landed_can_be_recreated(clockfake, capsys, monkeypatch):
    assert clock.deleted("66") is None
    play._journal("alarm_destroy", "10.0.0.11", alarm_id="66", written=False,
                  before={"attributes": clockfake_before("66"), "children": []})
    assert clock.deleted("66") is None
    with pytest.raises(KeyError, match="no deleted alarm 66"):
        clock.recreate("10.0.0.11", "66", clockfake.version)
    assert clockfake.writes == []


def test_list_shows_each_alarms_id(speaker, capsys):
    code, out, _ = run(["alarm", "list"], capsys)
    assert all(f"#{a.id} " in out for a in ALARMS)


# ---- alarm add / alarm edit ----------------------------------------------------
# Alarm 2 is iHeart, on ROAM_L; 66 is Sonos Spotify with a <Content> child, on
# ROAM_R (the pair's bonded follower). Neither source is one twiddle knows.

def alarm_after(f, aid):
    return next(a for a in parse_alarms(f.document()) if a.id == aid)


def without_id(attrs):
    return {k: v for k, v in attrs.items() if k != "ID"}


@pytest.mark.parametrize("days, spelt", [
    ("once", "ONCE"), ("daily", "DAILY"), ("weekdays", "WEEKDAYS"), ("weekends", "WEEKENDS"),
    ("mon,wed,fri", "ON_135"), ("Tue", "ON_2"), ("sat,sun", "WEEKENDS"),
    ("mon-fri", "WEEKDAYS"), ("fri-mon", "ON_0156"), ("ON_0246", "ON_0246"),
    ("on_210", "ON_012"), ("ON_12345", "WEEKDAYS"),
])
def test_add_each_recurrence_form_reads_back_identically(clockfake, capsys, days, spelt):
    argv = ["alarm", "add", "--room", "Sonos Roam", "--time", "7:15", "--days", days, "--json"]
    code, out, _ = run([*argv, "--dry-run"], capsys)
    planned = json.loads(out)["alarm"]
    assert code == 0 and "ID" not in planned and clockfake.sent == []
    code, out, _ = run(argv, capsys)
    payload = json.loads(out)
    assert code == 0 and payload["performed"] is True
    back = alarm_after(clockfake, payload["id"])            # the speaker's ID
    assert payload["id"] not in {a.id for a in ALARMS}
    assert without_id(back.to_attributes()) == planned       # every field, as planned
    assert str(back.recurrence) == spelt == planned["Recurrence"]
    assert back.start_time == "07:15:00" and back.room_uuid == ROAM_L
    assert payload["alarm"] == back.to_attributes()
    assert clockfake.writes == ["CreateAlarm"]
    [entry] = journal()
    assert entry["action"] == "alarm_create" and entry["written"] is True
    assert entry["after"]["attributes"] == clockfake.alarms[payload["id"]]


def test_add_sets_every_alarmclock_field(clockfake, capsys):
    code, out, _ = run(["alarm", "add", "--room", "Living Room", "--time", "06:05:30",
                        "--days", "mon,thu", "--duration", "45m", "--volume", "7",
                        "--mode", "shuffle", "--include-grouped-rooms", "--off",
                        "--source", "chime", "--json"], capsys)
    assert code == 0
    aid = json.loads(out)["id"]
    assert clockfake.alarms[aid] == {
        "ID": aid, "StartTime": "06:05:30", "Duration": "00:45:00", "Recurrence": "ON_14",
        "Enabled": "0", "RoomUUID": LIVING, "ProgramURI": "x-rincon-buzzer:0",
        "ProgramMetaData": "", "PlayMode": "SHUFFLE_NOREPEAT", "Volume": "7",
        "IncludeLinkedZones": "1"}


def test_add_says_what_it_made_and_its_defaults(clockfake, capsys):
    code, out, _ = run(["alarm", "add", "--room", "roam", "--time", "07:15"], capsys)
    assert code == 0
    aid = alarm_after(clockfake, max(clockfake.alarms, key=int)).id
    assert out == (f"created alarm {aid}: Sonos Roam 07:15:00 daily (on, vol 25) Sonos chime\n"
                   "  stops after 2h00, play mode normal, this room only\n")
    assert without_id(clockfake.alarms[aid]) == {
        "StartTime": "07:15:00", "Duration": "02:00:00", "Recurrence": "DAILY",
        "Enabled": "1", "RoomUUID": ROAM_L, "ProgramURI": "x-rincon-buzzer:0",
        "ProgramMetaData": "", "PlayMode": "NORMAL", "Volume": "25",
        "IncludeLinkedZones": "0"}


def test_add_dry_run_prints_the_alarm_and_writes_nothing(clockfake, capsys):
    code, out, _ = run(["alarm", "add", "--room", "roam", "--time", "07:15",
                        "--days", "weekdays", "--dry-run"], capsys)
    assert code == 0
    assert out == ("[dry-run] would create an alarm: Sonos Roam 07:15:00 weekdays "
                   "(on, vol 25) Sonos chime\n"
                   "  stops after 2h00, play mode normal, this room only\n")
    assert clockfake.sent == [] and journal() == []


@pytest.mark.parametrize("room, uuid", [("Sonos Roam (R)", ROAM_L), ("10.0.0.12", ROAM_L),
                                        ("Living Room (LR)", LIVING)])
def test_naming_a_bonded_follower_targets_its_room_and_says_so(clockfake, capsys, room, uuid):
    code, out, _ = run(["alarm", "add", "--room", room, "--time", "07:15", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0 and clockfake.alarms[payload["id"]]["RoomUUID"] == uuid
    assert payload["requested"] == room and "bonded follower" in payload["reason"]
    assert payload["redirected_from"].startswith(room if "." not in room else "Sonos Roam (R)")
    code, out, _ = run(["alarm", "add", "--room", room, "--time", "07:16"], capsys)
    assert "\n  note: " in out and "is a bonded follower; the alarm goes on" in out


def test_naming_the_room_is_not_a_redirect_whichever_unit_resolve_picks(
        clockfake, capsys, monkeypatch):
    house = household()
    house.speakers = [house.speakers[1], house.speakers[0], *house.speakers[2:]]  # R first
    assert house.resolve("Sonos Roam").matched.uuid == ROAM_R
    monkeypatch.setattr(alarm_cli, "_household", lambda args: house)
    code, out, _ = run(["alarm", "add", "--room", "Sonos Roam", "--time", "07:15", "--json"],
                       capsys)
    payload = json.loads(out)
    assert clockfake.alarms[payload["id"]]["RoomUUID"] == ROAM_L
    assert "redirected_from" not in payload and "reason" not in payload
    code, out, _ = run(["alarm", "add", "--room", "Sonos Roam (R)", "--time", "07:15",
                        "--json"], capsys)
    assert "bonded follower" in json.loads(out)["reason"]


def test_a_follower_in_a_room_grouped_elsewhere_targets_its_room_not_the_group(
        clockfake, capsys, monkeypatch):
    house = household()
    lr = house.groups[1].coordinator
    house.groups = [Group("g", lr, [lr, *house.groups[0].members])]
    monkeypatch.setattr(alarm_cli, "_household", lambda args: house)
    code, out, _ = run(["alarm", "add", "--room", "Sonos Roam (R)", "--time", "07:15",
                        "--json"], capsys)
    assert clockfake.alarms[json.loads(out)["id"]]["RoomUUID"] == ROAM_L


@pytest.mark.parametrize("argv, error", [
    (["--time", "25:00"], "can't read '25:00' as a time"),
    (["--time", "7"], "can't read '7' as a time"),
    (["--days", "funday"], "can't read 'funday' as a day"),
    (["--days", "t"], "can't read 't' as a day"),
    (["--days", "ON_7"], "invalid alarm recurrence"),
    (["--volume", "101"], "volume '101' is not 0-100"),
    (["--mode", "loud"], "unknown play mode 'loud'"),
    (["--duration", "forever"], "can't read 'forever' as a duration"),
    (["--duration", "00:75:00"], "can't read '00:75:00' as a duration"),
    (["--duration", "00:00:00"], "can't read '00:00:00' as a duration"),
    (["--duration", "25:00:00"], "can't read '25:00:00' as a duration"),
])
def test_a_value_that_cant_be_read_is_an_error_and_nothing_is_sent(
        clockfake, capsys, argv, error):
    base = {"--time": "07:15"}
    for flag in ("--time",):
        if flag not in argv:
            argv = [flag, base[flag], *argv]
    code, out, _ = run(["alarm", "add", "--room", "roam", *argv, "--json"], capsys)
    assert code == 1 and error in json.loads(out)["error"]
    code, out, _ = run(["alarm", "edit", "2", *argv, "--json"], capsys)
    assert code == 1 and error in json.loads(out)["error"]
    assert clockfake.sent == [] and journal() == []


def test_an_unknown_room_is_an_error(clockfake, capsys):
    code, out, _ = run(["alarm", "add", "--room", "Kitchen", "--time", "07:15", "--json"],
                       capsys)
    payload = json.loads(out)
    assert code == 1 and "no speaker matches 'Kitchen'" in payload["error"]
    assert "Sonos Roam" in payload["known"]
    assert clockfake.writes == []


def test_a_create_whose_reply_was_lost_still_reports_its_id(clockfake, capsys):
    clockfake.lose_reply = {"CreateAlarm"}
    code, out, _ = run(["alarm", "add", "--room", "roam", "--time", "07:15", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0 and payload["performed"] is True and "Timeout" in payload["warning"]
    assert payload["id"] in clockfake.alarms
    assert payload["alarm"] == clockfake.alarms[payload["id"]]
    code, out, _ = run(["alarm", "add", "--room", "roam", "--time", "07:16"], capsys)
    assert out.startswith("created alarm ") and ": Sonos Roam 07:16:00 daily" in out


# Each edit changes one field of alarm 2 and of alarm 66 (each value differs from both).
EDITS = [
    (["--time", "05:30"], {"StartTime": "05:30:00"}),
    (["--days", "weekends"], {"Recurrence": "WEEKENDS"}),
    (["--days", "mon,tue,wed"], {"Recurrence": "ON_123"}),
    (["--duration", "1h30"], {"Duration": "01:30:00"}),
    (["--duration", "none"], {"Duration": ""}),
    (["--volume", "0"], {"Volume": "0"}),
    (["--mode", "shuffle-repeat-one"], {"PlayMode": "SHUFFLE_REPEAT_ONE"}),
    (["--mode", "repeat_one"], {"PlayMode": "REPEAT_ONE"}),
    (["--include-grouped-rooms"], {"IncludeLinkedZones": "1"}),
    (["--room", "Living Room"], {"RoomUUID": LIVING}),
    (["--source", "keep", "--volume", "9"], {"Volume": "9"}),
    ("on/off", None),
]


@pytest.mark.parametrize("aid", ["2", "66"])
@pytest.mark.parametrize("argv, changed", EDITS)
def test_edit_changes_one_field_and_keeps_an_unrecognised_source_byte_for_byte(
        clockfake, capsys, aid, argv, changed):
    before = dict(clockfake.alarms[aid])
    children = list(clockfake.children[aid])
    if argv == "on/off":
        on = before["Enabled"] == "0"
        argv, changed = ["--on" if on else "--off"], {"Enabled": "1" if on else "0"}
    code, out, _ = run(["alarm", "edit", aid, *argv, "--json"], capsys)
    payload = json.loads(out)
    assert code == 0 and payload["performed"] is True
    assert clockfake.alarms[aid] == before | changed       # 66 stays on ROAM_R unless moved
    assert clockfake.alarms[aid]["ProgramURI"] == before["ProgramURI"]
    assert clockfake.alarms[aid]["ProgramMetaData"] == before["ProgramMetaData"]
    assert clockfake.children[aid] == children
    assert payload["before"] == before and payload["alarm"] == clockfake.alarms[aid]
    assert clockfake.writes == ["UpdateAlarm"]
    [entry] = journal()
    assert entry["action"] == "alarm_update" and entry["written"] is True
    assert entry["before"]["attributes"] == before
    assert entry["after"]["attributes"] == before | changed


def test_edit_source_chime_replaces_the_source(clockfake, capsys):
    code, out, _ = run(["alarm", "edit", "2", "--source", "chime"], capsys)
    assert code == 0
    assert out.startswith("updated alarm 2: Sonos Roam 08:20:00 Mon Tue Thu Fri (on, vol 32) "
                          "Sonos chime  (program_uri, program_metadata)")
    assert clockfake.alarms["2"]["ProgramURI"] == "x-rincon-buzzer:0"
    assert clockfake.alarms["2"]["ProgramMetaData"] == ""


def test_edit_to_a_follower_moves_the_alarm_to_its_room_and_says_so(clockfake, capsys):
    code, out, _ = run(["alarm", "edit", "66", "--room", "Sonos Roam (R)"], capsys)
    assert code == 0 and clockfake.alarms["66"]["RoomUUID"] == ROAM_L
    assert "(room_uuid)" in out and "note: Sonos Roam (R) [10.0.0.12] is a bonded follower" in out
    code, out, _ = run(["alarm", "edit", "2", "--room", "Sonos Roam (R)", "--json"], capsys)
    payload = json.loads(out)
    assert payload["performed"] is False and "bonded follower" in payload["reason"]


def test_edit_several_fields_at_once(clockfake, capsys):
    before = dict(clockfake.alarms["34"])
    code, out, _ = run(["alarm", "edit", "34", "--time", "06:00", "--volume", "15", "--on",
                        "--mode", "normal", "--json"], capsys)
    payload = json.loads(out)
    assert payload["fields"] == ["start_time", "enabled", "play_mode", "volume"]
    assert clockfake.alarms["34"] == before | {"StartTime": "06:00:00", "Volume": "15",
                                               "Enabled": "1", "PlayMode": "NORMAL"}


def test_edit_dry_run_prints_the_change_and_writes_nothing(clockfake, capsys):
    code, out, _ = run(["alarm", "edit", "2", "--volume", "10", "--dry-run"], capsys)
    assert code == 0
    assert out.startswith("[dry-run] would update alarm 2: Sonos Roam 08:20:00 Mon Tue Thu Fri "
                          "(on, vol 10) KQED  (volume)\n  stops after 2h00, play mode repeat,")
    code, out, _ = run(["alarm", "edit", "2", "--volume", "10", "--dry-run", "--json"], capsys)
    payload = json.loads(out)
    assert payload["alarm"] == clockfake.alarms["2"]
    assert payload["would_be"] == clockfake.alarms["2"] | {"Volume": "10"}
    assert payload["fields"] == ["volume"] and payload["performed"] is False
    assert clockfake.sent == ["ListAlarms"] * 2 and journal() == []


def test_edit_with_nothing_to_change(clockfake, capsys):
    code, out, _ = run(["alarm", "edit", "2", "--json"], capsys)
    assert code == 1 and json.loads(out)["error"] == "nothing to change"
    code, out, _ = run(["alarm", "edit", "2", "--source", "keep", "--json"], capsys)
    assert code == 1 and json.loads(out)["error"] == "nothing to change"
    assert clockfake.sent == []
    code, out, _ = run(["alarm", "edit", "2", "--volume", "32", "--days", "ON_1245", "--json"],
                       capsys)
    assert code == 0 and json.loads(out)["performed"] is False
    assert clockfake.writes == [] and journal() == []


def test_edit_refuses_if_the_list_moved_since_it_was_read(clockfake, capsys):
    reads = []

    def app_edit(f):
        reads.append(1)
        if len(reads) == 1:
            f.edit_in_app("11", Volume="4")
    clockfake.after_read = app_edit
    code, out, _ = run(["alarm", "edit", "2", "--volume", "10", "--json"], capsys)
    payload = json.loads(out)
    assert code == 1 and "nothing written" in payload["error"]
    assert clockfake.alarms["2"]["Volume"] == "32" and clockfake.alarms["11"]["Volume"] == "4"
    assert clockfake.writes == [] and journal() == []


def test_an_unknown_alarm_cant_be_edited(clockfake, capsys):
    code, out, _ = run(["alarm", "edit", "999", "--volume", "5", "--json"], capsys)
    assert code == 1 and json.loads(out)["error"] == "no alarm 999"


@pytest.mark.parametrize("text, want", [
    ("7:15", "07:15:00"), ("07:15", "07:15:00"), ("23:59:59", "23:59:59"), ("0:00", "00:00:00")])
def test_parse_time(text, want):
    assert alarm_cli.parse_time(text) == want


@pytest.mark.parametrize("text, want", [
    ("1h", "01:00:00"), ("30m", "00:30:00"), ("90", "01:30:00"), ("1h30", "01:30:00"),
    ("1:30", "01:30:00"), ("01:30:15", "01:30:15"), ("none", ""), ("0", "")])
def test_parse_duration(text, want):
    assert alarm_cli.parse_duration(text) == want


def test_play_mode_names_follow_the_speakers_meaning():
    assert alarm_cli.PLAY_MODES == {
        "normal": "NORMAL", "repeat": "REPEAT_ALL", "repeat-one": "REPEAT_ONE",
        "shuffle": "SHUFFLE_NOREPEAT", "shuffle-repeat": "SHUFFLE",
        "shuffle-repeat-one": "SHUFFLE_REPEAT_ONE"}


def test_edit_json_room_is_where_the_alarm_was_and_to_room_where_it_goes(clockfake, capsys):
    for dry in (["--dry-run"], []):
        code, out, _ = run(["alarm", "edit", "66", "--room", "Living Room", "--json", *dry],
                           capsys)
        payload = json.loads(out)
        assert code == 0 and payload["performed"] is not bool(dry)
        assert payload["room"] == "Sonos Roam" and payload["to_room"] == "Living Room"
    assert clockfake.alarms["66"]["RoomUUID"] == LIVING


def dens(grouped, north_first):
    north = Speaker(ip="10.0.0.21", uuid="RINCON_DEN_N", name="Den North", room="Den North",
                    group_id="d", is_coordinator=True)
    south = Speaker(ip="10.0.0.22", uuid="RINCON_DEN_S", name="Den South", room="Den South",
                    group_id="d" if grouped else "s", is_coordinator=not grouped)
    pair = [north, south] if north_first else [south, north]
    groups = ([Group("d", north, pair)] if grouped
              else [Group("d", north, [north]), Group("s", south, [south])])
    return Household(pair, groups, Topology(groups={}, vanished=[]))


@pytest.mark.parametrize("grouped", [True, False])
@pytest.mark.parametrize("north_first", [True, False])
def test_a_name_matching_two_rooms_is_ambiguous_even_when_they_are_grouped(
        clockfake, capsys, monkeypatch, grouped, north_first):
    house = dens(grouped, north_first)
    monkeypatch.setattr(alarm_cli, "_household", lambda args: house)
    for argv in (["add", "--time", "07:15"], ["edit", "2"]):
        code, out, _ = run(["alarm", *argv, "--room", "Den", "--json"], capsys)
        payload = json.loads(out)
        assert code == 1 and "matches more than one target" in payload["error"]
        assert payload["candidates"] == ["Den North", "Den South"]
    code, out, _ = run(["alarm", "add", "--time", "07:15", "--room", "Den South", "--json"],
                       capsys)
    assert clockfake.alarms[json.loads(out)["id"]]["RoomUUID"] == "RINCON_DEN_S"
    assert clockfake.writes == ["CreateAlarm"]


@pytest.mark.parametrize("north_first", [True, False])
def test_two_unbonded_units_sharing_a_room_name_are_two_rooms(
        clockfake, capsys, monkeypatch, north_first):
    house = dens(True, north_first)
    for sp in house.speakers:
        sp.name = sp.room = "Den"
    house.topology = Topology(groups={"d": [Member(uuid=s.uuid, ip=s.ip, zone_name="Den",
                                                   software="") for s in house.speakers]},
                              vanished=[])
    monkeypatch.setattr(alarm_cli, "_household", lambda args: house)
    for argv in (["add", "--time", "07:15"], ["edit", "2"]):
        code, out, _ = run(["alarm", *argv, "--room", "Den", "--json"], capsys)
        payload = json.loads(out)
        assert code == 1 and "matches more than one target" in payload["error"]
        assert payload["candidates"] == ["Den [10.0.0.21]", "Den [10.0.0.22]"]
        assert "by its IP" in payload["hint"]
    for ip, uuid in (("10.0.0.21", "RINCON_DEN_N"), ("10.0.0.22", "RINCON_DEN_S")):
        code, out, _ = run(["alarm", "add", "--time", "07:15", "--room", ip, "--json"], capsys)
        payload = json.loads(out)
        assert clockfake.alarms[payload["id"]]["RoomUUID"] == uuid
        assert "redirected_from" not in payload
    got = {r["room_uuid"]: r["status"] for r in alarm_cli.listing(
        house, [extra("RINCON_DEN_N", "1"), extra("RINCON_DEN_S", "2")], SAT_AFTERNOON)}
    assert got == {"RINCON_DEN_N": "ok", "RINCON_DEN_S": "ok"}


def den_theatre(other_first, maps=True):
    """A soundbar and its surround, and another unbonded speaker called Den,
    grouped. The surround's HTSatChanMapSet is only on the soundbar's member."""
    beam = Speaker(ip="10.0.0.31", uuid="RINCON_BEAM", name="Den", room="Den",
                   group_id="d", is_coordinator=not other_first, channel="LF,RF" if maps else "")
    other = Speaker(ip="10.0.0.32", uuid="RINCON_OTHER", name="Den", room="Den",
                    group_id="d", is_coordinator=other_first)
    sur = Speaker(ip="10.0.0.33", uuid="RINCON_SUR", name="Den Surround", room="Den",
                  group_id="d", invisible=True, is_satellite=True, channel="LR" if maps else "")
    units = [other, beam, sur] if other_first else [beam, other, sur]
    sat_map = "RINCON_BEAM:LF,RF;RINCON_SUR:LR" if maps else ""
    topo = Topology(groups={"d": [
        Member(uuid=beam.uuid, ip=beam.ip, zone_name="Den", software="", sat_chan_map=sat_map),
        Member(uuid=other.uuid, ip=other.ip, zone_name="Den", software=""),
        Member(uuid=sur.uuid, ip=sur.ip, zone_name="Den", software="", is_satellite=True,
               invisible=True)]}, vanished=[])
    coord = other if other_first else beam
    return Household(units, [Group("d", coord, units)], topo)


@pytest.mark.parametrize("other_first", [True, False])
def test_a_surround_goes_to_the_soundbar_whose_map_names_it(
        clockfake, capsys, monkeypatch, other_first):
    house = den_theatre(other_first)
    monkeypatch.setattr(alarm_cli, "_household", lambda args: house)
    code, out, _ = run(["alarm", "add", "--time", "07:15", "--room", "Den Surround", "--json"],
                       capsys)
    payload = json.loads(out)
    assert code == 0 and clockfake.alarms[payload["id"]]["RoomUUID"] == "RINCON_BEAM"
    assert payload["redirected_from"] == "Den Surround [10.0.0.33]"
    code, out, _ = run(["alarm", "edit", "2", "--room", "Den Surround", "--json"], capsys)
    assert code == 0 and clockfake.alarms["2"]["RoomUUID"] == "RINCON_BEAM"
    assert alarm_cli.aimed_at(house, "RINCON_OTHER")["status"] == "ok"


@pytest.mark.parametrize("other_first", [True, False])
def test_a_surround_no_map_names_is_ambiguous_between_two_units_of_its_room(
        clockfake, capsys, monkeypatch, other_first):
    house = den_theatre(other_first, maps=False)
    monkeypatch.setattr(alarm_cli, "_household", lambda args: house)
    for argv in (["add", "--time", "07:15"], ["edit", "2"]):
        code, out, _ = run(["alarm", *argv, "--room", "Den Surround", "--json"], capsys)
        payload = json.loads(out)
        assert code == 1 and "matches more than one target" in payload["error"]
        assert payload["candidates"] == ["Den [10.0.0.31]", "Den [10.0.0.32]"]
    assert clockfake.writes == []


# ---- writes refuse while an alarm can't be read --------------------------------

def _break_one(f):
    f.alarms["80"], f.children["80"] = dict(BAD["80"]), []


@pytest.mark.parametrize("argv, doing", [
    (["add", "--room", "Living Room", "--time", "06:05"], "add an alarm"),
    (["edit", "2", "--volume", "30"], "edit alarm 2"),
    (["rm", "2"], "delete alarm 2"),
    (["enable", "66"], "enable alarm 66"),
    (["disable", "2"], "disable alarm 2"),
    (["try", "34"], "fire alarm 34"),
    (["snapshot"], "take a snapshot"),
    (["restore"], "restore"),
])
def test_every_verb_that_reads_alarms_refuses_while_one_cant_be_read(
        clockfake, capsys, monkeypatch, argv, doing):
    # Even aimed at a readable alarm: the list it would check against has a
    # hole in it, and the change could land unseen.
    run(["alarm", "snapshot"], capsys)                     # for restore
    saved = alarm_cli.SNAPSHOT_FILE.read_text()
    clockfake.alarms.pop("1"), clockfake.children.pop("1")  # so restore has work to do
    _break_one(clockfake)
    asked = answers(monkeypatch, "y", "2")
    code, out, _ = run(["alarm", *argv, "--json"], capsys)
    assert code == 1
    payload = json.loads(out)
    assert payload["error"] == (f"twiddle can't read alarm 80 ({BAD_REASONS['80']}: "
                                "expected ONCE, DAILY, WEEKDAYS, WEEKENDS or ON_<days> "
                                f"(1-7 digits 0-6, Sunday 0)), so it won't {doing}: "
                                "nothing written")
    assert [u["id"] for u in payload["unreadable"]] == ["80"]
    assert asked == []
    assert clockfake.writes == [] and journal() == []
    assert alarm_cli.SNAPSHOT_FILE.read_text() == saved      # not overwritten


def test_restore_refuses_a_snapshot_holding_an_alarm_it_cant_read(clockfake, capsys):
    _break_one(clockfake)
    alarm_cli.SNAPSHOT_FILE.parent.mkdir(parents=True)
    alarm_cli.SNAPSHOT_FILE.write_text(json.dumps(
        {"taken_utc": "2026-10-04T12:00:00+00:00", "version": clockfake.version,
         "alarm_list": clockfake.document()}))
    clockfake.alarms.pop("80"), clockfake.children.pop("80")   # the household is fine now
    code, out, _ = run(["alarm", "restore", "--json"], capsys)
    assert code == 1
    assert "can't read alarm 80" in json.loads(out)["error"]
    assert clockfake.writes == [] and journal() == []

