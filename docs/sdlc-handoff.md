# Handoff: build `work-slice` and `milestone-demo` (next SDLC skills)

Read `SDLC.md`, `tools/sdlc.py`, `.agents/skills/plan-issue/SKILL.md` and
`references/plan-schema.md` first. Start in **plan mode on Opus**.

## State
- `triage-issue` takes an issue to `sdlc:approved`, with a PRD merged to `docs/prd/N-*.md`.
- `plan-issue` takes it to `sdlc:planned`:
  - the issue is the **epic**;
  - subtasks (`plan:subtask`) and one-session units of work (`plan:slice`) are native sub-issues;
  - order is native `blocked_by`;
  - optional GitHub milestones each name a demo.
- The plan is reviewed by the advisor, then by Codex rounds (`codex exec --sandbox read-only`,
  `VERDICT:` line), until they agree. The poster is not asked unless there's no consensus.
- `uv run python tools/sdlc.py ready --epic N` lists the units of work whose blockers all closed as completed.
  That is `work-slice`'s queue.
- Each unit's body has the marker `<!-- sdlc:v1 kind=slice epic=N key=K -->` and the sections
  Outcome, Scope, Acceptance criteria, Validation, Demo, Non-goals, Context and Dependencies.

## `work-slice` (one unit per session)
- Pick from `ready`, claim it, branch, implement only what its sections say, test offline, and open a PR that closes the unit.
- Review rounds: advisor, then Codex on the PR head, posted to the PR. Escalate after N rounds.
  Reuse `parse_verdict` and the round-budget pattern from planning.
- Move the epic `planned -> in-progress` on the first claim.

## `milestone-demo`
- When every unit in a milestone is closed as completed, write the demo + implementation doc and post it to the poster
  (`sdlc:demo-review`). Their `/approve` closes the milestone. The last one moves the epic to `sdlc:done`.

Prior art: cubism-rs `.agents/skills/{work-slice,codex-review,review-milestone}` and `scripts/sdlc.py`
(claims, SHA-bound review receipts, `check-slice`).
