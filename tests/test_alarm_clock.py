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
from xml.sax.saxutils import escape

import pytest
import requests

from twiddle import devices, play
from twiddle.alarms import baseline, clock
from twiddle.alarms.model import Alarm, Recurrence

from tests.test_alarm_cli import ALARMS, ALARMS_XML, ROAM_L

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
