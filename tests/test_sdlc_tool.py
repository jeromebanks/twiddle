"""tools/sdlc.py: state derivation, sign-off rules and transitions. No network."""
import json
from pathlib import Path

import pytest

from tools import sdlc

CONFIG = sdlc.load_config()
OWNER, POSTER, STRANGER = "owner", "poster", "stranger"
TRUSTED = {OWNER}


def issue(labels=(), author=POSTER, state="open"):
    return {"number": 12, "title": "Alarm manager", "state": state, "author": author, "labels": list(labels)}


_ids = iter(range(1, 10_000))


def comment(author, body, ts):
    return {"id": next(_ids), "author": author, "body": body, "created_at": f"2026-10-02T00:{ts:02d}:00Z",
            "url": f"https://example/c{ts}"}


def agent(kind, rev, ts, body="text", **extra):
    return comment(OWNER, sdlc.marker(kind, rev, **extra) + "\n" + body, ts)


def derive(labels, comments, **kw):
    return sdlc.derive_state(issue(labels, **kw), comments, TRUSTED, CONFIG)


def test_untriaged_is_the_agents_move():
    st = derive([], [])
    assert (st["state"], st["turn"], st["action"]) == ("untriaged", "agent", "triage")


def test_marked_vs_unmarked_reply():
    waiting = derive(["sdlc:needs-info"], [agent("question", None, 1)])
    assert (waiting["turn"], waiting["action"]) == ("poster", "wait_for_poster")
    # the maintainer's own unmarked comment is a reply, because the login can't tell
    replied = derive(["sdlc:needs-info"], [agent("question", None, 1), comment(OWNER, "my answer", 2)])
    assert (replied["turn"], replied["action"]) == ("agent", "respond_to_reply")


def test_marker_from_untrusted_author_does_not_count():
    spoof = comment(STRANGER, sdlc.marker("question", None) + "\nhi", 1)
    st = derive(["sdlc:needs-info"], [spoof])
    assert st["rounds"] == 0 and len(st["replies"]) == 1


def test_approve_quoted_inside_agent_comment_does_not_approve():
    prd = agent("prd", 1, 1, "Reply\n/approve\nto sign off")
    st = derive(["sdlc:prd-review"], [prd])
    assert st["decision"] is None and st["turn"] == "poster"


@pytest.mark.parametrize("body", ["> /approve", "```\n/approve\n```", "    /approve", "I'd /approve this", "approve"])
def test_approve_not_at_line_start_or_in_quote_or_code(body):
    st = derive(["sdlc:prd-review"], [agent("prd", 1, 1), comment(POSTER, body, 2)])
    assert st["decision"] is None


def test_approve_from_poster_is_valid_for_latest_revision():
    st = derive(["sdlc:prd-review"], [agent("prd", 1, 1), comment(POSTER, "looks good\n/approve", 2)])
    assert st["action"] == "record_approval" and st["decision"]["by"] == POSTER and st["decision"]["valid"]


def test_approve_from_stranger_is_ignored():
    st = derive(["sdlc:prd-review"], [agent("prd", 1, 1), comment(STRANGER, "/approve", 2)])
    assert st["decision"] is None and st["ignored_keywords"] == [{"by": STRANGER, "action": "approve"}]
    assert st["action"] == "respond_to_reply"


def test_approve_before_a_newer_revision_is_void():
    comments = [agent("prd", 1, 1), comment(POSTER, "/approve", 2), agent("prd", 2, 3)]
    st = derive(["sdlc:prd-review"], comments)
    assert st["decision"] is None and st["latest_doc"]["rev"] == 2 and st["turn"] == "poster"


def test_recorded_approval_then_new_revision_reopens():
    comments = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER, via="comment")]
    assert derive(["sdlc:approved"], comments)["action"] == "plan"
    reopened = comments + [agent("prd", 2, 3)]
    st = derive(["sdlc:approved"], reopened)
    assert st["reconcile"] == "label_approval"  # label says approved, rev 2 has no record


def test_label_only_maintainer_approval_needs_reconcile():
    st = derive(["sdlc:approved"], [agent("prd", 1, 1)])
    assert (st["reconcile"], st["action"]) == ("label_approval", "reconcile_label")


def test_ui_label_approval_leaves_the_review_label_behind():
    st = derive(["sdlc:prd-review", "sdlc:approved"], [agent("prd", 1, 1)])
    assert st["conflicts"] == [] and st["state"] == "approved"
    assert (st["reconcile"], st["action"], st["stale_labels"]) == ("label_approval", "reconcile_label", ["sdlc:prd-review"])


