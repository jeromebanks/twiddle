"""tools/sdlc.py: `close`, an ordinary issue fixed by a commit on main or a duplicate (#378 T1.1), or by an epic that
shipped, and what `state` says of a closed issue (T1.2). No network: the debt tests' fake `gh` answers from dicts."""
import json

import pytest

from tests.test_sdlc_debt import CONFIG, OWNER, REPO, FakeGitHub, run
from tools import sdlc

SHA = "b" * 40


class FakeTimeline(FakeGitHub):
    """The debt tests' fake, with each issue's timeline: its comments and `reopened` events in the order they
    happened, as GitHub's timeline API lists them. `same_second` gives every entry one timestamp."""

    def __init__(self):
        super().__init__()
        self.timeline, self.same_second = {}, False

    def now(self):
        return "2026-11-06T12:00:00Z" if self.same_second else super().now()

    def comment(self, n, body, author="owner"):
        super().comment(n, body, author)
        c = self.comments[n][-1]
        self.timeline.setdefault(n, []).append({"event": "commented", "id": c["id"], "created_at": c["created_at"]})

    def reopen(self, n):
        self.timeline.setdefault(n, []).append({"event": "reopened", "created_at": self.now()})
        self.issues[n]["state"], self.issues[n]["state_reason"] = "open", "reopened"

    def api(self, rest):
        path = rest[0]
        if path.startswith(f"repos/{REPO}/issues/") and path.split("?")[0].endswith("/timeline"):
            return json.dumps([self.timeline.get(int(path.split("/")[4]), [])])
        return super().api(rest)


@pytest.fixture
def gh(monkeypatch):
    fake = FakeTimeline()
    monkeypatch.setattr(sdlc, "gh", fake)
    monkeypatch.setattr(sdlc.time, "sleep", lambda s: None)
    fake.on_main[SHA] = "behind"
    return fake


def records(gh, n):
    return [mk for c in gh.comments[n] if (mk := sdlc.parse_marker(c["body"]) or {}).get("kind") == sdlc.CLOSED_KIND]


def sdlc_labels(gh, n):
    return [l for l in gh.issues[n]["labels"] if l in sdlc.state_labels(CONFIG)]


WHY = "fixed by the retry change"


def close(capsys, n, *argv, why=WHY):
    return run(capsys, "close", n, *argv, *(["--why", why] if why is not None else []))


def triaged(gh, state="sdlc:triage"):
    return gh.issue("a flaky test", [state, "bug"])


# --- completed by a commit on main (AC1) ---------------------------------------------------------------------

def test_completed_by_a_commit_on_main_records_closes_and_labels(gh, capsys):
    n = triaged(gh)
    code, out, err = close(capsys, n, "--reason", "completed", "--commit", SHA[:9])
    assert code == 0, err
    (rec,) = records(gh, n)
    assert rec == {"kind": "closed", "reason": "completed", "commit": SHA}   # the full SHA, in the marker
    assert SHA in gh.comments[n][-1]["body"] and "fixed by the retry change" in gh.comments[n][-1]["body"]
    assert (gh.issues[n]["state"], gh.issues[n]["state_reason"]) == ("closed", "completed")
    assert sdlc_labels(gh, n) == ["sdlc:done"] and "bug" in gh.issues[n]["labels"]
    # the record is written first, then the close, then the label
    kinds = [w[0] if w[0] in ("close", "edit") else "comment" for w in gh.writes]
    assert kinds == ["comment", "close", "edit"]
    assert ("close", ["issue", "close", str(n), "--repo", REPO, "--reason", "completed"]) in gh.writes
    assert f"#{n}: closed as completed" in out


@pytest.mark.parametrize("status", ["ahead", "diverged", None])
def test_a_commit_not_on_main_is_refused(gh, capsys, status):
    n = triaged(gh)
    sha = "c" * 40
    if status:
        gh.on_main[sha] = status
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", sha)
    assert code == 1 and ("is not on origin/main" in err if status else "isn't known to GitHub" in err)
    assert "`--reason completed` names the commit" in err and "`fixed`" not in err
    assert gh.writes == [] and gh.issues[n]["state"] == "open"


def test_an_untriaged_issue_with_no_label_gets_sdlc_done(gh, capsys):
    n = gh.issue("no label yet")
    assert close(capsys, n, "--reason", "completed", "--commit", SHA)[0] == 0
    assert sdlc_labels(gh, n) == ["sdlc:done"]


# --- N must be an issue ----------------------------------------------------------------------------------------

def test_a_pull_request_is_refused_before_any_write(gh, capsys):
    n = triaged(gh)
    gh.issues[n]["pr"] = True
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "is a pull request" in err
    assert gh.writes == [] and gh.comments[n] == []


