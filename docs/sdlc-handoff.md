# Handoff: build `plan-issue` (next SDLC skill)

Read `SDLC.md`, `tools/sdlc.py` and `.agents/skills/triage-issue/SKILL.md` first. Start in **plan mode on Opus**.

## State
- Stage 1 `triage-issue` is built and proven on #12: issue -> questions -> PRD -> `/approve` -> `sdlc:approved`, PRD merged to `docs/prd/12-sonos-alarm-manager.md`.
- Labels after `approved` already exist and are unused: `sdlc:planned`, `sdlc:in-progress`, `sdlc:demo-review`, `sdlc:done`, `sdlc:escalated`.
- Mechanisms to reuse: marked agent comments (`<!-- sdlc:v1 kind=... rev=N -->`), revision-bound approval, `transition`, `state`, `next` (`action: plan` is this skill's trigger), the round budget and `escalated`.

## What `plan-issue` must do
Turn an approved PRD or diagnosis into work an agent can do in separate sessions:
1. an **epic** issue for the whole PRD;
2. **milestones**, only if the work needs them;
3. **subtasks** per milestone, with dependencies between them (they can run in sequence or parallel);
4. **slices** per subtask, each small enough for one Claude Code session.
Bugs: subtasks come from the diagnosis's proposed fix; slices only if needed.

## Decisions to make with the user (grill-me)
- Native GitHub sub-issues + `blocked_by`, or checklists? (cubism uses native; check `gh` 2.88 support.)
- Is a slice an issue or a checklist item in the subtask? Required sections in a slice (cubism's: Outcome, Scope, Acceptance criteria, Validation, Demo, Non-goals).
- Does the poster approve the plan (a second sign-off, `sdlc:planned`), or only the PRD?
- Labels/state for epics, subtasks and slices vs the issue's own `sdlc:*` label.
- Deterministic parts to add to `tools/sdlc.py`: create/link issues, validate slice sections, `next` over slices with unmet dependencies.

## Not yet built (do after this)
`work-slice` (one slice per session; subtask PR; tests; advisor + Codex review rounds posted to the PR, escalate after N rounds) and `milestone-demo` (demo + implementation doc, bounced to the poster). Prior art: postscript_interpreter `.claude/skills/work-issue`, cubism-rs `.agents/skills/{plan-epic,work-slice,codex-review,review-milestone}` and `scripts/sdlc.py`.

## First check
`uv run python tools/sdlc.py state 12` should say `approved`, action `plan`.
