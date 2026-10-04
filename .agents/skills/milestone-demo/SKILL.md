---
name: milestone-demo
description: Show the poster a finished milestone of a twiddle epic and take their answer - run the milestone's demo steps (writes only with the user present and confirming), capture real terminal and TUI screenshots, publish them to the sdlc-demos branch and post a plain-language demo on the epic issue; then record the poster's /approve (closing the milestone, or the whole epic) or turn their /changes into new slices. Use when state's next says /milestone-demo N, when asked to demo a milestone or an epic, or when a poster replied to a demo.
---

<!-- No dollar-digit sequences in this file: the skill loader substitutes them with arguments. -->

# Demoing a milestone

Slices are merged without a human reading each PR (`work-slice`). The demo is the
moment a human sees the result: **what can the poster do now that they couldn't
before?** The poster is an end user, so the demo shows behaviour, not code. It is
posted on the epic issue, with real screenshots, and ends with their answer:

- `/approve`: the milestone is accepted. The last one closes the epic (`sdlc:done`).
- `/changes <what>`: the changes become new slices (`plan-issue`), then a new demo.
- A question: you answer it, and the demo stays in review.

While a milestone is complete but not accepted, **new slices of the epic wait**:
`claim` refuses, and `state`'s `next:` names this skill. Slices already claimed
can still finish.

- **`tools/sdlc.py` owns every post, label and push.** You run the demo, take the
  pictures, and write two markdown files.
- **The demo branch is public.** `demo-post` refuses anything that looks like an IP,
  a MAC or serial, a Sonos player or household ID, or a secret. `demo_shot.py`
  masks them as it draws. Still, look at every picture before you publish.
- **Never invent an outcome.** Whether an alarm rang, or how it sounded, is what the
  user tells you, not what a command printed.

`/milestone-demo [N]`. With no `N`, take the first `/milestone-demo` line of
`uv run python tools/sdlc.py next`.

## 1. Read the state

```bash
uv run python tools/sdlc.py state N
uv run python tools/sdlc.py demo-status N          # --json for the full brief
```

`demo-status` lists, per milestone:

- its **demo steps** (the milestone's description; each slice's Demo section when the epic has no milestones);
- its slices and their merged PRs;
- the **before** commit (just before its first merge);
- its demo's state.

`demo due:` names the milestone to show. An epic without milestones (most bugs) has one demo, `all`.

| action | do |
|---|---|
| `work_slices` and `next` says `/milestone-demo` | steps 2-7 for the due milestone |
| `record_demo_acceptance` | the poster said `/approve`: step 8 |
| `demo_reply` | the poster replied: step 9 |
| `turn` is `poster` | the demo is waiting on them: report it and stop |
| anything else | report the `next:` line and stop |

## 2. Plan the demo

Read the milestone's demo steps, the slices' Outcome and Demo sections, the merged
PRs' descriptions, and `CLAUDE.md`'s read-only vs writing table. Write a short
script of the steps, each one tagged:

- **read-only**: you run it, e.g. `alarm list`, `np`, `stations probe`, or a TUI that you only look at;
- **dry-run**: you run it, e.g. `alarm add ... --dry-run`;
- **write**: something fires on a real speaker. You run it only with the user present, and only after they say yes.