def test_recorded_approval_with_a_stale_review_label_only_needs_cleanup():
    comments = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER, via="comment")]
    st = derive(["sdlc:prd-review", "sdlc:approved"], comments)
    assert st["reconcile"] == "stale_labels" and st["conflicts"] == []


def test_notes_keep_state_and_triage_needs_no_body(tmp_path, capsys, monkeypatch):
    st = derive(["sdlc:prd-review"], [agent("prd", 1, 1), comment(POSTER, "why X?", 2)])
    assert sdlc.check_transition(st, "prd-review", "note", CONFIG) == []
    assert sdlc.check_transition(st, "needs-info", "note", CONFIG)
    assert sdlc.check_transition(derive([], []), "triage", "note", CONFIG) == []
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue([]), "comments": [], "trusted": [OWNER]}))
    assert sdlc.main(["transition", "12", "triage", "--kind", "note", "--from-file", str(f), "--dry-run"]) == 0
    assert "Triaging this now" in capsys.readouterr().out


def test_label_approved_with_nothing_to_approve_is_a_conflict():
    assert derive(["sdlc:approved"], [])["conflicts"]


@pytest.mark.parametrize("labels", [["sdlc:triage", "sdlc:prd-review"], ["sdlc:done", "sdlc:needs-info"]])
def test_two_state_labels_conflict(labels):
    st = derive(labels, [])
    assert st["state"] == "conflict" and st["action"] == "fix_conflict" and st["turn"] == "human"


def test_label_mismatching_the_latest_doc_is_a_conflict():
    assert derive(["sdlc:diagnosis-review"], [agent("prd", 1, 1)])["conflicts"]


def test_escalated_and_closed_and_later_states():
    assert derive(["sdlc:escalated"], [])["turn"] == "human"
    assert derive([], [], state="closed")["turn"] == "none"
    assert derive(["sdlc:done"], [])["turn"] == "later"
    assert derive(["sdlc:demo-review"], [])["conflicts"]          # demo review with no demo posted
    planned = derive(["sdlc:planned"], [])
    assert (planned["turn"], planned["action"]) == ("agent", "work_slices")


def test_rounds_count_questions_and_revisions():
    st = derive(["sdlc:prd-review"], [agent("question", None, 1), agent("prd", 1, 2), agent("prd", 2, 3)])
    assert st["rounds"] == 3


# --- transitions -----------------------------------------------------------

def test_transition_rules():
    st = derive([], [])
    assert sdlc.check_transition(st, "prd-review", "prd", CONFIG) == []
    assert sdlc.check_transition(st, "approved", "approval", CONFIG)         # no sign-off
    assert sdlc.check_transition(st, "prd-review", "question", CONFIG)       # kind/target mismatch
    assert sdlc.check_transition(st, "bogus", "note", CONFIG)
    assert sdlc.check_transition(derive(["sdlc:needs-info"], []), "approved", "approval", CONFIG)
    assert sdlc.check_transition(st, "escalated", "escalation", CONFIG) == []


def test_approval_transition_needs_a_valid_decision():
    st = derive(["sdlc:prd-review"], [agent("prd", 1, 1), comment(POSTER, "/approve", 2)])
    assert sdlc.check_transition(st, "approved", "approval", CONFIG) == []


def test_round_budget_blocks_more_revisions():
    comments = [agent("question", None, i) for i in range(1, 6)]
    st = derive(["sdlc:needs-info"], comments)
    errs = sdlc.check_transition(st, "prd-review", "prd", CONFIG)
    assert any("budget" in e for e in errs)
    assert sdlc.check_transition(st, "escalated", "escalation", CONFIG) == []


def test_superseded_rendering_keeps_marker_and_body():
    old = sdlc.render_comment("prd", 1, "BODY", CONFIG)
    new = sdlc.render_superseded(old, 2, "https://x/c2")
    mk = sdlc.parse_marker(new)
    assert mk["kind"] == "prd" and mk["rev"] == "1" and mk["superseded"] == "2" and "BODY" in new


def test_rendered_prd_is_marked_and_its_footer_cannot_approve():
    text = sdlc.render_comment("prd", 1, "body", CONFIG)
    assert sdlc.parse_marker(text) == {"kind": "prd", "rev": "1"}
    st = derive(["sdlc:prd-review"], [comment(OWNER, text, 1)])
    assert st["decision"] is None


