"""The AlarmClock writes, and restoring the alarms to a snapshot.

`FakeClock` stands in for a speaker at the HTTP layer: it reads each SOAP
envelope with a real XML parser, keeps the decoded arguments, and answers
ListAlarms the way a speaker does (the `<Alarms>` document escaped into
`CurrentAlarmList`). Like a speaker it assigns a fresh ID on every create and
moves the list version on every write. So the escaping is tested end to end:
a value escaped twice or not at all comes back different.
"""
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
import requests

from twiddle import alarm_cli, cli, devices, play, report
from twiddle.alarms import baseline, clock, model
from twiddle.alarms.model import Alarm, Recurrence

from tests.test_alarm_cli import (ALARMS, ALARMS_XML, BAD, GONE, ROAM_L,
                                  ROAM_R, household)

IP = "10.0.0.11"
_ORDER = ("ID", "StartTime", "Duration", "Recurrence", "Enabled", "RoomUUID",
          "ProgramURI", "ProgramMetaData", "PlayMode", "Volume", "IncludeLinkedZones")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class FakeClock:
    """A household's AlarmClock: the fixture's alarms, then whatever is written."""

    def __init__(self, alarms_xml=ALARMS_XML):
        self.alarms: dict[str, dict] = {}
        self.children: dict[str, list[str]] = {}
        for el in ET.fromstring(alarms_xml).iter("Alarm"):
            self.alarms[el.get("ID")] = dict(el.attrib)
            self.children[el.get("ID")] = [ET.tostring(c, encoding="unicode") for c in el]
        self.n = 108
        self.next_id = 200
        self.sent: list[str] = []          # every action, reads included
        self.refuse_rooms: set[str] = set()
        self.after_write = None            # called after each write (an app edit, say)
        self.after_read = None             # called after answering each ListAlarms
        self.lose_reply: set[str] = set()  # do these, then time out answering

    @property
    def version(self) -> str:
        return f"{ROAM_L}:{self.n}"

    @property
    def writes(self) -> list[str]:
        return [a for a in self.sent if a in clock.WRITES]

    def edit_in_app(self, alarm_id="1", **attrs):
        """Someone changes an alarm in the Sonos app."""
        self.alarms[alarm_id].update(attrs)
        self.n += 1

    def document(self) -> str:
        root = ET.Element("Alarms")
        for aid, attrs in self.alarms.items():
            el = ET.SubElement(root, "Alarm", {k: attrs[k] for k in _ORDER if k in attrs}
                               | {k: v for k, v in attrs.items() if k not in _ORDER})
            for c in self.children[aid]:
                el.append(ET.fromstring(c))
        return ET.tostring(root, encoding="unicode")

    def post(self, url, data=None, headers=None, timeout=None):
        body = ET.fromstring(data).find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
        call = body[0]
        action = _local(call.tag)
        args = {_local(c.tag): c.text or "" for c in call}
        self.sent.append(action)
        out = getattr(self, action)(args)
        if action in clock.WRITES:
            self.n += 1
            if self.after_write:
                self.after_write(self)
        elif self.after_read:
            self.after_read(self)
        if action in self.lose_reply:
            raise requests.Timeout("read timed out")
        inner = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in out.items())
        return Reply(f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                     f'<s:Body><u:{action}Response xmlns:u="urn:schemas-upnp-org:service:'
                     f'AlarmClock:1">{inner}</u:{action}Response></s:Body></s:Envelope>')

    def ListAlarms(self, args):
        return {"CurrentAlarmList": self.document(), "CurrentAlarmListVersion": self.version}

    def _attrs(self, args, aid):
        a = dict(args)
        a["StartTime"] = a.pop("StartLocalTime")
        return {"ID": aid} | {k: a[k] for k in _ORDER if k in a}

    def CreateAlarm(self, args):
        if args["RoomUUID"] in self.refuse_rooms:
            return Reply.fault()
        aid = str(self.next_id)
        self.next_id += 1
        self.alarms[aid] = self._attrs(args, aid)
        self.children[aid] = []            # CreateAlarm has no argument for them
        return {"AssignedID": aid}

    def UpdateAlarm(self, args):
        aid = args.pop("ID")
        self.alarms[aid] = self._attrs(args, aid)
        return {}

    def DestroyAlarm(self, args):
        del self.alarms[args["ID"]]
        del self.children[args["ID"]]
        return {}


