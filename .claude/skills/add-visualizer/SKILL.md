---
name: add-visualizer
description: Add a music visualizer to `twiddle`'s full-window visualizer (`v` in dial and scene, `twiddle viz demo`) -- or fix, tune or port one. Use when asked for a new visualization, "eye candy", an effect (plasma, rain, tunnel, spiral, bars, VU meter...), a colour palette, or to port a mode from spektr / cava / asciimatics.
---

<!-- No dollar-digit sequences in this file: the skill loader substitutes them with arguments. -->

# Adding a visualizer

A visualizer is **one file** in `src/twiddle/viz/modes/`, registered with
`@visualizer`. Nothing else needs editing: the `v` screen cycles through
every registered one (in file-name order, hence the `a_`, `b_` ... prefixes;
name yours `m_...` or later to put it at the end), and
`tests/test_viz_contract.py` tests it automatically.

A personal one can instead go in `~/.config/twiddle/viz/<name>.py`: same
code, loaded at startup, and skipped with an on-screen warning if it fails
to import. Built-ins are for ones worth keeping.

## The contract (`viz/base.py`)

```python
from twiddle.viz.base import Visualizer, visualizer
from twiddle.viz.analysis import Frame

@visualizer("name", blurb="one line: what it shows", palette="theme")
def name(f: Frame, w: int, h: int):
    return codes, heat                      # both numpy arrays of shape (h, w)

@visualizer("name", blurb="...", palette="ice")
class Name(Visualizer):
    def resize(self, w, h): ...             # before the first frame, and on every resize
    def render(self, f: Frame, w, h): ...   # -> (codes, heat)
```

- **`codes`**: int codepoints, one per cell. Only **single-width**
  characters: braille, block elements, box drawing, ASCII, `·`. No emoji or
  CJK, because they take two cells and break the row. Use space (32) for
  empty, never U+2800.
- **`heat`**: float 0 (cool) .. 1 (hot). **Never pick a colour.** The palette
  turns heat into colour, so a visualizer works in every palette and theme.
  That's spektr's rule; its modes port across easily because of it.
- `palette`: the one the screen starts on for this visualizer (`theme`,
  `fire`, `ice`, `neon`, `aurora`, `mono`, `candy`, `clay`, `psyche`, `synth`; `c` still cycles). A new
  palette is a line in `canvas.PALETTES` (colour stops, cool to hot).
- Use a function when it has no memory, a class when it has any (trails,
  particles, scrolling history). Keep state in numpy arrays sized in `resize`.
- Randomness: `np.random.default_rng(seed)` made in `resize`, so the
  contract test is deterministic.

## What a Frame gives you (`viz/analysis.py`)

| field | what |
|---|---|
| `bands`, `peaks` | 64 log-spaced bands, 45 Hz to 10 kHz, 0..1, with cava-style gravity; peaks are held caps |
| `raw_bands` | the same bands without smoothing (spectrograms want these) |
| `wave`, `left`, `right` | the last 2048 samples at 22050 Hz, about -1..1 |
| `rms`, `energy` | raw loudness; smoothed loudness relative to the song (0..1) |
| `bass`, `treble` | band averages: lowest quarter, top half |
| `onset`, `beat` | how sharply the lows just rose (0..1); a beat (bool) |
| `silent` | the stream is silent. **Draw silence as silence** (a flat line, nothing): a paused relay is zeros, and a picture that moves anyway lies about it |
| `t`, `dt`, `frame` | time since the screen opened, since the last frame, frame count |

Automatic sensitivity already scales the bands to the music. Don't
normalise again. Scale by `energy` or `onset` for "reacts to the music".

## Helpers (`viz/canvas.py`)

