"""The splash `dial` opens on: big letters, Twiddle waving, the slogan.

It goes up at the end of `DialApp.on_mount`, so everything the app starts
with -- the station feed, the output's state, the logos -- is already
running underneath; the splash only reports how far the feed has got.
Any key or a click closes it, and so does `timeout` seconds of nothing.

The key that closes it goes nowhere else. Behind it, `enter` tunes a
speaker, `+` writes the volume and `q` quits: a key meant as "hello" must
not do any of those. It's a modal screen and the key is stopped here.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from textual import events
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget

TIMEOUT_S = 20.0
FPS = 20


class SplashCanvas(Widget):
    DEFAULT_CSS = "SplashCanvas { width: 1fr; height: 1fr; }"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.strips: list[Strip] = []

    def render_line(self, y: int) -> Strip:
        if y < len(self.strips):
            return self.strips[y]
        return Strip.blank(self.size.width)


class SplashScreen(ModalScreen):
    def __init__(self, status: Callable[[], str] = lambda: "", *,
                 timeout: float | None = None, fps: int = FPS):
        super().__init__()
        from ..viz.splash import SplashArt     # numpy, only when shown
        self.art = SplashArt()
        self.status = status
        self.timeout = TIMEOUT_S if timeout is None else timeout
        self.fps = fps
        self._t0 = self._last = time.monotonic()
        self.frames = 0

    def compose(self):
        yield SplashCanvas()

    def on_mount(self) -> None:
        self.styles.background = self.art.palette.background     # opaque: nothing shows through
        self.set_interval(1 / self.fps, self.tick)
        self.set_timer(self.timeout, self.close)

    def tick(self) -> None:
        canvas = self.query_one(SplashCanvas)
        w, h = canvas.size.width, canvas.size.height
        if w <= 0 or h <= 0:
            return
        now = time.monotonic()
        dt, self._last = now - self._last, now
        try:
            status = self.status()
        except Exception:          # a progress line is never worth a crash
            status = ""
        canvas.strips = self.art.draw(w, h, now - self._t0, dt, status)
        canvas.refresh()
        self.frames += 1

    def close(self) -> None:
        if self.is_current:
            self.dismiss()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        self.close()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.close()
