"""tools/sdlc.py planning: plan state, validation, Codex verdicts, idempotent creation. No network."""
import copy
import json
from pathlib import Path

import pytest

from tools import sdlc

CONFIG = sdlc.load_config()
OWNER, POSTER = "owner", "poster"
_ids = iter(range(1, 10_000))


def comment(author, body, ts):
    return {"id": next(_ids), "author": author, "body": body, "created_at": f"2026-10-02T00:{ts:02d}:00Z",
            "url": f"https://example/c{ts}"}


def agent(kind, rev, ts, body="text", **extra):
    return comment(OWNER, sdlc.marker(kind, rev, **extra) + "\n" + body, ts)


def issue(labels):
    return {"number": 12, "title": "Alarm manager", "state": "open", "author": POSTER, "labels": list(labels)}


def derive(labels, comments):
    return sdlc.derive_state(issue(labels), comments, {OWNER}, CONFIG)


APPROVED = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER, via="comment")]


def review(rev, ts, verdict):
    return agent("plan-review", rev, ts, verdict=verdict)


def leaf(key, blocked_by=(), covers=(1,)):
    return {"key": key, "title": f"do {key}", "blocked_by": list(blocked_by), "covers": list(covers),
            "outcome": "o", "scope": "s", "acceptance": ["a"], "validation": "v", "demo": "d",
            "non_goals": "n", "context": "c", "complexity": "routine", "complexity_reason": "follows T0"}


def make_plan():
    return {"issue": 12, "kind": "feature",
            "milestones": [{"key": "M1", "title": "read", "demo": "list alarms"}],
            "subtasks": [
                {"key": "T1", "title": "model", "milestone": "M1", "summary": "parse alarms",
                 "slices": [leaf("T1.1", covers=(1, 2)), leaf("T1.2", ["T1.1"], covers=(3,))]},
                {**leaf("T2", ["T1.2"], covers=(4,)), "milestone": "M1"},
            ]}


# --- state -----------------------------------------------------------------

def test_planning_actions_progress():
    assert derive(["sdlc:approved"], APPROVED)["action"] == "plan"
    st = derive(["sdlc:approved"], APPROVED + [agent("plan", 1, 3)])
    assert (st["action"], st["latest_plan"]["rev"], st["plan_verdict"]) == ("continue_plan", 1, None)
    st = derive(["sdlc:approved"], APPROVED + [agent("plan", 1, 3), review(1, 4, "changes")])
    assert st["action"] == "continue_plan" and st["plan_rounds"] == 1
    st = derive(["sdlc:approved"], APPROVED + [agent("plan", 1, 3), review(1, 4, "approve")])
    assert st["action"] == "create_plan_issues"


def test_a_plan_revision_never_voids_the_prd_sign_off():
    st = derive(["sdlc:approved"], APPROVED + [agent("plan", 1, 3), agent("plan", 2, 4)])
    assert st["approved_rev"] == 1 and st["reconcile"] is None and st["conflicts"] == []
    assert st["latest_doc"]["kind"] == "prd" and st["rounds"] == 1   # triage budget untouched


def test_approval_of_an_older_plan_revision_does_not_count():
    st = derive(["sdlc:approved"], APPROVED + [agent("plan", 1, 3), review(1, 4, "approve"), agent("plan", 2, 5)])
    assert st["action"] == "continue_plan"


def test_spent_codex_rounds_mean_ask_the_poster():
    comments = APPROVED + [agent("plan", 1, 3)] + [review(1, 4 + i, "changes") for i in range(3)]
    st = derive(["sdlc:approved"], comments)
    assert st["action"] == "ask_poster"
    assert sdlc.check_transition(st, "needs-info", "question", CONFIG) == []


def test_poster_answer_to_a_planning_question_means_replan_with_fresh_rounds():
    comments = (APPROVED + [agent("plan", 1, 3)] + [review(1, 4 + i, "changes") for i in range(3)]
                + [agent("question", None, 8, phase="plan")])
    waiting = derive(["sdlc:needs-info"], comments)
    assert (waiting["turn"], waiting["plan_rounds"], waiting["rounds"]) == ("poster", 0, 1)
    st = derive(["sdlc:needs-info"], comments + [comment(POSTER, "the quiet one", 9)])
    assert (st["turn"], st["action"]) == ("agent", "replan")


def test_triage_needs_info_reply_is_still_respond_to_reply():
    st = derive(["sdlc:needs-info"], [agent("question", None, 1), comment(POSTER, "answer", 2)])
    assert st["action"] == "respond_to_reply"


def test_planning_question_ignores_the_triage_budget():
    many = [agent("question", None, i) for i in range(1, 5)] + [agent("prd", 1, 5),
                                                                 agent("approval", 1, 6, by=POSTER, via="comment")]
    st = derive(["sdlc:approved"], many)
    assert st["rounds"] == 5 and sdlc.check_transition(st, "needs-info", "question", CONFIG) == []


def test_planning_question_is_marked_phase_plan(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    f, q = tmp_path / "b.json", tmp_path / "q.md"
    f.write_text(json.dumps({"issue": issue(["sdlc:approved"]), "comments": APPROVED, "trusted": [OWNER]}))
    q.write_text("Quiet or loud?")
    assert sdlc.main(["transition", "12", "needs-info", "--kind", "question", "--body-file", str(q),
                      "--from-file", str(f), "--dry-run"]) == 0
    assert "kind=question phase=plan" in capsys.readouterr().out


# --- validation ------------------------------------------------------------

def test_a_good_plan_validates():
    assert sdlc.validate_plan(make_plan(), {1, 2, 3, 4}) == []


def test_missing_section_and_bad_dependencies():
    plan = make_plan()
    plan["subtasks"][0]["slices"][0]["validation"] = " "
    plan["subtasks"][0]["slices"][1]["blocked_by"] = ["T1"]          # a container, not a leaf
    plan["subtasks"][1]["blocked_by"] = ["T2"]
    errs = sdlc.validate_plan(plan)
    assert any("T1.1 has no Validation" in e for e in errs)
    assert any("blocked by T1," in e for e in errs)
    assert any("T2 is blocked by itself" in e for e in errs)


def test_cycle_is_reported_with_its_path():
    plan = make_plan()
    plan["subtasks"][0]["slices"][0]["blocked_by"] = ["T2"]
    errs = sdlc.validate_plan(plan)
    assert "dependency cycle (each waits on the next): T1.1 -> T2 -> T1.2 -> T1.1" in errs


def test_coverage_of_prd_criteria():
    errs = sdlc.validate_plan(make_plan(), {1, 2, 3, 4, 5})
    assert errs == ["acceptance criteria no slice covers: 5"]
    errs = sdlc.validate_plan(make_plan(), {1, 2, 3})
    assert errs == ["covers criteria the PRD doesn't have: 4"]


def test_duplicate_keys_and_milestones():
    plan = make_plan()
    plan["subtasks"][1]["key"] = "T1.1"
    plan["subtasks"][1]["milestone"] = "M9"
    plan["subtasks"][0]["slices"][1]["blocked_by"] = []
    errs = sdlc.validate_plan(plan)
    assert "duplicate key T1.1" in errs and any("needs a milestone" in e for e in errs)


def test_container_may_not_carry_blocked_by():
    plan = make_plan()
    plan["subtasks"][0]["blocked_by"] = ["T2"]
    assert any("put blocked_by on the slices" in e for e in sdlc.validate_plan(plan))


def test_parse_criteria_reads_acceptance_and_addendum_only():
    prd = ("## Scope\n1. not this\n## Acceptance criteria\n\n1. a\n2. b\n### sub\n3. c\n"
           "## Open questions\n4. no\n## Addendum (requested)\n22. ring\n## Resolved\n5. no\n")
    assert sdlc.parse_criteria(prd) == {1, 2, 3, 22}


def test_real_prd_12_has_criteria_1_to_22():
    path = sdlc.prd_path(12)
    assert path and sdlc.parse_criteria(path.read_text()) == set(range(1, 23))


def test_topo_order_puts_blockers_first():
    leaves = sdlc.plan_leaves(make_plan())
    assert sdlc.topo_order(leaves) == ["T1.1", "T1.2", "T2"]
    assert [l["_parent"] for l in leaves] == ["T1", "T1", None]


# --- rendering and verdicts -------------------------------------------------

def test_plan_round_trips_through_its_comment_even_with_fences_inside():
    plan = make_plan()
    plan["subtasks"][1]["validation"] = "```bash\nuv run pytest\n```"
    body = sdlc.render_comment("plan", 1, sdlc.render_plan(plan), CONFIG)
    assert sdlc.parse_marker(body) == {"kind": "plan", "rev": "1"}
    assert sdlc.extract_plan(body) == plan
    assert "```mermaid" in body and "T1_1 --> T1_2" in body and "| 4 | `T2` |" in body
    assert "#1" not in body.split("<details>")[0]   # 'AC 1', never an accidental issue link
    superseded = sdlc.render_superseded(body, 2, "https://x")
    assert sdlc.extract_plan(superseded) == plan


@pytest.mark.parametrize("report,verdict", [
    ("1. fine\n\nVERDICT: approve\n", "approve"),
    ("**VERDICT: changes**", "changes"),
    ("findings\nverdict: Approve.\n\n", "approve"),
])
def test_verdict_parsing(report, verdict):
    assert sdlc.parse_verdict(report) == verdict


@pytest.mark.parametrize("report", ["", "   \n", "VERDICT: approve\nbut one more thing", "VERDICT: maybe",
                                    "I'd say VERDICT: approve"])
def test_missing_or_garbled_verdict_is_a_failed_run(report):
    with pytest.raises(sdlc.SdlcError):
        sdlc.parse_verdict(report)


def test_leaf_body_carries_marker_sections_and_dependency_prose():
    l = sdlc.plan_leaves(make_plan())[1]
    body = sdlc.render_leaf_body(12, l, 40, {"T1.1": 41}, "https://prd")
    assert sdlc.parse_marker(body) == {"kind": "slice", "epic": "12", "key": "T1.2"}
    assert "Epic: #12 · Parent: #40 · Covers acceptance criteria 3 of [the PRD](https://prd)" in body
    for name in sdlc.LEAF_SECTIONS.values():
        assert f"## {name}" in body
    assert "- [ ] a" in body and "Blocked by #41 (T1.1)" in body


# --- creation --------------------------------------------------------------

def test_create_actions_from_nothing():
    acts = sdlc.plan_create_actions(make_plan(), {}, {}, {})
    assert acts == [("milestone", "M1"), ("create", "T1"), ("attach", None, "T1"),
                    ("create", "T1.1"), ("attach", "T1", "T1.1"),
                    ("create", "T1.2"), ("attach", "T1", "T1.2"), ("block", "T1.2", "T1.1"),
                    ("create", "T2"), ("attach", None, "T2"), ("block", "T2", "T1.2")]


def test_create_actions_resume_only_what_is_missing():
    existing = {"T1": 40, "T1.1": 41, "T1.2": 42}
    attached = {12: {40}, 40: {41}}
    blocked = {42: {41}}
    acts = sdlc.plan_create_actions(make_plan(), existing, attached, blocked)
    assert acts == [("milestone", "M1"), ("attach", "T1", "T1.2"),
                    ("create", "T2"), ("attach", None, "T2"), ("block", "T2", "T1.2")]


def test_create_actions_when_everything_exists():
    existing = {"T1": 40, "T1.1": 41, "T1.2": 42, "T2": 43}
    acts = sdlc.plan_create_actions(make_plan(), existing, {12: {40, 43}, 40: {41, 42}}, {42: {41}, 43: {42}})
    assert acts == []


def _bundle(tmp_path, comments, labels=("sdlc:approved",), **extra):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue(labels), "comments": comments, "trusted": [OWNER], **extra}))
    return str(f)