| helper | for |
|---|---|
| `empty(h, w)` | blank codes + heat to draw into |
| `dots_shape(h, w)`, `pack_braille(dots)`, `cell_max(field)` | 2x4 dots per cell: draw into a bool grid of `dots_shape`, pack it; `cell_max` of a float field of the same shape gives the heat |
| `polyline(dots, xs, ys)`, `points(dots, xs, ys)` | lines and dots in the braille grid (coordinates in dots, clipped) |
| `blocks_up(levels, h)` | bars from the bottom, 8 steps per cell; returns codes and a bottom-to-top fill to use as heat |
| `shade(field, ramp)` | a 0..1 field to density characters (`SHADES`, `SOLID`, or your own string) |
| `resample(values, n)` | bands to however many columns you have |
| `codes_of(str)` | a lookup table of characters |

The existing modes are the idioms: `a_spectrum` (blocks + caps),
`b_scope` (braille line + phosphor decay), `c_lissajous` (braille points),
`d_waterfall` (scrolling history at a fixed rate), `e_fire` (a cellular
automaton), `f_starfield` (particles), `g_pets` (text sprites: characters
blitted with clipping, poses stepped on beats), `h_buddy` (filled shapes
rasterised into half blocks: two square pixels a cell, features cut out as
holes), `i_fractal` (a per-cell escape-time field, only live points
iterated; colour cycling as a triangle wave of heat), `j_lasers` (long
braille lines: plot with `points` along a linspace, since `polyline` caps
its steps), `k_synthwave` (cells and braille mixed in one frame),
`l_tunnel` (many shapes' dots written in one `np.maximum.at`). Start from
the nearest one.

## Write it, look at it, test it

```bash
uv run twiddle viz snapshot NAME --size 80x24                 # one frame as text + ms/frame
uv run twiddle viz snapshot NAME --signal silence             # must be still / empty
uv run twiddle viz snapshot NAME --size 1x1                   # must not crash
uv run twiddle viz snapshot NAME --color --palette fire       # in colour
uv run twiddle viz demo NAME                                  # full screen, made-up music (ask the user to look)
uv run twiddle viz demo NAME --station kexp                   # a real station, tapped here only: plays nothing
uv run pytest tests/test_viz_contract.py -q
```

Signals: `music` (kick, chords, hats; the one to tune against), `sine`,
`noise`, `inverted` (stereo out of phase), `silence`.

- **Performance**: under about 5 ms/frame at 250x70 (`snapshot` prints
  it). Vectorise with numpy. A Python loop over cells is 17,500 iterations a
  frame at that size, too slow. A loop over a few hundred particles or
  columns is fine.
- The contract test runs every visualizer through five sizes (1x1 to
  250x70) and five signals for a dozen frames each. It checks exact shapes,
  printable single-width codes, finite heat in 0..1, that it moves to
  music, and a blurb. If it fails, fix the visualizer, not the test.
- `viz demo` and `v` need a person to look. A snapshot is text only, so ask
  the user to try it rather than claiming it looks good.

## Porting

- **spektr** (MIT; `@mode` returns `(codes, cidx)` from a `ctx`): `ctx.bands`
  → `f.bands`, `ctx.wave` → `f.wave`, `ctx.stereo` → `f.left`/`f.right`,
  `ctx.dot_rows/dot_cols` → `dots_shape(h, w)`, `ctx.energy` →
  `f.energy`, `ctx.pulse`/`ctx.drive` → `f.onset`. `cidx` is a palette index:
  divide by its ramp length for `heat`. Keep its licence notice in the
  file's docstring.
- **cava**: its maths (bands, gravity, sensitivity) is already in
  `analysis.py`. Port output styles, not the engine.
- **asciimatics** effects: the per-frame update rule ports; its Screen
  doesn't. Vectorise it.

## Don'ts

- Don't read the network, files or the clock. Everything comes in the
  Frame. `f.t`/`f.dt` are the clock.
- Don't make a visualizer look alive without signal. The screen already
  refuses to animate when there's nothing to tap. A visualizer given a
  silent frame must not invent motion either, because this repo's
  recurring trap is taking "something moves" for "sound exists".
