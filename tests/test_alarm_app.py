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
from textual.widgets import OptionList

from tests.test_alarm_cli import GONE, household
from tests.test_alarm_clock import FakeClock
from twiddle import alarm_cli, cli, devices, play
from twiddle.alarms import clock
from twiddle.alarms.app import AlarmApp, AskScreen, TypeIdScreen, alarm_line
from twiddle.alarms.model import parse_alarms
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
        assert text(app, "#keys") == "space on/off  d delete  r refresh  q quit"
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
