"""The `twiddle alarm` TUI, driven by Textual's pilot over a fake AlarmClock.

The household is the CLI tests' one, the alarms T1.1's recorded ListAlarms,
and the speaker `test_alarm_clock.FakeClock`, answering at the HTTP layer: so
space and d are seen to go through the CLI's own journalled `clock` writes.
Any other connection fails the test: no network, speaker or Spotify.
"""
import asyncio
import functools
import json
import socket

import pytest
from textual.widgets import Input, OptionList

from tests.test_alarm_cli import GONE, household
from tests.test_alarm_clock import (FIRED_ITSELF, RUNNING, ROAM_IP, FakeClock, FakeTransport,
                                   household_clock)
from twiddle import alarm_cli, cli, devices, play
from twiddle.alarms import clock
from twiddle.alarms.app import (AlarmApp, AskScreen, EditorScreen, RingingScreen, SourcePicker,
                                TypeIdScreen, alarm_line, clock_art)
from twiddle.alarms.model import parse_alarms
from twiddle.alarms import sources
from twiddle.alarms.sources import sound


class Clock(FakeClock):
    """FakeClock, and the household's clock: Saturday 3 October 2026, 13:20."""

    def GetTimeNow(self, args):
        return {"CurrentUTCTime": "2026-10-03 20:20:01",
                "CurrentLocalTime": "2026-10-03 13:20:01",
                "CurrentTimeZone": "x", "CurrentTimeGeneration": "1"}

    def GetFormat(self, args):
        return {"CurrentTimeFormat": "INV", "CurrentDateFormat": "INV"}


