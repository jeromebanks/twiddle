"""`twiddle viz`: the visualizers without dial or scene, for writing them.

All read-only, and none touches a speaker:

- `viz list`                  -- every visualizer, built-in and plugin
- `viz snapshot MODE`         -- one frame as text, from a made-up signal
- `viz demo [MODE]`           -- the full screen, on a made-up signal
                                 (`--url` / `--station` taps a real stream instead)
"""
from __future__ import annotations

import time

from ..control_cli import emit

# `signals.SIGNALS`' keys, spelled out so `--help` needn't import numpy
# (a test holds the two together).
SIGNAL_NAMES = ["inverted", "music", "noise", "silence", "sine"]


def _size(text: str) -> tuple[int, int]:
    try:
        w, h = (int(x) for x in text.lower().split("x"))
    except ValueError:
        raise SystemExit(f"twiddle: --size wants WxH, like 80x24, not {text!r}") from None
    if w < 1 or h < 1:
        raise SystemExit("twiddle: --size must be at least 1x1")
    return w, h


def _mode(name: str | None) -> str | None:
    from . import base
    base.load()
    if name is not None and name not in base.REGISTRY:
        raise SystemExit(f"twiddle: no visualizer {name!r}; `twiddle viz list` names them")
    return name


def cmd_list(args) -> int:
    from . import base
    warnings = base.load()
    rows = [{"name": e.name, "blurb": e.blurb, "palette": e.palette, "origin": e.origin}
            for e in base.REGISTRY.values()]
    lines = [f"{r['name']:12} {r['blurb']}" + ("" if r["origin"] == "builtin" else f"   [{r['origin']}]")
             for r in rows]
    lines += [f"⚠ {w}" for w in warnings]
    lines.append(f"\nplugins: {base.PLUGIN_DIR}/*.py")
    return emit(args, {"visualizers": rows, "warnings": warnings}, "\n".join(lines))


def cmd_snapshot(args) -> int:
    from rich.console import Console

    from . import base, signals
    from .analysis import FFT_N, Analyser
    from .canvas import Palette, to_strips

    name = _mode(args.mode)
    w, h = _size(args.size)
    runner = base.Runner(name)
    an = Analyser()
    gen = signals.SIGNALS[args.signal]()
    t0 = time.perf_counter()
    for _ in range(args.frames):
        codes, heat = runner.draw(an.frame(gen.window(FFT_N), 1 / 30), w, h)
    per_ms = (time.perf_counter() - t0) / max(args.frames, 1) * 1000
    if runner.error:
        raise SystemExit(f"twiddle: {runner.error}")
    if args.color:
        console = Console(force_terminal=True)
        for strip in to_strips(codes, heat, Palette.named(args.palette or base.REGISTRY[name].palette)):
            console.print(_render(strip), highlight=False)
    else:
        for row in codes:
            print(row.astype("<u4").tobytes().decode("utf-32-le"))
    print(f"-- {name} {w}x{h}, {args.signal}, frame {args.frames}: "
          f"{per_ms:.2f} ms/frame (analysis + render)")
    return 0


def _render(strip):
    from rich.text import Text
    t = Text()
    for seg in strip:
        t.append(seg.text, seg.style)
    return t


class SyntheticTap:
    """An `AudioTap` look-alike fed by `signals`, for `viz demo`."""

    def __init__(self, signal: str):
        from . import signals
        self.gen = signals.SIGNALS[signal]()
        self.error = None
        self.last_data = time.monotonic()
        self.stale = False
        self._t0 = time.monotonic()

    def start(self):
        return self

    def stop(self) -> None:
        pass

    def window(self, n: int, delay_s: float = 0.0):
        # Advance with the wall clock, not per call, as a live tap does.
        from .analysis import RATE
        self.gen.pos = int((time.monotonic() - self._t0) * RATE)
        return self.gen.window(n)


def cmd_demo(args) -> int:
    from textual.app import App

    from .. import stations
    from .screen import VizScreen
    from .source import TapSource
    from .tap import AudioTap

    name = _mode(args.mode)
    if args.station:
        st = stations.STATIONS.get(args.station)
        if st is None:
            raise SystemExit(f"twiddle: no station {args.station!r}; `twiddle stations` lists them")
        src = TapSource(st.url, f"{st.name} (tapped here only; nothing is tuned)", f"demo:{st.key}")
    elif args.url:
        src = TapSource(args.url, args.url, "demo:url")
    else:
        src = TapSource(f"synthetic:{args.signal}", f"a made-up signal: {args.signal}", "demo")

    def tap(url: str):
        if url.startswith("synthetic:"):
            return SyntheticTap(url.removeprefix("synthetic:"))
        return AudioTap(url)

    prefs: dict = {"mode": name} if name else {}

    class Demo(App):
        TITLE = "twiddle viz demo"

        def on_mount(self):
            screen = VizScreen(lambda: src, tap_factory=tap,
                               load_prefs=lambda: dict(prefs) if name else _saved(),
                               save_prefs=_keep)
            self.push_screen(screen, lambda _r=None: self.exit())

    Demo().run()
    return 0


def _saved() -> dict:
    from .screen import _load_prefs
    return _load_prefs()


def _keep(prefs: dict) -> None:
    from .screen import _save_prefs
    _save_prefs(prefs)


def register(sub, parents=None):
    kw = {"parents": parents} if parents else {}
    p = sub.add_parser(**kw, name="viz",
                       help="the music visualizers (v in dial / scene): list, snapshot, demo")
    vsub = p.add_subparsers(dest="viz_cmd", metavar="<command>", required=True)

    ls = vsub.add_parser(**kw, name="list", help="every visualizer, built-in and plugin")
    ls.set_defaults(func=cmd_list)

    sn = vsub.add_parser(**kw, name="snapshot",
                         help="draw one frame as text from a made-up signal (for writing one)")
    sn.add_argument("mode")
    sn.add_argument("--size", default="80x24", help="WxH in cells (default 80x24)")
    sn.add_argument("--signal", choices=SIGNAL_NAMES, default="music")
    sn.add_argument("--frames", type=int, default=45,
                    help="frames to run first, so trails and particles build (default 45)")
    sn.add_argument("--color", action="store_true", help="with the palette's colours")
    sn.add_argument("--palette", default=None, help="a palette name (default: the visualizer's own)")
    sn.set_defaults(func=cmd_snapshot)

    dm = vsub.add_parser(**kw, name="demo",
                         help="the full-screen visualizer on a made-up signal, or a stream")
    dm.add_argument("mode", nargs="?", default=None)
    dm.add_argument("--signal", choices=SIGNAL_NAMES, default="music")
    dm.add_argument("--url", default=None, help="tap this stream instead (plays nothing)")
    dm.add_argument("--station", default=None, help="tap this station's stream (plays nothing)")
    dm.set_defaults(func=cmd_demo)
