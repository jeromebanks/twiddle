"""How a Codex review runs: everything that decides its behaviour, in one stdlib-only module.

`tools/sdlc.py` does the GitHub reads, the posting and the gates; this module decides what Codex is asked
(the prompt, filled from the repo's template), with which settings (`.sdlc/config.json`'s `codex` section,
never the user's own Codex config), on which commit (it checks the worktree before and after, and writes
the `HEAD:` line itself), and what counts as a finished review (`parse_verdict`, one retry). Codex runs on a
scratch `CODEX_HOME` holding only a copy of the user's sign-in and a `config.toml` written from those settings
(`isolated_home`), so no user-level instructions, skills, memories, rules, plugins or MCP servers apply; the
saved report names the model, effort, `codex --version` and the prompt's sha256 under its `HEAD:` line.

    uv run python tools/sdlc.py codex-review --pr 50 --out <scratch>            # one round on a slice PR
    uv run python tools/sdlc.py codex-review --pr 50 --out <scratch> --dry-run  # the prompt and command only
    uv run python tools/sdlc.py codex-review --plan 12 --out <scratch>          # the posted plan revision
    uv run python tools/sdlc.py codex-review --milestone 12 --out <scratch>     # main...epic/12, from .worktrees/epic-12

It is the only way the skills run Codex: never `codex exec review`, the Codex plugin, or `codex exec` by hand.

Exit status: 0 a report was saved (either verdict), 1 a refusal (fix it and run again), `UNAVAILABLE` (3)
Codex can't run: the skill's cue for `review-defer`.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PR_PROMPT = ROOT / ".agents" / "skills" / "work-slice" / "references" / "codex-pr-prompt.md"
PLAN_PROMPT = ROOT / ".agents" / "skills" / "plan-issue" / "references" / "codex-plan-prompt.md"
MILESTONE_PROMPT = ROOT / ".agents" / "skills" / "milestone-demo" / "references" / "codex-milestone-prompt.md"

VERDICT_RE = re.compile(r"\A[*_`]*VERDICT:[*_`\s]*(approve|changes)[*_`.\s]*\Z", re.IGNORECASE)
HEAD_RE = re.compile(r"^\s*[*_`]*HEAD:[*_`\s]*([0-9a-f]{40})\b", re.MULTILINE | re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"<[A-Z]")
NAME_RE = re.compile(r"<([A-Z][A-Z0-9_]*)>")
# How `pr-review` records a round: Codex's report folded in <details>, then the implementer's answer after
# RESPONSE_MARK, a boundary `pr-review` removes from the report and the response first, so its first occurrence
# is the real one; its marker says whether there is one (`response=1|0`). Rounds recorded before that carry no
# `response` attribute and fall back to the heading that follows the report (LEGACY_MARK).
RESPONSE_MARK = "<!-- sdlc:response -->"
RESPONSE_HEADING = "### Claude's response"
LEGACY_MARK = f"</details>\n\n{RESPONSE_HEADING}\n\n"

UNAVAILABLE = 3   # Codex can't run: not on PATH, no sign-in, or no verdict twice (timeouts included)
ATTEMPTS = 2      # the first run, and one retry
CODEX_KEYS = ("model", "reasoning_effort", "sandbox", "timeout_seconds", "flags")
# the only `codex exec` flags `codex.flags` may add: none overrides a pinned setting, loosens the read-only
# sandbox the HEAD stamp relies on, or changes what goes to stdout (the report)
ALLOWED_FLAGS = {"--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--strict-config"}
COLORS = {"never", "auto", "always"}


class ReviewError(Exception):
    """A review that can't start, or a report that doesn't count. `sdlc.SdlcError` is this class."""


class CannotStart(Exception):
    """`codex` couldn't be launched at all: not on the PATH, or not executable."""


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


# what a round ran under, the lines right below `HEAD:` (`stamp`); only that block is read back, so a report
# can't claim a model of its own
PROVENANCE = {"model": "Model", "effort": "Effort", "codex": "Codex", "prompt": "Prompt-SHA256"}


def provenance(settings: dict[str, Any], version: str, prompt: str) -> dict[str, str]:
    return {"model": settings["model"], "effort": settings["reasoning_effort"], "codex": version,
            "prompt": hashlib.sha256(prompt.encode()).hexdigest()}


