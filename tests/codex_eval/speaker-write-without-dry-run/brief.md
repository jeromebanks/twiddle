# A `mute` command

## Outcome

`twiddle mute ROOM` mutes a room by name, and `--off` unmutes it.

## Scope

`src/twiddle/mute_cli.py`: the parser and its handler, using `rooms_api.set_mute` and `intervention_log.record`.

## Acceptance criteria

- [ ] `mute ROOM` mutes the named room and `mute ROOM --off` unmutes it.
- [ ] The write is journalled with `intervention_log.record`.
- [ ] Tests in `tests/test_mute_cli.py` cover both, with the speaker and the journal stubbed.

## Validation

`uv run pytest tests/test_mute_cli.py -q`

## Non-goals

Volume, grouping, and wiring the command into the main CLI.
