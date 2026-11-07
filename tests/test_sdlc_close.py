"""tools/sdlc.py: `close`, an ordinary issue fixed by a commit on main or a duplicate (#378 T1.1). No network:
the debt tests' fake `gh` answers from dicts."""
import json

import pytest

from tests.test_sdlc_debt import CONFIG, REPO, FakeGitHub, run
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


def close(capsys, n, *argv, why="fixed by the retry change"):
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
    (["--reason", "completed"], "--commit SHA goes with --reason completed"),
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

def record(gh, n, **extra):
    gh.comment(n, sdlc.render_comment(sdlc.CLOSED_KIND, None, "Closed.", CONFIG, **extra))


def test_a_run_cut_short_after_the_record_is_finished_without_a_second_record(gh, capsys):
    n = triaged(gh)
    record(gh, n, reason="completed", commit=SHA)
    code, _, err = close(capsys, n, "--reason", "completed", "--commit", SHA[:8], why="again")
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
    n = gh.issue("reopened", ["sdlc:done"])
    gh.issues[n]["state_reason"] = "reopened"
    record(gh, n, reason="completed", commit=SHA)
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