def stamp(report: str, head: str, prov: dict[str, str] | None = None) -> str:
    """The saved report: the commit the tool checked and what the run ran under, then what Codex said."""
    lines = [f"HEAD: {head}"] + [f"{label}: {prov[k]}" for k, label in PROVENANCE.items() if prov and prov.get(k)]
    return "\n".join(lines) + f"\n\n{report.strip()}\n"


def parse_provenance(report: str) -> dict[str, str]:
    """The provenance block `stamp` wrote right under the `HEAD:` line; {} for a report saved before it had one."""
    m = HEAD_RE.search(report or "")
    if not m:
        return {}
    labels = {label.lower(): k for k, label in PROVENANCE.items()}
    found = {}
    for line in report[m.end():].split("\n")[1:]:
        key, sep, value = line.partition(":")
        if not sep or key.strip().lower() not in labels or not value.strip():
            break
        found[labels[key.strip().lower()]] = value.strip()
    return found


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
    bad, i = [], 0
    while i < len(flags):
        f = flags[i]
        if f == "--color" and i + 1 < len(flags) and flags[i + 1] in COLORS:
            i += 1
        elif not (f in ALLOWED_FLAGS or (f.startswith("--color=") and f[8:] in COLORS)):
            bad.append(f)
        i += 1
    if bad:
        raise ReviewError(f"`codex.flags` may not pass {', '.join(bad)}: only {', '.join(sorted(ALLOWED_FLAGS))} and "
                          "`--color`; the model, effort and sandbox have keys of their own")
    return s


def codex_command(settings: dict[str, Any], prompt: str) -> list[str]:
    """The exact `codex exec` argv: every setting explicit, the prompt one argument."""
    return ["codex", "exec", "-m", settings["model"],
            "-c", f'model_reasoning_effort="{settings["reasoning_effort"]}"',
            "--sandbox", settings["sandbox"], *settings["flags"], prompt]


def config_toml(settings: dict[str, Any]) -> str:
    """The scratch `CODEX_HOME`'s whole `config.toml`: the repo's settings and nothing of the user's. The sign-in
    is read from the copy `isolated_home` writes (`file` is Codex's default store; pinned, so no keyring applies)."""
    q = json.dumps                      # a JSON string is a TOML basic string
    return (f"model = {q(settings['model'])}\nmodel_reasoning_effort = {q(settings['reasoning_effort'])}\n"
            f"sandbox_mode = {q(settings['sandbox'])}\ncli_auth_credentials_store = \"file\"\n")


def show_command(cmd: list[str], wt: Path, prompt_file: Path) -> str:
    """The command as a shell line, the prompt read from its file."""
    return (f"cd {shlex.quote(str(wt))} && " + " ".join(shlex.quote(a) for a in cmd[:-1])
            + f' "$(cat {shlex.quote(str(prompt_file))})" < /dev/null')


# --- the prompt ---------------------------------------------------------------

def latest_response(rounds: list[dict[str, Any]], record: str = "pr-review") -> tuple[int, str | None]:
    """(the latest recorded round's number, its "Claude's response") for the next round's prompt.

    `rounds` are the recorded rounds (`pr-review`, `plan-review` or `ship-review`, named by `record`), oldest
    first, each with its `verdict`, comment `body` and marker's `response` attribute. A round marked
    `response=0` has none, whatever its report quotes; one marked `response=1` is read after `RESPONSE_MARK`,
    where `response_section` puts it. A round that asked for changes must carry one."""
    if not rounds:
        return 0, None
    last = rounds[-1]
    body, has = last.get("body") or "", last.get("response")
    response, i = None, body.find(RESPONSE_MARK)
    at_boundary = i >= 0 and body[:i].endswith("</details>\n\n") and body.startswith(f"{RESPONSE_MARK}\n{RESPONSE_HEADING}", i)
    if has != "0" and at_boundary:
        response = body[i + len(RESPONSE_MARK):].strip().removeprefix(RESPONSE_HEADING).strip() or None
    elif has is None and LEGACY_MARK in body:     # recorded before the boundary existed
        response = body.split(LEGACY_MARK, 1)[1].strip() or None
    if response is None and last.get("verdict") == "changes":
        raise ReviewError(f"round {len(rounds)} asked for changes and recorded no response: Codex would never see "
                          f"the answers to its findings. Record the round with `{record} --response`, then run this")
    return len(rounds), response


def unmarked(text: str) -> str:
    """Text that can't be mistaken for a round's response boundary."""
    return text.replace(RESPONSE_MARK, "")


