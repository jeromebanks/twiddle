"""Twiddle, the project's mascot, dancing (who he is: CHARACTERS.md; he
also waves on dial's splash, `viz/splash.py`). One big cartoon critter: a rounded, filled-in body with stubby
arms and legs and an antenna with a bobble on top. It's drawn in half
blocks, two square pixels per cell, so a bigger window gets a sharper
critter.

The body is filled in and the face is cut out of it (eyes, brows, mouth
are holes that show the background), with shine in the eyes, blush on
the cheeks and a tongue in an open mouth. What moves it:

- each beat is a step: a new arm move (arms up, point, wave, flap),
  a hop sized by the loudness, a lean to alternate sides, one foot
  lifted; squash on landing, stretch in the air
- the mouth sings along with the vocal range (300 Hz to 3 kHz)
- a big kick is a grin: eyes shut into ^ ^, mouth wide, brows up;
  now and then a wink
- the antenna's bobble lags behind on a spring

In silence it stops, closes its eyes and sleeps (a `z`): a still picture,
because nothing is playing.
"""
from __future__ import annotations

import math

import numpy as np

from ..analysis import Frame
from ..base import Visualizer, visualizer
from ..canvas import SPACE, codes_of, empty

# Heats, which the palette makes colours ("clay": clay orange, peach, pink, cream).
SHADOW, RIM, BODY, BELLY, PINK, SHINE = 0.17, 0.42, 0.5, 0.64, 0.83, 1.0

HOP_S = 0.38          # time in the air
LAND_S = 0.17         # squash after landing
EXTENT = 4.4          # model units across the frame, both ways
GROUND = 1.52         # where the feet stand, model units below the body's centre

# Arm moves, one per beat: (left, right) angles, 0 = hanging down, pi = straight up.
# "wave" / "flap" add a wiggle while the music plays.
MOVES = (("up", 2.6, 2.6), ("out", 1.45, 1.45), ("point", 0.4, 2.45), ("point", 2.45, 0.4),
         ("wave", 0.5, 2.35), ("wave", 2.35, 0.5), ("flap", 1.1, 1.1), ("up", 2.8, 2.8))
NOTES = codes_of("♪♫")
VOCALS = slice(22, 50)     # bands covering about 300 Hz .. 3 kHz


def _segment(X, Y, ax, ay, bx, by):
    """Distance from every point to the segment a-b."""
    dx, dy = bx - ax, by - ay
    t = np.clip(((X - ax) * dx + (Y - ay) * dy) / (dx * dx + dy * dy + 1e-9), 0, 1)
    return np.hypot(X - ax - dx * t, Y - ay - dy * t)


def _ellipse(X, Y, cx, cy, rx, ry):
    return ((X - cx) / rx) ** 2 + ((Y - cy) / ry) ** 2 <= 1


@visualizer("buddy", blurb="Twiddle, the mascot, dancing, with a face that sings, grins and winks",
            palette="clay")
