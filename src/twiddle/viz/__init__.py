"""`v` in `dial` and `scene`: the whole window becomes a visualization of
the music, in characters.

Neither TUI ever holds decoded audio (a Sonos fetches its own stream; ffplay,
ffmpeg and librespot write straight to CoreAudio; the relay's PCM lives in
another process). So the visualizer runs its own read-only decode of the
same stream -- an *audio tap* (`tap.py`). It shows the stream, not what a
speaker emits: a Roam that drops out keeps dancing here. It is never
evidence that sound exists (docs/GUIDE.md's trap, again).

The pieces, each usable without the others:

- `tap.py`      -- ffmpeg decoding a URL into a ring buffer of stereo float PCM
- `source.py`   -- which URL to tap for what dial / scene is playing, or why none
- `analysis.py` -- PCM window -> `Frame` (bands, wave, stereo, beat, energy)
- `canvas.py`   -- helpers for drawing: braille, eighth blocks, shades, colour
- `base.py`     -- the visualizer contract and registry (`@visualizer`)
- `modes/`      -- the built-in visualizers, one per file
- `screen.py`   -- the full-screen Textual screen both apps push on `v`

numpy is imported here and nowhere outside `viz/`, so nothing else pays for
it. Writing a new visualizer: skill `add-visualizer`.
"""
