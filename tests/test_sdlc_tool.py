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