def test_a_missing_issue_is_refused(gh, capsys):
    code, _, err = close(capsys, 999, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "#999 isn't an issue here" in err and gh.writes == []


# --- flags, checked before anything is read (AC3, AC5) ---------------------------------------------------------

@pytest.mark.parametrize("argv,named", [
    (["--reason", "completed"], "--reason completed takes one of --commit SHA"),
    (["--reason", "completed", "--commit", SHA, "--by-epic", "243"], "--reason completed takes one of"),
    (["--reason", "duplicate", "--of", "5", "--by-epic", "243"], "--by-epic E[:Mk] goes with --reason completed"),
    (["--reason", "not-planned", "--by-epic", "243:M1"], "--by-epic E[:Mk] goes with --reason completed"),
    (["--reason", "completed", "--by-epic", "7"], "can't be closed by itself"),
    (["--reason", "completed", "--by-epic", "abc"], "isn't E or E:Mk"),
    (["--reason", "completed", "--by-epic", "243:"], "isn't E or E:Mk"),
    (["--reason", "completed", "--by-epic", "243:M 1"], "isn't E or E:Mk"),
    (["--reason", "completed", "--by-epic", "243:M1/x"], "isn't E or E:Mk"),
    (["--reason", "duplicate", "--by-epic", "x"], "--of M goes with --reason duplicate"),
    (["--reason", "duplicate", "--of", "5", "--commit", SHA], "--commit SHA goes with --reason completed"),
    (["--reason", "not-planned", "--commit", SHA], "--commit SHA goes with --reason completed"),
    (["--reason", "completed", "--commit", "main"], "isn't a commit SHA"),
    (["--reason", "duplicate"], "--of M goes with --reason duplicate"),
    (["--reason", "completed", "--commit", SHA, "--of", "5"], "--of M goes with --reason duplicate"),
    (["--reason", "not-planned", "--of", "5"], "--of M goes with --reason duplicate"),
    (["--reason", "duplicate", "--of", "7"], "can't be a duplicate of itself"),
    (["--reason", "not-planned"], "lands with M2"),
])
def test_bad_flags_are_refused_before_any_gh_call(monkeypatch, capsys, argv, named):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("refused before GitHub is read"))
    code, _, err = close(capsys, 7, *argv)
    assert code == 1 and named in err, err


@pytest.mark.parametrize("why,named", [
    (None, "--why says why"),
    ("  ", "--why says why"),
    ("seen at 192.168.1.20", "an IP address"),
    ("the Roam at 34:7e:5c:aa:bb:cc", "the repo is public"),
    ("RINCON_347E5C0123456789 dropped", "the repo is public"),
    ("token " + "f" * 32, "the repo is public"),
])
def test_a_missing_or_private_why_is_refused(monkeypatch, capsys, why, named):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("refused before GitHub is read"))
    code, _, err = close(capsys, 7, "--reason", "completed", "--commit", SHA, why=why)
    assert code == 1 and named in err, err


def test_transition_can_never_post_a_close_record(capsys):
    assert sdlc.CLOSED_KIND not in sdlc.KINDS
    with pytest.raises(SystemExit):
        sdlc.main(["transition", "7", "done", "--kind", "closed"])
    assert "invalid choice: 'closed'" in capsys.readouterr().err


# --- duplicate (AC4) -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("other_state,reason", [("open", None), ("closed", "completed"), ("closed", "duplicate")])
def test_duplicate_of_an_open_or_completed_issue_closes_as_duplicate(gh, capsys, other_state, reason):
    n = triaged(gh)
    m = gh.issue("the original", state=other_state)
    gh.issues[m]["state_reason"] = reason
    code, out, err = close(capsys, n, "--reason", "duplicate", "--of", m, why="same as the original")
    assert code == 0, err
    assert records(gh, n) == [{"kind": "closed", "reason": "duplicate", "of": str(m)}]
    assert f"duplicate** of #{m}" in gh.comments[n][-1]["body"]
    assert (gh.issues[n]["state"], gh.issues[n]["state_reason"]) == ("closed", "duplicate")
    assert ("close", ["issue", "close", str(n), "--repo", REPO, "--duplicate-of", str(m)]) in gh.writes
    assert sdlc_labels(gh, n) == ["sdlc:done"]


def test_duplicate_of_a_pr_a_missing_issue_or_a_not_planned_one_is_refused(gh, capsys):
    n = triaged(gh)
    pr = gh.issue("a PR")
    gh.issues[pr]["pr"] = True
    dropped = gh.issue("dropped", state="closed")
    gh.issues[dropped]["state_reason"] = "not_planned"
    for of, named in ((pr, "is a pull request"), (999, "#999 isn't an issue here"),
                      (dropped, "needs the poster's approval")):
        code, _, err = close(capsys, n, "--reason", "duplicate", "--of", of)
        assert code == 1 and named in err, err
    assert gh.writes == [] and gh.issues[n]["state"] == "open"