def test_plan_create_refuses_without_consensus(tmp_path, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("must not call gh"))
    plan_c = comment(OWNER, sdlc.render_comment("plan", 1, sdlc.render_plan(make_plan()), CONFIG), 3)
    f = _bundle(tmp_path, APPROVED + [plan_c, review(1, 4, "changes")])
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run"]) == 1


def test_plan_create_dry_run_lists_the_tree(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "check_plan", lambda plan, *a, **k: [])
    plan_c = comment(OWNER, sdlc.render_comment("plan", 1, sdlc.render_plan(make_plan()), CONFIG), 3)
    f = _bundle(tmp_path, APPROVED + [plan_c, review(1, 4, "approve")])
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would create milestone '#12 M1: read'" in out
    assert "would create '#12 T1.2: do T1.2' [plan:slice] in milestone M1" in out
    assert "would mark #new:T2 (T2) blocked by #new:T1.2 (T1.2)" in out
    assert "sdlc:planned" in out


def test_plan_post_dry_run_and_its_guards(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "check_plan", lambda plan, *a, **k: [])
    pf = tmp_path / "plan.json"
    pf.write_text(json.dumps(make_plan()))
    assert sdlc.main(["plan-post", "12", str(pf), "--from-file", _bundle(tmp_path, APPROVED), "--dry-run"]) == 0
    assert "kind=plan rev=1" in capsys.readouterr().out
    spent = APPROVED + [agent("plan", 1, 3)] + [review(1, 4 + i, "changes") for i in range(3)]
    assert sdlc.main(["plan-post", "12", str(pf), "--from-file", _bundle(tmp_path, spent), "--dry-run"]) == 1
    other = copy.deepcopy(make_plan())
    other["issue"] = 7
    pf.write_text(json.dumps(other))
    assert sdlc.main(["plan-post", "12", str(pf), "--from-file", _bundle(tmp_path, APPROVED), "--dry-run"]) == 1


