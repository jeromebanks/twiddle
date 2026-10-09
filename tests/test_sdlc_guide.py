"""docs/sdlc.html, the guide for people, must keep up with tools/sdlc.py: every label, command,
action, skill and milestone phase the workflow has is described there."""
import re
from pathlib import Path

import pytest

from tools import sdlc

ROOT = Path(__file__).resolve().parent.parent
GUIDE = (ROOT / "docs" / "sdlc.html").read_text()
SOURCE = (ROOT / "tools" / "sdlc.py").read_text()
CONFIG = sdlc.load_config()

COMMANDS = re.findall(r'add_parser\("([a-z-]+)"', SOURCE)
ACTIONS = sdlc.ALL_ACTIONS
SKILLS = ["triage-issue", "plan-issue", "work-slice", "milestone-demo"]


def test_the_lists_are_read():
    assert len(COMMANDS) > 25
    assert sdlc.TRIAGE_ACTIONS | sdlc.PLAN_ACTIONS | sdlc.DEMO_ACTIONS <= ACTIONS
    # every `action = "..."` the code can set is in the list
    assigned = set(re.findall(r'action = "([a-z_]+)"', SOURCE)) | set(re.findall(r'turn, action = "[a-z]+", "([a-z_]+)"', SOURCE))
    assert assigned - {"agent", "poster", "later"} <= ACTIONS


@pytest.mark.parametrize("label", [l["name"] for l in CONFIG["labels"]])
def test_every_state_label_is_described(label):
    assert label in GUIDE


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_is_named(command):
    assert f"<code>{command}" in GUIDE, f"`{command}` isn't in docs/sdlc.html: say which skill runs it, and when"


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_every_action_is_explained(action):
    assert f"<code>{action}</code>" in GUIDE, f"state's action `{action}` isn't in docs/sdlc.html's table"


@pytest.mark.parametrize("skill", SKILLS)
def test_every_skill_is_described(skill):
    assert (ROOT / ".agents" / "skills" / skill / "SKILL.md").is_file()
    assert f"<code>{skill}</code>" in GUIDE


@pytest.mark.parametrize("phase", list(sdlc.MILESTONE_FLOW))
def test_every_milestone_phase_is_drawn(phase):
    assert re.search(rf">{phase}<", GUIDE), f"milestone phase {phase} isn't in the guide's diagram"


def test_the_pr_review_is_run_by_the_tool_not_by_hand():
    prompt = (ROOT / ".agents" / "skills" / "work-slice" / "references" / "codex-pr-prompt.md").read_text()
    assert "HEAD:" not in prompt.split("\n---\n", 1)[1] and "rev-parse" not in prompt   # the tool stamps the head
    skill = (ROOT / ".agents" / "skills" / "work-slice" / "SKILL.md").read_text()
    section = skill[skill.index("## 8. Codex rounds"):skill.index("## 9.")]
    fenced = "\n".join(re.findall(r"```[a-z]*\n(.*?)```", section, re.DOTALL))
    assert "codex exec" not in fenced and "codex-review --pr" in fenced
    assert not re.search(r"\$[0-9]", skill)          # the skill loader substitutes dollar-digit sequences


SKILL_FILES = sorted((ROOT / ".agents" / "skills").rglob("*.md")) + sorted((ROOT / ".claude" / "skills").rglob("*.md"))
# Codex's own reviewers bring prompts and settings the repo doesn't own: never offered as a way to review
OTHER_REVIEWERS = re.compile(r"codex exec|codex[- ]plugin|/codex:|codex:(?:rescue|review|setup)|codex-companion", re.IGNORECASE)


def test_every_review_goes_through_codex_review():
    names = {p.relative_to(ROOT).as_posix() for p in SKILL_FILES}
    for skill in ("plan-issue", "work-slice", "milestone-demo"):
        assert f".agents/skills/{skill}/SKILL.md" in names
    for f in ("plan-issue/references/codex-plan-prompt.md", "milestone-demo/references/codex-milestone-prompt.md",
              "work-slice/references/codex-pr-prompt.md"):
        assert f".agents/skills/{f}" in names
    hits = [f"{p.relative_to(ROOT)}: {m.group(0)!r}" for p in SKILL_FILES for m in OTHER_REVIEWERS.finditer(p.read_text())]
    assert not hits, "run reviews with `tools/sdlc.py codex-review`, not: " + "; ".join(hits)
    for skill, flag in (("plan-issue", "codex-review --plan N"), ("milestone-demo", "codex-review --milestone N"),
                        ("work-slice", "codex-review --pr PR")):
        assert flag in (ROOT / ".agents" / "skills" / skill / "SKILL.md").read_text()
    sdlc_md = (ROOT / "SDLC.md").read_text()
    assert "the only way a review is run" in sdlc_md and "`codex exec review`" in sdlc_md and "Codex plugin" in sdlc_md


