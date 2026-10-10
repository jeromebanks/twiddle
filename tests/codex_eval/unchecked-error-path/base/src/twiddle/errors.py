"""Errors the tools raise for a failure the caller should report, not crash on."""


class ProbeError(Exception):
    """A stream could not be probed: `ffprobe` failed, or found nothing playable."""
