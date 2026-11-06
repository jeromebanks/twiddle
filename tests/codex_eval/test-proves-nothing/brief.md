# Step the volume

## Outcome

Volume keys can move a level up or down without ever asking a speaker for an impossible volume.

## Scope

`src/twiddle/volume_step.py`: add `step_volume(current, delta)`.

## Acceptance criteria

- [ ] `step_volume(current, delta)` returns `current + delta`.
- [ ] The result is never below 0 or above 100, whatever `current` and `delta` are, and a test proves it.

## Validation

`uv run pytest tests/test_volume_step.py -q`

## Non-goals

Sending the level to a speaker, and key handling.
