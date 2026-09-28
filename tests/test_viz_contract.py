"""Every registered visualizer, held to the contract in `viz/base.py`.

A visualizer added through the `add-visualizer` skill is picked up here
without touching this file: sizes from 1x1 to very wide, and signals from
silence to noise, over enough frames for trails and particles to build.
"""
import numpy as np
import pytest
from rich.cells import cell_len

from twiddle.viz import base, signals
from twiddle.viz.analysis import Analyser
from twiddle.viz.canvas import PALETTES, Palette, to_strips

base.load(plugins=None)
SIZES = [(1, 1), (3, 2), (7, 5), (80, 24), (250, 70)]
FRAMES = 12


@pytest.mark.parametrize("signal", sorted(signals.SIGNALS))
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("name", base.names())
def test_visualizer_keeps_the_contract(name, size, signal):
    w, h = size
    viz = base.create(name)
    viz.resize(w, h)
    an = Analyser()
    gen = signals.SIGNALS[signal]()
    for _ in range(FRAMES):
        f = an.frame(gen.window(4096), 1 / 30)
        codes, heat = viz.render(f, w, h)
        codes, heat = np.asarray(codes), np.asarray(heat)
        assert codes.shape == (h, w) and heat.shape == (h, w)
        assert np.issubdtype(codes.dtype, np.integer)
        assert np.isfinite(heat).all() and heat.min() >= 0 and heat.max() <= 1
        for c in set(np.unique(codes).tolist()):
            ch = chr(c)
            assert ch.isprintable(), f"{name} drew unprintable {c:#x}"
            assert cell_len(ch) == 1, f"{name} drew {ch!r}, which isn't one cell wide"
    strips = to_strips(codes, heat, Palette.named(viz.palette))
    assert [s.cell_length for s in strips] == [w] * h


def test_every_visualizer_has_a_name_and_a_blurb():
    for name in base.names():
        e = base.REGISTRY[name]
        assert e.name == name and e.blurb
        assert e.palette in PALETTES


def test_silence_comes_to_rest():
    """A paused relay is a stream of zeros. Whatever a visualizer was doing,
    silence must settle to a still picture: motion without sound is the
    trap this repo keeps meeting ("PLAYING" is not sound)."""
    for name in base.names():
        viz = base.create(name)
        viz.resize(60, 20)
        an = Analyser()
        music, silence = signals.SIGNALS["music"](), signals.SIGNALS["silence"]()
        for _ in range(20):
            viz.render(an.frame(music.window(4096), 1 / 30), 60, 20)
        frames = [viz.render(an.frame(silence.window(4096), 1 / 30), 60, 20)[0].tobytes()
                  for _ in range(150)]
        assert frames[-1] == frames[-2], f"{name} still moves in silence"


def test_a_visualizer_reacts_to_music():
    """Not a still picture: something changes between frames of music."""
    for name in base.names():
        viz = base.create(name)
        viz.resize(60, 20)
        an = Analyser()
        gen = signals.SIGNALS["music"]()
        seen = {viz.render(an.frame(gen.window(4096), 1 / 30), 60, 20)[0].tobytes()
                for _ in range(15)}
        assert len(seen) > 1, f"{name} didn't move to music"
