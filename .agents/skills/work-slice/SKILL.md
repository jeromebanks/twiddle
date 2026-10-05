---
name: work-slice
description: Build one ready slice of a planned twiddle epic, start to finish - claim it, work in its own git worktree, implement only what the slice issue says with offline tests, open a PR, record the full test run, get Codex to approve the exact head through review rounds, then merge and clean up. Use when asked to work a slice or issue from an epic, "build the next slice", "do the next thing" on an epic in sdlc:planned or sdlc:in-progress, or to resume a slice in review.
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
- **You merge.** `merge` is the gate. Nobody reviews individual PRs by hand: a human
  sees the result at the milestone demo, so the gate and the reviews must be honest.

`/work-slice [N]`. With no `N`, run `uv run python tools/sdlc.py state <epic>` (or
`next`) and take the `/work-slice N` it suggests.

## 1. Check

```bash
uv run python tools/sdlc.py slice-status N
uv run python tools/sdlc.py slice-check N          # add --resume when it says claimed / in-review
```

- `ready`: carry on.
- `claimed` or `in-review`: this is a resume. Use `claim N --resume` below, then pick
  up where the PR and its review comments left off.
- `blocked`, `escalated` or closed: report it and the `next` line, then stop.
- `slice-check` says a milestone demo comes first: report it and stop. `/milestone-demo <epic>`
  is next; a slice you already claimed can still be resumed.
- `slice-check` complains about missing sections, or the brief can't be built as
  written: don't guess. Report it. Splitting or fixing a slice is a planning job.

## 2. Claim and move into the worktree

```bash
uv run python tools/sdlc.py claim N                # or: claim N --resume
```

It prints `WORKTREE=`, `PRIMARY=`, `BRANCH=slice/N` and `BASE=origin/main`. Its other effects:

- the claim is posted on the slice, and you're assigned to it;
- on the first claim, the epic moves from `planned` to `in-progress`.

**Write `WORKTREE` and `PRIMARY` down as literal absolute paths.** Shell variables
don't survive between tool calls, so type the paths out every time:

- **Every file you Read, Edit or Write must have a path that starts with `WORKTREE`.**
  The same file under `PRIMARY` is the main checkout, on `main`. Editing it changes
  the wrong tree, and `test-record` then tests code that doesn't have your change.
- **Every shell command that touches the code runs in `WORKTREE`**: `cd <WORKTREE> && ...`, or `git -C <WORKTREE>`.
  Check `git rev-parse --abbrev-ref HEAD` says `slice/N` before you commit. Diff
  and rebase against `origin/main`, never your local `main`.
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

Have the `advisor` review the committed diff (`git diff origin/main...HEAD`).
Fix what it finds, rerun the affected tests, then commit and push.

## 7. Open the PR and record the tests

The PR body must contain **exactly one** `Closes #N`. Include:

- `Epic #E · <key>`;
- the slice's acceptance checklist, ticked where you proved it;
- the Validation commands you ran;
- any adjacent issues you noticed but left alone.

```bash
gh pr create --base main --head slice/N --title "<key>: <slice title>" --body-file <SCRATCH>/pr.md
cd <WORKTREE> && uv run python tools/sdlc.py test-record PR    # runs the full suite itself
```

`test-record` refuses if the tree is dirty or `HEAD` isn't the PR's head. It runs
`uv run pytest -q` itself and records the result on the PR, so you never report a
test run by hand. Run it again after every new commit. **Give the Bash call
`timeout: 600000`**: a fresh worktree does a first `uv sync`, and the suite takes
about a minute and a half.

## 8. Codex rounds

Save the slice brief where Codex can read it, since its sandbox has no network:
`gh issue view N --json title,body -q '.title + "\n\n" + .body' > <SCRATCH>/slice.md`. Fill
`references/codex-pr-prompt.md` into `<SCRATCH>/prompt.md` and run Codex **from
`WORKTREE`**, with `timeout: 600000` on the Bash call:

```bash
cd <WORKTREE> && codex exec --sandbox read-only --skip-git-repo-check "$(cat <SCRATCH>/prompt.md)" \
  < /dev/null > <SCRATCH>/codex.md 2> <SCRATCH>/codex.err
```

`< /dev/null` stops `codex exec` waiting on stdin forever. stderr is progress
noise. If `codex.md` is empty, or has no `HEAD:` line or verdict, the run failed:
read `codex.err` and run it again.

Answer every finding in `<SCRATCH>/response.md`, numbered like the findings:

- **Accepted**: say what you will change ("fixing in the next commit").
- **Rebutted**: give the evidence (a test, a line of code, the slice's Non-goals).

**Record the round before you push anything**, on the head Codex reviewed:

```bash
uv run python tools/sdlc.py pr-review PR --report <SCRATCH>/codex.md --response <SCRATCH>/response.md
```

`pr-review` refuses a report whose `HEAD:` line isn't the PR's current head, so a
round recorded after the fix is pushed is lost. Only then make the fixes you
accepted: commit, push, `test-record`, and run the next Codex round on the new
head, giving it this `response.md`.

Repeat until Codex says `VERDICT: approve` **on the current head**. Only reviews
that ask for changes spend the budget; re-approving a rebased head is free. After
`max_pr_rounds` (5) reviews asking for changes:

```bash
uv run python tools/sdlc.py escalate-slice N --reason "<what is disputed, in a sentence>"
```

Then report and stop.

## 9. Merge and clean up

```bash
uv run python tools/sdlc.py merge PR               # the gate; --dry-run to see it first
cd <PRIMARY> && uv run python tools/sdlc.py cleanup N
```

`merge` refuses unless all of these hold:

- the PR closes exactly this one slice;
- Codex's latest review of the **current head** approves;
- a passing test run is recorded on that head;
- the head is **not behind `main`**;
- GitHub says the PR is mergeable.

How to clear each refusal:

- **The head is behind `main`.** Another slice merged first, and nobody has tested
  this branch with it. Rebase:
  `git -C <WORKTREE> fetch origin main && git -C <WORKTREE> rebase origin/main`,
  then push with `--force-with-lease`. Then `test-record`, then a Codex round on
  the new head. A rebase changes the head, which voids both records.
- **Conflicts.** The same rebase, and resolve them.
- **UNKNOWN.** GitHub is still computing mergeability after a push. Wait a few
  seconds and run `merge` again. Don't rebase.

`merge` squash-merges, deletes the remote branch, checks the slice closed, and
prints the epic's `next`. `cleanup` must run from the primary checkout, not from
inside the worktree.

## 10. Report

End with:

- the slice and its PR, now merged;
- the test count on the merged head;
- the Codex rounds and how each ended;
- any non-blocking findings or adjacent issues you left alone;
- whether a milestone completed;
- **Next:** the `next:` line `merge` printed (or `uv run python tools/sdlc.py state <epic>`).
  It names the next `/work-slice N`, or `/milestone-demo <epic>` once a milestone's units are all
  merged: new slices of that epic wait (`claim` refuses) until the poster accepts its demo.
  If it says "in progress", another session holds that slice.
