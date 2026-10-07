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
from tests.test_alarm_clock import FakeClock
from twiddle import alarm_cli, cli, devices, play
from twiddle.alarms import clock
from twiddle.alarms.app import (AlarmApp, AskScreen, EditorScreen, SourcePicker, TypeIdScreen,
                                alarm_line)
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


def make(dry_run=False):
    return AlarmApp(household=household, dry_run=dry_run)


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
        assert text(app, "#keys") == "n new  enter edit  space on/off  d delete  r refresh  q quit"
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
