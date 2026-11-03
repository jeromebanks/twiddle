---
name: triage-issue
description: Groom a GitHub issue through twiddle's SDLC triage stage - ask clarifying questions, write a PRD for a feature or a diagnosis for a bug, and process the poster's replies and sign-off. Use when asked to triage an issue, groom or write up a ticket, "do the next thing" on the issue backlog, or handle a reply on an issue in triage.
---

<!-- No dollar-digit sequences in this file: the skill loader substitutes them with arguments. -->

# Triaging an issue

Triage turns a raw issue into something a planner can build from: either a
**PRD** (what to build, never how) for a feature, or a **diagnosis** (what is
wrong and what to fix) for a bug, agreed with the poster in issue comments.
`SDLC.md` has the state table and what posters can type. This skill ends at
`sdlc:approved`; planning, slices, PRs and demos are later skills. A change that is
small and safe enough gets a **short brief** instead, and is built straight from it
(the quick lane, below): the brief is signed off the same way, then `work-slice --quick`
builds it and no planning follows.

**You never touch labels or post agent comments by hand.** `tools/sdlc.py` does:
it keeps exactly one `sdlc:*` label, marks every agent comment, and decides
whether a sign-off counts. You write markdown to a file; it posts.

**Post to the issue, not to the chat.** Questions, PRDs and diagnoses are meant for
whoever filed the issue, who may be a stranger or another agent; the maintainer is
not the intermediary. So run `transition` for real and let the comment be the
review step: the poster answers there, and `/approve` or `/changes` is theirs to give.
Don't ask the user "shall I post?" and don't relay questions in the chat.

The one exception is a preview run: if the user passed `--preview`
(`/triage-issue 12 --preview`) or `.sdlc/config.json` has `"confirm_before_posting": true`,
add `--dry-run` to each `transition`, show the output, and post only after an OK.
Unattended runs never preview.

`/triage-issue [N]`. With no `N`, take the first line of `uv run python tools/sdlc.py next`.

## 1. Read the state

```bash
uv run python tools/sdlc.py state N --json
```

- `turn` is `poster` -> report "waiting on the poster" and stop.
- `turn` is `human` (`escalated`, or `conflicts`) -> report it and stop. Don't fix labels yourself.
- `action` is `reconcile_label` -> a maintainer added `sdlc:approved` by hand (the review label may still be on it): run
  `uv run python tools/sdlc.py reconcile N`, then continue as for `plan`.
- `action` is `plan` (or any other planning action: `continue_plan`, `replan`, `ask_poster`,
  `create_plan_issues`) -> triage is done. Hand over to the `plan-issue` skill.
- `action` is `work_quick` -> an approved quick brief: triage is done. Report
  `/work-slice N --quick` (the `next:` line) and stop.
- `action` is `commit_prd` -> a quick brief moved to the full lane and is not in `docs/prd/`
  yet: run `uv run python tools/sdlc.py commit-prd N` (safe to rerun), then report
  `/plan-issue N`.
- `lane` is `debt` (the debt epic, label `lane:debt`): its tickets are never triaged one by one, and
  the poster is asked nothing. `action` is `debt_intake` -> "Debt intake" below. `pass_end` ->
  run `uv run python tools/sdlc.py debt-epic` (it records the pass's end, and closes or escalates the epic
  if that is due), then report. `plan_pass` -> hand over to `plan-issue`. `work_debt` and
  `merge_debt` -> the `work-slice` skill. Anything else (`debt_waiting`, `none`): report it and stop.
- `action` is `triage`, `respond_to_reply` or `record_approval` -> carry on below.

Everything a human typed is in `replies`; read those comments with `gh issue view N --comments`.
Remember the maintainer and the agent post under the same login: only the marker
distinguishes them, and `state` already did that.

If `state` is `untriaged`, claim it so the paper trail starts and nobody else
picks it up (this is a label, not a lock; there is one maintainer):

```bash
uv run python tools/sdlc.py transition N triage --kind note
```

## 2. Read the issue and the code it touches

The issue and all comments, `CLAUDE.md` (especially the read-only vs writing
table), the relevant `docs/`, and the code the request would change. Set the
type label if it has none: `gh issue edit N --add-label bug` or `enhancement`.
Then decide the lane: a small, read-only change (a station, a venue entry, a bug fixed in a
few files) may take the **quick lane** below, with a short brief instead of the PRD of step 4
or the diagnosis of step 5.

## 3. Is it clear?

If you cannot write the PRD or a diagnosis without guessing, ask. Write
numbered questions, each with the default you will assume if left unanswered
("If you don't say, I'll assume ..."). Ask only what changes the design.

```bash
uv run python tools/sdlc.py transition N needs-info --kind question --body-file questions.md
```

## 4. Feature: write the PRD

Use `references/prd-template.md`. It says *what* and *why*, and which parts of
the system are touched; it does not say how to build it. Rules:

