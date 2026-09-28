"""The full-window visualizer both TUIs push on `v`.

It asks its `source` callable what to tap (a `TapSource`), in a worker, on
open and every few seconds after, so a station change or a relay restart is
picked up while it's showing. With nothing to tap it draws a still card
saying why -- never an animation that could pass for a reaction to music.

Only its own keys work while it's up (it's modal): nothing reaches the
app's playback keys. Keys: ←/→ visualizer, c colours, , and . move the picture 0.25 s earlier or
later to meet a speaker's buffering, i shows the overlay, v / esc / q close.
Mode, palette and each output's delay are remembered (dial's state file).
"""
from __future__ import annotations

import textwrap
import time
from collections.abc import Callable

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from textual import work
from textual.binding import Binding
from textual.color import Color
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget

from . import base
from .analysis import FFT_N, Analyser
from .canvas import PALETTES, Palette, to_strips
from .source import TapSource
from .tap import MAX_DELAY_S, AudioTap

FPS = 30
RESOLVE_S = 5.0
OVERLAY_S = 4.0
DELAY_STEP = 0.25
STATE_KEY = "viz"


def _load_prefs() -> dict:
    from ..dial import state
    prefs = state.get_state(STATE_KEY) or {}
    return prefs if isinstance(prefs, dict) else {}


def _save_prefs(prefs: dict) -> None:
    from ..dial import state
    try:
        state.set_state(STATE_KEY, prefs)
    except OSError:
        pass


class VizCanvas(Widget):
    DEFAULT_CSS = "VizCanvas { width: 1fr; height: 1fr; }"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.strips: list[Strip] = []

    def render_line(self, y: int) -> Strip:
        if y < len(self.strips):
            return self.strips[y]
        return Strip.blank(self.size.width)