@pytest.fixture(autouse=True)
def no_connections(monkeypatch):
    def refuse(self, *a, **k):
        raise AssertionError(f"the alarm TUI test opened a connection: {a}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "sendto", refuse)          # SSDP discovery
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def fake(monkeypatch):
    f = Clock()
    monkeypatch.setattr(devices.requests, "post", f.post)
    return f


def journal() -> list[dict]:
    if not play.INTERVENTION_LOG.exists():
        return []
    return [json.loads(l) for l in play.INTERVENTION_LOG.read_text().splitlines()]


def pilot(fn):
    """An async pilot test, run as a plain one (as test_scene_app does)."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))
    return run


def make(dry_run=False, **kw):
    return AlarmApp(household=household, dry_run=dry_run, **kw)


async def settle(app, pilot):
    """Every worker done, including the reload a write starts."""
    for _ in range(4):
        await app.workers.wait_for_complete()
        await pilot.pause()


def is_alarm(line: str) -> bool:
    return line.lstrip()[:1] in ("●", "○")


def lines(app) -> list[str]:
    return [str(o.prompt) for o in app.query_one("#alarms", OptionList).options]


def text(app, wid) -> str:
    return str(app.query_one(wid).render())


async def pick(app, pilot, aid):
    alarms = app.query_one("#alarms", OptionList)
    alarms.highlighted = [o.id for o in alarms.options].index(f"alarm:{aid}")
    await pilot.pause()


def needs_mac_alarm(fake, aid="300"):
    uri, meta = sound.Sound(host=lambda anchor: "10.0.0.5").build(next(iter(sound.SOUNDS)))
    fake.alarms[aid] = dict(fake.alarms["11"], ID=aid, StartTime="06:00:00",
                            Recurrence="DAILY", ProgramURI=uri, ProgramMetaData=meta)
    fake.children[aid] = []


# ---- what it shows -------------------------------------------------------------

@pilot
async def test_the_list_is_every_room_and_its_alarms(fake):
    fake.alarms["301"] = dict(fake.alarms["1"], ID="301", RoomUUID=GONE)
    fake.children["301"] = []
    needs_mac_alarm(fake)
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        got = lines(app)
        rows = {r["id"]: r for r in app.shown.rows}
        assert set(rows) == set(fake.alarms)
        # Rooms in order, each alarm under its own; the follower's and the
        # vanished speaker's under a heading that says so.
        assert got.index("Kitchen — set on Kitchen, a speaker no longer in the household") \
            < got.index("Living Room") < got.index("Sonos Roam")
        assert got[got.index("Living Room") + 1].strip() == "(no alarms)"
        follower = "Sonos Roam — set on Sonos Roam (R), a bonded follower"
        assert got.index(follower) > got.index("Sonos Roam")
        assert got[got.index(follower) + 1].startswith("  ○    17:00  Fri                i don't want to work. i just…")
        alarm_lines = [l for l in got if is_alarm(l)]
        assert len(alarm_lines) == len(rows)
        for r in rows.values():
            line = next(l for l in alarm_lines if l == alarm_line(r))
            assert line.startswith(f"  {'●' if r['enabled'] else '○'} {r['time_text']:>8}")
            assert f"vol {r['volume']}" in line and r["duration_text"] in line
        assert "⌁" in next(l for l in alarm_lines if "06:00" in l)
        assert next(l for l in alarm_lines if "Todd Rundgren" in l).endswith(" repeat")
        # Header, legend and keys.
        assert text(app, "#top").startswith("twiddle alarm")
        assert "Sat 10-03  13:20" in text(app, "#top")
        legend = text(app, "#legend")
        assert "⌁ = needs this Mac at fire time" in legend
        assert "next: today " not in legend           # 06:00 has passed today
        assert "next: tomorrow 06:00" in legend
        assert text(app, "#keys") == "n new  enter edit  space on/off  d delete  t try now  r refresh  q quit"
    assert fake.writes == [] and journal() == []


@pilot
async def test_an_unreadable_alarm_is_named_apart_and_the_rest_still_show(fake):
    fake.alarms["80"] = dict(fake.alarms["1"], ID="80", Volume="101")
    fake.children["80"] = []
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        got = lines(app)
        assert "can't read 1 alarm (fix or delete it in the Sonos app)" in got
        assert any("#80 Sonos Roam: expected a volume 0-100, got '101'" in l for l in got)
        assert sum(map(is_alarm, got)) == 10


@pilot
async def test_only_alarms_can_be_highlighted(fake):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        alarms = app.query_one("#alarms", OptionList)
        assert alarms.get_option_at_index(alarms.highlighted).id.startswith("alarm:")
        assert all(o.disabled for o in alarms.options if not (o.id or "").startswith("alarm:"))


@pilot
async def test_a_household_that_cant_be_read_says_so(fake, monkeypatch):
    def broken():
        raise OSError("no route to host")
    app = AlarmApp(household=broken)
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        assert app.status_text == "could not read the alarms: no route to host"
        await pilot.press("space", "d")
        await settle(app, pilot)
        assert app.screen is app.screen_stack[0]        # nothing to ask about
    assert fake.writes == [] and journal() == []


@pilot
async def test_r_reads_the_list_again(fake):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        fake.edit_in_app("11", Volume="4")
        await pilot.press("r")
        await settle(app, pilot)
        assert app.shown.found.version == fake.version
        assert app.shown.alarm("11").volume == 4
    assert fake.writes == [] and journal() == []


# ---- space: on/off ---------------------------------------------------------------

@pytest.mark.parametrize("aid, on", [("2", "0"), ("66", "1")])
@pilot
async def test_space_toggles_through_the_journalled_write(fake, aid, on):
    before = dict(fake.alarms[aid])
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, aid)
        await pilot.press("space")
        await settle(app, pilot)
        verb = "enable" if on == "1" else "disable"
        assert app.status_text.startswith(f"{verb}d alarm {aid}: Sonos Roam")
        assert app.shown.alarm(aid).enabled is (on == "1")     # read back
        assert app._current_id() == aid                      # the cursor stayed
    got = fake.alarms[aid]
    assert got["Enabled"] == on
    assert {k: v for k, v in got.items() if k != "Enabled"} == \
        {k: v for k, v in before.items() if k != "Enabled"}       # the source byte-for-byte
    assert fake.writes == ["UpdateAlarm"]
    [entry] = journal()
    assert entry["action"] == "alarm_update" and entry["written"] is True
    assert entry["after"]["attributes"]["Enabled"] == on


@pilot
async def test_a_second_key_while_a_write_is_going_writes_nothing_more(fake):
    import threading
    gate = threading.Event()
    fake.after_write = lambda f: gate.wait(5)      # the speaker takes its time answering
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, "2")
        await pilot.press("space")
        await pilot.pause(0.1)
        assert app.writing and fake.writes == ["UpdateAlarm"]
        await pilot.press("space", "d")
        assert app.screen is app.screen_stack[0]      # d asked nothing
        gate.set()
        await settle(app, pilot)
        assert app.screen is app.screen_stack[0]
        assert app.status_text.startswith("disabled alarm 2: Sonos Roam")
    assert fake.writes == ["UpdateAlarm"] and len(journal()) == 1


@pilot
async def test_two_followers_in_a_room_each_get_one_heading(fake, monkeypatch):
    # A third unit bonded into the Roam pair's room, with alarms interleaved
    # in time with the right Roam's: one heading each, never repeated.
    from twiddle.household import Speaker
    import tests.test_alarm_cli as tac
    third = Speaker(ip="10.0.0.15", uuid=tac.STRANGER, name="Sonos Roam (C)", room="Sonos Roam",
                    group_id="g1", channel="LF")

    def house():
        h = household()
        h.speakers.append(third)
        return h
    from twiddle import alarm_cli as ac
    monkeypatch.setattr(ac, "_is_bonded_follower", lambda h, sp: sp.uuid != tac.ROAM_L)
    for aid, at, uuid in (("401", "16:00:00", tac.STRANGER), ("402", "18:00:00", tac.STRANGER)):
        fake.alarms[aid] = dict(fake.alarms["66"], ID=aid, StartTime=at, RoomUUID=uuid)
        fake.children[aid] = []
    app = AlarmApp(household=house)
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        got = lines(app)
        for name in ("Sonos Roam (R)", "Sonos Roam (C)"):
            head = f"Sonos Roam — set on {name}, a bonded follower"
            assert got.count(head) == 1, got


@pilot
async def test_space_on_a_list_changed_in_the_app_writes_nothing_and_rereads(fake):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, "2")
        fake.edit_in_app("11", Volume="4")          # someone, in the Sonos app
        await pilot.press("space")
        await settle(app, pilot)
        assert app.status_text.startswith("the alarms changed since they were shown")
        assert app.shown.found.version == fake.version
    assert fake.writes == [] and journal() == []
    assert fake.alarms["2"]["Enabled"] == "1"


@pilot
async def test_space_reads_strictly_so_an_unreadable_alarm_refuses_it(fake):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, "2")
        # Unreadable, and the version unmoved: only the strict read can refuse.
        fake.alarms["80"] = dict(fake.alarms["1"], ID="80", Volume="101")
        fake.children["80"] = []
        await pilot.press("space")
        await settle(app, pilot)
        assert app.status_text.startswith("twiddle can't read alarm 80")
        assert "nothing written" in app.status_text
    assert fake.writes == [] and journal() == []


@pilot
async def test_a_write_that_landed_while_another_alarm_moved_is_reported_done(fake):
    fake.after_write = lambda f: f.edit_in_app("11", Volume="4")
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, "2")
        await pilot.press("space")
        await settle(app, pilot)
        assert app.status_text.startswith("disabled alarm 2, but:")
    assert fake.alarms["2"]["Enabled"] == "0"


# ---- d: delete, asked twice ------------------------------------------------------

@pilot
async def test_d_asks_twice_then_deletes_through_the_journalled_write(fake):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, "66")
        await pilot.press("d")
        assert isinstance(app.screen, AskScreen)
        assert "delete alarm 66: Sonos Roam 17:00:00" in str(app.screen.query_one("Static").render())
        await pilot.press("y")
        assert isinstance(app.screen, TypeIdScreen)
        await pilot.press("6", "6", "enter")
        await settle(app, pilot)
        assert app.status_text.startswith("deleted alarm 66: Sonos Roam")
        assert app.shown.alarm("66") is None
        assert not any("17:00  Fri" in l and "i don't" in l for l in lines(app))
    assert "66" not in fake.alarms
    assert fake.writes == ["DestroyAlarm"]
    [entry] = journal()
    assert entry["action"] == "alarm_destroy"
    gone = clock.deleted("66")
    assert gone is not None and gone.program_uri == parse_alarms(Clock().document())[
        [a.id for a in parse_alarms(Clock().document())].index("66")].program_uri


@pytest.mark.parametrize("keys", [["n"], ["escape"], ["x"], ["y", "escape"],
                                  ["y", "6", "enter"], ["y", "6", "7", "enter"],
                                  ["y", "enter"]])
@pilot
async def test_d_not_confirmed_twice_deletes_nothing(fake, keys):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, "66")
        await pilot.press("d", *keys)
        await settle(app, pilot)
        if not isinstance(app.screen, AskScreen):     # `x` leaves the question open
            assert app.status_text == "not deleted: alarm 66"
    assert "66" in fake.alarms
    assert fake.writes == [] and journal() == []


@pilot
async def test_d_on_a_list_changed_while_asking_writes_nothing(fake):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, "66")
        await pilot.press("d", "y")
        fake.edit_in_app("66", Volume="4")
        await pilot.press("6", "6", "enter")
        await settle(app, pilot)
        assert app.status_text.startswith("the alarms changed since they were shown")
    assert "66" in fake.alarms
    assert fake.writes == [] and journal() == []


# ---- --dry-run -------------------------------------------------------------------

@pytest.mark.parametrize("key, said", [("space", "[dry-run] would disable alarm 2: Sonos Roam"),
                                       ("d", "[dry-run] would delete alarm 2: Sonos Roam")])
@pilot
async def test_dry_run_writes_neither_speaker_nor_journal(fake, key, said):
    app = make(dry_run=True)
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        assert "dry-run" in text(app, "#top")
        await pick(app, pilot, "2")
        await pilot.press(key)
        await settle(app, pilot)
        assert app.screen is app.screen_stack[0]        # a dry run asks nothing
        assert app.status_text.startswith(said)
    assert fake.writes == [] and not play.INTERVENTION_LOG.exists()
    assert fake.alarms["2"]["Enabled"] == "1"


# ---- the editor: n and enter ----------------------------------------------------

class FakeSource(sources.Source):
    """A provider made up for a test: no network, no household. Its URI is
    its own, so `recognise` (and the list's ⌁) knows the alarms it built."""

    def __init__(self, name, title, *, needs_mac=False, items=(), fallback="the Sonos chime",
                 fails=None):
        self.name, self.title, self.needs_mac, self.fallback = name, title, needs_mac, fallback
        self.takes_choice = bool(items)
        self.items, self.fails = list(items), fails
        self.anchor, self.queries = None, []

    def choices(self, query=""):
        self.queries.append(query)
        return [sources.Choice(k, t) for k, t in self.items if query.lower() in t.lower()]

    def bind(self, anchor):
        self.anchor = anchor
        return self

    def build(self, choice=""):
        if self.fails:
            raise ValueError(self.fails)
        title = dict(self.items).get(choice, self.title)
        return (f"x-fake-{self.name}:{choice or 'it'}",
                f'<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/"><item>'
                f"<dc:title>{title}</dc:title></item></DIDL-Lite>")

    def owns(self, uri, metadata):
        return uri.startswith(f"x-fake-{self.name}:")


@pytest.fixture
def providers(monkeypatch):
    """Every provider faked: the real chime (it asks nothing), a station list,
    and a podcast that needs this Mac. The registry is put back afterwards."""
    monkeypatch.setattr(sources, "_REGISTRY", {})
    made = {"chime": sources.register(sources.Chime()),
            "radio": sources.register(FakeSource("radio", "dial station",
                                                 items=[("kalx", "KALX 90.7 Berkeley"),
                                                        ("kexp", "KEXP 90.3 Seattle")])),
            "podcast": sources.register(FakeSource("podcast", "Podcast episode", needs_mac=True,
                                                   items=[("ep1", "Episode one")]))}
    return made


async def open_new(app, pilot):
    await pilot.press("n")
    await pilot.pause()
    assert isinstance(app.screen, EditorScreen)
    return app.screen


async def open_edit(app, pilot, aid):
    await pick(app, pilot, aid)
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, EditorScreen) and app.screen.base.id == aid
    return app.screen


async def save(app, pilot):
    await pilot.press("ctrl+s")
    await settle(app, pilot)


async def choose(app, pilot, source_name, choice=None):
    """change… -> the provider -> (its choice) in the picker, by keys."""
    await pilot.click("#change")
    await pilot.pause()
    picker = app.screen
    assert isinstance(picker, SourcePicker)
    providers = picker.query_one("#providers", OptionList)
    providers.highlighted = [o.id for o in providers.options].index(source_name)
    await pilot.press("enter")
    await settle(app, pilot)
    if choice is not None:
        found = picker.query_one("#choices", OptionList)
        found.focus()
        found.highlighted = [c.key for c in picker.found].index(choice)
        await pilot.press("enter")
        await settle(app, pilot)
    return picker


def created(fake) -> dict:
    [aid] = [a for a in fake.alarms if int(a) >= 200]
    return fake.alarms[aid]


@pytest.mark.parametrize("clicks, want", [
    (["#once"], "ONCE"), (["#daily"], "DAILY"), (["#weekdays"], "WEEKDAYS"),
    (["#weekends"], "WEEKENDS"),
    (["#once", "#day-1", "#day-3", "#day-5"], "ON_135"),
    (["#weekdays", "#day-5", "#day-6"], "ON_12346"),
    (["#once", "#day-0", "#day-6"], "WEEKENDS"),        # the speaker's own name for the days
])
@pilot
async def test_n_creates_each_recurrence_form_and_it_reads_back(fake, providers, clicks, want):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await open_new(app, pilot)
        for c in clicks:
            await pilot.click(c)
            await pilot.pause()
        await save(app, pilot)
        got = created(fake)
        assert got["Recurrence"] == want
        assert str(app.shown.alarm(got["ID"]).recurrence) == want      # read back
        assert app.status_text.startswith(f"created alarm {got['ID']}: Sonos Roam 07:00:00")
    assert fake.writes == ["CreateAlarm"]
    [entry] = journal()
    assert entry["action"] == "alarm_create" and entry["written"] is True
    assert entry["after"]["attributes"]["Recurrence"] == want


@pilot
async def test_a_new_alarm_takes_every_field(fake, providers):
    import tests.test_alarm_cli as tac
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_new(app, pilot)
        ed.query_one("#time").value = "6:45"
        ed.query_one("#volume").value = "33"
        ed.query_one("#duration").value = "30m"
        ed.query_one("#room").value = tac.LIVING
        ed.query_one("#mode").value = alarm_cli.PLAY_MODES["shuffle"]
        ed.query_one("#grouped").value = True
        ed.query_one("#enabled").value = False
        await choose(app, pilot, "radio", "kalx")
        assert str(ed.query_one("#source").render()) == "dial station · KALX 90.7 Berkeley"
        await save(app, pilot)
    got = created(fake)
    assert {k: got[k] for k in ("StartTime", "Volume", "Duration", "RoomUUID", "PlayMode",
                                "IncludeLinkedZones", "Enabled", "ProgramURI")} == {
        "StartTime": "06:45:00", "Volume": "33", "Duration": "00:30:00", "RoomUUID": tac.LIVING,
        "PlayMode": "SHUFFLE_NOREPEAT", "IncludeLinkedZones": "1", "Enabled": "0",
        "ProgramURI": "x-fake-radio:kalx"}
    assert providers["radio"].anchor == "10.0.0.11"       # asked through the household's speaker


def _set(ed, field):
    import tests.test_alarm_cli as tac
    return {"start_time": lambda: setattr(ed.query_one("#time"), "value", "06:30"),
            "recurrence": lambda: setattr(ed.query_one("#day-0"), "value", True),
            "duration": lambda: setattr(ed.query_one("#duration"), "value", "45m"),
            "enabled": lambda: setattr(ed.query_one("#enabled"), "value", False),
            "room_uuid": lambda: setattr(ed.query_one("#room"), "value", tac.LIVING),
            "play_mode": lambda: setattr(ed.query_one("#mode"), "value", "SHUFFLE"),
            "volume": lambda: setattr(ed.query_one("#volume"), "value", "9"),
            "include_linked_zones": lambda: setattr(ed.query_one("#grouped"), "value", True),
            }[field]()


@pytest.mark.parametrize("field", ["start_time", "recurrence", "duration", "enabled", "room_uuid",
                                   "play_mode", "volume", "include_linked_zones"])
@pilot
async def test_each_field_edits_alone_and_an_unrecognised_source_stays_untouched(
        fake, providers, field):
    from twiddle.alarms import baseline
    from twiddle.alarms.model import Alarm
    import xml.etree.ElementTree as ET
    before = dict(fake.alarms["2"])                  # iHeart: no source here owns it
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_edit(app, pilot, "2")
        _set(ed, field)
        await save(app, pilot)
        assert app.status_text.startswith("updated alarm 2: ")
    got = fake.alarms["2"]
    assert (got["ProgramURI"], got["ProgramMetaData"]) == \
        (before["ProgramURI"], before["ProgramMetaData"])          # byte for byte
    was, now = (Alarm.from_element(ET.Element("Alarm", a)) for a in (before, got))
    assert baseline.differences(now, was) == [field]
    assert fake.writes == ["UpdateAlarm"]
    [entry] = journal()
    assert entry["action"] == "alarm_update" and entry["before"]["attributes"] == before


@pilot
async def test_the_editor_says_what_the_chosen_play_mode_does(fake, providers):
    fake.alarms["301"] = dict(fake.alarms["1"], ID="301", PlayMode="SOMETHING_NEW")
    fake.children["301"] = []
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_new(app, pilot)
        for name, line in alarm_cli.PLAY_MODE_LINES.items():
            ed.query_one("#mode").value = alarm_cli.PLAY_MODES[name]
            await pilot.pause()
            assert str(ed.query_one("#mode-line").render()) == line, name
        await pilot.press("escape")
        await pilot.pause()
        ed = await open_edit(app, pilot, "301")
        assert str(ed.query_one("#mode-line").render()) == "the speaker's own value"
    assert fake.writes == []


@pilot
async def test_saving_any_alarm_unchanged_writes_nothing(fake, providers):
    fake.alarms["301"] = dict(fake.alarms["1"], ID="301", RoomUUID=GONE, Duration="00:15:30",
                              PlayMode="SOMETHING_NEW")
    fake.children["301"] = []
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        for aid in list(fake.alarms):
            await open_edit(app, pilot, aid)
            await save(app, pilot)
            assert app.status_text == f"nothing changed: alarm {aid}", aid
    assert fake.writes == [] and journal() == []


@pilot
async def test_a_bonded_followers_alarm_keeps_its_room_when_only_its_volume_changes(fake, providers):
    import tests.test_alarm_cli as tac
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_edit(app, pilot, "66")
        room = ed.query_one("#room")
        assert room.value == tac.ROAM_R
        # Select has no public list of its options; the label is what it shows.
        assert str(room.query_one("SelectCurrent Static#label").render()) == \
            "Sonos Roam — set on Sonos Roam (R), a bonded follower (as it is)"
        ed.query_one("#volume").value = "12"
        await save(app, pilot)
    assert fake.alarms["66"]["RoomUUID"] == tac.ROAM_R and fake.alarms["66"]["Volume"] == "12"


@pilot
async def test_the_picker_lists_every_provider_with_a_tick_or_the_mac_warning(fake, providers):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_new(app, pilot)
        ed.query_one("#time").value = "7:15"
        assert str(ed.query_one("#mac").render()) == "✓ Plays without this Mac."   # the chime
        await pilot.click("#change")
        await pilot.pause()
        picker = app.screen
        shown = {o.id: str(o.prompt) for o in picker.query_one("#providers", OptionList).options}
        assert list(shown) == ["chime", "radio", "podcast"]       # every registered one, in order
        assert shown["radio"] == "dial station\n    ✓ Plays without this Mac."
        assert shown["podcast"] == ("Podcast episode\n    ⌁ Needs this Mac awake at 07:15. "
                                    "If it isn't: the Sonos chime.")
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is ed
    assert fake.writes == []


@pilot
async def test_a_fake_provider_registered_in_a_test_is_picked_and_marked(fake, providers):
    sources.register(FakeSource("bell", "Test bell", needs_mac=True, fallback="silence, sadly"))
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_new(app, pilot)
        ed.query_one("#time").value = "05:10"
        await choose(app, pilot, "bell")                 # takes no choice: built at once
        assert app.screen is ed
        assert str(ed.query_one("#source").render()) == "Test bell"
        assert str(ed.query_one("#mac").render()) == \
            "⌁ Needs this Mac awake at 05:10. If it isn't: silence, sadly."
        await save(app, pilot)
        got = created(fake)
        assert got["ProgramURI"] == "x-fake-bell:it"
        # The list marks it ⌁, as every alarm whose source needs this Mac.
        line = next(l for l in lines(app) if "05:10" in l)
        assert "Test bell ⌁" in line
        assert "⌁ = needs this Mac at fire time" in text(app, "#legend")


@pilot
async def test_a_choice_is_searched_and_a_build_that_fails_stays_in_the_picker(fake, providers):
    providers["podcast"].fails = "no feed for that episode"
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_new(app, pilot)
        picker = await choose(app, pilot, "radio")
        assert [c.key for c in picker.found] == ["kalx", "kexp"]      # all, before a search
        await pilot.press(*"seattle", "enter")
        await settle(app, pilot)
        assert [c.key for c in picker.found] == ["kexp"]
        assert providers["radio"].queries == ["", "seattle"]
        await pilot.press("escape")
        await pilot.pause()
        picker = await choose(app, pilot, "podcast", "ep1")
        assert app.screen is picker and picker.status_text == "no feed for that episode"
        await pilot.press("escape", "escape")
        await settle(app, pilot)
        assert app.status_text == "not saved"
    assert fake.writes == [] and journal() == []


@pytest.mark.parametrize("where", ["choices", "build"])
@pilot
async def test_a_provider_that_raises_anything_is_said_and_the_app_lives(
        fake, providers, monkeypatch, where):
    def broken(*a):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(providers["radio"], where, broken)
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await open_new(app, pilot)
        picker = await choose(app, pilot, "radio")
        if where == "build":
            await pilot.press(*"kalx", "enter")      # search, then pick it by keys
            await settle(app, pilot)
            found = picker.query_one("#choices", OptionList)
            found.focus()
            await pilot.press("enter")
            await settle(app, pilot)
        assert app.is_running and app.screen is picker
        assert picker.status_text == "connection reset"
    assert fake.writes == []


@pilot
async def test_escape_while_a_source_is_still_building_keeps_the_editor_as_it_was(
        fake, providers, monkeypatch):
    import threading
    gate = threading.Event()
    real = providers["radio"].build
    monkeypatch.setattr(providers["radio"], "build", lambda c="": gate.wait(5) and real(c))
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_new(app, pilot)
        await pilot.click("#change")
        await pilot.pause()
        picker = app.screen
        providers_list = picker.query_one("#providers", OptionList)
        providers_list.highlighted = 1                     # radio
        await pilot.press("enter")
        await settle(app, pilot)
        picker.query_one("#choices", OptionList).focus()
        await pilot.press("enter")                         # kalx: building, slowly
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is ed
        gate.set()
        await settle(app, pilot)
        assert app.is_running and app.screen is ed and ed.picked is None
        assert str(ed.query_one("#source").render()) == "Sonos chime"
    assert fake.writes == []


@pilot
async def test_a_search_overtaken_by_a_newer_one_never_replaces_its_results(
        fake, providers, monkeypatch):
    import threading
    slow = threading.Event()
    real = providers["radio"].choices

    def choices(query=""):
        if query == "berkeley":
            slow.wait(5)                     # the older search answers last
        return real(query)
    monkeypatch.setattr(providers["radio"], "choices", choices)
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await open_new(app, pilot)
        picker = await choose(app, pilot, "radio")
        query = picker.query_one("#query", Input)
        query.focus()
        await pilot.press(*"berkeley", "enter")
        await pilot.pause()
        query.value = ""
        query.focus()
        await pilot.press(*"seattle", "enter")
        await pilot.pause(0.2)
        assert [c.key for c in picker.found] == ["kexp"]
        slow.set()
        await settle(app, pilot)
        assert [c.key for c in picker.found] == ["kexp"]
        assert [str(o.prompt) for o in picker.query_one("#choices", OptionList).options] == \
            ["KEXP 90.3 Seattle"]


@pilot
async def test_a_build_overtaken_by_a_newer_choice_never_wins(fake, providers, monkeypatch):
    import threading
    gates = {"kalx": threading.Event(), "kexp": threading.Event()}
    real = providers["radio"].build

    def build(choice=""):
        gates[choice].wait(5)
        return real(choice)
    monkeypatch.setattr(providers["radio"], "build", build)
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_new(app, pilot)
        picker = await choose(app, pilot, "radio")
        found = picker.query_one("#choices", OptionList)
        found.focus()
        found.highlighted = 0
        await pilot.press("enter")              # kalx, slowly
        await pilot.pause()
        found.highlighted = 1
        await pilot.press("enter")              # then kexp, more slowly still
        await pilot.pause()
        gates["kalx"].set()                     # the overtaken one answers first
        await pilot.pause(0.3)
        assert app.screen is picker             # and is dropped
        gates["kexp"].set()
        await settle(app, pilot)
        assert app.screen is ed
        assert ed.picked.uri == "x-fake-radio:kexp"
        assert str(ed.query_one("#source").render()) == "dial station · KEXP 90.3 Seattle"


@pilot
async def test_the_lists_keys_do_nothing_under_the_editor(fake, providers):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_edit(app, pilot, "2")
        ed.query_one("#day-1").focus()
        await pilot.press("n", "d", "r", "space", "j", "k", "q")
        await settle(app, pilot)
        assert app.is_running
        assert app.screen is ed and sum(isinstance(s, EditorScreen) for s in app.screen_stack) == 1
    assert fake.writes == [] and journal() == []


@pilot
async def test_a_new_source_changes_only_the_source(fake, providers):
    before = dict(fake.alarms["1"])                       # the chime
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await open_edit(app, pilot, "1")
        await choose(app, pilot, "radio", "kexp")
        await save(app, pilot)
        assert app.status_text.endswith("(program_uri, program_metadata)")
    got = fake.alarms["1"]
    assert got["ProgramURI"] == "x-fake-radio:kexp"
    assert {k: v for k, v in got.items() if not k.startswith("Program")} == \
        {k: v for k, v in before.items() if not k.startswith("Program")}


@pilot
async def test_a_value_that_cant_be_read_keeps_the_editor_open(fake, providers):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await open_edit(app, pilot, "2")
        ed.query_one("#volume").value = "200"
        await save(app, pilot)
        assert app.screen is ed and ed.problem == "volume '200' is not 0-100"
    assert fake.writes == []


@pytest.mark.parametrize("new", [False, True])
@pilot
async def test_saving_over_a_list_changed_meanwhile_refuses_and_rereads(fake, providers, new):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await (open_new(app, pilot) if new else open_edit(app, pilot, "2"))
        ed.query_one("#volume").value = "9"
        fake.edit_in_app("11", Volume="4")              # someone, in the Sonos app
        await save(app, pilot)
        assert app.screen is app.screen_stack[0]
        assert app.status_text.startswith("the alarms changed since they were shown")
        assert app.shown.found.version == fake.version and app.shown.alarm("11").volume == 4
    assert fake.writes == [] and journal() == []
    assert fake.alarms["2"]["Volume"] != "9"


@pytest.mark.parametrize("new", [False, True])
@pilot
async def test_dry_run_saves_nothing_to_speaker_or_journal(fake, providers, new):
    app = make(dry_run=True)
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ed = await (open_new(app, pilot) if new else open_edit(app, pilot, "2"))
        ed.query_one("#volume").value = "9"
        await save(app, pilot)
        said = ("[dry-run] would create an alarm: Sonos Roam 07:00:00 weekdays (on, vol 9)"
                if new else "[dry-run] would update alarm 2: Sonos Roam 08:20:00")
        assert app.status_text.startswith(said)
    assert fake.writes == [] and not play.INTERVENTION_LOG.exists()


# ---- the command -----------------------------------------------------------------

def test_alarm_with_no_verb_opens_the_tui(monkeypatch):
    seen = {}
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def run(self):
        seen["dry_run"], seen["house"] = self.dry_run, self.household()
    monkeypatch.setattr(AlarmApp, "run", run)
    monkeypatch.setattr(alarm_cli, "_household", lambda args: ("house", args.anchor))
    assert cli.main(["alarm", "--dry-run", "--anchor", "10.0.0.11"]) == 0
    assert seen == {"dry_run": True, "house": ("house", "10.0.0.11")}
    assert cli.main(["alarm"]) == 0
    assert seen == {"dry_run": False, "house": ("house", None)}


def test_every_alarm_verb_takes_the_flags_given_before_it():
    parser = cli.build_parser()
    alarm = next(a for a in parser._subparsers._group_actions[0].choices.items()
                 if a[0] == "alarm")[1]
    verbs = alarm._subparsers._group_actions[0].choices
    assert len(verbs) > 10
    for name, verb in verbs.items():
        assert hasattr(verb.get_default("func"), "__wrapped__"), name


@pytest.mark.parametrize("argv", [["alarm"], ["alarm", "--json"]])
def test_alarm_with_no_verb_and_no_terminal_refuses_rather_than_hang(monkeypatch, capsys, argv):
    monkeypatch.setattr(AlarmApp, "run", lambda self: pytest.fail("opened the TUI"))
    assert cli.main(argv) == 1
    out = capsys.readouterr()
    assert "needs a terminal" in out.out + out.err


@pytest.mark.parametrize("argv", [["disable", "2"], ["enable", "66"], ["rm", "2"], ["try", "2"],
                                  ["edit", "2", "--volume", "9"]])
def test_dry_run_before_a_verb_still_holds(fake, monkeypatch, capsys, argv):
    monkeypatch.setattr(alarm_cli, "_household", lambda args: household())
    assert cli.main(["alarm", "--dry-run", *argv]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert fake.writes == [] and not play.INTERVENTION_LOG.exists()


def test_anchor_before_a_verb_still_holds(fake, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(alarm_cli, "_household",
                        lambda args: seen.append(args.anchor) or household())
    cli.main(["alarm", "--anchor", "10.0.0.13", "list"])
    cli.main(["alarm", "--anchor", "10.0.0.13", "list", "--anchor", "10.0.0.11"])
    assert seen == ["10.0.0.13", "10.0.0.11"]


# ---- t: try now, and the ringing screen --------------------------------------------

@pytest.fixture
def ring(monkeypatch):
    """The speakers' AlarmClock and AVTransport; nothing is ringing yet."""
    f = FakeTransport()
    monkeypatch.setattr(devices.requests, "post", f.post)
    monkeypatch.setattr(clock, "last_change", lambda ip, wait=3.0: f.state.get(ip))
    return f


def av_writes(ring):
    return [(a, args) for _, a, args in ring.av_writes]


def ringing(app):
    return isinstance(app.screen, RingingScreen)


async def poll(app, pilot):
    app.check()
    await settle(app, pilot)


@pilot
async def test_the_ringing_screen_comes_with_the_alarm_and_leaves_with_it(ring):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        assert not ringing(app)
        ring.running[ROAM_IP] = RUNNING             # alarm 34 goes off on the Roam
        await poll(app, pilot)
        assert ringing(app)
        shown = text(app.screen, "#ring-text")
        # the recording's stamp was an older build's local time; read as UTC
        # it is off by the offset (the slice's non-goal), as such a fire would be
        assert "Sonos Roam is ringing" in shown and "since 07:58" in shown
        assert "[ x  off ]" in shown and "[ z  snooze 10m ▾ ]" in shown
        assert "Sonos Roam" in shown.splitlines()[1] and "11 mid" not in shown
        assert alarm_cli.brief(app.shown.house, app.shown.alarm("34")) in shown
        assert "snooze 10m" in shown
        assert "(__)___(__)" in text(app.screen, "#ring-art")
        del ring.running[ROAM_IP]                   # it stops by itself
        await poll(app, pilot)
        assert not ringing(app)
        assert ring.av_writes == [] and ring.writes == []


@pilot
async def test_it_is_up_at_once_when_the_alarm_is_already_ringing(ring):
    household_clock(ring, "2026-10-08 15:40:00", "2026-10-08 22:40:00")
    ring.running[ROAM_IP] = FIRED_ITSELF        # went off by itself before the TUI opened
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)            # no poll of ours: the first list read starts one
        assert ringing(app)
        assert text(app.screen, "#ring-text").splitlines()[1].endswith(" · since 15:36")


@pytest.mark.parametrize("time_format, said", [("INV", "15:36"), ("12H", "3:36 PM")])
@pilot
async def test_the_ringing_screen_says_since_in_the_households_own_time(ring, time_format, said):
    # The poster's Roam, 2026-10-08: an alarm at 15:36 said "since 22:36", the UTC time.
    household_clock(ring, "2026-10-08 15:40:00", "2026-10-08 22:40:00", time_format)
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ring.running[ROAM_IP] = FIRED_ITSELF
        await poll(app, pilot)
        assert text(app.screen, "#ring-text").splitlines()[1].endswith(f" · since {said}")


@pytest.mark.parametrize("echo, said", [(None, " · since 08:20"), ("garbage", "")])
@pilot
async def test_t_sends_utc_and_its_echo_shows_as_the_households_time(ring, echo, said):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, "2")
        await pilot.press("t")
        await settle(app, pilot)
        [(ip, _, sent)] = ring.av_writes
        assert sent["LoggedStartTime"] == "2026-10-04 15:20:01"         # the household's UTC
        ring.running[ip] = FIRED_ITSELF | {
            "AlarmID": "2", "LoggedStartTime": echo or sent["LoggedStartTime"]}
        await poll(app, pilot)
        assert ringing(app)
        line = text(app.screen, "#ring-text").splitlines()[1]
        assert ("since" in line) is bool(said) and line.endswith(said)
    run = next(j for j in journal() if j["action"] == "alarm_run")
    assert run["logged_start"] == "2026-10-04 15:20:01"


def test_the_clock_swings():
    assert clock_art(0) != clock_art(1)


def centre(line: str) -> int:
    """Twice the column a line is centred on (so a half column counts)."""
    return (len(line) - len(line.lstrip())) + (len(line.rstrip()) - 1)


def test_the_clock_is_drawn_straight_in_both_frames():
    # The poster's screenshot: bells and head off to one side of the face.
    frames = [clock_art(f).splitlines() for f in (0, 1)]
    for lines in frames:
        assert len({centre(line) for line in lines}) == 1, lines
    assert [len(l) for l in frames[0]] == [len(l) for l in frames[1]]
    assert centre(frames[0][0]) == centre(frames[1][0])


async def click_on(app, pilot, action: str) -> None:
    """Click the ringing screen's control for `action` (stop, snooze, length)
    where it is drawn: the first cell of #ring-text whose link names it."""
    w = app.screen.query_one("#ring-text")
    for y in range(w.region.y, w.region.bottom):
        for x in range(w.region.x, w.region.right):
            if app.screen.get_style_at(x, y).meta.get("@click") == f"screen.{action}":
                await pilot.click(offset=(x, y))
                return
    raise AssertionError(f"no {action} control on the ringing screen")


@pilot
async def test_the_clock_sits_level_with_its_text_in_a_box_its_own_size(ring):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        screen = app.screen
        art, words = screen.query_one("#ring-art"), screen.query_one("#ring-text")
        drawn = clock_art(screen.frame).splitlines()
        assert [l.rstrip() for l in str(art.render()).splitlines()] == [l.rstrip() for l in drawn]
        assert art.region.width <= max(len(l) for l in drawn) + 4
        first = words.region.y + words.styles.padding.top
        assert art.region.y <= first < art.region.y + len(drawn) // 2


@pilot
async def test_a_click_on_off_stops_it_through_the_journalled_stop(ring):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        await click_on(app, pilot, "stop")
        await settle(app, pilot)
        assert av_writes(ring) == [("Stop", {"InstanceID": "0"})]
        assert app.status_text == "stopped the alarm in Sonos Roam"
        assert not ringing(app)
    assert [j["action"] for j in journal()] == ["alarm_stop"]


@pilot
async def test_a_click_on_the_length_cycles_it_and_on_snooze_snoozes_for_it(ring):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        await click_on(app, pilot, "length")
        await pilot.pause()
        assert "[ z  snooze 15m ▾ ]" in text(app.screen, "#ring-text")
        await click_on(app, pilot, "length")
        await pilot.pause()
        assert "[ z  snooze 30m ▾ ]" in text(app.screen, "#ring-text")
        await click_on(app, pilot, "snooze")
        await settle(app, pilot)
        assert av_writes(ring) == [("SnoozeAlarm", {"InstanceID": "0", "Duration": "00:30:00"})]
        assert not ringing(app)
    assert [j["action"] for j in journal()][:1] == ["alarm_snooze"]
    assert journal()[0]["minutes"] == 30


@pilot
async def test_a_second_click_while_the_stop_is_going_writes_nothing_more(ring, monkeypatch):
    import threading
    gate = threading.Event()
    stop = clock.stop_alarm
    def slow_stop(*a, **k):
        gate.wait(5)                                # the speaker takes its time answering
        return stop(*a, **k)
    monkeypatch.setattr(clock, "stop_alarm", slow_stop)
    ring.running[ROAM_IP] = RUNNING
    app = make()
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await settle(app, pilot)
            await poll(app, pilot)
            await click_on(app, pilot, "stop")
            await pilot.pause(0.1)
            assert app.screen.busy
            await click_on(app, pilot, "stop")
            await click_on(app, pilot, "snooze")
            await pilot.press("x", "z")
            gate.set()
            await settle(app, pilot)
            assert not ringing(app)
    finally:
        gate.set()
    assert av_writes(ring) == [("Stop", {"InstanceID": "0"})]
    assert [j["action"] for j in journal()] == ["alarm_stop"]


@pilot
async def test_a_bracket_in_what_rings_is_shown_as_it_is(ring):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        app.screen.update(dict(app.screen.row, room="[b]Den[/b]", what="KALX [live]"), ROAM_IP)
        await pilot.pause()
        shown = text(app.screen, "#ring-text")
        assert "[b]Den[/b] is ringing" in shown and "KALX [live]" in shown


@pilot
async def test_x_stops_it_through_the_journalled_stop(ring):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        await pilot.press("x")
        await settle(app, pilot)
        assert av_writes(ring) == [("Stop", {"InstanceID": "0"})]
        assert app.status_text == "stopped the alarm in Sonos Roam"
        assert not ringing(app)
        await poll(app, pilot)                      # the fake still says ringing: not again
        assert not ringing(app)
    assert [j["action"] for j in journal()] == ["alarm_stop"]
    assert journal()[0]["alarm_id"] == "34"


@pilot
async def test_z_snoozes_ten_minutes_and_m_picks_the_length(ring):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        await pilot.press("m")
        assert "snooze 15m" in text(app.screen, "#ring-text")
        await pilot.press("m", "m", "m")            # 30, 5, 10
        assert "snooze 10m" in text(app.screen, "#ring-text")
        await pilot.press("z")
        await settle(app, pilot)
        assert av_writes(ring) == [("SnoozeAlarm", {"InstanceID": "0", "Duration": "00:10:00"})]
        assert not ringing(app)
    assert [j["action"] for j in journal()][:1] == ["alarm_snooze"]
    assert journal()[0]["minutes"] == 10


@pilot
async def test_x_does_nothing_once_the_alarm_has_stopped(ring):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        del ring.running[ROAM_IP]                   # stopped between the poll and the key
        await pilot.press("x")
        await settle(app, pilot)
        assert ring.av_writes == [] and "any more" in app.status_text
        assert not ringing(app)


@pilot
async def test_a_refused_stop_is_said_and_the_screen_stays(ring):
    ring.running[ROAM_IP] = RUNNING
    ring.refuse_av.add("Stop")
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        await pilot.press("x")
        await settle(app, pilot)
        assert ringing(app)
        assert "could not stop the alarm in Sonos Roam" in text(app.screen, "#ring-status")


@pilot
async def test_t_fires_the_alarm_through_run_alarm_journalled(ring):
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, "2")
        await pilot.press("t")
        await settle(app, pilot)
        assert [a for a, _ in av_writes(ring)] == ["RunAlarm"]
        assert av_writes(ring)[0][1]["AlarmID"] == "2"
        assert app.status_text.startswith("fired alarm 2 on ")
    assert "alarm_run" in [j["action"] for j in journal()]