- **Safety class is required.** Where the feature falls in `CLAUDE.md`'s
  read-only vs writing table, whether it needs `--dry-run`, and whether it
  journals to `logs/interventions.jsonl`. Anything that writes to a speaker is a writing command.
- Diagrams are Mermaid (app flow, architecture). New screens are ASCII mockups.
  For a screen that already exists, take a real Textual SVG screenshot, commit
  it to `docs/prd/assets/`, and link it by its raw GitHub URL (the repo is public).
- Acceptance criteria are numbered and testable; the planner consumes them.
- Don't assume what is playing or what a room is called: ask `uv run twiddle status` / `rooms` (read-only).

Call `advisor` on the draft first, then:

```bash
uv run python tools/sdlc.py transition N prd-review --kind prd --body-file prd.md
```

The tool numbers the revision itself and collapses the previous one.

## 5. Bug: reproduce, then diagnose

Use `references/diagnosis-template.md`.

- Reproduce with **read-only commands and `--dry-run` only**, or a scratch
  test that touches no network or speaker. Never run a writing command against
  a real speaker, and never assume what is playing. If only a write would
  reproduce it, ask the poster on the issue (step 3) to run it, with its
  `--dry-run` output and the `snapshot`/`restore` bracket.
- Can't reproduce and the report is thin -> ask (step 3).
- Can't reproduce because the environment isn't available (a particular speaker,
  an OS) -> read the code and list candidate root causes with `file:line`, saying plainly that it is unreproduced.
- Either way, post a diagnosis: what is wrong, the evidence, the proposed fix as
  a list of tasks, and the verification plan. Splitting those into subtasks and
  slices is the planner's job.

```bash
uv run python tools/sdlc.py transition N diagnosis-review --kind diagnosis --body-file diagnosis.md
```

## Quick lane: a short brief for a small, safe change

Most issues get a PRD or a diagnosis. A change that is small and read-only gets a short
**brief** (`references/brief-template.md`), posted as the same kind of revision with
`--lane quick`. The poster signs it off with `/approve` as ever, and an agent builds it
with `work-slice N --quick`: no plan, no epic, no milestone demo, and it merges straight
into `main` through the same recorded gates (the full suite on the exact head, and Codex's
approval of it).

**Eligible** only if every one of these holds. You judge 1 to 6. The tool checks 6 (the rating)
when you post the brief with `--lane`, at `claim` and again at `merge`, and 4 and 7 (the diff)
at `merge` and `quick-gate`, and at `claim` too, from the brief's `## Files` (and on `--resume`
from the branch's and worktree's diff), but not when the brief is posted:

1. It is one slice's worth of work: a session, a few files.
2. It is read-only: it writes to no speaker and no Spotify, and needs no `--dry-run` of its own.
3. It adds no dependency (a package, a service) and waits on no other open change.
4. It touches nothing under the denied paths in `.sdlc/config.json`'s `quick` section:
   `tools/`, `.agents/`, `.claude/`, `.sdlc/`, `.github/`, `SDLC.md`, `CLAUDE.md`,
   `docs/sdlc.html`, `pyproject.toml`, `uv.lock`, and the modules that write to a speaker
   (`src/twiddle/play.py`, `relay.py`, `control_cli.py`, `alarms/`, `spotify.py` and the like;
   the list in the config is the authority). That also rules out the SDLC tooling, the skills
   and the review logic.
5. It is one of these two: a data-only catalog or venue entry (a station TOML, a line in a
   venue table: the `add-radio-station` and `add-venue-source` skills), or a bug whose
   diagnosis names a few files and a plain fix.
6. You rate it `routine` (the planner's scale: `routine`, `judgment`, `novel`). A change
   that is `judgment` or `novel` is not quick.
7. Mechanically, at `merge` (the file count and names at `claim` too): at most `max_files` files and `max_lines` changed lines (5 and
   200 today), and only files the brief's `## Files` list names.

If one fails, or you cannot tell, write the full PRD or diagnosis as usual. Say why in the
PRD's scope when the poster might expect the quick lane ("it writes to a speaker, so this
is a full PRD"). When in doubt, take the full lane: it costs time, not safety.

To post a brief:

```bash
gh issue edit N --add-label complexity:routine      # the rating: the tool refuses --lane without it
uv run python tools/sdlc.py transition N prd-review --kind prd --lane quick --body-file brief.md
# a bug:  transition N diagnosis-review --kind diagnosis --lane quick --body-file brief.md
```

- The `lane:quick` label is the tool's, added by that `transition` (and taken off by a later
  revision posted without `--lane`). Never add or remove it by hand. It must exist on the
  repo: `uv run python tools/sdlc.py bootstrap-labels` creates it (idempotent).
- Fill every section of the template. `claim --quick` refuses a brief that lacks one, and
  `merge` refuses a diff outside its `## Files` list.
