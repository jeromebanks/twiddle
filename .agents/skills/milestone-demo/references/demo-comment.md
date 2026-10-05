# The demo comment

The comment is read by the poster: someone who uses twiddle, not someone who
codes it. Lead with what they can do now, show it, and say exactly what you need
from them. `demo-post` adds the header and the footer that asks for `/approve` or
`/changes`, so don't write them yourself.

Images and links are relative to the demo folder. `demo-post` points them at the
published commit. Keep it short enough to read on a phone: the detail goes in
`README.md`.

---

```markdown
## <Milestone title, e.g. "See every alarm">

**What you can do now:** <one or two sentences in the poster's words: what they
can see or do that they couldn't before.>

### 1. <First thing, as the poster would say it, e.g. "Every alarm in the house, by room">

![<what the picture shows>](alarms.svg)

<One line on what to notice, e.g. "The R Roam's 7:00 alarm shows under Sonos Roam,
where you set it.">

### 2. <Next thing>

<For a bug fix, before and after, side by side or one under the other:>

| Before | After |
|---|---|
| ![before: WFMU's title as one string](before-np.svg) | ![after: artist and song separated](after-np.svg) |

<What the user confirmed, for anything that fired on a speaker, e.g.
"At 7:02 the Roam rang with KALX: confirmed by ear in the kitchen.">

### Try it yourself

    uv run twiddle alarm list

<Read-only commands only. Never a command that changes what a speaker does.>

### What's different from the plan

- <A change and why, in the poster's terms. Or: "Nothing: built as planned.">

### Not done yet

- <Known gaps, and what comes in the next milestone.>

<details><summary>How it was built</summary>

- #28 `T1.1` <title>: PR #55
- ...

The full write-up, with every step as it was run: [README](README.md).

</details>
```
