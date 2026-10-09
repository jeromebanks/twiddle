# Probe a stream's codec

## Outcome

The tools can say which audio codec a station's stream carries.

## Scope

`src/twiddle/probe_codec.py`: add `stream_codec(url)`, which asks `ffprobe`. `ProbeError` already exists in `src/twiddle/errors.py`.

## Acceptance criteria

- [ ] `stream_codec(url)` returns the codec name of the stream's first audio stream.
- [ ] When `ffprobe` exits with an error, or the stream has no audio, it raises `ProbeError` with the reason. It never raises anything else and never returns a guess.
- [ ] A test in `tests/test_probe_codec.py` covers the success case without running `ffprobe`.

## Validation

`uv run pytest tests/test_probe_codec.py -q`

## Non-goals

Caching, bitrate and sample rate, and any command that calls it.