def response_section(response: str) -> str:
    """What a recorded round appends after the folded report: the boundary, then the implementer's answer."""
    return f"\n\n{RESPONSE_MARK}\n{RESPONSE_HEADING}\n\n{unmarked(response).strip()}"


def fill_prompt(template: str, values: dict[str, str], keep: dict[str, bool]) -> str:
    """The text below the template's `---` line, filled. Each `- <TAG>...</TAG>` bullet named in `keep` stays
    (its tags dropped) or goes; then every `<NAME>` is replaced from `values`, in one pass, so a value is never
    read as a placeholder itself. A placeholder left over, or a bullet the template lacks, is refused."""
    if "\n---\n" not in template:
        raise ReviewError("the prompt template has no `---` line above the prompt")
    text = template.split("\n---\n", 1)[1].strip() + "\n"
    for tag, kept in keep.items():
        bullet = re.compile(rf"^- <{tag}>(.*?)</{tag}>\n", re.MULTILINE | re.DOTALL)
        if not bullet.search(text):
            raise ReviewError(f"the prompt template has no `- <{tag}>...</{tag}>` bullet")
        text = bullet.sub(lambda m: f"- {m.group(1)}\n" if kept else "", text)
    rest = NAME_RE.sub(lambda m: "" if m.group(1) in values else m.group(0), text)
    if m := PLACEHOLDER_RE.search(rest):
        raise ReviewError(f"the filled prompt still has a placeholder: {rest[m.start():m.start() + 40]!r}")
    return NAME_RE.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), text)


def fill_pr_prompt(template: str, slice_file: Path, base: str, epic: int, response_file: Path | None) -> str:
    return fill_prompt(template, {"SLICE_FILE": str(slice_file), "BASE": base, "EPIC": str(epic),
                                  "RESPONSE_FILE": str(response_file or "")}, {"ROUND2": bool(response_file)})


def _write_response(rounds: list[dict[str, Any]], record: str, scratch: Path, files: dict[str, Path]) -> int:
    """The latest round's response, written for Codex to read; the next round's number."""
    k, response = latest_response(rounds, record)
    if response:
        files["response"] = scratch / f"response-round-{k}.md"
        files["response"].write_text(response + "\n")
    return k + 1


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
    _write_response(rounds, "pr-review", scratch, files)
    prompt = fill_pr_prompt(PR_PROMPT.read_text() if template is None else template, files["brief"],
                            f"origin/{base}", epic, files.get("response"))
    files["prompt"].write_text(prompt)
    return prompt, files


def prepare_plan_round(issue: int, plan: dict[str, Any], requirements: str | Path, rounds: list[dict[str, Any]],
                       scratch: Path, template: str | None = None) -> tuple[str, dict[str, Path]]:
    """(the filled prompt, the files written for it) for one round on a posted plan revision.

    `plan` is the JSON as posted (written to `plan.json`, which `plan-review --plan` takes); `requirements` is
    the approved revision: a path Codex can read from its checkout of main (a `docs/prd/` file), or the
    approved comment's text, written to `requirements.md`."""
    files = {"plan": scratch / "plan.json", "prompt": scratch / "prompt.md"}
    files["plan"].write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
    if isinstance(requirements, Path):
        req = str(requirements)
    else:
        files["requirements"] = scratch / "requirements.md"
        files["requirements"].write_text(requirements.strip() + "\n")
        req = str(files["requirements"])
    k = _write_response(rounds, "plan-review", scratch, files)
    prompt = fill_prompt(PLAN_PROMPT.read_text() if template is None else template,
                         {"N": str(issue), "PLAN_FILE": str(files["plan"]), "PRD_FILE": req, "K": str(k),
                          "RESPONSE_FILE": str(files.get("response", ""))}, {"ROUND2": "response" in files})
    files["prompt"].write_text(prompt)
    return prompt, files


def _brief(s: dict[str, Any]) -> str:
    return f"{s.get('title', '')}\n\n{s.get('body', '')}\n"


