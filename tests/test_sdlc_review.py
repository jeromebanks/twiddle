"""tools/sdlc.py Codex reviews that move to the milestone: routine slices (`slice_review`), and slices whose
head Codex couldn't review (`review-defer`). Either way the milestone's review comes before its demo. No network."""
import json

import pytest

from tools import sdlc

CONFIG = sdlc.load_config()
OWNER, POSTER = "owner", "poster"
TRUSTED = {OWNER}
HEAD, OLD = "a" * 40, "b" * 40
_ids = iter(range(1, 10_000))


def comment(author, body, ts):
    return {"id": next(_ids), "author": author, "body": body, "created_at": f"2026-10-06T00:{ts:02d}:00Z",
            "url": f"https://example/c{ts}"}


def agent(kind, rev, ts, body="text", **extra):
    return comment(OWNER, sdlc.marker(kind, rev, **extra) + "\n" + body, ts)


HISTORY = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER), agent("plan", 1, 3),
           agent("plan-review", 1, 4, verdict="approve", round="1"), agent("plan-created", 1, 5)]
OWED = agent("review-owed", None, 20, milestone="M1", slice="T1.2", number="29", why="routine", sha=HEAD)
APPROVE = agent("ship-review", None, 21, milestone="M1", sha=HEAD, verdict="approve", round="1")
CHANGES = agent("ship-review", None, 21, milestone="M1", sha=HEAD, verdict="changes", round="1")


def issue(labels=("sdlc:in-progress",)):
    return {"number": 12, "title": "Alarm manager", "state": "open", "author": POSTER, "labels": list(labels)}


def derive(comments=()):
    return sdlc.derive_state(issue(), HISTORY + list(comments), TRUSTED, CONFIG)


def ms(key, done, total):
    return {"title": f"#12 {key}: x", "key": key, "done": done, "total": total}


def progress(*milestones, ready=()):
    return {"ready": list(ready), "in_flight": [], "escalated": [], "waiting": [], "open": 1,
            "milestones": list(milestones)}


def slice_issue(labels=("plan:slice",)):
    return {"number": 28, "title": "#12 T1.1: t", "state": "open", "state_reason": None, "labels": list(labels),
            "body": sdlc.marker("slice", None, epic="12", key="T1.1") + "\n", "assignees": [], "milestone": "#12 M1: x"}


def pr(**kw):
    return {"number": 50, "state": "OPEN", "isDraft": False, "baseRefName": "epic/12", "headRefName": "slice/28",
            "headRefOid": HEAD, "mergeable": "MERGEABLE", "body": "Closes #28", **kw}


ROUTINE = ("plan:slice", "complexity:routine")
JUDGMENT = ("plan:slice", "complexity:judgment")


# --- which slices are reviewed where --------------------------------------------------------

def test_review_mode_is_per_complexity_and_defaults_to_the_slice():
    assert sdlc.review_mode("routine", CONFIG) == "milestone"
    assert sdlc.review_mode("judgment", CONFIG) == "slice" and sdlc.review_mode("novel", CONFIG) == "slice"
    assert sdlc.review_mode(None, CONFIG) == "slice"                     # unrated (#12's slices): as before
    assert sdlc.review_mode("routine", {}) == "slice"                    # no `slice_review`: every slice reviewed
    assert sdlc.review_mode("routine", {"slice_review": {"routine": "nonsense"}}) == "slice"
    assert CONFIG["max_pr_rounds"] == 5


def gate(labels=JUDGMENT, reviews=(), deferrals=(), the_pr=None):
    the_pr = the_pr or pr()
    mode = sdlc.review_mode(sdlc.complexity_of(list(labels)), CONFIG)
    return sdlc.merge_gate_errors(the_pr, slice_issue(labels), [{"sha": HEAD, "result": "pass"}], list(reviews),
                                  "epic/12", 0, list(deferrals), mode)


def test_the_merge_gate_and_the_review():
    assert any("no Codex review" in e and "review-defer 50" in e for e in gate())
    assert gate(reviews=[{"sha": HEAD, "verdict": "approve"}]) == []
    assert gate(labels=ROUTINE) == []                                    # reviewed with its milestone
    assert gate(deferrals=[{"sha": HEAD}]) == []                         # Codex couldn't run on this head
    assert any("no Codex review" in e for e in gate(deferrals=[{"sha": OLD}]))   # a rebase needs a new deferral
    # a review that asked for changes always blocks, routine or deferred
    changes = [{"sha": HEAD, "verdict": "changes"}]
    assert any("asks for changes" in e for e in gate(labels=ROUTINE, reviews=changes))
    assert any("asks for changes" in e for e in gate(reviews=changes, deferrals=[{"sha": HEAD}]))
    # only the review relaxes: the tests, the base and the rest still hold
    assert any("targets main" in e for e in gate(labels=ROUTINE, the_pr=pr(baseRefName="main")))