@pytest.mark.parametrize("key", ["t", "x", "z", "click:stop", "click:snooze"])
@pilot
async def test_dry_run_t_x_and_z_write_neither_speaker_nor_journal(ring, key):
    if key != "t":
        ring.running[ROAM_IP] = RUNNING
    app = make(dry_run=True)
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        if key == "t":
            await pick(app, pilot, "2")
        else:
            await poll(app, pilot)
            assert ringing(app)
        if key.startswith("click:"):                # a click on the control, not its key
            await click_on(app, pilot, key.split(":")[1])
        else:
            await pilot.press(key)
        await settle(app, pilot)
        assert app.status_text.startswith("[dry-run] would ")
    assert ring.av_writes == [] and ring.writes == [] and not play.INTERVENTION_LOG.exists()


@pilot
async def test_x_is_refused_when_the_event_says_nothing_rings(ring):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        ring.state[ROAM_IP] = {"AlarmRunning": "0", "SnoozeRunning": "0"}   # a stale answer
        await pilot.press("x")
        await settle(app, pilot)
        assert ring.av_writes == [] and "any more" in app.status_text


@pilot
async def test_the_lists_keys_do_nothing_behind_the_ringing_screen(ring):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, "2")
        await poll(app, pilot)
        await pilot.press("t", "space", "enter", "d", "n", "r")
        await settle(app, pilot)
        assert ringing(app)
        assert app.focused is None or app.focused.screen is not app.screen
    assert ring.av_writes == [] and ring.writes == [] and not play.INTERVENTION_LOG.exists()


