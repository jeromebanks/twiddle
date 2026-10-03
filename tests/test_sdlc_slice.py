"""tools/sdlc.py building: slice state, the merge gate, review/test records and `next`. No network."""
import json

import pytest

from tools import sdlc

CONFIG = sdlc.load_config()
OWNER, STRANGER = "owner", "stranger"
TRUSTED = {OWNER}
HEAD, OLD = "a" * 40, "b" * 40
_ids = iter(range(1, 10_000))


def comment(author, body, ts):
    return {"id": next(_ids), "author": author, "body": body, "created_at": f"2026-10-03T00:{ts:02d}:00Z",
            "url": f"https://example/c{ts}"}


def agent(kind, ts, body="text", **extra):
    return comment(OWNER, sdlc.marker(kind, None, **extra) + "\n" + body, ts)


def slice_body(epic=12, key="T1.1"):
    leaf = {"key": key, "title": "t", "covers": [1], "outcome": "o", "scope": "s", "acceptance": ["a"],
            "validation": "v", "demo": "d", "non_goals": "n", "context": "c", "_parent": "T1"}
    return sdlc.render_leaf_body(epic, leaf, 24, {}, None)


def slice_issue(state="open", reason=None, labels=("plan:slice",), body=None):
    return {"number": 28, "title": "#12 T1.1: model", "state": state, "state_reason": reason,
            "labels": list(labels), "body": body if body is not None else slice_body(), "assignees": []}


def pr(**kw):
    return {"number": 50, "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefName": "slice/28",
            "headRefOid": HEAD, "mergeable": "MERGEABLE", "body": "Closes #28\n\nEpic #12 · T1.1", **kw}


DONE = {"number": 27, "state": "closed", "state_reason": "completed"}
OPEN_BLOCKER = {"number": 27, "state": "open", "state_reason": None}


def ran_on(sha, result="pass", ts=5):
    return agent("tests", ts, sha=sha, result=result, passed="1300")


def review_on(sha, verdict, ts, rnd=1):
    return agent("pr-review", ts, sha=sha, verdict=verdict, round=str(rnd))


# --- slice state -----------------------------------------------------------

def derive(issue=None, comments=(), blockers=(), the_pr=None, pr_comments=()):
    return sdlc.derive_slice(issue or slice_issue(), list(comments), TRUSTED, list(blockers), the_pr, list(pr_comments))


def test_slice_states_in_order():
    assert derive(blockers=[OPEN_BLOCKER])["state"] == "blocked"
    st = derive(blockers=[DONE])
    assert (st["state"], st["next"], st["epic"], st["key"]) == ("ready", "/work-slice 28", 12, "T1.1")
    claimed = [agent("claim", 1, branch="slice/28")]
    assert derive(comments=claimed)["state"] == "claimed"
    st = derive(comments=claimed, the_pr=pr(), pr_comments=[ran_on(HEAD), review_on(HEAD, "approve", 6)])
    assert st["state"] == "in-review" and st["tests_on_head"]["result"] == "pass"
    assert st["review_on_head"]["verdict"] == "approve" and st["next"] == "/work-slice 28"
    merged = derive(issue=slice_issue(state="closed", reason="completed"))
    assert merged["state"] == "merged" and "state 12" in merged["next"]
    assert derive(issue=slice_issue(labels=("plan:slice", "sdlc:escalated")))["state"] == "escalated"


def test_release_ends_a_claim_and_strangers_cannot_claim():
    assert sdlc.claim_status([agent("claim", 1, branch="slice/28"), agent("release", 2)], TRUSTED) is None
    spoof = comment(STRANGER, sdlc.marker("claim", None, branch="x") + "\nmine", 1)
    assert sdlc.claim_status([spoof], TRUSTED) is None


def test_records_of_an_older_head_do_not_count():
    st = derive(the_pr=pr(), pr_comments=[ran_on(OLD), review_on(OLD, "approve", 6)])
    assert st["tests_on_head"] is None and st["review_on_head"] is None and st["pr_rounds"] == 1


# --- slice-check -----------------------------------------------------------

def test_slice_check():
    assert sdlc.slice_check_errors(slice_issue(), [DONE], None) == []
    assert any("blocked by #27" in e for e in sdlc.slice_check_errors(slice_issue(), [OPEN_BLOCKER], None))
    claim = {"url": "u", "branch": "slice/28"}
    assert any("--resume" in e for e in sdlc.slice_check_errors(slice_issue(), [], claim))
    assert sdlc.slice_check_errors(slice_issue(), [], claim, resume=True) == []
    assert any("no claim" in e for e in sdlc.slice_check_errors(slice_issue(), [], None, resume=True))
    assert any("closed" in e for e in sdlc.slice_check_errors(slice_issue(state="closed"), [], None))
    assert sdlc.slice_check_errors(slice_issue(labels=()), [], None) == ["#28 is not a plan:slice issue made by plan-create"]
    thin = slice_issue(body=sdlc.marker("slice", None, epic="12", key="T1.1") + "\n## Outcome\nx")
    assert any("lacks sections" in e and "Validation" in e for e in sdlc.slice_check_errors(thin, [], None))


# --- merge gate ------------------------------------------------------------

def gate(the_pr=None, issue=None, tests=None, reviews=None):
    the_pr = the_pr or pr()
    t = tests if tests is not None else [{"sha": HEAD, "result": "pass"}]
    r = reviews if reviews is not None else [{"sha": HEAD, "verdict": "approve"}]
    return sdlc.merge_gate_errors(the_pr, issue or slice_issue(), t, r, "main")


def test_merge_gate_passes_with_approved_and_tested_head():
    assert gate() == []


