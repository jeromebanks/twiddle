"""Where shows come from. Add a module, register it in `base._registry`."""
from .base import EventSource, SourceError, fetch_all, sources

__all__ = ["EventSource", "SourceError", "fetch_all", "sources"]