# --- which issues it may close (AC11) --------------------------------------------------------------------------

def refused_cases(gh):
    slice_ = gh.issue("a slice", ["plan:slice"])
    subtask = gh.issue("a subtask", ["plan:subtask"])
    debt = gh.ticket()
    debt_epic = gh.debt_epic()
    escalated = gh.issue("stuck", ["sdlc:escalated"])
    planned = gh.feature_epic(label="sdlc:planned")
    building = gh.feature_epic(label="sdlc:in-progress")
    demo = gh.feature_epic(label="sdlc:demo-review")
    quick = gh.quick_change()
    return [(slice_, "`merge`"), (subtask, "`merge`"), (debt, "`debt-intake`"), (debt_epic, "`debt-epic`"),
            (escalated, "a human's"), (planned, "`ship`"), (building, "`ship`"), (demo, "`ship`"),
            (quick, "`work-slice`")]


def test_issues_another_verb_owns_are_refused_naming_it(gh, capsys):
    for n, named in refused_cases(gh):
        code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
        assert code == 1 and named in err, (n, err)
    assert gh.writes == []


def test_an_approved_issue_with_a_plan_created_is_refused(gh, capsys):
    n = gh.feature_epic(label="sdlc:approved")
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "plan was created" in err and gh.writes == []


def accepted_cases(gh):
    untriaged = gh.issue("new")
    triage = triaged(gh)
    needs_info = gh.issue("asked", ["sdlc:needs-info"])
    gh.say(needs_info, "question")
    prd = gh.issue("a prd", ["sdlc:prd-review"])
    gh.say(prd, "prd", 1)
    diag = gh.issue("a bug", ["sdlc:diagnosis-review"])
    gh.say(diag, "diagnosis", 1)
    approved = gh.feature_epic(created=(), label="sdlc:approved")
    return [untriaged, triage, needs_info, prd, diag, approved]


def test_issues_in_triage_or_approved_with_no_plan_are_accepted(gh, capsys):
    for n in accepted_cases(gh):
        code, out, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
        assert code == 0, (n, err)
        assert gh.issues[n]["state"] == "closed" and sdlc_labels(gh, n) == ["sdlc:done"], n


# --- resume (AC12) ---------------------------------------------------------------------------------------------

def record(gh, n, why=WHY, **extra):
    """A close record as `close` posts it (the run then cut short)."""
    note = sdlc.close_note(extra["reason"], why, extra.get("commit"), extra.get("of"))
    gh.comment(n, sdlc.render_comment(sdlc.CLOSED_KIND, None, note, CONFIG, **extra))


def test_a_run_cut_short_after_the_record_is_finished_without_a_second_record(gh, capsys):
    n = triaged(gh)
    record(gh, n, reason="completed", commit=SHA)
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA[:8], why=f"  {WHY} ")
    assert code == 0, err
    assert len(records(gh, n)) == 1 and not any(w[0].endswith("/comments") for w in gh.writes)
    assert gh.issues[n]["state"] == "closed" and sdlc_labels(gh, n) == ["sdlc:done"]


def test_a_run_cut_short_after_the_close_only_labels(gh, capsys):
    n = triaged(gh)
    record(gh, n, reason="completed", commit=SHA)
    gh.issues[n]["state"], gh.issues[n]["state_reason"] = "closed", "completed"
    code, out, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 0, err
    assert [w[0] for w in gh.writes] == ["edit"] and sdlc_labels(gh, n) == ["sdlc:done"]


def test_an_issue_already_closed_and_labelled_as_recorded_writes_nothing(gh, capsys):
    n = gh.issue("done", ["sdlc:done"], state="closed")
    gh.issues[n]["state_reason"] = "completed"
    record(gh, n, reason="completed", commit=SHA)
    code, out, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 0, err
    assert "nothing to do" in out and gh.writes == []


def test_different_arguments_from_the_record_are_refused(gh, capsys):
    n = triaged(gh)
    m = gh.issue("the original")
    record(gh, n, reason="completed", commit=SHA)
    other = "d" * 40
    gh.on_main[other] = "behind"
    for argv in (["--reason", "completed", "--commit", other], ["--reason", "duplicate", "--of", m]):
        code, _, err = close(capsys, n, *argv)
        assert code == 1 and "a different close is refused" in err, err
    assert gh.writes == []


def test_a_different_why_from_the_record_is_refused(gh, capsys):
    n = triaged(gh)
    record(gh, n, reason="completed", commit=SHA)
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA, why="a different explanation")
    assert code == 1 and "a different --why is refused" in err and repr(WHY) in err
    assert gh.writes == [] and gh.issues[n]["state"] == "open"


