# The Codex milestone-review prompt

`uv run python tools/sdlc.py codex-review --milestone N` fills this and runs it from the
epic's worktree, `.worktrees/epic-N`, which `sync` leaves at the head that will ship:
the text below the line, `<N>`, `<MILESTONE_FILE>`, `<SLICES>`, `<DEFERRED_SLICES>` and
`<RESPONSE_FILE>` replaced, the `<DEFERRED>` bullet kept only when slices merged without
a review of their own, and the `<ROUND2>` bullet only from round 2 on. Don't fill it by
hand. The tool checks the worktree's HEAD itself and writes the report's `HEAD:` line,
so the prompt doesn't ask Codex for it.

---

You are an independent reviewer of one milestone of work in the twiddle
repository, about to be released from the branch `epic/<N>` into `main`. The
current directory is a checkout of `epic/<N>`. You did not write this code.
Every slice in it was reviewed on its own already, except any listed below as
deferred: look for what only shows up when they are put together. Don't assume any claim in the commits or the issues
is true.

Read:
- what the milestone promised: `<MILESTONE_FILE>` (its demo steps, and each slice's
  Outcome and Acceptance criteria);
- the slices it is made of, each with its squash commit on `epic/<N>` and its issue's brief: <SLICES>;
- the change: `git diff origin/main...HEAD` and `git log --oneline origin/main..HEAD`;
- the safety rules every change is held to: `.agents/skills/review-rules.md`;
- <DEFERRED>these slices merged without a review of their own: <DEFERRED_SLICES>. This is
  their first review. For each, read `git show` of its squash commit and its brief's
  Acceptance criteria and Non-goals, and review it as closely as a pull request:
  correctness, the tests, and the safety rules below.</DEFERRED>
- <ROUND2>the answer to the previous round: `<RESPONSE_FILE>`. Where a finding
  was rebutted, decide whether the rebuttal holds.</ROUND2>

Do not modify anything, and do not run the full test suite: a passing run on
this head is recorded on the epic separately.

Check:
0. **Deferred slices**, if any are listed above: each one on its own, as a pull request.
1. **Only this milestone.** Every commit in `origin/main..HEAD` belongs to the slices
   listed, or is a merge of `main`. Name anything else.
2. **Together.** The slices fit each other: no duplicated helpers, conflicting
   assumptions, dead code left by one slice for another, or docs that disagree.
3. **Safety.** Every rule in `review-rules.md` holds: `--dry-run`, journalling,
   rooms by name, read-only staying read-only, offline tests, nothing private committed.
4. **Merging with main.** Merges of `main` into the branch kept both sides' intent.

Reply with numbered findings, each marked **blocking** or **non-blocking**, with
the file and line.

The final line of your reply must be exactly one of these, and nothing may come after it:
VERDICT: approve
VERDICT: changes

Use `approve` only when there are no blocking findings.