def test_transition_dry_run_from_bundle_posts_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    bundle = {"issue": issue([]), "comments": [], "trusted": [OWNER]}
    f, body = tmp_path / "b.json", tmp_path / "prd.md"
    f.write_text(json.dumps(bundle))
    body.write_text("## Problem\nx")
    rc = sdlc.main(["transition", "12", "prd-review", "--kind", "prd", "--body-file", str(body),
                    "--from-file", str(f), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "kind=prd rev=1" in out and "sdlc:prd-review" in out


def test_state_cli_from_file(tmp_path, capsys):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue([]), "comments": [], "trusted": [OWNER]}))
    assert sdlc.main(["state", "--from-file", str(f), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "triage"


def test_prd_pr_guard():
    ok = {"state": "OPEN", "baseRefName": "main", "mergeable": "MERGEABLE", "files": [{"path": "docs/prd/12-x.md"}]}
    assert sdlc.prd_pr_errors(ok, "main") == []
    assert sdlc.prd_pr_errors({**ok, "files": ok["files"] + [{"path": "src/twiddle/cli.py"}]}, "main")
    assert sdlc.prd_pr_errors({**ok, "files": []}, "main")
    assert sdlc.prd_pr_errors({**ok, "baseRefName": "dev"}, "main")
    assert sdlc.prd_pr_errors({**ok, "mergeable": "UNKNOWN"}, "main")
    assert sdlc.prd_pr_errors({**ok, "state": "MERGED"}, "main")


def test_state_shows_a_plan_settled_by_a_person(capsys):
    spent = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER, via="comment"), agent("plan", 1, 3)]
    spent += [agent("plan-review", 1, 4 + i, verdict="changes") for i in range(CONFIG["max_plan_rounds"])]
    asked = spent + [agent("question", None, 10, phase="plan"), comment(POSTER, "/approve", 11),
                     comment(STRANGER, "/approve", 12)]
    st = derive(["sdlc:needs-info"], asked)
    sdlc.print_state(st)
    out = capsys.readouterr().out
    assert f"settled: /approve by {POSTER} on plan rev 1" in out
    assert f"plan rounds: 0/{CONFIG['max_plan_rounds']} (this cycle spent {CONFIG['max_plan_rounds']})" in out
    assert f"ignored /approve from {STRANGER}" in out
    assert f"decision: /approve by {POSTER} (settles the plan)" in out


# --- a person's decision is read from GitHub, never from a file (#263 criterion 13) --------------------------

def _no_github(monkeypatch):
    for name in ("gh", "gh_json", "gh_pages", "post_comment", "patch_comment", "set_state_label"):
        monkeypatch.setattr(sdlc, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} called"))


def _saved(tmp_path, labels, comments):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue(labels), "comments": comments, "trusted": [OWNER]}))
    return str(f)


def test_an_approval_from_a_file_is_a_dry_run_or_nothing(tmp_path, capsys, monkeypatch):
    _no_github(monkeypatch)
    b = _saved(tmp_path, ["sdlc:prd-review"], [agent("prd", 1, 1), comment(POSTER, "/approve", 2)])
    assert sdlc.main(["transition", "12", "approved", "--kind", "approval", "--from-file", b]) == 1
    assert "--from-file goes with --dry-run only" in capsys.readouterr().err
    assert sdlc.main(["transition", "12", "approved", "--kind", "approval", "--from-file", b, "--dry-run"]) == 0
    assert "kind=approval rev=1 by=poster via=comment" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [["--by", OWNER], ["--from-file", "B"], ["--by", OWNER, "--from-file", "B"]])
def test_a_watched_run_cannot_name_the_approver(tmp_path, capsys, monkeypatch, flags):
    _no_github(monkeypatch)
    b = _saved(tmp_path, ["sdlc:approved"], [agent("prd", 1, 1)])
    flags = [b if f == "B" else f for f in flags]
    monkeypatch.setenv(sdlc.WATCH_RUN_ENV, "")   # set, even empty, is a watched run
    assert sdlc.main(["reconcile", "12", *flags]) == 1
    assert "go with --dry-run only" in capsys.readouterr().err
    if len(flags) == 4:   # offline, with an approver named: a dry run still shows what it would record
        assert sdlc.main(["reconcile", "12", *flags, "--dry-run"]) == 0
        assert "kind=approval rev=1 by=owner via=label" in capsys.readouterr().out
        monkeypatch.delenv(sdlc.WATCH_RUN_ENV)
        monkeypatch.setattr(sdlc, "post_comment", lambda n, body, cfg: {"html_url": "u"})
        assert sdlc.main(["reconcile", "12", *flags]) == 0   # a person, outside a watched run, keeps --by


