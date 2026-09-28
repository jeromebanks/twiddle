"""`twiddle scene` -- who is playing locally, what they sound like.

Three layers, each one open to new implementations without touching the
others, because this is meant to grow into a broader discovery tool:

  sources/   where shows come from      (EventSource: The List today)
  bands.py   what we learn about a band (Enricher: MusicBrainz et al, Spotify)
  players.py where the audio goes       (Player: Spotify Connect today)

`model.py` holds the domain types all three share, and imports nothing from
the UI, so a future non-TUI consumer (recommendations, a calendar export)
can use them as they are.
"""