def test_a_close_that_did_not_take_effect_is_finished_by_a_rerun(gh, capsys, monkeypatch):
    n = triaged(gh)
    real = gh.__call__

    def dropped(args, *, check=True):   # GitHub answers the close but it doesn't take; the label edit lands
        return "" if list(args[:2]) == ["issue", "close"] else real(args, check=check)
    monkeypatch.setattr(sdlc, "gh", dropped)
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "read-back" in err and "not closed as completed" in err
    assert gh.issues[n]["state"] == "open" and sdlc_labels(gh, n) == ["sdlc:done"]
    monkeypatch.setattr(sdlc, "gh", gh)
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 0, err
    assert len(records(gh, n)) == 1 and gh.issues[n]["state"] == "closed" and sdlc_labels(gh, n) == ["sdlc:done"]


@pytest.mark.parametrize("label,owner", [("plan:slice", "`merge`"), ("plan:subtask", "`merge`"),
                                         ("tech-debt", "`debt-intake`"), ("lane:debt", "`debt-epic`")])
def test_a_resume_past_its_label_still_refuses_an_owned_issue(gh, capsys, label, owner):
    n = gh.issue("cut short after the label", ["sdlc:done"])   # open: the close didn't take effect
    record(gh, n, reason="completed", commit=SHA)
    gh.issues[n]["labels"].append(label)   # it became another verb's before the rerun
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and owner in err, err
    assert gh.writes == [] and gh.issues[n]["state"] == "open"


def test_a_resume_of_a_closed_unlabelled_issue_still_refuses_an_owned_one(gh, capsys):
    n = triaged(gh)
    record(gh, n, reason="completed", commit=SHA)
    gh.issues[n]["state"], gh.issues[n]["state_reason"] = "closed", "completed"
    gh.issues[n]["labels"].append("plan:slice")
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "`merge`" in err and gh.writes == []


def test_a_closed_issue_with_no_record_is_refused(gh, capsys):
    n = gh.issue("closed by hand", ["sdlc:triage"], state="closed")
    gh.issues[n]["state_reason"] = "completed"
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "with no close record" in err and gh.writes == []


def test_a_closed_issue_whose_reason_differs_from_its_record_is_refused(gh, capsys):
    n = gh.issue("closed otherwise", ["sdlc:triage"], state="closed")
    gh.issues[n]["state_reason"] = "not_planned"
    record(gh, n, reason="completed", commit=SHA)
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "not completed as its record says" in err and gh.writes == []


def test_an_issue_reopened_after_its_close_is_a_humans(gh, capsys):
    n = gh.issue("reopened", ["sdlc:done"], state="closed")
    record(gh, n, reason="completed", commit=SHA)
    gh.reopen(n)
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "reopened after its close record" in err and gh.writes == []


def test_an_issue_reopened_and_relabelled_after_its_close_is_a_humans(gh, capsys):
    n = triaged(gh)
    assert close(capsys, n, "--reason", "completed", "--commit", SHA)[0] == 0
    gh.reopen(n)
    gh.issues[n]["labels"] = ["sdlc:triage", "bug"]   # a person put it back in triage
    before = len(gh.writes)
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "reopened after its close record" in err and len(gh.writes) == before
    assert gh.issues[n]["state"] == "open" and len(records(gh, n)) == 1


def test_an_issue_reopened_and_closed_again_by_a_person_is_a_humans(gh, capsys):
    n = triaged(gh)
    record(gh, n, reason="completed", commit=SHA)
    gh.issues[n]["state"], gh.issues[n]["state_reason"] = "closed", "completed"   # our close; the label edit failed
    gh.reopen(n)
    gh.issues[n]["state"], gh.issues[n]["state_reason"] = "closed", "completed"   # a person closed it again
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "reopened after its close record" in err and gh.writes == []
    assert sdlc_labels(gh, n) == ["sdlc:triage"]


@pytest.mark.parametrize("same_second", [False, True])
def test_an_issue_reopened_before_its_record_still_resumes(gh, capsys, same_second):
    gh.same_second = same_second   # timestamps can't order it: the timeline's order does
    n = triaged(gh)   # closed by hand and reopened long ago: GitHub keeps state_reason `reopened` while it is open
    gh.issues[n]["state"] = "closed"
    gh.reopen(n)
    record(gh, n, reason="completed", commit=SHA)
    assert close(capsys, n, "--reason", "completed", "--commit", SHA)[0] == 0
    assert len(records(gh, n)) == 1 and gh.issues[n]["state"] == "closed" and sdlc_labels(gh, n) == ["sdlc:done"]


def test_a_reopen_in_the_same_second_as_the_record_is_still_refused(gh, capsys):
    gh.same_second = True
    n = triaged(gh)
    record(gh, n, reason="completed", commit=SHA)
    gh.reopen(n)   # after the record, in the timeline's order, with the same timestamp
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "reopened after its close record" in err and gh.writes == []