class Reply:
    def __init__(self, text):
        self.text = text

    @staticmethod
    def fault():
        raise requests.HTTPError("500 Server Error: UPnPError 402")

    def raise_for_status(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    f = FakeClock()
    monkeypatch.setattr(devices.requests, "post", f.post)
    return f


def journal() -> list[dict]:
    if not play.INTERVENTION_LOG.exists():
        return []
    return [json.loads(line) for line in play.INTERVENTION_LOG.read_text().splitlines()]


def new_alarm(**kw) -> Alarm:
    return Alarm(start_time="06:30:00", recurrence=Recurrence.parse("WEEKDAYS"),
                 room_uuid=ROAM_L, volume=20, **kw)


def keys(alarms) -> list:
    return sorted(map(baseline.key, alarms), key=repr)


# ---- every write is checked against the version it was read at ---------------

@pytest.mark.parametrize("write", [
    lambda v: clock.create_alarm(IP, new_alarm(), v),
    lambda v: clock.update_alarm(IP, ALARMS[0], v),
    lambda v: clock.destroy_alarm(IP, "1", v),
])
def test_a_write_after_the_list_moved_is_refused_and_nothing_is_written(fake, write):
    read = clock.list_alarms(IP)
    fake.edit_in_app("1", Volume="11")
    with pytest.raises(clock.VersionChanged) as exc:
        write(read.version)
    assert fake.writes == []
    assert fake.sent == ["ListAlarms", "ListAlarms"]          # the list was re-read
    assert exc.value.current.version == fake.version != read.version
    assert exc.value.current.get("1").volume == 11
    assert journal() == []


def test_the_clock_refuses_anything_but_a_write_through_write():
    with pytest.raises(ValueError, match="not an AlarmClock write"):
        clock._write(IP, "RunAlarm", [])


# ---- every write is journalled with the alarm before and after ---------------

def test_create_gets_a_fresh_id_and_journals_the_alarm_after(fake):
    made, now = clock.create_alarm(IP, new_alarm(id="1"), clock.list_alarms(IP).version)
    assert made.id == "200"
    assert baseline.key(made) == baseline.key(new_alarm())
    assert now.version == fake.version
    assert fake.writes == ["CreateAlarm"]
    [rec] = journal()
    assert rec["action"] == "alarm_create" and rec["ip"] == IP
    assert rec["alarm_id"] == "200"
    assert rec["before"] is None
    assert rec["after"]["attributes"] == made.to_attributes()
    assert rec["written"] is True and "error" not in rec


def test_update_journals_the_alarm_before_and_after(fake):
    old = next(a for a in ALARMS if a.id == "68")
    changed = Alarm(**{**vars(old), "volume": 9, "enabled": False})
    after, _ = clock.update_alarm(IP, changed, clock.list_alarms(IP).version)
    assert after.volume == 9 and not after.enabled
    assert after.program_metadata == old.program_metadata       # bytes survive the wire
    [rec] = journal()
    assert rec["action"] == "alarm_update" and rec["alarm_id"] == "68"
    assert rec["before"]["attributes"] == old.to_attributes()
    assert rec["after"]["attributes"]["Volume"] == "9"


def test_destroy_journals_the_whole_alarm_and_the_journal_can_recreate_it(fake):
    gone = next(a for a in ALARMS if a.id == "66")
    now = clock.destroy_alarm(IP, "66", clock.list_alarms(IP).version)
    assert now.get("66") is None
    [rec] = journal()
    assert rec["action"] == "alarm_destroy" and rec["after"] is None
    el = ET.Element("Alarm", rec["before"]["attributes"])
    for c in rec["before"]["children"]:
        el.append(ET.fromstring(c))
    assert Alarm.from_element(el) == gone
    again, _ = clock.create_alarm(IP, Alarm.from_element(el), now.version)
    assert baseline.key(again) == baseline.key(gone)


def test_a_failed_write_is_still_journalled(fake):
    fake.refuse_rooms.add(ROAM_L)
    with pytest.raises(clock.AlarmWriteError) as exc:
        clock.create_alarm(IP, new_alarm(), clock.list_alarms(IP).version)
    assert exc.value.landed is False and isinstance(exc.value.__cause__, requests.HTTPError)
    [rec] = journal()
    assert rec["action"] == "alarm_create"
    assert "UPnPError" in rec["error"] and rec["sent"]["RoomUUID"] == ROAM_L
    assert rec["written"] is False


def test_a_failed_read_back_is_journalled_as_written(fake, monkeypatch):
    v = clock.list_alarms(IP).version
    real = clock.list_alarms
    monkeypatch.setattr(clock, "list_alarms",
                        lambda ip: real(ip) if not fake.writes else 1 / 0)
    with pytest.raises(clock.AlarmWriteError) as exc:
        clock.create_alarm(IP, new_alarm(), v)
    assert exc.value.landed is True and exc.value.alarm_id == "200"
    [rec] = journal()
    assert rec["written"] is True and rec["alarm_id"] == "200"
    assert "ZeroDivisionError" in rec["error"]


def _interrupt_read_back(fake, monkeypatch):
    """Ctrl-C at the first read after a write; returns a function that undoes it."""
    real = clock.list_alarms

    def read(ip):
        if fake.writes:
            raise KeyboardInterrupt
        return real(ip)
    monkeypatch.setattr(clock, "list_alarms", read)
    return lambda: monkeypatch.setattr(clock, "list_alarms", real)


def test_ctrl_c_in_a_destroys_read_back_still_journals_it_as_written(fake, monkeypatch):
    gone = next(a for a in ALARMS if a.id == "66")
    v = clock.list_alarms(IP).version
    uninterrupt = _interrupt_read_back(fake, monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        clock.destroy_alarm(IP, "66", v)
    [rec] = journal()
    assert rec["action"] == "alarm_destroy" and rec["written"] is True
    assert rec["before"] and clock.deleted("66") == gone
    uninterrupt()
    again, _, _ = clock.recreate(IP, "66", clock.list_alarms(IP).version)
    assert baseline.key(again) == baseline.key(gone)


@pytest.mark.parametrize("write", [
    lambda v: clock.create_alarm(IP, new_alarm(), v),
    lambda v: clock.update_alarm(IP, ALARMS[0], v),
])
def test_ctrl_c_in_a_create_or_updates_read_back_journals_written(fake, monkeypatch, write):
    v = clock.list_alarms(IP).version
    _interrupt_read_back(fake, monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        write(v)
    [rec] = journal()
    assert rec["written"] is True
    assert rec["alarm_id"] == ("200" if rec["action"] == "alarm_create" else "1")


def test_ctrl_c_while_sending_a_write_journals_it_as_unknown(fake, monkeypatch):
    v = clock.list_alarms(IP).version

    def interrupted(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(clock, "_write", interrupted)
    with pytest.raises(KeyboardInterrupt):
        clock.destroy_alarm(IP, "66", v)
    [rec] = journal()
    assert rec["written"] is None
    assert clock.deleted("66") is None


def test_unknown_ids_are_refused_before_writing(fake):
    v = clock.list_alarms(IP).version
    with pytest.raises(KeyError):
        clock.destroy_alarm(IP, "999", v)
    with pytest.raises(KeyError):
        clock.update_alarm(IP, new_alarm(id="999"), v)
    assert fake.writes == [] and journal() == []


# ---- planning a restore -----------------------------------------------------

def snapshot(fake) -> baseline.AlarmSnapshot:
    return baseline.AlarmSnapshot.of(clock.list_alarms(IP))


def test_an_unchanged_household_needs_nothing(fake):
    snap = snapshot(fake)
    assert baseline.plan(snap.alarms, clock.list_alarms(IP).alarms) == []


def test_the_snapshot_keeps_the_alarm_list_exactly(fake, tmp_path):
    snap = snapshot(fake)
    again = baseline.AlarmSnapshot.load(snap.save(tmp_path / "alarms.json"))
    assert again == snap
    assert again.alarms == clock.list_alarms(IP).alarms
    assert again.version == fake.version


def test_plan_updates_by_id_creates_the_missing_and_destroys_last(fake):
    snap = snapshot(fake)
    current = [a for a in snap.alarms if a.id != "2"]
    current[0] = Alarm(**{**vars(current[0]), "volume": 5})
    current.append(new_alarm(id="300"))
    ops = [(c.op, (c.want or c.have).id, c.fields) for c in baseline.plan(snap.alarms, current)]
    assert ops == [("update", "1", ["volume"]), ("create", "2", []), ("destroy", "300", [])]


def test_an_equal_alarm_under_another_id_is_left_alone(fake):
    """After a restore recreated alarm 2 as 200, restoring again does nothing."""
    snap = snapshot(fake)
    current = [Alarm(**{**vars(a), "id": "200"}) if a.id == "2" else a for a in snap.alarms]
    assert baseline.plan(snap.alarms, current) == []


def test_recurrence_is_compared_by_days():
    a = new_alarm(id="1")
    b = Alarm(**{**vars(a), "recurrence": Recurrence.parse("ON_12345")})
    assert baseline.differences(a, b) == []


# ---- restoring ----------------------------------------------------------------

def test_restore_makes_the_household_match_the_baseline(fake):
    snap = snapshot(fake)
    v = fake.version
    v = clock.destroy_alarm(IP, "66", v).version       # the &apos; and <Content> one
    v = clock.destroy_alarm(IP, "2", v).version        # an &amp; one
    _, now = clock.update_alarm(IP, Alarm(**{**vars(ALARMS[0]), "volume": 3,
                                             "start_time": "09:00:00"}), v)
    _, now = clock.create_alarm(IP, new_alarm(), now.version)
    before = len(journal())

    out = baseline.restore(IP, snap, clock.list_alarms(IP))

    assert out.ok, (out.error, out.left)
    final = clock.list_alarms(IP)
    assert keys(final.alarms) == keys(snap.alarms)
    for old, new in out.id_map.items():
        want = next(a for a in snap.alarms if a.id == old)
        got = final.get(new)
        assert got.program_uri == want.program_uri
        assert got.program_metadata == want.program_metadata    # byte-for-byte
    assert set(out.id_map) == {"66", "2"}
    assert [d["op"] for d in out.done] == ["update", "create", "create", "destroy"]
    # The recreated Spotify alarm lost its <Content>: said, not hidden, not failed.
    assert any("child elements" in n and f"alarm {out.id_map['66']}" in n for n in out.notes)
    recs = journal()[before:]
    assert [r["action"] for r in recs] == ["alarm_update", "alarm_create", "alarm_create",
                                           "alarm_destroy", "alarm_restore"]
    assert recs[-1]["id_map"] == out.id_map and recs[-1]["left"] == 0
    # and again: nothing to do
    assert baseline.plan(snap.alarms, final.alarms) == []


def test_a_restore_that_cannot_finish_reports_what_is_left(fake):
    snap = snapshot(fake)
    clock.destroy_alarm(IP, "1", fake.version)
    clock.destroy_alarm(IP, "2", fake.version)
    fake.refuse_rooms.add(ROAM_L)      # a speaker that rejects them both

    out = baseline.restore(IP, snap, clock.list_alarms(IP))

    assert not out.ok
    assert "UPnPError" in out.error
    assert sorted(c.want.id for c in out.left) == ["1", "2"]
    assert all(c.op == "create" for c in out.left)
    assert journal()[-1]["action"] == "alarm_restore" and journal()[-1]["left"] == 2


def test_someone_editing_during_a_restore_stops_it(fake):
    snap = snapshot(fake)
    clock.destroy_alarm(IP, "1", fake.version)
    clock.destroy_alarm(IP, "2", fake.version)
    reads = []

    def app_edit(f):               # after the first create's read-back, before the next
        reads.append(1)
        if len(reads) == 3:
            f.edit_in_app("3", Volume="1")
    fake.after_read = app_edit

    out = baseline.restore(IP, snap, clock.list_alarms(IP))

    assert "VersionChanged" in out.error
    assert len(out.done) == 1 and fake.writes.count("CreateAlarm") == 1
    assert {(c.op, c.want.id) for c in out.left} == {("create", "2"), ("update", "3")}
    assert journal()[-1]["id_map"] == out.id_map != {}


def test_an_edit_between_a_write_and_its_read_back_stops_the_restore(fake):
    """The app edits an alarm still waiting for its update, after restore's
    first write and before that write's read-back: the read-back shows more
    than our write changed, so restore stops instead of overwriting it."""
    snap = snapshot(fake)
    clock.destroy_alarm(IP, "1", fake.version)
    clock.update_alarm(IP, Alarm(**{**vars(ALARMS[2]), "volume": 5}), fake.version)

    def app_edit(f):
        f.after_write = None
        f.edit_in_app("3", Volume="99")
    fake.after_write = app_edit

    out = baseline.restore(IP, snap, clock.list_alarms(IP))

    assert "VersionChanged" in out.error and "alarms 3 changed" in out.error
    assert fake.writes.count("UpdateAlarm") == 1          # only the setup's
    assert clock.list_alarms(IP).get("3").volume == 99    # not overwritten
    assert [d["op"] for d in out.done] == ["create"] and out.id_map == {"1": "200"}
    assert [(c.op, c.have.id) for c in out.left] == [("update", "3")]
    create = next(r for r in journal() if r["action"] == "alarm_create")
    assert create["others_changed"] == ["3"] and create["written"] is True


@pytest.mark.parametrize("action", ["CreateAlarm", "DestroyAlarm"])
def test_a_write_whose_answer_is_lost_is_reconciled_from_the_list(fake, action):
    """A timeout after the speaker acted: the read-back shows the write landed."""
    fake.lose_reply = {action}
    v = clock.list_alarms(IP).version
    with pytest.raises(clock.AlarmWriteError) as exc:
        if action == "CreateAlarm":
            clock.create_alarm(IP, new_alarm(), v)
        else:
            clock.destroy_alarm(IP, "1", v)
    assert exc.value.landed is True
    [rec] = journal()
    assert rec["written"] is True and "Timeout" in rec["error"]
    if action == "CreateAlarm":
        assert exc.value.alarm_id == rec["alarm_id"] == "200"
        assert rec["after"]["attributes"]["ID"] == "200"
    else:
        assert rec["alarm_id"] == "1" and rec["after"] is None


def test_restore_counts_a_create_whose_answer_was_lost(fake):
    snap = snapshot(fake)
    clock.destroy_alarm(IP, "2", fake.version)
    fake.lose_reply = {"CreateAlarm"}

    out = baseline.restore(IP, snap, clock.list_alarms(IP))

    assert "Timeout" in out.error
    assert out.id_map == {"2": "200"} and out.left == []
    assert journal()[-1]["id_map"] == {"2": "200"}


def test_restore_maps_a_create_whose_answer_and_read_back_were_both_lost(fake):
    snap = snapshot(fake)
    clock.destroy_alarm(IP, "2", fake.version)
    fake.lose_reply = {"CreateAlarm"}
    reads = []

    def lose_read_back(f):         # restore's first read, its pre-check, then the read-back
        reads.append(1)
        if len(reads) == 3:
            raise requests.Timeout("read timed out")
    fake.after_read = lose_read_back

    out = baseline.restore(IP, snap, clock.list_alarms(IP))

    assert "Timeout" in out.error
    create = next(r for r in journal() if r["action"] == "alarm_create")
    assert create["written"] is None and create["alarm_id"] is None
    assert out.id_map == {"2": "200"} and out.left == []
    assert len(out.done) == 1 and out.done[0]["new_id"] == "200"
    assert out.done[0]["recovered"] and out.done[0]["uncertain"]
    assert journal()[-1]["id_map"] == {"2": "200"}


def test_an_alarm_someone_else_recreates_is_not_credited_to_restore(fake):
    """Alarm 2 is missing; the app recreates it after restore has read the
    list. The create is refused, nothing is written, and restore claims none
    of it."""
    snap = snapshot(fake)
    clock.destroy_alarm(IP, "2", fake.version)
    read = clock.list_alarms(IP)
    fake.alarms["201"] = dict(next(a for a in snap.alarms if a.id == "2").to_attributes(),
                              ID="201")
    fake.children["201"] = []
    fake.n += 1
    before = len(journal())

    out = baseline.restore(IP, snap, read)

    assert "nothing written" in out.error and fake.writes.count("CreateAlarm") == 0
    assert out.done == [] and out.id_map == {} and out.left == []
    [rec] = journal()[before:]
    assert rec["action"] == "alarm_restore" and rec["changes"] == 0 and rec["id_map"] == {}


# ---- AVTransport: ringing, try, stop, snooze ----------------------------------
#
# Recorded 2026-10-04: GetRunningAlarmProperties' fault with nothing ringing
# (a Roam and a Beam answered the same), the Roam's initial AVTransport
# NOTIFY, trimmed to three of its variables but otherwise as sent, and
# GetRunningAlarmProperties while alarm 34 rang after a real `alarm try 34`
# (tests/fixtures/avtransport_running_alarm.xml, anonymised).

SCPD = Path(__file__).parent / "fixtures" / "avtransport_alarm_scpd.xml"
FAULT_800 = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
             's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
             '<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>'
             '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>800</errorCode>'
             '</UPnPError></detail></s:Fault></s:Body></s:Envelope>')
NOTIFY = ('<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0"><e:property><LastChange>'
          '&lt;Event xmlns=&quot;urn:schemas-upnp-org:metadata-1-0/AVT/&quot; '
          'xmlns:r=&quot;urn:schemas-rinconnetworks-com:metadata-1-0/&quot;&gt;'
          '&lt;InstanceID val=&quot;0&quot;&gt;&lt;TransportState val=&quot;STOPPED&quot;/&gt;'
          '&lt;r:AlarmRunning val=&quot;0&quot;/&gt;&lt;r:SnoozeRunning val=&quot;0&quot;/&gt;'
          '&lt;/InstanceID&gt;&lt;/Event&gt;</LastChange></e:property></e:propertyset>')
RUNNING_XML = (Path(__file__).parent / "fixtures" / "avtransport_running_alarm.xml").read_text()
RUNNING = clock._response(RUNNING_XML, "GetRunningAlarmProperties")
RINGING = {"AlarmRunning": "1", "SnoozeRunning": "0"}      # its LastChange, as read
ROAM_IP, LIVING_IP = "10.0.0.11", "10.0.0.13"
REAL_LAST_CHANGE = clock.last_change          # before any fixture replaces it


def scpd_in_args(direction: str = "in") -> dict[str, list[str]]:
    """Each action's in- (or out-) arguments, in the speaker's own order."""
    ns = {"u": "urn:schemas-upnp-org:service-1-0"}
    out = {}
    for a in ET.parse(SCPD).getroot().iterfind(".//u:action", ns):
        out[a.findtext("u:name", namespaces=ns)] = [
            arg.findtext("u:name", namespaces=ns)
            for arg in a.iterfind(".//u:argument", ns)
            if arg.findtext("u:direction", namespaces=ns) == direction]
    return out


class FaultReply:
    def __init__(self, text):
        self.text = text


class FakeTransport(FakeClock):
    """The household's AlarmClock as above, plus each group coordinator's
    AVTransport: what's going off where, and every call it is sent."""

    def __init__(self):
        super().__init__()
        self.running: dict[str, dict] = {}          # ip -> GetRunningAlarmProperties
        self.state: dict[str, dict] = {}            # ip -> LastChange variables
        self.av: list[tuple[str, str, dict]] = []   # (ip, action, args)
        self.refuse_av: set[str] = set()            # answer these with a UPnP fault

    @property
    def av_writes(self):
        return [(ip, a, args) for ip, a, args in self.av if a in clock.AV_WRITES]

    def post(self, url, data=None, headers=None, timeout=None):
        if "/AVTransport/" not in url:
            return super().post(url, data, headers, timeout)
        ip = url.split("//", 1)[1].split(":", 1)[0]
        call = ET.fromstring(data).find("{http://schemas.xmlsoap.org/soap/envelope/}Body")[0]
        action = _local(call.tag)
        args = [(_local(c.tag), c.text or "") for c in call]
        assert [k for k, _ in args] == scpd_in_args()[action], action
        self.av.append((ip, action, dict(args)))
        if action in self.refuse_av:
            raise requests.HTTPError("500 Server Error", response=FaultReply(
                FAULT_800.replace(">800<", ">701<")))
        if action in self.lose_reply:      # it acted, then the answer was lost
            raise requests.Timeout("read timed out")
        out = {}
        if action == "GetRunningAlarmProperties":
            if ip not in self.running:
                raise requests.HTTPError("500 Server Error", response=FaultReply(FAULT_800))
            out = self.running[ip]
        inner = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in out.items())
        return Reply(f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                     f'<s:Body><u:{action}Response xmlns:u="urn:schemas-upnp-org:service:'
                     f'AVTransport:1">{inner}</u:{action}Response></s:Body></s:Envelope>')

    def GetTimeNow(self, args):
        return {"CurrentUTCTime": "2026-10-04 15:20:01", "CurrentLocalTime": "2026-10-04 08:20:01",
                "CurrentTimeZone": "x", "CurrentTimeGeneration": "1"}

    def GetFormat(self, args):
        return {"CurrentTimeFormat": "INV", "CurrentDateFormat": "INV"}


@pytest.fixture
def av(monkeypatch):
    f = FakeTransport()
    monkeypatch.setattr(devices.requests, "post", f.post)
    monkeypatch.setattr(alarm_cli, "_household", lambda args: household())
    monkeypatch.setattr(clock, "last_change", lambda ip, wait=3.0: f.state.get(ip))
    return f


def run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_the_recorded_scpd_has_every_action_sent():
    assert set(scpd_in_args()) >= clock.AV_READS | clock.AV_WRITES


def test_the_recorded_running_response_has_the_scpds_out_arguments():
    assert list(RUNNING) == scpd_in_args("out")["GetRunningAlarmProperties"]


def test_the_recorded_running_alarm_parses():
    got = clock.parse_running_alarm(RUNNING_XML)
    assert (got.alarm_id, got.group_id, got.logged_start) == (
        "34", f"{ROAM_R}:1000000001", "2026-10-04 14:58:15")


def test_the_recorded_fault_800_means_nothing_is_ringing(av):
    assert clock.upnp_error(FAULT_800) == "800"
    assert clock.running_alarm(ROAM_IP) is None


def test_any_other_fault_is_an_error(av, monkeypatch):
    def broken(*a, **k):
        raise requests.HTTPError("500", response=FaultReply(FAULT_800.replace(">800<", ">402<")))
    monkeypatch.setattr(devices.requests, "post", broken)
    with pytest.raises(requests.HTTPError):
        clock.running_alarm(ROAM_IP)


def test_the_recorded_last_change_gives_alarm_and_snooze_state():
    state = clock.parse_last_change(NOTIFY)
    assert state == {"TransportState": "STOPPED", "AlarmRunning": "0", "SnoozeRunning": "0"}


# ---- alarm status (read-only) --------------------------------------------------

def test_status_reports_the_ringing_room_and_alarm(av, capsys):
    av.running[ROAM_IP] = RUNNING
    av.state[ROAM_IP] = RINGING
    code, out, _ = run(["alarm", "status", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["ringing"] == ["Sonos Roam"]
    roam = next(r for r in payload["rooms"] if r["group"] == "Sonos Roam")
    assert (roam["alarm_id"], roam["room"], roam["ringing"]) == ("34", "Sonos Roam", True)
    assert roam["logged_start"] == "2026-10-04 14:58:15"
    living = next(r for r in payload["rooms"] if r["group"] == "Living Room")
    assert living["ringing"] is False
    # every group's coordinator was asked, and nothing was written or journalled
    assert {ip for ip, a, _ in av.av} == {ROAM_IP, LIVING_IP}
    assert av.av_writes == [] and av.writes == [] and journal() == []


def test_status_names_the_alarm_in_words(av, capsys):
    av.running[ROAM_IP] = RUNNING
    code, out, _ = run(["alarm", "status"], capsys)
    assert code == 0
    assert "Sonos Roam: RINGING alarm 34: Sonos Roam 08:20:00" in out
    assert "Living Room: nothing ringing" in out


# The poster's Roam, 2026-10-08: alarm 119 made at 22:33Z for 15:36, a household
# 7 hours behind UTC. Going off by itself, it reported LoggedStartTime in UTC.
FIRED_ITSELF = {"AlarmID": "34", "GroupID": f"{ROAM_R}:1000000001",
                "LoggedStartTime": "2026-10-08 22:36:12"}


def household_clock(av, local, utc, time_format="INV"):
    av.GetTimeNow = lambda args: {"CurrentUTCTime": utc, "CurrentLocalTime": local,
                                  "CurrentTimeZone": "x", "CurrentTimeGeneration": "1"}
    av.GetFormat = lambda args: {"CurrentTimeFormat": time_format, "CurrentDateFormat": "INV"}


@pytest.mark.parametrize("time_format, said", [("INV", "15:36"), ("24H", "15:36"),
                                               ("12H", "3:36 PM")])
def test_status_says_since_in_the_households_own_time(av, capsys, time_format, said):
    household_clock(av, "2026-10-08 15:40:00", "2026-10-08 22:40:00", time_format)
    av.running[ROAM_IP] = FIRED_ITSELF
    code, out, _ = run(["alarm", "status", "--room", "Sonos Roam"], capsys)
    assert code == 0
    assert out.strip().endswith(f", since {said}")
    code, out, _ = run(["alarm", "status", "--room", "Sonos Roam", "--json"], capsys)
    [roam] = json.loads(out)["rooms"]
    assert (roam["since"], roam["logged_start"]) == (said, "2026-10-08 22:36:12")


def test_a_household_on_utc_says_the_stamp_unchanged(av, capsys):
    household_clock(av, "2026-10-08 22:40:00", "2026-10-08 22:40:00")
    av.running[ROAM_IP] = FIRED_ITSELF
    _, out, _ = run(["alarm", "status", "--room", "Sonos Roam"], capsys)
    assert out.strip().endswith(", since 22:36")


@pytest.mark.parametrize("stamp", ["", "soon", "2026-13-40 99:00:00", "15:36"])
def test_an_unreadable_start_says_no_since(av, capsys, stamp):
    av.running[ROAM_IP] = FIRED_ITSELF | {"LoggedStartTime": stamp}
    code, out, _ = run(["alarm", "status", "--room", "Sonos Roam"], capsys)
    assert code == 0
    assert out.startswith("Sonos Roam: RINGING alarm 34: ") and "since" not in out


def test_status_without_the_households_clock_still_answers(av, capsys):
    def broken(args):
        raise requests.HTTPError("500", response=FaultReply(FAULT_800.replace(">800<", ">402<")))
    av.GetTimeNow = broken
    av.running[ROAM_IP] = FIRED_ITSELF
    code, out, _ = run(["alarm", "status", "--room", "Sonos Roam"], capsys)
    assert code == 0
    assert out.startswith("Sonos Roam: RINGING alarm 34: ") and "since" not in out


def test_try_sends_utc_and_its_echo_reads_back_as_the_households_time(av, capsys):
    household_clock(av, "2026-10-08 15:36:05", "2026-10-08 22:36:05", "12H")
    code, out, _ = run(["alarm", "try", "34", "--json"], capsys)
    assert code == 0, out
    [(_, _, sent)] = av.av_writes
    assert sent["LoggedStartTime"] == "2026-10-08 22:36:05"
    assert journal()[0]["logged_start"] == "2026-10-08 22:36:05"
    av.running[ROAM_IP] = FIRED_ITSELF | {"LoggedStartTime": sent["LoggedStartTime"]}
    _, out, _ = run(["alarm", "status", "--room", "Sonos Roam"], capsys)  # a fresh process
    assert out.strip().endswith(", since 3:36 PM")


def test_the_journal_keeps_what_try_sent_whatever_the_speaker_echoes(av, capsys):
    run(["alarm", "try", "34"], capsys)
    av.running[ROAM_IP] = FIRED_ITSELF | {"LoggedStartTime": "garbage"}
    _, out, _ = run(["alarm", "status", "--room", "Sonos Roam"], capsys)
    assert "since" not in out
    assert journal()[0]["logged_start"] == "2026-10-04 15:20:01"


def test_the_households_offset_is_to_the_minute():
    from datetime import datetime, timedelta
    hh = clock.HouseholdTime(datetime(2026, 10, 8, 15, 40, 1), datetime(2026, 10, 8, 22, 40, 0))
    assert hh.offset == timedelta(hours=-7)
    assert hh.local_of(datetime(2026, 10, 8, 0, 10)) == datetime(2026, 10, 7, 17, 10)
    assert clock.parse_stamp("2026-10-08 22:36:12") == datetime(2026, 10, 8, 22, 36, 12)
    assert clock.parse_stamp("2026-10-08T22:36:12") is None


def test_status_with_nothing_ringing(av, capsys):
    av.state[ROAM_IP] = clock.parse_last_change(NOTIFY)
    code, out, _ = run(["alarm", "status", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["ringing"] == []
    assert "ListAlarms" not in av.sent                  # nothing to name


def test_a_snoozed_alarm_known_only_from_last_change(av, capsys):
    av.state[ROAM_IP] = {"AlarmRunning": "0", "SnoozeRunning": "1"}
    code, out, _ = run(["alarm", "status", "--room", "Sonos Roam"], capsys)
    assert code == 0
    assert out.strip() == "Sonos Roam: snoozed an alarm"


# ---- alarm try ------------------------------------------------------------------

def test_try_fires_the_alarm_on_its_rooms_coordinator_and_journals_it(av, capsys):
    code, out, _ = run(["alarm", "try", "34", "--json"], capsys)
    assert code == 0, out
    [(ip, action, args)] = av.av_writes
    assert (ip, action) == (ROAM_IP, "RunAlarm")
    alarm = next(a for a in ALARMS if a.id == "34")
    assert args == {"InstanceID": "0", "AlarmID": "34",
                    "LoggedStartTime": "2026-10-04 15:20:01", "Duration": "02:00:00",
                    "ProgramURI": alarm.program_uri, "ProgramMetaData": alarm.program_metadata,
                    "PlayMode": "SHUFFLE", "Volume": "25", "IncludeLinkedZones": "0"}
    run_rec, start, end = journal()
    assert (run_rec["action"], run_rec["alarm_id"], run_rec["ip"]) == ("alarm_run", "34", ROAM_IP)
    assert run_rec["alarm"]["attributes"]["ID"] == "34"
    assert (start["action"], end["action"]) == ("alarm_run_stop_start", "alarm_run_stop_end")
    assert json.loads(out)["performed"] is True


def test_try_a_bonded_followers_alarm_goes_to_the_groups_coordinator(av, capsys):
    code, _, _ = run(["alarm", "try", "66"], capsys)
    assert code == 0
    assert [(ip, a) for ip, a, _ in av.av_writes] == [(ROAM_IP, "RunAlarm")]


def test_try_dry_run_prints_and_writes_nothing(av, capsys):
    code, out, _ = run(["alarm", "try", "34", "--dry-run"], capsys)
    assert code == 0
    assert out.startswith("[dry-run] would fire alarm 34 on Sonos Roam (L) [10.0.0.11]")
    assert av.av == [] and journal() == []


def test_try_an_alarm_aimed_at_a_vanished_speaker_is_refused(av, capsys):
    av.alarms["34"]["RoomUUID"] = GONE
    code, _, err = run(["alarm", "try", "34"], capsys)
    assert code == 1
    assert "isn't here (Kitchen)" in err
    assert av.av == [] and journal() == []


# ---- alarm stop / alarm snooze ----------------------------------------------------

@pytest.mark.parametrize("argv, action", [
    (["stop"], "Stop"), (["snooze"], "SnoozeAlarm")])
def test_stop_and_snooze_journal_and_go_to_the_coordinator(av, capsys, argv, action):
    av.running[ROAM_IP] = RUNNING
    code, out, _ = run(["alarm", *argv, "--room", "Sonos Roam (R)", "--json"], capsys)
    assert code == 0, out
    assert [(ip, a) for ip, a, _ in av.av_writes] == [(ROAM_IP, action)]
    payload = json.loads(out)
    assert payload["performed"] is True and payload["redirected_from"].startswith("Sonos Roam (R)")
    rec = journal()[0]
    assert (rec["action"], rec["ip"], rec["alarm_id"]) == (
        f"alarm_{argv[0]}", ROAM_IP, "34")


@pytest.mark.parametrize("verb", ["stop", "snooze"])
def test_stop_and_snooze_dry_run_print_and_write_nothing(av, capsys, verb):
    av.running[ROAM_IP] = RUNNING
    code, out, _ = run(["alarm", verb, "--room", "Sonos Roam", "--dry-run"], capsys)
    assert code == 0
    assert out.startswith(f"[dry-run] would {verb} the alarm in Sonos Roam")
    assert av.av_writes == [] and journal() == []


@pytest.mark.parametrize("verb", ["stop", "snooze"])
def test_stop_and_snooze_refuse_when_nothing_is_ringing(av, capsys, verb):
    av.state[ROAM_IP] = clock.parse_last_change(NOTIFY)
    code, _, err = run(["alarm", verb, "--room", "Sonos Roam"], capsys)
    assert code == 1
    assert "no alarm is going off in Sonos Roam" in err
    assert av.av_writes == [] and journal() == []


def test_a_snoozed_alarm_can_be_stopped(av, capsys):
    av.state[ROAM_IP] = {"AlarmRunning": "0", "SnoozeRunning": "1"}
    code, _, _ = run(["alarm", "stop", "--room", "Sonos Roam"], capsys)
    assert code == 0
    assert [a for _, a, _ in av.av_writes] == ["Stop"]


@pytest.mark.parametrize("extra, duration", [
    ([], "00:10:00"), (["--minutes", "5"], "00:05:00"), (["--minutes", "15"], "00:15:00"),
    (["--minutes", "30"], "00:30:00")])
def test_snooze_defaults_to_10_minutes_and_takes_5_10_15_30(av, capsys, extra, duration):
    av.running[ROAM_IP] = RUNNING
    code, _, _ = run(["alarm", "snooze", "--room", "Sonos Roam", *extra], capsys)
    assert code == 0
    [(_, _, args)] = av.av_writes
    assert args == {"InstanceID": "0", "Duration": duration}
    snooze, start, end = journal()
    assert snooze["minutes"] == int(duration[3:5])
    assert (start["action"], end["action"]) == ("alarm_snooze_ring_start",
                                                "alarm_snooze_ring_end")


def test_snooze_refuses_any_other_length(av, capsys):
    av.running[ROAM_IP] = RUNNING
    with pytest.raises(SystemExit):
        cli.main(["alarm", "snooze", "--room", "Sonos Roam", "--minutes", "7"])
    with pytest.raises(ValueError):
        clock.snooze_alarm(ROAM_IP, 7)
    assert av.av_writes == [] and journal() == []


def test_the_snooze_and_duration_spans_are_ones_analyse_pairs(av, capsys):
    av.running[ROAM_IP] = RUNNING
    run(["alarm", "snooze", "--room", "Sonos Roam"], capsys)
    run(["alarm", "try", "34"], capsys)
    points, spans = report._load_interventions(play.INTERVENTION_LOG)
    assert sorted(name for _, _, name, _ in spans) == ["alarm_run_stop", "alarm_snooze_ring"]
    assert all(end - start == 240 and ip == ROAM_IP for start, end, _, ip in spans)
    assert {p[1] for p in points} >= {"alarm_snooze", "alarm_run"}


def test_no_event_leaves_status_to_get_running_alarm_properties(av, capsys):
    av.running[ROAM_IP] = RUNNING
    code, out, _ = run(["alarm", "status", "--room", "Sonos Roam", "--json"], capsys)
    [roam] = json.loads(out)["rooms"]
    assert (code, roam["ringing"], roam["alarm_running"]) == (0, True, None)


@pytest.mark.parametrize("state, ringing", [
    ({"AlarmRunning": "0", "SnoozeRunning": "0"}, False),   # the event says plainly: no
    ({"AlarmRunning": "1", "SnoozeRunning": "0"}, True),
    (None, True)])                                          # no event: the ID decides
def test_an_event_saying_nothing_runs_overrules_a_running_alarm_id(av, capsys, state, ringing):
    av.running[ROAM_IP] = RUNNING
    if state:
        av.state[ROAM_IP] = state
    assert (clock.alarm_now(ROAM_IP) is not None) is ringing
    code, _, _ = run(["alarm", "stop", "--room", "Sonos Roam"], capsys)
    assert code == (0 if ringing else 1)
    assert [a for _, a, _ in av.av_writes] == (["Stop"] if ringing else [])


# ---- last_change: one GENA event, on loopback only ----------------------------

class Gena:
    """`requests.request` for SUBSCRIBE/UNSUBSCRIBE. On SUBSCRIBE it posts
    `notify` (if any) to the callback, as a speaker does, from this thread."""

    def __init__(self, notify=NOTIFY, refuse=False, stall=False, trickle=False):
        self.notify, self.refuse, self.stall, self.trickle = notify, refuse, stall, trickle
        self.unsubscribed: list[str] = []
        self.stalled = None                # the half-sent NOTIFY's socket

    def __call__(self, method, url, headers=None, timeout=None):
        import http.client
        from urllib.parse import urlsplit
        if self.refuse:
            raise requests.ConnectionError("refused")
        if method == "UNSUBSCRIBE":
            self.unsubscribed.append(headers["SID"])
            return type("R", (), {"headers": {}})()
        assert method == "SUBSCRIBE" and url.endswith("/MediaRenderer/AVTransport/Event")
        cb = urlsplit(headers["CALLBACK"].strip("<>"))
        assert cb.hostname == "127.0.0.1"
        if self.stall or self.trickle:     # headers promise a body that never comes
            import socket
            self.stalled = socket.create_connection((cb.hostname, cb.port), timeout=2)
            self.stalled.sendall(b"NOTIFY / HTTP/1.1\r\nHost: x\r\n"
                                 b"Content-Length: 100000\r\n\r\n<e:propertyset")
            if self.trickle:               # ... a byte at a time, faster than `wait`
                import threading
                threading.Thread(target=self._drip, daemon=True).start()
        elif self.notify is not None:
            conn = http.client.HTTPConnection(cb.hostname, cb.port, timeout=2)
            conn.request("NOTIFY", cb.path or "/", self.notify.encode(),
                         {"NT": "upnp:event", "SID": "uuid:fake-sid", "SEQ": "0"})
            conn.getresponse().read()
            conn.close()
        return type("R", (), {"headers": {"SID": "uuid:fake-sid"}})()

    def _drip(self):
        import time
        try:
            for _ in range(200):           # 10s of bytes, if nothing stops it
                self.stalled.sendall(b" ")
                time.sleep(0.05)
        except OSError:
            pass


def test_last_change_takes_the_initial_event_and_unsubscribes(monkeypatch):
    gena = Gena()
    monkeypatch.setattr(clock.requests, "request", gena)
    assert REAL_LAST_CHANGE("127.0.0.1", wait=2) == {
        "TransportState": "STOPPED", "AlarmRunning": "0", "SnoozeRunning": "0"}
    assert gena.unsubscribed == ["uuid:fake-sid"]


def test_last_change_with_no_event_is_none_and_still_unsubscribes(monkeypatch):
    gena = Gena(notify=None)
    monkeypatch.setattr(clock.requests, "request", gena)
    assert REAL_LAST_CHANGE("127.0.0.1", wait=0.2) is None
    assert gena.unsubscribed == ["uuid:fake-sid"]


def test_last_change_when_subscribing_fails_is_none(monkeypatch):
    gena = Gena(refuse=True)
    monkeypatch.setattr(clock.requests, "request", gena)
    assert REAL_LAST_CHANGE("127.0.0.1", wait=0.2) is None
    assert gena.unsubscribed == []


@pytest.mark.parametrize("how", ["stall", "trickle"])
def test_a_notify_that_stalls_or_trickles_cant_hang_the_command(monkeypatch, how):
    import time
    gena = Gena(**{how: True})
    monkeypatch.setattr(clock.requests, "request", gena)
    t0 = time.monotonic()
    assert REAL_LAST_CHANGE("127.0.0.1", wait=0.3) is None
    assert time.monotonic() - t0 < 2
    assert gena.unsubscribed == ["uuid:fake-sid"]
    gena.stalled.close()


def test_the_callback_listens_only_on_the_address_facing_the_speaker(monkeypatch):
    bound = []
    real = clock_http_server()

    class Spy(real):
        def __init__(self, addr, handler, *a, **k):
            bound.append(addr[0])
            super().__init__(addr, handler)
    monkeypatch.setattr("http.server.ThreadingHTTPServer", Spy)
    monkeypatch.setattr(clock.requests, "request", Gena())
    REAL_LAST_CHANGE("127.0.0.1", wait=1)
    assert bound == ["127.0.0.1"]


def clock_http_server():
    from http.server import ThreadingHTTPServer
    return ThreadingHTTPServer


WRITES_WITH_A_LATER_ACT = [
    (lambda: clock.run_alarm(ROAM_IP, ALARMS[4], "2026-10-04 14:58:15"), "RunAlarm",
     "alarm_run", "alarm_run_stop"),
    (lambda: clock.snooze_alarm(ROAM_IP, 10, "34"), "SnoozeAlarm",
     "alarm_snooze", "alarm_snooze_ring")]


@pytest.mark.parametrize("write, action, name, span", WRITES_WITH_A_LATER_ACT)
def test_a_lost_reply_still_journals_the_later_span(av, write, action, name, span):
    av.lose_reply.add(action)
    with pytest.raises(requests.Timeout):
        write()
    assert [a for _, a, _ in av.av_writes] == [action]       # the speaker had it
    assert [r["action"] for r in journal()] == [name, f"{span}_start", f"{span}_end"]


@pytest.mark.parametrize("write, action, name, span", WRITES_WITH_A_LATER_ACT)
def test_a_refused_write_journals_no_later_span(av, write, action, name, span):
    av.refuse_av.add(action)
    with pytest.raises(requests.HTTPError):
        write()
    assert [r["action"] for r in journal()] == [name]


# ---- an alarm twiddle can't read --------------------------------------------------

def test_a_strict_read_refuses_the_list_a_tolerant_one_lists_around_it(fake):
    fake.alarms["80"], fake.children["80"] = dict(BAD["80"]), []
    with pytest.raises(model.UnreadableAlarm, match="alarm 80: invalid alarm recurrence"):
        clock.list_alarms(IP)
    got = clock.list_alarms(IP, tolerant=True)
    assert got.alarms == ALARMS and got.version == fake.version
    [bad] = got.unreadable
    assert (bad.id, bad.room_uuid) == ("80", ROAM_L)
    assert got.xml == fake.document()                    # still the whole document
    assert fake.writes == []


def test_a_strict_read_has_no_unreadable(fake):
    assert clock.list_alarms(IP).unreadable == []


@pytest.mark.parametrize("argv, action", [
    (["stop"], "Stop"), (["snooze"], "SnoozeAlarm")])
def test_stop_and_snooze_work_while_another_alarm_cant_be_read(av, capsys, argv, action):
    # They never read the list, so a bad alarm elsewhere can't block silencing one.
    av.alarms["80"], av.children["80"] = dict(BAD["80"]), []
    av.running[ROAM_IP] = RUNNING
    code, out, _ = run(["alarm", *argv, "--room", "Sonos Roam", "--json"], capsys)
    assert code == 0, out
    assert [(ip, a) for ip, a, _ in av.av_writes] == [(ROAM_IP, action)]
    assert journal()[0]["action"] == f"alarm_{argv[0]}"
    assert "ListAlarms" not in av.sent
    code, out, _ = run(["alarm", *argv, "--room", "Sonos Roam", "--dry-run"], capsys)
    assert code == 0 and out.startswith(f"[dry-run] would {argv[0]}")


def test_status_names_the_ringing_alarm_while_another_cant_be_read(av, capsys):
    av.alarms["80"], av.children["80"] = dict(BAD["80"]), []
    av.running[ROAM_IP] = RUNNING
    code, out, _ = run(["alarm", "status"], capsys)
    assert code == 0, out
    assert "Sonos Roam: RINGING alarm 34: Sonos Roam 08:20:00" in out
    assert av.av_writes == [] and av.writes == [] and journal() == []
