"""tools/sdlc.py: the quick lane in the ledger — the lane marker, the state, `next`, the frozen brief. No network."""
import json

import pytest

from tools import sdlc

CONFIG = sdlc.load_config()
OWNER, POSTER = "owner", "poster"
TRUSTED = {OWNER}
ROUTINE = "complexity:routine"

_ids = iter(range(1, 10_000))


def issue(labels=(), state="open", number=12):
    return {"number": number, "title": "Add a station", "state": state, "author": POSTER, "labels": list(labels)}


def comment(author, body, ts):
    n = next(_ids)
    return {"id": n, "author": author, "body": body, "created_at": f"2026-10-02T00:{ts:02d}:00Z",
            "url": f"https://example/issues/12#issuecomment-{1000 + n}"}


def agent(kind, rev, ts, body="text", **extra):
    return comment(OWNER, sdlc.render_comment(kind, rev, body, CONFIG, **extra), ts)


def derive(labels, comments, **kw):
    return sdlc.derive_state(issue(labels, **kw), comments, TRUSTED, CONFIG)


def quick_brief(ts=1, rev=1, kind="prd", body="## Outcome\nthe brief"):
    return agent(kind, rev, ts, body, lane="quick")


def approved_quick(kind="prd"):
    """A quick brief and its recorded approval."""
    return [quick_brief(kind=kind), agent("approval", 1, 2, by=POSTER, via="comment")]


# --- the lane marker, posted by `transition --lane quick` ---------------------

def bundle_file(tmp_path, labels, comments=()):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue(labels), "comments": list(comments), "trusted": [OWNER]}))
    body = tmp_path / "brief.md"
    body.write_text("## Outcome\nx")
    return str(f), str(body)


@pytest.mark.parametrize("kind,to", [("prd", "prd-review"), ("diagnosis", "diagnosis-review")])
def test_a_quick_revision_carries_the_lane(tmp_path, capsys, monkeypatch, kind, to):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    f, body = bundle_file(tmp_path, [ROUTINE])
    assert sdlc.main(["transition", "12", to, "--kind", kind, "--lane", "quick", "--body-file", body,
                      "--from-file", f, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"kind={kind} rev=1 lane=quick" in out and "lane:quick" in out


@pytest.mark.parametrize("labels,named", [([], "unrated"), (["complexity:judgment"], "complexity:judgment"),
                                          (["complexity:novel"], "complexity:novel")])
def test_only_a_routine_issue_takes_the_quick_lane(tmp_path, capsys, labels, named):
    f, body = bundle_file(tmp_path, labels)
    assert sdlc.main(["transition", "12", "prd-review", "--kind", "prd", "--lane", "quick", "--body-file", body,
                      "--from-file", f, "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert ROUTINE in err and named in err


def test_the_lane_goes_on_a_brief_only():
    st = derive([ROUTINE], [])
    errs = sdlc.check_transition(st, "needs-info", "question", CONFIG, lane="quick", labels=[ROUTINE])
    assert any("prd or diagnosis" in e for e in errs)
    assert sdlc.check_transition(st, "prd-review", "prd", CONFIG, lane="quick", labels=[ROUTINE]) == []


class FakeGitHub:
    """Just enough of `gh` for a real (not dry-run) transition: what was posted and how the labels moved."""

    def __init__(self, monkeypatch, labels):
        self.labels, self.posted, self.edits = set(labels), [], []
        monkeypatch.setattr(sdlc, "repo_of", lambda config: "o/r")
        monkeypatch.setattr(sdlc, "gh_json", lambda args: [{"name": n} for n in self.labels])
        monkeypatch.setattr(sdlc, "post_comment", lambda n, body, config: self.posted.append(body) or {"html_url": "u"})
        monkeypatch.setattr(sdlc, "patch_comment", lambda *a: None)
        monkeypatch.setattr(sdlc, "gh", lambda args, **k: self.edits.append(args) or "")


def test_the_lane_label_follows_the_revision(tmp_path, monkeypatch):
    every = {l["name"] for l in CONFIG["labels"]} | {"lane:quick"}
    fake = FakeGitHub(monkeypatch, every)
    f, body = bundle_file(tmp_path, [ROUTINE])
    sdlc.main(["transition", "12", "prd-review", "--kind", "prd", "--lane", "quick", "--body-file", body, "--from-file", f])
    assert "lane=quick" in fake.posted[0].splitlines()[0]
    assert fake.edits[-1][fake.edits[-1].index("lane:quick") - 1] == "--add-label"
    # a later revision without --lane is the full lane: the label comes off in the same edit
    f, body = bundle_file(tmp_path, [ROUTINE, "sdlc:prd-review", "lane:quick"], [quick_brief()])
    sdlc.main(["transition", "12", "prd-review", "--kind", "prd", "--body-file", body, "--from-file", f])
    assert "lane=" not in fake.posted[1].splitlines()[0]
    assert fake.edits[-1][fake.edits[-1].index("lane:quick") - 1] == "--remove-label"


def test_a_missing_lane_label_posts_nothing(tmp_path, capsys, monkeypatch):
    fake = FakeGitHub(monkeypatch, {l["name"] for l in CONFIG["labels"]})
    f, body = bundle_file(tmp_path, [ROUTINE])
    assert sdlc.main(["transition", "12", "prd-review", "--kind", "prd", "--lane", "quick", "--body-file", body,
                      "--from-file", f]) == 1
    assert "lane:quick does not exist" in capsys.readouterr().err
    assert fake.posted == [] and fake.edits == []


def test_bootstrap_labels_creates_the_lane_label():
    assert "lane:quick" in {l["name"] for l in CONFIG["extra_labels"]}
    assert "lane:quick" not in sdlc.state_labels(CONFIG)   # a lane, never a state


# --- the state: lane, action, next ---------------------------------------------

def test_state_shows_the_lane(tmp_path, capsys):
    review = derive([ROUTINE, "sdlc:prd-review"], [quick_brief()])
    assert review["lane"] == "quick" and review["action"] == "wait_for_poster"   # triage as usual until approved
    assert derive([ROUTINE, "sdlc:approved"], approved_quick())["lane"] == "quick"
    full = derive(["sdlc:approved"], [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER)])
    assert full["lane"] is None and full["action"] == "plan"
    # a later revision without the marker returns the issue to the full lane
    later = derive(["sdlc:prd-review"], approved_quick() + [agent("prd", 2, 3)])
    assert later["lane"] is None
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue([ROUTINE, "sdlc:approved"]), "comments": approved_quick(), "trusted": [OWNER]}))
    assert sdlc.main(["state", "--from-file", str(f), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["lane"] == "quick"
    assert sdlc.main(["state", "--from-file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "state: approved   lane: quick   turn: agent   action: work_quick" in out
    assert "next: /work-slice 12 --quick" in out


def test_a_full_lane_state_line_is_unchanged(tmp_path, capsys):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue([]), "comments": [], "trusted": [OWNER]}))
    sdlc.main(["state", "--from-file", str(f)])
    assert "lane" not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["prd", "diagnosis"])
