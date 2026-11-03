# #243 M2: Process fixes that cost the poster time (demo)

Shown from `epic/243` at `c8660ee` (after `sync 243` brought `main` in), with `main` (`9933645`) as the "before".
Every picture is a pair run back to back: the same command on the same input, once with `main`'s tools and once
with the epic's.

## What was built

| Slice | What it does | PR |
|---|---|---|
| T11 | While an epic is being built, a Codex review of a plan amendment reads `epic/N` (the code already built), not `main` (#216) | #318 |
| T12 | A person's `/approve` settles a plan whose Codex rounds are spent: from `needs-info` (the poster's question) and from `escalated` (an amendment handed to a person) | #319 |
| T13 | The poster's `/changes` on an epic being built, with no demo up, is recorded and planned instead of ignored | #317 |
| T14 | `plan-create` never stops silently: it says what it made, what's missing and the command to rerun; `state` offers that rerun | #337 |

## Before the demo

- `sync 243`: `epic/243` was 3 commits behind `main`. The tool merged `main` in (`c8660ee`), ran the full suite
  there (5930 passed), recorded the run on the epic and pushed.
- Codex review: **none owed**. All four slices are judgment slices and each was approved by Codex on its own PR;
  `demo-status` lists no slice owed a milestone review. The milestone's whole-diff review runs at ship time,
  after `/approve`.

## The input: real issue history, cut at a real moment

The `state` pictures read saved copies of real issues (`state --from-file`, offline: nothing is sent to GitHub),
cut off at a moment that really happened. Where a reply is added to show what the new rule does, it says so.

| File | What it is |
|---|---|
| `263-approve.json` | #263 up to its **second** "the plan needs one more pass" question (rounds spent twice by then), with the label it had (`needs-info`). Added: a reply `/approve` from the poster. In reality the poster answered **A** (another set of rounds), as they did all three times #263 asked. |
| `100-escalated.json` | #100 on Oct 6: its M2 plan amendment spent its 3 Codex rounds, was handed to a person (escalated), and the poster replied `/approve I think Codex is being extra cauthios. ...`. Real, word for word. One edit: the escalation's hidden marker is written the way today's tool writes it (`phase=plan`); the October one predates that field. Without it the new rule (rightly) leaves an older escalation to a person, as `100-escalated-as-it-was.json` showed (`turn: human`). |
| `243-changes.json` | #243 today (M2 built, no demo up). Added: a `/changes` reply from the poster. |
| `263-cut-short.json` | #263 at 19:05 today, the moment after `plan-create` made its 3 issues and before it posted its `plan-created` record, with the label it had then (`approved`) and the 3 issues as GitHub lists them. |
| `243-amendment.json` | #243 on Nov 2 at 22:35, when the F2/F3 amendment (plan rev 13) was posted for its first Codex review. M1's debt lane was then only on `epic/243`; it reached `main` on Nov 3. |

## The steps, as run

All read-only. Nothing touched a speaker, and nothing was written to GitHub except this demo.

1. **T11, a plan amendment is reviewed against the epic's branch.** `codex-review --plan 243 --dry-run` on
   `243-amendment.json` (the prompt is filtered to its title and base lines). Before: "Codex reads main"; that is
   how rev 13 was really reviewed (and approved) on Nov 2, against a `main` that didn't have M1's debt lane yet, though the
   amendment's fix slices were for that code. (Today's `main` has M1, so the picture shows the branch chosen, not the harm.) After: "Codex reads epic/243 ...
   (the epic's branch, ahead of main)", and the prompt tells Codex so. `t11-plan-base-before.svg`,
   `t11-plan-base-after.svg`
2. **T12, `/approve` on a plan question.** `state` on `263-approve.json`. Before: `/approve ... (not valid in this
   state)`, and the next step is another planning pass. After: `/approve ... (settles the plan)`,
   `action: create_plan_issues`, and a `settled:` line naming who settled which plan revision.
   `t12-needs-info-before.svg`, `t12-needs-info-after.svg`
3. **T12, `/approve` on an escalated amendment.** `state` on `100-escalated.json`. Before: `turn: human`,
   "needs a human" (in October this took a hand-written maintainer override). After: `turn: agent`,
   `create_plan_issues`, settled by the poster's `/approve`. `t12-escalated-before.svg`, `t12-escalated-after.svg`
4. **T13, `/changes` with no demo up.** `state` on `243-changes.json`. Before: the `/changes` is shown as a decision
   but the epic carries on building (`work_slices`). After: `action: record_changes`, next
   `demo-changes 243` and then `/plan-issue 243`. `t13-changes-before.svg`, `t13-changes-after.svg`
5. **T14, a creation cut short.** `state` on `263-cut-short.json`. Before: `next: /plan-issue 263`, which would
   start planning again although the issues exist. After: `next: plan-create 263`: "3 issue(s) of the plan exist
   but there is no plan-created record: plan-create was cut short; a rerun finishes it".
   `t14-cut-short-before.svg`, `t14-cut-short-after.svg`
6. **T14, what `plan-create` itself prints when it stops.** It can't be stopped partway for real without writing
   to GitHub, so this is T14's own offline tests, which inject GitHub failures at each point (a failed write, a
   read-back that lags, a failed label move, from `needs-info` and from `escalated`) and check the message names
   what was made, what is missing and the rerun, and that the rerun makes no duplicate. 9 passed. `t14-tests.svg`

Also at `after`: the four slices' test files (`test_sdlc_plan.py`, `test_sdlc_demo.py`, `test_sdlc_invariants.py`,
`test_sdlc_tool.py`): 2428 passed.

## What changed from the plan

- Nothing in behaviour. The demo uses real issue history (#263, #100, #243) instead of made-up fixtures, as the
  milestone's steps allowed ("`state` on fixtures").

## Known gaps

- The read-back failures that actually happened today on #243, #263 and #100 were transient (most likely GitHub listing new
  issues a few seconds late, which is what T14 now waits for). They can't be replayed offline; step 5 shows what `state` now says at that moment,
  and step 6 what `plan-create` prints.
- An escalation recorded before this milestone (no `phase=plan` in its marker) is still left to a person. That is
  deliberate: only a plan escalation can be settled by `/approve`.
- `()` after "replies since" in steps 2 and 4, and "(recorded by plan-create)" on step 2's `settled:` line, are the added
  reply having no GitHub link, since it was never posted. `plan-create` did not run.
- In step 2 the `next:` line reads `/plan-issue 263` both before and after; what changed is the `action:` line
  (`replan` before, `create_plan_issues` after), and `/plan-issue` then creates the issues.
- An escalation the new rule does not lift (an older one with no `phase=plan`) still prints `/approve ... (settles the
  plan)` and a `settled:` line while saying `turn: human` (`100-escalated-as-it-was.json` at `after`). Confusing,
  not wrong in what it does; to be filed as tech debt.
- After M2 ships, #216 is closed with the ship's commit.