def test_a_record_missing_from_the_timeline_is_refused(gh, capsys):
    n = triaged(gh)
    record(gh, n, reason="completed", commit=SHA)
    gh.timeline[n] = []
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "isn't in its timeline" in err and gh.writes == []


def test_a_record_by_a_stranger_does_not_count(gh, capsys):
    n = triaged(gh)
    gh.comment(n, sdlc.render_comment(sdlc.CLOSED_KIND, None, "x", CONFIG, reason="completed", commit="e" * 40),
               author="stranger")
    assert close(capsys, n, "--reason", "completed", "--commit", SHA)[0] == 0
    assert len(records(gh, n)) == 2 and gh.issues[n]["state"] == "closed"


# --- dry run (AC13) --------------------------------------------------------------------------------------------

def test_dry_run_checks_everything_and_prints_each_write(gh, capsys):
    n = triaged(gh)
    code, out, err = close(capsys, n, "--reason", "completed", "--commit", SHA[:7], "--dry-run")
    assert code == 0, err
    assert out.splitlines() == [f"would post the close record on #{n} (completed, by {SHA[:12]})",
                                f"would close #{n} as completed",
                                f"would label #{n} sdlc:done (remove sdlc:triage)"]
    assert gh.writes == [] and gh.comments[n] == [] and gh.issues[n]["state"] == "open"


def test_dry_run_of_a_duplicate_and_of_a_resume(gh, capsys):
    n, m = triaged(gh), gh.issue("the original")
    code, out, _ = close(capsys, n, "--reason", "duplicate", "--of", m, "--dry-run")
    assert code == 0 and f"would close #{n} as a duplicate of #{m}" in out
    record(gh, n, reason="duplicate", of=str(m))
    code, out, _ = close(capsys, n, "--reason", "duplicate", "--of", m, "--dry-run")
    assert code == 0 and "post the close record" not in out and f"would close #{n}" in out
    assert gh.writes == []


def test_dry_run_still_refuses(gh, capsys):
    n = gh.issue("a slice", ["plan:slice"])
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA, "--dry-run")
    assert code == 1 and "`merge`" in err and gh.writes == []


def test_a_bad_read_back_says_to_rerun(gh, capsys, monkeypatch):
    n = triaged(gh)
    monkeypatch.setattr(sdlc, "set_state_label", lambda *a, **k: None)   # the label edit is lost
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "read-back" in err and f"rerun the same `close {n}`" in err
    assert len(records(gh, n)) == 1 and gh.issues[n]["state"] == "closed"



def test_a_failed_close_after_the_record_says_to_rerun(gh, capsys, monkeypatch):
    n = triaged(gh)
    real = gh.__call__

    def failing(args, *, check=True):
        if list(args[:2]) == ["issue", "close"]:
            raise sdlc.SdlcError("gh issue close failed: HTTP 502")
        return real(args, check=check)
    monkeypatch.setattr(sdlc, "gh", failing)
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "HTTP 502" in err and f"rerun the same `close {n}`" in err
    assert len(records(gh, n)) == 1 and gh.issues[n]["state"] == "open"
    monkeypatch.setattr(sdlc, "gh", gh)   # the rerun finishes it, with no second record
    assert close(capsys, n, "--reason", "completed", "--commit", SHA)[0] == 0
    assert len(records(gh, n)) == 1 and gh.issues[n]["state"] == "closed" and sdlc_labels(gh, n) == ["sdlc:done"]


# --- completed by an epic that shipped (T1.2, AC2) --------------------------------------------------------------

SHIP = {"M1": "1" * 40, "M2": "2" * 40, sdlc.NO_MILESTONE: "3" * 40}
LEAF = {"key": "T1", "title": "t", "covers": [1], "outcome": "o", "scope": "s", "acceptance": ["a"],
        "validation": "v", "demo": "d", "non_goals": "n", "context": "c"}


def epic(gh, milestones=("M1", "M2"), shipped=(), label="sdlc:in-progress", state="open", created=True):
    """A feature epic as `state E` reads it: a PRD, its approval, a plan whose JSON names `milestones` (none: a plan
    with no named milestones), its plan-created record (`created=False`: none yet) and a ship record per `shipped`."""
    n = gh.issue("a feature", [label, "enhancement"], state=state)
    gh.say(n, "prd", 1)
    gh.say(n, "approval", 1, by=OWNER, via="comment")
    plan = {"issue": n, "summary": "s", "subtasks": [{**LEAF, "key": f"T{i}", "milestone": m}
                                                     for i, m in enumerate(milestones or [None], 1) if m] or [LEAF]}
    if milestones:
        plan["milestones"] = [{"key": m, "title": m, "demo": "d"} for m in milestones]
    gh.comment(n, sdlc.render_comment("plan", 1, sdlc.render_plan(plan), CONFIG))
    gh.say(n, "plan-review", 1, verdict="approve", round="1")
    if created:
        gh.say(n, "plan-created", 1, **({"created": ",".join(milestones)} if milestones else {}))
    for m in shipped:
        gh.say(n, "shipped", **({} if m == sdlc.NO_MILESTONE else {"milestone": m}), sha=SHIP[m])
    return n