@pytest.mark.parametrize("label", ["sdlc:approved", "sdlc:in-progress"])
def test_an_approved_quick_issue_is_built_not_planned(kind, label):
    st = derive([ROUTINE, label], approved_quick(kind))
    assert (st["turn"], st["action"], st["conflicts"]) == ("agent", "work_quick", [])
    assert sdlc.next_command(st) == "/work-slice 12 --quick"
    # `with_next` asks GitHub for nothing: a quick issue has no epic progress
    assert sdlc.with_next(st, CONFIG, trusted=TRUSTED)["progress"] is None


def test_a_quick_approval_is_reconciled_first():
    st = derive([ROUTINE, "sdlc:approved"], [quick_brief()])   # ticked by hand: no approval on record
    assert st["action"] == "reconcile_label"


@pytest.mark.parametrize("labels,kw", [(["sdlc:done"], {}), (["sdlc:in-progress"], {"state": "closed"}),
                                       (["sdlc:done"], {"state": "closed"})])
def test_a_finished_quick_issue_has_nothing_to_do(labels, kw):
    st = derive([ROUTINE, *labels], approved_quick(), **kw)
    assert st["action"] == "none" and sdlc.next_command(st).startswith("nothing")


def test_a_quick_issue_never_reaches_epic_code():
    # labelled planned, or building a brief that isn't the approved quick one: a conflict, never work_slices
    planned = derive([ROUTINE, "sdlc:planned"], approved_quick())
    unapproved = derive([ROUTINE, "sdlc:in-progress"], approved_quick() + [quick_brief(ts=3, rev=2)])
    for st in (planned, unapproved):
        assert st["action"] == "fix_conflict" and st["conflicts"]
    assert not sdlc.epic_building(derive([ROUTINE, "sdlc:in-progress"], approved_quick()))
    assert sdlc.epic_building(derive(["sdlc:in-progress"], [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER)]))


@pytest.mark.parametrize("argv", [
    ["demo-post", "12", "--milestone", "M1", "--dir", "{d}", "--body-file", "{b}"],
    ["demo-request", "12", "--milestone", "M1", "--body-file", "{b}"],
    ["demo-changes", "12", "--milestone", "M1", "--found", "--body-file", "{b}"],
])
def test_epic_commands_refuse_a_quick_issue(tmp_path, capsys, monkeypatch, argv):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("must refuse before GitHub"))
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue([ROUTINE, "sdlc:in-progress"]), "comments": approved_quick(),
                             "trusted": [OWNER]}))
    (tmp_path / "body.md").write_text("x")
    argv = [a.format(d=tmp_path, b=tmp_path / "body.md") for a in argv] + ["--from-file", str(f), "--dry-run"]
    assert sdlc.main(argv) == 1
    assert "is in-progress" in capsys.readouterr().err


