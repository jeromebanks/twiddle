"""The watcher's fixed tool list, its `next:` parser and the `claude -p` command line (#263 T1.4).

Nothing here runs `claude`: the rules are checked by what they match, with `bash_rule_matches`
modelling Claude Code's documented semantics (`*` is any text, spaces included; a compound
command is split, and every simple command must be allowed).
"""
from __future__ import annotations

import itertools
import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from tools import sdlc
from tools import sdlc_watch_cmd as w

ROOT = Path(__file__).resolve().parents[1]
TOOLS = w.load_tools((ROOT / w.TOOLS_FILE).read_text())
PRIMARY = Path("/Users/someone/dev/twiddle")
SCRATCH = Path("/private/tmp/watch-run/scratch")
ISSUE, EPIC = 345, 263
WORKTREE = PRIMARY / ".worktrees" / f"slice-{ISSUE}"
DIRS = [PRIMARY, SCRATCH]


def rules(issue: int = ISSUE, demo_shot: bool = True) -> w.Rules:
    return w.build_rules(TOOLS, issue, primary=PRIMARY, scratch=SCRATCH, demo_shot=demo_shot)


def allowed(cmd: str, r: w.Rules | None = None) -> bool:
    return w.bash_allowed(r or rules(), cmd, DIRS)


# --- what no rule may match ----------------------------------------------------------------------------------

def claude_md_writing_verbs() -> set[str]:
    """The commands of CLAUDE.md's read-only vs writing table whose effect writes to a speaker, alarms or
    Spotify: `relay start/up/down` -> relay start, relay up, relay down; placeholders and flags dropped."""
    verbs: set[str] = set()
    for line in (ROOT / "CLAUDE.md").read_text().splitlines():
        row = re.match(r"\|\s*(`.*?)\s\|\s(\*\*.*)$", line)   # a cell may hold `|` (`sleep <duration|off>`)
        if not row:
            continue
        what, effect = row[1], row[2]
        if not re.search(r"\*\*(writes|Writes)", effect) or not re.search(r"speaker|alarm|Spotify|Sonos|transport", effect):
            continue
        for span in re.findall(r"`([^`]+)`", what):
            words = list(itertools.takewhile(lambda x: not x.startswith(("<", "[", "-")), span.split()))
            if not words:
                continue
            head, last = words[:-1], words[-1]
            for alt in last.split("/"):
                verbs.add(" ".join([*head, alt]))
    return verbs


WRITING = claude_md_writing_verbs() | {"serve", "radio", "soak"}   # the old top-level `diag` spellings


def test_claude_md_rows_are_read():
    for v in ("play", "volume", "sleep", "restore", "tune", "relay start", "relay down", "spotify play",
              "spotify discover", "diag soak", "alarm rm", "alarm add", "alarm try", "alarm snooze",
              "alarm restore", "comedy sleep", "dial", "scene"):
        assert v in WRITING, v