@pytest.mark.parametrize("kw,needle", [
    ({"reviews": []}, "no Codex review"),
    ({"reviews": [{"sha": OLD, "verdict": "approve"}]}, "no Codex review of the current head"),
    ({"reviews": [{"sha": HEAD, "verdict": "approve"}, {"sha": HEAD, "verdict": "changes"}]}, "asks for changes"),
    ({"tests": []}, "no recorded test run"),
    ({"tests": [{"sha": OLD, "result": "pass"}]}, "no recorded test run"),
    ({"tests": [{"sha": HEAD, "result": "fail"}]}, "tests failed"),
    ({"the_pr": pr(body="Closes #28 and closes #29")}, "exactly one slice"),
    ({"the_pr": pr(body="no reference")}, "exactly one slice"),
    ({"issue": slice_issue(labels=())}, "not an open plan:slice"),
    ({"the_pr": pr(mergeable="CONFLICTING")}, "not mergeable"),
    ({"the_pr": pr(baseRefName="dev")}, "targets dev"),
    ({"the_pr": pr(isDraft=True)}, "draft"),
])
def test_merge_gate_refusals(kw, needle):
    errs = gate(**kw)
    assert any(needle in e for e in errs), errs


def test_closes_refs():
    assert sdlc.closes_refs("Closes #28\nfixes #28, Resolves: #30\nsee #31") == [28, 30]


# --- pr-review -------------------------------------------------------------

def _review_bundle(tmp_path, comments=()):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"pr": pr(), "pr_comments": list(comments), "trusted": [OWNER]}))
    return str(f)


@pytest.mark.parametrize("report,ok", [
    (f"HEAD: {HEAD}\n1. fine\n\nVERDICT: approve\n", True),
    (f"**HEAD:** `{HEAD}`\n\nVERDICT: changes", True),
    ("1. fine\n\nVERDICT: approve", False),                 # can't show what it reviewed
    (f"HEAD: {OLD}\n\nVERDICT: approve", False),            # reviewed another commit
    (f"HEAD: {HEAD}\nlooks fine", False),                   # no verdict
])
def test_pr_review_binds_to_the_reviewed_head(tmp_path, capsys, monkeypatch, report, ok):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    r = tmp_path / "r.md"
    r.write_text(report)
    rc = sdlc.main(["pr-review", "50", "--report", str(r), "--from-file", _review_bundle(tmp_path), "--dry-run"])
    assert (rc == 0) == ok
    if ok:
        assert f"kind=pr-review sha={HEAD}" in capsys.readouterr().out


def test_pr_review_budget(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("must not call gh"))
    r = tmp_path / "r.md"
    r.write_text(f"HEAD: {HEAD}\nVERDICT: changes")
    spent = [review_on(OLD, "changes", i, i) for i in range(1, CONFIG["max_pr_rounds"] + 1)]
    assert sdlc.main(["pr-review", "50", "--report", str(r), "--from-file", _review_bundle(tmp_path, spent),
                      "--dry-run"]) == 1
    assert "escalate-slice" in capsys.readouterr().err


# --- progress and next ----------------------------------------------------

def leaf(n, key, state="open", reason=None, assignees=(), labels=("plan:slice",), ms="#12 M1: read"):
    return {"number": n, "key": key, "state": state, "state_reason": reason, "assignees": list(assignees),
            "labels": list(labels), "milestone": ms}


def test_progress_and_epic_next():
    leaves = [leaf(28, "T1.1", "closed", "completed"), leaf(29, "T1.2", assignees=["owner"]),
              leaf(30, "T2"), leaf(31, "T3.1", ms="#12 M2: write")]
    blockers = {29: [{"number": 28, "state": "closed", "state_reason": "completed"}],
                30: [{"number": 29, "state": "open"}], 31: []}
    p = sdlc.summarise_progress(leaves, blockers)
    assert (p["ready"], p["in_flight"], p["waiting"], p["open"]) == ([31], [29], [30], 3)
    assert {m["title"]: (m["done"], m["total"]) for m in p["milestones"]} == {"#12 M1: read": (1, 3), "#12 M2: write": (0, 1)}
    epic = {"number": 12, "action": "work_slices", "state": "in-progress", "conflicts": []}
    assert sdlc.next_command(epic, p) == "/work-slice 31"
    assert sdlc.next_command(epic, {**p, "ready": []}).startswith("nothing new to start: in progress #29")
    done = {"ready": [], "in_flight": [], "escalated": [], "open": 0, "waiting": [], "milestones": []}
    assert sdlc.next_command(epic, done).startswith("/milestone-demo 12")


@pytest.mark.parametrize("action,want", [
    ("triage", "/triage-issue 7"), ("record_approval", "/triage-issue 7"), ("plan", "/plan-issue 7"),
    ("create_plan_issues", "/plan-issue 7"), ("replan", "/plan-issue 7"),
    ("wait_for_poster", "nothing: waiting on the poster"), ("human", "nothing for an agent"),
])
def test_next_command_per_action(action, want):
    st = {"number": 7, "action": action, "state": "x", "conflicts": []}
    assert sdlc.next_command(st).startswith(want)


def test_plan_issues_are_never_triaged():
    issue = {"number": 28, "title": "t", "state": "open", "author": OWNER, "labels": ["plan:slice"]}
    st = sdlc.derive_state(issue, [], TRUSTED, CONFIG)
    assert (st["turn"], st["plan_kind"]) == ("none", "slice")
    assert sdlc.check_transition(st, "triage", "note", CONFIG)
    assert "slice-status 28" in sdlc.next_command(st)


def test_pytest_summary():
    assert sdlc.parse_pytest_summary("....\n1258 passed in 77.78s (0:01:17)\n") == (1258, 0)
    assert sdlc.parse_pytest_summary("FAILED x\n2 failed, 10 passed, 1 error in 3s") == (10, 3)
