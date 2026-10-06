# Diagnosis: repo-owned, mechanical Codex reviews (#100)

Approved revision 2, signed off by @jeromebanks on 2026-10-06 ([diagnosis](https://github.com/jeromebanks/twiddle/issues/100#issuecomment-6022384648), [sign-off](https://github.com/jeromebanks/twiddle/issues/100#issuecomment-6022455935), [approval record](https://github.com/jeromebanks/twiddle/issues/100#issuecomment-6022465031)). The text below is rev 2 exactly as approved.

> This was filed as tech debt on #12 (Sonos alarms). It's SDLC tooling, not alarm work, so it has been taken off #12's debt and is triaged here on its own as a bug. The misfiling is tracked in #115.

## What is wrong

Codex reviews waste runs and need hand-assembly, though the findings themselves are good. Slice #38 (PR #99) took 6 Codex runs (per the issue body) for 4 rounds. All 4 are recorded on PR #99: three `changes`, then `approve`. There are four causes:

**Revision 2**, for the poster's `/changes`:
- the review logic must be owned by this repo's skills and script, not by wherever a run happens to pick it up;
- add an eval that runs only when that logic changes.

These are causes 5 and 6 below, and fixes 6–8.

1. **Codex is asked to prove which commit it reviewed, and often doesn't.** Both review prompts tell Codex to print `HEAD: <sha>` first. When it forgets, `pr-review` / `ship-review` refuse the report and the whole run is thrown away. As the poster pointed out, this attestation proves nothing the tool couldn't check itself.
2. **Every round's prompt is filled by hand.** One round broke because a `sed` used `/` as its delimiter and a path contained one. Another was given the previous round's `response.md`, so Codex never saw the answers to its findings.
3. **`test-record` straight after `git push` can fail** with "HEAD is not PR's head", because GitHub's `headRefOid` takes a moment to catch up with a push. Nothing says to wait.
4. **No self-review before the PR.** Each round found one more edge case of a familiar kind (bounds, shutdown order, dropped error paths, test hygiene). A short checklist would catch these before Codex sees the diff.
5. **The repo doesn't own its reviews.** Only the prompt text lives here. Everything else a review depends on comes from the machine it runs on:
   - the model and reasoning effort, from the user's `~/.codex/config.toml`;
   - instructions from a global `~/.codex/AGENTS.md`, plus any user-level skills, memories, rules, plugins and MCP servers;
   - the flags, the retry and the "can't run" rules, which are skill prose an agent copies by hand.

   So two runs on the same head can review under different instructions, and nothing on the PR shows which.
6. **Nothing tests that a review catches anything.** A change to a prompt, the flags or the model could make Codex approve everything, and every existing test would still pass. They check how a report is parsed, not whether the review is any good.

## Reproduction

Not reproduced locally: it needs a live `codex` and a PR. The evidence is PR #99's review history, and the code paths below show why each failure happens.

## Evidence

- `.agents/skills/work-slice/references/codex-pr-prompt.md:13-14` and `.agents/skills/milestone-demo/references/codex-milestone-prompt.md` (the "First, run `git rev-parse HEAD`" step): Codex is responsible for the attestation.
- `tools/sdlc.py:2509-2514` (`pr-review`) and `tools/sdlc.py:3007-3012` (`ship-review`): no `HEAD:` line means the run is refused.
- **Why the tool can check the HEAD itself:**
  - `work-slice/SKILL.md:150` runs `codex exec --sandbox read-only` from the slice's worktree, and the milestone review runs from `.worktrees/epic-N`.
  - Under that sandbox Codex can't change HEAD. Whatever the directory's HEAD is when the run starts is what Codex reads.
  - So the tool can check the worktree's HEAD against the PR head (or `epic/N`'s) before the run, confirm it hasn't changed afterwards, and write the `HEAD:` line itself.
- `tools/sdlc.py:2478-2480` (`test-record`): compares local HEAD with `headRefOid` once, with no retry.
- **Where today's review settings come from (cause 5):**
  - `codex exec --help` says `-c` overrides values "otherwise loaded from `~/.codex/config.toml`", and `-p` layers a profile from `$CODEX_HOME`.
  - On the maintainer's machine that file sets the model and reasoning effort, and `~/.codex/` also holds an `AGENTS.md`, skills, memories, rules and plugins.
  - None of these are in the repo, and the commands in `work-slice/SKILL.md:150` and `plan-issue/SKILL.md:126` pass no model.
  - Codex's built-in `codex exec review` and the Codex plugin for Claude Code bring prompts of their own. Neither is used today, and the fix keeps it that way.
- No test runs a real review; `tests/` exercises `parse_verdict` and `HEAD_RE` only on canned text.
- `work-slice/SKILL.md:145-160`: the prompt-filling, the `codex exec` command and the "no `HEAD:` line → run again" rule are all prose instructions.

## Proposed fix

1. **One tool command runs every Codex review.** It covers the milestone review too, which goes beyond #100's original proposal, because that review has the same HEAD-attestation step. The command is `tools/sdlc.py codex-review`, for a PR round (`--pr PR`) or a milestone round (`--milestone N`). It:
   - fills the right prompt from the issue, the base branch and the slice or milestone brief;
   - from round 2 on, includes the latest `response.md` and refuses if it's missing;
   - checks that the worktree is clean and its HEAD equals the PR head (or `epic/N`), runs `codex exec` with the documented flags, and checks that HEAD hasn't moved;
   - **writes `HEAD: <sha>` at the top of the saved report itself**;
   - retries once if there is no `VERDICT:` line;
   - prints the findings and the saved report's path.
2. **The "print HEAD" step comes out of both prompts.** `pr-review` and `ship-review` keep checking the `HEAD:` line, which the tool has now written. A report saved by hand without one is still refused. Depends on 1.
3. **`test-record` waits for GitHub.** It polls `headRefOid` for up to about 30 seconds until it equals local HEAD, then fails with the current message.
4. **A pre-PR self-review checklist in `work-slice` §6:**
   - every bound is finite and validated;
   - shutdown and cancel happen in the right order;
   - every error path returns a status instead of dropping the request;
   - tests listen on loopback only and stub anything that opens a socket to a fake address.
5. **The skills use the command.** `work-slice` §8 and `milestone-demo` call `codex-review` instead of filling prompts by hand. The "can't run → `review-defer`" rule stays, triggered by the command's exit status rather than by reading `codex.md`. Its condition "`codex.md` still has no `HEAD:` line after one retry" becomes "no `VERDICT:` line after the one automatic retry", since the tool now writes the HEAD line. The other conditions stay: `codex` not on PATH, sign-in, quota or rate limit, network, timed out twice. Depends on 1–2.

6. **The repo owns every Codex review**: PR rounds, milestone rounds, and `plan-issue`'s plan review, which is driven the same way (this goes beyond the poster's ask, for the same reason).
   - `codex-review` (and a plan mode of it) is the only way the skills run Codex.
   - It takes everything it uses from the repo: the prompts in the skills' `references/`, and the model, reasoning effort, flags and timeout from a `codex` section of `.sdlc/config.json`.
   - It runs Codex isolated from user-level instructions, skills, memories, rules, plugins and MCP servers. For example, a scratch `CODEX_HOME` holding only the sign-in and a config the tool writes.
   - If a setting can't be pinned, the command refuses instead of falling back to the user's.
   - Each recorded round names the model, effort and a hash of the exact prompt, so it's clear what the review ran under.
   - The skills say not to use `codex exec review`, the Codex plugin, or a hand-run `codex exec`. Depends on 1.
