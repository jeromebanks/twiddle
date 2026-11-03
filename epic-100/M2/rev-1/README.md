# #100 M2: A review eval that runs only when the review logic changes (demo, second try)

Shown from `epic/100` at `14b78ea0af6a` ("after"); `main` at `9933645` is "before".

## Where this stands

- **Sync:** `sync 100` found `epic/100` already had `main`; the full suite passed on `14b78ea0af6a` (6022 tests) and was recorded.
- **Codex's milestone review, round 2 of 5:** `approve` on `14b78ea0af6a` (recorded on #100). Round 1, on `6b2632dadc8e`, asked for changes; fix slices F1, F2 and F3 (below) answered it, and round 2 confirmed both earlier findings fixed. It also reviewed C1 and T5 as pull requests for the first time (they had merged without a review of their own). One non-blocking finding was filed as tech debt: #340.
- **Review eval:** passed for the review logic `30de46113c2a` on codex `0.166.0` ([result](https://github.com/jeromebanks/twiddle/issues/100#issuecomment-6616710977)). The sync changed nothing, so the fingerprint didn't move and the eval wasn't run again.

## What was built

| key | issue | PR | what |
|---|---|---|---|
| T5 | #158 | #164 | the review logic's fingerprint (every file that decides how a review is run and judged), a rules file, and a ledger of eval results on an issue |
| T7 | #159 | #178 | `codex-eval`: five fixtures (four planted bugs, one clean change), a scorer, the run, `--status`, `--force`, `--defer` |
| C1 | #157 | #161 | `codex-review` refuses a round that couldn't be recorded (the head already approved, the rounds spent) before Codex starts (#134) |
| T6 | #160 | #311 | `merge` refuses a slice PR whose review logic has no passing (or deferred) eval for the current codex |
| F1 | #326 | #330 | fix: a quick change whose brief was re-approved on an unchanged head gets its fresh review |
| F2 | #327 | #335 | fix: what a debt or quick round is handed lives in fingerprinted `codex_review.py`, and `merge`'s gate-code check covers it |
| F3 | #328 | #339 | fix: the eval gate holds for quick and debt PRs too, with no deferral |

## The steps as run

All from a detached checkout of `14b78ea0af6a`, read-only. Each picture's `$` line is its window title; the exact command is listed here.

1. **`codex-eval --status` names the fingerprint, the codex version and the recorded result.**
   - `before-status.svg`: `uv run python tools/sdlc.py codex-eval --status` on `main`: no such command.
   - `after-status.svg`: the same on `epic/100`: `30de46113c2a`, 38 inputs, skills suppressed; codex 0.166.0; passed.
2. **`codex-eval` against real Codex finds every planted bug and approves the clean fixture, and posts its result.**
   - `eval-run.svg`: `cat eval-run.log`, the log of the run that recorded the pass (3 Nov, 14:09-14:12, a plain `codex-eval` on a pair with no result yet, during F2's build; only its `scratch:` line, a local path, was removed). Same fingerprint and codex version as this head, so it is this head's eval.
   - `eval-force-dry.svg`: `uv run python tools/sdlc.py codex-eval --force --dry-run`: what `--force` would run. `--force` itself wasn't run: it posts a second result to the ledger, and the demo only runs read-only steps.
3. **A plain `codex-eval` then says the pair already passed and runs nothing.**
   - `eval-again.svg`: `uv run python tools/sdlc.py codex-eval`, run only after `--status` in the same checkout said `passed`.
4. **The session logs: the pinned model, and nothing loaded from `~/.codex`.**
   - `session-logs.svg`: `python3 -I sessioncheck.py ...`, a small reader of each run's session log (the `turn_context`'s model and effort; the full `world_state`'s `skills`, `host_skills` and `agents_md`; a count of paths under the real `~/.codex`).
   - The five eval runs (M2's tool): `gpt-6.1-sol`, effort high, **no skills offered**, no instruction file, nothing from `~/.codex`.
   - The milestone review round 2 ran on `main`'s tool, as every milestone review does until the milestone ships, and `main` doesn't have M2's skills suppression yet: Codex's built-in skills and the repo's committed `.agents/skills` were **offered** (none opened as instructions; it read `.agents/skills` only as part of the diff it reviewed). No instruction file, nothing from `~/.codex`. Once M2 ships, every review runs with skills off, and a run whose log shows skills offered saves no report (`test_a_run_offered_skills_or_with_a_log_nobody_knows_saves_nothing`).
5. **`codex-review` warns about a missing eval; `merge` refuses a review-logic change without one; the codex upgrade case; deferrals.** Shown by named offline tests, which build exactly the saved bundles the plan describes:
   - `tests-merge-gate.svg`: `uv run pytest -p no:cacheprovider -v --no-header` of `test_codex_review_warns_when_the_pair_has_no_pass_and_still_runs`, `test_a_prompt_change_with_no_eval_is_refused_naming_codex_eval_and_a_pass_lets_it_through`, `test_a_pr_on_a_pair_that_passed_is_unaffected_until_codex_is_upgraded`, `test_a_deferral_lets_the_merge_through_owed_and_a_fail_blocks`, `test_merge_from_a_saved_bundle_never_merges` (all in `tests/test_sdlc_eval.py`).
   - `tests-lanes.svg`: the same for F2 and F3: `tests/test_sdlc_main_eval.py`'s quick and debt cases (a review-logic change needs a pass and a deferral doesn't count; an unchanged one waits only after a codex upgrade; the slice lane still takes a deferral) and `test_what_a_debt_or_quick_round_is_handed_is_fingerprinted`.
6. **`codex-review` refuses, before Codex starts, a round on an approved head or with its rounds spent (#134); and a re-approved quick brief gets its review (F1).**
   - `tests-review-refusals.svg`: `test_a_pr_round_that_could_not_be_recorded_or_would_change_nothing_never_starts_codex`, `test_an_approval_against_an_earlier_brief_does_not_stand_on_an_unchanged_head`, `test_pr_review_refuses_a_brief_reapproved_since_the_review` (`tests/test_sdlc_review.py`).

No step writes to a speaker; nothing here touches one.

## What shipping M2 changes

From the moment M2 is on `main`:

- **Every merge needs a passing eval for the current (fingerprint, codex version) pair**: slice PRs into `epic/N`, quick changes and debt PRs into `main`.
- A **slice** may still go through on a deferral (`codex-eval --defer --reason ...`, when Codex can't run); the eval is then owed before its milestone's demo. **Quick and debt PRs can't defer**: they have no demo to owe it to, so they wait for a pass.
- **After any codex upgrade, run `codex-eval` before the next merge.** An unchanged fingerprint on a new codex has no pass on record, so every lane waits until one is posted (about 3-4 minutes; it runs only when the pair has no result).
- A PR that changes the review logic (a review prompt, the rules, `codex_review.py`, `codex_eval.py`, the eval fixtures) needs the eval run on its own head.
- Every Codex review runs with skills off, and a run whose session log shows skills offered or an instruction file from outside the checkout saves nothing.

## What changed from the plan

- The `--from-file` steps (codex-review's warning, merge's refusal then pass, #134's refusals) are shown by their offline tests rather than hand-built bundles: the tests build those bundles and assert the refusal text.
- `codex-eval --force` wasn't re-run (it would post a duplicate result); the run that recorded the pass is shown instead, with `--force --dry-run`.
- F1, F2 and F3 were added after round 1 of the milestone review and the plan review (plan rev 11): the quick and debt lanes landed on `main` while M2 was being built, and the eval gate and the fingerprint had to cover them too.

## Known gaps

- #340 (tech debt, from round 2): two separate lookups find the merged PRD a plan is held to, one in `codex_review.py` and one in `sdlc.py`. They agree today.
- Until M2 ships, milestone reviews run on `main`'s tool, which still offers Codex its skills (step 4).
