"""The shape of a published scene dataset: the contract between producers and clients.

`dataset.py` is the file (envelope, reader, atomic publish), `model.py` a show,
`band.py` and `profiles.py` a band and its record, `genre.py` the genre
families the "sounds like" column speaks. Nothing here scrapes, enriches or
draws: a producer (`twiddle.scenedata`) writes this shape and a client
(`twiddle.scene`) reads it, and neither imports the other. Keep it that way:
this package may use `twiddle.lookup`'s plain types and stable third-party
libraries, never `scene`, `scenedata` or Textual.
"""
