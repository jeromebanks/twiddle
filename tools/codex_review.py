"""How a Codex review runs: everything that decides its behaviour, in one stdlib-only module.

`tools/sdlc.py` does the GitHub reads, the posting and the gates; this module decides what Codex is asked
(the prompt, filled from the repo's template), with which settings (`.sdlc/config.json`'s `codex` section,
never the user's own Codex config), on which commit (it checks the worktree before and after, and writes
the `HEAD:` line itself), and what counts as a finished review (`parse_verdict`, one retry).

    uv run python tools/sdlc.py codex-review --pr 50 --out <scratch>            # one round on a slice PR
    uv run python tools/sdlc.py codex-review --pr 50 --out <scratch> --dry-run  # the prompt and command only

Exit status: 0 a report was saved (either verdict), 1 a refusal (fix it and run again), `UNAVAILABLE` (3)
Codex can't run: the skill's cue for `review-defer`.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PR_PROMPT = ROOT / ".agents" / "skills" / "work-slice" / "references" / "codex-pr-prompt.md"

VERDICT_RE = re.compile(r"\A[*_`]*VERDICT:[*_`\s]*(approve|changes)[*_`.\s]*\Z", re.IGNORECASE)
HEAD_RE = re.compile(r"^\s*[*_`]*HEAD:[*_`\s]*([0-9a-f]{40})\b", re.MULTILINE | re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"<[A-Z]")
ROUND2_RE = re.compile(r"^- <ROUND2>(.*?)</ROUND2>\n", re.MULTILINE | re.DOTALL)
RESPONSE_HEADING = "### Claude's response"

UNAVAILABLE = 3   # Codex can't run: not on PATH, or no verdict twice (timeouts included)
ATTEMPTS = 2      # the first run, and one retry
CODEX_KEYS = ("model", "reasoning_effort", "sandbox", "timeout_seconds", "flags")
# flags that would override a pinned setting (or the read-only sandbox the HEAD stamp relies on)
FORBIDDEN_FLAGS = ("-m", "--model", "-c", "--config", "-s", "--sandbox", "-p", "--profile", "--oss",
                   "--dangerously", "--approve-for-me", "--add-dir", "--worktree", "-C", "--cd")


class ReviewError(Exception):
    """A review that can't start, or a report that doesn't count. `sdlc.SdlcError` is this class."""


# --- the report ---------------------------------------------------------------

def parse_verdict(report: str) -> str:
    """`approve` or `changes` from the last non-empty line of a Codex report. Anything else is a failed run."""
    lines = [l.strip() for l in (report or "").splitlines() if l.strip()]
    if not lines:
        raise ReviewError("the Codex report is empty: the review did not run")
    m = VERDICT_RE.match(lines[-1])
    if not m:
        raise ReviewError("the Codex report does not end in `VERDICT: approve` or `VERDICT: changes`; "
                          "treat it as a failed run and run the review again")
    return m.group(1).lower()


def stamp(report: str, head: str) -> str:
    """The saved report: the commit the tool checked, then what Codex said."""
    return f"HEAD: {head}\n\n{report.strip()}\n"


# --- settings -----------------------------------------------------------------

def codex_settings(config: dict[str, Any]) -> dict[str, Any]:
    """`.sdlc/config.json`'s `codex` section, checked. There's no fallback to the user's Codex config."""
    s = config.get("codex")
    if not isinstance(s, dict):
        raise ReviewError("`.sdlc/config.json` has no `codex` section (model, reasoning_effort, sandbox, "
                          "timeout_seconds, flags): the repo pins every review setting, so nothing runs without it")
    if missing := [k for k in CODEX_KEYS if k not in s]:
        raise ReviewError(f"`.sdlc/config.json`'s `codex` section lacks {', '.join(missing)}")
    if not all(isinstance(s[k], str) and s[k].strip() for k in ("model", "reasoning_effort")):
        raise ReviewError("`codex.model` and `codex.reasoning_effort` must be non-empty strings")
    if s["sandbox"] != "read-only":
        raise ReviewError(f"`codex.sandbox` is {s['sandbox']!r}: a review runs `read-only`, or its HEAD can't be vouched for")
    t = s["timeout_seconds"]
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 24 * 3600:
        raise ReviewError(f"`codex.timeout_seconds` must be a positive number of seconds (got {t!r})")
    flags = s["flags"]
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        raise ReviewError("`codex.flags` must be a list of strings")
    if bad := [f for f in flags if f.split("=")[0] in FORBIDDEN_FLAGS or f.startswith("--dangerously")]:
        raise ReviewError(f"`codex.flags` may not set {', '.join(bad)}: the model, effort and sandbox have keys of their own")
    return s


def codex_command(settings: dict[str, Any], prompt: str) -> list[str]:
    """The exact `codex exec` argv: every setting explicit, the prompt one argument."""
    return ["codex", "exec", "-m", settings["model"],
            "-c", f'model_reasoning_effort="{settings["reasoning_effort"]}"',
            "--sandbox", settings["sandbox"], *settings["flags"], prompt]


def show_command(cmd: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in cmd[:-1]) + ' "$(cat prompt.md)" < /dev/null'


# --- the prompt ---------------------------------------------------------------

def latest_response(rounds: list[dict[str, Any]]) -> tuple[int, str | None]:
    """(the latest recorded round's number, its "Claude's response") for the next round's prompt.

    `rounds` are the PR's recorded `pr-review` rounds, oldest first, each with its `verdict` and comment
    `body`. The response is the section after the report's closing `</details>`, so a heading quoted inside
    Codex's report is never taken for it. A round that asked for changes must carry one."""
    if not rounds:
        return 0, None
    last = rounds[-1]
    body = last.get("body") or ""
    tail = body[body.rfind("</details>"):] if "</details>" in body else body
    response = None
    if RESPONSE_HEADING in tail:
        response = tail.split(RESPONSE_HEADING, 1)[1].strip() or None
    if response is None and last.get("verdict") == "changes":
        raise ReviewError(f"round {len(rounds)} asked for changes and recorded no response: Codex would never see "
                          "the answers to its findings. Record the round with `pr-review --response`, then run this")
    return len(rounds), response


