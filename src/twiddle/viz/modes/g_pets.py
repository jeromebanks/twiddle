"""A troupe of little cartoon animals dancing together: a kitty with a
flower, a bunny with a bow, a floppy-eared puppy, a frog, a bear and a chick.
All originals, drawn in single-width characters.

What moves them:

- each beat is one step of the routine (a new pose) and a hop; the hop is
  higher when the song is loud
- every 16 beats the troupe changes formation: all together, a wave
  rippling down the line, partners (pairs lean in, hold hands, hearts
  float up), a parade (hands linked, walking off one edge and back on
  the other)
- the dance floor lights up with the spectrum, notes float up on beats,
  and sparkles in the air follow the treble

In silence they stop and fall asleep where they stand (eyes shut, a `z`):
a still picture, because nothing is playing.
"""
from __future__ import annotations

import math

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import SPACE, codes_of, empty, resample

SW, SH = 9, 4          # a sprite: 9 wide; 2 rows of head, arms, feet
SLOT = SW + 2          # sprite plus the gap to its neighbour
HOP_S = 0.4            # how long a hop lasts
BEATS_PER_FORMATION = 16

# name, head (two rows, E = an eye), heat of its body. Accents (bows,
# blush, beak, hearts) are always the hottest colour.
SPECIES = (
    ("kitty", (" /\\___/\\✿", "(˶E ω E˶)"), 0.86),
    ("bunny", ("  (\\ /)ʚɞ", "( ˶E×E˶ )"), 0.36),
    ("puppy", (" ╭─────╮ ", "U(˶EᴥE˶)U"), 0.68),
    ("frog", (" (E)_(E) ", "(˶ ‿‿‿ ˶)"), 0.52),
    ("bear", (" ∩_____∩ ", "ʕ ˶EᴥE˶ ʔ"), 0.22),
    ("chick", ("   \\|/   ", "( ˶E▾E˶ )"), 0.72),
)
ACCENT = set("✿ʚɞ˶×▾♥")

ARMS = {
    "idle": " /(   )\\ ",
    "up": "\\ (   ) /",
    "left": " ┌(   )┘ ",
    "right": " └(   )┐ ",
    "hands": "──(   )──",
    "shake": " ~(   )~ ",
}
LEGS = {
    "stand": "  (\")(\") ",
    "apart": " (\")  (\")",
}

# One step per beat: (arms, legs, seen from behind).
TOGETHER = (("up", "stand", False), ("left", "apart", False), ("right", "apart", False),
            ("up", "stand", False), ("shake", "stand", False), ("idle", "apart", False),
            ("up", "stand", True), ("up", "apart", False))
PAIRS = (("hands", "stand", False), ("hands", "apart", False), ("up", "stand", False),
         ("hands", "apart", False), ("shake", "stand", False), ("hands", "stand", False),
         ("up", "apart", True), ("hands", "apart", False))
PARADE = (("hands", "stand", False), ("hands", "apart", False))
FORMATIONS = ("together", "wave", "pairs", "parade")

MIRROR = {"left": "right", "right": "left"}
NOTES = codes_of("♪♫♥")
SPARKLES = codes_of("·*+˚")


class _Sprites:
    """Sprites built on first use: (codes, accent mask, opaque mask). The
    inside of an outline is opaque, so notes pass behind, not through."""

    def __init__(self):
        self.cache: dict[tuple, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

    def get(self, species: int, eyes: str, arms: str, legs: str, back: bool):
        key = (species, eyes, arms, legs, back)
        hit = self.cache.get(key)
        if hit is None:
            head = list(SPECIES[species][1])
            if back:
                face = head[1]
                head[1] = face[0] + " " * (len(face) - 2) + face[-1]
                head[0] = head[0].replace("E", " ")
            rows = [r.replace("E", eyes) for r in head] + [ARMS[arms], LEGS[legs]]
            codes = np.array([[ord(c) for c in r] for r in rows], dtype=np.int32)
            accent = np.array([[c in ACCENT for c in r] for r in rows])
            ink = codes != SPACE
            cols = np.arange(SW)
            first = np.where(ink.any(1), ink.argmax(1), SW)
            last = SW - 1 - ink[:, ::-1].argmax(1)
            inside = (cols >= first[:, None]) & (cols <= last[:, None])
            hit = self.cache[key] = (codes, accent, inside)
        return hit


_SPRITES = _Sprites()


def _blit(codes, heat, sprite, x: int, y: int, level: float) -> None:
    """Draw a sprite with its top-left at (x, y), clipped; around its
    outline, what's behind shows through."""
    sc, accent, solid = sprite
    h, w = codes.shape
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + SW, w), min(y + SH, h)
    if x0 >= x1 or y0 >= y1:
        return
    sub = (slice(y0 - y, y1 - y), slice(x0 - x, x1 - x))
    m = solid[sub]
    codes[y0:y1, x0:x1][m] = sc[sub][m]
    heat[y0:y1, x0:x1][m] = np.where(accent[sub], 1.0, level)[m]


