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


# what a debt or quick round is handed (F2): exact names, not a bare `brief` pattern (`demo_brief` only fetches)
BRIEF_BUILDERS = {"debt_brief", "approved_brief", "approved_brief_id", "comment_anchor", "quick_brief"}


def review_logic_in(source: str) -> list[str]:
    """Why `source` (an sdlc.py) breaks the rule that what a round is handed lives in codex_review.py; [] = it doesn't."""
    import ast
    from tools import codex_review
    tree = ast.parse(source)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    review = {n for n, v in vars(codex_review).items() if callable(v) and getattr(v, "__module__", "") == codex_review.__name__}
    errs = [f"sdlc.py redefines codex_review's {sorted(defined & review)}"] if defined & review else []
    choosers = re.compile(r"prompt|requirements|milestone_slices|owed_slices|approved_comment|plan_rounds|latest_response")
    if found := sorted({d for d in defined if choosers.search(d)} | (defined & BRIEF_BUILDERS)):
        errs.append("these belong in tools/codex_review.py: " + ", ".join(found))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    if used & {"fill_prompt", "fill_pr_prompt", "PR_PROMPT", "PLAN_PROMPT", "MILESTONE_PROMPT"}:
        errs.append("sdlc.py reads or fills a prompt template itself")
    return errs


def test_sdlc_py_neither_fills_a_prompt_nor_chooses_what_goes_in_it():
    """What a round is handed is review logic, fingerprinted with codex_review.py: sdlc.py only fetches and posts."""
    assert review_logic_in(SOURCE) == []


@pytest.mark.parametrize("name", sorted(BRIEF_BUILDERS))
def test_the_guard_refuses_a_brief_builder_defined_in_sdlc_py(name):
    errs = review_logic_in(f"def {name}(x):\n    return x\n")
    assert any("belong in tools/codex_review.py" in e and name in e for e in errs), errs
    # an alias is not a definition, and `demo_brief` only fetches: neither is refused
    assert review_logic_in(f"from tools import codex_review\n{name} = codex_review.{name}\n") == []
    assert review_logic_in("def demo_brief(n):\n    return n\n") == []


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


WORKFLOW_DOCS = ["SDLC.md", "CLAUDE.md", "docs/sdlc.html", *(str(p.relative_to(ROOT)) for p in sorted((ROOT / ".agents").rglob("*.md")))]


@pytest.mark.parametrize("doc", WORKFLOW_DOCS)
def test_no_workflow_doc_promises_the_retired_cleanup_pass(doc):
    """The cleanup pass is gone (#250): no doc may still send debt to one, budget it, or have `merge` close it.
    docs/prd/ keeps the history; a doc may say the pass was retired."""
    text = re.sub(r"\s+", " ", (ROOT / doc).read_text())
    stale = [m.group(0) for m in re.finditer(r"[^.]*\b(cleanup pass|cleanup slice|--cleanup|cleanup_slices_per_milestone|"
                                             r"Pays down|budgeted)\b[^.]*", text, re.I)
             if not re.search(r"retired|is gone|are gone", m.group(0))]
    assert not stale, stale


# What tools/sdlc.py's verbs do to GitHub: these write nothing there (CLAUDE.md's tools/sdlc.py row). Every other
# verb counts as writing, so a new verb is a writer until someone adds it here.
GITHUB_READ_ONLY = {"state", "next", "plan-validate", "slice-status", "quick-gate", "slice-check",
                    "debt-pass-validate", "ready", "demo-status", "cleanup", "codex-review"}
WRITERS = set(COMMANDS) - GITHUB_READ_ONLY
TRIAGE = (ROOT / ".agents" / "skills" / "triage-issue" / "SKILL.md").read_text()
PREVIEW_OPENING = "The one exception is a preview run"


def invoked_verbs(skill: str) -> set[str]:
    """The tools/sdlc.py verbs a skill tells an agent to run: `tools/sdlc.py <verb>` anywhere, the first word of a
    fenced line (or of a `# a bug:  transition ...` comment), and a backticked command in a table row. A verb only
    mentioned in prose (`ship E` lifts the wait; a person can run `escalate-to-full`) is not an invocation."""
    found = set(re.findall(r"tools/sdlc\.py ([a-z][a-z-]*)", skill))
    for block in re.findall(r"```[a-z]*\n(.*?)```", skill, re.DOTALL):
        found |= set(re.findall(r"^(?:#[^\n]*?:\s+)?([a-z][a-z-]*)\b", block, re.MULTILINE))
    for row in re.findall(r"^\|.*$", skill, re.MULTILINE):
        found |= set(re.findall(r"`([a-z][a-z-]*) ", row))
    return found & set(COMMANDS)


def preview_rule(skill: str) -> str:
    """The preview paragraph: from its fixed opening to the next blank line."""
    assert PREVIEW_OPENING in skill, "the triage-issue skill's preview rule is gone, or its opening changed"
    return skill[skill.index(PREVIEW_OPENING):].split("\n\n", 1)[0]


def unheld_writes(skill: str) -> list[str]:
    """Writes the skill runs that its preview rule doesn't hold for an OK. A verb is named as `verb` (or `verb ...`
    with anything but `--lift` after it, so `wait --lift` alone doesn't count as naming `wait`)."""
    rule = preview_rule(skill)
    missing = [v for v in sorted(invoked_verbs(skill) & WRITERS)
               if not re.search(rf"`{re.escape(v)}(?:`| (?!--lift`))", rule)]
    if re.search(r"\bwait N --lift\b", skill) and "`wait --lift`" not in rule:
        missing.append("wait --lift")
    return missing


def test_the_read_only_verbs_exist():
    assert GITHUB_READ_ONLY <= set(COMMANDS), GITHUB_READ_ONLY - set(COMMANDS)


def test_the_triage_extractor_reads_invocations():
    assert {"transition", "wait", "reconcile", "commit-prd", "merge-prd", "debt-intake"} <= invoked_verbs(TRIAGE)
    assert "ship" not in invoked_verbs(TRIAGE) and "escalate-to-full" not in invoked_verbs(TRIAGE)   # prose only


def test_a_preview_run_of_triage_holds_every_write_it_runs():
    """#379 M1's review: `wait` and `wait --lift` ran for real under --preview / confirm_before_posting, because the
    preview rule named only `transition`. Every GitHub write the skill runs must be named in the rule."""
    assert unheld_writes(TRIAGE) == []


@pytest.mark.parametrize("drop, reported", [
    ("`wait` (parking an issue), ", "wait"),
    ("`wait --lift` (ending a wait), ", "wait --lift"),
    ("`reconcile`, ", "reconcile"),
])
def test_the_preview_check_bites(drop, reported):
    assert drop in preview_rule(TRIAGE)
    assert reported in unheld_writes(TRIAGE.replace(drop, "", 1))


def test_the_preview_check_fails_without_its_paragraph():
    with pytest.raises(AssertionError, match="preview rule is gone"):
        unheld_writes(TRIAGE.replace(PREVIEW_OPENING, "Except in a preview run"))
