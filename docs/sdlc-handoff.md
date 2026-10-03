# Handoff: build `milestone-demo` (the last SDLC skill)

Read `SDLC.md`, `tools/sdlc.py` and `.agents/skills/work-slice/SKILL.md` first. Start in **plan mode on Opus**.

## State
- `triage-issue` takes an issue to `approved`; `plan-issue` to `planned`; `work-slice` builds one slice per session:
  - each slice gets its own worktree and claim;
  - the PR's test run and Codex review are recorded against its head SHA;
  - the agent merges once the gate passes.
- `uv run python tools/sdlc.py state <epic>` shows each milestone's merged count. When every unit is merged, its
  `next:` says `/milestone-demo N (not built yet)`.
- Issue #44 (milestone-at-a-time creation) changes when later milestones get their issues. The demo is the natural hook
  for replanning the next milestone.

## `milestone-demo`
- When a milestone's units are all merged:
  - write the demo and implementation doc (what was built, how to see it, what changed from the plan);
  - run the milestone's demo steps with the user present. Real speaker fires belong here, between
    `alarm snapshot`/`restore` and `twiddle snapshot`/`restore` for #12;
  - post it to the poster and move to `sdlc:demo-review`.
- The poster's `/approve` accepts the milestone. The last one moves the epic to `sdlc:done`. `/changes` becomes new slices
  through `plan-issue`.

Prior art: cubism-rs `.agents/skills/review-milestone`.
