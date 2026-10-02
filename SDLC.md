# SDLC: how an issue becomes shipped work

GitHub is the audit trail. Every step is a comment or a label on the issue, so
the state of any issue can be read off the issue, and a Claude Code (or Codex)
session can pick up whatever needs doing next.

```
issue ─► sdlc:triage ─► sdlc:needs-info ◄─► (poster answers)
              │
              ├─► sdlc:prd-review        (feature: a PRD, what to build)
              └─► sdlc:diagnosis-review  (bug: reproduce, what's wrong, the fix)
                        │  poster: /approve   (or /changes ..., or just comment)
                        ▼
                  sdlc:approved ─► sdlc:planned ─► sdlc:in-progress ─► sdlc:demo-review ─► sdlc:done
                                   └──────── later skills (not built yet) ────────┘
   any stage ─► sdlc:escalated  (a human is needed: no consensus, or the agent is stuck)
```

Only the triage half exists so far (`triage-issue`); the labels after
`approved` are created now so they never need renaming.

## Whose move

| Label | Whose move |
|---|---|
| none, `sdlc:triage` | the agent |
| `sdlc:needs-info`, `sdlc:prd-review`, `sdlc:diagnosis-review` | the poster, until they reply; then the agent |
| `sdlc:approved` | the agent (planning, once that skill exists) |
| `sdlc:escalated` | a human |

`uv run python tools/sdlc.py next` lists issues where it is the agent's move;
`uv run python tools/sdlc.py state N` explains one.

## What you can type (posters and maintainers)

- **Reply in a comment** to answer a question or react to a PRD.
- **`/approve`** on its own line signs off on the *latest* PRD/diagnosis revision.
- **`/changes <what>`** asks for changes. Any other reply is treated as feedback.
- A maintainer may instead add the **`sdlc:approved`** label; the next run records it as an approval comment.

`/approve` counts only from the issue's author or someone who can write to the
repo, only when it starts a line (not in a quote or code block), and only for the
latest revision: when a new revision is posted, earlier approvals no longer count.

## Rules the tooling enforces

- Agent comments start with `<!-- sdlc:v1 kind=... rev=N -->`. The agent posts through
  `gh` as the maintainer, so the marker is the only thing telling the two apart. Markers
  from accounts that can't write to the repo are ignored. Anyone with write access can
  forge one: this is an audit trail, not a security boundary.
- One `sdlc:*` label per issue, changed only by `tools/sdlc.py`. Two at once is a
  conflict and needs a human.
- Each PRD/diagnosis revision is a new comment; the previous one is collapsed and
  linked to its successor. After `max_rounds` (`.sdlc/config.json`) the agent escalates.
- Reproducing a bug uses read-only commands and `--dry-run` only (see `CLAUDE.md`).

## First-time setup

```bash
uv run python tools/sdlc.py bootstrap-labels   # idempotent
```
