# #243 M2: Process fixes that cost the poster time (demo, rev 2)

Shown from `epic/243` at `24bde6b`, which already contains `main` (`a67d11a`).

## Why there is a rev 2

Rev 1 showed T11-T14 on `fe9a6eb`, and you accepted it. Before shipping, Codex reviewed the whole milestone on
`fe9a6eb` and found two blocking problems
([review 1](https://github.com/jeromebanks/twiddle/issues/243#issuecomment-6646524157)). Both were confirmed
against the code and recorded as demo findings
([demo-changes](https://github.com/jeromebanks/twiddle/issues/243#issuecomment-6646610658)). Plan rev 17 added
two fix slices, F4 and F5. Both are merged, so this demo shows the whole milestone again with the fixes in.

There are two kinds of "before":

- **F4 and F5:** `fe9a6eb`, the code rev 1 showed and Codex reviewed. On `main`, T12 doesn't exist yet, so `main`
  would show F5's "before" for the wrong reason.
- **T11-T14:** `main` (`a67d11a`). These were retaken on today's code. They show the same results as rev 1, so
  F4 and F5 changed none of them.

Each pair was run back to back on the same saved input.

## What was built

| Slice | What it does | PR |
|---|---|---|
| T11 | While an epic is being built, a Codex review of a plan amendment reads `epic/N`, not `main` (#216) | #318 |
| T12 | A person's `/approve` settles a plan whose Codex rounds are spent: from `needs-info` and from `escalated` | #319 |
| T13 | The poster's `/changes` on an epic being built, with no demo up, is recorded and planned | #317 |
| T14 | `plan-create` never stops silently, and `state` offers the rerun | #337 |
| **F4** | `plan-create` records a person's settlement only as read from GitHub. With `--from-file` it is a dry run or nothing (Codex finding 1) | #357 |
| **F5** | When the poster sends `/changes` and then `/approve` on a plan change whose rounds are spent, the later `/approve` wins. `/approve` then `/changes` still records the changes (Codex finding 2) | #358 |

## Before the demo

- Codex review: **none owed**. `demo-status` lists no slice owed a milestone review. F4 and F5 are judgment slices,
  each approved by Codex on its own PR. The whole milestone gets a second Codex review at ship time, after `/approve`.
- The epic's last recorded suite run is on `e64a73c`, `epic/243` with `main` merged in: 6020 passed. At `24bde6b`,
  the sdlc test files (`test_sdlc_plan.py`, `test_sdlc_demo.py`, `test_sdlc_invariants.py`, `test_sdlc_tool.py`,
  `test_sdlc_epic.py`) gave 2516 passed.

## The input

All the `state` pictures read saved copies of real issues (`state --from-file`, offline: nothing is sent to GitHub).
Rev 1's files are reused unchanged (see rev 1's README for what each one is). F5 adds two:

| File | What it is |
|---|---|
| `100-changes-then-approve.json` | #100 on Oct 6 at 23:48, when its M2 plan amendment (rev 8) spent its 3rd Codex round. M1 had already shipped, which is the case Codex described. Three edits: the escalation that followed was left out; the label was set to `in-progress`, as for any epic being built; and a `/changes put the review eval in its own milestone` from the poster was added at 23:50. The `/approve` after it is the poster's real reply, word for word. |
| `100-approve-then-changes.json` | The same, with the two replies in the other order. |

## The steps, as run

Nothing here touched a speaker, and nothing was written to GitHub except this demo.

1. **F5, `/changes` then `/approve`.** `state` on `100-changes-then-approve.json`. Before (`fe9a6eb`): the plan shows
   as settled (`settled: /approve by jeromebanks ...`), but the action is `record_changes`, so the next step would
   reset the rounds and plan again. After: `action: create_plan_issues`, `next: /plan-issue 100`.
   `f5-changes-then-approve-before.svg`, `f5-changes-then-approve-after.svg`
2. **F5, `/approve` then `/changes`.** `state` on `100-approve-then-changes.json`: `record_changes` both before and
   after. The later reply still wins. `f5-approve-then-changes-before.svg`, `f5-approve-then-changes-after.svg`
3. **F4, a settlement from a saved file.** At `after`, `plan-create 263 --from-file 263-approve.json` (no
   `--dry-run`) exits 1 before it posts anything:

   ```
   error: `plan-create` records a person's decision, which is read from GitHub, never from a file: --from-file goes with --dry-run only (nothing was posted)
   ```

   It was run with a stand-in `gh` on `PATH` that fails every call and logs it, and with a stand-in `git` that
   refuses anything that writes. The log was empty. The same command at `fe9a6eb` went straight to "create
   milestone '#263 M1: ...'" and tried to POST it. The stand-in `gh` stopped it, so nothing reached GitHub. That
   run isn't pictured: it is a write. With `--dry-run`, the creation is still described, `(settled by @jeromebanks)`
   included: `f4-dry-run.svg`.
4. **F4 and F5's own tests**, offline. These include the case that can't be shown without writing to GitHub: once
   `plan-create` records the settled revision, the superseded `/changes` doesn't come back, and a fresh `/changes`
   after that is still recorded. 8 passed: `f4-f5-tests.svg`
5. **T12, `/approve` on a plan question** (#263): before `replan`, after `create_plan_issues` with a `settled:` line.
   `t12-needs-info-before.svg`, `t12-needs-info-after.svg`
6. **T12, `/approve` on an escalated amendment** (#100, the real reply): before `turn: human`, after
   `create_plan_issues`. `t12-escalated-before.svg`, `t12-escalated-after.svg`
7. **T13, `/changes` with no demo** (#243): before `work_slices`, after `record_changes`. `t13-changes-before.svg`,
   `t13-changes-after.svg`
8. **T14, a creation cut short** (#263): before `next: /plan-issue 263`, after `next: plan-create 263 ... a rerun
   finishes it`. `t14-cut-short-before.svg`, `t14-cut-short-after.svg`. T14's tests: 9 passed, `t14-tests.svg`
9. **T11, a plan amendment reviewed against the epic's branch** (#243's rev 13 amendment): before "Codex reads main",
   after "Codex reads epic/243 at 24bde6beec20 (the epic's branch, ahead of main)". The prompt is filtered to those
   lines. `t11-plan-base-before.svg`, `t11-plan-base-after.svg`

## What changed from the plan

- F4 and F5 were added by plan rev 17, after Codex's ship review. Neither has a Demo section ("internal"). They are
  shown here anyway, because they change what you'd see.
- For F5, Codex's plan review also asked for a test that a fresh `/changes` after the settled record is still
  recorded. It was added (`test_the_superseded_changes_is_consumed_once_the_settled_revision_is_created`).

## Known gaps

- F5's input is #100 with an invented `/changes` added. The only real reply in it is the `/approve`.
- `()` after "replies since" and "asked for changes" marks the added reply, which has no GitHub link because it was
  never posted.
- #338 (tech debt, filed at rev 1): on an escalation the new rule doesn't lift, `state` still calls the `/approve`
  "settles the plan" while also saying it needs a person.
- #216 is closed with the ship's commit once M2 ships.
