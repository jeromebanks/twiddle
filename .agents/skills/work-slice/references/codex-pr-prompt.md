# The Codex PR-review prompt

`uv run python tools/sdlc.py codex-review --pr PR` fills this and runs it: the text
below the line, `<SLICE_FILE>`, `<BASE>`, `<EPIC>` and `<RESPONSE_FILE>` replaced, the
`<SLICE>` bullets kept for a slice PR, the `<QUICK>` bullets for a quick change's and
the `<DEBT>` bullets for a debt PR's (neither has an epic, so no `<EPIC>`), and the
`<ROUND2>` bullet kept only from round 2 on. Don't fill it by hand. The tool checks the worktree's HEAD itself and writes the
report's `HEAD:` line (and, for a quick change, the `Brief:` line naming the approved
brief it gave Codex), so the prompt asks Codex for neither.

---

You are an independent reviewer of a pull request that implements one slice of
work in the twiddle repository (the current directory is its git worktree). You
did not write this code. Judge it on its merits, and do not assume any claim in
the PR, the commits or a previous review is true.

Read:
- <SLICE>the slice's brief: `<SLICE_FILE>` (its Outcome, Scope, Acceptance criteria,
  Validation, Demo, Non-goals and Context sections define the job);</SLICE>
- <SLICE>the change: `git diff <BASE>...HEAD` and `git log <BASE>..HEAD` (`<BASE>` is
  epic #<EPIC>'s branch, which this slice merges into);</SLICE>
- <QUICK>the approved brief of this quick change: `<SLICE_FILE>` (its Outcome, Scope,
  Files, Acceptance criteria, Validation, Non-goals and Context sections define the job).
  Its Files section bounds the change: a changed file it doesn't list is a blocking
  finding;</QUICK>
- <QUICK>the change: `git diff <BASE>...HEAD` and `git log <BASE>..HEAD` (`<BASE>` is
  main: a quick change merges straight into main, with no epic, milestone or demo
  after it, so this review is the last look anyone takes before it lands);</QUICK>
- <DEBT>the brief of this debt PR: `<SLICE_FILE>`. Each tech-debt ticket it closes is
  the spec, and its pass group's scope and files (from the pass plan, at the top)
  bound the change. A problem you find outside that scope is not this ticket's debt:
  report it as **non-blocking** (it is filed as new debt), never as blocking;</DEBT>
- <DEBT>the change: `git diff <BASE>...HEAD` and `git log <BASE>..HEAD` (`<BASE>` is
  main: a debt PR merges straight into main, and no milestone review or demo follows,
  so this review is the last look anyone takes before it lands);</DEBT>
- `CLAUDE.md`, especially the read-only vs writing table;
- the code around the change, as far as you need it;
- <ROUND2>the implementer's answer to the previous round: `<RESPONSE_FILE>`. Where they
  rebutted a finding, decide whether the rebuttal holds, and don't repeat a finding it
  answered unless you can show why it's wrong. Check that each finding they accepted is
  actually fixed in this head.</ROUND2>

Do not modify anything, and do not run the full test suite: a passing run on
this head is recorded on the PR separately. You may run a few focused tests if
you need to check a specific claim.

Check:
1. **Acceptance.** Each acceptance criterion in the brief is met, and a test proves it. Name any that aren't.
2. **Scope.** Nothing outside the Scope; nothing listed in Non-goals; no unrelated changes.
3. **Safety.** Anything that can write to a speaker or Spotify takes `--dry-run`,
   journals to `logs/interventions.jsonl`, and resolves rooms by name. No test
   touches the network, a speaker or Spotify. No secret, token or real device
   identifier is committed.
4. **Correctness.** Bugs, unhandled failures, edge cases the brief implies, and
   tests that pass without testing what they claim.
5. **Fit.** The code reads like the surrounding code (naming, comments, idiom), and
   the docs this slice owns are updated.

Reply with numbered findings. Mark each one **blocking** (a criterion unmet, unsafe,
a real bug, or a test that proves nothing) or **non-blocking** (a suggestion).
Name the file and line.

The final line of your reply must be exactly one of these, and nothing may come after it:
VERDICT: approve
VERDICT: changes

Use `approve` only when there are no blocking findings.
