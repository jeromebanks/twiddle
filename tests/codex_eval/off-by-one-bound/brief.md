# Journal tail: the newest entries

## Outcome

Code that shows the end of the intervention journal can ask for just the newest entries.

## Scope

`src/twiddle/journal_log.py`: add `newest(entries, limit)`, which takes the entries `read_entries` returns.

## Acceptance criteria

- [ ] `newest(entries, limit)` returns exactly the newest `limit` entries, oldest first.
- [ ] A journal with fewer than `limit` entries is returned whole.
- [ ] A `limit` of zero or less returns no entries.
- [ ] Tests in `tests/test_journal_newest.py` cover these.

## Validation

`uv run pytest tests/test_journal_newest.py -q`

## Non-goals

Reading from the file, paging, and any command that calls it.
