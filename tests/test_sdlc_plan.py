"""tools/sdlc.py planning: plan state, validation, Codex verdicts, idempotent creation. No network."""
import copy
import json

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
    monkeypatch.setattr(sdlc, "check_plan", lambda plan: [])
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
    monkeypatch.setattr(sdlc, "check_plan", lambda plan: [])
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
    assert p["units"][30] == {"key": "T1", "complexity": "routine"}
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


DEMO_M1 = [agent("demo", 1, 20, milestone="M1", sha="d" * 40), agent("demo-approval", 1, 21, milestone="M1", by=POSTER)]


def created_m1(plan):
    return APPROVED + [plan_comment(plan, 1, 3), review(1, 4, "approve"), agent("plan-created", 1, 5, created="M1")]


def test_plan_create_makes_only_the_first_milestone_by_default(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "check_plan", lambda plan: [])
    f = _bundle(tmp_path, APPROVED + [plan_comment(two_milestones(), 1, 3), review(1, 4, "approve")])
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "#12: creating M1; later: M2" in out and "'#12 T2: do T2'" in out
    assert "M2" not in out.split("later: M2")[1] and "T3" not in out           # nothing of M2 yet
    assert sdlc.main(["plan-create", "12", "--from-file", f, "--dry-run", "--milestone", "M2"]) == 1
    assert "M2 comes after M1, which aren't created yet" in capsys.readouterr().err


def test_create_all_keeps_the_old_behaviour(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "check_plan", lambda plan: [])
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
    monkeypatch.setattr(sdlc, "check_plan", lambda plan: [])
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