class Buddy(Visualizer):
    def resize(self, w: int, h: int) -> None:
        self.rng = np.random.default_rng(5)
        R, C = 2 * h, w
        self.unit = max(min(C, R) / EXTENT, 0.5)            # pixels per model unit
        self.px = 1.0 / self.unit                            # one pixel, in model units
        self.cx = C / 2
        self.cy = R - 1.5 - GROUND * self.unit - 0.2 * self.unit
        # Only the columns the critter can reach are computed.
        c0 = max(0, int(self.cx - 2.3 * self.unit))
        c1 = min(C, int(math.ceil(self.cx + 2.3 * self.unit)) + 1)
        self.cols = slice(c0, c1)
        xs = (np.arange(c0, c1) + 0.5 - self.cx) / self.unit
        ys = (np.arange(R) + 0.5 - self.cy) / self.unit
        self.X, self.Y = np.meshgrid(xs.astype(np.float32), ys.astype(np.float32))
        self.step = 0
        self.beat_t = -10.0
        self.arms = np.array([0.25, 0.25])
        self.lean = 0.0
        self.mouth = 0.0
        self.grin_until = -1.0
        self.wink_until = -1.0
        self.blink_at = 2.5
        self.bob = np.zeros(2)          # antenna bobble: offset from rest, and velocity
        self.bob_v = np.zeros(2)
        self.base_prev: tuple[float, float] | None = None
        self.notes: list[list] = []     # [x, y, vy, age, life, code]

    # ---- motion --------------------------------------------------------------

    def _advance(self, f: Frame) -> None:
        if f.beat or (f.energy > 0.25 and f.t - self.beat_t > 0.9):
            self.step += 1
            self.beat_t = f.t
            if f.onset > 0.6 and self.step % 2 == 0:
                self.grin_until = f.t + 0.35
            elif self.step % 8 == 6:
                self.wink_until = f.t + 0.45
            if len(self.notes) < 30:
                side = 1 if self.step % 2 else -1
                self.notes.append([self.cx + side * (1.2 + 0.4 * self.rng.random()) * self.unit,
                                   (self.cy - 0.6 * self.unit) / 2, -3.0 - 2 * self.rng.random(),
                                   0.0, 1.4 + self.rng.random(), int(NOTES[self.step % 2])])
        k = min(1.0, f.dt * 14)
        name, left, right = MOVES[self.step % len(MOVES)]
        wiggle = math.sin(f.t * 12)
        if name == "wave":
            left, right = (left + 0.35 * wiggle, right) if left > right else (left, right + 0.35 * wiggle)
        elif name == "flap":
            left, right = left + 0.5 * wiggle, right + 0.5 * wiggle
        elif name == "up":
            left, right = left + 0.12 * wiggle, right - 0.12 * wiggle
        self.arms += (np.array([left, right]) - self.arms) * k
        side = 1 if self.step % 2 else -1
        self.lean += (side * (0.06 + 0.1 * f.energy) - self.lean) * k
        vocal = float(np.mean(f.bands[VOCALS])) if len(f.bands) >= VOCALS.stop else f.energy
        self.mouth += (np.clip((vocal - 0.12) * 1.8, 0, 1) - self.mouth) * min(1.0, f.dt * 18)
        if f.t > self.blink_at + 0.14:
            self.blink_at = f.t + 2.0 + 3.0 * self.rng.random()

    def _rest(self) -> None:
        """Asleep: everything straight to its resting place, so the picture is still."""
        self.arms[:] = 0.25
        self.lean = 0.0
        self.mouth = 0.0
        self.bob[:] = 0.0
        self.bob_v[:] = 0.0
        self.base_prev = None
        self.notes.clear()

    def _spring(self, base: tuple[float, float], dt: float) -> None:
        """The bobble is left behind when the body moves, then springs back."""
        if self.base_prev is not None and dt > 0:
            self.bob[0] -= base[0] - self.base_prev[0]
            self.bob[1] -= 0.5 * (base[1] - self.base_prev[1])
        self.base_prev = base
        dt = min(dt, 0.05)
        self.bob_v += (-140 * self.bob - 9 * self.bob_v) * dt
        self.bob += self.bob_v * dt
        n = float(np.hypot(*self.bob))
        if n > 0.35:
            self.bob *= 0.35 / n

    # ---- drawing -------------------------------------------------------------

    def render(self, f: Frame, w: int, h: int):
        asleep = f.silent
        if asleep:
            self._rest()
        else:
            self._advance(f)
        X, Y, px = self.X, self.Y, self.px
        P = np.full(X.shape, -1.0, dtype=np.float32)       # heat per pixel; -1 is nothing

        # Hop, squash and stretch, anchored at the hips so the feet stay put.
        age = f.t - self.beat_t
        hop, sy = 0.0, 1.0
        if not asleep and 0 <= age < HOP_S:
            s = math.sin(math.pi * age / HOP_S)
            hop, sy = (0.12 + 0.3 * f.energy) * s, 1 + 0.08 * s
        elif not asleep and HOP_S <= age < HOP_S + LAND_S:
            sy = 1 - 0.12 * math.sin(math.pi * (age - HOP_S) / LAND_S)
        elif asleep:
            sy = 0.97
        sx = 1 / math.sqrt(sy)
        by = 0.8 - 0.8 * sy - hop
        lean = self.lean

        def world(u, v):
            return u * sx - lean * sy * v, by + sy * v

        V = (Y - by) / sy
        U = (X + lean * sy * V) / sx

        # Shadow, smaller when it's in the air.
        P[_ellipse(X, Y, lean * 0.3, GROUND + 0.05, 0.95 * (1 - hop), max(0.07, 1.2 * px))] = SHADOW

        # Legs and feet; one foot up on alternate steps.
        spread = 0.08 if MOVES[self.step % len(MOVES)][0] == "up" else 0.0
        for sign in (-1, 1):
            hx, hy = world(0.42 * sign, 0.78)
            lift = 0.0 if asleep else 0.16 * math.sin(math.pi * min(age, HOP_S) / HOP_S) * (
                (self.step % 2 == 0) == (sign > 0))
            fx, fy = hx + spread * sign, hy + 0.45 - lift
            P[_segment(X, Y, hx, hy, fx, fy) <= 0.17] = BODY
            P[_ellipse(X, Y, fx + 0.06 * sign, fy + 0.07, 0.25, 0.14)] = BODY

        # Body: a rounded square, shaded round its lower right, a gloss on
        # the upper left, and a paler belly.
        shape = (np.abs(U) / 1.05) ** 3.2 + (np.abs(V) / 0.85) ** 3.2
        body = shape <= 1
        P[body] = BODY
        P[body & (shape > 0.7) & (U + 1.4 * V > 0.35)] = RIM
        P[_ellipse(U, V, -0.72, -0.52, 0.13, 0.07)] = BELLY
        P[body & _ellipse(U, V, 0, 0.55, 0.55, 0.26)] = BELLY

        # Arms and hands.
        for i, sign in enumerate((-1, 1)):
            ax, ay = world(0.92 * sign, 0.05)
            a = float(self.arms[i])
            ex, ey = ax + 0.72 * math.sin(a) * sign, ay + 0.72 * math.cos(a)
            P[_segment(X, Y, ax, ay, ex, ey) <= 0.14] = BODY
            P[(X - ex) ** 2 + (Y - ey) ** 2 <= 0.2 ** 2] = BODY

        # Antenna with a bobble.
        tx, ty = world(0.0, -0.8)
        if not asleep:
            self._spring((tx, ty), f.dt)
        bx_, by_ = tx + self.bob[0], ty - 0.5 + self.bob[1]
        P[_segment(X, Y, tx, ty, bx_, by_) <= max(0.045, 0.6 * px)] = BODY
        P[(X - bx_) ** 2 + (Y - by_) ** 2 <= 0.15 ** 2] = PINK
        P[(X - bx_ + 0.05) ** 2 + (Y - by_ + 0.05) ** 2 <= max(0.04, 0.5 * px) ** 2] = SHINE

        self._face(f, P, U, V, body, asleep)
        codes, heat = self._cells(P, w, h)
        self._text(f, codes, heat, asleep)
        return codes, heat

    def _face(self, f, P, U, V, body, asleep):
        px = self.px
        line = max(0.05, 1.1 * px)                  # thinnest stroke that still shows
        grin = not asleep and f.t < self.grin_until
        wink = not asleep and not grin and f.t < self.wink_until
        blink = not asleep and self.blink_at <= f.t < self.blink_at + 0.14
        hole = np.zeros(U.shape, dtype=bool)

        def arc(cx, cy, r, upper):
            d = np.hypot(U - cx, V - cy)
            half = V < cy + 0.01 if upper else V > cy - 0.01
            return (np.abs(d - r) <= line / 2) & half

        look = 4 * self.lean                        # eyes follow the lean
        for sign in (-1, 1):
            ex, ey = 0.38 * sign + 0.06 * look, -0.2
            if asleep:
                hole |= arc(ex, ey - 0.06, 0.13, upper=False)           # closed: a smile-shaped lid
            elif grin or (wink and sign < 0):
                hole |= arc(ex, ey + 0.08, 0.14, upper=True)            # ^
            elif blink:
                hole |= (np.abs(V - ey) <= line / 2) & (np.abs(U - ex) <= 0.16)
            else:
                hole |= _ellipse(U, V, ex, ey, 0.18, 0.26)
            if not asleep:
                # Brows: the outer end droops a little; up for a grin, down on the wink.
                y = ey - 0.39 - (0.04 if grin else 0.0)
                droop = -0.04 if wink and sign < 0 else 0.03
                d = _segment(U, V, ex - 0.13 * sign, y, ex + 0.13 * sign, y + droop)
                hole |= d <= max(0.035, 0.6 * px)

        # Mouth: a smile, or open (singing, grinning) with a tongue.
        my = 0.16
        opening = 0.0 if asleep else max(self.mouth, 0.8 if grin else 0.0)
        tongue = np.zeros(U.shape, dtype=bool)
        if opening < 0.15:
            width = 0.1 if asleep else 0.15
            hole |= arc(0.0, my - 0.1, width, upper=False) & (np.abs(U) <= width)
        else:
            mw = 0.15 + (0.08 if grin else 0.03 * opening)
            mh = 0.06 + 0.2 * opening
            mouth = (V >= my - 0.03) & _ellipse(U, V, 0, my - 0.03, mw, mh)
            hole |= mouth
            if mh * self.unit >= 3:
                tongue = mouth & _ellipse(U, V, 0, my - 0.03 + mh, 0.6 * mw, 0.45 * mh)

        blush = body & (_ellipse(U, V, -0.68, 0.08, 0.14, 0.08) | _ellipse(U, V, 0.68, 0.08, 0.14, 0.08))
        P[blush] = PINK
        P[body & hole] = -1.0
        P[tongue] = PINK
        # Shine in open eyes: a big and a small highlight.
        if not asleep and not blink:
            for sign in (-1, 1):
                if grin or (wink and sign < 0):
                    continue
                ex, ey = 0.38 * sign + 0.06 * look, -0.2
                r1, r2 = max(0.07, 0.8 * px), max(0.035, 0.5 * px)
                P[((U - ex + 0.06) ** 2 + (V - ey + 0.1) ** 2 <= r1 ** 2)] = SHINE
                P[((U - ex - 0.06) ** 2 + (V - ey - 0.1) ** 2 <= r2 ** 2)] = SHINE

    def _cells(self, P, w, h):
        """Pixels to half blocks: top pixel, bottom pixel, one colour per cell
        (the hotter one: a feature wins over the body around it)."""
        codes, heat = empty(h, w)
        top, bot = P[0::2], P[1::2]
        t, b = top >= 0, bot >= 0
        sub = np.where(t & b, ord("█"), np.where(t, ord("▀"), np.where(b, ord("▄"), SPACE)))
        codes[:, self.cols] = sub
        heat[:, self.cols] = np.clip(np.maximum(top, bot), 0, 1)
        return codes, heat

    def _text(self, f, codes, heat, asleep):
        h, w = codes.shape

        def put(x, y, code, level):
            x, y = int(round(x)), int(round(y))
            if 0 <= x < w and 0 <= y < h and codes[y, x] == SPACE:
                codes[y, x], heat[y, x] = code, level

        if asleep:
            for dx, dy, ch in ((1.25, -1.15, "z"), (1.55, -1.5, "z"), (1.9, -1.9, "Z")):
                put(self.cx + dx * self.unit, (self.cy + dy * self.unit) / 2, ord(ch), 0.9)
            return
        keep = []
        for n in self.notes:
            n[3] += f.dt
            n[1] += n[2] * f.dt
            if n[3] < n[4] and n[1] >= 0:
                keep.append(n)
                put(n[0] + 0.8 * math.sin(n[3] * 5), n[1], n[5], 0.9 * (1 - 0.4 * n[3] / n[4]))
        self.notes = keep
