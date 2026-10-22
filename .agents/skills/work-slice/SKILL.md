---
name: work-slice
description: Build one ready slice of a planned twiddle epic, start to finish - claim it, work in its own git worktree, implement only what the slice issue says with offline tests, open a PR, record the full test run, get Codex to approve the exact head through review rounds, then squash-merge it into the epic's branch (epic/N, never main) and clean up. Also builds a tech-debt ticket of the debt epic's open pass (`--debt`), and merges its PR into main through the debt gate. Use when asked to work a slice or issue from an epic, "build the next slice", "do the next thing" on an epic in sdlc:planned or sdlc:in-progress, or to resume a slice in review.
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

**`/work-slice T --debt` is another:** a tech-debt ticket of the debt epic's open pass (with the
rest of its group), built from `origin/main` and merged into `main`. See "A debt ticket" at the end:
it says what differs, and its commands win over §2's `claim N`, §7's `--base epic/E` and §8's rule
that a review may be owed to a milestone. `state D` on the debt epic says `work_debt` and
`next: /work-slice T --debt`; `slice-status T` says `lane: debt`.

**`/work-slice N --quick` is a different lane:** an approved *quick change* (a small, read-only
change with a short brief and no epic), built from `origin/main` and merged into `main`. For
it, the rest of this file applies only where "A quick change" (after §10) points back to it:
that section says what differs, and its commands win over §2's `claim N`, §7's `--base epic/E`
and §8's rule that a review may be owed to a milestone. `state N` says `lane: quick` and
`next: /work-slice N --quick` for one; `slice-status N` says `lane: quick` too.

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
- `slice-check` says a milestone demo or release comes first, or a later milestone waits:
  report it and stop. A slice you already claimed can still be resumed.
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
cd <WORKTREE> && uv run python tools/sdlc.py test-record PR    # runs the full suite itself
```

`test-record` refuses if the tree is dirty or `HEAD` isn't the PR's head. It runs
`uv run pytest -q` itself and records the result on the PR, so you never report a
test run by hand. Run it again after every new commit. **Give the Bash call
`timeout: 600000`**: a fresh worktree does a first `uv sync`, and the suite is big,
so give it all the time the tool allows.

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
reports a quota, usage or rate limit, or a network error. In those cases the tool prints the
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
git -C <PRIMARY> pull --ff-only                    # the primary checkout, on main
cd <PRIMARY> && uv run python tools/sdlc.py merge PR    # the gate; --dry-run to see it first
cd <PRIMARY> && uv run python tools/sdlc.py cleanup N
```

**Run `merge` from the primary checkout, never from `WORKTREE`.** The worktree holds the slice's
own `tools/sdlc.py`, and a PR must never be judged by a gate it changed. So `merge` first
compares its running `tools/sdlc.py` and `tools/sdlc_quick.py` with `origin/main`'s, byte for
byte (after one bounded `git fetch`), and refuses, merging nothing, on any difference, a failed
fetch or an unreadable copy. The refusal names the primary checkout, and says to update it
(`git -C <PRIMARY> pull --ff-only`) when it is behind too. `--dry-run` is checked the same way.

**If `merge` itself is refused by a permission check** (not by the gate), don't work around it,
and never merge with a bare `gh pr merge`. Leave the PR ready and report: once a passing run and
an approving (or owed) review are recorded on its head, `slice-status N` says
`next: uv run python tools/sdlc.py merge PR` with the gate's results, and the session that started
you runs that one command.

The poster lifts that refusal once, by adding this rule to `.claude/settings.json` (an agent never
edits it; `SDLC.md`, "The merge permission", has the reasons):

```json
{
  "permissions": {
    "allow": [
      "Bash(uv run python tools/sdlc.py merge *)",
      "Bash(uv run python tools/sdlc.py cleanup *)"
    ]
  }
}
```

It should cover the `uv run` part of `cd <PRIMARY> && uv run python tools/sdlc.py merge PR`,
and `cleanup N` (not yet shown for a sub-agent). If you are a sub-agent and are still refused, that is the finding to report: say
so, with the refusal verbatim.

