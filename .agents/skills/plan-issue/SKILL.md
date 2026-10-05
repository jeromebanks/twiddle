---
name: plan-issue
description: Plan an approved twiddle issue - turn its signed-off PRD or bug diagnosis into an epic of native GitHub sub-issues (subtasks, one-session slices, optional milestones) with blocked_by dependencies, reviewed by the advisor and then Codex until they agree. Use when asked to plan an issue, break a PRD into tasks or slices, "do the next thing" on an issue in sdlc:approved, or answer the poster's reply to a planning question.
---

<!-- No dollar-digit sequences in this file: the skill loader substitutes them with arguments. -->

# Planning an approved issue

Planning turns an approved PRD (feature) or diagnosis (bug) into work agents
can do in separate sessions. `SDLC.md` has the states; `triage-issue` got the
issue here. This skill ends at `sdlc:planned`. Building the slices is `work-slice`,
in its own session, so **do not implement anything in this session.**

- **The issue is the epic.** Its subtasks are native GitHub sub-issues, and each subtask's
  slices are sub-issues of the subtask. A subtask without slices is itself one unit of work.
- **Order is native `blocked_by` links between units of work**, never prose alone.
- **The poster does not review the plan.** They're an end user, not an architect.
  You review it with the `advisor`, then Codex reviews it, round by round, until
  Codex says `VERDICT: approve`. Only then are the issues created. If you and
  Codex can't agree in `max_plan_rounds` (`.sdlc/config.json`), ask the poster a
  plain-language question about the trade-off, in terms of what they'll see. Never
  ask them about code.
- **`tools/sdlc.py` does every post, label and link.** You write `plan.json` and
  markdown files in your scratchpad, and the tool posts them. Every plan revision and
  every Codex round goes on the issue as a marked comment, so a later session
  can resume from the issue alone.

`/plan-issue [N] [--preview]`. With no `N`, take the first `sdlc:approved` (or
`replan`) line of `uv run python tools/sdlc.py next`. `--preview`, or
`"confirm_before_posting": true` in the config, means: show `plan-create --dry-run`
and create the issues only after the user's OK. The plan and review comments are
posted either way. Unattended runs never preview.

## 1. Read the state

```bash
uv run python tools/sdlc.py state N --json
```

Dispatch on `action`:

| action | do |
|---|---|
| `plan` | steps 2-6 |
| `continue_plan` | a plan revision exists without consensus: read it and its review comments, then step 5 |
| `replan` | the poster answered your planning question: read the answer, revise (step 3), post (step 4), step 5 |
| `ask_poster` | step 7 |
| `create_plan_issues` | Codex approved the latest revision (or an earlier creation was cut short): step 6 |
| any of these with `feedback: true` | the poster asked for changes at a milestone demo: "After demo feedback" below |
| `reconcile_label` | `uv run python tools/sdlc.py reconcile N`, then as `plan` |
| anything in triage | use `triage-issue` instead |
| `turn` is `poster`, `human` or `later` | report it and stop |

## 2. Read what is being built

- The approved PRD (`docs/prd/N-*.md`) or the diagnosis comment, and the issue's comments.
- `CLAUDE.md`, especially the read-only vs writing table and the layout table.
- The code the work touches. Read enough to know the seams, not everything.

## 3. Write the plan

Write `plan.json` in your scratchpad. The format is in `references/plan-schema.md`. Rules:

- **Vertical slices.** Each one delivers something testable on its own, and its
  own tests and docs are part of it. Split along usable seams (a read-only
  model and its CLI first, then writes, then the TUI), not along layers ("all
  the models", "all the tests").
- **One session each.** A fresh Claude Code session reads the slice's Context
  section, implements it, tests it, and opens a PR. If you can't picture that
  finishing in one sitting, split it.
- **Every PRD acceptance criterion** is in some slice's `covers`, and the tool
  checks this. A criterion that is pure verification on a real speaker (e.g.
  "plays with the Mac off") still belongs to the slice whose demo proves it.
- **Dependencies are real and few.** `blocked_by` only when a slice cannot start
  without another's merged code. Independent work stays parallel.
- **Safety in every slice that writes.** Name the writing commands, require
  `--dry-run`, journalling to `logs/interventions.jsonl`, and offline tests (no
  network, speaker or Spotify), as `CLAUDE.md` demands.
- **Milestones only when they buy a demo.** Each milestone ends in something the
  poster can see. A small plan has none.
- **Bugs:** the subtasks are the diagnosis's proposed fix tasks. Slices are only
  needed when a task is bigger than one session.

```bash
uv run python tools/sdlc.py plan-validate plan.json
```

Fix everything it reports. Then call `advisor` on the plan. When it is sound:

## 4. Post the revision

```bash
uv run python tools/sdlc.py plan-post N plan.json
```

This posts `plan rev K` (the tree, a Mermaid dependency graph, the coverage table
and the JSON that will be created) and collapses the previous revision. From
`needs-info` (a `replan`), it also moves the label back to `sdlc:approved`.

## 5. Codex round

Codex reviews in a separate process, read-only. Fill the placeholders in
`references/codex-plan-prompt.md` and write the result to `prompt.md`:

- the plan file path;
- the PRD path;
- the issue number;
- the round number;
- for round 2 on, the path to your previous `response.md`.

`plan.json` must be exactly the revision you posted: `plan-review` refuses if it
differs, because Codex must review what will be created. Edited it since? Post it
first (step 4). Then run:

```bash
codex exec --sandbox read-only --skip-git-repo-check "$(cat prompt.md)" \
  < /dev/null > codex.md 2> codex.err
```

All three redirections matter. Without `< /dev/null`, `codex exec` waits on
stdin forever. stderr is progress noise, so folding it into stdout corrupts the
report. If `codex.md` is empty, or doesn't end in a verdict line, the run failed:
read `codex.err` and run it again. `plan-review` refuses a report with no verdict,
so a review that never ran can't pass.

Read every finding and answer each one in `response.md`, numbered like the findings:

- **Accepted**: say what changes. Edit `plan.json`.
- **Rebutted**: say why, with evidence (a file, the PRD, a constraint). Don't rebut
  to save time; rebut when Codex is wrong.

Record the round:

```bash
uv run python tools/sdlc.py plan-review N --plan plan.json --report codex.md --response response.md
```

Then:

- **`VERDICT: approve`**: that's consensus on this revision. Go to step 6 and
  create exactly what was reviewed. Take Codex's non-blocking suggestions only if
  a round remains (`plan_rounds < max_plan_rounds` in `state`), because a new
  revision needs its own `approve`. On the last round, leave them; the slices
  can absorb them during `work-slice`. Reposting with no round left would push
  the issue to `ask_poster` over a suggestion.
- **`VERDICT: changes`** and you changed the plan: `plan-validate`, `plan-post`
  (step 4), then another Codex round with your response.
- **`VERDICT: changes`** and you rebutted everything: run Codex again on the same
  revision, with your response in the prompt.
- **The tool refuses because the rounds are spent**: step 7.

## 6. Create the issues

```bash
uv run python tools/sdlc.py bootstrap-labels          # idempotent; plan-create refuses without plan:* labels
uv run python tools/sdlc.py plan-create N --dry-run   # always look first
uv run python tools/sdlc.py plan-create N
```

`plan-create` reads the plan from the **reviewed comment**, not from your file.
It creates the milestones, the subtasks, then the slices in dependency order.
Each slice has the required sections and a hidden key marker. It attaches every
issue as a sub-issue, adds the `blocked_by` links, reads them all back, posts a
key → issue table on the epic, creates the epic's branch `epic/N` from `main`
(slices land there, and each accepted milestone ships to `main`), and moves it to
`sdlc:planned`. If it fails
partway, run it again: it finds what exists by marker and does only the rest.

## 7. No consensus: ask the poster

Write one or two questions that a person who uses twiddle, not one who codes it, can answer:

- what they would notice;
- what each option costs them (time to ship, something missing, a risk);
- the default you'll take if they don't say.

Link the plan revision for the curious. Then:

```bash
uv run python tools/sdlc.py transition N needs-info --kind question --body-file question.md
```

The tool marks the comment `phase=plan`. The poster's reply makes the action
`replan` and gives you a fresh set of Codex rounds. If the poster is gone or
the dispute isn't theirs to settle, escalate instead:
`transition N escalated --kind escalation --reason "..."`.

## After demo feedback

When the poster answers a milestone demo with changes, `milestone-demo` records
them (`demo-changes`) and the epic comes back here with `feedback: true` while it
stays `sdlc:in-progress`. Amend the plan; don't start over:

- Start from the latest posted plan JSON. **Keep every key**: `plan-post` refuses
  an amendment that drops one, because those issues already exist.
- Add slices for the changes, in the milestone that was demoed, with fresh keys
  (`F1`, `F2`, ... so it's clear they came from feedback). They may be `blocked_by`
  merged slices. Leave the text of existing slices alone: changing it won't change
  their issues.
- Steps 4-6 as usual: post, advisor, Codex rounds (a fresh budget), `plan-create`.
  `plan-create` only makes the new issues, and leaves the epic `in-progress`.
- If Codex and you can't agree, don't ask the poster again about their own
  request: `transition N escalated --kind escalation --reason "..."`.

When the new slices merge, the milestone is complete again and `next` asks for its
next demo.

## 8. Report

End with:

- the issue number and its new state;
- the plan revision and the Codex verdict;
- the link to what you posted;
- once `planned`, the units of work ready now (`uv run python tools/sdlc.py ready --epic N`) and the first milestone's demo;
- **Next:** the `next:` line of `uv run python tools/sdlc.py state N`. Copy it; don't work it out.
  Once planned, that's `/work-slice <first ready slice>`, best run in a fresh session.