def section(body: str, name: str) -> str:
    """One `## <name>` section of a slice issue's body."""
    m = re.search(rf"^## {re.escape(name)}\s*\n(.*?)(?=^## |\Z)", body or "", re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def prepare_milestone_round(epic: int, milestone: dict[str, Any], slices: list[dict[str, Any]],
                            owed: list[dict[str, Any]], rounds: list[dict[str, Any]], scratch: Path,
                            template: str | None = None) -> tuple[str, dict[str, Path]]:
    """(the filled prompt, the files written for it) for one round on a milestone's diff.

    `milestone` is {key, title, steps}; `slices` are its merged slices, each {number, key, title, body, pr,
    merge_commit} (the squash commit on `epic/N`); `owed` are the slices `merge` recorded as merging without a
    review of their own ({number, slice, why}). Each slice's brief goes in `slice-<number>.md`, and
    `milestone.md` holds the demo steps and each slice's Outcome and Acceptance criteria."""
    by_number = {str(s["number"]): s for s in slices}
    if missing := [s for s in slices if not s.get("merge_commit")]:
        raise ReviewError("no squash commit on the epic branch for " + ", ".join(f"#{s['number']} {s['key']}" for s in missing)
                          + ": the milestone's slices must all be merged")
    if stray := [o for o in owed if str(o.get("number")) not in by_number]:
        raise ReviewError("a review is owed for " + ", ".join(f"{o.get('slice')} (#{o.get('number')})" for o in stray)
                          + f", which isn't a merged slice of {milestone['key']}: nothing to point Codex at")
    files = {"milestone": scratch / "milestone.md", "prompt": scratch / "prompt.md"}
    # an epic without milestones (most bugs) has no milestone description: its slices' Demo sections are the steps
    steps = (milestone.get("steps") or "").strip() or "\n".join(
        f"- {s['key']}: {d}" for s in slices if (d := section(s.get("body", ""), "Demo"))) or "(none given)"
    lines = [f"# {milestone['title']}", "", "## Demo steps", "", steps, ""]
    for s in slices:
        brief = files[f"slice-{s['number']}"] = scratch / f"slice-{s['number']}.md"
        brief.write_text(_brief(s))
        lines += [f"## {s['key']} (#{s['number']}, PR #{s.get('pr')}, squash {s['merge_commit']}): {s.get('title', '')}",
                  "", f"The whole brief: {brief}", "", "### Outcome", "", section(s.get("body", ""), "Outcome") or "(none)",
                  "", "### Acceptance criteria", "", section(s.get("body", ""), "Acceptance criteria") or "(none)", ""]
    files["milestone"].write_text("\n".join(lines))

    def one(s: dict[str, Any]) -> str:
        return f"#{s['number']} {s['key']} (PR #{s.get('pr')}, squash `{s['merge_commit']}`, brief `{files['slice-' + str(s['number'])]}`)"
    why = {"routine": "routine", "unavailable": "Codex couldn't run on its PR"}
    deferred = [f"{one(by_number[str(o['number'])])}: {why.get(o.get('why'), o.get('why') or '?')}" for o in owed]
    _write_response(rounds, "ship-review", scratch, files)
    prompt = fill_prompt(MILESTONE_PROMPT.read_text() if template is None else template,
                         {"N": str(epic), "MILESTONE_FILE": str(files["milestone"]),
                          "SLICES": "; ".join(one(s) for s in slices), "DEFERRED_SLICES": "; ".join(deferred),
                          "RESPONSE_FILE": str(files.get("response", ""))},
                         {"DEFERRED": bool(deferred), "ROUND2": "response" in files})
    files["prompt"].write_text(prompt)
    return prompt, files


# --- the isolated CODEX_HOME -----------------------------------------------------

def user_codex_home() -> Path:
    """Where the user's own Codex keeps its sign-in: `CODEX_HOME` if they set one, else `~/.codex`."""
    return Path(os.environ["CODEX_HOME"]).expanduser() if os.environ.get("CODEX_HOME") else Path.home() / ".codex"


def sign_in() -> Path:
    """The user's `auth.json`, resolved (a dotfiles link stays a link); refused when there is none."""
    auth = user_codex_home() / "auth.json"
    if not auth.is_file():
        raise ReviewError(f"no Codex sign-in at {auth}: run `codex login` in a terminal, then run this again. "
                          "A review runs on a scratch CODEX_HOME given a copy of this file, so a sign-in kept only "
                          "in the keyring (`cli_auth_credentials_store`) can't be used")
    return auth.resolve()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _is_sign_in(data: bytes) -> bool:
    """A whole `auth.json` (a JSON object), not one an interrupted write left half-written."""
    try:
        return isinstance(json.loads(data), dict)
    except ValueError:
        return False


def _write_atomic(target: Path, data: bytes, unless_changed_from: str | None = None) -> bool:
    """`target` holds `data` or what it held before, never half of it: a 0600 temp file beside it, renamed over it.

    With `unless_changed_from` (a sha256), `target` is re-read once the temp file is ready, just before the rename,
    and left alone (False) if it no longer matches. That is as late as a check can go: `rename` has no
    compare-and-swap, so a write landing between that read and the rename can still be lost."""
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if unless_changed_from is not None and ((now := _read(target)) is None or _sha(now) != unless_changed_from):
            os.unlink(tmp)
            return False
        os.replace(tmp, target)
        return True
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def restore_sign_in(entry: Path, user_auth: Path, start: str, log=print) -> None:
    """After a run, bring a sign-in Codex refreshed in its private copy back to the user's own `auth.json`, then
    drop the copy. Written back atomically, and only when the copy changed, is a whole `auth.json`, and the user's
    is still what it was when the run started (`start`, its sha256): a sign-in made elsewhere meanwhile is never
    overwritten, and a half-written refresh never reaches it."""
    if not entry.exists():
        log(f"warning: Codex removed {entry} during the run; your sign-in at {user_auth} was left as it was")
        return
    data = entry.read_bytes()
    if _sha(data) == start:
        entry.unlink()
        return
    if not _is_sign_in(data):
        log(f"warning: Codex's refreshed sign-in was left half-written (the run was cut short?), so {user_auth} "
            "was kept as it was; if `codex` says to sign in, run `codex login`")
        entry.unlink()
        return
    try:
        written = _write_atomic(user_auth, data, unless_changed_from=start)
    except OSError as exc:
        log(f"warning: couldn't copy Codex's refreshed sign-in back to {user_auth} ({exc}); it is still in {entry}: "
            f"copy it over yourself, or run `codex login`")
        return
    if not written:
        log(f"warning: Codex refreshed its sign-in during the run, but {user_auth} changed meanwhile (a sign-in "
            "elsewhere?), so it was kept and the refreshed token was discarded")
    entry.unlink()


@contextlib.contextmanager
def isolated_home(scratch: Path, settings: dict[str, Any], user_auth: Path, log=print):
    """A fresh `<scratch>/codex-home` holding only `auth.json` (a private 0600 copy of `user_auth`, from `sign_in`)
    and the generated `config.toml`; yields the environment Codex runs with.

    A copy, not a link: Codex's file store refreshes `auth.json` in place (open, truncate, write), which through a
    link would land in the user's file unguarded. However the run ends, `restore_sign_in` reconciles the copy and
    removes it; the rest stays, so the session log can be read afterwards."""
    home = scratch / "codex-home"
    entry = home / "auth.json"
    data = user_auth.read_bytes()             # read once: the copy and the start hash are the same bytes
    if entry.is_symlink() or (entry.exists() and entry.read_bytes() == data):
        entry.unlink()                        # what a killed run leaves when nothing was refreshed
    elif entry.exists():
        raise ReviewError(f"{entry} holds a sign-in an earlier run couldn't copy back: copy it over {user_auth} "
                          "(or delete it and run `codex login`), then run this again")
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    (home / "config.toml").write_text(config_toml(settings))
    fd = os.open(entry, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        yield {**os.environ, "CODEX_HOME": str(home)}
    finally:
        try:
            restore_sign_in(entry, user_auth, _sha(data), log)
        except OSError as exc:          # never hide how the run itself ended
            log(f"warning: couldn't check Codex's sign-in after the run ({exc}): look at {entry} and {user_auth}")


def codex_version(env: dict[str, str]) -> str:
    """`codex --version`'s version number (`codex-cli 0.160.0` -> `0.160.0`), or `unknown`. `CannotStart` when
    there is no `codex` to ask."""
    try:
        proc = subprocess.run(["codex", "--version"], cwd=env["CODEX_HOME"], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
    except OSError as exc:
        raise CannotStart(str(exc)) from exc
    except subprocess.TimeoutExpired:
        return "unknown"
    words = proc.stdout.split()
    return words[-1] if proc.returncode == 0 and words else "unknown"


# --- the run ------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ReviewError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout


def dirty_files(wt: Path) -> list[str]:
    """What `git status` says isn't committed (the porcelain lines' paths; their leading status column kept intact)."""
    return [l[3:] for l in _git(["status", "--porcelain"], wt).splitlines() if l.strip()]


def git_head(cwd: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd).strip()


def check_worktree(wt: Path, pr_head: str, what: str = "the PR's head", make: str = "`claim N --resume`") -> str:
    """The worktree's HEAD, once it's clean and is `what` (the PR's head, or epic/N's); otherwise say which it isn't."""
    if not (wt / ".git").exists():
        raise ReviewError(f"no worktree at {wt}: {make} makes it")
    if dirty := dirty_files(wt):
        raise ReviewError(f"the worktree {wt} is not clean, so Codex would not review the pushed head: "
                          + ", ".join(dirty[:8])
                          + " (commit and push, or keep scratch files outside the worktree)")
    head = git_head(wt)
    if head != pr_head:
        raise ReviewError(f"the worktree's HEAD {head[:12]} is not {what} {pr_head[:12]}: "
                          + ("push, or check out the PR's branch" if what == "the PR's head" else f"{make} brings it up to date"))
    return head


@contextlib.contextmanager
def _sigterm_interrupts():
    """While Codex runs, SIGTERM interrupts the wait like Ctrl-C does, so the cleanup below still runs."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    old = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def run_once(cmd: list[str], wt: Path, out: Path, err: Path, timeout: float, env: dict[str, str] | None = None) -> str | None:
    """Codex's stdout, or None if it timed out. Codex runs in its own process group, so neither Ctrl-C nor a
    SIGTERM to this process reaches it: however the wait ends, a group still running is killed."""
    with out.open("w") as fo, err.open("a") as fe:
        try:
            proc = subprocess.Popen(cmd, cwd=wt, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                                    start_new_session=True)
        except OSError as exc:
            raise CannotStart(str(exc)) from exc
        try:
            with _sigterm_interrupts():
                proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            fe.write(f"\n[codex-review] timed out after {timeout:g}s\n")
            return None
        finally:
            if proc.poll() is None:
                # the group can hold a sandbox helper this user may not signal (EPERM): then at least Codex itself
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                proc.wait()
    return out.read_text()


def tail(path: Path, n: int = 20) -> str:
    try:
        return "\n".join(path.read_text().splitlines()[-n:])
    except OSError:
        return ""


def run_review(cmd: list[str], wt: Path, head: str, scratch: Path, settings: dict[str, Any],
               log=print) -> tuple[int, Path | None]:
    """Run Codex on an isolated `CODEX_HOME`, retrying once when it times out or ends with no verdict.
    (status, the stamped report).

    The report is saved only when HEAD is where it was: a review of a commit that moved under it is no
    review. `UNAVAILABLE` is the status for "Codex can't run" (the skill then defers the review): no `codex`,
    or no sign-in, both found before Codex is started."""
    report, err = scratch / "codex.md", scratch / "codex.err"
    report.unlink(missing_ok=True)
    err.write_text("")
    timeout = settings["timeout_seconds"]
    try:
        user_auth = sign_in()
    except ReviewError as exc:
        log(f"{exc} (exit {UNAVAILABLE}: `review-defer`)")
        return UNAVAILABLE, None
    # SIGTERM anywhere in here, not only while Codex runs, still reconciles the sign-in on the way out
    with _sigterm_interrupts(), isolated_home(scratch, settings, user_auth, log) as env:
        try:
            prov = provenance(settings, codex_version(env), cmd[-1])
        except CannotStart as exc:
            log(f"codex can't be started ({exc}): Codex can't run (exit {UNAVAILABLE}: `review-defer`)")
            return UNAVAILABLE, None
        for attempt in range(1, ATTEMPTS + 1):
            raw = scratch / f"codex-{attempt}.out"
            try:
                output = run_once(cmd, wt, raw, err, timeout, env)
            except CannotStart as exc:
                log(f"codex can't be started ({exc}): Codex can't run (exit {UNAVAILABLE}: `review-defer`)")
                return UNAVAILABLE, None
            if (now := git_head(wt)) != head:
                raise ReviewError(f"HEAD moved from {head[:12]} to {now[:12]} while Codex ran: nothing it said "
                                  "can be bound to a commit, so no report was saved. Run the review again")
            if dirty := dirty_files(wt):
                raise ReviewError("the worktree changed while Codex ran (" + ", ".join(dirty[:8])
                                  + "): it didn't review the pushed head, so no report was saved. Restore it and run again")
            if output is None:
                log(f"attempt {attempt}: Codex timed out after {timeout:g}s")
                continue
            try:
                parse_verdict(output)
            except ReviewError as exc:
                log(f"attempt {attempt}: {exc}")
                continue
            report.write_text(stamp(output, head, prov))
            return 0, report
    log(f"Codex gave no verdict in {ATTEMPTS} attempts (exit {UNAVAILABLE}: `review-defer`). "
        f"The end of {err}:\n{tail(err)}")
    return UNAVAILABLE, None