Then `merge` refuses unless all of these hold:

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

## A quick change (`--quick`)

A quick change is an issue whose approved brief was posted with `--lane quick` (`triage-issue`
says which changes qualify). Its unit of work is the *issue*, there is no epic and no milestone,
and the brief (a comment on the issue, not the poster's request in the issue body) is the slice's
brief: Outcome, Scope, Files, Acceptance criteria, Validation, Non-goals, Context. Everything in
§1 to §10 holds except as follows.

**1. Check.** `slice-status N` prints `lane: quick`, and `Codex review: on the PR (mandatory ...)`.
`slice-check N` lists what stops a claim: no approved brief or a newer revision not yet approved,
a missing section, a rating other than `complexity:routine`, an escalation, a claim already
held (then `--resume`). If the brief can't be built as written, don't guess: report it.
An `escalated` change means stop and report, as in §1.

**2. Claim.**

```bash
uv run python tools/sdlc.py claim N --quick --model <the model you are>   # or: claim N --quick --resume
```

It prints the same `WORKTREE=`, `PRIMARY=`, `BRANCH=slice/N` lines, with `BASE=origin/main`, and
moves the issue from `sdlc:approved` to `sdlc:in-progress` (and the brief is now frozen). Work
only in `WORKTREE`, as in §2. `claim` without `--quick` refuses a quick change, and `--retry` is
never allowed on one. Read the brief you are building from the approved revision's text:
`uv run python tools/sdlc.py state N --json`, its `approved_doc.body`.

**3-5. Plan, build, test.** As §3 to §5, from the brief. Its **Files** list bounds the diff: touch
no file it doesn't name (a renamed file counts by both paths), nothing under a denied path
(`.sdlc/config.json`'s `quick` section), and stay within `max_files` and `max_lines` there
(5 and 200 today). If the honest change needs more, don't squeeze it in: stop and go to
*Escalating* below.

**6. Self-review.** As §6, against the brief's Acceptance criteria.

**7. The PR targets `main`,** not an epic branch, and carries **evidence in place of a demo**
(there is none for a quick change):

```bash
gh pr create --base main --head slice/N --title "<the issue's title>" --body-file <SCRATCH>/pr.md
```

The body has exactly one `Closes #N` (and no other `Closes`, `Fixes` or `Resolves` of another
issue), the brief's acceptance criteria ticked where proved, and the evidence: the read-only
commands you ran and what they printed (for a station, `stations probe` and `np`; for a bug,
the failing case before and the passing one after). The repo is public: no IP, MAC, device ID
or secret. Then `test-record PR` from `WORKTREE`, with `timeout: 600000`, exactly as in §7.

**8. Codex reviews the PR itself,** never with a milestone: a routine rating doesn't defer it.

```bash
cd <WORKTREE> && uv run python tools/sdlc.py codex-review --pr PR --out <SCRATCH>
```

Run it in the background and wait, as §8 says. It reviews `git diff origin/main...HEAD` against the
approved brief (the `<QUICK>` parts of `references/codex-pr-prompt.md`: the Files list bounds the
change, and nobody looks at it after this review), and stamps the report with `Brief: <the approved
comment's id>` under `HEAD:`. Answer each finding in `<SCRATCH>/response.md`, then record the round
**before pushing any fix**:

```bash
uv run python tools/sdlc.py pr-review PR --report <SCRATCH>/codex.md --response <SCRATCH>/response.md
```

`pr-review` refuses a report that has no `Brief:` line, or one for a brief that has since been
revised. The round budget is the `quick` section's `max_change_rounds` (2 today), not
`max_pr_rounds`: recording a round that asks for changes beyond it makes the tool escalate the
change to the full lane itself (it prints that and exits 1). Fixing is commit, push,
`test-record`, `codex-review` again on the new head; a re-approval of a rebased head spends nothing.

**If Codex can't run** (the cases §8 lists), `review-defer` is refused for a quick PR: there is
no milestone to owe the review to, and `merge` would refuse anyway. Leave the PR open, report the
failure from `<SCRATCH>/codex.err` verbatim and the PR number, and stop. Don't escalate and
don't merge: a person decides between waiting for Codex and `escalate-to-full N`.

**9. Merge.** `uv run python tools/sdlc.py quick-gate PR` (read-only) prints every reason the PR
may not merge yet, or `ok`; `merge PR --dry-run`, from `<PRIMARY>` like `merge` itself, shows the squash. Then:

```bash
git -C <PRIMARY> pull --ff-only
cd <PRIMARY> && uv run python tools/sdlc.py merge PR
cd <PRIMARY> && uv run python tools/sdlc.py cleanup N
```

As for a slice, `merge` runs only from the primary checkout on an up-to-date `main` (it refuses
from `WORKTREE`, whose gate code is the PR's), and a session refused by a permission check
leaves the PR ready: `state N` and `slice-status N` then say `merge PR` is due, with the gate's results.

`merge` requires: the base is `main`; the PR closes exactly this one open quick issue, still
`sdlc:in-progress` with its brief unchanged and rated `routine`; a passing full-suite record on
the current head; Codex's approval of the current head, recorded against the issue's current
approved brief; the head not behind `main` (rebase onto `origin/main`, push with
`--force-with-lease`, then `test-record` and a new Codex round: a rebase voids both); a
mergeable PR; and a diff that stays inside the quick rules. It squash-merges into `main`, makes
sure the issue is closed as completed, moves it to `sdlc:done`, and posts a record on the issue
(the PR, the head, the test and review records). `merge` and `claim` do those label moves; you
never touch a label. File what you leave behind as in §9, with the quick issue's number, and by
the same rule: a follow-up is tech debt because of what it is, not because of the lane it came
from. Work the change should have included to support itself is tech debt
(`file-issue N --debt --source review|advisor|adjacent ...`); anything else is an ordinary issue,
filed without `--debt`, for triage. Its debt, labelled `tech-debt`, joins the debt epic like any
other, and `file-issue` and `state` on the debt issue say so.

**10. Report** as §10, with the PR and the test count; there is no milestone, and `next:` is
"nothing for #N".

### Escalating to the full lane

If the change is bigger than its brief, it leaves the quick lane with its approved brief
(no new PRD). Most of the time the tool does it: recording a Codex round past `max_change_rounds`,
or `merge` finding a denied path, the size cap or a file outside the brief's Files list, runs
`escalate-to-full N` itself. It prints the reasons, posts a `lane=full` record, puts the issue
back on `sdlc:approved`, and commits the brief to `docs/prd/N-*.md` with `commit-prd N`. Then
stop and report: the PR and the worktree are left as they are, and closing them is a person's
call (`/plan-issue N` plans the work afresh, and `cleanup N --force` discards the worktree). If
you see the change growing before a trigger fires, you may run it yourself, with
`escalate-to-full N --reason "<what grew>" --dry-run` first. `commit-prd N` is safe to rerun
when an escalation was cut short (`state N` says `commit_prd`).

## A debt ticket (`--debt`)

A debt ticket is a `tech-debt` issue that the debt epic's open pass (a plan comment on the debt
epic, `debt-pass-post`) put in a group of one to `debt.max_per_pr` tickets. The group is one unit
of work: one worktree, one branch, one PR into `main` that closes exactly its tickets. The tickets
(and the group's one-line scope and file list in the pass plan) are the brief; nothing else is
loaded. There is no epic, no milestone, no demo and no sign-off. Everything in §1 to §10 holds
except as follows.

**1. Check.** `uv run python tools/sdlc.py slice-status T` prints `lane: debt` and `Codex review: on
the PR (mandatory ...)`; `slice-check T` lists what stops a claim: a ticket that is not in
the open pass, is claimed already (then `--resume`), is escalated, or waits on a person. `state D`
(the debt epic) says what is due: only a `work_debt` line is yours. A `merge_debt` line means a
group's PR is ready to merge (go to 9).

**2. Claim.**

```bash
uv run python tools/sdlc.py claim T --debt --model <the model you are>   # or: claim T --debt --resume
```

It claims T's whole group (the claim is posted on every member), prints the usual `WORKTREE=`,
`PRIMARY=`, `BRANCH=slice/T` and `BASE=origin/main`, and makes the worktree from `origin/main`.
Work only in `WORKTREE`, as in §2. `T` is any ticket of the group; the branch and the PR carry its
number. `claim` without `--debt` refuses a debt ticket.

**3-6. Plan, build, test, self-review.** As §3 to §6, from the tickets and the group's scope and
files. Offline tests only, and never a command that writes to a speaker. A finding **inside** a
ticket's scope is fixed in this PR, not filed. A problem you notice **outside** it that your diff
neither causes nor touches is not this ticket's debt: drop it, or file it without `--debt`, for
triage, when it matters. A related flaw the ticket's own work turns up is debt of debt:

```bash
uv run python tools/sdlc.py file-issue T --debt --source review --title "..." --body-file <SCRATCH>/debt-1.md
```

`T` is the ticket whose work found it; the new ticket joins the debt epic and is generation 2. The
tool refuses past `debt.max_generation` and says to file an ordinary issue (without `--debt`)
instead. Do not work around that.

**When a ticket's own check needs a real speaker** (verifying a form on one, say), build and test
everything offline, then ask on the ticket and leave the group:

```bash
uv run python tools/sdlc.py debt-request T --body-file <SCRATCH>/request.md --dry-run
uv run python tools/sdlc.py debt-request T --body-file <SCRATCH>/request.md
```

The body is the step's own `--dry-run` output, then `uv run twiddle snapshot --room R`, the
command, and `uv run twiddle restore --room R`, for a person to run. You never run the write. The
ticket waits for a reply from its author or a collaborator; the pass carries on without it.

**7. The PR targets `main`:**

```bash
gh pr create --base main --head slice/T --title "<what it fixes>" --body-file <SCRATCH>/pr.md
```

The body has one `Closes #X` for each ticket of the group, and no other `Closes`, `Fixes` or
`Resolves`: the gate compares them with what GitHub will close. Add the acceptance proof and the
read-only commands you ran. Then `test-record PR` from `WORKTREE` with `timeout: 600000`, as §7.

**8. Codex reviews the PR itself,** and it is never deferred to anything:

```bash
cd <WORKTREE> && uv run python tools/sdlc.py codex-review --pr PR --out <SCRATCH>
```

It is held to the tickets and the group's scope and files. Answer each finding, record the round
with `pr-review` before you push a fix, and repeat on the new head, as §8. After `max_pr_rounds`
reviews that ask for changes: `escalate-slice T --reason "..."` hands the **whole group** to a
human and the pass ends without it. `review-defer` is refused for a debt PR. If Codex cannot run,
leave the PR open, report `<SCRATCH>/codex.err` verbatim and stop.

**9. Merge** from the primary checkout (never from `WORKTREE`), as the section above says:

```bash
git -C <PRIMARY> pull --ff-only
cd <PRIMARY> && uv run python tools/sdlc.py merge PR --dry-run
cd <PRIMARY> && uv run python tools/sdlc.py merge PR
```

The debt gate refuses unless the base is `main` and the head is not behind it; the PR closes only
open tickets of one claimed group of the open pass (every active member, each a sub-issue of the
epic) and GitHub will close the same ones; a passing run and Codex's approval are recorded on the
exact head; and it is mergeable. It then squash-merges, deletes the branch on GitHub and here,
removes the group's worktree, closes the tickets as completed and posts a record on each ticket
and on the debt epic. If that was the pass's last work it also ends the pass (the next pass, or the
epic closed). A separate `cleanup` is only needed when `merge` says a cleanup was left over. If a
permission check refuses `merge`, do what §9 says: leave it ready and report.

**10. Report** as §10, with the PR and the test count. There is no milestone. **Next:** the `next:`
line `merge` printed (or `state D`): the next group's `/work-slice T --debt`, a pass to plan, or
nothing.