def test_no_prompt_asks_codex_for_the_head():
    for skill, name in (("milestone-demo", "codex-milestone-prompt.md"), ("plan-issue", "codex-plan-prompt.md")):
        prompt = (ROOT / ".agents" / "skills" / skill / "references" / name).read_text().split("\n---\n", 1)[1]
        assert "HEAD:" not in prompt and "rev-parse" not in prompt


PROMPTS = {"work-slice": "codex-pr-prompt.md", "milestone-demo": "codex-milestone-prompt.md",
           "plan-issue": "codex-plan-prompt.md"}


def test_every_prompt_holds_codex_to_review_rules_and_claude_md_only_for_layout():
    rules = ROOT / ".agents" / "skills" / "review-rules.md"
    assert rules.is_file()
    for skill, name in PROMPTS.items():
        prompt = (ROOT / ".agents" / "skills" / skill / "references" / name).read_text().split("\n---\n", 1)[1]
        assert "`.agents/skills/review-rules.md`" in prompt, f"{name} must point Codex at review-rules.md"
        lines = [l for l in prompt.splitlines() if "CLAUDE.md" in l]
        if skill == "plan-issue":       # knowledge of the code under review: its Layout table, and nothing else
            assert len(lines) == 1 and "Layout" in lines[0] and "only" in lines[0] and "read-only" not in lines[0]
        else:
            assert not lines, f"{name} names CLAUDE.md: its safety rules are review-rules.md's"
    claude = (ROOT / "CLAUDE.md").read_text()
    safety = claude[claude.index("## Read-only vs writing"):claude.index("## The daemon")]
    assert "(.agents/skills/review-rules.md)" in safety, "CLAUDE.md's safety table must link to review-rules.md"


def test_sdlc_py_neither_fills_a_prompt_nor_chooses_what_goes_in_it():
    """What a round is handed is review logic, fingerprinted with codex_review.py: sdlc.py only fetches and posts."""
    import ast
    from tools import codex_review
    tree = ast.parse(SOURCE)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    review = {n for n, v in vars(codex_review).items() if callable(v) and getattr(v, "__module__", "") == codex_review.__name__}
    assert not defined & review, f"sdlc.py redefines codex_review's {sorted(defined & review)}"
    choosers = re.compile(r"prompt|requirements|milestone_slices|owed_slices|approved_comment|plan_rounds|latest_response")
    assert not [d for d in defined if choosers.search(d)], "these belong in tools/codex_review.py: " + \
        ", ".join(sorted(d for d in defined if choosers.search(d)))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not used & {"fill_prompt", "fill_pr_prompt", "PR_PROMPT", "PLAN_PROMPT", "MILESTONE_PROMPT"}, \
        "sdlc.py reads or fills a prompt template itself"


def test_what_a_round_is_handed_comes_from_codex_review():
    """Each `codex_review.prepare_*_round` call in sdlc.py is handed its rounds (and a milestone's owed slices)
    straight from a codex_review function: no filter or selection of sdlc.py's own sits in between."""
    import ast
    import inspect
    from tools import codex_review
    tree = ast.parse(SOURCE)
    checked = 0
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        assigned = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    for name in (t.elts if isinstance(t, ast.Tuple) else [t]):
                        if isinstance(name, ast.Name):
                            assigned.setdefault(name.id, []).append(node.value)
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr.startswith("prepare_") and getattr(n.func.value, "id", "") == "codex_review"]:
            params = list(inspect.signature(getattr(codex_review, call.func.attr)).parameters)
            given = dict(zip(params, call.args)) | {k.arg: k.value for k in call.keywords}
            for param in ("rounds", "owed"):
                if param not in given:
                    continue
                arg = given[param]
                values = assigned.get(arg.id, []) if isinstance(arg, ast.Name) else [arg]
                assert values, f"{fn.name}: `{param}` handed to {call.func.attr} isn't assigned in it"
                for v in values:
                    assert isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and \
                        getattr(v.func.value, "id", "") == "codex_review", \
                        f"{fn.name}: `{param}` for {call.func.attr} is chosen in sdlc.py ({ast.unparse(v)})"
                checked += 1
    assert checked >= 4          # the PR's, the plan's and the milestone's rounds, and the milestone's owed slices
