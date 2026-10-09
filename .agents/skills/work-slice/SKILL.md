---
name: work-slice
description: Build one ready slice of a planned twiddle epic, start to finish - claim it, work in its own git worktree, implement only what the slice issue says with offline tests, open a PR, record the full test run, get Codex to approve the exact head through review rounds, then squash-merge it into the epic's branch (epic/N, never main) and clean up. Use when asked to work a slice or issue from an epic, "build the next slice", "do the next thing" on an epic in sdlc:planned or sdlc:in-progress, or to resume a slice in review.
---

<!-- No dollar-digit sequences in this file: the skill loader substitutes them with arguments. -->

# Building one slice

A slice is one unit of work made by `plan-issue`: an issue labelled `plan:slice`,
a sub-issue of its subtask (or of the epic itself). It's sized for one session,
and its sections (Outcome, Scope, Acceptance criteria, Validation, Demo, Non-goals,
Context) are the whole brief. `SDLC.md` explains how the epic got here.

- **The slice issue is the brief.** Read what its Context section points at, plus
  `CLAUDE.md`. Don't load the PRD, the epic or other slices unless Context
  names them. Your context window is for implementation.
- **`tools/sdlc.py` owns every claim, record, label and merge.** You write code,
  tests, and markdown files for the PR and the reviews. The tool posts them and
  binds every test run and every review to a head SHA, so a new commit voids both.
- **Codex has to approve the exact head you merge.** You run it, but you never
  author its verdict. If Codex can't be run, leave the PR open and report that.
- **Slices land on the epic's branch, `epic/E`, never on `main`.** `main` only gets
  a milestone after the poster accepts its demo (`ship`, in `milestone-demo`). Your
  base is always `origin/epic/E`, written `<BASE>` below.
- **You merge.** `merge` is the gate. Nobody reviews individual PRs by hand: a human
  sees the result at the milestone demo, so the gate and the reviews must be honest.

`/work-slice [N]`. With no `N`, run `uv run python tools/sdlc.py state <epic>` (or
`next`) and take the `/work-slice N` it suggests. That line names the model the planner's
rating maps to, e.g. `/work-slice 29 (sonnet: routine)`: a coordinator runs the
slice on that model.

## 1. Check

```bash
uv run python tools/sdlc.py slice-status N
uv run python tools/sdlc.py slice-check N          # add --resume when it says claimed / in-review
```

- `ready`: carry on.
- `claimed` or `in-review`: this is a resume. Use `claim N --resume` below, then pick
  up where the PR and its review comments left off.
- `escalated`, and you were started as its retry on a stronger model: `claim N --retry
  --model <you>` below, then pick up where the PR left off. Otherwise:
- `blocked`, `escalated` or closed: report it and the `next` line, then stop.
- `slice-check` says a milestone demo or release comes first, a later milestone waits, or the
  cleanup pass comes first: report it and stop. A slice you already claimed can still be resumed.
- `slice-check` complains about missing sections, or the brief can't be built as
  written: don't guess. Report it. Splitting or fixing a slice is a planning job.

## 2. Claim and move into the worktree

```bash
uv run python tools/sdlc.py claim N --model <the model you are>   # or: claim N --resume
```

It prints `WORKTREE=`, `PRIMARY=`, `BRANCH=slice/N` and `BASE=origin/epic/E`. Write `BASE`
down too. Its other effects:

- the claim is posted on the slice (with your model, so it shows over time whether
  the ratings held up), and you're assigned to it;
- on the first claim, the epic moves from `planned` to `in-progress`.

If it says origin has no `epic/E`, the epic was planned before epic branches:
run `uv run python tools/sdlc.py epic-branch E`, then claim again.

**Write `WORKTREE` and `PRIMARY` down as literal absolute paths.** Shell variables
don't survive between tool calls, so type the paths out every time:

- **Every file you Read, Edit or Write must have a path that starts with `WORKTREE`.**
  The same file under `PRIMARY` is the main checkout, on `main`. Editing it changes
  the wrong tree, and `test-record` then tests code that doesn't have your change.
- **Every shell command that touches the code runs in `WORKTREE`**: `cd <WORKTREE> && ...`, or `git -C <WORKTREE>`.
  Check `git rev-parse --abbrev-ref HEAD` says `slice/N` before you commit. Diff
  and rebase against `<BASE>` (`origin/epic/E`), never `main`.
- **Scratch files never go in the worktree.** This means the PR body, the slice
  brief, the Codex prompt, its report and stderr, and your response. Put them in
  your session's scratchpad directory (or a `mktemp -d`), called `<SCRATCH>` below, by absolute path. Untracked
  files make the tree dirty, and `test-record` then refuses. Never commit them.

## 3. Plan

Read the slice's Context section and `CLAUDE.md`, especially the read-only vs
writing table. Then write a short plan with one line per acceptance checkbox:
what you'll change, and the test that proves it. Have the `advisor` check the plan.

## 4. Implement only the slice

- Its Scope and Non-goals bind you. Leave adjacent improvements for later and mention them in the report.
- Match the surrounding code: its comment density, naming and idiom. Update the
  docs this slice owns, which its acceptance criteria name.
