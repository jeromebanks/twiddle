---
name: milestone-demo
description: Show the poster a finished milestone of a twiddle epic and take their answer - run the milestone's demo steps from the epic's branch, capture real terminal and TUI screenshots, publish them to the sdlc-demos branch and post a plain-language demo on the epic issue; then record the poster's /approve and ship the accepted milestone to main (sync, Codex review of the milestone, a merge commit), or turn their /changes into new slices. Use when state's next says /milestone-demo N, when asked to demo or ship a milestone or an epic, or when a poster replied to a demo.
---

<!-- No dollar-digit sequences in this file: the skill loader substitutes them with arguments. -->

# Demoing a milestone

Slices are merged without a human reading each PR (`work-slice`). The demo is the
moment a human sees the result: **what can the poster do now that they couldn't
before?** The poster is an end user, so the demo shows behaviour, not code. It is
posted on the epic issue, with real screenshots, and ends with their answer:

- `/approve`: the milestone is accepted, and you **ship** it: `epic/N` is merged into
  `main`. Until then nothing of it is on `main`. The last ship closes the epic (`sdlc:done`).
- `/changes <what>`: the changes become new slices (`plan-issue`), then a new demo.
- A question: you answer it, and the demo stays in review.

Slices land on the epic's branch, `epic/N`, so the demo shows `epic/N`, and `main`
is the "before". While a milestone is complete but not yet accepted **and shipped**,
**new slices of the epic wait**: `claim` refuses, `merge` refuses a slice of any
other milestone, and `state`'s `next:` names this skill.

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
- **before** and **after**: `origin/main` and `origin/epic/N`. A milestone built before
  epic branches (straight onto `main`) shows `main` against the commit before its first merge;
- its demo's state.

`demo due:` names the milestone to show. An epic without milestones (most bugs) has one demo, `all`.

| action | do |
|---|---|
| `work_slices` and `next` says `/milestone-demo` | steps 2-7 for the due milestone |
| `record_demo_acceptance` | the poster said `/approve`: step 8, then step 10 |
| `next` says `... is accepted; ship it` | step 10 |
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
after**: the same command at `before` and at `after`, captured one right
after the other, because live output like `np` changes minute to minute. Then
add what "Try it yourself" will list (read-only commands only), and what changed
from the plan (compare the slices' Outcomes with the PRs).

Have the `advisor` check the script.

## 3. Run it with the user

You're in the session with the person running it. They may not be the poster.

- Read-only and `--dry-run` steps: run them.
- **Writes**, one at a time. **The user runs them, not you**: several ask
  for confirmation at a terminal (`alarm rm` asks twice, and with no terminal
  the answer is no), and your shell has none.
  1. Before the first write, the user runs `! uv run twiddle snapshot --room <room>` (and
     `! uv run twiddle alarm snapshot` if alarms change).
  2. You run the step with `--dry-run` and show them what it will do.
  3. They run the real command themselves, as `! uv run twiddle ...`, so it lands in
     this session.
  4. Afterwards, ask what they heard or saw, and record it:
     `uv run python tools/observe.py add --heard "<their words>"`.
  5. When done, they restore: `! uv run twiddle restore --room <room>`, then
     `alarm restore --dry-run` (you can run that), then `! uv run twiddle alarm restore`.

  The pictures of a write are its `--dry-run` and the read-only state afterwards
  (e.g. `alarm list`), never the write itself.
- If the user isn't available for writes, demo what you can and say plainly which
  steps weren't shown and why. Never fake one.
- If a step fails, that's a finding, not something to hide. Note it for "Known gaps",
  or stop and report if the milestone clearly doesn't work.

## 4. Take the pictures

Put everything in `<SCRATCH>/demo/` (your scratchpad), not in the repo. **Every file
in it is published**, so nothing else goes there. `demo_shot.py` takes **read-only and
`--dry-run` commands only**: it runs what it's given, and a command's prompt would end
up in the picture instead of in front of the user.


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
- **Run every step from `after`**, never from the primary checkout (that's `main`, which
  doesn't have the milestone yet): `git fetch origin && git worktree add --detach <SCRATCH>/after <after>`,
  then `cd <SCRATCH>/after && uv sync -q && uv run python <PRIMARY>/tools/demo_shot.py cli --out <SCRATCH>/demo/alarms.svg -- uv run twiddle alarm list`
  (`demo_shot.py --cwd <SCRATCH>/after` does the same). TUIs too.
- **Before:** the same with `git worktree add --detach <SCRATCH>/before <before>`. Take
  the "after" picture straight away. Remove both worktrees when done (`git worktree remove`).
- Look at each picture: `qlmanage -t -s 1400 -o <SCRATCH>/previews <SCRATCH>/demo/<file>.svg`
  makes a PNG you can Read (`mkdir -p <SCRATCH>/previews` first, outside the published folder).
  Retake anything blank, cut off or mid-load.

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
issues, and moves the epic back to `in-progress`. The milestone isn't on `main`
yet: go on to step 10 and ship it.

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

## 10. Ship the accepted milestone

`main` gets the milestone now, as one merge commit of `epic/N`. Every step is a
tool step; never merge or push by hand.

1. **Bring `main` in and test the result:**

   ```bash
   uv run python tools/sdlc.py sync N
   ```

   It merges `origin/main` into `epic/N` (never a rebase) in `.worktrees/epic-N`, runs
   the full suite there (give the Bash call `timeout: 600000`), records the run on
   the epic, and pushes only on a pass. Run it even when `epic/N` is current: it
   records the run on the head that ships. A conflict or a failing suite escalates
   the epic to a human, and you stop.
2. **Codex reviews the milestone's whole diff** on that head. Fill
   `references/codex-milestone-prompt.md` into `<SCRATCH>/ship-prompt.md` and run it
   from `<PRIMARY>/.worktrees/epic-N`, as `work-slice` §8 runs Codex
   (`< /dev/null`, stdout to `<SCRATCH>/ship-codex.md`, stderr apart, `timeout: 600000`).
   Answer each finding in `<SCRATCH>/ship-response.md`, then record the round **before
   anything changes on `epic/N`**:

   ```bash
   uv run python tools/sdlc.py ship-review N --report <SCRATCH>/ship-codex.md --response <SCRATCH>/ship-response.md
   ```

   Every slice was already reviewed, so this looks for what only shows up
   together. A blocking finding is a demo finding: it needs fix slices
   (`plan-issue`), never a hand edit on `epic/N`.
3. **Ship:**

   ```bash
   uv run python tools/sdlc.py ship N --dry-run
   uv run python tools/sdlc.py ship N
   ```

   It refuses unless the milestone is accepted, `epic/N` contains `main`, a passing
   run and Codex's approval are recorded on its current head, and no slice of a
   milestone that isn't accepted is on it. Then it opens a pull request
   `epic/N -> main` and merges it with a **merge commit**, so each slice stays one
   revertable commit on `main`, and records the release on the epic. A milestone
   that was built straight onto `main`, before epic branches, ships as a recorded
   no-op. The last ship deletes `epic/N`, moves the epic to `done` and closes it.

Report the release and the **Next:** line `ship` prints: the next `/work-slice`, or
nothing when the epic is done.

**Rolling back one slice before it ships** (a demo shows it's wrong):
`uv run python tools/sdlc.py revert-slice S --reason "..."`. It makes one tested revert
commit on `epic/N`, reopens the slice, and voids its milestone's demo. The slice is
built again with `/work-slice S`.
