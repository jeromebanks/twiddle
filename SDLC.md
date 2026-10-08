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
  `ship --accept-merge <sha>` after a human has looked at it. Anything else stops the ship.
  A milestone built straight onto `main`, before epic branches, ships as a recorded no-op:
  no sync, and no `ship-review`, which refuses it (`main...epic/N` would be a later milestone's diff).
  After the last milestone, `epic/N` is deleted.
- **One milestone at a time on the branch.** Only the earliest milestone that hasn't shipped
  is built: `next:` offers only its slices, and `claim` and `merge` refuse a later milestone's,
  so nothing unseen rides along to `main`. Once it's complete, new slices wait for its demo and
  its release. `ship` also refuses while changes are being planned or built, or a release PR
  would carry a merged slice of an unaccepted milestone (read from the PRs into `epic/N`).
- `ship` checks `main` again right before it merges, and after. A `main` that moved in between
  escalates, because the merge then holds commits the recorded run never saw: the release is
  recorded `untested`, and nothing starts and the epic can't finish until `verify-main N`
  passes on `main`. Rerunning `ship`
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
   `codex-review --plan N` runs each round on the revision as posted, held to the currently
   approved PRD or diagnosis, in a scratch checkout of `origin/main` it makes and removes.
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
`/work-slice 29 (sonnet: routine)`. The rating also says where Codex reviews the slice
(see "Codex reviews" below). `plan-annotate N` backfills ratings onto an epic
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
4. **Codex** reviews the head read-only, unless the slice is reviewed with its milestone
   (below). `codex-review --pr PR` runs each round on the repo's own settings and stamps the
   report with the `HEAD:` it checked, then the model, effort, `codex --version` and the sha256 of
   the exact prompt (see "What a review runs under" below). The
   implementer fixes or rebuts each finding, and each round is posted with `pr-review`. After
   `max_pr_rounds` (5) without approval: `escalate-slice`, and the slice goes to a human.
5. `merge` is the gate: the PR targets `epic/E` and isn't behind it, Codex's latest review approves
   the **current head** (or the review is owed to the milestone), a passing test run is recorded on the current head, the PR closes one open
   `plan:slice`, no other milestone is waiting for its demo or release, and it is mergeable. Then
   the agent squash-merges into `epic/E` and closes the slice itself (GitHub only acts on
   `Closes #N` in the default branch). A new commit voids both records. Humans review at
   milestone demos, not per PR.
6. `cleanup N`, run from the primary checkout, removes the worktree and the branch.

### Codex reviews

**`codex-review` is the only way a review is run**: `--pr PR` for a slice, `--plan N` for a plan
revision, `--milestone N` for a milestone's diff. It fills the repo's prompt, runs Codex on the
repo's settings, checks the commit it reads and stamps the report. Never Codex's own
`codex exec review`, the Codex plugin for Claude Code, or a `codex exec` run by hand: each brings
instructions or settings the repo doesn't own, and nothing on the record would show it.

Codex reviews twice: each slice on its PR, and each milestone's whole diff before it ships
(`codex-review --milestone N`, recorded with `ship-review`). A slice's own review moves to its
milestone's in two cases:

- **Routine slices.** `.sdlc/config.json`'s `slice_review` maps a complexity to `slice` or
  `milestone`. Here `routine` is `milestone`; `judgment`, `novel` and unrated slices are
  reviewed on their PR. Without `slice_review`, every slice is.
- **Codex can't run** (not installed, not signed in, out of quota, the network, timing out).
  `review-defer PR --reason "<the error>"` records it on the PR, bound to the head. It is
  refused once Codex has asked for changes that no later round approved: findings nobody
  checked can't be deferred.

Either way only the review relaxes: the tests, the base and the rest of the gate still
hold, and a review that asks for changes always blocks. `merge` posts a `review-owed`
record on the epic **before** the squash. A milestone with a review owed is in phase
`review` once complete: new slices wait, `demo-post` refuses, and `next:` says
`/milestone-demo N`, which syncs `epic/N` and has Codex review the milestone, reading each
owed slice as closely as a pull request. An approving `ship-review` clears every review
owed before it; one asking for changes is a demo finding (fix slices). If the head doesn't
move before the ship, the same approval serves the ship.

### What a review runs under

`codex-review` runs Codex on a scratch `CODEX_HOME`, `<out>/codex-home`, that holds only two
things: `auth.json`, a private 0600 copy of your sign-in (`$CODEX_HOME/auth.json` if you set
one, else `~/.codex/auth.json`), and a `config.toml` the tool writes from `.sdlc/config.json`'s
`codex` section (model, effort, sandbox, and the `file` sign-in store). So nothing in your own
Codex home applies to a review: not its `config.toml`, `AGENTS.md`, skills, memories, rules,
plugins or MCP servers. With no `auth.json` to copy, it refuses before starting Codex (exit 3,
the `review-defer` cue): run `codex login` in a terminal. A sign-in kept only in the keyring
can't be used.

The sign-in survives a refresh. It's a copy, not a link, because Codex's file store rewrites
`auth.json` in place (open, truncate, write): through a link, a refresh would land in your file
unguarded, over a sign-in made elsewhere, or half-written if the run were cut short. Instead,
once the run ends, however it ends (a verdict, a timeout, a failure, an interrupt), the tool
writes a changed copy back to your file atomically, and only when the copy is a whole
`auth.json` and your own file is still what it was when the run started. Otherwise your file is
kept and a warning says why (the refreshed token was discarded, or `codex login` may be needed).
The copy is then removed. If the write-back itself fails, the copy is left in `codex-home` with a
warning, and the next run refuses until it's dealt with. The rest of `codex-home` stays, with the
run's session log under `sessions/`.

What isolation can't cover: anything Codex reads from outside `CODEX_HOME`. That includes the
repo itself (its `AGENTS.md`, and its `.agents/skills/` unless they are suppressed, below), the environment (`OPENAI_API_KEY` and the
like), and anything it finds through `HOME`, which is left alone because git and the keychain
need it. Nor a sign-in made elsewhere in the instant between the last check of your file and the
rename over it: the check is made with the new file ready, just before the rename, but `rename`
has no compare-and-swap, and there is no lock this tool shares with every writer of `auth.json`.
And the tool only sets things up; the real binary decides what it loads. That is
checked by hand once, in the demo of #100's first milestone, from a real run's session log.

The saved report's header (`HEAD:`, `Model:`, `Effort:`, `Codex:`, `Prompt-SHA256:`) is written
by the tool. `pr-review`, `plan-review` and `ship-review` copy it into the round's comment and its marker. Only the lines
directly under `HEAD:` count, so a report can't claim a model of its own. A report saved before
the header existed is still recorded, without it.

### The review logic's fingerprint, and its eval

How a review behaves is decided by its prompts, its rules, its settings, the code that fills and
runs it, and the `codex` binary. The repo owns all of these except the binary. `tools/codex_eval.py`
hashes the parts it owns into one **fingerprint**, and an eval result (`codex-eval`, run in a later
slice) is recorded against the pair (fingerprint, `codex --version`). A pass for one pair never
counts for another, so a new `codex` needs a new eval even when nothing in the repo changed.

The fingerprint's inputs, by name (`codex-eval --status --inputs` lists them):

- the three prompts (`codex-pr-prompt.md`, `codex-milestone-prompt.md`, `codex-plan-prompt.md`),
  `.agents/skills/review-rules.md`, and `plan-schema.md` (the plan prompt has Codex read it);
- `.sdlc/config.json`'s `codex` section, as canonical JSON (not the rest of the file);
- `tools/codex_review.py`, which fills and runs every round and also **chooses what a round is
  handed**: which requirements a plan round is held to, which slices and briefs a milestone round
  lists, which of them are owed a review, and which response a round reads. `sdlc.py` only
  fetches the bundle and passes it in, and a test fails if it defines a function that fills a
  prompt or picks its inputs;
- `tools/codex_eval.py`, and everything under `tests/codex_eval/` (the eval's fixtures and scorer);
- every `AGENTS.md` and `AGENTS.override.md` anywhere in the repo (none exists today), because Codex
  loads them on its own.

Not `sdlc.py` and not `CLAUDE.md`, so ordinary edits there cost no eval. The prompts point Codex at
`review-rules.md` for the safety rules. The plan prompt names `CLAUDE.md` only for its Layout
table, as knowledge of the code under review, and `codex_eval.EXCLUDED` says why each such file is
left out. A test fails if a prompt names a file, or `codex_review.py` holds a path, that is neither
an input nor excluded. The fingerprint reads the same from a commit (`git ls-tree`) as from a
checkout of it (`git ls-files`, untracked files included), and prints as its first 12 hex digits:
a full hash looks like an identifier to `demo-post`.

**Skills are suppressed.** M1's demo showed that a real review was offered Codex's built-in skills
and the repo's own `.agents/skills`, and its session log showed `skills.includeInstructions: true`.
`codex.skills` in the config picks one of two modes, so the choice is part of the fingerprint:

- `suppressed` (in force): the generated `config.toml` sets `[skills] include_instructions = false`.
  This was confirmed against codex-cli 0.161.0 without spending tokens: a run with no sign-in still
  writes its session log, and with the setting that log has no `<skills_instructions>` message and
  `skills.includeInstructions: false`. Without the setting it has both. After every run the tool
  reads the run's session log (`codex_review.check_session`). If skills were offered, or Codex
  loaded an `AGENTS.md` from outside the reviewed checkout, or the log isn't a shape the tool knows
  (a new `codex` may change it), the round saves no report and exits 3, with the reason at the
  end of `codex.err`. A `codex` that ignores the setting can't produce a review that looks like
  one that ran without skills. For the same reason `codex.flags` may not pass `--ephemeral`, which
  stops the log being written. The first time this exit happens after a `codex` upgrade, look in
  `<out>/codex-home/sessions/` before deferring: the tool may need to learn the new shape.
- `fingerprinted`, only if a future `codex` drops the setting: skills are offered, and every
  `.agents/skills/**/SKILL.md` becomes an input. Codex's built-in skills ship with the binary, so
  the version in the pair covers them. The `AGENTS.md` check still runs.

What the fingerprint can't cover: what the binary does with the same inputs (that is the
version's half of the pair), the model behind the API, and anything outside the repo that
isolation doesn't already keep out (above).

**Results.** Each result is a marked `codex-eval` comment on the issue `.sdlc/config.json`'s
`codex_eval_issue` names (#100). It holds the full fingerprint, the `codex` version, the model,
the effort, the outcome (`pass`, `fail` or `deferred`) and one line per fixture. A `codex` whose
`--version` says nothing usable pairs with nothing, and no pass can be recorded for it. A closed issue's
comments still read the same, so the results outlive the epic. The latest result per pair is the
one that counts. A result comment is a record, not part of the conversation: it never counts as
the agent's last comment, so one posted during a demo never hides the poster's answer.

`codex-eval --status` prints the fingerprint, `codex --version` and that pair's result. It is
read-only and never runs `codex exec`. Before each round, `codex-review` fingerprints the review
logic that round actually runs under. The files the tool reads (the prompts, its code, the
fixtures) come from the checkout the command runs from. The files Codex reads (`review-rules.md`,
`plan-schema.md`, any `AGENTS.md`, and the skills in fingerprinted mode) come from the checkout
Codex runs in: the slice's worktree, the scratch checkout of `main` for a plan, or the epic's
worktree. The settings are the ones the round runs with, a `--config` of its own included. It
warns when that pair has no passing result, and the round still runs.

**Round limits** (`.sdlc/config.json`): `max_pr_rounds` (default 5) is how many Codex rounds
that ask for changes a slice PR, or a milestone, may take before it escalates.
`max_plan_rounds` (default 3) is the same for a plan, and `max_rounds` (5) for triage.

## Worktrees, and several sessions at once

Every worktree here is a plain **git worktree** that `tools/sdlc.py` or a skill makes with
`git worktree add`. None of them comes from Claude Code's own worktree feature (`EnterWorktree`,
`claude --worktree`, an agent's `isolation: "worktree"`). That feature puts its worktrees under
`.claude/worktrees/`, branches them from `origin/main` and names the branches itself. The SDLC
needs `slice/S` cut from `origin/epic/N`, at a path that `claim`, `merge` and `cleanup` can find
again from the issue. A session starts in the primary checkout and stays rooted there. The skill
makes it work in the worktree by writing every path out in full (`work-slice` §2).

| Worktree | Made by | For | Removed by |
|---|---|---|---|
| `.worktrees/slice-S`, on `slice/S` from `origin/epic/N` | `claim S` | building one slice | `cleanup S` |
| `.worktrees/epic-N`, detached at `origin/epic/N` | `sync N`, `revert-slice S` | merging `main` in, reverting a slice, Codex's milestone review (`codex-review --milestone N` reads it as `sync` left it: it never resets it, and refuses it dirty or behind `epic/N`) | nobody: `sync` and `revert-slice` reset it to `origin/epic/N` before each use |
| `.worktrees/sdlc-demos`, on `sdlc-demos` | `demo-post` | committing a demo's pictures | nobody: kept |
| `.worktrees/verify-main-N`, detached at `origin/main` | `verify-main N` | the suite on `main` after an untested release | `verify-main N`, when it's done |
| `<out>/main`, detached at `origin/main` | `codex-review --plan N` | Codex's plan review reads the repo there | `codex-review --plan N`, when the round ends (a left-over one is replaced on the next run) |
| `<scratchpad>/before` and `after`, detached | `milestone-demo` | the pictures of `main` and `epic/N` | the skill (`git worktree remove`) |
| a throwaway worktree on `prd/N`, from `origin/main` | `triage-issue` | the approved PRD's one-file PR | the skill |

`.worktrees/` is gitignored. **The primary checkout stays on `main`, and stays clean.** Every
session runs `tools/sdlc.py` from it, and the tool adds and removes worktrees from it, so nothing
switches its branch or edits files in it.

### Running issues in parallel

Run one Claude Code session per piece of work, each started in the primary checkout, and give
each a different issue or slice: for example `/work-slice 35` in one terminal and
`/triage-issue 80` in another. `ready` lists every slice that can be claimed, across all epics.

- **Different issues share nothing but the primary checkout.** Each epic has its own `epic/N`.
  Each slice has its own worktree, branch and `.venv`, so its first `uv sync` and suite run are
  slower. Triage and planning write only to GitHub and to the session's scratchpad.
- **Slices of one epic** run in parallel only within the milestone being built: one milestone
  is built at a time. `blocked_by` holds back any slice that needs another's merged code.
- **Two sessions can't take the same slice.** `claim` refuses a slice with an active claim, which
  it reads off GitHub. On one machine the worktree also guards it: a second `claim S` finds
  `.worktrees/slice-S` and stops before posting anything. Across two machines, the GitHub check
  is the only guard. It reads and then posts, so two claims seconds apart could both land. Claim
  from one machine.
- **Merges into `epic/N` happen one at a time.** The gate refuses a PR that is behind `epic/N`.
  The second slice to finish rebases onto `origin/epic/N`. Its new head needs a fresh
  `test-record` and its review again (a Codex round, or a new deferral; nothing more for a slice
  reviewed with its milestone), because the records are bound to the head SHA. A review of a
  rebased head doesn't spend the round budget.
- **`sync` and `revert-slice` share `.worktrees/epic-N`** and take a lock on it. A second one
  started meanwhile refuses ("wait for it, then rerun") and doesn't queue.
- **Only one session touches a speaker at a time.** Slices never touch one (tests are offline).
  A milestone demo writes to the real household with the user present, so run one demo at a time.
- **The machine is shared.** Each full suite takes about two minutes, and parallel runs slow
  each other down. Give the Bash call `timeout: 600000`, as the skills say.

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