@pilot
async def test_a_snoozed_alarm_brings_the_screen_back_when_it_rings_again(ring):
    from datetime import datetime, timedelta, timezone
    t = [datetime.now(timezone.utc)]       # snooze_alarm's own 'now' is the real one
    ring.running[ROAM_IP] = RUNNING
    app = make(now=lambda: t[0])
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        await pilot.press("z")
        await settle(app, pilot)
        assert not ringing(app)
        await poll(app, pilot)                      # still answering, same alarm: hushed
        assert not ringing(app)
        t[0] += timedelta(hours=1)                  # past when the snooze runs out
        await poll(app, pilot)
        assert ringing(app)


@pilot
async def test_an_alarm_made_since_the_list_was_read_is_named_once_it_is_read_again(ring):
    made = ring.alarms.pop("34")
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        ring.alarms["34"] = made                    # made in the Sonos app just now,
        ring.running[ROAM_IP] = RUNNING             # and ringing
        await poll(app, pilot)
        assert ringing(app) and "an alarm" in text(app.screen, "#ring-text")
        await settle(app, pilot)                    # the screen asked for the list again
        await poll(app, pilot)
        assert alarm_cli.brief(app.shown.house, app.shown.alarm("34")) \
            in text(app.screen, "#ring-text")


LIVING_IP = "10.0.0.13"


