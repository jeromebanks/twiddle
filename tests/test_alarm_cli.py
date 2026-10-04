"""`twiddle alarm list`: every alarm, under its room, and nothing written.

The alarms are T1.1's recorded (anonymised) ListAlarms; the household around
them is built here. The end-to-end tests answer at the HTTP layer, so the real
SOAP envelopes are built and every action the command sends is seen.
"""
import json
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

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


def test_a_malformed_alarm_list_is_an_error_not_a_traceback(speaker, monkeypatch, capsys):
    monkeypatch.setitem(ANSWERS, "ListAlarms", _envelope(
        "ListAlarms", f"<CurrentAlarmList>{escape('<Alarms><Alarm ID=\"1\"/></Alarms>')}"
                      "</CurrentAlarmList>"))
    code, out, _ = run(["alarm", "list", "--json"], capsys)
    assert code == 1
    assert "alarm 1: missing" in json.loads(out)["error"]


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
    assert "stopped: HTTPError" in out
    assert "STILL DIFFERENT: create  Sonos Roam 08:20:00" in out


def test_restore_without_a_snapshot_is_an_error(clockfake, capsys):
    code, out, _ = run(["alarm", "restore", "--json"], capsys)
    assert code == 1 and "no alarm snapshot" in json.loads(out)["error"]
    assert clockfake.sent == []