class LabelledIssue:
    """A fake `gh` for one issue a collaborator moved to sdlc:approved in GitHub's UI: it answers the issue, its
    comments, the collaborators and the issue's events, and keeps what is posted."""

    def __init__(self, monkeypatch, actor):
        self.comments = [{"id": 1, "user": {"login": OWNER}, "body": sdlc.marker("prd", 1) + "\nPRD",
                          "created_at": "2026-10-02T00:01:00Z", "html_url": "u1"}]
        self.actor, self.writes = actor, []
        monkeypatch.setattr(sdlc, "gh", self.gh)

    def gh(self, args, check=True):
        if args[:2] == ["issue", "view"]:
            return json.dumps({"number": 12, "title": "Alarm manager", "state": "open", "author": {"login": POSTER},
                               "labels": [{"name": "sdlc:approved"}], "body": ""})
        if args[:2] == ["api", "-X"]:
            assert args[2:4] == ["POST", "repos/jeromebanks/twiddle/issues/12/comments"], args
            body = Path(args[-1].split("@", 1)[1]).read_text()
            self.writes.append(body)
            self.comments.append({"id": len(self.comments) + 1, "user": {"login": OWNER}, "body": body,
                                  "created_at": "2026-10-02T00:09:00Z", "html_url": "u2"})
            return json.dumps({"html_url": "u2"})
        path = args[1]
        if path.endswith("/comments"):
            return json.dumps([self.comments])
        if path.endswith("/collaborators"):
            return json.dumps([[{"login": OWNER, "permissions": {"push": True}}]])
        if path.endswith("/events"):
            return json.dumps([[{"event": "labeled", "label": {"name": "sdlc:approved"}, "actor": {"login": self.actor}}]])
        raise AssertionError(f"unexpected gh {args}")


@pytest.mark.parametrize("watched", [False, True])
def test_a_label_approval_is_backed_from_the_issue_s_events(monkeypatch, capsys, watched):
    if watched:   # a watched run backs a label with no --by: who added it comes from GitHub
        monkeypatch.setenv(sdlc.WATCH_RUN_ENV, "1")
    gh = LabelledIssue(monkeypatch, OWNER)
    assert sdlc.main(["state", "12", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "reconcile_label"
    assert sdlc.main(["reconcile", "12"]) == 0
    assert len(gh.writes) == 1 and "kind=approval rev=1 by=owner via=label" in gh.writes[0]
    capsys.readouterr()
    assert sdlc.main(["state", "12", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["reconcile"] is None and st["action"] != "reconcile_label" and st["approved_rev"] == 1


def test_a_label_added_by_someone_else_is_not_backed(monkeypatch, capsys):
    monkeypatch.setenv(sdlc.WATCH_RUN_ENV, "1")
    gh = LabelledIssue(monkeypatch, STRANGER)
    assert sdlc.main(["reconcile", "12"]) == 1
    err = capsys.readouterr().err
    assert "applied by stranger, who is not a collaborator" in err and "--by" not in err and gh.writes == []


def test_a_resume_only_epic_names_the_resume_as_a_runnable_command():
    """When the only agent move is resuming claimed work, `next:` leads with the command itself (#263 T2.1)."""
    epic = {"number": 12, "action": "work_slices", "state": "in-progress", "conflicts": []}
    m1 = {"title": "#12 M1: x", "key": "M1", "done": 0, "total": 2}
    units = {29: {"key": "T1.1", "milestone": "M1", "model": "opus", "complexity": "judgment"},
             30: {"key": "T1.2", "milestone": "M1"}}
    p = {"ready": [], "in_flight": [29, 30], "escalated": [], "open": 2, "waiting": [], "milestones": [m1],
         "units": units}
    nxt = sdlc.next_command(epic, p)
    assert nxt == "/work-slice 29 (opus: judgment): resumes it; nothing new to start: in progress #29, #30"
    assert nxt.split(":")[0].split(" (")[0] == "/work-slice 29"      # the runnable part, as an offered slice's
    unrated = sdlc.next_command(epic, {**p, "in_flight": [30]})
    assert unrated == "/work-slice 30: resumes it; nothing new to start: in progress #30"
    # a slice of another milestone is never named: it couldn't land on the epic's branch while M1 is built
    other = {**units, 30: {"key": "T2.1", "milestone": "M2"}}
    p2 = {**p, "in_flight": [30, 29], "units": other}
    assert sdlc.next_command(epic, p2).startswith("/work-slice 29 (opus: judgment): resumes it; "
                                                  "nothing new to start: in progress #30, #29")
    only_other = sdlc.next_command(epic, {**p2, "in_flight": [30]})
    assert only_other == "nothing new to start: in progress #30, none of them in #12 M1: x, the milestone being built"