@pytest.mark.parametrize("key, action", [("x", "Stop"), ("z", "SnoozeAlarm")])
@pilot
async def test_x_and_z_write_to_the_room_the_screen_shows(ring, key, action):
    from tests.test_alarm_cli import LIVING
    ring.alarms["400"] = dict(ring.alarms["2"], ID="400", RoomUUID=LIVING)
    ring.children["400"] = []
    ring.running[LIVING_IP] = dict(RUNNING, AlarmID="400")
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        assert "Living Room" in text(app.screen, "#ring-text")
        ring.running[ROAM_IP] = RUNNING             # a second room rings; the display moves
        del ring.running[LIVING_IP]
        await poll(app, pilot)
        assert "Sonos Roam" in text(app.screen, "#ring-text")
        await pilot.press(key)
        await settle(app, pilot)
    assert [(ip, a) for ip, a, _ in ring.av_writes] == [(ROAM_IP, action)]


@pytest.mark.parametrize("key", ["x", "z"])
@pilot
async def test_a_failed_read_before_x_or_z_is_said_not_a_crash(ring, monkeypatch, key):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        def boom(ip, events=True):
            raise TimeoutError("read timed out")
        monkeypatch.setattr(clock, "alarm_now", boom)
        await pilot.press(key)
        await settle(app, pilot)
        assert ringing(app)
        assert "read timed out" in text(app.screen, "#ring-status")
    assert ring.av_writes == []