def _put(codes, heat, x: int, y: int, code: int, level: float, over: bool = True) -> None:
    h, w = codes.shape
    if 0 <= x < w and 0 <= y < h and (over or codes[y, x] == SPACE):
        codes[y, x] = code
        heat[y, x] = level


@visualizer("pets", blurb="cartoon animals dancing together: steps on the beat, hops with the kick",
            palette="candy")
class Pets(Visualizer):
    def resize(self, w: int, h: int) -> None:
        self.rng = np.random.default_rng(7)
        self.n_rows = int(np.clip((h - 4) // 10, 1, 4))
        gap = int(np.clip((h - 4) // self.n_rows, 8, 14))
        self.floors = [h - 1 - r * gap for r in range(self.n_rows)]     # front row first
        self.per_row = int(np.clip((w + 2) // SLOT, 1, 16))
        self.x0 = (w - (self.per_row * SLOT - 2)) // 2
        # The parade's lap: long enough that nobody is seen twice.
        self.span = max(self.per_row * SLOT, w + 2 * SLOT)
        self.step = 0
        self.beats = 0
        self.beat_t = -10.0
        self.shift = 0.0            # the parade's walk, in columns
        self.notes: list[list] = []  # [x, y, vy, age, life, code, heat]

    # ---- the dance ---------------------------------------------------------

    def _formation(self) -> str:
        return FORMATIONS[(self.beats // BEATS_PER_FORMATION) % len(FORMATIONS)]

    def _pose(self, formation: str, slot: int):
        step = self.step
        if formation == "wave":
            return TOGETHER[(step - slot) % len(TOGETHER)]
        if formation == "parade":
            return PARADE[step % len(PARADE)]
        if formation == "pairs" and not (slot % 2 == 0 and slot == self.per_row - 1):
            return PAIRS[step % len(PAIRS)]
        return TOGETHER[step % len(TOGETHER)]

    def _advance(self, f: Frame) -> None:
        # A beat is a step. Music with no clear kick still dances, slower.
        if f.beat or (f.energy > 0.25 and f.t - self.beat_t > 0.9):
            self.step += 1
            self.beats += 1
            self.beat_t = f.t
            self._spawn_notes()
        formation = self._formation()
        speed = 3.0 + 9.0 * f.energy
        if formation == "parade":
            self.shift = (self.shift + speed * f.dt) % self.span
        elif self.shift:
            # Finish the lap, back to their own places, then stop.
            self.shift += 2 * speed * f.dt
            if self.shift >= self.span:
                self.shift = 0.0

    def _spawn_notes(self) -> None:
        if len(self.notes) > 80:
            return
        pairs = self._formation() == "pairs"
        for r, floor in enumerate(self.floors):
            for _ in range(1 + (self.per_row > 4)):
                slot = int(self.rng.integers(self.per_row))
                x = self.x0 + slot * SLOT + int(self.rng.integers(SW))
                code = int(NOTES[int(self.rng.integers(2))])
                heat = 0.6
                if pairs and slot % 2 == 0 and slot + 1 < self.per_row:
                    x, code, heat = self.x0 + slot * SLOT + SLOT - 1, int(NOTES[2]), 1.0
                self.notes.append([float(x), float(floor - SH - 2), -5.0 - 3.0 * self.rng.random(),
                                   0.0, 1.2 + self.rng.random(), code, heat])

    # ---- drawing -----------------------------------------------------------

    def render(self, f: Frame, w: int, h: int):
        codes, heat = empty(h, w)
        asleep = f.silent
        if asleep:
            self.notes.clear()
        else:
            self._advance(f)
        formation = self._formation()
        tiles = resample(f.bands, w // 4 + 3)
        if not asleep:
            self._draw_notes(codes, heat, f)          # behind the dancers
            self._draw_sparkles(codes, heat, f)

        for r in range(self.n_rows - 1, -1, -1):          # back rows first
            floor = self.floors[r]
            depth = 1.0 - 0.12 * r
            # The dance floor: tiles lit by the spectrum.
            if 0 <= floor < h:
                xs = np.arange(w)
                lit = np.where(asleep, 0.08, 0.15 + 0.85 * tiles[(xs + 2 * r) // 4])
                codes[floor] = np.where((xs + 2 * r) % 4 == 3, SPACE, ord("▀"))
                heat[floor] = lit * depth
            self._draw_row(codes, heat, f, r, floor, formation, depth, asleep)
        return codes, heat

    def _draw_row(self, codes, heat, f, r, floor, formation, depth, asleep):
        n, w = self.per_row, codes.shape[1]
        offset = (SLOT // 2) * (r % 2)                   # back rows stand in the gaps
        age_base = f.t - self.beat_t
        hop_h = 1 + (f.energy > 0.5)
        drawn = []
        for slot in range(n):
            pos = (slot * SLOT + self.shift) % self.span
            species = (slot + 2 * r) % len(SPECIES)
            if asleep:
                arms, legs, back, dy, eyes = "idle", "stand", False, 0, "-"
            else:
                arms, legs, back = self._pose(formation, slot)
                delay = 0.3 * slot / max(n - 1, 1) if formation == "wave" else 0.0
                age = age_base - delay
                dy = int(round(hop_h * math.sin(math.pi * age / HOP_S))) if 0 <= age < HOP_S else 0
                eyes = "^" if 0 <= age < HOP_S else "•"
                if (f.frame + 37 * slot + 11 * r) % 90 < 3:
                    eyes = "-"                          # a blink
                lean = 0
                if formation == "pairs" and slot + (slot % 2 == 0) < n:
                    left = slot % 2 == 0
                    arms = MIRROR.get(arms, arms) if left else arms
                    if arms == "hands" and self.step % 2:
                        lean = 1 if left else -1        # close in: hands meet
                    pos += lean
            x = self.x0 + offset + int(round(pos))
            y = floor - SH - dy
            level = SPECIES[species][2]     # a hue, not a brightness: the same in every row
            sprite = _SPRITES.get(species, eyes, arms, legs, back)
            if not self.shift and (x < 0 or x + SW > w) and n > 1:
                continue                                # standing half off screen
            for xx in (x, x - self.span):              # the parade wraps around
                _blit(codes, heat, sprite, xx, y, level)
            if asleep:
                _put(codes, heat, x + SW - 1, y - 1, ord("z"), 0.5 * depth)
            drawn.append((x, y, arms))

        # Linked hands: fill the gap between neighbours holding hands.
        if formation == "parade" and not asleep:
            drawn.sort()
            for (xa, ya, aa), (xb, yb, ab) in zip(drawn, drawn[1:]):
                if aa == ab == "hands" and ya == yb and xb - xa == SLOT:
                    for gx in (xa + SW, xa + SW + 1):
                        _put(codes, heat, gx, ya + 2, ord("─"), 0.6 * depth)

    def _draw_notes(self, codes, heat, f):
        keep = []
        for n in self.notes:
            n[3] += f.dt
            n[1] += n[2] * f.dt
            if n[3] < n[4] and n[1] >= 0:
                keep.append(n)
                x = int(round(n[0] + 0.8 * math.sin(n[3] * 5)))
                fade = 1.0 - 0.5 * n[3] / n[4]
                _put(codes, heat, x, int(n[1]), n[5], n[6] * fade, over=False)
        self.notes = keep

    def _draw_sparkles(self, codes, heat, f):
        top = self.floors[-1] - SH - 3                   # the air above the troupe
        if top < 2:
            return
        count = int(f.treble * f.energy * top * codes.shape[1] / 60)
        if count <= 0:
            return
        ys = self.rng.integers(0, top, count)
        xs = self.rng.integers(0, codes.shape[1], count)
        cs = SPARKLES[self.rng.integers(0, len(SPARKLES), count)]
        empty_ = codes[ys, xs] == SPACE
        codes[ys[empty_], xs[empty_]] = cs[empty_]
        heat[ys[empty_], xs[empty_]] = 0.4 + 0.6 * self.rng.random(int(empty_.sum()))
