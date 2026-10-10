# Format a duration

## Outcome

Anything that shows how long a track is can turn a number of seconds into text.

## Scope

`src/twiddle/clock_text.py`: add `format_duration(seconds)`, using the existing `pad2`.

## Acceptance criteria

- [ ] Under an hour it reads `M:SS` (`65` is `1:05`); from an hour up it reads `H:MM:SS` (`3723` is `1:02:03`).
- [ ] A negative number of seconds raises `ValueError`.
- [ ] Tests in `tests/test_clock_text.py` cover both forms, the boundaries at 59:59 and 1:00:00, and the error.

## Validation

`uv run pytest tests/test_clock_text.py -q`

## Non-goals

Fractions of a second, other languages, and anything that calls it.