def test_ship_refuses_a_quick_issue(capsys, monkeypatch):
    bundle = {"issue": issue([ROUTINE, "sdlc:done"]), "comments": approved_quick(), "trusted": [OWNER]}
    monkeypatch.setattr(sdlc, "repo_of", lambda config: "o/r")
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, config, trusted=None: bundle)
    monkeypatch.setattr(sdlc, "fetch_plan_issues", lambda *a: pytest.fail("a quick issue has no epic"))
    assert sdlc.main(["ship", "12", "--dry-run"]) == 1
    assert "quick change" in capsys.readouterr().err


def test_next_lists_a_quick_issue_only_while_it_needs_an_agent(monkeypatch, capsys):
    bundles = {
        1: {"issue": issue([ROUTINE, "sdlc:approved"], number=1), "comments": approved_quick(), "trusted": [OWNER]},
        2: {"issue": issue([ROUTINE, "sdlc:in-progress"], number=2), "comments": approved_quick(), "trusted": [OWNER]},
        3: {"issue": issue([ROUTINE, "sdlc:done"], number=3), "comments": approved_quick(), "trusted": [OWNER]},
        4: {"issue": issue([ROUTINE, "sdlc:prd-review"], number=4), "comments": [quick_brief()], "trusted": [OWNER]},
    }
    monkeypatch.setattr(sdlc, "repo_of", lambda config: "o/r")
    monkeypatch.setattr(sdlc, "fetch_trusted", lambda config: TRUSTED)
    monkeypatch.setattr(sdlc, "gh_json", lambda args: [{"number": n, "labels": []} for n in bundles])
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, config, trusted=None: bundles[n])
    monkeypatch.setattr(sdlc, "epic_progress", lambda *a: pytest.fail("a quick issue is not an epic"))
    assert sdlc.main(["next", "--json"]) == 0
    listed = {st["number"]: st["next"] for st in json.loads(capsys.readouterr().out)}
    assert listed == {1: "/work-slice 1 --quick", 2: "/work-slice 2 --quick"}


# --- the brief is frozen once building starts ------------------------------------

@pytest.mark.parametrize("to,kind", [("prd-review", "prd"), ("diagnosis-review", "diagnosis"), ("needs-info", "question")])
def test_a_quick_brief_is_frozen_while_it_is_built(to, kind):
    building = derive([ROUTINE, "sdlc:in-progress"], approved_quick())
    errs = sdlc.check_transition(building, to, kind, CONFIG)
    assert len(errs) == 1 and "escalate-to-full 12" in errs[0]
    # before the claim, a revision works as it does today
    approved = derive([ROUTINE, "sdlc:approved"], approved_quick())
    assert sdlc.check_transition(approved, to, kind, CONFIG) == []
    assert sdlc.check_transition(building, "escalated", "escalation", CONFIG) == []


def test_a_full_lane_issue_transitions_as_before():
    full = derive(["sdlc:approved"], [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER)])
    assert sdlc.check_transition(full, "prd-review", "prd", CONFIG) == []
    epic = derive(["sdlc:in-progress"], [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER)])
    assert not any("escalate-to-full" in e for e in sdlc.check_transition(epic, "prd-review", "prd", CONFIG))


def test_the_approval_says_what_comes_next(tmp_path, capsys):
    f = tmp_path / "b.json"
    reply = comment(POSTER, "/approve", 2)
    f.write_text(json.dumps({"issue": issue([ROUTINE, "sdlc:prd-review"]), "comments": [quick_brief(), reply],
                             "trusted": [OWNER]}))
    assert sdlc.main(["transition", "12", "approved", "--kind", "approval", "--from-file", str(f), "--dry-run"]) == 0
    assert "work-slice 12 --quick" in capsys.readouterr().out


# --- the approved brief, shared by claim, slice-check and the Codex brief ----------

def test_approved_brief_is_the_approved_revision_without_its_marker():
    comments = approved_quick()
    st = derive([ROUTINE, "sdlc:approved"], comments)
    text = sdlc.approved_brief(st)
    assert "## Outcome\nthe brief" in text and "sdlc:v1" not in text
    assert sdlc.approved_brief_id(st) == str(1000 + comments[0]["id"])


def test_approved_brief_is_the_earlier_revision_while_a_newer_one_is_in_review():
    first = quick_brief(body="## Outcome\nfirst")
    second = quick_brief(ts=3, rev=2, body="## Outcome\nsecond")
    # posting rev 2 collapses rev 1's comment, as `transition` does
    first["body"] = sdlc.render_superseded(first["body"], 2, second["url"])
    st = derive([ROUTINE, "sdlc:prd-review"], [first, agent("approval", 1, 2, by=POSTER), second])
    assert st["approved_rev"] is None
    text = sdlc.approved_brief(st)
    assert "PRD rev 1\n\n## Outcome\nfirst" in text
    assert "second" not in text and "Superseded" not in text and "<details>" not in text and "sdlc:v1" not in text
    assert sdlc.approved_brief_id(st) == str(1000 + first["id"])


def test_nothing_approved_means_no_brief():
    st = derive([ROUTINE, "sdlc:prd-review"], [quick_brief()])
    assert sdlc.approved_brief(st) is None and sdlc.approved_brief_id(st) is None
