"""The full-window visualizer, driven by Textual's pilot: `v` opens it in
both apps, the keys work, and it never leaves a tap running."""
import asyncio

import numpy as np
from textual.app import App

from twiddle.viz import base
from twiddle.viz.cli import SyntheticTap
from twiddle.viz.screen import VizCanvas, VizScreen
from twiddle.viz.source import TapSource


class RecordingTap(SyntheticTap):
    made: list = []

    def __init__(self, url):
        super().__init__("music")
        self.url = url
        self.stopped = False
        RecordingTap.made.append(self)

    def stop(self):
        self.stopped = True


def options(prefs=None):
    RecordingTap.made = []
    store = {"prefs": dict(prefs or {})}

    def save(p):
        store["prefs"] = dict(p)
    return store, {"tap_factory": RecordingTap, "load_prefs": lambda: dict(store["prefs"]),
                   "save_prefs": save, "plugins": None}


def _text(screen) -> str:
    return "\n".join(s.text for s in screen.query_one(VizCanvas).strips)


class Host(App):
    def __init__(self, src, **kw):
        super().__init__()
        self.src, self.kw = src, kw

    def on_mount(self):
        self.push_screen(VizScreen(lambda: self.src, **self.kw))


def test_it_draws_the_music_cycles_and_remembers():
    store, kw = options()
    src = TapSource("synthetic:music", "KALX on Roam", "room:Roam")

    async def run():
        app = Host(src, **kw)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            screen = app.screen
            assert isinstance(screen, VizScreen) and RecordingTap.made[0].url == "synthetic:music"
            assert screen.frames > 3
            text = _text(screen)
            assert "KALX on Roam" in text and "not the speaker" in text    # the overlay
            first = screen.mode
            await pilot.press("right")
            assert screen.mode != first and store["prefs"]["mode"] == screen.mode
            await pilot.press("c")
            assert store["prefs"]["palette"] is not None
            await pilot.press("full_stop", "full_stop")
            assert store["prefs"]["delay"] == {"room:Roam": 0.5}
            await pilot.press("v")
            await pilot.pause()
            assert not isinstance(app.screen, VizScreen)
            assert RecordingTap.made[0].stopped
    asyncio.run(run())


def test_with_nothing_to_tap_it_says_why_and_stays_still():
    _store, kw = options()
    src = TapSource(None, "Spotify", "local", "Spotify on this Mac plays through librespot")

    async def run():
        app = Host(src, **kw)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            screen = app.screen
            screen._overlay_until = 0           # past the overlay: just the card
            await pilot.pause(0.1)
            a = _text(screen)
            await pilot.pause(0.2)
            assert "no signal" in a and "librespot" in a
            assert _text(screen) == a           # not animating: nothing to react to
            assert RecordingTap.made == []
    asyncio.run(run())


def test_a_broken_visualizer_is_a_message_not_a_crash():
    @base.visualizer("zz-broken", blurb="raises")
    def broken(f, w, h):
        raise RuntimeError("boom")
    try:
        _store, kw = options({"mode": "zz-broken"})

        async def run():
            app = Host(TapSource("synthetic:music", "x", "k"), **kw)
            async with app.run_test(size=(80, 20)) as pilot:
                await pilot.pause(0.3)
                assert "boom" in _text(app.screen)
        asyncio.run(run())
    finally:
        base.REGISTRY.pop("zz-broken", None)


def test_a_changed_source_swaps_the_tap():
    _store, kw = options()

    async def run():
        app = Host(TapSource("synthetic:music", "one", "a"), **kw)
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause(0.2)
            screen = app.screen
            screen.set_source(TapSource("synthetic:sine", "two", "b"))
            assert RecordingTap.made[0].stopped and RecordingTap.made[1].url == "synthetic:sine"
            screen.set_source(TapSource("synthetic:sine", "two", "b"))   # same: kept
            assert len(RecordingTap.made) == 2
    asyncio.run(run())


def test_plugins_load_and_a_broken_one_is_skipped(tmp_path):
    (tmp_path / "good.py").write_text(
        "from twiddle.viz.base import visualizer\n"
        "from twiddle.viz.canvas import empty\n"
        "@visualizer('zz-plugin', blurb='from a plugin')\n"
        "def plugin(f, w, h):\n    return empty(h, w)\n")
    (tmp_path / "bad.py").write_text("import nonexistent_module_xyz\n")
    try:
        warnings = base.load_plugins(tmp_path)
        assert base.REGISTRY["zz-plugin"].origin.endswith("good.py")
        assert len(warnings) == 1 and "bad.py" in warnings[0]
    finally:
        base.REGISTRY.pop("zz-plugin", None)