- **Safety:**
  - Tests are offline: no network, speaker or Spotify.
  - Never run a writing command against a real speaker. Read-only commands and
    `--dry-run` are fine; firing anything for real belongs to the milestone demo.
  - Every new writing command takes `--dry-run` and journals to `logs/interventions.jsonl`.
- **If it won't fit one session**, keep the coherent part working and tested, stop,
  and report what's left. Don't create follow-up slices yourself.

## 5. Test, commit, push

Iterate on the slice's own Validation commands, then run the full `uv run pytest`.
Commit with the repo's trailers and push:

```bash
git push -u origin slice/N
```

## 6. Self-review

First work through this checklist on your own diff:

- every bound (a timeout, a limit, a retry count) is finite and validated;
- shutdown and cancel ordering: what is stopped first, and what happens to work in flight;
- every error path returns a status, never silently drops the request;
- tests listen on loopback only, and stub anything that opens a socket to a fake address.

Then have the `advisor` review the committed diff (`git diff <BASE>...HEAD`).
Fix what it finds, rerun the affected tests, then commit and push.

## 7. Open the PR and record the tests

The PR body must contain **exactly one** `Closes #N`. Include:

- `Epic #E · <key>`;
- the slice's acceptance checklist, ticked where you proved it;
- the Validation commands you ran;
- any adjacent issues you noticed but left alone.

```bash
gh pr create --base epic/E --head slice/N --title "<key>: <slice title>" --body-file <SCRATCH>/pr.md
```

If this slice changed the review logic (a prompt, `review-rules.md`, the `codex` settings, `tools/codex_review.py`,
`tools/codex_eval.py` or anything under `tests/codex_eval/`; `SDLC.md` lists the fingerprint's inputs), run the
eval once the code is final, before `test-record` and `codex-review`: a later commit to those inputs voids its result, so
run it again whenever self-review or a Codex round makes you change one.
Run the Bash call in the background (`run_in_background`); a full eval takes 10-15 minutes.

```bash
uv run python tools/sdlc.py codex-eval
```

It posts its result, and runs nothing when this fingerprint and `codex` version already passed. A `fail`
means Codex missed a planted bug or flagged the clean change: fix the review logic, not the fixture, unless the
fixture is what is wrong. Exit 3 means Codex can't run: carry on, and `review-defer` applies as in §8.

Then record the tests:

```bash
cd <WORKTREE> && uv run python tools/sdlc.py test-record PR    # runs the full suite itself
```

`test-record` refuses if the tree is dirty or `HEAD` isn't the PR's head. It runs
`uv run pytest -q` itself and records the result on the PR, so you never report a
test run by hand. Run it again after every new commit. **Give the Bash call
`timeout: 600000`**: a fresh worktree does a first `uv sync`, and the suite takes
about a minute and a half.

## 8. Codex rounds

**First, does this slice get its own review?** `slice-status N` says `Codex review: on the PR`
or `Codex review: with the milestone`. It comes from the slice's complexity label and
`.sdlc/config.json`'s `slice_review` (here, routine slices are reviewed with their milestone;
judgment, novel and unrated slices on their own PR). **With the milestone**: skip this
section and go to §9. `merge` records that the review is owed, and Codex reviews the slice
with its milestone, before the demo.

Run one round with the tool, **from `WORKTREE`**. Run the Bash call in the background
(`run_in_background`) and wait for it to finish: with its one retry it can take longer than a
foreground call is allowed to.

```bash
cd <WORKTREE> && uv run python tools/sdlc.py codex-review --pr PR --out <SCRATCH>
```

It does what used to be done by hand, so don't run Codex any other way:

- it writes the slice brief to `<SCRATCH>`, since Codex's sandbox has no network;
- it fills `references/codex-pr-prompt.md`; from round 2 on, it adds your response from the
  latest recorded round. It refuses if that round asked for changes and has no response;
- it refuses unless the worktree is clean and its HEAD is the PR's head;
- it refuses, before starting Codex, a round that couldn't be recorded (the change rounds are spent: `escalate-slice`)
  or would change nothing (Codex already approved this head: go on to `merge`); `--dry-run` refuses the same way;
- it runs Codex with the model, reasoning effort, sandbox, flags and timeout from
  `.sdlc/config.json`'s `codex` section, never your own Codex config;
- it fails, and saves nothing, if HEAD moved while Codex ran;
- it writes the report's `HEAD:` line itself, to `<SCRATCH>/codex.md`, and retries once when
  Codex times out or ends with no verdict.

It prints the report and its path. `--dry-run` prints the filled prompt and the exact command
and runs nothing. Exit 1 is a refusal: fix what it says and run it again.

**If Codex can't run, defer its review to the milestone.** "Can't run" means `codex-review`
exits with status 3: `codex` isn't on the PATH, or there was no `VERDICT:` line after the one
automatic retry (two timeouts count). It also means Codex fails to sign in or says to log in,
reports a quota, usage or rate limit, or a network error. And it means the run's session log
didn't show what the repo's settings say Codex was given: skills offered, an `AGENTS.md` from
outside the checkout, or a log of a shape the tool doesn't know (a new `codex`); no report is
saved (SDLC.md, "The review logic's fingerprint"). In those cases the tool prints the
end of `<SCRATCH>/codex.err`. Copy the failure from there:

```bash
uv run python tools/sdlc.py review-defer PR --reason "<the error, verbatim>"
```

and go to §9. The deferral is bound to this head: a rebase needs a new one. It is refused
after Codex has asked for changes that no later round approved; answer those findings
and wait for Codex, or escalate. A slow review, or one you disagree with, is not
"can't run".

Answer every finding in `<SCRATCH>/response.md`, numbered like the findings:

- **Accepted**: say what you will change ("fixing in the next commit").
- **Rebutted**: give the evidence (a test, a line of code, the slice's Non-goals).

**Record the round before you push anything**, on the head Codex reviewed:

```bash
uv run python tools/sdlc.py pr-review PR --report <SCRATCH>/codex.md --response <SCRATCH>/response.md
```

`pr-review` refuses a report whose `HEAD:` line isn't the PR's current head, so a
round recorded after the fix is pushed is lost. Only then make the fixes you
accepted: commit, push, `test-record`, and run `codex-review` again on the new head.
It takes this response from the round you just recorded.

Repeat until Codex says `VERDICT: approve` **on the current head**. Only reviews
that ask for changes spend the budget; re-approving a rebased head is free. After
`max_pr_rounds` (5) reviews asking for changes:

```bash
uv run python tools/sdlc.py escalate-slice N --reason "<what is disputed, in a sentence>"
```

Then report and stop. **If you ran on a cheaper model than the strongest one in
`.sdlc/config.json`'s `models`**, say so in the reason: the coordinator's next move is
to retry the slice once on the strongest model: a fresh `/work-slice N` session on that
model that claims with `claim N --retry --model <strongest>`. That lifts the escalation,
keeps the branch, the worktree and the open PR, and gives its Codex rounds a fresh
budget. A second escalation goes to a human (`--retry` refuses twice).

## 9. Merge and clean up

```bash
uv run python tools/sdlc.py merge PR               # the gate; --dry-run to see it first
cd <PRIMARY> && uv run python tools/sdlc.py cleanup N
```

`merge` refuses unless all of these hold:

- the PR closes exactly this one slice;
- Codex's latest review of the **current head** approves, or the slice's review is
  owed to its milestone (a routine slice, or `review-defer` on this head). A review asking for
  changes always blocks;
- a passing test run is recorded on that head;
- the PR targets `epic/E`, and its head is **not behind `epic/E`**;
- the slice belongs to the epic's current milestone, the earliest one not yet shipped;
- GitHub says the PR is mergeable.

How to clear each refusal:

- **The head is behind `epic/E`.** Another slice merged first (or `sync` brought `main` in), and nobody has tested
  this branch with it. Rebase:
  `git -C <WORKTREE> fetch origin epic/E && git -C <WORKTREE> rebase origin/epic/E`,
  then push with `--force-with-lease`. Then `test-record`, then a Codex round on
  the new head (or a new `review-defer`, if Codex still can't run; nothing, for a slice
  reviewed with its milestone). A rebase changes the head, which voids both records.
- **Conflicts.** The same rebase, and resolve them.
- **UNKNOWN.** GitHub is still computing mergeability after a push. Wait a few
  seconds and run `merge` again. Don't rebase.
- **An earlier milestone hasn't shipped.** Leave the PR open and report it: it can merge
  once that milestone ships (`state <epic>` says what it waits for).

**Before merging, file what you're leaving behind**, so it isn't lost in PR text.
That means the non-blocking findings from the Codex rounds, the ones the advisor
left alone, and the adjacent issues in your PR body. File one issue per item, in
plain words, with where it is:

```bash
uv run python tools/sdlc.py file-issue E --debt --source review --title "..." --body-file <SCRATCH>/debt-1.md
```

Use `--source advisor` or `--source adjacent` as fits. A problem that isn't this
epic's work (a bug that already existed elsewhere) goes without `--debt`, to triage.
After each accepted milestone, a budgeted cleanup pass (`plan-issue --cleanup`) picks
tech debt up as slices. A slice with a `Pays down` section is one of those: `merge`
closes the debt issues it names.

`merge` squash-merges into `epic/E` (one commit per slice, so a bad slice is one
`revert-slice` later), deletes the remote branch, closes the slice itself (GitHub
only honours `Closes #N` on `main`) and reads it back, and
prints the epic's `next`. `cleanup` must run from the primary checkout, not from
inside the worktree.

## 10. Report

End with:

- the slice and its PR, now merged;
- the test count on the merged head;
- the Codex rounds and how each ended;
- the issues you filed for non-blocking findings and adjacent problems;
- whether a milestone completed;
- **Next:** the `next:` line `merge` printed (or `uv run python tools/sdlc.py state <epic>`).
  It names the next `/work-slice N`, or `/milestone-demo <epic>` once a milestone's units are all
  merged: new slices of that epic wait (`claim` refuses) until the poster accepts its demo.
  If it says "in progress", another session holds that slice.