class FakeEpics(FakeTimeline):
    """The fake, with GitHub's close reason in `gh issue view` (as `stateReason`) and a missing issue an error."""

    def __call__(self, args, *, check=True):
        args = list(args)
        if args[:2] == ["issue", "view"]:
            if int(args[2]) not in self.issues:
                raise sdlc.SdlcError(f"gh issue view {args[2]} failed: Could not resolve to an issue")
            out = json.loads(super().__call__(args, check=check))
            return json.dumps({**out, "stateReason": (self.issues[int(args[2])]["state_reason"] or "").upper()})
        return super().__call__(args, check=check)


@pytest.fixture
def ghe(monkeypatch):
    fake = FakeEpics()
    monkeypatch.setattr(sdlc, "gh", fake)
    monkeypatch.setattr(sdlc.time, "sleep", lambda s: None)
    fake.on_main[SHA] = "behind"
    return fake


def test_by_an_epic_whose_last_milestone_shipped_records_closes_and_labels(ghe, capsys):
    e = epic(ghe, shipped=("M1", "M2"), label="sdlc:done", state="closed")   # done and closed: the normal case
    n = triaged(ghe)
    code, out, err = close(capsys, n, "--reason", "completed", "--by-epic", e)
    assert code == 0, err
    (rec,) = records(ghe, n)
    assert rec == {"kind": "closed", "reason": "completed", "epic": str(e)}   # the epic named, not its last milestone
    body = ghe.comments[n][-1]["body"]
    assert f"#{e} (its last milestone, M2)" in body and SHIP["M2"] in body and WHY in body
    assert (ghe.issues[n]["state"], ghe.issues[n]["state_reason"]) == ("closed", "completed")
    assert sdlc_labels(ghe, n) == ["sdlc:done"]
    assert ("close", ["issue", "close", str(n), "--repo", REPO, "--reason", "completed"]) in ghe.writes
    assert f"#{n}: closed as completed, fixed by #{e} (its last milestone, M2)" in out


def test_by_an_epic_whose_last_milestone_has_not_shipped_is_refused(ghe, capsys):
    e = epic(ghe, shipped=("M1",))
    n = triaged(ghe)
    code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", e)
    assert code == 1 and f"#{e}'s M2 (the last milestone of its latest plan) hasn't shipped to main" in err
    assert "shipped so far: M1" in err
    assert ghe.writes == [] and ghe.issues[n]["state"] == "open"


def test_by_a_named_milestone_that_shipped_closes_naming_it(ghe, capsys):
    e = epic(ghe, shipped=("M1",))
    n = triaged(ghe)
    code, out, err = close(capsys, n, "--reason", "completed", "--by-epic", f"{e}:M1")
    assert code == 0, err
    (rec,) = records(ghe, n)
    assert rec == {"kind": "closed", "reason": "completed", "epic": str(e), "milestone": "M1"}
    assert f"#{e}'s M1, shipped to `main` in {SHIP['M1']}" in ghe.comments[n][-1]["body"]
    assert f"fixed by #{e}'s M1" in out


def test_by_a_named_milestone_not_shipped_or_not_in_the_plan_is_refused(ghe, capsys):
    e = epic(ghe, shipped=("M1",))
    n = triaged(ghe)
    code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", f"{e}:M2")
    assert code == 1 and f"#{e}'s M2 hasn't shipped to main" in err and "last milestone" not in err
    code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", f"{e}:M3")
    assert code == 1 and f"#{e}'s latest plan has no milestone M3 (its milestones: M1, M2)" in err
    assert ghe.writes == []


def test_e_all_is_the_whole_epic_whatever_its_plan(ghe, capsys):
    """`waits=E:all` is how #379 records a whole-epic wait, even for an epic with named milestones: its lift passes
    `all`, and it means what `--by-epic E` does, with the same record."""
    e = epic(ghe, shipped=("M1",))
    n = triaged(ghe)
    code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", f"{e}:all")
    assert code == 1 and f"#{e}'s M2 (the last milestone of its latest plan) hasn't shipped" in err
    ghe.say(e, "shipped", milestone="M2", sha=SHIP["M2"])
    assert close(capsys, n, "--reason", "completed", "--by-epic", f"{e}:all")[0] == 0
    assert records(ghe, n) == [{"kind": "closed", "reason": "completed", "epic": str(e)}]


