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
