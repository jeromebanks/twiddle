# SDLC: how an issue becomes shipped work

For people new to it, [`docs/sdlc.html`](docs/sdlc.html) walks through the same workflow with
diagrams: every state from filing to closing, and the skill that moves each one on.

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
                  sdlc:planned ─► sdlc:in-progress ◄─► sdlc:demo-review
                     (work-slice: one slice     (milestone-demo:    poster: /approve
                      per session, onto epic/N)  one per milestone)  ─► ship M to main
                                                                       (the last: sdlc:done)
   any stage ─► sdlc:escalated  (a human is needed: no consensus, or the agent is stuck)
```

The skills: `triage-issue` (to `approved`), `plan-issue` (to `planned`), `work-slice`
(builds the slices; the epic is `in-progress` from the first claim) and `milestone-demo`
(shows the poster each finished milestone, then ships each accepted one to `main`; the last ship is `done`).

## Branches

```
main ──●────────────────────●───────────────────────●──►      (only accepted milestones)
        \ epic/N             \ ship M1 (merge commit) \ ship M2
         ●──s1──s2──s3──sync──●──s4──s5────────────────●
             (each slice: one squash commit, from a slice/S PR)
```

- `plan-create` makes **`epic/N`** from `main` (`epic-branch N` does it for an epic planned
  earlier, or adopts an existing branch). Every slice PR targets it and lands as one squash
  commit, so a bad slice is one revert (`revert-slice S`) before it ever reaches `main`.
- The **demo** runs from `epic/N`, with `main` as the "before".
- **`sync N`** merges `main` into `epic/N` (never a rebase: slice branches are built on it),
  runs the full suite there, records it on the epic, and pushes on a pass. A conflict or a
  failure escalates.
- **`ship N`** releases one accepted milestone, in order: a pull request `epic/N -> main`, merged
  with a merge commit. It needs `epic/N` to contain `main`, a passing run and Codex's approval of
  the milestone's diff (`ship-review`), both on the head that ships, and no slice of an unaccepted
  milestone on the branch. Every commit on `epic/N` must trace to a slice: its squash, a
  `git cherry-pick -x` of one, or a revert of one. A merge must be a clean merge of `main`
  (exactly what git would make of its parents); one that resolved a conflict ships only with
  `ship --accept-merge <sha>` after a human has looked at it. Anything else stops the ship. After the last milestone, `epic/N` is deleted.
- **One milestone at a time on the branch.** Only the earliest milestone that hasn't shipped
  is built: `next:` offers only its slices, and `claim` and `merge` refuse a later milestone's,
  so nothing unseen rides along to `main`. Once it's complete, new slices wait for its demo and
  its release. `ship` also refuses while changes are being planned or built, or a release PR
  would carry a merged slice of an unaccepted milestone (read from the PRs into `epic/N`).
- `ship` checks `main` again right before it merges, and after. A `main` that moved in between
  escalates, because the merge then holds commits the recorded run never saw. Rerunning `ship`
  records a release PR that merged without its record, and finishes an epic whose last ship
  was cut short.

**What to run next is always printed:** `uv run python tools/sdlc.py state N` (or `next`)
ends with a `next:` line, such as `/plan-issue 12` or `/work-slice 28`. Every skill ends its report with it.

## Whose move

| Label | Whose move |
|---|---|
| none, `sdlc:triage` | the agent |
| `sdlc:needs-info`, `sdlc:prd-review`, `sdlc:diagnosis-review` | the poster, until they reply; then the agent |
| `sdlc:approved` | the agent: `plan-issue` (the poster is not asked to review the plan) |
| `sdlc:needs-info` after a PRD approval | the poster, answering a planning question; then the agent replans |
| `sdlc:planned`, `sdlc:in-progress` | the agent: `/work-slice <ready slice>`, `/milestone-demo N` once a milestone's units are all merged or an accepted one is due to ship, or `sync N` when `epic/N` is behind `main` and nothing else is going on (state's `next:`) |
| `sdlc:demo-review` | the poster, until they reply to the demo; then the agent (`/milestone-demo N`) |
| `sdlc:in-progress` after a demo's `/changes` | the agent: `/plan-issue N` plans them as new slices |
| `plan:slice` / `plan:subtask` issues | never triaged; a slice's state is `slice-status N` |
| `sdlc:escalated` | a human |

`uv run python tools/sdlc.py next` lists issues where it is the agent's move;
`uv run python tools/sdlc.py state N` explains one.

## What you can type (posters and maintainers)

- **Reply in a comment** to answer a question or react to a PRD.
- **`/approve`** on its own line signs off on the *latest* PRD/diagnosis revision, or accepts the latest milestone demo.
- **`/changes <what>`** asks for changes. Any other reply is treated as feedback.
- A maintainer may instead add the **`sdlc:approved`** label; the next run records it as an approval comment.

`/approve` counts only from the issue's author or someone who can write to the
repo, only when it starts a line (not in a quote or code block), and only for the
latest revision: when a new revision is posted, earlier approvals no longer count.

## People are reached through the issue

The skills may be run by an orchestrator with nobody in the chat, so no skill asks
a question in the session. Every human decision is an issue comment that
`tools/sdlc.py` reads back:

- a PRD's sign-off;
- a planning question;
- a demo's `/approve` or `/changes`;
- what someone heard when they ran a demo step that writes to a speaker
  (`demo-request`; their reply makes the action `demo_heard`).

Publishing a demo is gated by `demo-post`'s checks, not by a person's OK.
`--preview` and `confirm_before_posting` are opt-ins for an interactive run.

**What a demo finds** is filed, never left in the chat:

- **broken**, a promise of this milestone fails: `demo-changes --found`, no demo is
  posted, and `plan-issue` plans fix slices;
- **non-blocking**: `file-issue --debt`, linked from the demo;
- **out of scope**: `file-issue`, a new issue for triage.

`work-slice` files the non-blocking review findings and adjacent issues the same way
when it merges.

**Tech debt is budgeted.** After each accepted milestone, if the epic has open
`tech-debt` issues, `next:` says `/plan-issue N --cleanup`. That plans up to
`cleanup_slices_per_milestone` of them as slices in the next milestone, Codex-reviewed,
before its other slices, and `merge` closes the debt they pay down. After the last
milestone the rest stay filed for triage. Ready slices are always taken from the
earliest milestone first, so fix slices go before later work.

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

**One milestone at a time.** The plan covers the whole epic, but by default
(`"create": "milestone"` in `.sdlc/config.json`, or per plan) only the first milestone's
issues are created. Later milestones depend on what earlier ones teach. When a
milestone's demo is accepted, `next:` says `/plan-issue N`: the planner revises the
rest of the plan, with a `Lessons from M1` section, and Codex reviews it with a fresh
budget. Only then does `plan-create` make the next milestone. `"create": "all"` keeps
the old behaviour, creating everything at once.

No consensus within `max_plan_rounds` on the first plan means the planner asks the poster
one plain-language question (`sdlc:needs-info`, marked `phase=plan`). Their answer earns a
fresh set of rounds. An amendment while the epic is being built (demo changes, the next
milestone, a cleanup pass) that runs out of rounds is escalated to a human instead
(`escalate_plan`). A plan revision never voids the PRD's sign-off.

`uv run python tools/sdlc.py ready --epic N` lists the units of work whose
blockers have all closed as completed.

The planner rates each unit `routine`, `judgment` or `novel` (with a reason Codex
checks). It becomes a `complexity:<level>` label and a line in the slice, and
`.sdlc/config.json`'s `models` maps it to the model that builds it: `next:` says e.g.
`/work-slice 29 (sonnet: routine)`. `plan-annotate N` backfills ratings onto an epic
created before them. A slice that escalates on a cheaper model is retried once on the
strongest before a human gets it: `claim N --retry --model <strongest>` lifts the
escalation, keeps the branch and PR, and gives the retry a fresh Codex budget.

## Building (`work-slice`)

One slice per session, in its own git worktree (`.worktrees/slice-N`, branch `slice/N`), so
several can run at once. A slice's state is read off GitHub:

`blocked → ready → claimed → in-review → merged`, plus `escalated`.

1. `claim N` posts a claim on the slice, assigns it, creates the worktree from `origin/epic/E`,
   and moves the epic to `in-progress`.
2. The implementer builds only the slice, with offline tests. Real speaker fires belong to
   milestone demos.
3. The PR closes exactly that slice. `test-record` runs the full `pytest` itself and records the
   result on the PR for the head SHA. The repo has no CI, so this is the test gate.
4. **Codex** reviews the head read-only. Its report must name the `HEAD:` it reviewed. The
   implementer fixes or rebuts each finding, and each round is posted with `pr-review`. After
   `max_pr_rounds` (5) without approval: `escalate-slice`, and the slice goes to a human.
5. `merge` is the gate: the PR targets `epic/E` and isn't behind it, Codex's latest review approves
   the **current head**, a passing test run is recorded on the current head, the PR closes one open
   `plan:slice`, no other milestone is waiting for its demo or release, and it is mergeable. Then
   the agent squash-merges into `epic/E` and closes the slice itself (GitHub only acts on
   `Closes #N` in the default branch). A new commit voids both records. Humans review at
   milestone demos, not per PR.
