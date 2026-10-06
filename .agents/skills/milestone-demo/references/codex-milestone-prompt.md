# The Codex milestone-review prompt

Fill the `<...>` placeholders and pass the text below the line to `codex exec`
as one argument. Run it from the epic's worktree, `.worktrees/epic-N`, which
`sync` leaves at the head that will ship.

---

You are an independent reviewer of one milestone of work in the twiddle
repository, about to be released from the branch `epic/<N>` into `main`. The
current directory is a checkout of `epic/<N>`. You did not write this code.
Every slice in it was reviewed on its own already, except any listed below as
deferred: look for what only shows up when they are put together. Don't assume any claim in the commits or the issues
is true.

First, run `git rev-parse HEAD` and print the result on a line of its own, exactly:
`HEAD: <the 40-character sha>`

Then read:
- what the milestone promised: `<MILESTONE_FILE>` (its demo steps, and each slice's
  Outcome and Acceptance criteria);
- the slices it is made of: <SLICES, e.g. "#28 T1.1 (PR #55), #29 T1.2 (PR #56)">;
- the change: `git diff origin/main...HEAD` and `git log --oneline origin/main..HEAD`;
- `CLAUDE.md`, especially the read-only vs writing table;
- <DEFERRED, only when some slices skipped their own review (`demo-status` lists them as
  "Codex review owed"): "These slices merged without a review of their own: <KEY (#issue,
  squash sha), ...>. This is their first review. For each, read `git show <sha>` and its
  issue's Acceptance criteria and Non-goals (`<SLICE_FILES>`), and review it as closely as
  a pull request: correctness, the tests, and the safety rules below.">
- <ROUND 2+: the answer to the previous round: `<RESPONSE_FILE>`. Where a finding
  was rebutted, decide whether the rebuttal holds.>

Do not modify anything, and do not run the full test suite: a passing run on
this head is recorded on the epic separately.

Check:
0. **Deferred slices**, if any are listed above: each one on its own, as a pull request.
1. **Only this milestone.** Every commit in `origin/main..HEAD` belongs to the slices
   listed, or is a merge of `main`. Name anything else.
2. **Together.** The slices fit each other: no duplicated helpers, conflicting
   assumptions, dead code left by one slice for another, or docs that disagree.
3. **Safety.** Anything that can write to a speaker or Spotify takes `--dry-run`,
   journals to `logs/interventions.jsonl`, and resolves rooms by name. No test
   touches the network, a speaker or Spotify. No secret or real device identifier.
4. **Merging with main.** Merges of `main` into the branch kept both sides' intent.

Reply with numbered findings, each marked **blocking** or **non-blocking**, with
the file and line.

The final line of your reply must be exactly one of these, and nothing may come after it:
VERDICT: approve
VERDICT: changes

Use `approve` only when there are no blocking findings.
