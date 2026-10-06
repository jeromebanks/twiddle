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
