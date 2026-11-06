"""What a watcher run executes, and with which tools (stdlib only, no twiddle imports; runs nothing).

Three parts, each pure but for a few `git` reads:

- `parse_next`: a `next:` line from `tools/sdlc.py` -> a `Step`: one of the four SDLC skills
  as a slash command + its model, one `sdlc.py` command from a fixed mapping (`sync N`,
  `merge PR`, `commit-prd N`), or a skip with its reason (kept for `status`).
- `read_pinned_tools`: `.sdlc/watch-tools.json` from the pinned commit, never the working tree,
  refused unless the pin is reachable from `origin/main` or an `origin/epic/*`.
- `build_argv`: the `claude -p` command line for a skill step: the fixed tool list, with the
  run's issue, primary checkout and scratch dir filled in, under a permission mode that denies
  anything unlisted. Never `--bare`, a budget, a turn cap or a permission skip.

A headless run can't answer a prompt, so the tool list does the job a person's "allow" would.
Claude Code matches a Bash rule against each simple command of a compound one, and its `*`
matches any text, spaces included (`bash_rule_matches` models that, for the tests and for
`status`): so `git push origin slice/*` would also allow `git push origin slice/1 main`. A push
or a label edit is therefore never a wildcard here, but an exact rule for the run's own issue.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOLS_FILE = ".sdlc/watch-tools.json"
SKILLS = ("triage-issue", "plan-issue", "work-slice", "milestone-demo")
SDLC = ("uv", "run", "python", "tools/sdlc.py")
MAPPED = ("sync", "merge", "commit-prd")         # the only non-slash `next:` steps that run, as that one command
# never on a command line the watcher builds (PRD criteria 7 and 18)
FORBIDDEN_ARGS = ("--bare", "--max-budget-usd", "--max-turns", "--dangerously-skip-permissions",
                  "--allow-dangerously-skip-permissions", "bypassPermissions")
PERMISSION_MODE = "dontAsk"                       # anything no rule allows is denied, with no prompt
# hooks can approve a call under dontAsk, and MCP tools aren't covered by --tools: neither is loaded
SETTINGS = json.dumps({"disableAllHooks": True})
SETTING_SOURCES = "project"                       # the repo's skills; never the user's allow rules or hooks
GIT_TIMEOUT_S = 30

SLASH = re.compile(r"/(?P<skill>[a-z-]+) (?P<n>\d+)(?P<flags>(?: --(?:quick|debt))?)"
                   r"(?: \((?P<model>[a-z][a-z0-9.-]*): (?P<complexity>[a-z]+)\))?(?P<rest>:\s.*)?")
MAPPED_STEP = re.compile(r"(?P<q>`?)uv run python tools/sdlc\.py (?P<verb>[a-z-]+) (?P<n>\d+)(?P=q)"
                         r"(?P<rest>(?::\s| \().*)?")


class WatchCmdError(ValueError):
    """The tool list, its pin or a run's paths can't be used: nothing is run."""


@dataclass(frozen=True)
class Step:
    """What a `next:` line asks for. `kind`: `skill` (a `claude -p` run), `sdlc` (one mapped command) or `skip`."""
    kind: str
    text: str                                     # the `next:` line as given, for status and the log
    issue: int | None = None
    skill: str = ""
    flags: tuple[str, ...] = ()
    model: str | None = None                      # from the line's parenthesis; None: the config's default
    argv: tuple[str, ...] = ()                    # a mapped sdlc.py command
    reason: str = ""                              # why a skip runs nothing

    @property
    def command(self) -> str:
        """The slash command a skill run is given as its prompt, e.g. `/work-slice 160 --quick`."""
        return " ".join([f"/{self.skill}", str(self.issue), *self.flags]) if self.kind == "skill" else ""


def parse_next(line: str) -> Step:
    """A `next:` line -> a Step. Only a leading slash command or a leading mapped command runs; anything else
    (`nothing new to start: ...`, a poster's move, `ship`, `verify-main`, `state`) is a skip with its text kept."""
    text = line.strip()
    if text.startswith("next:"):
        text = text[len("next:"):].strip()
    if m := SLASH.fullmatch(text):
        flags = tuple(m["flags"].split())
        if m["skill"] not in SKILLS:
            return Step("skip", text, reason=f"/{m['skill']} is not one of the watcher's skills")
        if flags and m["skill"] != "work-slice":
            return Step("skip", text, reason=f"{flags[0]} is only a /work-slice flag")
        return Step("skill", text, int(m["n"]), m["skill"], flags, m["model"])
    if m := MAPPED_STEP.fullmatch(text):
        if m["verb"] in MAPPED:
            return Step("sdlc", text, int(m["n"]), argv=(*SDLC, m["verb"], m["n"]))
        return Step("skip", text, reason=f"`sdlc.py {m['verb']}` is not in the watcher's mapping ({', '.join(MAPPED)})")
    if text.startswith("/"):
        return Step("skip", text, reason="a slash command the watcher can't read")
    return Step("skip", text, reason="not a command: nothing for the watcher to run")


def resolve_model(step: Step, default: str) -> str:
    """The model a skill run gets: the one `next:` names, else the config's default."""
    if not default and not step.model:
        raise WatchCmdError("no model: the `next:` line names none and there is no default")
    return step.model or default


# --- the tool list --------------------------------------------------------------------------------------------

LIST_KEYS = ("skills", "tools", "allow", "bash", "bash_per_issue", "bash_per_epic", "edit", "deny")


def load_tools(text: str) -> dict[str, Any]:
    """`.sdlc/watch-tools.json`'s text, checked: the four skills exactly, no permission skip, no bare `uv run *`."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WatchCmdError(f"{TOOLS_FILE} is not JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise WatchCmdError(f"{TOOLS_FILE}: schema 1 expected")
    for k in LIST_KEYS:
        if not isinstance(data.get(k), list) or not all(isinstance(x, str) and x for x in data[k]):
            raise WatchCmdError(f"{TOOLS_FILE}: `{k}` must be a list of strings")
    if not isinstance(data.get("demo_shot"), str):
        raise WatchCmdError(f"{TOOLS_FILE}: `demo_shot` must be a string")
    if tuple(data["skills"]) != SKILLS:
        raise WatchCmdError(f"{TOOLS_FILE}: `skills` must be exactly {', '.join(SKILLS)}")
    for rule in [*data["bash"], *data["bash_per_issue"], *data["bash_per_epic"], data["demo_shot"]]:
        if rule.strip() in ("*", "uv *", "uv run *") or any(a in rule for a in FORBIDDEN_ARGS):
            raise WatchCmdError(f"{TOOLS_FILE}: refused rule `{rule}`")
    if any(t in ("Bash", "Edit", "Write", "Skill") or "(" in t for t in data["allow"]):
        raise WatchCmdError(f"{TOOLS_FILE}: `allow` takes only plain tools with no command or path to scope")
    return data


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S)


def pin_errors(primary: Path, pin: str) -> list[str]:
    """Why `pin` can't be the commit a run's permissions come from (empty: it can). It must name a commit
    reachable from `origin/main` or an `origin/epic/*`, as the local remote-tracking refs say: pushed."""
    if not re.fullmatch(r"[0-9a-f]{40}", pin or ""):
        return [f"the pin {pin!r} is not a full commit SHA"]
    if git(["cat-file", "-e", f"{pin}^{{commit}}"], primary).returncode:
        return [f"the pin {pin} is not a commit in {primary}"]
    refs = git(["for-each-ref", "--format=%(refname)", "refs/remotes/origin/main", "refs/remotes/origin/epic/"],
               primary).stdout.split()
    if not any(git(["merge-base", "--is-ancestor", pin, r], primary).returncode == 0 for r in refs):
        return [f"the pin {pin} is not pushed: no origin/main or origin/epic/* contains it"]
    return []


def read_pinned_tools(primary: Path, pin: str) -> dict[str, Any]:
    """The tool list as committed at the pin (`git show <pin>:.sdlc/watch-tools.json`), never the working tree."""
    if errs := pin_errors(primary, pin):
        raise WatchCmdError("; ".join(errs))
    shown = git(["show", f"{pin}:{TOOLS_FILE}"], primary)
    if shown.returncode:
        raise WatchCmdError(f"the pin {pin} has no {TOOLS_FILE}")
    return load_tools(shown.stdout)


def demo_shot_matches_main(primary: Path) -> bool:
    """The primary checkout's `tools/demo_shot.py` is byte for byte `origin/main`'s (T0.2's guard is in it)."""
    path = primary / "tools" / "demo_shot.py"
    if not path.is_file():
        return False
    here = git(["hash-object", str(path)], primary)
    main = git(["rev-parse", "origin/main:tools/demo_shot.py"], primary)
    return here.returncode == 0 and main.returncode == 0 and here.stdout.strip() == main.stdout.strip()


# --- the command line -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rules:
    """The filled-in tool list of one run."""
    tools: tuple[str, ...]                        # --tools: what exists in the session
    allowed: tuple[str, ...]                      # --allowedTools: what runs with no prompt
    bash: tuple[str, ...] = field(default=())     # the Bash patterns among them, unwrapped
    edit: tuple[str, ...] = field(default=())     # the Edit paths among them (`//abs/**`)
    deny: tuple[str, ...] = field(default=())     # --disallowedTools' Bash patterns: they win over any allow


def fill(rule: str, values: dict[str, str]) -> str:
    out = rule
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    if re.search(r"\{[A-Z]+\}", out):
        raise WatchCmdError(f"{TOOLS_FILE}: unknown placeholder in `{rule}`")
    return out


def check_paths(primary: Path, scratch: Path) -> None:
    """Both absolute; the scratch dir neither the primary checkout, inside it, nor above it (an Edit rule
    for it would then cover `.claude/`, `.sdlc/` or `CLAUDE.md`)."""
    for name, p in (("primary checkout", primary), ("scratch dir", scratch)):
        if not p.is_absolute() or any(part in ("..", ".") for part in p.parts):
            raise WatchCmdError(f"the {name} must be an absolute, normalised path: {p}")
    if scratch == primary or primary in scratch.parents or scratch in primary.parents:
        raise WatchCmdError(f"the scratch dir {scratch} must be outside the primary checkout {primary}")


def build_rules(tools: dict[str, Any], issue: int, *, primary: Path, scratch: Path, demo_shot: bool,
                epic: int | None = None) -> Rules:
    """The run's rules: `issue` is the one its command names, `epic` the epic in care (None: its rules dropped)."""
    check_paths(primary, scratch)
    values = {"PRIMARY": str(primary), "SCRATCH": str(scratch), "N": str(issue),
              "WORKTREE": str(primary / ".worktrees" / f"slice-{issue}")}
    bash = [fill(r, values) for r in tools["bash"] + tools["bash_per_issue"]]
    if epic is not None:
        bash += [fill(r, {**values, "E": str(epic)}) for r in tools["bash_per_epic"]]
    if demo_shot:
        bash.append(fill(tools["demo_shot"], values))
    filled = [fill(r, values) for r in tools["edit"]]
    if bad := [e for e in filled if not e.startswith("/") or e.startswith("//")]:
        raise WatchCmdError(f"{TOOLS_FILE}: an edit path must be absolute once filled: {bad[0]}")
    edit = ["/" + e for e in filled]   # `//abs`: from the filesystem root (one `/` is the settings source's)
    allowed = (*tools["allow"], *(f"Skill({s})" for s in tools["skills"]),
               *(f"Bash({b})" for b in bash), *(f"Edit({e})" for e in edit))
    deny = tuple(fill(r, values) for r in tools["deny"])
    return Rules(tuple(tools["tools"]), allowed, tuple(bash), tuple(edit), deny)


def build_argv(step: Step, tools: dict[str, Any], *, primary: Path, scratch: Path, default_model: str,
               demo_shot: bool, epic: int | None = None) -> list[str]:
    """The `claude -p` command line of a skill step, from an already-loaded tool list (`read_pinned_tools`;
    `demo_shot`: `demo_shot_matches_main`, else that one entry is dropped; `epic`: the epic in care). Runs
    nothing."""
    if step.kind != "skill" or step.issue is None:
        raise WatchCmdError(f"only a skill step runs as `claude -p` (this is {step.kind}: {step.text})")
    rules = build_rules(tools, step.issue, primary=primary, scratch=scratch, demo_shot=demo_shot, epic=epic)
    argv = ["claude", "-p", step.command,
            "--model", resolve_model(step, default_model),
            "--permission-mode", PERMISSION_MODE,
            "--permission-prompts", "none",
            "--setting-sources", SETTING_SOURCES,
            "--settings", SETTINGS,
            "--strict-mcp-config",
            "--tools", ",".join(rules.tools),
            "--allowedTools", *rules.allowed,
            "--disallowedTools", *(f"Bash({d})" for d in rules.deny),
            "--add-dir", str(scratch)]
    if bad := [a for a in argv[3:] if any(f in a for f in FORBIDDEN_ARGS)]:
        raise WatchCmdError(f"refused: the command line would carry {bad[0]}")
    return argv


def build_pinned_argv(step: Step, *, primary: Path, pin: str, scratch: Path, default_model: str,
                      epic: int | None = None) -> list[str]:
    """`build_argv` over the tool list at the pin: what a watcher run uses."""
    return build_argv(step, read_pinned_tools(primary, pin), primary=primary, scratch=scratch,
                      default_model=default_model, demo_shot=demo_shot_matches_main(primary), epic=epic)


# --- how Claude Code matches the rules (documented semantics, for tests and status) ---------------------------

SEPARATORS = ("&&", "||", "|&", ";", "|", "&", "\n")


def split_compound(command: str) -> list[str]:
    """A command's simple commands, split on `&& || ; | |& &` and newlines outside quotes."""
    parts, cur, quote, i = [], "", "", 0
    while i < len(command):
        c = command[i]
        if quote:
            cur += c
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in "'\"":
            quote, cur, i = c, cur + c, i + 1
            continue
        sep = next((s for s in SEPARATORS if command.startswith(s, i)), None)
        if sep == "&" and (command[i - 1:i] == ">" or command[i + 1:i + 2] == ">"):
            sep = None                            # a redirect (`2>&1`, `&>`), not a background `&`
        if sep:
            parts.append(cur)
            cur, i = "", i + len(sep)
            continue
        cur, i = cur + c, i + 1
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def bash_rule_matches(rule: str, command: str) -> bool:
    """`Bash(rule)` against one simple command: `*` is any text, spaces included; a rule with no `*` is exact;
    a trailing ` *` that is the only wildcard also matches the bare command."""
    command = command.strip()
    rx = ".*".join(re.escape(p) for p in rule.split("*"))
    if re.fullmatch(rx, command, re.S):
        return True
    return rule.endswith(" *") and rule.count("*") == 1 and command == rule[:-2]


def edit_rule_matches(rule: str, path: Path) -> bool:
    """`Edit(//abs/...)` against an absolute path: `**` any depth, `*` within one segment."""
    if not rule.startswith("//"):
        return False
    pat = rule[1:]
    rx = re.escape(pat).replace(r"\*\*", "\0").replace(r"\*", "[^/]*").replace("\0", ".*")
    return re.fullmatch(rx, str(path)) is not None


def bash_allowed(rules: Rules, command: str, working_dirs: list[Path]) -> bool:
    """Would the run's rules let `command` run with no prompt: no simple command denied, every one allowed, a
    `cd` into a working directory (the primary checkout and `--add-dir`) counting as read-only, and no `git`
    after a `cd` into another directory than the primary checkout (Claude Code prompts for that)."""
    moved = False
    for part in split_compound(command):
        if any(bash_rule_matches(r, part) for r in rules.deny):
            return False
        try:
            words = shlex.split(part)
        except ValueError:
            return False
        if len(words) == 2 and words[0] == "cd":
            target = Path(words[1])
            if target.is_absolute() and any(target == d or d in target.parents for d in working_dirs):
                moved = moved or target != working_dirs[0]
                continue
        if moved and words[:1] == ["git"]:
            return False
        if not any(bash_rule_matches(r, part) for r in rules.bash):
            return False
    return True


# --- the skills a run may load ---------------------------------------------------------------------------------

def frontmatter(text: str) -> dict[str, str]:
    """A SKILL.md's `---` block as top-level keys (values as raw text; enough to find `name` and `allowed-tools`)."""
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    out: dict[str, str] = {}
    for line in (m[1].splitlines() if m else []):
        if km := re.match(r"([A-Za-z][\w-]*):\s*(.*)", line):
            out[km[1]] = km[2]
    return out


def scan_skills(primary: Path, home: Path) -> list[str]:
    """Why a run must not start (empty: it may). Each of the four skills is the repo's own, in
    `.claude/skills/<name>/` (and the `.agents/skills/<name>/` it points at), declaring no `allowed-tools`
    (that grants tools while the skill runs, even in `-p`); and no other loadable skill or command shadows one
    of the four names: the repo's, the user's, or a plugin's."""
    errs: list[str] = []
    own = {primary / ".claude" / "skills" / s / "SKILL.md" for s in SKILLS}
    for s in SKILLS:
        for f in (primary / ".claude" / "skills" / s / "SKILL.md", primary / ".agents" / "skills" / s / "SKILL.md"):
            if not f.is_file():
                errs.append(f"{f} is missing")
                continue
            meta = frontmatter(f.read_text(errors="replace"))
            if "allowed-tools" in meta:
                errs.append(f"{f} declares allowed-tools")
            if meta.get("name") != s:
                errs.append(f"{f} is named {meta.get('name')!r}, not {s!r}")
    found = [*sorted((primary / ".claude" / "skills").glob("*/SKILL.md")),
             *sorted((home / ".claude" / "skills").glob("*/SKILL.md")),
             *sorted((home / ".claude" / "plugins").glob("**/skills/*/SKILL.md"))]
    for f in found:
        if f in own:
            continue
        name = frontmatter(f.read_text(errors="replace")).get("name") or f.parent.name
        if name in SKILLS or f.parent.name in SKILLS:
            errs.append(f"{f} shadows the {name if name in SKILLS else f.parent.name} skill")
    for d in (primary / ".claude" / "commands", home / ".claude" / "commands"):
        errs += [f"{d / (s + '.md')} shadows the {s} skill" for s in SKILLS if (d / f"{s}.md").exists()]
    return errs