def fill_pr_prompt(template: str, slice_file: Path, base: str, epic: int, response_file: Path | None) -> str:
    """The text below the template's `---` line, its placeholders replaced (plain string replacement)."""
    if "\n---\n" not in template:
        raise ReviewError("the PR prompt template has no `---` line above the prompt")
    text = template.split("\n---\n", 1)[1].strip() + "\n"
    if not ROUND2_RE.search(text):
        raise ReviewError("the PR prompt template has no `- <ROUND2>...</ROUND2>` bullet")
    text = ROUND2_RE.sub(lambda m: f"- {m.group(1)}\n" if response_file else "", text)
    for key, value in (("<SLICE_FILE>", str(slice_file)), ("<BASE>", base), ("<EPIC>", str(epic)),
                       ("<RESPONSE_FILE>", str(response_file or ""))):
        text = text.replace(key, value)
    if m := PLACEHOLDER_RE.search(text):
        raise ReviewError(f"the filled prompt still has a placeholder: {text[m.start():m.start() + 40]!r}")
    return text


def prepare_pr_round(pr: dict[str, Any], slice_issue: dict[str, Any], epic: int, rounds: list[dict[str, Any]],
                     scratch: Path, template: str | None = None) -> tuple[str, dict[str, Path]]:
    """(the filled prompt, the files written for it) for one round on a slice PR.

    The base is the epic's branch the PR merges into; the brief and the previous round's response are
    written to `scratch`, since Codex's sandbox has no network."""
    base = f"epic/{epic}"
    if pr.get("baseRefName") != base:
        raise ReviewError(f"PR #{pr.get('number')} targets {pr.get('baseRefName')}, not {base}: a slice merges into its epic's branch")
    files = {"brief": scratch / "slice.md", "prompt": scratch / "prompt.md"}
    files["brief"].write_text(f"{slice_issue.get('title', '')}\n\n{slice_issue.get('body', '')}\n")
    k, response = latest_response(rounds)
    if response:
        files["response"] = scratch / f"response-round-{k}.md"
        files["response"].write_text(response + "\n")
    prompt = fill_pr_prompt(PR_PROMPT.read_text() if template is None else template, files["brief"],
                            f"origin/{base}", epic, files.get("response"))
    files["prompt"].write_text(prompt)
    return prompt, files


# --- the run ------------------------------------------------------------------

def git_head(cwd: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def check_worktree(wt: Path, pr_head: str) -> str:
    """The worktree's HEAD, once it's clean and is the PR's head; otherwise say which it isn't."""
    if not (wt / ".git").exists():
        raise ReviewError(f"no worktree at {wt}: `claim N --resume` makes it")
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True).stdout
    if dirty.strip():
        raise ReviewError(f"the worktree {wt} is not clean, so Codex would not review the pushed head: "
                          + ", ".join(l[3:] for l in dirty.splitlines()[:8])
                          + " (commit and push, or keep scratch files outside the worktree)")
    head = git_head(wt)
    if head != pr_head:
        raise ReviewError(f"the worktree's HEAD {head[:12]} is not the PR's head {pr_head[:12]}: "
                          "push, or check out the PR's branch")
    return head


def run_once(cmd: list[str], wt: Path, out: Path, err: Path, timeout: float) -> str | None:
    """Codex's stdout, or None if it timed out (its whole process group is killed)."""
    with out.open("w") as fo, err.open("a") as fe:
        proc = subprocess.Popen(cmd, cwd=wt, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            fe.write(f"\n[codex-review] timed out after {timeout:g}s\n")
            return None
    return out.read_text()


def tail(path: Path, n: int = 20) -> str:
    try:
        return "\n".join(path.read_text().splitlines()[-n:])
    except OSError:
        return ""


def run_review(cmd: list[str], wt: Path, head: str, scratch: Path, timeout: float, log=print) -> tuple[int, Path | None]:
    """Run Codex, retrying once when it times out or ends with no verdict. (status, the stamped report).

    The report is saved only when HEAD is where it was: a review of a commit that moved under it is no
    review. `UNAVAILABLE` is the status for "Codex can't run" (the skill then defers the review)."""
    report, err = scratch / "codex.md", scratch / "codex.err"
    report.unlink(missing_ok=True)
    err.write_text("")
    for attempt in range(1, ATTEMPTS + 1):
        raw = scratch / f"codex-{attempt}.out"
        try:
            output = run_once(cmd, wt, raw, err, timeout)
        except FileNotFoundError:
            log(f"codex is not on the PATH: Codex can't run (exit {UNAVAILABLE}: `review-defer`)")
            return UNAVAILABLE, None
        if (now := git_head(wt)) != head:
            raise ReviewError(f"HEAD moved from {head[:12]} to {now[:12]} while Codex ran: nothing it said "
                              "can be bound to a commit, so no report was saved. Run the review again")
        if output is None:
            log(f"attempt {attempt}: Codex timed out after {timeout:g}s")
            continue
        try:
            parse_verdict(output)
        except ReviewError as exc:
            log(f"attempt {attempt}: {exc}")
            continue
        report.write_text(stamp(output, head))
        return 0, report
    log(f"Codex gave no verdict in {ATTEMPTS} attempts (exit {UNAVAILABLE}: `review-defer`). "
        f"The end of {err}:\n{tail(err)}")
    return UNAVAILABLE, None
