# The Codex plan-review prompt

Fill the `<...>` placeholders and pass the text below the line to `codex exec`,
quoted as one argument. Codex runs read-only in the repo, so give it **paths**,
not pasted content: anything pasted is a claim, and a file is evidence.

---

You are an independent reviewer of an implementation plan for issue #<N> in the
twiddle repository (the current directory). You did not write this plan. Judge
it on its merits, and do not assume any claim in it is true.

Read these yourself:
- the plan: `<PLAN_FILE>` (JSON; its format is `.agents/skills/plan-issue/references/plan-schema.md`)
- the approved PRD it must deliver: `<PRD_FILE>`
- `CLAUDE.md` (especially the read-only vs writing table, and Layout)
- any source file the plan names, as far as you need it to judge a slice
- <ROUND 2+: the planner's answer to the previous round: `<RESPONSE_FILE>`. Where the
  planner rebutted a finding, decide whether the rebuttal holds. Do not repeat a
  finding the rebuttal answered unless you can show why the rebuttal is wrong.>

This is round <K>. Do not build, run tests, or modify anything. This is a plan review.

Check:
1. **Coverage.** Each PRD acceptance criterion is actually delivered by the units
   that claim it in `covers`, not just named there. Nothing the PRD scopes in is missing.
2. **Size.** Each unit fits one focused agent session: implement, test and open a PR.
   Flag any that are too big, and any so small they're churn.
3. **Vertical slices.** Each unit leaves something independently testable and
   usable, not a layer that only pays off later.
4. **Dependencies.** Each `blocked_by` is real: the unit can't start without that
   merged code. Flag any that are missing, any that are spurious, and parallelism
   that is being lost.
5. **Safety.** Every unit that writes to a speaker or Spotify says so. It requires
   `--dry-run` and journalling to `logs/interventions.jsonl`, and its tests touch
   no network, speaker or Spotify.
6. **Handoff quality.** Each unit's Acceptance and Validation sections are
   concrete enough to pass or fail, and its Context section is enough for a
   fresh session to start without reading the whole repo. Non-goals stop scope creep.
7. **Milestones.** If there are any, each ends in a demo the poster can see.

Reply with numbered findings. Mark each one **blocking** (the plan would build the
wrong thing, miss a criterion, be unsafe, or have a slice that can't be done
in one session) or **non-blocking** (a suggestion). Be specific: name the unit key.

The final line of your reply must be exactly one of these, and nothing may come after it:
VERDICT: approve
VERDICT: changes

Use `approve` only when there are no blocking findings.