6. `cleanup N`, run from the primary checkout, removes the worktree and the branch.

## Milestone demos (`milestone-demo`)

When every unit of work in a milestone has merged (for an epic without milestones,
every unit), the poster sees it working. **New slices of the epic wait** until they
accept it and it ships: `next:` says `/milestone-demo N` and `claim` refuses. Slices
already claimed of the same milestone can finish; nothing of a later milestone lands.

1. The agent runs the milestone's demo steps. Read-only and `--dry-run` steps run
   freely. A real speaker write is never run by the agent: it asks on the issue
   (`demo-request`, each step with its dry run and the `snapshot`/`restore` bracket),
   and what the person heard, in their reply, is recorded with `observe.py` and quoted.
   A milestone the demo finds broken gets fix slices first (`demo-changes --found`).
2. `tools/demo_shot.py` draws the real output and TUI screens as SVG, run in a worktree of
   `epic/N`. A bug fix shows before (`main`) and after (`epic/N`).
3. `demo-post` commits the pictures and a full write-up to the orphan branch
   **`sdlc-demos`** (`epic-N/M1/rev-K/`; never merged), pins every link to that commit,
   posts the demo on the epic (`kind=demo milestone=M1 rev=K`), and moves it to
   `sdlc:demo-review`. The repo is public, so it refuses anything that looks like an
   IP, a MAC, a Sonos ID or a secret.
4. The poster answers on the issue:
   - `/approve`: `demo-accept` records it, closes the GitHub milestone, and moves the epic
     back to `in-progress`. Then the agent ships it: `sync`, a Codex review of the milestone's
     diff (`ship-review`), and `ship`. After the last milestone ships, the epic is `done`.
   - `/changes`: `demo-changes` records them in plain words, the epic returns to
     `in-progress`, and `plan-issue` amends the plan with new slices (Codex-reviewed;
     nothing already created is dropped). When they merge, the milestone gets demo rev 2.
   - A question: answered with a note, and the demo stays in review.

A demo, like a plan revision, never voids the PRD's sign-off.

## First-time setup

```bash
uv run python tools/sdlc.py bootstrap-labels   # idempotent
```
