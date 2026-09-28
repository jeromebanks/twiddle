# The cast

The characters who live in this project. They are here for fun, and so the
tools have a face. Each gets a name, a look, a backstory and rules for how
he or she behaves on screen, so every new place a character turns up stays
in character.

| Character | Home | Where you meet them |
|---|---|---|
| **Twiddle** | `dial`, and the project as a whole | the `dial` splash; the `buddy` visualizer (`v` in dial or scene) |
| *(open)* | `scene` | ideas below |
| *(open)* | the relay, the daemon | ideas below |

---

## Twiddle

> *all the radio. none of the static.*

**What he is.** A small, round, clay-orange critter about the size of a
transistor radio. He has stubby arms and legs, a pale peach belly, pink
cheeks, big shiny eyes, and one antenna with a pink bobble on top. He is a
**staticling**, and as far as anyone knows the only one.

**Where he came from.** Every radio dial has gaps between the stations, and
the gaps are full of hiss. Most people turn past them fast. Twiddle was born
in one of those gaps, at 3am, somewhere between 1610 and 1620 AM on a
Bakelite set that had been left on in an empty kitchen. The hiss piled up
for so long that it turned into something, and that something was hungry.

He eats static. That is his whole job, and he loves it. The crackle
between stations is his breakfast, and a clean signal with no hiss left in
it is what he leaves behind. That is where dial's slogan comes from: *all
the radio, none of the static.* He's the reason.

His antenna is a real receiver. The bobble picks up every station at once,
which is why he knows what's playing everywhere before you do. It is also
why he can't sit still when a song comes on.

**Why he's clay-coloured.** The Bakelite set was a reddish-brown one, and he
came out the same colour. His canonical palette is `clay`
(`viz/canvas.py`): clay orange body, peach belly, pink bobble and blush,
cream shine in the eyes.

**Personality.**

- A terrible singer and a very enthusiastic one. When the vocals come in,
  his mouth goes along with them, a beat late.
- He dances on the beat and hops higher when it's loud. On a big kick he
  grins so hard his eyes shut into `^ ^`. Every so often he winks at you.
- He falls asleep the moment the music stops, right where he's standing,
  with a `z`. He won't pretend there's music when there isn't. See the rule
  below.
- He waves at everyone he meets. That's the splash.
- Favourite station: whatever you're about to tune to. Least favourite
  sound: a Sonos going quiet halfway through a song. He takes it
  personally, and he'd like you to know that the daemon is working on it (see
  `twiddle diag`).

**How he behaves on screen. These are the rules; keep to them.**

- **He reacts to music only when there is music.** In the visualizer he
  dances to a real audio tap. With nothing playing he sleeps, and the
  picture is still. On the splash nothing is playing yet, so he only waves,
  blinks and sways his bobble. He never hops or sings there. This is the
  visualizer screen's rule (nothing may look like a reaction to music
  unless it is one) given a face, and it matters more here than elsewhere:
  this project exists because "it looks like it's playing" turned out to
  be a lie more than once (`docs/GUIDE.md`'s traps).
- He's drawn in half blocks, two square pixels per cell, so he gets
  sharper in a bigger window. Give him a box about twice as wide as it is
  tall. Below about 8 rows he stops reading as a face; leave him out and
  let the words carry the moment.
- One Twiddle at a time. There is only one staticling.

**Where he lives in the code.**

| What | Where |
|---|---|
| His body, face and moves | `src/twiddle/viz/modes/h_buddy.py` (the visualizer's registry key stays `buddy`: saved prefs refer to it) |
| The splash picture: big `dial` letters, Twiddle waving, the slogan | `src/twiddle/viz/splash.py` |
| The splash screen: any key or 20 s closes it, and startup runs behind it | `src/twiddle/dial/splash.py` |

To use him somewhere new, make a `Buddy()`, `resize(w, h)` it, and feed
`render()` a `Frame`. A real one comes from `viz/analysis.py`. For a
hand-made one, set `silent=False` and `energy` below 0.25 for "awake,
calm", or `silent=True` for "asleep". `viz/splash.py` is the worked
example.

---

## Ideas for the rest of the cast

These are not built yet, just so the next character has a starting point.
Each should get the same treatment as Twiddle: a name, a look, a palette, a
backstory, and on-screen rules that stay honest about what is and isn't
happening.

- **scene** (local shows): someone who goes to every gig and stands at the
  back with a flyer. Could be a small bat with a tote bag, since bats are
  out late and know every venue's back door. Palette: `neon` or `candy`.
- **The relay** (Spotify → Roams): a courier who carries the audio across
  the house by hand and gets out of breath when `dropped_chunks` climbs.
- **The daemon** (the overnight watch): an owl with a clipboard who writes
  down every time a speaker blinks. Doesn't dance, and doesn't approve of
  dancing on the job.
- **The pets troupe** in the `pets` visualizer (kitty, bunny, puppy, frog,
  bear, chick) are already Twiddle's backing dancers, whether they know it
  or not.