For each step, plan the picture that shows it. A **bug fix** shows **before and
after**: the same command at the `before` commit and at `main`, captured one right
after the other, because live output like `np` changes minute to minute. Then
add what "Try it yourself" will list (read-only commands only), and what changed
from the plan (compare the slices' Outcomes with the PRs).

Have the `advisor` check the script.

## 3. Run it with the user

You're in the session with the person running it. They may not be the poster.

- Read-only and `--dry-run` steps: run them.
- **Writes**, one at a time:
  1. Before the first write, `uv run twiddle snapshot --room <room>` (and
     `uv run twiddle alarm snapshot` if alarms change).
  2. Run the step with `--dry-run` and show the user what it will do.
  3. Ask them. Only a yes fires it.
  4. Afterwards, ask what they heard or saw, and record it:
     `uv run python tools/observe.py add --heard "<their words>"`.
  5. When done, restore: `uv run twiddle restore --room <room>`, then
     `uv run twiddle alarm restore --dry-run`, then `alarm restore`.
- If the user isn't available for writes, demo what you can and say plainly which
  steps weren't shown and why. Never fake one.
- If a step fails, that's a finding, not something to hide. Note it for "Known gaps",
  or stop and report if the milestone clearly doesn't work.

## 4. Take the pictures

Put everything in `<SCRATCH>/demo/` (your scratchpad), not in the repo:

```bash
# a command's output, drawn as a terminal window
uv run python tools/demo_shot.py cli --out <SCRATCH>/demo/alarms.svg -- uv run twiddle alarm list
# a TUI, driven by keys: a shot at each shot:<name>
uv run python tools/demo_shot.py tui --out-dir <SCRATCH>/demo \
  --step wait:8 --step escape --step wait:6 --step shot:dial -- dial
```

- **TUIs** start with a splash. `escape` dismisses it, and live data needs a few
  seconds (`wait:`). **Only read-only keys** belong in `--step`: navigation, `t`,
  `i`, `?`. Never `enter`, `s`, `d`, `R`, volume or mute in `dial`/`scene`. A
  writing key is a demo step, never a screenshot.
- **Before:** `git worktree add <SCRATCH>/before <before-sha>`, then
  `cd <SCRATCH>/before && uv sync -q && uv run python <PRIMARY>/tools/demo_shot.py cli --out <SCRATCH>/demo/before-np.svg -- uv run twiddle np wfmu`.
  Take the "after" picture straight away. Remove the worktree when done.
- Look at each picture: `qlmanage -t -s 1400 -o <SCRATCH>/demo <file>.svg` makes a
  PNG you can Read. Retake anything blank, cut off or mid-load. The PNG previews
  are not published: delete them.

## 5. Write the two files

- `<SCRATCH>/demo/README.md`, the **full write-up**, published next to the pictures:
  - what was built, slice by slice, with PRs;
  - the steps as run (and who confirmed each write);
  - what changed from the plan, and why;
  - known gaps;
  - every picture.
- `<SCRATCH>/comment.md`, the **comment**, written for the poster. Follow
  `references/demo-comment.md`. Images and links are relative (`![...](alarms.svg)`,
  `[the full write-up](README.md)`): `demo-post` points them at the published commit.

## 6. Show the user, then publish

```bash
uv run python tools/sdlc.py demo-post N --milestone M1 --dir <SCRATCH>/demo --body-file <SCRATCH>/comment.md --dry-run
```

Show the user the comment and the pictures. **Publishing to a public branch is
permanent**, so wait for their OK. Then run it again without `--dry-run`:

- it commits the folder to `sdlc-demos` under `epic-N/M1/rev-K/` (its own worktree);
- it pushes, and pins every link to that commit;
- it posts the demo on the epic, collapses an earlier revision, and moves the epic to `sdlc:demo-review`.

The first demo ever posted: open the comment and check the pictures show inline.
If they don't, report it rather than carrying on.

## 7. Report

- The milestone, and the link to the demo.
- What was shown, what wasn't, and why.
- What the user heard (their words).
- **Next:** `nothing: waiting on the poster to reply on #N`.

## 8. The poster approved

```bash
uv run python tools/sdlc.py demo-accept N --dry-run
uv run python tools/sdlc.py demo-accept N
```

It records the acceptance, closes the GitHub milestone and its finished subtask
issues, and moves the epic back to `in-progress`. If this was the last milestone
and nothing is left, it moves the epic to `done` and closes it. Report the
**Next:** line it prints, which will be the next `/work-slice`, or nothing when the epic is done.

## 9. The poster replied

Read their reply as an end user's, not a reviewer's.

- **A question** ("what's the bell for?"): answer plainly, then
  `uv run python tools/sdlc.py transition N demo-review --kind note --body-file <SCRATCH>/answer.md`.
  The demo stays in review.
- **A change request** (`/changes ...`, or a comment that clearly asks for something
  different): restate it in plain words, one bullet per change, as something they'd
  notice. Then:
  `uv run python tools/sdlc.py demo-changes N --body-file <SCRATCH>/changes.md`.
  The epic returns to `in-progress`, and **Next:** is `/plan-issue N`, which plans
  the changes as new slices. After they merge, this skill runs again and posts demo rev 2.
- **Unclear**: answer with a note that asks which they meant.