def test_plan_review_records_round_and_verdict(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    rep, resp, pf = tmp_path / "r.md", tmp_path / "s.md", tmp_path / "plan.json"
    rep.write_text("1. T2 is too big\n\nVERDICT: changes\n")
    resp.write_text("1. Accepted: split into T2.1/T2.2 in rev 2.")
    pf.write_text(json.dumps(make_plan()))
    plan_c = comment(OWNER, sdlc.render_comment("plan", 1, sdlc.render_plan(make_plan()), CONFIG), 3)
    f = _bundle(tmp_path, APPROVED + [plan_c])
    args = ["plan-review", "12", "--plan", str(pf), "--report", str(rep), "--from-file", f, "--dry-run"]
    assert sdlc.main(args + ["--response", str(resp)]) == 0
    out = capsys.readouterr().out
    assert "kind=plan-review rev=1 verdict=changes round=1" in out and "Accepted: split" in out
    rep.write_text("looks fine")
    assert sdlc.main(args) == 1


def test_plan_review_refuses_a_file_that_differs_from_the_posted_revision(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("must not call gh"))
    rep, pf = tmp_path / "r.md", tmp_path / "plan.json"
    rep.write_text("VERDICT: approve")
    edited = make_plan()
    edited["subtasks"][1]["title"] = "edited after posting"
    pf.write_text(json.dumps(edited))
    plan_c = comment(OWNER, sdlc.render_comment("plan", 1, sdlc.render_plan(make_plan()), CONFIG), 3)
    f = _bundle(tmp_path, APPROVED + [plan_c])
    assert sdlc.main(["plan-review", "12", "--plan", str(pf), "--report", str(rep), "--from-file", f, "--dry-run"]) == 1
    assert "not plan rev 1 as posted" in capsys.readouterr().err


def test_revising_after_an_approve_on_the_last_round_leaves_no_round_to_review_it():
    comments = (APPROVED + [agent("plan", 1, 3), review(1, 4, "changes"), review(1, 5, "changes"),
                            review(1, 6, "approve")])
    assert derive(["sdlc:approved"], comments)["action"] == "create_plan_issues"
    # taking Codex's last non-blocking suggestions now would cost a poster question: the skill says don't
    assert derive(["sdlc:approved"], comments + [agent("plan", 2, 7)])["action"] == "ask_poster"


def test_ready_needs_every_blocker_completed():
    leaves = [{"number": 41, "key": "T1.1", "state": "open"}, {"number": 42, "key": "T1.2", "state": "open"},
              {"number": 43, "key": "T2", "state": "open"}, {"number": 44, "key": "T0", "state": "closed"}]
    blockers = {42: [{"number": 44, "state": "closed", "state_reason": "completed"}],
                43: [{"number": 42, "state": "open"}, {"number": 45, "state": "closed", "state_reason": "not_planned"}]}
    ready, waiting = sdlc.ready_leaves(leaves, blockers)
    assert [r["key"] for r in ready] == ["T1.1", "T1.2"]
    assert [(w["key"], w["unmet"]) for w in waiting] == [("T2", [42, 45])]


# --- complexity and the model that builds a slice (#49) ---------------------------

def test_every_unit_needs_a_complexity_and_a_reason():
    plan = make_plan()
    plan["subtasks"][0]["slices"][0]["complexity"] = "hard"
    del plan["subtasks"][1]["complexity_reason"]
    errs = sdlc.validate_plan(plan)
    assert "T1.1 needs a complexity: one of routine, judgment, novel" in errs
    assert any(e.startswith("T2 needs a complexity_reason") for e in errs)


def test_complexity_reaches_the_slice_body_and_the_plan_comment():
    l = {**leaf("T1.1"), "complexity": "judgment", "complexity_reason": "touches the\n journal"}
    body = sdlc.render_leaf_body(12, l, 24, {}, None)
    assert "Complexity: judgment — touches the journal" in body.splitlines()[2]
    assert "*routine*" in sdlc.render_plan(make_plan())


def test_the_model_comes_from_the_config():
    cfg = {**CONFIG, "models": {"routine": "haiku", "judgment": "opus"}}
    assert sdlc.model_for("routine", cfg) == "haiku" and sdlc.model_for("novel", cfg) is None
    leaves = [{"number": 30, "key": "T1", "state": "open", "assignees": [], "labels": ["plan:slice", "complexity:routine"],
               "complexity": "routine", "milestone": None}]
    p = sdlc.summarise_progress(leaves, {})
    assert p["units"][30] == {"key": "T1", "complexity": "routine", "milestone": "all"}
    p["units"][30]["model"] = sdlc.model_for("routine", cfg)
    epic = {"number": 12, "action": "work_slices", "state": "in-progress", "conflicts": []}
    assert sdlc.next_command(epic, p) == "/work-slice 30 (haiku: routine)"
    assert sdlc.complexity_of(["plan:slice", "complexity:novel"]) == "novel"
    assert sdlc.complexity_of(["complexity:weird"]) is None


def test_claim_records_the_model():
    claim = agent("claim", None, 1, branch="slice/28", model="sonnet")
    assert sdlc.claim_status([claim], {OWNER})["model"] == "sonnet"


def test_plan_annotate_backfills_once():
    body = sdlc.render_leaf_body(12, {k: v for k, v in leaf("T1.1").items() if not k.startswith("complexity")}, 24, {}, None)
    assert "Complexity:" not in body
    records = [{"kind": "slice", "number": 28, "key": "T1.1", "labels": ["plan:slice"], "body": body},
               {"kind": "slice", "number": 29, "key": "T9", "labels": ["plan:slice"], "body": body}]
    acts = sdlc.annotate_actions(records, {"T1.1": ("judgment", "the journal")})
    assert [(a["number"], a["add"], a["drop"]) for a in acts] == [(28, ["complexity:judgment"], [])]
    new = acts[0]["body"]
    assert new.splitlines()[2] == "Complexity: judgment — the journal"
    assert new.replace("Complexity: judgment — the journal\n", "") == body       # nothing else touched
    done = [{**records[0], "labels": ["plan:slice", "complexity:judgment"], "body": new}]
    assert sdlc.annotate_actions(done, {"T1.1": ("judgment", "the journal")}) == []
    changed = sdlc.annotate_actions(done, {"T1.1": ("novel", "unknown API")})
    assert changed[0]["add"] == ["complexity:novel"] and changed[0]["drop"] == ["complexity:judgment"]
    assert "Complexity: novel — unknown API" in changed[0]["body"] and changed[0]["body"].count("Complexity:") == 1


def test_bootstrap_creates_the_complexity_labels():
    names = {l["name"] for l in CONFIG["extra_labels"]}
    assert {f"complexity:{c}" for c in sdlc.COMPLEXITY} <= names


# --- one milestone's issues at a time (#44) -----------------------------------------

def two_milestones(**extra):
    plan = make_plan()
    plan["milestones"].append({"key": "M2", "title": "write", "demo": "add an alarm"})
    plan["subtasks"].append({**leaf("T3", ["T2"], covers=(5,)), "milestone": "M2"})
    return {**plan, **extra}


def plan_comment(plan, rev, ts):
    return comment(OWNER, sdlc.render_comment("plan", rev, sdlc.render_plan(plan), CONFIG), ts)


DEMO_M1 = [agent("demo", 1, 20, milestone="M1", sha="d" * 40), comment(POSTER, "/approve", 21), agent("demo-approval", 1, 21, milestone="M1", by=POSTER)]


def created_m1(plan):
    return APPROVED + [plan_comment(plan, 1, 3), review(1, 4, "approve"), agent("plan-created", 1, 5, created="M1")]


def test_plan_create_makes_only_the_first_milestone_by_default(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "check_plan", lambda plan, *a, **k: [])
    f = _bundle(tmp_path, APPROVED + [plan_comment(two_milestones(), 1, 3), review(1, 4, "approve")])
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "#12: creating M1; later: M2" in out and "'#12 T2: do T2'" in out
    assert "M2" not in out.split("later: M2")[1] and "T3" not in out           # nothing of M2 yet
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run", "--milestone", "M2"]) == 1
    assert "M2 comes after M1, which aren't created yet" in capsys.readouterr().err


def test_create_all_keeps_the_old_behaviour(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "check_plan", lambda plan, *a, **k: [])
    f = _bundle(tmp_path, APPROVED + [plan_comment(two_milestones(create="all"), 1, 3), review(1, 4, "approve")])
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "'#12 T3: do T3'" in out and "creating" not in out
    cfg = {**CONFIG, "create": "all"}
    st = derive(["sdlc:approved"], APPROVED + [plan_comment(two_milestones(), 1, 3), review(1, 4, "approve")])
    assert sdlc.creation_target(two_milestones(), st, cfg) == (None, ["M1", "M2"])


def test_the_next_milestone_is_replanned_after_the_last_ones_demo():
    plan = two_milestones()
    building = derive(["sdlc:in-progress"], created_m1(plan))
    assert (building["action"], building["created_milestones"], building["uncreated_milestones"]) == ("work_slices", ["M1"], ["M2"])
    due = derive(["sdlc:in-progress"], created_m1(plan) + DEMO_M1)
    assert due["action"] == "plan_next_milestone" and due["plan_rounds"] == 0      # a fresh round budget
    assert sdlc.next_command(due) == "/plan-issue 12: plan M2 with what earlier milestones taught"
    unshipped = {"ready": [], "in_flight": [], "escalated": [], "waiting": [], "open": 0,
                 "milestones": [{"title": "#12 M1: read", "key": "M1", "done": 3, "total": 3}]}
    assert sdlc.next_command(due, unshipped).startswith("/milestone-demo 12: M1 is accepted; ship it")
    replan = created_m1(plan) + DEMO_M1 + [plan_comment({**plan, "lessons": {"milestone": "M1", "text": "t"}}, 2, 30)]
    assert derive(["sdlc:in-progress"], replan)["action"] == "continue_plan"
    agreed = derive(["sdlc:in-progress"], replan + [review(2, 31, "approve")])
    assert agreed["action"] == "create_plan_issues"
    assert sdlc.creation_target(plan, agreed, CONFIG) == ({"M1", "M2"}, ["M1", "M2"])
    built = derive(["sdlc:in-progress"], replan + [review(2, 31, "approve"), agent("plan-created", 2, 32, created="M1,M2")])
    assert (built["action"], built["uncreated_milestones"]) == ("work_slices", [])


def test_demo_changes_before_acceptance_dont_create_the_next_milestone():
    plan = two_milestones()
    st = derive(["sdlc:in-progress"], created_m1(plan) + [agent("demo", 1, 20, milestone="M1", sha="d" * 40)])
    assert sdlc.creation_target(plan, st, CONFIG) == ({"M1"}, ["M1"])     # only new issues of M1 (fix slices)


def test_a_legacy_plan_created_marker_means_everything_exists():
    st = derive(["sdlc:in-progress"], APPROVED + [plan_comment(two_milestones(), 1, 3), review(1, 4, "approve"),
                                                  agent("plan-created", 1, 5)] + DEMO_M1)
    assert (st["uncreated_milestones"], st["action"]) == ([], "work_slices")


def test_a_replan_needs_its_lessons(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "check_plan", lambda plan, *a, **k: [])
    f = _bundle(tmp_path, created_m1(two_milestones()) + DEMO_M1, labels=("sdlc:in-progress",))
    pf = tmp_path / "plan.json"
    pf.write_text(json.dumps(two_milestones()))
    assert sdlc.main(["plan-post", "12", str(pf), "--from-file", f, "--dry-run"]) == 1
    assert "needs `lessons`" in capsys.readouterr().err
    pf.write_text(json.dumps(two_milestones(lessons={"milestone": "M1", "text": "Reads were slow; M2 caches them."})))
    assert sdlc.main(["plan-post", "12", str(pf), "--from-file", f, "--dry-run"]) == 0
    assert "## Lessons from M1\n\nReads were slow" in capsys.readouterr().out


def test_validation_of_create_lessons_and_later_milestone_blockers():
    plan = two_milestones(create="some", lessons={"milestone": "M9", "text": "x"})
    plan["subtasks"][0]["slices"][0]["blocked_by"] = ["T3"]
    errs = sdlc.validate_plan(plan)
    assert any("`create` is" in e for e in errs) and any("`lessons` is" in e for e in errs)
    assert any(e.startswith("T1.1 (M1) is blocked by T3 of a later milestone (M2)") for e in errs)
    assert sdlc.validate_plan(two_milestones()) == []        # M2 waiting on M1 is fine


def test_create_actions_for_one_milestone_resume_and_link_back():
    plan = two_milestones()
    m1 = sdlc.plan_create_actions(plan, {}, {}, {}, {"M1"})
    assert ("create", "T3") not in m1 and ("create", "T2") in m1
    # M2 after M1 exists: only T3, attached and blocked by M1's T2; a rerun after a failure finishes it
    existing = {"T1": 1, "T1.1": 2, "T1.2": 3, "T2": 4}
    attached = {12: {1, 4}, 1: {2, 3}}
    blocked = {3: {2}, 4: {3}}
    m2 = sdlc.plan_create_actions(plan, existing, attached, blocked, {"M1", "M2"})
    assert m2 == [("milestone", "M1"), ("milestone", "M2"), ("create", "T3"), ("attach", None, "T3"), ("block", "T3", "T2")]
    half = sdlc.plan_create_actions(plan, {**existing, "T3": 5}, {12: {1, 4, 5}, 1: {2, 3}}, blocked, {"M1", "M2"})
    assert [a for a in half if a[0] != "milestone"] == [("block", "T3", "T2")]


def test_not_finished_while_milestones_are_uncreated():
    p = {"open": 0, "milestones": [{"key": "M1"}]}
    assert sdlc.finished(p, {"M1": {"accepted": True}}, {"M1": "x"})
    assert not sdlc.finished(p, {"M1": {"accepted": True}}, {"M1": "x"}, ["M2"])


def test_milestones_are_listed_in_the_order_they_ship():
    plan = two_milestones()
    plan["milestones"].reverse()
    assert any(e.startswith("list milestones in order (M1, M2)") for e in sdlc.validate_plan(plan))


def test_spent_rounds_on_an_amendment_escalate_instead_of_asking_the_poster():
    plan = two_milestones()
    rounds = [review(2, 31 + i, "changes") for i in range(CONFIG["max_plan_rounds"])]
    st = derive(["sdlc:in-progress"], created_m1(plan) + DEMO_M1 +
                [plan_comment({**plan, "lessons": {"milestone": "M1", "text": "t"}}, 2, 30)] + rounds)
    assert st["action"] == "escalate_plan"
    assert "escalate it (`transition 12 escalated" in sdlc.next_command(st)


def test_a_milestone_needs_units_of_work():
    plan = two_milestones()
    plan["subtasks"] = [{**t, "milestone": "M2"} for t in plan["subtasks"]]
    assert "milestone M1 has no units of work: give it some, or drop it" in sdlc.validate_plan(plan)


# --- codex-review --plan: Codex on the posted revision, held to the approved one --------------------------

import re  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

from tools import codex_review  # noqa: E402
from tests.fake_codex import APPROVES, NO_VERDICT, QUICK, SETTINGS, SLOW, calls, install, no_real_codex_home  # noqa: E402,F401

DOC_URL = "https://github.com/o/r/issues/12#issuecomment-{}"


def doc(rev, ts, cid, text="## Acceptance criteria\n\n1. alarms are listed"):
    c = agent("prd", rev, ts, f"# PRD rev {rev}\n\n{text}")
    c["id"], c["url"] = cid, DOC_URL.format(cid)
    return c


def git_(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class PlanReview:
    """A primary checkout whose origin/main may hold docs/prd/12-*.md, the fake `codex`, and the issue's bundle.
    `epic`: files committed on top of main as origin/epic/12 (an epic being built), its sha in `self.epic`."""

    def __init__(self, tmp_path, monkeypatch, docs=None, plan=(APPROVES,), settings=SETTINGS, epic=None):
        self.tmp, self.root = tmp_path, tmp_path / "primary"
        self.log, _, _ = install(tmp_path, monkeypatch, plan)
        for k in ("AUTHOR", "COMMITTER"):
            monkeypatch.setenv(f"GIT_{k}_NAME", "t")
            monkeypatch.setenv(f"GIT_{k}_EMAIL", "t@example.invalid")
        self.root.mkdir()
        git_("init", "-q", cwd=self.root)
        (self.root / "CLAUDE.md").write_text("the repo\n")
        for name, text in (docs or {}).items():
            (self.root / "docs" / "prd").mkdir(parents=True, exist_ok=True)
            (self.root / "docs" / "prd" / name).write_text(text)
        git_("add", ".", cwd=self.root)
        git_("commit", "-qm", "main", cwd=self.root)
        git_("update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.root)
        self.main = git_("rev-parse", "HEAD", cwd=self.root)
        self.epic = None
        if epic is not None:
            git_("checkout", "-q", "-b", "epic", cwd=self.root)
            for name, text in epic.items():
                (self.root / name).parent.mkdir(parents=True, exist_ok=True)
                (self.root / name).write_text(text)
            git_("add", ".", cwd=self.root)
            git_("commit", "-q", "--allow-empty", "-m", "a slice merged into epic/12", cwd=self.root)
            git_("update-ref", "refs/remotes/origin/epic/12", "HEAD", cwd=self.root)
            self.epic = git_("rev-parse", "HEAD", cwd=self.root)
            git_("checkout", "-q", "-", cwd=self.root)
        monkeypatch.setattr(sdlc, "primary_root", lambda: self.root)
        monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("codex-review --from-file must not call gh"))
        self.config = tmp_path / "config.json"
        self.config.write_text(json.dumps({**CONFIG, "codex": settings}))
        self.out = tmp_path / "scratch"

    def run(self, comments, *extra, labels=("sdlc:approved",)):
        return sdlc.main(["--config", str(self.config), "codex-review", "--plan", "12", "--out", str(self.out),
                          "--from-file", _bundle(self.tmp, comments, labels), *extra])

    def prompt(self):
        return (self.out / "prompt.md").read_text()


APPROVED_555 = [doc(1, 1, 444), agent("approval", 1, 2, by=POSTER), doc(2, 3, 555), agent("approval", 2, 4, by=POSTER)]
POSTED = [plan_comment(make_plan(), 1, 5)]


def test_a_plan_round_reviews_the_posted_json_in_a_scratch_checkout_of_main(tmp_path, monkeypatch, capsys):
    r = PlanReview(tmp_path, monkeypatch)                      # no docs/prd/12-*.md merged yet
    assert r.run(APPROVED_555 + POSTED) == 0
    prompt, saved = r.prompt(), (r.out / "codex.md").read_text()
    # the posted plan, not a local file, and the approved comment written out, named in the prompt
    assert json.loads((r.out / "plan.json").read_text()) == make_plan() and f"`{r.out / 'plan.json'}`" in prompt
    assert f"`{r.out / 'requirements.md'}`" in prompt and "# PRD rev 2" in (r.out / "requirements.md").read_text()
    assert "sdlc:v1" not in (r.out / "requirements.md").read_text()
    assert "This is round 1." in prompt and "planner's answer" not in prompt and not re.search(r"<[A-Za-z/]", prompt)
    (call,) = calls(r.log)
    # run in a checkout of origin/main made for the round, on the isolated CODEX_HOME, and removed after
    assert call["cwd"] == str((r.out / "main").resolve()) and call["argv"][-1] == prompt
    assert call["codex_home"] == str(r.out / "codex-home") and call["entries"] == {"auth.json": False, "config.toml": False}
    assert call["config"] == codex_review.config_toml(SETTINGS)
    assert not (r.out / "main").exists() and str(r.out / "main") not in git_("worktree", "list", cwd=r.root)
    assert saved.startswith(f"HEAD: {r.main}\nModel: test-model\n") and saved.rstrip().endswith("VERDICT: approve")
    # plan-review takes the plan the command wrote, and shows what the round ran under
    capsys.readouterr()
    assert sdlc.main(["plan-review", "12", "--plan", str(r.out / "plan.json"), "--report", str(r.out / "codex.md"),
                      "--from-file", _bundle(tmp_path, APPROVED_555 + POSTED), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "verdict=approve round=1 response=0 model=test-model effort=low codex=9.9.9-test prompt=" in out
    assert "Ran `test-model` at effort `low`, codex `9.9.9-test`" in out


@pytest.mark.parametrize("docs, names", [
    ({"12-alarms.md": f"# PRD (#12)\n\nApproved revision 2 ([PRD]({DOC_URL.format(555)})).\n\n## Goals\n"}, "file"),
    # an older revision's doc, and one whose anchor only starts with the approved id: the comment is used
    ({"12-alarms.md": f"# PRD (#12)\n\nApproved revision 1 ([PRD]({DOC_URL.format(444)})).\n\n## Goals\n"}, "comment"),
    ({"12-alarms.md": f"# PRD (#12)\n\nApproved revision 9 ([PRD]({DOC_URL.format(5550)})).\n\n## Goals\n"}, "comment"),
    # the link has to be in the header, not quoted further down
    ({"12-alarms.md": f"# PRD (#12)\n\nApproved revision 1.\n\n## Notes\n\nsee {DOC_URL.format(555)}\n"}, "comment"),
])
def test_the_requirements_are_the_approved_revision(tmp_path, monkeypatch, capsys, docs, names):
    r = PlanReview(tmp_path, monkeypatch, docs=docs)
    assert r.run(APPROVED_555 + POSTED, "--dry-run") == 0
    prompt, out = r.prompt(), capsys.readouterr().out
    if names == "file":
        assert "the approved PRD it must deliver: `docs/prd/12-alarms.md`" in prompt
        assert "links comment 555" in out and not (r.out / "requirements.md").exists()
    else:
        assert f"`{r.out / 'requirements.md'}`" in prompt and "# PRD rev 2" in (r.out / "requirements.md").read_text()
        assert "the approved comment 555" in out
    assert calls(r.log) == [] and not (r.out / "main").exists()      # --dry-run runs nothing, makes no checkout


def test_round_two_takes_the_last_rounds_response_even_across_a_new_revision(tmp_path, monkeypatch, capsys):
    r = PlanReview(tmp_path, monkeypatch)
    body, extra = sdlc.review_body("**Round 1/3 — Codex: `changes`**", "Codex's review",
                                   "1. T2 is too big\n\nVERDICT: changes", "1. Accepted: split T2 in rev 2.")
    round1 = comment(OWNER, sdlc.render_comment("plan-review", 1, body, CONFIG, verdict="changes", round="1", **extra), 6)
    rev2 = plan_comment(two_milestones(), 2, 7)
    assert r.run(APPROVED_555 + POSTED + [round1, rev2], "--dry-run") == 0
    prompt = r.prompt()
    resp = r.out / "response-round-1.md"
    assert "This is round 2." in prompt and f"`{resp}`" in prompt and "planner's answer" in prompt
    assert resp.read_text().strip() == "1. Accepted: split T2 in rev 2."
    assert json.loads((r.out / "plan.json").read_text()) == two_milestones()       # the latest revision
    # a round that asked for changes with no response recorded: refused
    bare = agent("plan-review", 1, 6, "**Round 1**\n\n<details><summary>x</summary>\n\nr\n\n</details>",
                 verdict="changes", round="1", response="0")
    assert r.run(APPROVED_555 + POSTED + [bare, rev2], "--dry-run") == 1
    assert "plan-review --response" in capsys.readouterr().err


def test_no_verdict_is_retried_once_and_a_plan_not_under_review_is_refused(tmp_path, monkeypatch, capsys):
    r = PlanReview(tmp_path, monkeypatch, plan=(NO_VERDICT, APPROVES), epic={})
    assert r.run(APPROVED_555 + POSTED) == 0 and len(calls(r.log)) == 2
    r.log.unlink()
    (tmp_path / "plan.json").write_text(json.dumps([NO_VERDICT]))
    assert r.run(APPROVED_555 + POSTED) == codex_review.UNAVAILABLE and len(calls(r.log)) == 2
    assert not (r.out / "main").exists()
    # an epic being built has no plan under review: a dry run says so, a real run refuses before Codex
    r.log.unlink()
    built = APPROVED_555 + POSTED + [review(1, 6, "approve"), agent("plan-created", 1, 7)]
    assert r.run(built, "--dry-run", labels=("sdlc:in-progress",)) == 0
    assert "no plan under review" in capsys.readouterr().out
    assert r.run(built, labels=("sdlc:in-progress",)) == 1 and calls(r.log) == []
    assert "no plan under review" in capsys.readouterr().err
    # nothing approved: nothing to hold the plan to
    assert r.run([doc(1, 1, 444)] + POSTED, "--dry-run") == 1
    assert "no approved" in capsys.readouterr().err


def test_a_plan_round_that_times_out_is_retried_once(tmp_path, monkeypatch):
    r = PlanReview(tmp_path, monkeypatch, plan=(SLOW, APPROVES), settings=QUICK)
    assert r.run(APPROVED_555 + POSTED) == 0 and len(calls(r.log)) == 2
    assert "timed out after 1s" in (r.out / "codex.err").read_text() and (r.out / "codex.md").exists()


def test_a_directory_in_the_checkouts_place_is_never_deleted(tmp_path, monkeypatch, capsys):
    r = PlanReview(tmp_path, monkeypatch)
    (r.out / "main").mkdir(parents=True)
    (r.out / "main" / "keep.txt").write_text("someone's")
    assert r.run(APPROVED_555 + POSTED) == 1 and calls(r.log) == []
    assert "isn't a checkout this command made" in capsys.readouterr().err
    assert (r.out / "main" / "keep.txt").read_text() == "someone's"
    # one a cut-short run left registered is replaced
    (r.out / "main" / "keep.txt").unlink()
    (r.out / "main").rmdir()
    git_("worktree", "add", "-q", "--detach", str(r.out / "main"), r.main, cwd=r.root)
    assert r.run(APPROVED_555 + POSTED) == 0 and not (r.out / "main").exists()


# --- an amendment to an epic being built is reviewed on epic/N (#216) -------------------------------------

EPIC_ONLY = "NOTES-epic.md"
# the epic built M1 and its demo was accepted; rev 2 amends the plan while the epic is in progress
AMENDED = APPROVED_555 + [plan_comment(two_milestones(), 1, 5), review(1, 6, "approve"),
                          agent("plan-created", 1, 7, created="M1"),
                          agent("demo", 1, 20, milestone="M1", sha="d" * 40),
                          agent("demo-approval", 1, 21, milestone="M1", by=POSTER),
                          plan_comment({**two_milestones(), "lessons": {"milestone": "M1", "text": "t"}}, 2, 30)]


def test_an_amendment_is_in_progress_and_under_review():
    st = sdlc.derive_state(issue(["sdlc:in-progress"]), AMENDED, {OWNER}, CONFIG)
    assert (st["state"], st["action"]) == ("in-progress", "continue_plan")


def test_an_amendment_dry_run_reads_epic_n_and_says_it_is_ahead_of_main(tmp_path, monkeypatch, capsys):
    r = PlanReview(tmp_path, monkeypatch, epic={EPIC_ONLY: "built on epic/12\n"})
    assert r.run(AMENDED, "--dry-run", labels=("sdlc:in-progress",)) == 0
    out, prompt = capsys.readouterr().out, r.prompt()
    assert f"Codex reads epic/12 at {r.epic[:12]} (the epic's branch, ahead of main)" in out
    assert f"cd {r.out / 'epic-12'} && codex exec" in out
    assert "a checkout of `epic/12`)" in prompt and "the current directory is `epic/12`, the epic's own branch" in prompt
    assert "ahead of `main`" in prompt and not re.search(r"<[A-Za-z/]", prompt)
    assert calls(r.log) == [] and not (r.out / "epic-12").exists()


@pytest.mark.parametrize("labels", [("sdlc:approved",), ("sdlc:planning",)])
def test_a_first_plan_reads_main_even_when_an_epic_branch_exists(tmp_path, monkeypatch, capsys, labels):
    r = PlanReview(tmp_path, monkeypatch, epic={EPIC_ONLY: "x\n"})
    assert r.run(APPROVED_555 + POSTED, "--dry-run", labels=labels) == 0
    out, prompt = capsys.readouterr().out, r.prompt()
    assert f"Codex reads main at {r.main[:12]}" in out and "ahead of main" not in out
    assert "a checkout of `main`)" in prompt and "epic's own branch" not in prompt


def test_an_epic_being_built_without_its_branch_is_refused_not_read_as_main(tmp_path, monkeypatch, capsys):
    r = PlanReview(tmp_path, monkeypatch)                      # no origin/epic/12
    for flags in (("--dry-run",), ()):
        assert r.run(AMENDED, *flags, labels=("sdlc:in-progress",)) == 1
        err = capsys.readouterr().err
        assert "no origin/epic/12" in err and "never main" in err and "epic-branch 12" in err
    assert calls(r.log) == [] and not (r.out / "main").exists()


def _origin(tmp_path, branches):
    """A primary checkout with a local bare `origin` holding main and `branches`; no network."""
    bare, root = tmp_path / "origin.git", tmp_path / "primary"
    tmp_path.mkdir(parents=True)
    git_("init", "-q", "--bare", str(bare), cwd=tmp_path)
    root.mkdir()
    git_("init", "-q", cwd=root)
    git_("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "m", cwd=root)
    git_("remote", "add", "origin", str(bare), cwd=root)
    for b in ("main", *branches):
        git_("push", "-q", "origin", f"HEAD:refs/heads/{b}", cwd=root)
    return root, git_("rev-parse", "HEAD", cwd=root)


def test_plan_base_fetches_epic_n_and_refuses_a_missing_or_unfetchable_one(tmp_path):
    building, first = {"state": "in-progress"}, {"state": "approved"}
    root, sha = _origin(tmp_path / "a", ["epic/12"])
    assert sdlc.plan_base(12, building, CONFIG, root, fetch=True) == ("epic/12", sha)
    assert sdlc.plan_base(12, {"state": "planned"}, CONFIG, root, fetch=True) == ("epic/12", sha)
    assert sdlc.plan_base(12, first, CONFIG, root, fetch=True) == ("main", sha)
    # origin has no epic/12: refused, never main
    root, _ = _origin(tmp_path / "b", [])
    with pytest.raises(sdlc.SdlcError, match="could not fetch origin/epic/12.*never main.*epic-branch 12"):
        sdlc.plan_base(12, building, CONFIG, root, fetch=True)
    # origin unreachable: refused even though a stale origin/epic/12 is still here
    root, _ = _origin(tmp_path / "c", ["epic/12"])
    git_("fetch", "-q", "origin", "+refs/heads/epic/12:refs/remotes/origin/epic/12", cwd=root)
    git_("remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=root)
    with pytest.raises(sdlc.SdlcError, match="could not fetch origin/epic/12"):
        sdlc.plan_base(12, building, CONFIG, root, fetch=True)


def test_a_real_amendment_round_runs_codex_in_a_checkout_of_epic_n(tmp_path, monkeypatch):
    r = PlanReview(tmp_path, monkeypatch, epic={EPIC_ONLY: "only epic/12 has this\n"})
    # the fake `codex`, wrapped: it reads a file only epic/12 has from its working directory, into its report
    real, codex = tmp_path / "bin" / "codex-real", tmp_path / "bin" / "codex"
    codex.rename(real)
    codex.write_text(f"#!{sys.executable}\nimport os, pathlib, sys\n"
                     f"if sys.argv[1:] != ['--version']:\n"
                     f"    sys.stdout.write('read: ' + pathlib.Path({EPIC_ONLY!r}).read_text()); sys.stdout.flush()\n"
                     f"os.execv({str(real)!r}, [{str(real)!r}, *sys.argv[1:]])\n")
    codex.chmod(0o755)
    assert r.run(AMENDED, labels=("sdlc:in-progress",)) == 0
    (call,) = calls(r.log)
    saved = (r.out / "codex.md").read_text()
    assert call["cwd"] == str((r.out / "epic-12").resolve())
    assert saved.startswith(f"HEAD: {r.epic}\n") and r.epic != r.main
    assert "read: only epic/12 has this" in saved and saved.rstrip().endswith("VERDICT: approve")
    assert not (r.out / "epic-12").exists() and str(r.out / "epic-12") not in git_("worktree", "list", cwd=r.root)
    # and the round it made can be recorded
    assert sdlc.main(["plan-review", "12", "--plan", str(r.out / "plan.json"), "--report", str(r.out / "codex.md"),
                      "--from-file", _bundle(tmp_path, AMENDED, ("sdlc:in-progress",)), "--dry-run"]) == 0


# --- a person's /approve settles a plan whose Codex rounds are spent (#243 T12, #146's plan half) ----------------

STRANGER = "stranger"
SPENT = APPROVED + [agent("plan", 1, 3)] + [review(1, 4 + i, "changes") for i in range(CONFIG["max_plan_rounds"])]


def test_a_valid_approve_after_spent_rounds_settles_the_plan():
    st = derive(["sdlc:approved"], SPENT + [comment(POSTER, "ship it\n/approve", 10)])
    assert st["action"] == "create_plan_issues"
    assert (st["plan_settled"]["by"], st["plan_settled"]["rev"]) == (POSTER, 1)
    # a collaborator's counts too
    assert derive(["sdlc:approved"], SPENT + [comment(OWNER, "/approve", 10)])["plan_settled"]["by"] == OWNER


def test_a_strangers_or_an_agents_approve_never_settles():
    st = derive(["sdlc:approved"], SPENT + [comment(STRANGER, "/approve", 10)])
    assert st["action"] == "ask_poster" and st["plan_settled"] is None
    assert st["ignored_keywords"] == [{"by": STRANGER, "action": "approve"}]
    for c in (agent("note", None, 10, body="/approve"),                              # an agent's own comment
              comment(POSTER, sdlc.marker("note", None) + "\n/approve", 10)):        # a marker, whoever wrote it
        st = derive(["sdlc:approved"], SPENT + [c])
        assert (st["action"], st["plan_settled"]) == ("ask_poster", None)


def test_an_approve_before_the_latest_revision_or_with_rounds_left_changes_nothing():
    st = derive(["sdlc:approved"], SPENT + [comment(POSTER, "/approve", 10), agent("plan", 2, 11)])
    assert st["plan_settled"] is None and st["action"] == "ask_poster"
    left = APPROVED + [agent("plan", 1, 3), review(1, 4, "changes"), comment(POSTER, "/approve", 5)]
    st = derive(["sdlc:approved"], left)
    assert (st["action"], st["plan_settled"]) == ("continue_plan", None)
    # given with rounds left, it stays void once later rounds spend them
    st = derive(["sdlc:approved"], left + [review(1, 6, "changes"), review(1, 7, "changes")])
    assert (st["action"], st["plan_settled"]) == ("ask_poster", None)


def test_a_later_changes_withdraws_the_settling_approve():
    st = derive(["sdlc:approved"], SPENT + [comment(POSTER, "/approve", 10), comment(POSTER, "/changes fix it first", 11)])
    assert (st["action"], st["plan_settled"]) == ("ask_poster", None)
    st = derive(["sdlc:approved"], SPENT + [comment(POSTER, "/approve", 10), comment(OWNER, "/changes not yet", 11)])
    assert st["plan_settled"] is None
    # a stranger's /changes withdraws nothing, and a new /approve after the /changes settles again
    st = derive(["sdlc:approved"], SPENT + [comment(POSTER, "/approve", 10), comment(STRANGER, "/changes no", 11)])
    assert st["plan_settled"]["by"] == POSTER
    st = derive(["sdlc:approved"], SPENT + [comment(POSTER, "/changes x", 10), comment(POSTER, "/approve", 11)])
    assert st["action"] == "create_plan_issues"


def test_settled_after_the_poster_was_asked():
    asked = SPENT + [agent("question", None, 10, phase="plan")]
    assert derive(["sdlc:needs-info"], asked)["plan_cycle_rounds"] == CONFIG["max_plan_rounds"]
    st = derive(["sdlc:needs-info"], asked + [comment(POSTER, "the first plan is fine\n/approve", 11)])
    assert (st["action"], st["plan_settled"]["by"]) == ("create_plan_issues", POSTER)
    # the same /approve: `state` says it settles the plan, and it is never a PRD sign-off `transition` could record
    assert st["decision"]["settles_plan"] and not st["decision"]["valid"]
    assert "no valid /approve" in " ".join(sdlc.check_transition(st, "approved", "approval", CONFIG))
    # an answer without /approve is still a replan
    assert derive(["sdlc:needs-info"], asked + [comment(POSTER, "the quiet one", 11)])["action"] == "replan"


def three_revisions():
    return APPROVED + [agent("plan", 1, 3), review(1, 4, "changes"), agent("plan", 2, 5), review(2, 6, "changes"),
                       agent("plan", 3, 7), review(3, 8, "changes")]


def test_rounds_spent_across_revisions_settle_before_and_after_the_question():
    before = derive(["sdlc:approved"], three_revisions() + [comment(POSTER, "/approve", 10)])
    assert (before["action"], before["plan_settled"]["rev"]) == ("create_plan_issues", 3)
    asked = three_revisions() + [agent("question", None, 10, phase="plan")]
    after = derive(["sdlc:needs-info"], asked + [comment(POSTER, "/approve", 11)])
    assert (after["action"], after["plan_settled"]["rev"]) == ("create_plan_issues", 3)
    # a new Codex round after the reset ends that settling window: the /approve after it changes nothing
    fresh = asked + [comment(POSTER, "go on", 11), agent("plan", 4, 12), review(4, 13, "changes"),
                     comment(POSTER, "/approve", 14)]
    st = derive(["sdlc:approved"], fresh)
    assert (st["action"], st["plan_settled"], st["plan_cycle_rounds"]) == ("continue_plan", None, 1)
    # and an /approve inside the window is void once a new round is recorded, even on the same revision
    st = derive(["sdlc:approved"], asked + [comment(POSTER, "/approve", 11), review(3, 12, "changes")])
    assert (st["plan_settled"], st["action"]) == (None, "continue_plan")
    # and stays void when that new cycle spends its rounds too
    spent_again = asked + [comment(POSTER, "/approve", 11), review(3, 12, "changes"), review(3, 13, "changes"),
                           review(3, 14, "changes")]
    st = derive(["sdlc:approved"], spent_again)
    assert (st["plan_settled"], st["action"]) == (None, "ask_poster")
    # an /approve posted just before the question still settles it
    st = derive(["sdlc:needs-info"], three_revisions() + [comment(POSTER, "/approve", 9),
                                                          agent("question", None, 10, phase="plan")])
    assert (st["turn"], st["action"]) == ("agent", "create_plan_issues")


def spent_amendment(labels=("sdlc:in-progress",)):
    plan = two_milestones()
    rounds = [review(2, 31 + i, "changes") for i in range(CONFIG["max_plan_rounds"])]
    comments = (created_m1(plan) + DEMO_M1 + [plan_comment({**plan, "lessons": {"milestone": "M1", "text": "t"}}, 2, 30)]
                + rounds)
    return plan, comments


def test_settled_amendment_at_escalate_plan_and_once_escalated():
    _, comments = spent_amendment()
    assert derive(["sdlc:in-progress"], comments)["action"] == "escalate_plan"
    st = derive(["sdlc:in-progress"], comments + [comment(POSTER, "/approve", 40)])
    assert (st["action"], st["plan_settled"]["by"]) == ("create_plan_issues", POSTER)
    escalated = comments + [agent("escalation", None, 40, phase="plan")]
    assert derive(["sdlc:escalated"], escalated)["action"] == "human"
    assert derive(["sdlc:escalated"], escalated + [comment(STRANGER, "/approve", 41)])["action"] == "human"
    st = derive(["sdlc:escalated"], escalated + [comment(POSTER, "/approve", 41)])
    assert (st["turn"], st["action"], st["plan_settled"]["by"]) == ("agent", "create_plan_issues", POSTER)
    assert sdlc.next_command(st) == "/plan-issue 12"
    # an escalation before anything was created stays a human's (only an amendment's is settled here)
    first = SPENT + [agent("escalation", None, 10, phase="plan"), comment(POSTER, "/approve", 11)]
    assert derive(["sdlc:escalated"], first)["action"] == "human"
    # an escalation that isn't escalate_plan's (a sync conflict after the settling /approve) stays a human's,
    # and so does one with no phase (from before this) or one from before the round that spent the cycle
    st = derive(["sdlc:in-progress"], comments + [comment(POSTER, "/approve", 40)])
    assert st["action"] == "create_plan_issues"
    for esc in (agent("escalation", None, 41), agent("escalation", None, 41, phase="sync")):
        assert derive(["sdlc:escalated"], comments + [comment(POSTER, "/approve", 40), esc])["action"] == "human"
    early = comments[:-1] + [agent("escalation", None, 32, phase="plan"), comments[-1], comment(POSTER, "/approve", 41)]
    assert derive(["sdlc:escalated"], early)["action"] == "human"


def test_the_escalation_at_escalate_plan_is_marked_phase_plan(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    _, comments = spent_amendment()
    f = _bundle(tmp_path, comments, labels=("sdlc:in-progress",))
    assert sdlc.main(["transition", "12", "escalated", "--kind", "escalation", "--reason", "no consensus",
                      "--from-file", f, "--dry-run"]) == 0
    assert "kind=escalation phase=plan" in capsys.readouterr().out


def test_a_settled_creation_cut_short_after_its_record_can_finish(tmp_path, monkeypatch):
    plan_c = comment(OWNER, sdlc.render_comment("plan", 1, sdlc.render_plan(make_plan()), CONFIG), 3)
    settled = (APPROVED + [plan_c] + [review(1, 4 + i, "changes") for i in range(CONFIG["max_plan_rounds"])]
               + [comment(POSTER, "/approve", 10)])
    record = agent("plan-created", 1, 11, settled_by=POSTER)
    for labels in (["sdlc:approved"], ["sdlc:needs-info"]):
        st = derive(labels, settled + [record])
        assert (st["action"], st["plan_settled"]["by"]) == ("create_plan_issues", POSTER)
    f = _bundle(tmp_path, settled + [record])
    posts = _Posts(monkeypatch)
    github = _FromGitHub(monkeypatch, f)
    assert sdlc.main(["plan-create", "12"]) == 0
    assert posts.labels == ["planned"] and posts.comments == []          # the record is not posted twice
    assert github.branches == [12]
    # a record without settled_by (an old one, or Codex's approve) grants nothing more than before
    assert derive(["sdlc:approved"], settled + [agent("plan-created", 1, 11)])["action"] == "continue_plan"


class _Posts:
    """plan-create offline, for real: every GitHub write recorded, none sent."""

    def __init__(self, monkeypatch):
        self.comments, self.labels, self.n = [], [], 500
        monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("must not call gh directly"))
        monkeypatch.setattr(sdlc, "check_plan", lambda plan, *a, **k: [])
        monkeypatch.setattr(sdlc.time, "sleep", lambda s: None)
        monkeypatch.setattr(sdlc, "post_json", self.post_json)
        monkeypatch.setattr(sdlc, "post_comment", lambda n, body, config: self.comments.append(body) or {})
        monkeypatch.setattr(sdlc, "set_state_label", lambda n, cur, to, config, **k: self.labels.append(to))

    def post_json(self, path, payload):
        self.n += 1
        return {"number": self.n, "id": self.n}


class _FromGitHub:
    """plan-create reading a fixture bundle as GitHub (`fetch_bundle`), as a person's settlement must be read: every
    online read it makes answers from the fixture's `plan_issues`/`plan_links`/`plan_milestones` (`on_github`) plus
    what this run wrote through the `_Posts` recorder installed before it, so the read-back finds it; `gh` stays
    forbidden."""

    def __init__(self, monkeypatch, path):
        bundle = json.loads(Path(path).read_text())
        links = bundle.get("plan_links") or {}
        self.records = list(bundle.get("plan_issues") or [])
        self.attached, self.blocked = ({int(k): set(v) for k, v in (links.get(w) or {}).items()}
                                       for w in ("attached", "blocked"))
        self.milestones = dict(bundle.get("plan_milestones") or {})
        self.branches = []
        recorded = sdlc.post_json
        bundle = {k: v for k, v in bundle.items() if k not in ("plan_issues", "plan_links", "plan_milestones")}

        def post_json(path, payload):
            out = recorded(path, payload)
            parts = path.split("/")
            if parts[-1] == "issues":
                mk = sdlc.parse_marker(payload["body"]) or {}
                self.records.append({"key": mk["key"], "number": out["number"], "id": out["id"], "kind": mk["kind"]})
            elif parts[-1] == "sub_issues":
                self.attached.setdefault(int(parts[-2]), set()).add(payload["sub_issue_id"])   # id == number here
            elif parts[-1] == "blocked_by":
                self.blocked.setdefault(int(parts[-3]), set()).add(payload["issue_id"])
            elif parts[-1] == "milestones":
                self.milestones[payload["title"]] = out["number"]
            return out

        def gh_pages(path):
            assert path.split("?")[0].endswith("/milestones"), path
            return [{"title": t, "number": n} for t, n in self.milestones.items()]

        def gh_json(args):
            assert args[:2] == ["label", "list"], args
            return [{"name": l} for l in [sdlc.LEAF_LABEL, sdlc.CONTAINER_LABEL]
                    + [sdlc.COMPLEXITY_PREFIX + c for c in sdlc.COMPLEXITY]]

        monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, cfg, trusted=None: copy.deepcopy(bundle))
        monkeypatch.setattr(sdlc, "planning_ref", lambda n, config, offline: None)
        monkeypatch.setattr(sdlc, "post_json", post_json)
        monkeypatch.setattr(sdlc, "gh_pages", gh_pages)
        monkeypatch.setattr(sdlc, "gh_json", gh_json)
        monkeypatch.setattr(sdlc, "fetch_plan_issues", lambda epic, config, trusted: list(self.records))
        monkeypatch.setattr(sdlc, "fetch_links", lambda epic, records, config: (
            {k: set(v) for k, v in self.attached.items()}, {k: set(v) for k, v in self.blocked.items()}))
        monkeypatch.setattr(sdlc, "ensure_epic_branch",
                            lambda n, config, **k: self.branches.append(n) or ("0" * 40, True))


def test_plan_create_from_a_settled_escalation_resumes_building_and_names_who_settled(tmp_path, capsys, monkeypatch):
    _, comments = spent_amendment()
    settled = comments + [agent("escalation", None, 40, phase="plan"), comment(POSTER, "/approve", 41)]
    f = _bundle(tmp_path, settled, labels=("sdlc:escalated",))
    posts = _Posts(monkeypatch)
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run"]) == 0
    assert f"(settled by @{POSTER})" in capsys.readouterr().out and posts.labels == [] and posts.comments == []
    github = _FromGitHub(monkeypatch, f)
    assert sdlc.main(["plan-create", "12"]) == 0
    assert posts.labels == ["in-progress"] and github.branches == [12]
    record = posts.comments[-1]
    mk = sdlc.parse_marker(record)
    assert (mk["kind"], mk["rev"], mk["settled_by"]) == ("plan-created", "2", POSTER)
    assert f"after @{POSTER} settled it with" in record
    # read back: the epic is in-progress, building, and nothing is due from the plan any more
    after = settled + [comment(OWNER, record, 42)]
    assert derive(["sdlc:in-progress"], after)["action"] == "work_slices"


def test_plan_create_from_a_settled_question_moves_it_to_planned(tmp_path, capsys, monkeypatch):
    plan_c = comment(OWNER, sdlc.render_comment("plan", 1, sdlc.render_plan(make_plan()), CONFIG), 3)
    comments = (APPROVED + [plan_c] + [review(1, 4 + i, "changes") for i in range(CONFIG["max_plan_rounds"])]
                + [agent("question", None, 10, phase="plan"), comment(POSTER, "/approve", 11)])
    f = _bundle(tmp_path, comments, labels=("sdlc:needs-info",))
    posts = _Posts(monkeypatch)
    github = _FromGitHub(monkeypatch, f)
    assert sdlc.main(["plan-create", "12"]) == 0
    assert posts.labels == ["planned"] and sdlc.parse_marker(posts.comments[-1])["settled_by"] == POSTER
    assert github.branches == [12]
    # without the /approve plan-create refuses, and says what would settle it
    f = _bundle(tmp_path, comments[:-1], labels=("sdlc:needs-info",))
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run"]) == 1
    assert "a `/approve` after it" in capsys.readouterr().err


# --- plan-create never stops silently (#243 T14) -------------------------------------------

def on_github(plan, keys=None):
    """What a plan-create left on GitHub, as a fixture bundle carries it: the issues of `keys` (all: None) found by
    marker, their sub-issue and blocked_by links, and the plan's milestones."""
    items = [t for t in plan["subtasks"] if t.get("slices")] + sdlc.plan_leaves(plan)
    keys = [i["key"] for i in items] if keys is None else keys
    nums = {k: 100 + n for n, k in enumerate(keys)}
    attached, blocked = {}, {}
    for i in items:
        if i["key"] not in nums:
            continue
        parent = 12 if not i.get("_parent") else nums.get(i["_parent"])
        if parent:
            attached.setdefault(str(parent), []).append(nums[i["key"]])
        bs = [nums[b] for b in i.get("blocked_by") or [] if b in nums]
        if bs:
            blocked[str(nums[i["key"]])] = bs
    records = [{"key": k, "number": n, "id": n, "kind": "slice"} for k, n in nums.items()]
    return {"plan_issues": records, "plan_links": {"attached": attached, "blocked": blocked},
            "plan_milestones": {f"#12 {m['key']}: {m['title']}": 900 + n for n, m in enumerate(plan["milestones"])}}


class _Failing(_Posts):
    """_Posts whose `fail_at`-th GitHub write (post_json) or whose label move raises, as GitHub does when cut off."""

    def __init__(self, monkeypatch, fail_at=None, label_fails=False):
        super().__init__(monkeypatch)
        self.calls, self.fail_at = 0, fail_at
        if label_fails:
            def boom(n, cur, to, config, **k):
                raise sdlc.SdlcError("gh: HTTP 502")
            monkeypatch.setattr(sdlc, "set_state_label", boom)

    def post_json(self, path, payload):
        self.calls += 1
        if self.calls == self.fail_at:
            raise sdlc.SdlcError("gh: HTTP 502")
        return super().post_json(path, payload)


def test_a_plan_create_cut_short_says_what_it_made_and_the_rerun_finishes_it(tmp_path, capsys, monkeypatch):
    plan = make_plan()
    comments = APPROVED + [plan_comment(plan, 1, 3), review(1, 4, "approve")]
    posts = _Failing(monkeypatch, fail_at=4)   # the milestone, T1, T1 under the epic; then creating T1.1 fails
    assert sdlc.main(["plan-create", "12", "--from-file", _bundle(tmp_path, comments)]) == 1
    err = capsys.readouterr().err
    assert "plan-create stopped creating (gh: HTTP 502)" in err
    assert "made by this run: milestone M1, #502 (T1), #502 under #12" in err
    assert "still missing (8): create T1.1;" in err and "T2 blocked by T1.2" in err
    assert "rerun `uv run python tools/sdlc.py plan-create 12`" in err
    assert posts.comments == [] and posts.labels == []
    # state: issues exist and no record -> finish the creation, never a fresh /plan-issue
    made = {**on_github(plan, ["T1"]), "plan_milestones": {"#12 M1: read": 501}}
    bundle = {"issue": issue(["sdlc:approved"]), "comments": comments, "trusted": [OWNER], **made}
    st = sdlc.with_next(sdlc.bundle_state(bundle, CONFIG), CONFIG, offline=True, trusted={OWNER}, bundle=bundle)
    assert st["action"] == "create_plan_issues"
    assert st["next"].startswith("`uv run python tools/sdlc.py plan-create 12`: 1 issue(s) of the plan exist")
    # the rerun does only the rest, then posts the record and moves the label
    posts = _Posts(monkeypatch)
    calls = []
    monkeypatch.setattr(sdlc, "post_json", lambda path, payload: calls.append(path) or posts.post_json(path, payload))
    assert sdlc.main(["plan-create", "12", "--from-file", _bundle(tmp_path, comments, **made)]) == 0
    assert not any(p.endswith("/milestones") for p in calls) and len(calls) == 11 - 3
    assert posts.labels == ["planned"] and sdlc.parse_marker(posts.comments[0])["kind"] == "plan-created"


def test_state_finishes_a_creation_with_issues_and_no_record_and_only_then():
    comments = APPROVED + [plan_comment(make_plan(), 1, 3), review(1, 4, "approve")]
    made = on_github(make_plan())

    def nxt(labels, comments, **extra):
        bundle = {"issue": issue(labels), "comments": comments, "trusted": [OWNER], **extra}
        return sdlc.with_next(sdlc.bundle_state(bundle, CONFIG), CONFIG, offline=True, trusted={OWNER}, bundle=bundle)

    for labels in (["sdlc:approved"], ["sdlc:planned"]):   # planned: the label moved, the record never posted
        st = nxt(labels, comments, **made)
        assert (st["turn"], st["action"]) == ("agent", "create_plan_issues")
        assert "plan-create 12" in st["next"] and "no plan-created record" in st["next"]
    # no issues found: a fresh creation is /plan-issue's
    for labels in (["sdlc:approved"], ["sdlc:planned"]):
        assert nxt(labels, comments)["next"] == "/plan-issue 12"
    # a plan not agreed, or a record already posted, is never offered to finish
    assert nxt(["sdlc:approved"], comments[:-1], **made)["action"] == "continue_plan"
    st = nxt(["sdlc:planned"], comments + [agent("plan-created", 1, 5)], **made)
    assert st["action"] == "work_slices"
    # the record posted, the label never moved: plan-create only finishes the branch and the label
    st = nxt(["sdlc:approved"], comments + [agent("plan-created", 1, 5)])
    assert st["action"] == "create_plan_issues" and "is posted; plan-create only makes epic/12" in st["next"]


def test_plan_create_finishes_a_planned_epic_whose_record_was_never_posted(tmp_path, monkeypatch):
    comments = APPROVED + [plan_comment(make_plan(), 1, 3), review(1, 4, "approve")]
    posts = _Posts(monkeypatch)
    f = _bundle(tmp_path, comments, labels=("sdlc:planned",), **on_github(make_plan()))
    assert sdlc.main(["plan-create", "12", "--from-file", f]) == 0
    assert posts.n == 500 and posts.labels == [] and len(posts.comments) == 1   # only the record; it stays planned


def _settled_from(where):
    """A person-settled plan in needs-info (the first plan) or escalated (an amendment): (comments, plan, label to)."""
    if where == "needs-info":
        plan = make_plan()
        comments = (APPROVED + [plan_comment(plan, 1, 3)] + [review(1, 4 + i, "changes")
                                                             for i in range(CONFIG["max_plan_rounds"])]
                    + [agent("question", None, 10, phase="plan"), comment(POSTER, "/approve", 11)])
        return comments, plan, "planned"
    plan, comments = spent_amendment()
    return comments + [agent("escalation", None, 40, phase="plan"), comment(POSTER, "/approve", 41)], plan, "in-progress"


@pytest.mark.parametrize("where", ["needs-info", "escalated"])
def test_a_settled_creation_cut_short_before_its_label_moves_is_finished_once(where, tmp_path, capsys, monkeypatch):
    comments, plan, to = _settled_from(where)
    labels = (f"sdlc:{where}",)
    made = on_github(plan)
    # creation fails after plan-created is posted, before the label moves
    posts = _Failing(monkeypatch, label_fails=True)
    _FromGitHub(monkeypatch, _bundle(tmp_path, comments, labels=labels, **made))
    assert sdlc.main(["plan-create", "12"]) == 1
    err = capsys.readouterr().err
    assert "plan-created for plan rev" in err and "is posted" in err and "plan-create 12" in err
    assert len(posts.comments) == 1 and posts.n == 500
    record = posts.comments[0]
    assert sdlc.parse_marker(record)["settled_by"] == POSTER
    after = comments + [comment(OWNER, record, 50)]
    # state offers to finish it: the record stands in for the /approve
    bundle = {"issue": issue(labels), "comments": after, "trusted": [OWNER], **made}
    st = sdlc.with_next(sdlc.bundle_state(bundle, CONFIG), CONFIG, offline=True, trusted={OWNER}, bundle=bundle)
    assert (st["turn"], st["action"], st["plan_settled"]["by"]) == ("agent", "create_plan_issues", POSTER)
    assert st["next"].startswith("`uv run python tools/sdlc.py plan-create 12`") and "is posted" in st["next"]
    # the rerun: no issue, link or milestone, no second record, no new /approve; only the label
    posts = _Posts(monkeypatch)
    _FromGitHub(monkeypatch, _bundle(tmp_path, after, labels=labels, **made))
    assert sdlc.main(["plan-create", "12"]) == 0
    assert posts.n == 500 and posts.comments == [] and posts.labels == [to]


# --- a person's settlement is read from GitHub, never from a file (#243 F4) -----------------------------------

@pytest.mark.parametrize("where", ["needs-info", "escalated", "spent"])
def test_plan_create_from_a_file_refuses_a_creation_a_person_settled(where, tmp_path, capsys, monkeypatch):
    """A `--from-file` bundle could carry an invented `/approve`: a settled creation runs only as read from GitHub.
    The dry run still shows it; Codex's approval from a file is unchanged (the tests above)."""
    if where == "spent":   # the first plan, its rounds spent, then an /approve: no question, no escalation
        comments = (APPROVED + [plan_comment(make_plan(), 1, 3)]
                    + [review(1, 4 + i, "changes") for i in range(CONFIG["max_plan_rounds"])]
                    + [comment(POSTER, "/approve", 10)])
        labels = ("sdlc:approved",)
    else:
        comments, _, _ = _settled_from(where)
        labels = (f"sdlc:{where}",)
    f = _bundle(tmp_path, comments, labels=labels)
    st = sdlc.bundle_state(json.loads(Path(f).read_text()), CONFIG)
    assert (st["action"], st["plan_settled"]["by"]) == ("create_plan_issues", POSTER)
    posts = _Posts(monkeypatch)
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda *a, **k: pytest.fail("--from-file must not read GitHub"))
    assert sdlc.main(["plan-create", "12", "--from-file", f]) == 1
    err = capsys.readouterr().err
    assert "`plan-create` records a person's decision, which is read from GitHub, never from a file" in err
    assert (posts.n, posts.comments, posts.labels) == (500, [], [])
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"(settled by @{POSTER})" in out and "would create" in out
    assert (posts.n, posts.comments, posts.labels) == (500, [], [])


def test_plan_create_from_a_file_refuses_to_finish_a_settled_record(tmp_path, capsys, monkeypatch):
    """The plan-created record a settled run left behind (`settled_by=`, no /approve to cite) settles it too: a file
    can't finish it either, while the same bundle read from GitHub can (the T14 tests above)."""
    plan_c = comment(OWNER, sdlc.render_comment("plan", 1, sdlc.render_plan(make_plan()), CONFIG), 3)
    comments = (APPROVED + [plan_c] + [review(1, 4 + i, "changes") for i in range(CONFIG["max_plan_rounds"])]
                + [agent("plan-created", 1, 11, settled_by=POSTER)])
    f = _bundle(tmp_path, comments)
    st = sdlc.bundle_state(json.loads(Path(f).read_text()), CONFIG)
    assert st["action"] == "create_plan_issues"
    assert (st["plan_settled"]["by"], st["plan_settled"]["comment_id"]) == (POSTER, None)   # the record, not a reply
    posts = _Posts(monkeypatch)
    assert sdlc.main(["plan-create", "12", "--from-file", f]) == 1
    assert "read from GitHub, never from a file" in capsys.readouterr().err
    assert (posts.n, posts.comments, posts.labels) == (500, [], [])
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run"]) == 0
    assert f"(settled by @{POSTER})" in capsys.readouterr().out and posts.labels == []


def test_a_later_escalation_after_a_settled_record_stays_a_humans():
    comments, _, _ = _settled_from("escalated")
    record = agent("plan-created", 2, 50, settled_by=POSTER, created="M1,M2")
    for esc in (agent("escalation", None, 51, phase="sync"), agent("escalation", None, 51, phase="plan"),
                agent("escalation", None, 51)):
        assert derive(["sdlc:escalated"], comments + [record, esc])["action"] == "human"
    # a record for an older revision doesn't finish the latest
    old = comments[:-2] + [agent("escalation", None, 40, phase="plan"), comment(POSTER, "/approve", 41),
                           agent("plan-created", 1, 50, settled_by=POSTER, created="M1")]
    assert derive(["sdlc:escalated"], old)["action"] == "human"


def test_the_read_back_looks_again_before_it_gives_up(monkeypatch, capsys):
    slept, seen = [], iter([({}, [("create", "T1")]), ({}, [("create", "T1")]), ({"T1": {}}, [])])
    monkeypatch.setattr(sdlc.time, "sleep", slept.append)
    assert sdlc.plan_read_back(12, lambda: next(seen)) == ({"T1": {}}, [])
    assert slept == list(sdlc.READBACK_WAITS[:2])
    slept.clear()
    by_key, left = sdlc.plan_read_back(12, lambda: ({}, [("block", "T2", "T1")]))
    assert left and slept == list(sdlc.READBACK_WAITS) and sum(slept) < 60   # bounded, then it stops


def test_the_rerun_keeps_the_milestone_it_was_asked_for(tmp_path, capsys, monkeypatch):
    plan = two_milestones()
    _Failing(monkeypatch, fail_at=2)   # M2's milestone, then T3 fails
    made = on_github(plan, ["T1", "T1.1", "T1.2", "T2"])
    made["plan_milestones"] = {"#12 M1: read": 900}
    f = _bundle(tmp_path, created_m1(plan), **made)
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--milestone", "M2"]) == 1
    err = capsys.readouterr().err
    assert "made by this run: milestone M2\n" in err and "still missing (3): create T3; attach T3 to the epic; T3 blocked by T2" in err
    assert "rerun `uv run python tools/sdlc.py plan-create 12 --milestone M2`" in err


def test_a_read_back_that_fails_says_what_was_made_and_the_rerun(tmp_path, capsys, monkeypatch):
    """Online (every GitHub read stubbed): the writes go out, then the read-back itself fails."""
    comments = APPROVED + [plan_comment(make_plan(), 1, 3), review(1, 4, "approve")]
    bundle = {"issue": issue(["sdlc:approved"]), "comments": comments, "trusted": [OWNER]}
    posts = _Posts(monkeypatch)
    reads = iter([[]])

    def fetch(epic, config, trusted):
        try:
            return next(reads)
        except StopIteration:
            raise sdlc.SdlcError("gh: HTTP 502") from None

    monkeypatch.setattr(sdlc, "load_bundle", lambda args, config: bundle)
    monkeypatch.setattr(sdlc, "planning_ref", lambda *a, **k: "origin/main")
    monkeypatch.setattr(sdlc, "find_prd", lambda *a, **k: None)
    monkeypatch.setattr(sdlc, "gh_json", lambda *a, **k: [{"name": l} for l in
                                                         [sdlc.LEAF_LABEL, sdlc.CONTAINER_LABEL]
                                                         + [sdlc.COMPLEXITY_PREFIX + c for c in sdlc.COMPLEXITY]])
    monkeypatch.setattr(sdlc, "gh_pages", lambda *a, **k: [])
    monkeypatch.setattr(sdlc, "fetch_plan_issues", fetch)
    monkeypatch.setattr(sdlc, "fetch_links", lambda *a, **k: ({}, {}))
    assert sdlc.main(["plan-create", "12"]) == 1
    err = capsys.readouterr().err
    assert "plan-create stopped at its read-back (gh: HTTP 502)" in err
    assert "made by this run: milestone M1, #502 (T1)" in err
    assert "rerun `uv run python tools/sdlc.py plan-create 12` in a minute" in err
    assert posts.comments == [] and posts.labels == []
