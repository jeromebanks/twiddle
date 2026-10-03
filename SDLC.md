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
                  sdlc:approved ─► (plan: advisor, then Codex rounds until VERDICT: approve)
                        │      └─ no consensus ─► sdlc:needs-info (a plain-language question to the poster)
                        ▼
                  sdlc:planned ─► sdlc:in-progress ─► sdlc:demo-review ─► sdlc:done
                     (work-slice: one slice per session) └─ milestone-demo (not built yet) ┘
   any stage ─► sdlc:escalated  (a human is needed: no consensus, or the agent is stuck)
```

Built so far: `triage-issue` (to `approved`), `plan-issue` (to `planned`) and `work-slice`
(builds the slices; the epic is `in-progress` from the first claim).

**What to run next is always printed:** `uv run python tools/sdlc.py state N` (or `next`)
ends with a `next:` line, such as `/plan-issue 12` or `/work-slice 28`. Every skill ends its report with it.
The later labels exist already so they never need renaming.

## Whose move

| Label | Whose move |
|---|---|
| none, `sdlc:triage` | the agent |
| `sdlc:needs-info`, `sdlc:prd-review`, `sdlc:diagnosis-review` | the poster, until they reply; then the agent |
| `sdlc:approved` | the agent: `plan-issue` (the poster is not asked to review the plan) |
| `sdlc:needs-info` after a PRD approval | the poster, answering a planning question; then the agent replans |
| `sdlc:planned`, `sdlc:in-progress` | the agent: `/work-slice <ready slice>` (state's `next:`) |
| `plan:slice` / `plan:subtask` issues | never triaged; a slice's state is `slice-status N` |
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

## Planning (`plan-issue`)

The approved issue becomes the **epic**. The planner writes a plan (subtasks,
one-session slices, milestones only when they buy a demo) and posts it as
`plan rev K`. The poster is an end user, so they are not asked to sign it off.
Instead the agents review it:

1. The planner checks it with its `advisor`.
2. **Codex** reviews it read-only, ending in `VERDICT: approve` or `VERDICT: changes`.
3. The planner accepts or rebuts each finding, and each round is posted as a
   `plan-review` comment.
4. When Codex approves the latest revision, that's consensus, and `plan-create`
   builds it.

The build makes milestones, then subtasks as **native sub-issues** of the epic,
then slices as sub-issues of their subtask (labels `plan:subtask` / `plan:slice`).
Ordering is **native `blocked_by`** links. All of it is created from the
reviewed comment, so what was reviewed is what was built. A rerun finishes a
partial creation instead of duplicating it.

No consensus within `max_plan_rounds` means the planner asks the poster one
plain-language question (`sdlc:needs-info`, marked `phase=plan`). Their answer
earns a fresh set of rounds. A plan revision never voids the PRD's sign-off.

`uv run python tools/sdlc.py ready --epic N` lists the units of work whose
blockers have all closed as completed.

## Building (`work-slice`)

One slice per session, in its own git worktree (`.worktrees/slice-N`, branch `slice/N`), so
several can run at once. A slice's state is read off GitHub:

`blocked → ready → claimed → in-review → merged`, plus `escalated`.

1. `claim N` posts a claim on the slice, assigns it, creates the worktree from `origin/main`,
   and moves the epic to `in-progress`.
2. The implementer builds only the slice, with offline tests. Real speaker fires belong to
   milestone demos.
3. The PR closes exactly that slice. `test-record` runs the full `pytest` itself and records the
   result on the PR for the head SHA. The repo has no CI, so this is the test gate.
4. **Codex** reviews the head read-only. Its report must name the `HEAD:` it reviewed. The
   implementer fixes or rebuts each finding, and each round is posted with `pr-review`. After
   `max_pr_rounds` (5) without approval: `escalate-slice`, and the slice goes to a human.
5. `merge` is the gate: Codex's latest review approves the **current head**, a passing test run is
   recorded on the current head, the PR closes one open `plan:slice`, and it is mergeable. Then
   the agent squash-merges. A new commit voids both records. Humans review at milestone demos,
   not per PR.
6. `cleanup N`, run from the primary checkout, removes the worktree and the branch.

## First-time setup

```bash
uv run python tools/sdlc.py bootstrap-labels   # idempotent
```
