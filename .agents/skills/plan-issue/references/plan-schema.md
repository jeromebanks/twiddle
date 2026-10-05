# plan.json

One JSON file per plan. `tools/sdlc.py plan-validate` checks it, `plan-post`
renders it into the plan comment (with the JSON embedded), and `plan-create`
builds the issues from **that comment's** JSON, so what Codex reviewed is
exactly what gets created.

```json
{
  "issue": 12,
  "kind": "feature",
  "milestones": [
    {"key": "M1", "title": "See every alarm", "demo": "`twiddle alarm list` on the real household, read-only"}
  ],
  "subtasks": [
    {
      "key": "T1", "title": "Alarm model and list", "milestone": "M1",
      "summary": "Read ListAlarms into a model and print it.",
      "slices": [
        {
          "key": "T1.1", "title": "Parse ListAlarms into Alarm records",
          "blocked_by": [], "covers": [1, 3],
          "outcome": "What exists when this closes, observably.",
          "scope": "Files/modules touched; what is in and out of this session.",
          "acceptance": ["A recorded ListAlarms fixture parses into ...", "..."],
          "validation": "Exact commands: `uv run pytest tests/test_alarms.py -q` ...",
          "demo": "What the poster sees, or 'none: internal, enables T1.2'.",
          "non_goals": "Adjacent work this session must not absorb.",
          "context": "The minimum a fresh session reads: docs/prd/12-...md §Scope, src/twiddle/household.py ...",
          "complexity": "routine",
          "complexity_reason": "Follows the ListZones parser in household.py; no design choices."
        }
      ]
    },
    {
      "key": "T2", "title": "Discount alarms in analyse", "milestone": "M1",
      "blocked_by": ["T1.1"], "covers": [18],
      "outcome": "...", "scope": "...", "acceptance": ["..."], "validation": "...",
      "demo": "...", "non_goals": "...", "context": "...",
      "complexity": "judgment", "complexity_reason": "Touches the evidence path: what analyse discounts."
    }
  ]
}
```

Two optional top-level fields:

- `"create": "milestone"` (the default, from `.sdlc/config.json`) or `"all"`. With
  `milestone`, `plan-create` makes one milestone's issues at a time: the first now,
  each next one only after the previous one's demo is accepted and the plan is
  revised with what it taught. `all` makes everything at once. It suits a batch of
  independent, well-understood slices, like stations or venue sources. A plan with
  one milestone, or none, is the same either way.
- `"lessons": {"milestone": "M1", "text": "..."}`: what the last created milestone
  taught, and what this revision changes because of it. It is shown as `Lessons
  from M1`, and is required on every revision after a milestone was created while
  others are still to come.

A cleanup pass (`plan-issue --cleanup`) adds `"cleanup_of": "M1"`, the accepted
milestone whose tech debt it pays down. Its new slices carry `"debt": [81, 84]`,
the `tech-debt` issues they close (rendered as a `Pays down` section; `merge`
closes them). It adds at most `cleanup_slices_per_milestone` new slices.

## Rules the validator enforces

- `issue` is the epic (the triaged issue itself). `kind` is `feature` or `bug`.
- Keys are unique across milestones, subtasks and slices. They start with a letter
  and use only letters, digits, `.`, `-` and `_` (`M1`, `T3`, `T3.2`).
- **Units of work** are slices, plus subtasks that have no slices. Every unit needs:
  - a non-empty `title`;
  - `outcome`, `scope`, `validation`, `demo`, `non_goals` and `context`;
  - `acceptance`, a non-empty list (it becomes a checkbox list);
  - `complexity`, how hard it is, and `complexity_reason`, one line on why:
    - `routine`: a clear pattern to follow, little design;
    - `judgment`: real design choices, safety-sensitive code (anything that writes to a
      speaker or Spotify), or the evidence path (the journal, `analyse`);
    - `novel`: unknowns that need investigating first.

    Rate the work, not the model: `.sdlc/config.json`'s `models` maps each level to the
    model that builds it, so changing models never means editing issues.
- A subtask that has slices needs a `summary`, and may not carry `blocked_by`: put
  it on its slices.
- `blocked_by` names other units of work, not a subtask that has slices, and
  never the unit itself. The graph has no cycles. A unit may wait on an earlier
  milestone's unit, never on a later one's: that one may not exist yet.
- With `milestones`, every subtask names one. Without them, none does.
- `covers` lists PRD acceptance-criterion numbers: the numbered items under
  `## Acceptance criteria` and `## Addendum` in `docs/prd/N-*.md`. Together, the
  units must cover every one, and none may cite a number the PRD doesn't have.
  A feature needs its PRD merged first. For a bug with no PRD file, coverage isn't checked.
- The rendered comment must fit GitHub's 65,536-character limit. If it doesn't,
  tighten the text: Context should point at files, not paste them.

## What gets created

| plan | GitHub |
|---|---|
| milestone | a milestone titled `#N M1: <title>`, its description the demo |
| subtask with slices | issue `#N T1: <title>`, label `plan:subtask`, sub-issue of the epic |
| slice | issue `#N T1.1: <title>`, label `plan:slice`, sub-issue of its subtask |
| subtask without slices | issue labelled `plan:slice`, sub-issue of the epic |
| `blocked_by` | a native issue dependency |
| `complexity` | a `complexity:<level>` label, and a `Complexity: <level> — <reason>` line in the body |

Each unit's body starts with `<!-- sdlc:v1 kind=slice epic=N key=T1.1 -->`, then
`Epic: #N · Parent: #P · Covers acceptance criteria …`, then the seven sections
and a `Dependencies` line explaining the native links. `plan-create` uses the
marker to find what already exists, so a rerun does not duplicate anything.