@pilot
async def test_the_events_decide_as_they_do_for_alarm_status(ring):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        assert ringing(app)
        # The properties still name the alarm, but the event says plainly: nothing rings.
        ring.state[ROAM_IP] = {"AlarmRunning": "0", "SnoozeRunning": "0"}
        await poll(app, pilot)
        assert not ringing(app)
        # A snooze is not a ringing alarm.
        ring.state[ROAM_IP] = {"AlarmRunning": "0", "SnoozeRunning": "1"}
        await poll(app, pilot)
        assert not ringing(app)
        # The event alone, with no properties answer.
        del ring.running[ROAM_IP]
        ring.state[ROAM_IP] = {"AlarmRunning": "1", "SnoozeRunning": "0"}
        await poll(app, pilot)
        assert ringing(app) and "Sonos Roam is ringing" in text(app.screen, "#ring-text")


@pilot
async def test_a_stop_finishing_after_the_screen_moved_on_hushes_only_what_it_stopped(
        ring, monkeypatch):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        stopped = app.ringing.key
        other = ("10.0.0.13", "400", "2026-10-04 15:00:00")
        real = clock.stop_alarm

        def late(ip, alarm_id=None):                # polling moves the screen mid-write
            app.call_from_thread(setattr, app.ringing, "key", other)
            real(ip, alarm_id)
        monkeypatch.setattr(clock, "stop_alarm", late)
        await pilot.press("x")
        await settle(app, pilot)
        assert ringing(app) and other not in app.hushed and stopped in app.hushed