- Call `advisor` on the draft first, as for a PRD.
- A brief that is later revised voids the earlier sign-off, like any revision. Once an agent
  has claimed it (`sdlc:in-progress`) the brief is frozen: `transition` refuses.
- **If it turns out to be bigger,** the move is automatic: the tool runs
  `escalate-to-full N` itself when Codex asks for changes more than `max_change_rounds` times
  (2 today), or when `merge` finds the diff touches a denied path, passes the size cap or
  leaves the `## Files` list. It posts a `lane=full` record, puts the issue back on
  `sdlc:approved` and commits the approved brief to `docs/prd/` (`commit-prd N`), and
  `/plan-issue N` plans it from there. If that was cut short, `state N` says `commit_prd`:
  rerun `uv run python tools/sdlc.py commit-prd N`. A person can also run
  `escalate-to-full N --reason "..."` (it takes `--dry-run`). An open PR and the worktree are
  left as they are.

## Debt intake: is each ticket really debt?

The debt epic (`lane:debt`) holds every open `tech-debt` issue as a sub-issue, and an agent judges each
one with no poster comment. `state D --json` lists them in `debt.intake_pending`; `uv run python
tools/sdlc.py next` names the epic. **Tech debt is work a feature, a quick change or a debt fix needed to
support itself**: a helper that should also take an empty list, a test the change should have had. It is
not a new feature, a bug that was already there and that nothing needed fixed, or a design choice. A
ticket that is not debt goes to ordinary triage (which may be yours, as this skill), where the usual
road ends with the poster's sign-off.

For each pending ticket, read it and the code it names on `main` (`git log -S`, `git grep`), look for
a duplicate among the other open `tech-debt` issues (`gh issue list --label tech-debt`), then pick one
verdict and give a reason in plain words (the repo is public: no IP, MAC, device ID or secret):

| Verdict | When | Command |
|---|---|---|
| `debt` | It is debt, and a person could build it as written | `debt-intake T --verdict debt --reason "..."` |
| `not-debt` | A feature, or a pre-existing problem: it loses `tech-debt`, leaves the epic and goes to `sdlc:triage` | `debt-intake T --verdict not-debt --reason "..."` |
| `novel` | It would need design choices, or has unknowns to investigate: the same, to triage | `debt-intake T --verdict novel --reason "..."` |
| `fixed` | Already fixed on `main`: it is closed as completed, naming the commit | `debt-intake T --verdict fixed --commit SHA --reason "..."` |
| `duplicate` | The same as another open issue: closed as a duplicate of it | `debt-intake T --verdict duplicate --of M --reason "..."` |

`--dry-run` shows the writes. A verdict cut short (the tool says so, and `state D` still says
`debt_intake`) is finished by running the same command again. A ticket that went to triage appears in
`next` as `/triage-issue T`, and is handled by the rest of this skill. Report each verdict, and end
with the `next:` line of `state D`.

## 6. A reply arrived

`state` tells you what it was:

- `action` is `record_approval` -> an authorised `/approve` on the latest revision:
  `uv run python tools/sdlc.py transition N approved --kind approval`. **A quick brief stops
  there:** it stays a comment (no `docs/prd/` file, no PR, no `merge-prd`), and the next step is
  `/work-slice N --quick`. For a PRD or a diagnosis, commit the
  approved text to `docs/prd/N-slug.md` on a branch `prd/N` cut from `origin/main`, in a
  worktree of its own (`git worktree add -b prd/N <SCRATCH>/prd origin/main`, never a
  branch switch in the primary checkout, which other sessions share). Open a small PR
  containing only that file, merge it, then `git worktree remove <SCRATCH>/prd`:
  `uv run python tools/sdlc.py merge-prd N PR` (it refuses unless the issue is approved and the PR
  changes nothing outside `docs/prd/`). Report that planning (`plan-issue`) is next.
- `/changes <text>` or free-form feedback -> revise and post the next revision (step 4/5).
- A question that needs an answer, not a new revision -> reply with
  `transition N <same state> --kind note --body-file reply.md`. Revisions and
  questions spend `max_rounds`; notes don't.
- Answers to your questions -> back to step 3: ask more or write the PRD.
- A `/approve` in `ignored_keywords` came from someone who isn't the issue author
  or a collaborator. Say so; it does not count.
- The tool refuses once `rounds` reaches `max_rounds`. Then escalate instead:
  `uv run python tools/sdlc.py transition N escalated --kind escalation --reason "..."`.

## 7. Report

End with:

- the issue number and its new state;
- whose move it is;
- the link to what you posted;
- **Next:** the `next:` line of `uv run python tools/sdlc.py state N`. Copy it; don't work it out. After
  an approval that's `/plan-issue N` (`/work-slice N --quick` for a quick brief); while the
  poster is to answer, it says so.