def test_owed_why():
    approve = [{"sha": HEAD, "verdict": "approve"}]
    assert sdlc.owed_why(pr(), approve, [], "milestone") is None         # reviewed anyway: nothing owed
    assert sdlc.owed_why(pr(), [], [{"sha": HEAD}], "slice") == "unavailable"
    assert sdlc.owed_why(pr(), [], [], "milestone") == "routine"
    assert sdlc.owed_why(pr(), [], [], "slice") is None


# --- review-defer -----------------------------------------------------------------------------

def _defer(tmp_path, comments=(), labels=JUDGMENT, reason="codex: command not found"):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"pr": pr(), "pr_comments": list(comments), "trusted": [OWNER],
                             "slice": slice_issue(labels)}))
    return sdlc.main(["review-defer", "50", "--reason", reason, "--from-file", str(f), "--dry-run"])


def review(sha, verdict, ts):
    return agent("pr-review", None, ts, sha=sha, verdict=verdict, round=str(ts))


def test_review_defer(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    assert _defer(tmp_path) == 0
    out = capsys.readouterr().out
    assert f"kind=review-deferred sha={HEAD}" in out and "command not found" in out
    # findings nobody checked can't be deferred, even on a later head
    assert _defer(tmp_path, [review(OLD, "changes", 1)]) == 1
    assert "asked for changes" in capsys.readouterr().err
    assert _defer(tmp_path, [review(OLD, "changes", 1), review(OLD, "approve", 2)]) == 0   # answered, then rebased
    capsys.readouterr()
    assert _defer(tmp_path, [review(HEAD, "approve", 1)]) == 1
    assert "already approved" in capsys.readouterr().err
    assert _defer(tmp_path, labels=ROUTINE) == 1
    assert "no deferral needed" in capsys.readouterr().err
    assert _defer(tmp_path, reason="  ") == 1


def test_slice_status_shows_a_deferral():
    the_pr = pr()
    st = sdlc.derive_slice(slice_issue(), [], TRUSTED, [], the_pr,
                           [agent("tests", None, 1, sha=HEAD, result="pass", passed="9"),
                            agent("review-deferred", None, 2, sha=HEAD)])
    assert st["deferred_on_head"] and st["review_on_head"] is None


# --- merge records what it owes, before the squash ------------------------------------------

class GitHub:
    def __init__(self, monkeypatch):
        self.calls, self.posted = [], []
        monkeypatch.setattr(sdlc, "gh", self.gh)
        monkeypatch.setattr(sdlc, "post_comment", lambda n, body, cfg: self.posted.append((n, body)) or {})

    def gh(self, args, check=True):
        self.calls.append(args)
        if args[:2] == ["pr", "merge"]:
            self.calls.append(["posted so far", len(self.posted)])
        return ""


def stub_merge(monkeypatch, labels, pr_comments):
    reads = iter([slice_issue(labels), {**slice_issue(labels), "state": "closed", "state_reason": "completed"}])
    monkeypatch.setattr(sdlc, "fetch_pr", lambda n, cfg: (pr(), pr_comments))
    monkeypatch.setattr(sdlc, "fetch_trusted", lambda cfg: TRUSTED)
    monkeypatch.setattr(sdlc, "fetch_slice", lambda n, cfg: (next(reads), []))
    monkeypatch.setattr(sdlc, "gh_json", lambda args: {"behind_by": 0})
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, cfg, trusted=None: {"issue": issue(), "comments": HISTORY,
                                                                            "trusted": [OWNER]})
    monkeypatch.setattr(sdlc, "epic_progress", lambda n, cfg, trusted: progress(ms("M1", 1, 3)))
    monkeypatch.setattr(sdlc, "with_next", lambda st, cfg, offline=False, trusted=None: {**st, "next": "x", "progress": None})


TESTED = [agent("tests", None, 1, sha=HEAD, result="pass", passed="9")]


def test_merge_owes_a_routine_slices_review_to_its_milestone(monkeypatch, capsys):
    gh = GitHub(monkeypatch)
    stub_merge(monkeypatch, ROUTINE, TESTED)
    assert sdlc.main(["merge", "50"]) == 0
    (n, body), = gh.posted
    assert n == 12 and f"kind=review-owed milestone=M1 slice=T1.1 number=28 why=routine sha={HEAD}" in body
    assert ["posted so far", 1] in gh.calls                    # recorded before the squash, never after


def test_merge_owes_a_deferred_review(monkeypatch):
    gh = GitHub(monkeypatch)
    stub_merge(monkeypatch, JUDGMENT, TESTED + [agent("review-deferred", None, 2, sha=HEAD)])
    assert sdlc.main(["merge", "50"]) == 0
    assert "why=unavailable" in gh.posted[0][1]