7. **A review eval**, `tools/sdlc.py codex-eval`.
   - Small fixture changes go under `tests/codex_eval/`. Each is a base plus a diff with one planted bug and the finding it should get (file, line, a keyword). At least one fixture is clean, to catch a reviewer that flags everything.
   - Each fixture runs through the same `codex-review` path and must come back with `VERDICT: changes` and a finding that names the planted bug's file and either its line (within a few lines) or the enclosing function. The clean one must come back `approve`. The tolerance keeps the eval from being flaky.
   - The result is recorded against a fingerprint of everything that decides how a review behaves:
     - the prompts;
     - the `codex` config section;
     - the `codex-review` code;
     - the eval's fixtures and its scorer;
     - `codex --version`, the one input the repo can't own, and the likeliest silent change.
   - Results must be findable by fingerprint from any branch or epic. A comment on one PR isn't enough; the planner picks the mechanism.
   - **It runs only when that fingerprint changes**: never in `pytest` and never on every commit. An unchanged fingerprint costs no tokens.
   - `codex-review` warns when the current fingerprint has no passing eval. A PR that changes the fingerprint can't merge without a passing eval for the new fingerprint, or a recorded deferral.
   - **A Codex outage doesn't block merges** (the policy from #78): if the eval can't run, it is deferred and bound to the fingerprint, as `review-defer` is bound to a head, and is owed before the milestone's demo. Depends on 6.
8. **The fingerprint check is offline.** A pytest asserts the fingerprint covers every file the review reads, so adding a new prompt or config key without bringing it under the eval fails the suite. Depends on 7.

## Verification

Offline tests with a fake `codex` on `PATH` (a script that prints a canned report), no network:

- the `HEAD:` line in the saved report is written by the tool and equals the worktree's HEAD, even when Codex's own output has none;
- the command refuses when the worktree's HEAD isn't the PR's or `epic/N`'s head, or the worktree is dirty, and runs no Codex;
- a report with no `VERDICT:` is retried exactly once, then fails with a status `review-defer` can use;
- round 2+ refuses without a response file, and the filled prompt contains the response's path;
- `test-record` succeeds once a stubbed `headRefOid` catches up, and fails after the timeout;
- the existing `pr-review` / `ship-review` tests still pass.

- with a fake `codex` that dumps its environment and arguments: the tool sets a scratch `CODEX_HOME` holding only the sign-in and the config it wrote, and passes the model and effort from `.sdlc/config.json`. That shows what the tool sets. It can't show what the real binary loads, which is checked below;
- the recorded round carries the model, effort and prompt hash;
- the fingerprint changes when any of its inputs changes, and not otherwise. `merge` refuses a PR that changes it until a passing eval or a deferral is recorded for the new fingerprint;
- `codex-eval` scores canned fake-Codex outputs correctly: a hit, a miss, a wrong line, and the clean fixture flagged.

Once, by hand, with real Codex:
- `codex-eval` passes;
- that run's session log shows the pinned model and no instruction source outside the repo and the scratch `CODEX_HOME`, checked from the log without planting files in `~/.codex`;
- a second run is skipped, because the fingerprint hasn't changed.

Then on the next real slice: no Codex run is thrown away for a missing `HEAD:` line.

## Safety class

None of it writes to a speaker, and nothing needs `--dry-run` or `logs/interventions.jsonl`. `codex-review` and `codex-eval` run Codex read-only and write only to scratch (the eval's fixtures are applied in a scratch checkout, never the primary one). They spend Codex tokens only for a real review, or when the review logic has changed. It touches GitHub only through the existing `pr-review` / `ship-review` / `test-record` paths, and it reads PR or branch state to check the HEAD.