def test_an_epic_planned_without_milestones(ghe, capsys):
    e = epic(ghe, milestones=())
    n = triaged(ghe)
    code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", e)
    assert code == 1 and f"#{e} hasn't shipped to main" in err
    code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", f"{e}:M1")
    assert code == 1 and "has no milestone M1 (it has no named milestones" in err
    ghe.say(e, "shipped", sha=SHIP[sdlc.NO_MILESTONE])   # its one release, `all`
    code, out, err = close(capsys, n, "--reason", "completed", "--by-epic", e)
    assert code == 0, err
    assert records(ghe, n) == [{"kind": "closed", "reason": "completed", "epic": str(e)}]
    assert f"fixed by #{e}, shipped to `main` in {SHIP[sdlc.NO_MILESTONE]}" in ghe.comments[n][-1]["body"]


def test_by_epic_refuses_what_is_not_an_epic_with_a_created_plan(ghe, capsys):
    cases = [(ghe.issue("a slice", ["plan:slice"]), "is a plan:slice issue, not an epic"),
             (ghe.issue("a subtask", ["plan:subtask"]), "is a plan:subtask issue, not an epic"),
             (ghe.ticket(), "is a tech-debt ticket, not an epic"),
             (ghe.debt_epic(), "is the debt epic"),
             (ghe.quick_change(), "is a quick change, not an epic"),
             (epic(ghe, shipped=("M1", "M2"), created=False, label="sdlc:approved"), "isn't an epic with a created plan"),
             (ghe.issue("an ordinary issue", ["sdlc:triage"]), "isn't an epic with a created plan"),
             (999, "#999 couldn't be read")]
    n = triaged(ghe)
    for e, named in cases:
        code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", e)
        assert code == 1 and named in err, (e, err)
    assert ghe.writes == [] and ghe.issues[n]["state"] == "open"


def test_a_rerun_by_a_different_epic_or_milestone_is_refused(ghe, capsys):
    e = epic(ghe, shipped=("M1", "M2"))
    other = epic(ghe, shipped=("M1", "M2"))
    n = triaged(ghe)
    record(ghe, n, reason="completed", epic=str(e))   # cut short after the record
    for by in (f"{e}:M1", other, f"{other}:M2"):
        code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", by)
        assert code == 1 and f"already has a close record (reason=completed epic={e})" in err, (by, err)
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA)
    assert code == 1 and "already has a close record" in err
    assert ghe.writes == []
    code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", e)   # the same: finished, one record
    assert code == 0, err
    assert len(records(ghe, n)) == 1 and ghe.issues[n]["state"] == "closed" and sdlc_labels(ghe, n) == ["sdlc:done"]


def test_a_rerun_by_a_milestone_after_a_whole_epic_record_is_refused_and_back(ghe, capsys):
    e = epic(ghe, shipped=("M1", "M2"))
    n = triaged(ghe)
    record(ghe, n, reason="completed", epic=str(e), milestone="M1")
    code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", e)
    assert code == 1 and "milestone=M1" in err and ghe.writes == []


def test_dry_run_by_an_epic_prints_the_writes(ghe, capsys):
    e = epic(ghe, shipped=("M1",))
    n = triaged(ghe)
    code, out, err = close(capsys, n, "--reason", "completed", "--by-epic", f"{e}:M1", "--dry-run")
    assert code == 0, err
    assert out.splitlines() == [f"would post the close record on #{n} (completed, by #{e}'s M1)",
                                f"would close #{n} as completed", f"would label #{n} sdlc:done (remove sdlc:triage)"]
    assert ghe.writes == []
    code, _, err = close(capsys, n, "--reason", "completed", "--by-epic", e, "--dry-run")
    assert code == 1 and "hasn't shipped" in err and ghe.writes == []


def test_close_issue_and_epic_shipped_are_callable_without_argparse(ghe, capsys):
    """What #379's `then=close` lift calls: plain arguments, lines back, an SdlcError on a refusal."""
    e = epic(ghe, shipped=("M1",))
    n = triaged(ghe)
    trusted = sdlc.fetch_trusted(CONFIG)
    assert sdlc.epic_shipped(e, "M1", CONFIG, trusted) == {"epic": e, "milestone": "M1", "shipped": "M1",
                                                           "sha": SHIP["M1"]}
    with pytest.raises(sdlc.SdlcError, match="M2 .the last milestone"):
        sdlc.epic_shipped(e, None, CONFIG, trusted)
    with pytest.raises(sdlc.SdlcError, match="hasn't shipped"):
        sdlc.close_issue(n, "completed", WHY, CONFIG, epic=e, milestone=sdlc.NO_MILESTONE, trusted=trusted)
    assert sdlc.close_issue(n, "completed", WHY, CONFIG, epic=e, milestone="M1", dry_run=True)[0].startswith("would post")
    assert ghe.writes == []
    assert sdlc.close_issue(n, "completed", WHY, CONFIG, epic=e, milestone="M1", trusted=trusted) == [
        f"#{n}: closed as completed, fixed by #{e}'s M1; labelled sdlc:done"]
    assert sdlc.close_issue(n, "completed", WHY, CONFIG, epic=e, milestone="M1") == [
        f"#{n}: already closed as completed and labelled sdlc:done, as its record says: nothing to do"]
    with pytest.raises(sdlc.SdlcError, match="goes with an epic"):
        sdlc.close_issue(n, "completed", WHY, CONFIG, commit=SHA, milestone="M1")


