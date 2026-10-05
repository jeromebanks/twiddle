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
`sdlc:approved`; planning, slices, PRs and demos are later skills.

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

## 6. A reply arrived

`state` tells you what it was:

- `action` is `record_approval` -> an authorised `/approve` on the latest revision:
  `uv run python tools/sdlc.py transition N approved --kind approval`. Then commit the
  approved text to `docs/prd/N-slug.md` on a branch cut from `main`, open a small PR
  containing only that file, and merge it:
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
  an approval that's `/plan-issue N`; while the poster is to answer, it says so.