def test_merge_owes_nothing_for_an_approved_head(monkeypatch):
    gh = GitHub(monkeypatch)
    stub_merge(monkeypatch, ROUTINE, TESTED + [agent("pr-review", None, 2, sha=HEAD, verdict="approve", round="1")])
    assert sdlc.main(["merge", "50"]) == 0 and gh.posted == []


# --- the milestone's review comes before its demo -------------------------------------------

def test_an_owed_review_holds_the_milestone_before_its_demo():
    p = progress(ms("M1", 3, 3), ms("M2", 0, 2), ready=(40,))
    st = derive([OWED])
    assert st["review_owed"] == {"M1": [{"slice": "T1.2", "number": "29", "why": "routine", "sha": HEAD}]}
    view = sdlc.epic_view(st, p)
    assert view["current"]["phase"] == "review" and view["due"] == "review"
    assert view["next"].startswith("/milestone-demo 12: #12 M1: x is complete, but Codex hasn't reviewed T1.2 (routine)")
    assert "Codex review" in sdlc.claim_pause_errors(st, p)[0]
    assert sdlc.due_milestone(st["demos"], p, st["shipped"], st["review_owed"])["key"] == "M1"
    # still building: owing a review changes nothing yet
    assert sdlc.epic_view(st, progress(ms("M1", 2, 3)))["current"]["phase"] == "building"
    # an approving milestone review clears it; one asking for changes doesn't
    assert sdlc.epic_view(derive([OWED, APPROVE]), p)["current"]["phase"] == "complete"
    assert sdlc.epic_view(derive([OWED, CHANGES]), p)["current"]["phase"] == "review"
    # a slice owed after the review (a fix slice) needs another
    later = agent("review-owed", None, 22, milestone="M1", slice="F1", number="44", why="unavailable", sha=OLD)
    assert sdlc.derive_state(issue(), HISTORY + [OWED, APPROVE, later], TRUSTED, CONFIG)["review_owed"]["M1"][0]["slice"] == "F1"


def _bundle(tmp_path, comments, prog):
    f = tmp_path / "bundle.json"
    f.write_text(json.dumps({"issue": issue(), "comments": HISTORY + comments, "trusted": [OWNER], "progress": prog,
                             "epic_head": HEAD}))
    return str(f)


def test_demo_post_waits_for_the_review(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "git", lambda *a, **k: pytest.fail("dry run must not touch git"))
    d = tmp_path / "demo"
    d.mkdir()
    (d / "README.md").write_text("# M1\n" + "every alarm, by room " * 20)
    body = tmp_path / "comment.md"
    body.write_text("You can see every alarm.\n\n[the write-up](README.md)\n")
    args = ["demo-post", "12", "--milestone", "M1", "--dir", str(d), "--body-file", str(body), "--dry-run", "--from-file"]
    assert sdlc.main(args + [_bundle(tmp_path, [OWED], progress(ms("M1", 3, 3)))]) == 1
    assert "comes before its demo" in capsys.readouterr().err
    assert sdlc.main(args + [_bundle(tmp_path, [OWED, APPROVE], progress(ms("M1", 3, 3)))]) == 0


def test_ship_review_runs_before_the_demo_when_a_review_is_owed(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    r = tmp_path / "r.md"
    r.write_text(f"HEAD: {HEAD}\n1. T1.2 is fine\n\nVERDICT: approve\n")
    run = ["ship-review", "12", "--report", str(r), "--dry-run", "--from-file"]
    assert sdlc.main(run + [_bundle(tmp_path, [OWED], progress(ms("M1", 3, 3)))]) == 0
    assert f"kind=ship-review milestone=M1 sha={HEAD} verdict=approve" in capsys.readouterr().out
    # nothing owed and not accepted: no milestone review yet (the demo comes first)
    assert sdlc.main(run + [_bundle(tmp_path, [], progress(ms("M1", 3, 3)))]) == 1
    assert "isn't accepted" in capsys.readouterr().err
    # the same review on the same head serves the ship too
    ready = derive([OWED, APPROVE, agent("demo", 1, 30, milestone="M1", sha=HEAD),
                    agent("demo-approval", 1, 31, milestone="M1", by=POSTER),
                    agent("epic-tests", None, 32, sha=HEAD, result="pass", passed="9")])
    assert sdlc.ship_gate_errors(ready, "M1", HEAD, True, []) == []
    owed = {**ready, "review_owed": {"M1": [{"slice": "T1.2"}]}}
    assert any("`ship-review` first" in e for e in sdlc.ship_gate_errors(owed, "M1", HEAD, True, []))