class VizScreen(ModalScreen):
    """Modal so the app's own keys stop here: with only the picture showing,
    dial's enter / volume / R and scene's space / R would otherwise act on a
    speaker unseen, confirmations included. Opaque, so nothing shows through."""
    DEFAULT_CSS = """
    VizScreen { background: $background; }
    VizScreen:ansi { background: $background; }
    """
    BINDINGS = [
        Binding("escape,v,q", "close", "Close"),
        Binding("right,n,l", "cycle(1)", "Next"),
        Binding("left,p,h", "cycle(-1)", "Previous"),
        Binding("c", "palette", "Colours"),
        Binding("full_stop", "delay(1)", "Later"),
        Binding("comma", "delay(-1)", "Sooner"),
        Binding("i,question_mark", "overlay", "Info"),
    ]

    def __init__(self, source: Callable[[], TapSource], *,
                 tap_factory: Callable[[str], AudioTap] = AudioTap,
                 load_prefs: Callable[[], dict] = _load_prefs,
                 save_prefs: Callable[[dict], None] = _save_prefs,
                 plugins=base.PLUGIN_DIR, fps: int = FPS):
        super().__init__()
        self.source_fn = source
        self.tap_factory = tap_factory
        self.save_prefs = save_prefs
        self.prefs = load_prefs()
        self.warnings = base.load(plugins)
        names = base.names()
        mode = self.prefs.get("mode")
        self.mode = mode if mode in names else names[0]
        self.runner = base.Runner(self.mode)
        self.palette_choice: str | None = self.prefs.get("palette")
        if self.palette_choice not in PALETTES:
            self.palette_choice = None      # None: each visualizer's own
        self._palette: Palette | None = None
        self.analyser = Analyser()
        self.src: TapSource | None = None
        self.tap: AudioTap | None = None
        self.fps = fps
        self._last = time.monotonic()
        self._overlay_until = time.monotonic() + OVERLAY_S
        self._pinned = False
        self.frames = 0

    def compose(self):
        yield VizCanvas(id="viz")

    def on_mount(self) -> None:
        self.resolve()
        self.set_interval(1 / self.fps, self.tick)
        self.set_interval(RESOLVE_S, self.resolve)

    def on_unmount(self) -> None:
        self._stop_tap()

    # ---- the source ---------------------------------------------------------

    @work(thread=True, exclusive=True, group="viz-source")
    def resolve(self) -> None:
        try:
            src = self.source_fn()
        except Exception as exc:
            src = TapSource(None, "?", "", f"couldn't tell what's playing: {type(exc).__name__}: {exc}")
        try:
            self.app.call_from_thread(self.set_source, src)
        except RuntimeError:
            pass        # the app is closing

    def set_source(self, src: TapSource) -> None:
        changed = self.src is None or src.url != self.src.url
        self.src = src
        if not changed:
            return
        self._stop_tap()
        self.analyser = Analyser()
        if src.ok:
            self.tap = self.tap_factory(src.url).start()
        self._show_overlay()

    def _stop_tap(self) -> None:
        tap, self.tap = self.tap, None
        if tap is not None:
            tap.stop()

    @property
    def delay(self) -> float:
        key = self.src.key if self.src else ""
        return float((self.prefs.get("delay") or {}).get(key, 0.0))

    # ---- drawing --------------------------------------------------------------

    def palette(self) -> Palette:
        name = self.palette_choice or base.REGISTRY[self.mode].palette
        if self._palette is None or self._palette.name != name:
            self._palette = Palette.named(name, self.app.current_theme)
        return self._palette

    def tick(self) -> None:
        canvas = self.query_one(VizCanvas)
        w, h = canvas.size.width, canvas.size.height
        if w <= 0 or h <= 0:
            return
        now = time.monotonic()
        dt, self._last = now - self._last, now
        tap = self.tap
        if tap is None or tap.stale:
            strips = self._card(w, h, tap)
        else:
            f = self.analyser.frame(tap.window(FFT_N, self.delay), dt)
            codes, heat = self.runner.draw(f, w, h)
            strips = to_strips(codes, heat, self.palette())
        canvas.strips = self._with_overlay(strips, w)
        canvas.refresh()
        self.frames += 1

    def _card(self, w: int, h: int, tap: AudioTap | None) -> list[Strip]:
        """Still, and plainly not music: what would play, and why it can't."""
        src = self.src
        if src is None:
            lines = ["finding what's playing…"]
        elif tap is None:
            lines = ["no signal", "", src.label, "", *textwrap.wrap(src.reason, max(20, w - 8))]
        elif tap.error:
            lines = ["no signal", "", src.label, "", *textwrap.wrap(tap.error, max(20, w - 8))]
        elif tap.last_data is None:
            lines = [f"tuning in to {src.label}…"]
        else:
            lines = ["the stream has stalled", "", src.label, "", "reconnecting…"]
        theme = self.app.current_theme
        fg = Color.parse(getattr(theme, "foreground", None) or "#bbbbbb")
        bg = Color.parse(getattr(theme, "background", None) or "#101010")
        style = Style(color=fg.blend(bg, 0.45).rich_color, bgcolor=bg.rich_color)
        blank = Style(bgcolor=bg.rich_color)
        top = max(0, (h - len(lines)) // 2)
        out = []
        for y in range(h):
            i = y - top
            text = lines[i] if 0 <= i < len(lines) else ""
            text = text[:w]
            pad = max(0, (w - cell_len(text)) // 2)
            out.append(Strip([Segment(" " * pad, blank), Segment(text, style)]).extend_cell_length(w, blank))
        return out

    def _overlay_lines(self) -> list[str]:
        e = base.REGISTRY[self.mode]
        pal = self.palette_choice or f"{e.palette} (its own)"
        i = base.names().index(self.mode) + 1
        lines = [f" {e.name} ({i}/{len(base.names())}): {e.blurb} · colours: {pal} "]
        if self.src is not None:
            lines.append(f" {self.src.label} ")
        if self.tap is not None:
            lines.append(f" the stream, not the speaker · picture delayed {self.delay:+.2f}s ")
        lines.append(" ←/→ visualizer  c colours  ,/. delay  i info  v close ")
        if self.runner.error:
            lines.append(f" ⚠ {self.runner.error} ")
        lines += [f" ⚠ {w} " for w in self.warnings]
        return lines

    def _with_overlay(self, strips: list[Strip], w: int) -> list[Strip]:
        if not (self._pinned or time.monotonic() < self._overlay_until or self.runner.error):
            return strips
        theme = self.app.current_theme
        fg = Color.parse(getattr(theme, "foreground", None) or "#e0e0e0")
        panel = Color.parse(getattr(theme, "panel", None) or getattr(theme, "surface", None) or "#303030")
        style = Style(color=fg.rich_color, bgcolor=panel.rich_color)
        out = list(strips)
        for y, text in enumerate(self._overlay_lines()[:len(out)]):
            seg = Strip([Segment(text, style)]).crop(0, w)
            n = seg.cell_length
            out[y] = Strip.join([seg, out[y].crop(n, w)])
        return out

    # ---- keys -------------------------------------------------------------------

    def _show_overlay(self) -> None:
        self._overlay_until = time.monotonic() + OVERLAY_S

    def action_close(self) -> None:
        self._stop_tap()
        self.app.pop_screen()

    def action_cycle(self, step: int) -> None:
        names = base.names()
        self.mode = names[(names.index(self.mode) + step) % len(names)]
        self.runner = base.Runner(self.mode)
        self.prefs["mode"] = self.mode
        self.save_prefs(self.prefs)
        self._show_overlay()

    def action_palette(self) -> None:
        order = [None, *PALETTES]
        i = order.index(self.palette_choice) if self.palette_choice in order else 0
        self.palette_choice = order[(i + 1) % len(order)]
        self.prefs["palette"] = self.palette_choice
        self.save_prefs(self.prefs)
        self._show_overlay()

    def action_delay(self, step: int) -> None:
        if self.src is None:
            return
        delays = dict(self.prefs.get("delay") or {})
        d = round(min(max(self.delay + step * DELAY_STEP, 0.0), MAX_DELAY_S), 2)
        delays[self.src.key] = d
        self.prefs["delay"] = delays
        self.save_prefs(self.prefs)
        self._show_overlay()

    def action_overlay(self) -> None:
        self._pinned = not self._pinned
        self._show_overlay()