def test_epic_shipped_in_reads_a_state_alone(monkeypatch):
    """The shipped check of a state already read (a ship's own bundle, say), with no GitHub at all."""
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("no GitHub"))
    st = {"state": "done", "plan_created": True, "planned_milestones": ["M1", "M2"],
          "shipped": {"M1": SHIP["M1"], "M2": SHIP["M2"]}, "issue_closed": True}
    assert sdlc.epic_shipped_in(st, 5)["shipped"] == "M2"
    assert sdlc.epic_shipped_in(st, 5, "M1")["sha"] == SHIP["M1"]
    with pytest.raises(sdlc.SdlcError, match="no milestone M9"):
        sdlc.epic_shipped_in(st, 5, "M9")


# --- what `state` says of a closed issue (T1.2, AC14) -----------------------------------------------------------

def state_out(capsys, n):
    code, out, err = run(capsys, "state", n)
    assert code == 0, err
    return out


@pytest.mark.parametrize("argv,end", [
    (["--reason", "completed", "--commit", SHA[:9]], f"closed: completed by {SHA[:12]}"),
    (["--reason", "completed", "--by-epic", "{e}"], "closed: completed by epic #{e}"),
    (["--reason", "completed", "--by-epic", "{e}:M1"], "closed: completed by epic #{e}'s M1"),
    (["--reason", "duplicate", "--of", "{d}"], "closed: duplicate of #{d}"),
])
def test_state_of_an_issue_close_closed_says_how_it_ended(ghe, capsys, argv, end):
    e, d = epic(ghe, shipped=("M1", "M2")), ghe.issue("the same bug")
    n = triaged(ghe)
    assert close(capsys, n, *[a.format(e=e, d=d) for a in argv])[0] == 0
    out = state_out(capsys, n)
    (line,) = [l.strip() for l in out.splitlines() if l.strip().startswith("closed:")]
    assert line.startswith(end.format(e=e, d=d) + " (u")   # and the record's link
    assert f"next: nothing: #{n} is done" in out


def test_state_of_an_issue_closed_as_not_planned_by_its_record(ghe, capsys):
    """M2 writes this record; its reading is here, from a fixture record."""
    n = triaged(ghe)
    ghe.say(n, sdlc.CLOSED_KIND, reason="not-planned")
    ghe.issues[n]["state"], ghe.issues[n]["state_reason"] = "closed", "not_planned"
    assert "  closed: not planned (u" in state_out(capsys, n)


def test_state_of_an_issue_closed_by_hand_is_as_before(ghe, capsys):
    n = triaged(ghe)
    ghe.issues[n]["state"], ghe.issues[n]["state_reason"] = "closed", "completed"
    out = state_out(capsys, n)
    assert "closed:" not in out and "next: nothing: the issue is closed" in out
    assert sdlc.bundle_state(sdlc.fetch_bundle(n, CONFIG), CONFIG)["closed_by"] is None


def test_state_of_an_open_issue_with_a_record_prints_no_end(ghe, capsys):
    n = triaged(ghe)
    record(ghe, n, reason="completed", commit=SHA)   # cut short: still open
    assert "closed:" not in state_out(capsys, n)


def test_state_of_an_issue_closed_again_by_hand_says_the_record_no_longer_matches(ghe, capsys):
    n = triaged(ghe)
    assert close(capsys, n, "--reason", "completed", "--commit", SHA)[0] == 0
    ghe.reopen(n)
    ghe.issues[n]["state"], ghe.issues[n]["state_reason"] = "closed", "not_planned"
    assert f"closed: not_planned on GitHub, closed by hand after its close record (completed by {SHA[:12]})" \
        in state_out(capsys, n)


def test_a_close_record_by_a_stranger_is_no_end(ghe, capsys):
    n = triaged(ghe)
    ghe.comment(n, sdlc.render_comment(sdlc.CLOSED_KIND, None, "x", CONFIG, reason="duplicate", of="5"), "stranger")
    ghe.issues[n]["state"], ghe.issues[n]["state_reason"] = "closed", "duplicate"
    assert "closed:" not in state_out(capsys, n)