@pilot
async def test_a_stop_finishing_after_the_screen_was_replaced_leaves_the_new_one(
        ring, monkeypatch):
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        old = app.ringing
        other = ("10.0.0.13", "400", "2026-10-04 15:00:00")
        real = clock.stop_alarm

        def late(ip, alarm_id=None):                # A's screen goes, B's comes, mid-write
            def swap():
                app._hush_screen()
                app._ring(old.key, "10.0.0.13", {"room": "Living Room", "what": "x"})
                app.ringing.key = old.key           # even the very same key
            app.call_from_thread(swap)
            real(ip, alarm_id)
        monkeypatch.setattr(clock, "stop_alarm", late)
        await pilot.press("x")
        await settle(app, pilot)
        assert app.ringing is not old and ringing(app)


@pilot
async def test_a_slow_poll_is_waited_for_not_voided(ring, monkeypatch):
    import threading
    release = threading.Event()
    real = clock.alarm_now

    def slow(ip, events=True):                      # each read takes longer than the interval
        release.wait(5)
        return real(ip, events)
    monkeypatch.setattr(clock, "alarm_now", slow)
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause(0.3)
        app.check()
        app.check()                                 # ticks while one is still reading: no second
        await pilot.pause(0.2)
        assert app.polling and not ringing(app)
        release.set()
        await settle(app, pilot)
        assert ringing(app) and not app.polling


@pilot
async def test_a_poll_reading_while_x_succeeds_cannot_reopen_that_alarm(ring, monkeypatch):
    import threading
    ring.running[ROAM_IP] = RUNNING
    app = make()
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        await poll(app, pilot)
        assert ringing(app)
        gate, real = threading.Event(), clock.alarm_now

        def late(ip, events=True):                  # read ringing, then held up on the next room
            got = real(ip, events)
            if ip == "10.0.0.13":
                gate.wait(5)
            return got
        monkeypatch.setattr(clock, "alarm_now", late)
        app.check()
        await pilot.pause(0.2)
        monkeypatch.setattr(clock, "alarm_now", real)
        await pilot.press("x")                      # stops and hushes while the poll is held
        await pilot.pause(0.3)
        assert not ringing(app)
        gate.set()
        await settle(app, pilot)
        assert not ringing(app)
