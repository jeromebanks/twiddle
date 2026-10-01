"""The sleep timer: the SOAP it speaks, what it journals, and dial's `z`."""
import asyncio
import json
from datetime import datetime

import pytest

from twiddle import play
from twiddle.dial.output import LocalOutput

from .test_dial import FakeOutput, make_app


@pytest.mark.parametrize("text,seconds", [
    ("0:45:00", 2700), ("1:30:05", 5405), ("00:00:10", 10), ("", None),
    ("NOT_IMPLEMENTED", None), (None, None),
])
def test_parse_hms(text, seconds):
    assert play.parse_hms(text) == seconds


def test_get_sleep_timer_reads_the_remaining_duration(monkeypatch):
    replies = {"on": "<RemainingSleepTimerDuration>0:42:17</RemainingSleepTimerDuration>"
                     "<CurrentSleepTimerGeneration>3</CurrentSleepTimerGeneration>",
               "off": "<RemainingSleepTimerDuration></RemainingSleepTimerDuration>"}
    asked = []

    def av(ip, action, body):
        asked.append(action)
        return replies[ip]
    monkeypatch.setattr(play, "_av", av)
    assert play.get_sleep_timer("on") == 42 * 60 + 17
    assert play.get_sleep_timer("off") is None
    assert asked == ["GetRemainingSleepTimerDuration"] * 2


def _journal():
    return [json.loads(line) for line in play.INTERVENTION_LOG.read_text().splitlines()]


def test_arming_journals_a_span_around_the_stop_it_will_cause(monkeypatch):
    sent = []
    monkeypatch.setattr(play, "_av", lambda ip, action, body: sent.append((action, body)))
    fires = play.set_sleep_timer("10.0.0.4", 1800, source="dial")
    assert sent == [("ConfigureSleepTimer",
                     "<NewSleepTimerDuration>00:30:00</NewSleepTimerDuration>")]
    recs = _journal()
    assert [r["action"] for r in recs] == [
        "configure_sleep_timer", "sleep_timer_fire_start", "sleep_timer_fire_end"]
    start, end = (datetime.fromisoformat(r["ts"]) for r in recs[1:])
    assert start < fires < end                   # the stop lands inside the span
    assert recs[1]["span"] and recs[1]["source"] == "dial"


def test_cancelling_journals_the_call_but_no_span(monkeypatch):
    sent = []
    monkeypatch.setattr(play, "_av", lambda ip, action, body: sent.append(body))
    assert play.set_sleep_timer("10.0.0.4", 0) is None
    assert sent == ["<NewSleepTimerDuration></NewSleepTimerDuration>"]
    assert [r["action"] for r in _journal()] == ["configure_sleep_timer"]


def test_the_macs_timer_is_dials_own_and_can_be_called_off():
    out = LocalOutput(pactl=None, ffmpeg="/bin/true", spawn=lambda *a: None)
    assert out.set_sleep_timer(600) == 600
    assert 598 <= out.state().sleep_s <= 600
    assert out.set_sleep_timer(0) is None
    assert out.state().sleep_s is None
    out.close()


# ---- dial -------------------------------------------------------------------------


class SleepyOutput(FakeOutput):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.timers = []

    def set_sleep_timer(self, seconds):
        self.timers.append(seconds)
        self.st.sleep_s = seconds or None
        return self.st.sleep_s


def run(coro):
    return asyncio.run(coro)


def test_z_steps_through_presets_and_sends_once(monkeypatch):
    from twiddle.dial import app as app_mod
    monkeypatch.setattr(app_mod, "SLEEP_SETTLE_S", 0.2)
    out = SleepyOutput(tuned="dronezone")

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            for _ in range(3):
                await pilot.press("z")
            assert "45 min" in app.sub_title and out.timers == []
            await asyncio.sleep(0.4)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert out.timers == [45 * 60]                      # three presses, one write
            assert "☾ sleep in 45:00" in app.sub_title or "44:5" in app.sub_title
            await pilot.press("z")                              # 45 left: next is 60
            assert "60 min" in app.sub_title
            await pilot.press("Z")
            await asyncio.sleep(0.4)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert out.timers == [45 * 60, 0] and "☾" not in app.sub_title
    run(go())


def test_a_running_timer_shows_in_the_header_on_start():
    out = SleepyOutput(tuned="dronezone")
    out.st.sleep_s = 20 * 60

    async def go():
        app = make_app(out)
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "☾ sleep in 20:00" in app.sub_title or "☾ sleep in 19:5" in app.sub_title
    run(go())


def test_a_sonos_room_uses_the_speakers_own_timer(monkeypatch):
    from .test_dial import sonos
    out, g, _ = sonos(monkeypatch, "x-rincon-mp3radio://ice1.somafm.com/dronezone-128-mp3")
    armed = []
    monkeypatch.setattr(play, "set_sleep_timer",
                        lambda ip, s, **j: armed.append((ip, s, j)))
    monkeypatch.setattr(play, "get_sleep_timer", lambda ip: 1799)
    assert out.set_sleep_timer(1800) == 1799
    assert armed == [(g.ip, 1800, {"source": "dial"})]
    assert out.state().sleep_s == 1799


def test_dry_run_arms_no_timer(monkeypatch):
    from .test_dial import sonos
    out, _, _ = sonos(monkeypatch, "", dry_run=True)
    monkeypatch.setattr(play, "set_sleep_timer", lambda *a, **k: pytest.fail("wrote"))
    assert out.set_sleep_timer(1800) == 1800
