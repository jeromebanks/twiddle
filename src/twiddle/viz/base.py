"""The visualizer contract, and the registry the screen cycles through.

A visualizer turns a `Frame` into two (h, w) numpy arrays:

    codes -- int, one Unicode codepoint per cell (single-width characters only)
    heat  -- float, 0 (cool) .. 1 (hot); the palette makes it a colour

Stateless ones are plain functions; ones with memory (trails, particles,
scrolling history) are classes. Either way, register with `@visualizer`:

    @visualizer("bars", blurb="plain bars")
    def bars(f: Frame, w: int, h: int):
        codes, fill = blocks_up(resample(f.bands, w), h)
        return codes, fill

    @visualizer("trails", blurb="...", palette="ice")
    class Trails(Visualizer):
        def resize(self, w, h): self.field = np.zeros((h, w), np.float32)
        def render(self, f, w, h): ...

`resize` is called before the first `render` and whenever the size changes.
`palette` is the one the screen starts on for this visualizer (the `c` key
still cycles). The shape spektr's plugins have, so its modes port across.

`tests/test_viz_contract.py` runs every registered visualizer through
sizes and signals and holds it to all of the above; a new one gets that
for free. Built-ins live in `modes/`, one per file, imported by
`load_builtins`. Anything in `~/.config/twiddle/viz/*.py` is loaded too; a
broken plugin is skipped with a warning, never raised.
"""
from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .analysis import Frame

PLUGIN_DIR = Path.home() / ".config" / "twiddle" / "viz"


class Visualizer:
    """Subclass for a visualizer with state. `name`, `blurb` and `palette`
    are set by `@visualizer`."""
    name = ""
    blurb = ""
    palette = "theme"

    def resize(self, w: int, h: int) -> None:
        pass

    def render(self, f: Frame, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError


class _FunctionVisualizer(Visualizer):
    fn: Callable[[Frame, int, int], tuple[np.ndarray, np.ndarray]]

    def render(self, f, w, h):
        return type(self).fn(f, w, h)


@dataclass(frozen=True)
class Entry:
    name: str
    blurb: str
    palette: str
    make: Callable[[], Visualizer]
    origin: str          # "builtin" or a plugin's path


REGISTRY: dict[str, Entry] = {}
_origin = "builtin"


def visualizer(name: str, *, blurb: str = "", palette: str = "theme"):
    """Register a function or a `Visualizer` subclass under `name`. A later
    registration of the same name replaces it (a plugin may override a
    built-in)."""
    def register(obj):
        if isinstance(obj, type) and issubclass(obj, Visualizer):
            cls = obj
        elif callable(obj):
            cls = type(f"_{name}", (_FunctionVisualizer,), {"fn": staticmethod(obj)})
        else:
            raise TypeError(f"@visualizer({name!r}) needs a function or a Visualizer subclass")
        cls.name, cls.blurb, cls.palette = name, blurb, palette
        REGISTRY[name] = Entry(name, blurb, palette, cls, _origin)
        return obj
    return register


def load_builtins() -> None:
    from . import modes
    for m in sorted(pkgutil.iter_modules(modes.__path__), key=lambda m: m.name):
        importlib.import_module(f"{modes.__name__}.{m.name}")


def load_plugins(directory: Path = PLUGIN_DIR) -> list[str]:
    """Import every `*.py` in `directory`. Returns warnings, one per file
    that failed; those register nothing."""
    global _origin
    warnings = []
    if not directory.is_dir():
        return warnings
    for path in sorted(directory.glob("*.py")):
        _origin = str(path)
        before = dict(REGISTRY)
        try:
            spec = importlib.util.spec_from_file_location(f"twiddle_viz_plugin_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            REGISTRY.clear()
            REGISTRY.update(before)
            warnings.append(f"visualizer plugin {path.name} skipped: {type(exc).__name__}: {exc}")
        finally:
            _origin = "builtin"
    return warnings


_loaded: list[str] | None = None


def load(plugins: Path | None = PLUGIN_DIR) -> list[str]:
    """Built-ins, then plugins; once. Returns the plugin warnings."""
    global _loaded
    if _loaded is None:
        load_builtins()
        _loaded = load_plugins(plugins) if plugins is not None else []
    return _loaded


def names() -> list[str]:
    return list(REGISTRY)


def create(name: str) -> Visualizer:
    return REGISTRY[name].make()


# ---- running one, defensively ----------------------------------------------


class Runner:
    """Holds one visualizer instance: calls `resize` when the size changes,
    and checks what comes back, so a plugin's bug is a message on screen
    rather than a crash of the whole app."""

    def __init__(self, name: str):
        self.name = name
        self.viz = create(name)
        self.size: tuple[int, int] | None = None
        self.error: str | None = None

    def draw(self, f: Frame, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
        try:
            if self.size != (w, h):
                self.viz.resize(w, h)
                self.size = (w, h)
            codes, heat = self.viz.render(f, w, h)
            codes, heat = check(codes, heat, w, h)
            self.error = None
            return codes, heat
        except Exception as exc:
            self.error = f"{self.name}: {type(exc).__name__}: {exc}"
            return (np.full((h, w), 32, np.int32), np.zeros((h, w), np.float32))


def check(codes, heat, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """What the contract promises, enforced: shapes, integer codes, finite
    heat in 0..1. Raises ValueError on a wrong shape; clamps the rest."""
    codes, heat = np.asarray(codes), np.asarray(heat)
    if codes.shape != (h, w) or heat.shape != (h, w):
        raise ValueError(f"returned {codes.shape} codes / {heat.shape} heat for a {h}x{w} screen")
    if not np.issubdtype(codes.dtype, np.integer):
        raise ValueError(f"codes must be integers, not {codes.dtype}")
    bad = ((codes < 32) | ((codes >= 0x7F) & (codes < 0xA0))
           | ((codes >= 0xD800) & (codes < 0xE000)) | (codes > 0x10FFFF))
    codes = np.where(bad, 32, codes).astype(np.int32)
    heat = np.clip(np.nan_to_num(heat.astype(np.float32), nan=0.0, posinf=1.0, neginf=0.0), 0, 1)
    return codes, heat
