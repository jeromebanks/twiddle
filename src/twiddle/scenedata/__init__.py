"""The producer side of `twiddle scene`: collect shows, enrich bands, publish a dataset.

Everything bespoke lives here -- the per-venue scrapers (`sources/`), the
band-identity enrichers and the build itself (`builder.py`). The `scene`
client only ever reads the dataset this package publishes.
"""