def twiddle_words(words: list[str]) -> list[str] | None:
    """The words after the program name of a twiddle invocation, or None for another program."""
    if words[:1] == ["twiddle"]:
        return words[1:]
    if words[:2] == ["uv", "run"]:
        rest = words[2:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        if rest[:1] == ["twiddle"]:
            return rest[1:]
        if rest[:3] in (["python", "-m", "twiddle"], ["python", "-m", "twiddle.cli"]):
            return rest[3:]
    return None


def forbidden(cmd: str) -> str | None:
    """Why one simple command must never be allowed headless (None: it may be)."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    if not words:
        return None
    if words[0] == "claude" or (words[:2] == ["uv", "run"] and "claude" in words[2:3]):
        return "a nested claude (a permission skip, or any tool list it likes)"
    if (tw := twiddle_words(words)) is not None:
        verbs = [x for x in tw if not x.startswith("-")]
        if not verbs:
            return "bare twiddle"
        for v in WRITING:
            if verbs[:len(v.split())] == v.split():
                return f"twiddle {v} writes"
    if words[:2] == ["uv", "run"] and len(words) == 2:
        return "bare uv run"
    if words[:3] in (["gh", "pr", "merge"], ["gh", "issue", "comment"], ["gh", "pr", "comment"]):
        return " ".join(words[:3])
    if words[:2] in (["gh", "api"], ["gh", "label"]):
        return "raw gh " + words[1]
    if words[:3] == ["gh", "issue", "edit"]:
        labels = [lab for i, x in enumerate(words) if x in ("--add-label", "--remove-label") and i + 1 < len(words)
                  for lab in words[i + 1].split(",")]
        labels += [lab for x in words if x.startswith(("--add-label=", "--remove-label="))
                   for lab in x.split("=", 1)[1].split(",")]
        if any(lab.startswith(("sdlc:", "plan:", "lane:")) for lab in labels) or "--remove-label" in cmd:
            return "a protected label edit"
    if words[0] == "git":
        i = 1
        while i < len(words) and words[i] in ("-C", "-c"):
            i += 2
        if words[i:i + 1] == ["push"]:
            args = words[i + 1:]
            if any(a in ("--all", "--mirror", "--tags", "--delete", "-d", "--force", "-f") for a in args):
                return "a wide push"
            refspecs = [a for a in args if not a.startswith("-")][1:]
            if len(refspecs) != 1:
                return "a push of other than one refspec"
            dst = refspecs[0].lstrip("+").split(":")[-1].removeprefix("refs/heads/")
            if dst in ("main", "HEAD") or dst.startswith("epic/") or ":" in refspecs[0] or "+" in refspecs[0]:
                return "a raw push to main/epic or a remote mapping"
    py = words[3:4] if words[:3] == ["uv", "run", "python"] else []
    if py and py[0].endswith("sdlc_watch.py") and any(x in ("pause", "resume") for x in words[4:5]):
        return "watcher pause/resume"
    if py and py[0].endswith("demo_shot.py") and words[4:5] == ["tui"]:
        return "a TUI picture (M2)"
    return None


FILLS = ["", "x", " main", ":main", " HEAD:epic/263", " origin main", " push origin main", " +slice/345 main",
         " --add-label sdlc:approved", " --remove-label plan:slice", " --base main", " twiddle play --room roam",
         " play --room roam", " pr merge 5", " issue comment 5 --body x", " --dangerously-skip-permissions",
         " tui --out-dir x -- dial", " -m twiddle play"]


def instances(rule: str):
    """Commands the rule could match: each `*` filled with each adversarial text (and the bare form)."""
    parts = rule.split("*")
    for combo in itertools.product(FILLS, repeat=len(parts) - 1):
        yield "".join(p + f for p, f in zip(parts, combo)) + parts[-1]
    if rule.endswith(" *") and rule.count("*") == 1:
        yield rule[:-2]


def test_no_rule_can_match_a_forbidden_command():
    bad = [(r, c, why) for r in rules().bash for c in instances(r) for why in [forbidden(c)] if why]
    assert not bad, bad[:5]


FORBIDDEN_SAMPLES = [
    *[f"{pre} {v} --room roam" for v in sorted(WRITING)
      for pre in ("uv run twiddle", "twiddle", "uv run python -m twiddle")],
    "uv run twiddle alarm rm 12", "uv run twiddle relay start --room roam", "uv run twiddle spotify play x",
    "uv run twiddle play", "uv run --with x twiddle play --room roam",
    "gh pr merge 501", "gh pr merge 501 --squash", "gh issue comment 345 --body hi", "gh pr comment 501 --body hi",
    "gh api -X POST repos/o/r/issues/345/comments -f body=x", "gh api repos/o/r/issues/345/labels -f labels[]=x",
    f"gh issue edit {ISSUE} --add-label sdlc:approved", f"gh issue edit {ISSUE} --add-label bug --add-label sdlc:done",
    f"gh issue edit {ISSUE} --add-label lane:quick", f"gh issue edit {ISSUE} --remove-label plan:slice",
    f"gh issue edit {ISSUE} --add-label bug,sdlc:approved", f"gh issue edit 999 --add-label bug",
    f"git push -u origin slice/{ISSUE} main", f"git push origin slice/{ISSUE}:main", "git push origin HEAD:epic/263",
    f"git push origin +slice/{ISSUE} main", "git push origin main", "git push origin epic/263", "git push",
    "git push origin", f"git push --force origin slice/{ISSUE}", f"git -C {PRIMARY} push origin main",
    "uv run anything", "uv run python -c 'print(1)'", "uv run python tools/sdlc_watch.py pause",
    "uv run python tools/sdlc_watch.py resume 12", "uv run python tools/sdlc_watch.py status",
    f"uv run python {PRIMARY}/tools/demo_shot.py tui --out-dir {SCRATCH}/demo --step shot:x -- dial",
    "uv run python tools/demo_shot.py cli --out x.svg -- uv run twiddle status",   # relative: another checkout's
    "claude --dangerously-skip-permissions -p hi", "claude -p hi --permission-mode bypassPermissions",
    f"uv run python tools/sdlc.py status && uv run twiddle play --room roam",
    f"uv run pytest; gh pr merge 501",
]


@pytest.mark.parametrize("cmd", FORBIDDEN_SAMPLES)
def test_forbidden_commands_are_denied(cmd):
    assert not allowed(cmd), cmd


def test_the_push_refspec_cases_are_denied_by_the_built_rules():
    for cmd in (f"git push -u origin slice/160 main", "git push origin slice/160:main", "git push origin HEAD:epic/263",
                "git push origin +slice/160 main"):
        assert not allowed(cmd, rules(160)), cmd
    assert allowed("git push -u origin slice/160", rules(160))
    assert not allowed("git push -u origin slice/161", rules(160))   # only the run's own issue


def test_pushes_and_label_edits_are_never_wildcards():
    for r in TOOLS["bash"] + TOOLS["bash_per_issue"]:
        if r.startswith(("git push", "gh issue edit", "gh label", "gh api")) or "push" in r.split():
            assert "*" not in r, r
    assert not any(r.startswith("git push") for r in TOOLS["bash"]), "pushes are per issue"
    labels = {r.rsplit(" ", 1)[1] for r in TOOLS["bash_per_issue"] if r.startswith("gh issue edit")}
    assert labels == {"bug", "enhancement", "complexity:routine", "complexity:judgment", "complexity:novel"}


def test_an_unscoped_tool_in_allow_is_refused():
    for tool in ("Bash", "Edit", "Write", "Skill", "Bash(uv run *)"):
        data = json.loads((ROOT / w.TOOLS_FILE).read_text())
        data["allow"] = [*data["allow"], tool]
        with pytest.raises(w.WatchCmdError):
            w.load_tools(json.dumps(data))


@pytest.mark.parametrize("rule", ["uv run *", "uv *", "*", "uv run twiddle *", "git push *", "gh *"])
def test_a_wide_rule_is_refused_or_caught(rule):
    data = json.loads((ROOT / w.TOOLS_FILE).read_text())
    data["bash"] = [*data["bash"], rule]
    try:
        t = w.load_tools(json.dumps(data))
    except w.WatchCmdError:
        return
    r = w.build_rules(t, ISSUE, primary=PRIMARY, scratch=SCRATCH, demo_shot=True)
    assert any(forbidden(c) for b in r.bash for c in instances(b)), rule


def test_the_demo_shot_rule_is_the_one_wildcard_over_a_command_line():
    """`demo_shot.py cli *` matches `cli ... -- uv run twiddle play` as text: T0.2's guard refuses that inside a
    watcher run (`TWIDDLE_SDLC_WATCH_RUN`, which the run contract sets). It names the primary's own copy."""
    r = rules()
    shot = f"uv run python {PRIMARY}/tools/demo_shot.py cli *"
    assert shot in r.bash
    assert shot not in rules(demo_shot=False).bash
    assert not allowed("uv run python tools/demo_shot.py cli --out x.svg -- uv run twiddle status")
    assert not allowed(f"uv run python {WORKTREE}/tools/demo_shot.py cli --out x.svg -- uv run twiddle status")


# --- every command the four skills run -----------------------------------------------------------------------

COMMANDS = ("uv ", "git ", "gh ", "cd ", "mkdir ", "qlmanage ", "claude ")
# A skill line that starts with a command name but isn't something a headless run executes. Reviewed:
# each says why, and each must still be in a skill (a stale entry fails).
IGNORED = {
    "gh pr merge": "prose: what no skill runs",
    "uv run": "prose: the `uv run` part of the merge rule",
    "cd <WORKTREE> && ...": "prose: how every worktree command starts",
    "git -C <WORKTREE>": "prose: a fragment",
    "uv run twiddle snapshot --room R": "a person runs it: a write's demo-request body",
    "uv run twiddle restore --room R": "a person runs it: a write's demo-request body",
    "uv run twiddle snapshot --room <room>": "a person runs it: the bracket of a demo-request",
    "uv run python tools/demo_shot.py cli --out <SCRATCH>/demo/alarms.svg -- uv run twiddle alarm list":
        "a run uses the inline `<PRIMARY>/tools/demo_shot.py` form (the primary's own copy)",
    "uv run python tools/demo_shot.py tui --out-dir <SCRATCH>/demo --step wait:8 --step escape --step wait:6 "
    "--step shot:dial -- dial": "TUI pictures are M2's: denied headless",
}
NOT_COMMANDS = ("#", "{", "}", "[", "]", '"')   # a fenced comment or JSON line


def skill_commands() -> list[tuple[str, str]]:
    """(skill, command) for every fenced line and inline span of the four SKILL.md files that starts with a
    command name; fenced `\\` continuations joined and trailing `# comments` dropped."""
    out = []
    for s in w.SKILLS:
        text = (ROOT / ".agents" / "skills" / s / "SKILL.md").read_text()
        for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
            for line in re.sub(r"\s*\\\n\s*", " ", block).splitlines():
                line = re.sub(r"\s{2,}#.*$", "", line).strip()
                if not line or line.startswith(NOT_COMMANDS):
                    continue
                assert line.startswith(COMMANDS) or line in IGNORED, f"{s}: fenced line {line!r}"
                out.append((s, line))
        prose = re.sub(r"```.*?```", "", text, flags=re.S)
        out += [(s, m) for m in re.findall(r"`([^`\n]+)`", prose) if m.startswith(COMMANDS) or m == "uv run"]
    return out


SUBS = [("<the model you are>", "opus"), ("<WORKTREE>", str(WORKTREE)), ("<PRIMARY>", str(PRIMARY)),
        ("<SCRATCH>", str(SCRATCH)), ("<REVIEW>", f"{SCRATCH}/review1"), ("<BRANCH>", f"slice/{ISSUE}"),
        ("<BASE>", f"origin/epic/{EPIC}"), ("<epic>", str(EPIC)), ("<after>", "a" * 40), ("<before>", "b" * 40)]


def concrete(cmd: str) -> str:
    for k, v in SUBS:
        cmd = cmd.replace(k, v)
    cmd = re.sub(r"<[^<>]+>", "x", cmd)
    for k, v in (("N", ISSUE), ("T", ISSUE), ("PR", 501), ("E", EPIC), ("D", 250)):
        cmd = re.sub(rf"(?<![A-Za-z0-9_-]){k}(?![A-Za-z0-9_])", str(v), cmd)
    return cmd


def test_every_command_the_skills_run_is_allowed():
    found = skill_commands()
    assert len(found) > 100
    denied = [(s, c) for s, c in found if c not in IGNORED and not allowed(concrete(c))]
    assert not denied, denied


def test_the_ignore_list_is_current_and_its_writes_stay_denied():
    found = {c for _, c in skill_commands()}
    assert not set(IGNORED) - found, set(IGNORED) - found
    for c in ("gh pr merge", "uv run twiddle restore --room R", "uv run", IGNORED_TUI := next(
            k for k in IGNORED if " tui " in k)):
        assert not allowed(concrete(c)), c
    assert IGNORED_TUI


def test_triage_quick_brief_path_is_allowed():
    assert allowed(f"gh issue edit {ISSUE} --add-label complexity:routine")
    assert allowed(f"uv run python tools/sdlc.py transition {ISSUE} prd-review --kind prd --lane quick "
                   f"--body-file {SCRATCH}/brief.md")


def test_milestone_demo_picture_line_is_allowed_verbatim():
    line = (f"cd {SCRATCH}/after && uv sync -q && uv run python {PRIMARY}/tools/demo_shot.py cli "
            f"--out {SCRATCH}/demo/s.svg -- uv run twiddle status")
    assert allowed(line)
    assert allowed(f"qlmanage -t -s 1400 -o {SCRATCH}/previews {SCRATCH}/demo/s.svg")
    assert not w.bash_allowed(rules(), line, [PRIMARY])   # the scratch dir is reachable only through --add-dir


# --- files -----------------------------------------------------------------------------------------------------

def edit_allowed(path: Path, r: w.Rules | None = None) -> bool:
    return any(w.edit_rule_matches(e, path) for e in (r or rules()).edit)


@pytest.mark.parametrize("path", [".claude/settings.json", ".claude/skills/work-slice/SKILL.md",
                                  ".sdlc/config.json", ".sdlc/watch-tools.json", "CLAUDE.md", "tools/sdlc.py",
                                  ".git/config"])
def test_no_edit_rule_covers_the_primary_checkout(path):
    assert not edit_allowed(PRIMARY / path)


def test_edit_rules_cover_worktrees_and_the_scratch_dir():
    assert edit_allowed(WORKTREE / "tools" / "x.py")
    assert edit_allowed(PRIMARY / ".worktrees" / "slice-9" / ".sdlc" / "watch-tools.json")
    assert edit_allowed(SCRATCH / "demo" / "s.svg")
    assert all(e.startswith("//") for e in rules().edit)
    assert not edit_allowed(Path("/Users/someone/.claude/settings.json"))


@pytest.mark.parametrize("scratch", [PRIMARY, PRIMARY / "logs", PRIMARY.parent, Path("relative/dir")])
def test_a_scratch_dir_that_would_cover_the_checkout_is_refused(scratch):
    with pytest.raises(w.WatchCmdError):
        w.build_rules(TOOLS, ISSUE, primary=PRIMARY, scratch=scratch, demo_shot=True)


# --- the command line ------------------------------------------------------------------------------------------

STEPS = [w.parse_next(x) for x in ("/triage-issue 12", "/plan-issue 263", "/work-slice 160 (opus: judgment)",
                                    "/work-slice 12 --quick", "/work-slice 131 --debt (sonnet: routine)",
                                    "/milestone-demo 263: #263 M1: x is complete, and new slices wait for its demo")]


def argv(step: w.Step, **kw) -> list[str]:
    return w.build_argv(step, TOOLS, primary=PRIMARY, scratch=SCRATCH, default_model="opus",
                        demo_shot=kw.get("demo_shot", True))


@pytest.mark.parametrize("step", STEPS, ids=lambda s: s.text[:24])
def test_no_command_line_carries_a_budget_or_a_skip(step):
    a = argv(step)
    for bad in ("--bare", "--max-budget-usd", "--max-turns", "--dangerously-skip-permissions", "bypassPermissions",
                "--allow-dangerously-skip-permissions"):
        assert not any(bad in x for x in a), bad
    assert a[:3] == ["claude", "-p", step.command]
    assert a[a.index("--permission-mode") + 1] == "dontAsk"
    assert a[a.index("--permission-prompts") + 1] == "none"
    assert a[a.index("--setting-sources") + 1] == "project"     # never the user's allow rules
    assert json.loads(a[a.index("--settings") + 1]) == {"disableAllHooks": True}
    assert "--strict-mcp-config" in a
    assert a[a.index("--add-dir") + 1] == str(SCRATCH)


def test_the_model_comes_from_the_line_else_the_default():
    assert argv(STEPS[2])[4] == "opus" and argv(STEPS[4])[4] == "sonnet"
    a = w.build_argv(STEPS[0], TOOLS, primary=PRIMARY, scratch=SCRATCH, default_model="fable", demo_shot=True)
    assert a[a.index("--model") + 1] == "fable"
    with pytest.raises(w.WatchCmdError):
        w.build_argv(STEPS[0], TOOLS, primary=PRIMARY, scratch=SCRATCH, default_model="", demo_shot=True)


def test_skills_are_scoped_to_the_four_by_name():
    a = argv(STEPS[2])
    allowed_tools = a[a.index("--allowedTools") + 1:a.index("--add-dir")]
    assert sorted(x for x in allowed_tools if x.startswith("Skill")) == sorted(f"Skill({s})" for s in w.SKILLS)
    assert "Skill" not in allowed_tools and "Bash" not in allowed_tools and "Edit" not in allowed_tools
    assert {"Read", "Glob", "Grep", "Agent"} <= set(allowed_tools)
    assert a[a.index("--tools") + 1].split(",") == TOOLS["tools"]


def test_a_skill_granting_a_speaker_write_cant_be_invoked():
    """A skill with `allowed-tools: Bash(uv run twiddle play *)` widens a run only if it's invoked: no
    Skill rule but the four's lets it be, and nothing on the list runs its write."""
    a = argv(STEPS[2])
    assert "Skill(player)" not in a and not any(x in ("Skill", "Skill(*)") for x in a)
    assert not allowed("uv run twiddle play --room roam")


def test_a_mapped_or_skipped_step_never_builds_a_claude_line():
    for text in ("`uv run python tools/sdlc.py sync 263`: epic/263 is 2 commit(s) behind main",
                 "nothing new to start: in progress #161 (`/work-slice 161` resumes one)"):
        with pytest.raises(w.WatchCmdError):
            argv(w.parse_next(text))


# --- the `next:` parser: cases from sdlc.py's own producers, and copied lines --------------------------------

def epic_next(**progress) -> str:
    st = {"number": EPIC, "state": "in-progress", "action": "work_slices", "turn": "agent"}
    p = {"milestones": [{"key": "M1", "title": "#263 M1: The watcher", "done": 0, "total": 9}],
         "units": {160: {"milestone": "M1", "model": "opus", "complexity": "judgment"}},
         "ready": [160], "in_flight": [], "escalated": [], "behind_main": 0}
    p.update(progress)
    return sdlc.epic_view(st, p)["next"]


def test_parse_a_slash_command_with_and_without_a_model():
    s = w.parse_next(epic_next())
    assert (s.kind, s.command, s.model) == ("skill", "/work-slice 160", "opus")
    s = w.parse_next("next: /work-slice 344 (opus: judgment)")   # as `state 263` prints it
    assert (s.command, s.model) == ("/work-slice 344", "opus")
    s = w.parse_next("/triage-issue 2")
    assert (s.command, s.model) == ("/triage-issue 2", None)
    s = w.parse_next(epic_next(milestones=[{"key": "M1", "title": "#263 M1: The watcher", "done": 9, "total": 9}],
                               ready=[]))
    assert (s.kind, s.command, s.model) == ("skill", "/milestone-demo 263", None)
    s = w.parse_next("/plan-issue 263: the Codex rounds on this amendment are spent; escalate it "
                     "(`transition 263 escalated --kind escalation --reason ...`)")
    assert (s.kind, s.command, s.model) == ("skill", "/plan-issue 263", None)


def test_parse_quick_and_debt():
    s = w.parse_next(sdlc.next_command({"number": 12, "action": "work_quick", "turn": "agent", "state": "approved"}))
    assert (s.command, s.flags, s.model) == ("/work-slice 12 --quick", ("--quick",), None)
    s = w.parse_next("/work-slice 131 --debt (sonnet: routine)")
    assert (s.command, s.model) == ("/work-slice 131 --debt", "sonnet")
    assert w.parse_next("/triage-issue 12 --quick").kind == "skip"


def test_parse_the_mapped_steps():
    s = w.parse_next(epic_next(ready=[], behind_main=2))
    assert s.text.startswith("`uv run python tools/sdlc.py sync 263`: ")
    assert (s.kind, s.argv) == ("sdlc", ("uv", "run", "python", "tools/sdlc.py", "sync", "263"))
    s = w.parse_next(sdlc.next_command({"number": 12, "action": "commit_prd", "turn": "agent", "state": "approved"}))
    assert (s.kind, s.argv[-2:]) == ("sdlc", ("commit-prd", "12"))
    for line in (sdlc.merge_next(501, None), sdlc.merge_next(501, []),
                 sdlc.debt_merge_next([{"pr": 501, "group": "g1", "pass": 1}], [])):
        assert not line.startswith("`")
        s = w.parse_next(line)
        assert (s.kind, s.argv[-2:]) == ("sdlc", ("merge", "501")), line


@pytest.mark.parametrize("line", [
    epic_next(ready=[], in_flight=[161]),                    # 'nothing new to start: ... (`/work-slice 161` ...)'
    "nothing: waiting on the poster to reply on #12",
    sdlc.next_command({"number": 12, "action": "wait_for_poster", "turn": "poster", "state": "needs-info"}),
    "nothing for an agent: #12 needs a human",
    "`uv run python tools/sdlc.py ship 263`: every milestone has shipped; it finishes the epic",
    "`uv run python tools/sdlc.py verify-main 263`: T1 reached main with commits no recorded run saw",
    "`uv run python tools/sdlc.py state 263` (needs the slices' progress)",
    "/add-radio-station 12",
    "uv run python tools/sdlc.py merge 501 && gh pr merge 501",
    "/work-slice 160; rm -rf /",
])
def test_anything_else_is_skipped_with_its_text(line):
    s = w.parse_next(line)
    assert s.kind == "skip" and s.text == line and s.reason


# --- the pin: the list comes from a pushed commit, never the working tree ------------------------------------

def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    """A primary checkout whose origin/main holds this repo's tool list and demo_shot.py."""
    origin, root = tmp_path / "origin.git", tmp_path / "repo"
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git("config", k, v, cwd=root)
    for p in (w.TOOLS_FILE, "tools/demo_shot.py"):
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_bytes((ROOT / p).read_bytes())
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "main", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("push", "-q", "origin", "main", cwd=root)
    _git("fetch", "-q", "origin", cwd=root)
    return root


def pinned(root: Path, pin: str) -> list[str]:
    return w.build_pinned_argv(STEPS[2], primary=root, pin=pin, scratch=root.parent / "scratch", default_model="opus")


def test_the_list_is_read_from_the_pin_not_the_working_tree(checkout):
    pin = _git("rev-parse", "HEAD", cwd=checkout)
    before = pinned(checkout, pin)
    data = json.loads((checkout / w.TOOLS_FILE).read_text())
    data["bash"].append("uv run twiddle play *")
    (checkout / w.TOOLS_FILE).write_text(json.dumps(data))
    assert pinned(checkout, pin) == before
    assert "Bash(uv run twiddle play *)" not in before


def test_an_unpushed_pin_is_refused(checkout):
    (checkout / "x").write_text("x")
    _git("add", "x", cwd=checkout)
    _git("commit", "-q", "-m", "local", cwd=checkout)
    with pytest.raises(w.WatchCmdError, match="not pushed"):
        pinned(checkout, _git("rev-parse", "HEAD", cwd=checkout))
    with pytest.raises(w.WatchCmdError, match="full commit SHA"):
        pinned(checkout, "HEAD")


def test_a_pin_on_an_epic_branch_is_accepted(checkout):
    _git("switch", "-q", "-c", "epic/9", cwd=checkout)
    (checkout / "x").write_text("x")
    _git("add", "x", cwd=checkout)
    _git("commit", "-q", "-m", "epic", cwd=checkout)
    _git("push", "-q", "origin", "epic/9", cwd=checkout)
    _git("fetch", "-q", "origin", cwd=checkout)
    assert pinned(checkout, _git("rev-parse", "HEAD", cwd=checkout))[0] == "claude"


def test_the_demo_shot_entry_needs_mains_demo_shot(checkout):
    pin = _git("rev-parse", "HEAD", cwd=checkout)
    rule = f"Bash(uv run python {checkout}/tools/demo_shot.py cli *)"
    assert rule in pinned(checkout, pin)
    (checkout / "tools" / "demo_shot.py").write_text("# changed\n")
    assert rule not in pinned(checkout, pin)


# --- the skill scan T1.5 runs ----------------------------------------------------------------------------------

def skill(path: Path, name: str, extra: str = "") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n{extra}---\n\nbody\n")


@pytest.fixture
def skills(tmp_path):
    primary, home = tmp_path / "repo", tmp_path / "home"
    for s in w.SKILLS:
        skill(primary / ".claude" / "skills" / s, s)
        skill(primary / ".agents" / "skills" / s, s)
    skill(primary / ".claude" / "skills" / "add-radio-station", "add-radio-station")
    return primary, home


def test_the_repos_own_skills_pass_the_scan(skills):
    assert w.scan_skills(*skills) == []
    assert w.scan_skills(ROOT, skills[1]) == []    # this checkout's real SKILL.md files


def test_a_skill_declaring_allowed_tools_fails_the_scan(skills):
    primary, home = skills
    skill(primary / ".claude" / "skills" / "work-slice", "work-slice", "allowed-tools: Bash(uv run twiddle play *)\n")
    assert any("declares allowed-tools" in e for e in w.scan_skills(primary, home))


def test_a_skill_granting_tools_outside_the_four_doesnt_fail_but_cant_run(skills):
    primary, home = skills
    skill(primary / ".claude" / "skills" / "player", "player", "allowed-tools: Bash(uv run twiddle play *)\n")
    assert w.scan_skills(primary, home) == []
    test_a_skill_granting_a_speaker_write_cant_be_invoked()


@pytest.mark.parametrize("where", ["user", "plugin", "renamed", "command"])
def test_a_skill_shadowing_work_slice_fails_the_scan(skills, where):
    primary, home = skills
    if where == "user":
        skill(home / ".claude" / "skills" / "work-slice", "work-slice")
    elif where == "plugin":
        skill(home / ".claude" / "plugins" / "cache" / "x" / "skills" / "work-slice", "work-slice")
    elif where == "renamed":
        skill(primary / ".claude" / "skills" / "evil", "work-slice")
    else:
        (home / ".claude" / "commands").mkdir(parents=True)
        (home / ".claude" / "commands" / "work-slice.md").write_text("hi")
    assert any("shadows the work-slice skill" in e for e in w.scan_skills(primary, home))


def test_a_missing_or_misnamed_skill_fails_the_scan(skills):
    primary, home = skills
    (primary / ".agents" / "skills" / "plan-issue" / "SKILL.md").unlink()
    skill(primary / ".claude" / "skills" / "triage-issue", "triage")
    errs = w.scan_skills(primary, home)
    assert any("missing" in e for e in errs) and any("named 'triage'" in e for e in errs)
