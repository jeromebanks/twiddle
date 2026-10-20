# Quick brief template

Body file for `transition N prd-review --kind prd --lane quick` (a feature or a data-only
addition) or `transition N diagnosis-review --kind diagnosis --lane quick` (a bug). The tool
adds the marker, the revision header and the sign-off footer; don't write those.

This is the *whole* spec of a quick change: there is no plan and no PRD after it. `work-slice
N --quick` builds from it, `quick-gate` and `merge` hold the diff to its `## Files` list, and
Codex reviews the PR against it. Use the headings below exactly: `claim --quick` refuses a
brief that lacks one (it matches a line that is `## ` and the name), and says which.

```markdown
## Outcome
One or two sentences: what is true when this is done, in the poster's terms.

## Scope
- In: what changes.
- Out: what you are deliberately leaving alone.

## Files
- `src/twiddle/stations/catalog/psyched.toml`
- `tests/test_catalog.py`

## Acceptance criteria
1. Numbered, observable, testable. (Not checkboxes: after an escalation to the full lane
   this text is copied into docs/prd/, where the planner reads the numbered items.)
2. ...

## Validation
The commands that prove it, read-only ones only: `uv run pytest tests/test_catalog.py -q`,
`uv run twiddle stations probe <url>`, then the full `uv run pytest -q`.

## Non-goals
- Not: ...

## Context
What to read first: the file to copy the shape of, the skill to follow (`add-radio-station`),
the issue's own request.

## Safety class
Read-only: no speaker write, no Spotify write, no `logs/interventions.jsonl` entry.
```

How to fill each part so the tool accepts it:

- **Files** lists every file the diff will touch, tests and fixtures included, one bullet per
  path with the path in the first backtick span. An entry is an exact path or a pattern
  (`src/twiddle/stations/catalog/*.toml`; `*` also crosses `/`). A diff that changes a file
  not listed, a renamed file's old path included, is refused at `merge` and the change moves
  to the full lane. Nothing listed may be a denied path (`.sdlc/config.json`'s `quick`
  section: `tools/`, `.agents/`, `.claude/`, `.sdlc/`, `.github/`, `SDLC.md`, `CLAUDE.md`,
  `docs/sdlc.html`, `pyproject.toml`, `uv.lock`, and the speaker-writing modules), and the
  count must stay within `max_files` and `max_lines` there (5 files and 200 added plus
  deleted lines, as set today). If the honest list is longer, it is not a quick change.
- **Acceptance criteria** are what Codex and `merge` hold the PR to, so keep them few and
  checkable.
- **Validation** is read-only, like a bug's reproduction: never a command that writes to a
  speaker.
- **Safety class** is one line. If you cannot truthfully write "read-only" there, the change
  is not quick.
- A brief has no `## Demo`: the PR body carries the evidence (the commands run and what they
  printed) in place of one.
- A diagnosis for a bug uses the same sections, with the diagnosis template's content folded
  in: the reproduction and evidence go under `## Context`, the fix under `## Scope`.
