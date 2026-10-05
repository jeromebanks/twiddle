"""`twiddle scene` -- the client: browse tonight's local shows and who the bands are.

Three packages, one-way dependencies:

  scenespec/  the dataset's shape: what a producer writes and a client reads
  scenedata/  one producer: sources, enrichers, `scene build` (the bespoke part)
  scene/      this package, the client: it reads a dataset and plays music

A client has only the dataset a producer published, so nothing here imports
`scenedata` (a test enforces it; `cli.py` starts a build lazily). Who a band is
comes from the dataset; the client applies your pin (`book.PinEnricher`) and
fetches song lists (`book.TrackEnricher`), which need your own sign-in.

  book.py     profiles seeded from the dataset; background pin + song-list work
  players.py  where the audio goes (Spotify Connect today)
  app.py      the Textual UI, presentation only; everything is injected
"""
