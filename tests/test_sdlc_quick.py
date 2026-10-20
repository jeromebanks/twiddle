"""tools/sdlc.py: the quick lane in the ledger — the lane marker, the state, `next`, the frozen brief. No network."""
import dataclasses
import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from tools import sdlc, sdlc_quick

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
                                          (["complexity:novel"], "complexity:novel"),
                                          ([ROUTINE, "complexity:judgment"], "complexity:judgment"),
                                          ([ROUTINE, "complexity:novel"], "complexity:novel")])
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
def test_an_approved_quick_issue_is_built_not_planned(monkeypatch, kind, label):
    monkeypatch.setattr(sdlc, "epic_progress", lambda *a: pytest.fail("a quick issue is not an epic"))
    st = derive([ROUTINE, label], approved_quick(kind))
    assert (st["turn"], st["action"], st["conflicts"]) == ("agent", "work_quick", [])
    assert sdlc.next_command(st) == "/work-slice 12 --quick"
    # `with_next` asks GitHub for nothing: a quick issue has no epic progress
    assert sdlc.with_next(st, CONFIG, trusted=TRUSTED)["progress"] is None


def test_a_question_on_an_approved_quick_brief_is_triage_not_planning(tmp_path, capsys):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue([ROUTINE, "sdlc:approved"]), "comments": approved_quick(),
                             "trusted": [OWNER]}))
    (tmp_path / "q.md").write_text("which stream?")
    assert sdlc.main(["transition", "12", "needs-info", "--kind", "question", "--body-file", str(tmp_path / "q.md"),
                      "--from-file", str(f), "--dry-run"]) == 0
    assert "phase=plan" not in capsys.readouterr().out
    asked = approved_quick() + [agent("question", None, 3), comment(POSTER, "the AAC one", 4)]
    st = derive([ROUTINE, "sdlc:needs-info"], asked)
    assert st["action"] == "respond_to_reply" and sdlc.next_command(st) == "/triage-issue 12"


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
@pytest.mark.parametrize("offline", [True, False])
def test_epic_commands_refuse_a_quick_issue(tmp_path, capsys, monkeypatch, argv, offline):
    bundle = {"issue": issue([ROUTINE, "sdlc:in-progress"]), "comments": approved_quick(), "trusted": [OWNER]}
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("must refuse before GitHub"))
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, config, trusted=None: bundle)
    monkeypatch.setattr(sdlc, "fetch_plan_issues", lambda *a: pytest.fail("a quick issue has no plan issues"))
    monkeypatch.setattr(sdlc, "epic_progress", lambda *a: pytest.fail("a quick issue is not an epic"))
    f = tmp_path / "b.json"
    f.write_text(json.dumps(bundle))
    (tmp_path / "body.md").write_text("x")
    argv = [a.format(d=tmp_path, b=tmp_path / "body.md") for a in argv] + ["--dry-run"]
    assert sdlc.main(argv + (["--from-file", str(f)] if offline else [])) == 1
    err = capsys.readouterr().err
    assert "quick change" in err or "is in-progress" in err


def test_demo_status_refuses_a_quick_issue_before_any_epic_lookup(capsys, monkeypatch):
    bundle = {"issue": issue([ROUTINE, "sdlc:in-progress"]), "comments": approved_quick(), "trusted": [OWNER]}
    monkeypatch.setattr(sdlc, "repo_of", lambda config: "o/r")
    monkeypatch.setattr(sdlc, "fetch_trusted", lambda config: TRUSTED)
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, config, trusted=None: bundle)
    monkeypatch.setattr(sdlc, "fetch_plan_issues", lambda *a: pytest.fail("a quick issue has no plan issues"))
    monkeypatch.setattr(sdlc, "epic_progress", lambda *a: pytest.fail("a quick issue is not an epic"))
    assert sdlc.main(["demo-status", "12"]) == 1
    assert "quick change" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["ship-review", "12", "--report", "r.md"], ["codex-review", "--milestone", "12"]])
def test_milestone_reviews_refuse_a_quick_issue(capsys, monkeypatch, argv):
    bundle = {"issue": issue([ROUTINE, "sdlc:in-progress"]), "comments": approved_quick(), "trusted": [OWNER]}
    monkeypatch.setattr(sdlc, "repo_of", lambda config: "o/r")
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, config, trusted=None: bundle)
    monkeypatch.setattr(sdlc, "epic_progress", lambda *a: pytest.fail("a quick issue is not an epic"))
    assert sdlc.main(argv) == 1
    assert "quick change" in capsys.readouterr().err


def test_epic_view_never_offers_an_epic_step_for_a_quick_issue():
    st = derive([ROUTINE, "sdlc:in-progress"], approved_quick())
    empty = sdlc.summarise_progress([], {})
    view = sdlc.epic_view(st, {**empty, "behind_main": 0, "debt": [], "cleanup_budget": 1})
    assert view["due"] == "quick" and view["next"].startswith("/work-slice 12 --quick")
    assert sdlc.pause_errors(view) == []


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


# --- claiming a quick change (`claim N --quick`), and slice-check / slice-status ---------

BRIEF = "\n\n".join(f"## {name}\n{name.lower()} of the change" for name in sdlc_quick.BRIEF_SECTIONS)
HEAD = "a" * 40


def marked(kind, ts, **extra):
    return comment(OWNER, sdlc.marker(kind, None, **extra) + "\nx", ts)


def ready_quick(brief=BRIEF, kind="prd"):
    return [quick_brief(kind=kind, body=brief), agent("approval", 1, 2, by=POSTER, via="comment")]


CLAIM = marked("claim", 5, branch="slice/12", base="main", quick="1")


def quick_bundle(labels=(ROUTINE, "sdlc:approved"), comments=None, state="open", pr=None, pr_comments=(), **kw):
    i = {**issue(list(labels), state=state), "body": "the poster's request", **kw}
    return {"issue": i, "comments": ready_quick() if comments is None else list(comments), "trusted": [OWNER],
            "blockers": [], "pr": pr, "pr_comments": list(pr_comments)}


def check(b, resume=False, is_slice=False):
    st = sdlc.derive_state(b["issue"], b["comments"], TRUSTED, CONFIG)
    return sdlc_quick.quick_check_errors(b["issue"], st, sdlc.approved_brief(st),
                                         sdlc.claim_status(b["comments"], TRUSTED), resume, is_slice)


def test_an_approved_quick_brief_may_be_claimed():
    assert check(quick_bundle()) == []
    # a diagnosis brief too, and a brief with no Demo: the quick lane's sections have Files instead
    assert check(quick_bundle(comments=ready_quick(kind="diagnosis"))) == []
    assert "Demo" not in BRIEF


@pytest.mark.parametrize("case,needle", [
    ("unapproved", "has no approved brief"),
    ("stale", "the latest is rev 2"),
    ("full-lane", "is not a quick brief"),
    ("stale-full", "the latest is rev 2"),
    ("escalated", "escalated to a human"),
    ("needs-info", "is needs-info, not approved"),
    ("closed", "is closed"),
    ("unrated", "complexity:routine"),
    ("judgment", "complexity:judgment"),
    ("claimed", "already claimed"),
    ("no-files", "lacks sections: Files"),
    ("no-context", "lacks sections: Context"),
    ("lowercase", "lacks sections: Non-goals"),     # read as codex_review.section reads it: exactly
    ("resume-unclaimed", "no claim to resume"),
    ("slice", "is a plan:slice issue"),
])
def test_each_refusal_has_its_own_message(case, needle):
    resume, is_slice = case == "resume-unclaimed", case == "slice"
    b = {"unapproved": quick_bundle([ROUTINE, "sdlc:prd-review"], [quick_brief(body=BRIEF)]),
         "stale": quick_bundle([ROUTINE, "sdlc:prd-review"], ready_quick() + [quick_brief(ts=3, rev=2, body=BRIEF)]),
         "full-lane": quick_bundle(comments=[agent("prd", 1, 1, BRIEF), agent("approval", 1, 2, by=POSTER)]),
         "stale-full": quick_bundle([ROUTINE, "sdlc:prd-review"], ready_quick() + [agent("prd", 2, 3, BRIEF)]),
         "escalated": quick_bundle([ROUTINE, "sdlc:escalated"]),
         "needs-info": quick_bundle([ROUTINE, "sdlc:needs-info"], ready_quick() + [agent("question", None, 3)]),
         "closed": quick_bundle(state="closed"),
         "unrated": quick_bundle(["sdlc:approved"]),
         "judgment": quick_bundle([ROUTINE, "complexity:judgment", "sdlc:approved"]),
         "claimed": quick_bundle([ROUTINE, "sdlc:in-progress"], ready_quick() + [CLAIM]),
         "no-files": quick_bundle(comments=ready_quick(BRIEF.replace("## Files", "## Paths"))),
         "no-context": quick_bundle(comments=ready_quick(BRIEF.replace("## Context", "Context:"))),
         "lowercase": quick_bundle(comments=ready_quick(BRIEF.replace("## Non-goals", "## non-goals"))),
         "resume-unclaimed": quick_bundle(),
         "slice": quick_bundle(["plan:slice", ROUTINE]),
         }[case]
    errs = check(b, resume, is_slice)
    assert errs and any(needle in e for e in errs), errs
    if case not in ("unapproved", "stale", "stale-full"):   # each other defect is the only refusal it causes
        assert len(errs) == 1, errs


def test_the_brief_is_checked_not_the_issue_body():
    b = quick_bundle(body=BRIEF)   # the poster's issue body has every section, the approved brief has one
    b["comments"] = ready_quick("## Outcome\nonly this")
    assert any("lacks sections: Scope, Files" in e for e in check(b))


def test_a_released_claim_may_be_claimed_again():
    b = quick_bundle([ROUTINE, "sdlc:in-progress"], ready_quick() + [CLAIM, marked("release", 6)])
    assert check(b) == []


# --- claim N --quick against a scratch repo with a bare origin -------------------------

def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    origin, root = tmp_path / "origin.git", tmp_path / "repo"
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git("config", k, v, cwd=root)
    (root / "code.py").write_text("x = 1\n")
    (root / ".gitignore").write_text(".worktrees/\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "main", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("push", "-q", "origin", "main", cwd=root)
    monkeypatch.chdir(root)    # primary_root() and worktree_path() read the checkout they run in
    return root


class FakeClaim:
    """`gh` for a claim: the bundle it reads, the comments it posts, the edits it makes."""

    def __init__(self, monkeypatch, bundle):
        self.bundle, self.posted, self.edits = bundle, [], []
        monkeypatch.setattr(sdlc, "repo_of", lambda config: "o/r")
        monkeypatch.setattr(sdlc, "slice_bundle", lambda n, config: self.bundle)
        monkeypatch.setattr(sdlc, "post_comment", lambda n, body, config: self.posted.append(body) or {"html_url": "u"})
        monkeypatch.setattr(sdlc, "gh", lambda args, **k: self.edits.append(args) or "")
        monkeypatch.setattr(sdlc, "gh_json", lambda args: pytest.fail(f"a quick claim reads no epic: {args}"))
        monkeypatch.setattr(sdlc, "fetch_bundle", lambda *a, **k: pytest.fail("a quick claim reads no epic"))


def test_claim_quick_dry_run_names_the_worktree_and_makes_nothing(repo, monkeypatch, capsys):
    fake = FakeClaim(monkeypatch, quick_bundle())
    assert sdlc.main(["claim", "12", "--quick", "--dry-run"]) == 0
    out = capsys.readouterr().out
    wt = repo / ".worktrees" / "slice-12"
    assert f"worktree {wt} on slice/12 from origin/main" in out
    assert not wt.exists() and fake.posted == [] and fake.edits == []
    assert not _git("branch", "--list", "slice/12", cwd=repo)


def test_claim_quick_makes_the_worktree_posts_one_claim_and_moves_the_label(repo, monkeypatch, capsys):
    fake = FakeClaim(monkeypatch, quick_bundle())
    assert sdlc.main(["claim", "12", "--quick", "--model", "sonnet"]) == 0
    out = capsys.readouterr().out
    wt = repo / ".worktrees" / "slice-12"
    assert f"WORKTREE={wt}" in out and "BRANCH=slice/12" in out and "BASE=origin/main" in out
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "slice/12"
    assert _git("rev-parse", "HEAD", cwd=wt) == _git("rev-parse", "origin/main", cwd=repo)
    assert len(fake.posted) == 1
    mk = sdlc.parse_marker(fake.posted[0])
    assert (mk["kind"], mk["branch"], mk["base"], mk["quick"], mk["model"]) == ("claim", "slice/12", "main", "1", "sonnet")
    assert any("--add-assignee" in e for e in fake.edits)
    label = next(e for e in fake.edits if "--add-label" in e)
    assert label[label.index("--add-label") + 1] == "sdlc:in-progress"
    assert label[label.index("--remove-label") + 1] == "sdlc:approved"
    assert "#12: approved -> in-progress" in out

    # the claim is now on record: a second fresh claim is refused, a resume carries on
    fake.bundle = quick_bundle([ROUTINE, "sdlc:in-progress"], ready_quick() + [CLAIM])
    fake.posted.clear(), fake.edits.clear()
    assert sdlc.main(["claim", "12", "--quick"]) == 1
    assert "already claimed" in capsys.readouterr().err
    assert sdlc.main(["claim", "12", "--quick", "--resume"]) == 0
    assert f"WORKTREE={wt}" in capsys.readouterr().out
    assert fake.posted == [] and fake.edits == []   # no second claim, and the label is already in-progress

    # `cleanup` needs nothing new: the worktree and the branch are a slice's
    monkeypatch.setattr(sdlc, "find_slice_pr", lambda n, config: {"state": "MERGED"})
    assert sdlc.main(["cleanup", "12"]) == 0
    assert not wt.exists() and not _git("branch", "--list", "slice/12", cwd=repo)


RESUME_BRIEF = BRIEF.replace("## Files\nfiles of the change", "## Files\n- `station.toml`")


def test_claim_quick_resume_reattaches_a_pushed_branch(repo, monkeypatch, capsys):
    # its brief lists the file the branch adds: a resume checks the pushed work against the brief (F4)
    FakeClaim(monkeypatch, quick_bundle(comments=ready_quick(RESUME_BRIEF)))
    sdlc.main(["claim", "12", "--quick"])
    wt = repo / ".worktrees" / "slice-12"
    (wt / "station.toml").write_text("key = 'x'\n")
    _git("add", ".", cwd=wt)
    _git("commit", "-q", "-m", "station", cwd=wt)
    _git("push", "-q", "origin", "slice/12", cwd=wt)
    pushed = _git("rev-parse", "HEAD", cwd=wt)
    _git("worktree", "remove", "--force", str(wt), cwd=repo)
    _git("branch", "-D", "slice/12", cwd=repo)
    # a claim cut short before its label moved: resuming finishes the move
    fake = FakeClaim(monkeypatch, quick_bundle([ROUTINE, "sdlc:approved"], ready_quick(RESUME_BRIEF) + [CLAIM]))
    capsys.readouterr()
    assert sdlc.main(["claim", "12", "--quick", "--resume"]) == 0
    assert _git("rev-parse", "HEAD", cwd=wt) == pushed
    assert fake.posted == [] and any("sdlc:in-progress" in e for e in fake.edits)


@pytest.mark.parametrize("argv,bundle,needle", [
    (["claim", "12"], quick_bundle(), "`claim 12 --quick`"),
    (["claim", "12", "--quick"], quick_bundle(["plan:slice", ROUTINE]), "without --quick"),
    (["claim", "12", "--quick", "--retry", "--model", "opus"], quick_bundle(), "never retried"),
])
def test_the_wrong_flag_is_refused_either_way(repo, monkeypatch, capsys, argv, bundle, needle):
    fake = FakeClaim(monkeypatch, bundle)
    assert sdlc.main(argv) == 1
    assert needle in capsys.readouterr().err
    assert fake.posted == [] and fake.edits == [] and not (repo / ".worktrees").exists()


# --- claim --quick applies the quick rules too: the brief's Files, and on resume the work resumed (F4) ---

def files_brief(*entries):
    return BRIEF.replace("## Files\nfiles of the change", "## Files\n" + "\n".join(f"- `{e}`" for e in entries))


def claim_refused(monkeypatch, capsys, brief, *flags, comments=()):
    fake = FakeClaim(monkeypatch, quick_bundle(comments=ready_quick(brief) + list(comments)))
    code = sdlc.main(["claim", "12", "--quick", *flags])
    return code, capsys.readouterr(), fake


@pytest.mark.parametrize("entry,reaches", [
    ("tools/sdlc.py", "tools/sdlc.py"),
    ("src/twiddle/play.py", "src/twiddle/play.py"),
    ("src/twiddle/a*/clock.py", "src/twiddle/alarms/"),   # its literal prefix `src/twiddle/a` overlaps two denied
    ("*.py", "tools/"),                                    # no literal prefix: it reaches every denied path
    ("tool?/x.py", "tools/"),
])
@pytest.mark.parametrize("dry", [False, True])
def test_a_brief_naming_a_denied_path_is_refused_at_claim(repo, monkeypatch, capsys, entry, reaches, dry):
    code, out, fake = claim_refused(monkeypatch, capsys, files_brief(entry, "tests/test_stations.py"),
                                    *(["--dry-run"] if dry else []))
    assert code == 1, out
    assert "the brief's `## Files` names a path a quick change may not" in out.err and reaches in out.err
    assert out.err.rstrip().endswith("`escalate-to-full 12`")
    assert fake.posted == [] and fake.edits == [] and not (repo / ".worktrees").exists()   # and nothing escalated


def test_a_brief_whose_patterns_stay_clear_of_denied_paths_claims(repo, monkeypatch, capsys):
    assert claim_refused(monkeypatch, capsys, GATE_BRIEF, "--dry-run")[0] == 0     # src/twiddle/stations/catalog/*.toml
    for e in ("src/twiddle/stations/catalog/*.toml", "tests/test_*.py", "docs/STATIONS.md", "toolsx/a.py",
              "toolsx/*.py", "src/twiddle/alarms_notes/*.md", "uv.lock.d/*"):   # a sibling is never under it
        assert sdlc_quick.entry_reaches(e, [], QUICK_RULES) == [], e
    for e, d in (("tools*", "tools/"), ("tools/x*", "tools/"), ("src/twiddle/alarms/*", "src/twiddle/alarms/"),
                 ("uv.lo?k", "uv.lock")):
        assert d in sdlc_quick.entry_reaches(e, [], QUICK_RULES), e


def test_a_pattern_names_the_denied_files_on_main_it_matches():
    tracked = ["src/twiddle/alarms/clock.py", "src/twiddle/alarms/model.py", "src/twiddle/stations/model.py"]
    assert sdlc_quick.entry_reaches("src/twiddle/a*/clock.py", tracked, QUICK_RULES) == ["src/twiddle/alarms/clock.py"]
    # nothing on main matches it today, but the prefix still could: refused all the same
    assert sdlc_quick.entry_reaches("src/twiddle/alarms/n*.py", tracked, QUICK_RULES) == ["src/twiddle/alarms/"]


def test_too_many_files_in_the_brief_is_refused_at_claim(repo, monkeypatch, capsys):
    six = [f"tests/test_{i}.py" for i in range(6)]
    code, out, fake = claim_refused(monkeypatch, capsys, files_brief(*six), "--dry-run")
    assert code == 1 and "the brief's `## Files` lists 6 files, more than the quick lane's 5" in out.err
    assert "`escalate-to-full 12`" in out.err and fake.posted == []
    assert claim_refused(monkeypatch, capsys, files_brief(*six[:5]), "--dry-run")[0] == 0


def test_an_exact_path_brief_is_checked_with_no_git(monkeypatch, capsys):
    monkeypatch.setattr(sdlc, "git", lambda *a, **k: pytest.fail(f"no git for an exact-path brief: {a}"))
    code, out, _ = claim_refused(monkeypatch, capsys, files_brief("src/twiddle/play.py"), "--dry-run")
    assert code == 1 and "src/twiddle/play.py" in out.err


def test_tracked_files_are_main_s_not_the_index_s(repo):
    (repo / "src").mkdir()
    (repo / "src" / "on_main.py").write_text("x\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "on main", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)
    _git("rm", "-q", "--cached", "src/on_main.py", cwd=repo)          # the index lacks a file main has
    (repo / "staged_only.py").write_text("y\n")
    _git("add", "staged_only.py", cwd=repo)                            # and holds one main lacks
    tracked = sdlc.tracked_on("origin/main", repo)
    assert "src/on_main.py" in tracked and "staged_only.py" not in tracked
    assert "src/on_main.py" not in _git("ls-files", cwd=repo)          # what `git ls-files` would have missed


def test_the_demo_claim_fixture(monkeypatch, capsys):
    # the milestone demo runs `claim 12 --quick --dry-run --from-file tests/fixtures/quick_claim/denied_path.json`
    for name in ("gh", "gh_json", "post_comment", "git"):
        monkeypatch.setattr(sdlc, name, lambda *a, **k: pytest.fail(f"--from-file --dry-run reads nothing else: {a}"))
    f = str(Path(__file__).resolve().parent / "fixtures" / "quick_claim" / "denied_path.json")
    assert sdlc.main(["claim", "12", "--quick", "--dry-run", "--from-file", f]) == 1
    err = capsys.readouterr().err
    assert "may not (src/twiddle/play.py)" in err and "`escalate-to-full 12`" in err
    assert sdlc.main(["claim", "12", "--quick", "--from-file", f]) == 1               # never for real
    assert "--quick --dry-run only" in capsys.readouterr().err
    assert sdlc.main(["claim", "12", "--quick", "--dry-run", "--resume", "--from-file", f]) == 1
    assert "no --resume" in capsys.readouterr().err


def test_a_pattern_brief_from_a_file_is_checked_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sdlc, "git", lambda *a, **k: pytest.fail(f"--from-file runs no git: {a}"))
    f = tmp_path / "b.json"
    f.write_text(json.dumps(quick_bundle(comments=ready_quick(files_brief("src/twiddle/a*/clock.py")))))
    assert sdlc.main(["claim", "12", "--quick", "--dry-run", "--from-file", str(f)]) == 1
    assert "src/twiddle/a*/clock.py (reaches src/twiddle/alarm_cli.py, src/twiddle/alarms/)" in capsys.readouterr().err


def test_an_eligible_brief_from_a_file_is_checked_offline(tmp_path, monkeypatch, capsys):
    for name in ("git", "gh", "gh_json", "post_comment"):
        monkeypatch.setattr(sdlc, name, lambda *a, **k: pytest.fail(f"--from-file reads nothing else: {a}"))
    f = tmp_path / "b.json"
    f.write_text(json.dumps(quick_bundle(comments=ready_quick(GATE_BRIEF))))
    assert sdlc.main(["claim", "12", "--quick", "--dry-run", "--from-file", str(f)]) == 0
    assert "would claim #12" in capsys.readouterr().out


@pytest.mark.parametrize("entry", ["./tools/sdlc.py", "/tools/sdlc.py", "tools\\sdlc.py", "src/../tools/x.py", "./*.py"])
def test_an_unnormalised_entry_is_read_as_the_path_it_spells(entry):
    assert sdlc_quick.entry_reaches(entry, [], QUICK_RULES)


# a resumed claim: the work it carries on, read with the merge gate's diff rules

SAFE = "src/twiddle/stations/catalog/x.toml"


def resumable(repo, monkeypatch, brief):
    """A quick change claimed in the scratch repo; the claim on record, so `--resume` carries it on."""
    fake = FakeClaim(monkeypatch, quick_bundle(comments=ready_quick(brief)))
    assert sdlc.main(["claim", "12", "--quick"]) == 0
    fake.bundle = quick_bundle([ROUTINE, "sdlc:in-progress"], ready_quick(brief) + [CLAIM])
    fake.posted.clear(), fake.edits.clear()
    return repo / ".worktrees" / "slice-12", fake


def commit(wt, path, lines=1, push=False):
    (wt / path).parent.mkdir(parents=True, exist_ok=True)
    (wt / path).write_text("".join(f"{i}\n" for i in range(lines)))
    _git("add", "-A", cwd=wt)
    _git("commit", "-q", "-m", path, cwd=wt)
    if push:
        _git("push", "-q", "origin", "slice/12", cwd=wt)


def resume(capsys, *flags):
    code = sdlc.main(["claim", "12", "--quick", "--resume", *flags])
    return code, capsys.readouterr()


@pytest.mark.parametrize("dry", [False, True])
def test_a_local_commit_on_a_safe_pushed_branch_is_refused(repo, monkeypatch, capsys, dry):
    wt, fake = resumable(repo, monkeypatch, files_brief(SAFE))
    commit(wt, SAFE, push=True)
    commit(wt, "src/twiddle/play.py")                                   # local only, on top of the pushed one
    capsys.readouterr()
    code, out = resume(capsys, *(["--dry-run"] if dry else []))
    assert code == 1
    assert "the local branch slice/12: the diff touches a path a quick change may not (src/twiddle/play.py)" in out.err
    assert "origin/slice/12" not in out.err                             # the pushed branch alone is safe
    assert out.err.rstrip().endswith("`escalate-to-full 12`") and fake.posted == [] and fake.edits == []


def test_a_local_only_branch_touching_a_denied_path_is_refused(repo, monkeypatch, capsys):
    wt, _ = resumable(repo, monkeypatch, files_brief(SAFE))
    commit(wt, "tools/sdlc.py")
    _git("worktree", "remove", "--force", str(wt), cwd=repo)            # the branch alone, never pushed
    capsys.readouterr()
    code, out = resume(capsys)
    assert code == 1 and "the local branch slice/12: the diff touches a path a quick change may not (tools/sdlc.py)" \
        in out.err
    assert not wt.exists()                                              # refused before the worktree is reattached


@pytest.mark.parametrize("change", ["modified", "outside", "untracked", "renamed", "staged-then-reverted"])
def test_an_uncommitted_change_in_the_worktree_is_refused(repo, monkeypatch, capsys, change):
    commit(repo, "tools/x.py")                                          # a denied file on main
    _git("push", "-q", "origin", "main", cwd=repo)
    wt, _ = resumable(repo, monkeypatch, files_brief(SAFE, "code.py"))
    if change == "modified":
        (wt / "tools" / "x.py").write_text("changed\n")                 # unstaged
    elif change == "outside":
        (wt / ".gitignore").write_text(".worktrees/\nmore\n")             # tracked, and outside the brief
    elif change == "staged-then-reverted":
        (wt / "tools" / "x.py").write_text("staged\n")
        _git("add", "tools/x.py", cwd=wt)                               # the index holds the denied edit
        _git("restore", "--source=HEAD", "--worktree", "tools/x.py", cwd=wt)   # and the working copy is HEAD's
        assert (wt / "tools" / "x.py").read_text() == _git("show", "HEAD:tools/x.py", cwd=wt) + "\n"
        assert _git("diff", "--cached", "--name-only", cwd=wt) == "tools/x.py"
    elif change == "untracked":
        (wt / "tools" / "new.py").write_text("x\n")
    else:
        _git("mv", "code.py", "uv.lock", cwd=wt)                        # moved into a denied name, not committed
    index = _git("ls-files", "-s", cwd=wt)
    capsys.readouterr()
    code, out = resume(capsys)
    want = {"modified": "may not (tools/x.py)", "outside": "outside the brief's `## Files` list (.gitignore)",
            "untracked": "may not (tools/new.py)", "renamed": "may not (uv.lock)",
            "staged-then-reverted": "may not (tools/x.py)"}[change]
    where = f"the worktree {wt}'s index" if change == "staged-then-reverted" else f"the worktree {wt}"
    assert code == 1 and f"{where}: the diff" in out.err and want in out.err, out.err
    assert _git("ls-files", "-s", cwd=wt) == index                     # the worktree's own index never moved
    assert _git("status", "--porcelain", cwd=wt)                        # and its change is still there


def test_a_pushed_branch_over_the_size_cap_is_refused(repo, monkeypatch, capsys):
    wt, _ = resumable(repo, monkeypatch, files_brief(SAFE))
    commit(wt, SAFE, lines=QUICK_RULES.max_lines + 1, push=True)
    _git("worktree", "remove", "--force", str(wt), cwd=repo)
    _git("branch", "-D", "slice/12", cwd=repo)                          # only the pushed branch is left
    capsys.readouterr()
    code, out = resume(capsys)
    assert code == 1 and f"origin/slice/12: the diff changes {QUICK_RULES.max_lines + 1} lines, more than" in out.err


def test_a_safe_resume_carries_on(repo, monkeypatch, capsys):
    wt, fake = resumable(repo, monkeypatch, files_brief(SAFE, "notes/*.md"))
    commit(wt, SAFE, push=True)
    commit(wt, "notes/a.md")
    (wt / "notes" / "b.md").write_text("new\n")
    capsys.readouterr()
    code, out = resume(capsys)
    assert code == 0 and f"WORKTREE={wt}" in out.out and fake.posted == []


def test_numstat_rows_read_like_github_s():
    out = "3\t1\tsrc/a.py\0-\t-\tpic.png\0" + "0\t0\t\0old/x.py\0new/x.py\0"
    assert sdlc_quick.numstat_files(out) == [
        {"filename": "src/a.py", "additions": 3, "deletions": 1},
        {"filename": "pic.png", "additions": 0, "deletions": 0},
        {"filename": "new/x.py", "previous_filename": "old/x.py", "additions": 0, "deletions": 0}]
    assert sdlc_quick.numstat_files("") == []


# --- slice-status and slice-check read a quick change -------------------------------

def status(tmp_path, capsys, b, *flags):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(b))
    code = sdlc.main(["slice-status", "12", "--from-file", str(f), *flags])
    out = capsys.readouterr().out
    return code, (json.loads(out) if "--json" in flags else out)


PR = {"number": 50, "state": "OPEN", "headRefOid": HEAD, "baseRefName": "main", "headRefName": "slice/12"}
RECORDS = [marked("tests", 7, sha=HEAD, result="pass", passed="1300"),
           marked("pr-review", 8, sha=HEAD, verdict="approve", round="1")]


@pytest.mark.parametrize("case,want", [("ready", "ready"), ("claimed", "claimed"), ("in-review", "in-review"),
                                       ("merged", "merged")])
def test_slice_status_reads_a_quick_change(tmp_path, capsys, case, want):
    b = {"ready": quick_bundle(),
         "claimed": quick_bundle([ROUTINE, "sdlc:in-progress"], ready_quick() + [CLAIM]),
         "in-review": quick_bundle([ROUTINE, "sdlc:in-progress"], ready_quick() + [CLAIM], pr=PR, pr_comments=RECORDS),
         "merged": quick_bundle([ROUTINE, "sdlc:done"], ready_quick() + [CLAIM], state="closed", state_reason="completed",
                                pr={**PR, "state": "MERGED"}, pr_comments=RECORDS),
         }[case]
    assert status(tmp_path, capsys, b)[0] == 0
    st = status(tmp_path, capsys, b, "--json")[1]
    assert (st["state"], st["lane"], st["epic"]) == (want, "quick", None)
    assert (st["review"], st["review_mandatory"]) == ("slice", True)
    if case in ("in-review", "merged"):
        assert st["tests_on_head"]["result"] == "pass" and st["review_on_head"]["verdict"] == "approve"
    if case != "merged":
        assert st["next"] == "/work-slice 12 --quick"
    out = status(tmp_path, capsys, b)[1]
    assert f"state: {want}   lane: quick" in out
    assert "Codex review: on the PR (mandatory" in out and "with the milestone" not in out


def test_a_routine_quick_change_is_still_reviewed_on_the_pr(tmp_path, capsys):
    # every quick change is routine, which the config sends to the milestone for a slice; never for a quick one
    assert sdlc.review_mode("routine", CONFIG) == "milestone"
    st = status(tmp_path, capsys, quick_bundle(), "--json")[1]
    assert st["complexity"] == "routine" and st["review"] == "slice"


def test_slice_status_of_a_brief_still_in_review_says_what_comes_first(tmp_path, capsys):
    st = status(tmp_path, capsys, quick_bundle([ROUTINE, "sdlc:prd-review"], [quick_brief(body=BRIEF)]), "--json")[1]
    assert st["state"] == "not-ready" and st["next"] == "nothing: waiting on the poster to reply on #12"
    assert any("has no approved brief" in e for e in st["refusals"])


def test_slice_check_reads_the_quick_brief(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda *a, **k: pytest.fail("a quick change has no epic to pause it"))
    f = tmp_path / "c.json"
    f.write_text(json.dumps(quick_bundle()))
    assert sdlc.main(["slice-check", "12", "--from-file", str(f)]) == 0
    assert "#12: ok to start" in capsys.readouterr().out
    f.write_text(json.dumps(quick_bundle(comments=ready_quick("## Outcome\nonly this"))))
    assert sdlc.main(["slice-check", "12", "--from-file", str(f)]) == 1
    out = capsys.readouterr().out
    assert "lacks sections" in out and "not workable" in out
    f.write_text(json.dumps(quick_bundle([ROUTINE, "sdlc:in-progress"], ready_quick() + [CLAIM])))
    assert sdlc.main(["slice-check", "12", "--from-file", str(f), "--resume"]) == 0
    assert "#12: ok to resume" in capsys.readouterr().out


def test_complexity_rule_is_shared_with_the_transition():
    assert sdlc_quick.complexity_errors(12, [ROUTINE]) == []
    assert sdlc_quick.complexity_errors(12, [ROUTINE, "complexity:novel"])


@pytest.mark.parametrize("pr", [None, PR])
def test_a_brief_revised_after_the_claim_stops_the_build(tmp_path, capsys, pr):
    revised = ready_quick() + [CLAIM, quick_brief(ts=9, rev=2, body=BRIEF)]
    st = status(tmp_path, capsys, quick_bundle([ROUTINE, "sdlc:in-progress"], revised, pr=pr), "--json")[1]
    assert st["state"] == ("in-review" if pr else "claimed")
    assert any("the latest is rev 2" in e for e in st["refusals"])
    assert "--quick" not in st["next"] and st["next"].startswith("nothing for an agent")


def test_a_deferral_never_counts_for_a_quick_change(tmp_path, capsys):
    deferred = [RECORDS[0], marked("review-deferred", 8, sha=HEAD)]
    b = quick_bundle([ROUTINE, "sdlc:in-progress"], ready_quick() + [CLAIM], pr=PR, pr_comments=deferred)
    st = status(tmp_path, capsys, b, "--json")[1]
    assert st["deferred_on_head"] is False and st["review_on_head"] is None and st["review_mandatory"] is True
    out = status(tmp_path, capsys, b)[1]
    assert "Codex not yet" in out and "milestone" not in out.replace("never owed to a milestone", "")


@pytest.mark.parametrize("labels,brief,needle", [
    ([ROUTINE, "sdlc:approved"], "## Outcome\nonly this", "lacks sections"),
    (["sdlc:approved"], BRIEF, "complexity:routine"),
    ([ROUTINE, "complexity:novel", "sdlc:approved"], BRIEF, "complexity:novel"),
])
def test_a_refusal_triage_cannot_see_goes_back_to_triage(tmp_path, capsys, labels, brief, needle):
    b = quick_bundle(labels, ready_quick(brief))
    st = status(tmp_path, capsys, b, "--json")[1]
    assert st["state"] == "not-ready" and st["next"].startswith("/triage-issue 12: ") and needle in st["next"]
    out = status(tmp_path, capsys, b)[1]
    assert "refused: " in out and needle in out and "/work-slice" not in out


# --- the quick merge gate: config, denied paths, the diff rules, `quick-gate` ----------------

QUICK_RULES = sdlc.load_quick(CONFIG)


def test_load_quick_reads_the_defaults():
    q = sdlc.load_quick(sdlc.load_config())
    assert (q.max_files, q.max_lines, q.max_change_rounds) == (5, 200, 2)
    assert "tools/" in q.denied_paths and "src/twiddle/play.py" in q.denied_paths
    with pytest.raises(dataclasses.FrozenInstanceError):
        q.max_files = 9


def test_every_denied_twiddle_path_exists():
    # derived from CLAUDE.md's writing rows and layout table, and the code: what writes to a speaker (devices.soap is
    # the SOAP write path), Spotify or launchd, and where a key or a click is wired to a write (the TUIs' app.py,
    # dial's volume gauge, the visualizer screen's volume and mute keys)
    root = Path(__file__).resolve().parent.parent
    listed = [d for d in QUICK_RULES.denied_paths if d.startswith("src/twiddle/")]
    assert len(listed) > 15
    assert [d for d in listed if not (root / d).exists()] == []


def bad_quick(**over):
    q = {**CONFIG["quick"], **over}
    return {**CONFIG, "quick": {k: v for k, v in q.items() if v is not ...}}


@pytest.mark.parametrize("key", ["max_files", "max_lines", "max_change_rounds"])
@pytest.mark.parametrize("value", [-1, 0, True, 2.0, "5", None, ...])
def test_a_bad_quick_number_names_its_key(key, value):
    with pytest.raises(sdlc.SdlcError, match=f"quick.{key}"):
        sdlc.load_quick(bad_quick(**{key: value}))


@pytest.mark.parametrize("value", [[], "tools/", None, ..., ["/etc/passwd"], ["tools/", ""], ["../x"], ["a/../b"], [3],
                                   ["./tools/"], ["tools//x"], [" tools/"], ["tools/ "], ["/"]])
def test_bad_denied_paths_name_the_key(value):
    with pytest.raises(sdlc.SdlcError, match="quick.denied_paths"):
        sdlc.load_quick(bad_quick(denied_paths=value))


def test_no_quick_section_is_refused():
    with pytest.raises(sdlc.SdlcError, match="quick"):
        sdlc.load_quick({k: v for k, v in CONFIG.items() if k != "quick"})


@pytest.mark.parametrize("path,denied", [
    ("tools/sdlc.py", True), (".agents/skills/x/SKILL.md", True), ("uv.lock", True), ("src/twiddle/play.py", True),
    ("src/twiddle/alarms/clock.py", True), ("SDLC.md", True),
    ("src/twiddle/stations/catalog/psyched.toml", False), ("tests/test_stations.py", False),
    ("toolsx/a.py", False), ("uv.lock.bak", False), ("src/twiddle/play.pyc", False), ("docs/SDLC.md", False),
])
def test_path_denied(path, denied):
    assert sdlc_quick.path_denied(path, QUICK_RULES) is denied


def test_a_denied_directory_without_its_slash_still_matches_only_under_it():
    q = sdlc.load_quick(bad_quick(denied_paths=["tools"]))
    assert sdlc_quick.path_denied("tools/sdlc.py", q) and not sdlc_quick.path_denied("toolsx/a.py", q)


GATE_BRIEF = BRIEF.replace("## Files\nfiles of the change",
                           "## Files\n- `src/twiddle/stations/catalog/*.toml`: the new station\n"
                           "- tests/test_stations.py\n* `docs/STATIONS.md` (its row)")
STATION = "src/twiddle/stations/catalog/psyched.toml"


def changed(name, adds=10, dels=0, previous=None, status="added"):
    return {"filename": name, "status": "renamed" if previous else status, "additions": adds, "deletions": dels,
            **({"previous_filename": previous} if previous else {})}


def gate_bundle(labels=(ROUTINE, "sdlc:in-progress"), comments=None, pr=None, records=None, files=None,
                behind_by=0, brief_of=None, **issue_kw):
    """A quick PR that passes every rule; each test breaks one thing. `records` may be a function of the
    approved brief's comment id (the ids are fresh each call)."""
    comments = ready_quick(GATE_BRIEF) + [CLAIM] if comments is None else comments
    st = sdlc.derive_state(issue(list(labels)), comments, TRUSTED, CONFIG)
    brief = sdlc.approved_brief_id(st) if brief_of is None else brief_of
    pr = {"number": 50, "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefName": "slice/12",
          "headRefOid": HEAD, "mergeable": "MERGEABLE", "body": "Adds a station.\n\nCloses #12", **(pr or {})}
    if callable(records):
        records = records(sdlc.approved_brief_id(st))
    elif records is None:
        records = [marked("tests", 7, sha=HEAD, result="pass", passed="1300"),
                   marked("pr-review", 8, sha=HEAD, verdict="approve", round="1", **({"brief": brief} if brief else {}))]
    return {"pr": pr, "pr_comments": records, "trusted": [OWNER], "behind_by": behind_by,
            "files": [changed(STATION), changed("tests/test_stations.py", 5, 1)] if files is None else files,
            "issue": {**issue(list(labels)), **issue_kw}, "comments": comments}


def gate(b, config=CONFIG):
    return sdlc.quick_gate_errors(b, config)


def refused(b, needle, config=CONFIG):
    errs = gate(b, config)
    assert any(needle in e for e in errs), errs
    return errs


def test_a_good_quick_pr_passes_the_gate():
    assert gate(gate_bundle()) == []


@pytest.mark.parametrize("case,needle", [
    ("merged", "PR is MERGED, not OPEN"),
    ("draft", "PR is a draft"),
    ("epic-base", "PR targets epic/12, not main"),
    ("closes-none", "it closes none"),
    ("closes-two", "it closes [12, 13]"),
    ("unread", "#12 could not be read"),
    ("closed-issue", "#12 is closed"),
    ("slice-issue", "is a plan:slice issue"),
    ("no-review", "no Codex review of the current head"),
    ("old-review", "no Codex review of the current head"),
    ("changes", "asks for changes"),
    ("no-tests", "no recorded test run"),
    ("failed", "the tests failed"),
    ("old-tests", "no recorded test run"),
    ("behind", "1 commit(s) behind main"),
    ("unknown-behind", "could not tell whether the head is up to date with main"),
    ("conflicts", "GitHub says the PR conflicts (CONFLICTING)"),
    ("computing", "still computing mergeability"),
])
def test_each_gate_rule_refuses_with_its_own_message(case, needle):
    tests = marked("tests", 7, sha=HEAD, result="pass", passed="1300")
    approve = lambda sha: lambda brief: marked("pr-review", 8, sha=sha, verdict="approve", round="1", brief=brief)  # noqa: E731
    withr = lambda *rs: lambda brief: [r(brief) if callable(r) else r for r in rs]   # noqa: E731
    b = {"merged": lambda: gate_bundle(pr={"state": "MERGED"}),
         "draft": lambda: gate_bundle(pr={"isDraft": True}),
         "epic-base": lambda: gate_bundle(pr={"baseRefName": "epic/12"}),
         "closes-none": lambda: gate_bundle(pr={"body": "no keyword"}),
         "closes-two": lambda: gate_bundle(pr={"body": "Closes #12, closes #13"}),
         "unread": lambda: {**gate_bundle(), "issue": None},
         "closed-issue": lambda: gate_bundle(state="closed"),
         "slice-issue": lambda: gate_bundle(labels=["plan:slice", ROUTINE, "sdlc:in-progress"]),
         "no-review": lambda: gate_bundle(records=[tests]),
         "old-review": lambda: gate_bundle(records=withr(tests, approve("b" * 40))),
         "changes": lambda: gate_bundle(records=withr(tests, approve(HEAD),
                                                 marked("pr-review", 9, sha=HEAD, verdict="changes", round="2"))),
         "no-tests": lambda: gate_bundle(records=withr(approve(HEAD))),
         "failed": lambda: gate_bundle(records=withr(marked("tests", 7, sha=HEAD, result="fail"), approve(HEAD))),
         "old-tests": lambda: gate_bundle(records=withr(marked("tests", 7, sha="b" * 40, result="pass"), approve(HEAD))),
         "behind": lambda: gate_bundle(behind_by=1),
         "unknown-behind": lambda: gate_bundle(behind_by=None),
         "conflicts": lambda: gate_bundle(pr={"mergeable": "CONFLICTING"}),
         "computing": lambda: gate_bundle(pr={"mergeable": "UNKNOWN"}),
         }[case]()
    errs = refused(b, needle)
    if case not in ("unread", "closes-none", "closes-two", "slice-issue"):   # those also lose what hangs off the issue
        assert len(errs) == 1, errs


@pytest.mark.parametrize("case,needle", [
    ("revised", "the latest is rev 2"),
    ("unrated", "complexity:routine"),
    ("rerated", "complexity:judgment"),
    ("escalated", "escalated to a human"),
    ("conflict", "needs a human first"),
    ("not-in-progress", "is approved, not in-progress"),
    ("full-lane", "is not a quick brief"),
])
def test_what_changed_after_the_claim_is_refused_at_merge(case, needle):
    revised = ready_quick(GATE_BRIEF) + [CLAIM, quick_brief(ts=9, rev=2, body=GATE_BRIEF)]
    b = {"revised": lambda: gate_bundle(labels=[ROUTINE, "sdlc:prd-review"], comments=revised),
         "unrated": lambda: gate_bundle(labels=["sdlc:in-progress"]),
         "rerated": lambda: gate_bundle(labels=["complexity:judgment", "sdlc:in-progress"]),
         "escalated": lambda: gate_bundle(labels=[ROUTINE, "sdlc:escalated"]),
         # two state labels at once is a conflict derive_state reports
         "conflict": lambda: gate_bundle(labels=[ROUTINE, "sdlc:in-progress", "sdlc:approved"]),
         "not-in-progress": lambda: gate_bundle(labels=[ROUTINE, "sdlc:approved"]),
         "full-lane": lambda: gate_bundle(comments=[agent("prd", 1, 1, GATE_BRIEF), agent("approval", 1, 2, by=POSTER),
                                                    CLAIM]),
         }[case]()
    refused(b, needle)


def test_a_denied_path_is_refused_even_with_a_pass_and_an_approval():
    for path in ("src/twiddle/play.py", "tools/sdlc.py", ".sdlc/config.json", "src/twiddle/viz/screen.py",
                 "src/twiddle/dial/gauge.py"):
        brief = GATE_BRIEF.replace("- tests/test_stations.py", f"- tests/test_stations.py\n- `{path}`")
        b = gate_bundle(comments=ready_quick(brief) + [CLAIM], files=[changed(STATION), changed(path, status="modified")])
        errs = gate(b)
        assert len(errs) == 1 and path in errs[0] and errs[0].endswith("`escalate-to-full 12`"), errs


@pytest.mark.parametrize("path", [
    "src/twiddle/spotify.py", "src/twiddle/devices.py", "src/twiddle/play.py", "src/twiddle/alarms/clock.py",
    "src/twiddle/scene/app.py", "src/twiddle/dial/app.py",
])
def test_each_speaker_writing_module_is_denied_to_a_quick_change(path):
    # the gate reads the real .sdlc/config.json (CONFIG), so dropping one of these from `quick.denied_paths` fails here;
    # alarms/clock.py is denied by its directory entry
    brief = GATE_BRIEF.replace("- tests/test_stations.py", f"- tests/test_stations.py\n- `{path}`")
    b = gate_bundle(comments=ready_quick(brief) + [CLAIM], files=[changed(STATION), changed(path, status="modified")])
    errs = gate(b)
    assert len(errs) == 1 and path in errs[0] and errs[0].endswith("`escalate-to-full 12`"), errs
    assert sdlc_quick.path_denied(path, QUICK_RULES)


@pytest.mark.parametrize("files,ok", [
    ([changed(f"src/twiddle/stations/catalog/s{i}.toml", 40) for i in range(5)], True),            # 5 files, 200 lines
    ([changed(f"src/twiddle/stations/catalog/s{i}.toml", 20, 20) for i in range(5)], True),        # deletions count too
    ([changed(f"src/twiddle/stations/catalog/s{i}.toml", 1) for i in range(6)], False),            # 6 files
    ([changed(STATION, 150, 51)], False),                                                         # 201 lines
])
def test_the_size_cap(files, ok):
    errs = gate(gate_bundle(files=files))
    assert (errs == []) is ok, errs
    if not ok:
        assert len(errs) == 1 and "more than the quick lane's" in errs[0] and "escalate-to-full 12" in errs[0]


def test_the_size_cap_comes_from_the_config():
    tight = bad_quick(max_files=1, max_lines=10)
    refused(gate_bundle(), "changes 2 files, more than the quick lane's 1", tight)
    refused(gate_bundle(files=[changed(STATION, 11)]), "changes 11 lines, more than the quick lane's 10", tight)
    assert gate(gate_bundle(files=[changed(STATION, 10)]), tight) == []


def test_a_file_outside_the_brief_is_refused():
    errs = refused(gate_bundle(files=[changed(STATION), changed("src/twiddle/stations/model.py", status="modified")]),
                   "outside the brief's `## Files` list (src/twiddle/stations/model.py)")
    assert len(errs) == 1 and errs[0].endswith("`escalate-to-full 12`")
    assert sdlc_quick.brief_files(GATE_BRIEF) == ["src/twiddle/stations/catalog/*.toml", "tests/test_stations.py",
                                                  "docs/STATIONS.md"]


def test_a_brief_without_a_files_list_allows_nothing():
    b = gate_bundle(comments=ready_quick(GATE_BRIEF.replace("- `src", "the station: `src")) + [CLAIM])
    assert sdlc_quick.brief_files(sdlc.approved_brief(sdlc.derive_state(
        issue([ROUTINE]), b["comments"], TRUSTED, CONFIG))) == ["tests/test_stations.py", "docs/STATIONS.md"]
    refused(b, f"outside the brief's `## Files` list ({STATION})")


@pytest.mark.parametrize("files,needle", [
    ([changed(STATION, previous="src/twiddle/play.py")], "may not (src/twiddle/play.py)"),       # out of a denied place
    ([changed(STATION, previous="src/twiddle/stations/old.toml")], "list (src/twiddle/stations/old.toml)"),
    ([{**changed(STATION, previous="tools/x.toml"), "status": "copied"}], "may not (tools/x.toml)"),
])
def test_a_move_is_read_at_both_ends(files, needle):
    refused(gate_bundle(files=files), needle)


@pytest.mark.parametrize("files", [[], ...])
def test_an_unread_diff_never_passes(files):
    b = gate_bundle()
    b = {**b, "files": files} if files is not ... else {k: v for k, v in b.items() if k != "files"}
    errs = refused(b, "could not read the PR's changed files")
    assert len(errs) == 1, errs


def test_a_plain_add_in_the_allowed_directory_passes():
    assert gate(gate_bundle(files=[changed("src/twiddle/stations/catalog/new.toml")])) == []
    # a rename inside the list passes too
    assert gate(gate_bundle(files=[changed(STATION, previous="src/twiddle/stations/catalog/old.toml")])) == []


def test_a_removed_denied_file_is_still_a_change():
    brief = GATE_BRIEF.replace("- tests/test_stations.py", "- tests/test_stations.py\n- `tools/old.py`")
    refused(gate_bundle(comments=ready_quick(brief) + [CLAIM], files=[changed("tools/old.py", 0, 30, status="removed")]),
            "may not (tools/old.py)")


def test_too_many_change_rounds_go_to_the_full_lane():
    b = gate_bundle()
    changes = [marked("pr-review", 3 + i, sha="c" * 40, verdict="changes", round=str(i + 1)) for i in range(3)]
    two = {**b, "pr_comments": changes[:2] + b["pr_comments"]}
    assert gate(two) == []                                                    # max_change_rounds 2: still allowed
    errs = refused({**b, "pr_comments": changes + b["pr_comments"]}, "Codex asked for changes in 3 rounds")
    assert len(errs) == 1 and errs[0].endswith("`escalate-to-full 12`")


def test_a_deferral_is_never_a_quick_review():
    b = gate_bundle(records=[marked("tests", 7, sha=HEAD, result="pass"), marked("review-deferred", 8, sha=HEAD)])
    errs = refused(b, "no Codex review of the current head")
    assert len(errs) == 1 and "review-defer" not in errs[0]


def test_a_review_is_bound_to_the_brief_it_read():
    # recorded against another brief comment, at the same head: refused
    other = gate_bundle(brief_of="999")
    refused(other, "recorded against brief 999, not the approved brief "
            + sdlc.approved_brief_id(sdlc.quick_issue_state(other, CONFIG)))
    # recorded with no brief at all (a record written before reviews named their brief): refused
    refused(gate_bundle(brief_of=""), "recorded against brief none")
    # the brief revised and re-approved: rev 2 is the approved brief, so the review of rev 1 at head H no longer counts
    rev2 = ready_quick(GATE_BRIEF) + [quick_brief(ts=3, rev=2, body=GATE_BRIEF), agent("approval", 2, 4, by=POSTER),
                                      CLAIM]
    rev1_id = sdlc.comment_anchor({"url": rev2[0]["url"], "id": rev2[0]["id"]})
    b = gate_bundle(comments=rev2, brief_of=rev1_id)
    rev2_id = sdlc.approved_brief_id(sdlc.quick_issue_state(b, CONFIG))
    assert rev2_id != rev1_id
    refused(b, f"recorded against brief {rev1_id}, not the approved brief {rev2_id}")
    # ...until a Codex round against rev 2 is recorded on the same head H
    b["pr_comments"].append(marked("pr-review", 20, sha=HEAD, verdict="approve", round="2", brief=rev2_id))
    assert gate(b) == []


def gate_cli(tmp_path, capsys, monkeypatch, b):
    for name in ("gh", "gh_json", "gh_pages", "post_comment", "patch_comment", "fetch_trusted"):
        monkeypatch.setattr(sdlc, name, lambda *a, **k: pytest.fail("quick-gate --from-file reads nothing else"))
    f = tmp_path / "g.json"
    f.write_text(json.dumps(b))
    code = sdlc.main(["quick-gate", "50", "--from-file", str(f)])
    return code, capsys.readouterr().out


def test_quick_gate_prints_ok(tmp_path, capsys, monkeypatch):
    assert gate_cli(tmp_path, capsys, monkeypatch, gate_bundle()) == (0, "ok\n")


def test_quick_gate_prints_every_refusal(tmp_path, capsys, monkeypatch):
    files = [changed(f"src/twiddle/stations/catalog/s{i}.toml") for i in range(5)] + [changed("src/twiddle/play.py")]
    code, out = gate_cli(tmp_path, capsys, monkeypatch, gate_bundle(files=files, pr={"isDraft": True}))
    lines = out.splitlines()
    assert code == 1 and all(l.startswith("refused: ") for l in lines) and "ok" not in lines
    assert any("draft" in l for l in lines) and any("may not (src/twiddle/play.py)" in l for l in lines)
    assert any("6 files" in l for l in lines) and any("outside the brief" in l for l in lines)


def test_slice_status_shows_a_quick_change_s_rounds(tmp_path, capsys):
    b = quick_bundle([ROUTINE, "sdlc:in-progress"], ready_quick() + [CLAIM], pr=PR,
                     pr_comments=RECORDS + [marked("pr-review", 9, sha="b" * 40, verdict="changes", round="2")])
    out = status(tmp_path, capsys, b)[1]
    assert "change rounds 1/2" in out and "rounds 1/5" not in out


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "quick_gate"


@pytest.mark.parametrize("name,code,needles", [
    ("ok", 0, ["ok"]),
    ("denied_path", 1, ["may not (src/twiddle/play.py)"]),
    ("sixth_file", 1, ["changes 6 files, more than the quick lane's 5"]),
    ("unlisted_file", 1, ["outside the brief's `## Files` list (src/twiddle/stations/model.py)"]),
])
def test_the_demo_fixtures(capsys, monkeypatch, name, code, needles):
    # the milestone demo runs `quick-gate 50 --from-file tests/fixtures/quick_gate/<name>.json` on these
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("quick-gate --from-file reads nothing else"))
    assert sdlc.main(["quick-gate", "50", "--from-file", str(FIXTURES / f"{name}.json")]) == code
    out = capsys.readouterr().out
    assert all(n in out for n in needles), out


# --- merge for a quick PR: squash into main, close, sdlc:done, one record ----------------------

class MergeGitHub:
    """`gh` faked at the module boundary: every call in order, the posts among them, and the issue's read-backs."""

    def __init__(self, monkeypatch, b, reads=("open", "closed"), first_head=HEAD, fail_on=None):
        self.calls, self.b, self.fail_on = [], b, fail_on
        states = iter(reads)
        routed = {**b["issue"], "state": "open"}
        self.reads = 0

        def fetch_slice(n, cfg):
            self.reads += 1
            if self.reads == 1:   # merge's routing read: the issue and its comments
                return routed, b["comments"]
            st = next(states)
            labels = [l for l in b["issue"]["labels"]]
            return {**b["issue"], "state": st, "state_reason": "completed" if st == "closed" else None,
                    "labels": labels}, []

        monkeypatch.setattr(sdlc, "repo_of", lambda cfg: "o/r")
        monkeypatch.setattr(sdlc, "fetch_trusted", lambda cfg: set(b["trusted"]))
        monkeypatch.setattr(sdlc, "fetch_pr", lambda n, cfg: ({**b["pr"], "headRefOid": first_head}, b["pr_comments"]))
        monkeypatch.setattr(sdlc, "fetch_slice", fetch_slice)
        monkeypatch.setattr(sdlc, "quick_gate_bundle", lambda n, cfg: b)
        monkeypatch.setattr(sdlc, "gh", self.gh)
        monkeypatch.setattr(sdlc, "post_comment", lambda n, body, cfg: self.calls.append(["post", n, body]) or {})

    def gh(self, args, check=True):
        self.calls.append(args)
        if self.fail_on and args[:2] == self.fail_on:
            raise sdlc.SdlcError(f"gh {' '.join(args[:3])} failed: boom")
        return ""

    def kinds(self):
        return [c[0] if c[0] == "post" else " ".join(c[:2]) for c in self.calls]


def run_merge(capsys, *flags):
    code = sdlc.main(["merge", "50", *flags])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_merge_dry_run_on_a_quick_pr_names_the_pr_issue_and_head(monkeypatch, capsys):
    gh = MergeGitHub(monkeypatch, gate_bundle())
    code, out, _ = run_merge(capsys, "--dry-run")
    assert code == 0 and gh.calls == []
    assert f"would squash-merge quick PR #50 (closes #12) into main at {HEAD[:12]}" in out
    assert "kind=quick-merged pr=50 sha=" + HEAD in out and "epic" not in out


def test_merge_dry_run_on_a_failing_quick_pr_lists_every_refusal(monkeypatch, capsys):
    b = gate_bundle(pr={"isDraft": True}, files=[changed(STATION), changed("src/twiddle/play.py")])
    gh = MergeGitHub(monkeypatch, b)
    code, _, err = run_merge(capsys, "--dry-run")
    assert code == 1 and gh.calls == []
    assert "PR is a draft" in err and "may not (src/twiddle/play.py)" in err


def test_a_quick_merge_squashes_closes_labels_and_records_in_that_order(tmp_path, monkeypatch, capsys):
    b = gate_bundle()
    gh = MergeGitHub(monkeypatch, b, reads=("open", "closed"))
    code, out, _ = run_merge(capsys)
    assert code == 0, out
    assert gh.kinds() == ["pr merge", "api -X", "issue close", "issue edit", "post"]
    merge = gh.calls[0]
    assert merge == ["pr", "merge", "50", "--repo", "o/r", "--squash", "--match-head-commit", HEAD]
    edit = gh.calls[3]
    assert edit[edit.index("sdlc:in-progress") - 1] == "--remove-label" and "sdlc:done" in edit
    posts = [c for c in gh.calls if c[0] == "post"]
    assert len(posts) == 1 and posts[0][1] == 12
    record = posts[0][2]
    tests_url, review_url = (c["url"] for c in b["pr_comments"])
    assert f"kind=quick-merged pr=50 sha={HEAD}" in record and tests_url in record and review_url in record
    assert "into main; #12 closed and sdlc:done" in out
    # what `state 12` then reads: closed, done, the record posted, and nothing left to do
    after = {"issue": {**b["issue"], "state": "closed", "labels": [ROUTINE, "sdlc:done"]},
             "comments": b["comments"] + [comment(OWNER, record, 30)], "trusted": [OWNER]}
    f = tmp_path / "after.json"
    f.write_text(json.dumps(after))
    assert sdlc.main(["state", "--from-file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "state: done   lane: quick   turn: none   action: none" in out and "next: nothing" in out


def test_a_quick_issue_github_closed_itself_is_not_closed_twice(monkeypatch, capsys):
    gh = MergeGitHub(monkeypatch, gate_bundle(), reads=("closed",))
    assert run_merge(capsys)[0] == 0
    assert gh.kinds() == ["pr merge", "api -X", "issue edit", "post"]


def test_a_quick_issue_that_will_not_close_fails_loudly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    gh = MergeGitHub(monkeypatch, gate_bundle(), reads=("open", "open"))
    code, _, err = run_merge(capsys)
    assert code == 1 and "reads back open" in err and "gh issue close 12 --reason completed" in err
    assert gh.kinds() == ["pr merge", "api -X", "issue close"]   # no label move, no record
    assert "merged PR #50 into main" in err and "gh issue comment 12 --body-file" in err


def test_a_failed_label_move_after_the_squash_says_what_is_left(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    gh = MergeGitHub(monkeypatch, gate_bundle(), reads=("open", "closed"), fail_on=["issue", "edit"])
    code, _, err = run_merge(capsys)
    assert code == 1 and gh.kinds() == ["pr merge", "api -X", "issue close", "issue edit"]   # closed, nothing posted
    assert f"merged PR #50 into main at {HEAD[:12]}" in err
    assert "--add-label sdlc:done --remove-label sdlc:in-progress" in err
    path = err.split("--body-file ")[1].split("`")[0]
    assert Path(path).parent == tmp_path and "kind=quick-merged" in Path(path).read_text()


def test_a_bad_read_back_after_the_squash_still_says_what_is_left(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    gh = MergeGitHub(monkeypatch, gate_bundle())
    reads = []

    def fetch_slice(n, cfg):   # the routing read works; the read-back after the squash gets malformed JSON
        reads.append(n)
        if len(reads) > 1:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return gh.b["issue"], gh.b["comments"]
    monkeypatch.setattr(sdlc, "fetch_slice", fetch_slice)
    code, _, err = run_merge(capsys)
    assert code == 1 and gh.kinds() == ["pr merge", "api -X"]
    assert f"merged PR #50 into main at {HEAD[:12]}" in err and "gh issue close 12 --reason completed" in err
    assert "--add-label sdlc:done" in err and "gh issue comment 12 --body-file" in err


def test_merge_squashes_the_head_the_gate_checked(monkeypatch, capsys):
    gh = MergeGitHub(monkeypatch, gate_bundle(), first_head="d" * 40)   # merge's first read saw an older head
    assert run_merge(capsys)[0] == 0
    assert gh.calls[0][-2:] == ["--match-head-commit", HEAD]


def test_a_quick_merge_never_looks_up_an_epic(monkeypatch, capsys):
    MergeGitHub(monkeypatch, gate_bundle())
    for name in ("epic_progress", "milestone_hold_errors", "with_next"):
        monkeypatch.setattr(sdlc, name, lambda *a, **k: pytest.fail(f"a quick merge has no epic ({name})"))
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda *a, **k: pytest.fail("no epic bundle"))
    assert run_merge(capsys)[0] == 0


def test_review_defer_refuses_a_quick_pr_and_names_the_options(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("refused before anything is posted"))
    b = gate_bundle(records=lambda brief: [marked("tests", 7, sha=HEAD, result="pass")])
    f = tmp_path / "d.json"
    f.write_text(json.dumps({"pr": b["pr"], "pr_comments": b["pr_comments"], "trusted": [OWNER],
                             "slice": b["issue"], "comments": b["comments"]}))
    assert sdlc.main(["review-defer", "50", "--reason", "codex: not found", "--from-file", str(f), "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "never deferred" in err and "stays open" in err and "escalate-to-full 12" in err


@pytest.mark.parametrize("dry", [False, True])
def test_escalate_slice_on_a_quick_issue_leaves_one_state_label(monkeypatch, capsys, dry):
    b = gate_bundle()
    calls = []
    monkeypatch.setattr(sdlc, "repo_of", lambda cfg: "o/r")
    monkeypatch.setattr(sdlc, "fetch_trusted", lambda cfg: TRUSTED)
    monkeypatch.setattr(sdlc, "fetch_slice", lambda n, cfg: (b["issue"], b["comments"]))
    monkeypatch.setattr(sdlc, "gh", lambda args, **k: calls.append(args) or "")
    monkeypatch.setattr(sdlc, "post_comment", lambda n, body, cfg: calls.append(["post"]) or {})
    assert sdlc.main(["escalate-slice", "12", "--reason", "stuck", *(["--dry-run"] if dry else [])]) == 0
    if dry:
        assert calls == [] and "would move #12 to sdlc:escalated (from sdlc:in-progress)" in capsys.readouterr().out
        return
    (edit,) = [c for c in calls if c[:2] == ["issue", "edit"]]
    labels = set(b["issue"]["labels"]) - {edit[i + 1] for i, a in enumerate(edit) if a == "--remove-label"}
    labels |= {edit[i + 1] for i, a in enumerate(edit) if a == "--add-label"}
    assert [l for l in labels if l.startswith("sdlc:")] == ["sdlc:escalated"]


def test_escalate_slice_on_a_slice_only_adds_the_label(monkeypatch, capsys):
    calls = []
    slice_issue = {**issue(["plan:slice", "complexity:judgment"]), "body": ""}
    monkeypatch.setattr(sdlc, "repo_of", lambda cfg: "o/r")
    monkeypatch.setattr(sdlc, "fetch_trusted", lambda cfg: TRUSTED)
    monkeypatch.setattr(sdlc, "fetch_slice", lambda n, cfg: (slice_issue, []))
    monkeypatch.setattr(sdlc, "gh", lambda args, **k: calls.append(args) or "")
    monkeypatch.setattr(sdlc, "post_comment", lambda n, body, cfg: {})
    assert sdlc.main(["escalate-slice", "12", "--reason", "stuck"]) == 0
    assert calls == [["issue", "edit", "12", "--repo", "o/r", "--add-label", "sdlc:escalated"]]


# --- a brief that moved to the full lane: `commit-prd`, `merge-prd`, then planning -------------------------

MOVED_BRIEF = ("## Outcome\nthe station plays\n\n## Files\n- `src/twiddle/stations/catalog/x.toml`\n\n"
         "## Acceptance criteria\n1. it is listed\n2. it plays\n\n## Validation\npytest\n")


def escalated(rev2=False, lane=True):
    """An approved quick brief (and, with `rev2`, a second approved revision) moved to the full lane by hand."""
    out = [quick_brief(1, body=MOVED_BRIEF), comment(POSTER, "/approve", 2), agent("approval", 1, 3, by=POSTER, via="comment")]
    if rev2:
        out += [quick_brief(4, rev=2, body=MOVED_BRIEF + "\nrevised\n"), comment(POSTER, "/approve", 5),
                agent("approval", 2, 6, by=POSTER, via="comment")]
    return out + ([agent("lane", None, 7, "Moved to the full lane: Codex asked for changes 3 times.", lane="full")]
                  if lane else [])


def anchor(c):
    return c["url"].rsplit("-", 1)[1]


ESCALATED_LABELS = [ROUTINE, "sdlc:approved", "lane:quick"]


def test_a_brief_moved_to_the_full_lane_is_committed_before_it_is_planned():
    st = derive(ESCALATED_LABELS, escalated())
    assert (st["action"], st["turn"], st["lane"]) == ("commit_prd", "agent", None)
    assert not sdlc.is_quick(st)            # never a quick change again: no quick claim, gate or merge
    assert sdlc.next_command(st) == "`uv run python tools/sdlc.py commit-prd 12`"
    assert sdlc_quick.quick_check_errors(issue(ESCALATED_LABELS), st, sdlc.approved_brief(st), None)
    done = derive(ESCALATED_LABELS, escalated() + [agent("prd-merged", None, 9, comment="1")])
    assert (done["action"], sdlc.next_command(done)) == ("plan", "/plan-issue 12")


def test_a_lane_record_counts_only_on_an_approved_quick_brief():
    full_prd = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER, via="comment"), agent("lane", None, 3, lane="full")]
    assert derive(["sdlc:approved"], full_prd)["action"] == "plan"          # never quick: nothing to commit
    unapproved = [quick_brief(1), agent("lane", None, 2, lane="full")]
    assert derive([ROUTINE, "sdlc:prd-review"], unapproved)["to_full"] is None
    # a prd-merged before any escalation means nothing
    early = [agent("prd-merged", None, 1)] + escalated()[1:]
    assert derive(ESCALATED_LABELS, [quick_brief(0, body=MOVED_BRIEF)] + early)["action"] == "commit_prd"


def test_a_new_revision_after_the_escalation_is_what_gets_approved_next():
    st = derive([ROUTINE, "sdlc:prd-review"], escalated() + [agent("prd", 2, 8, "## Outcome\nbigger")])
    assert st["to_full"] is None and st["action"] == "wait_for_poster"


def test_an_escalated_brief_still_labelled_in_progress_needs_a_human():
    st = derive([ROUTINE, "sdlc:in-progress", "lane:quick"], escalated())
    assert st["action"] == "fix_conflict" and any("docs/prd/" in c for c in st["conflicts"])


class PrdGitHub:
    """`gh` for commit-prd / merge-prd over a scratch repo's bare origin: PRs are branches there, and a merge
    moves origin's main to the PR's head. Every call is kept; `fail` names a step that fails once."""

    def __init__(self, monkeypatch, root, comments, labels=ESCALATED_LABELS):
        self.root, self.origin = root, root.parent / "origin.git"
        self.issue, self.comments, self.calls, self.posted = issue(labels), list(comments), [], []
        self.prs, self.fail, self.after_create = {}, set(), []
        monkeypatch.setattr(sdlc, "repo_of", lambda config: "o/r")
        monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, config, trusted=None: self.bundle())
        monkeypatch.setattr(sdlc, "post_comment", self.post)
        monkeypatch.setattr(sdlc, "gh", self.gh)
        monkeypatch.setattr(sdlc, "gh_json", self.gh_json)
        monkeypatch.setattr(sdlc.time, "sleep", lambda s: pytest.fail("a fake PR is mergeable at once"))

    def bundle(self):
        return {"issue": self.issue, "comments": list(self.comments), "trusted": [OWNER]}

    def state(self):
        return sdlc.bundle_state(self.bundle(), CONFIG)

    def post(self, n, body, config):
        if "post" in self.fail:
            self.fail.discard("post")
            raise sdlc.SdlcError("network down")
        self.posted.append(body)
        self.comments.append(comment(OWNER, body, 40 + len(self.posted)))
        return {"html_url": self.comments[-1]["url"]}

    def sha(self, ref):
        return subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{ref}"], cwd=self.origin,
                              capture_output=True, text=True).stdout.strip()

    def open_pr(self, branch, files):
        """A PR someone else opened: `files` committed on `branch` from main."""
        wt = self.root.parent / f"wt-{branch.replace('/', '-')}"
        _git("worktree", "add", "-q", "--detach", str(wt), "origin/main", cwd=self.root)
        for path, text in files.items():
            (wt / path).parent.mkdir(parents=True, exist_ok=True)
            (wt / path).write_text(text)
        _git("add", ".", cwd=wt)
        _git("commit", "-q", "-m", "docs", cwd=wt)
        _git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=wt)
        _git("worktree", "remove", "--force", str(wt), cwd=self.root)
        number = 100 + len(self.prs)
        self.prs[number] = {"head": branch, "state": "OPEN"}
        return number

    def gh(self, args, **k):
        self.calls.append(args)
        if args[:2] == ["pr", "create"]:
            number = 100 + len(self.prs)
            self.prs[number] = {"head": args[args.index("--head") + 1], "state": "OPEN"}
            for more in self.after_create:     # what someone posts on the issue while the PR is being made
                self.comments.append(more)
            return f"https://github.com/o/r/pull/{number}\n"
        if args[:2] == ["pr", "merge"]:
            if "merge" in self.fail:
                self.fail.discard("merge")
                raise sdlc.SdlcError("gh pr merge failed: GitHub is down")
            pr = self.prs[int(args[2])]
            sha = self.sha(pr["head"])
            assert args[args.index("--match-head-commit") + 1] == sha
            _git("update-ref", "refs/heads/main", sha, cwd=self.origin)
            _git("update-ref", "-d", f"refs/heads/{pr['head']}", cwd=self.origin)
            pr["state"] = "MERGED"
            return ""
        pytest.fail(f"unexpected gh {args}")

    def gh_json(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "list"]:
            head, state = args[args.index("--head") + 1], args[args.index("--state") + 1]
            return [{"number": n, "url": f"u/{n}"} for n, pr in self.prs.items()
                    if pr["head"] == head and pr["state"].lower() == state]
        if args[:2] == ["pr", "view"]:
            pr = self.prs[int(args[2])]
            if args[-1] == "mergeable":
                return {"mergeable": "MERGEABLE"}
            files = _git("diff", "--name-only", f"main...{pr['head']}", cwd=self.origin).split()
            return {"state": pr["state"], "mergeable": "MERGEABLE", "baseRefName": "main", "headRefName": pr["head"],
                    "headRefOid": self.sha(pr["head"]), "files": [{"path": p} for p in files]}
        pytest.fail(f"unexpected gh_json {args}")

    def on_main(self):
        _git("fetch", "-q", "origin", "main", cwd=self.root)
        return [p for p in _git("ls-tree", "--name-only", "origin/main", "docs/prd/", cwd=self.root).split() if p]

    def created(self):
        return [c for c in self.calls if c[:2] == ["pr", "create"]]


def plan_for_12():
    leaf = {"key": "T1", "title": "do it", "blocked_by": [], "covers": [1, 2], "outcome": "o", "scope": "s",
            "acceptance": ["a"], "validation": "v", "demo": "d", "non_goals": "n", "context": "c",
            "complexity": "routine", "complexity_reason": "small", "milestone": "M1"}
    return {"issue": 12, "kind": "feature", "milestones": [{"key": "M1", "title": "it", "demo": "d"}],
            "subtasks": [leaf]}


def test_commit_prd_merges_the_brief_and_planning_starts(repo, monkeypatch, capsys, tmp_path):
    fake = PrdGitHub(monkeypatch, repo, escalated())
    assert sdlc.main(["commit-prd", "12"]) == 0
    assert len(fake.created()) == 1 and "Closes" not in json.dumps(fake.calls)   # main must never close the issue
    assert fake.on_main() == ["docs/prd/12-add-a-station.md"]
    text = _git("show", "origin/main:docs/prd/12-add-a-station.md", cwd=repo)
    brief = fake.comments[0]
    assert sdlc.header_links(text, anchor(brief))
    assert "[sign-off](" in text and "[approval record](" in text and "moved to the full lane" in text
    assert "## Acceptance criteria\n1. it is listed" in text and "🤖" not in text and "/approve" not in text
    assert len(fake.posted) == 1 and f"comment={anchor(brief)}" in fake.posted[0].splitlines()[0]
    st = fake.state()
    assert (st["action"], sdlc.next_command(st)) == ("plan", "/plan-issue 12")
    capsys.readouterr()
    assert sdlc.main(["state", "12"]) == 0
    out = capsys.readouterr().out
    assert "action: plan" in out and "next: /plan-issue 12" in out
    # planning finds it: the plan prompt's requirements, and plan-validate's criteria
    assert sdlc.approved_requirements(12, anchor(brief), "", "origin/main", repo)[1] == Path("docs/prd/12-add-a-station.md")
    _git("merge", "-q", "--ff-only", "origin/main", cwd=repo)
    monkeypatch.setattr(sdlc, "ROOT", repo)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_for_12()))
    assert sdlc.main(["plan-validate", str(plan)]) == 0
    assert sdlc.plan_criteria(plan_for_12())[0] == {1, 2}
    assert _git("worktree", "list", cwd=repo).count("\n") == 0                     # the throwaway worktree is gone


# --- planning finds a PRD merged on GitHub that this checkout hasn't pulled (F2) ---------------------------

def _recording_git(monkeypatch):
    """Wrap `sdlc.git`, keeping every call's arguments, so a test can say whether anything was fetched."""
    calls, real = [], sdlc.git
    monkeypatch.setattr(sdlc, "git", lambda args, *a, **k: calls.append(list(args)) or real(args, *a, **k))
    return calls


def _merge_on_origin(fake, files):
    """Someone else's PR with `files`, merged into the bare origin's main: this checkout is now behind it."""
    pr = fake.open_pr(f"docs-{len(fake.prs)}", files)
    fake.gh(["pr", "merge", str(pr), "--match-head-commit", fake.sha(fake.prs[pr]["head"])])


PRD_12 = "# PRD: add a station (#12)\n\n[PRD](x#issuecomment-1)\n\n## Acceptance criteria\n\n1. a\n2. b\n"


def test_commit_prd_then_planning_starts_with_no_fast_forward(repo, monkeypatch, capsys, tmp_path):
    fake = PrdGitHub(monkeypatch, repo, escalated())
    assert sdlc.main(["commit-prd", "12"]) == 0
    assert not (repo / "docs").exists()          # the checkout's working tree never has it: nobody pulled
    monkeypatch.setattr(sdlc, "ROOT", repo)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_for_12()))
    capsys.readouterr()
    assert sdlc.main(["plan-validate", str(plan)]) == 0
    assert "ok" in capsys.readouterr().out
    assert sdlc.main(["plan-post", "12", str(plan), "--dry-run"]) == 0       # plan-post finds it the same way
    assert "plan rev 1" in capsys.readouterr().out
    assert sdlc.plan_criteria(plan_for_12(), "origin/main")[0] == {1, 2}    # the entry points pass the ref down
    assert not (repo / "docs").exists() and _git("status", "--porcelain", cwd=repo) == ""   # nothing pulled


def test_plan_validate_finds_a_full_lane_prd_merged_after_the_last_pull(repo, monkeypatch, capsys, tmp_path):
    fake = PrdGitHub(monkeypatch, repo, [], labels=["sdlc:approved"])
    _merge_on_origin(fake, {"docs/prd/12-add-a-station.md": PRD_12})
    assert _git("ls-tree", "--name-only", "origin/main", "docs/prd/", cwd=repo) == ""   # behind: not fetched yet
    monkeypatch.setattr(sdlc, "ROOT", repo)
    calls = _recording_git(monkeypatch)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_for_12()))
    assert sdlc.main(["plan-validate", str(plan)]) == 0
    assert [c for c in calls if c[0] == "fetch"] == [["fetch", "-q", "origin", "main"]]   # once
    assert sdlc.plan_criteria(plan_for_12(), "origin/main")[0] == {1, 2}
    assert not (repo / "docs").exists()


def test_a_prd_in_the_working_tree_is_read_as_before_with_no_fetch(repo, monkeypatch, capsys, tmp_path):
    fake = PrdGitHub(monkeypatch, repo, [], labels=["sdlc:approved"])
    _merge_on_origin(fake, {"docs/prd/12-other.md": PRD_12 + "3. c\n"})   # main has another: the local one wins
    (repo / "docs" / "prd").mkdir(parents=True)
    (repo / "docs" / "prd" / "12-local.md").write_text(PRD_12)
    monkeypatch.setattr(sdlc, "ROOT", repo)
    calls = _recording_git(monkeypatch)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_for_12()))
    assert sdlc.main(["plan-validate", str(plan)]) == 0
    assert calls == [] and sdlc.planning_ref(12, CONFIG, offline=False) is None
    assert sdlc.find_prd(12, "origin/main") == ("docs/prd/12-local.md", PRD_12)
    assert sdlc.plan_errors(plan_for_12(), None) == sdlc.check_plan(plan_for_12()) == []


def test_a_failed_fetch_is_said_and_the_last_fetched_main_is_read(repo, monkeypatch, capsys, tmp_path):
    fake = PrdGitHub(monkeypatch, repo, [], labels=["sdlc:approved"])
    _merge_on_origin(fake, {"docs/prd/12-add-a-station.md": PRD_12})
    _git("fetch", "-q", "origin", "main", cwd=repo)
    _git("remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=repo)   # a local path: no network
    monkeypatch.setattr(sdlc, "ROOT", repo)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_for_12()))
    assert sdlc.main(["plan-validate", str(plan)]) == 0
    assert "git fetch -q origin failed" in (err := capsys.readouterr().err)
    assert "reading docs/prd/12-*.md from the last fetched origin/main" in err


def test_a_fetch_that_hangs_is_bounded_and_the_last_fetched_main_is_read(repo, monkeypatch, capsys, tmp_path):
    fake = PrdGitHub(monkeypatch, repo, [], labels=["sdlc:approved"])
    _merge_on_origin(fake, {"docs/prd/12-add-a-station.md": PRD_12})
    _git("fetch", "-q", "origin", "main", cwd=repo)
    monkeypatch.setattr(sdlc, "ROOT", repo)
    run, waits = subprocess.run, []

    def hanging_fetch(cmd, *a, **k):
        if cmd[:2] == ["git", "fetch"]:
            waits.append(k.get("timeout"))
            raise subprocess.TimeoutExpired(cmd, k.get("timeout"))
        return run(cmd, *a, **k)
    monkeypatch.setattr(sdlc.subprocess, "run", hanging_fetch)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_for_12()))
    assert sdlc.main(["plan-validate", str(plan)]) == 0
    assert waits == [sdlc.PRD_FETCH_SECONDS] and 0 < sdlc.PRD_FETCH_SECONDS < float("inf")
    err = capsys.readouterr().err
    assert f"timed out after {sdlc.PRD_FETCH_SECONDS}s" in err and "from the last fetched origin/main" in err
    monkeypatch.setattr(sdlc, "PRD_FETCH_SECONDS", 0)
    assert sdlc.main(["plan-validate", str(plan)]) == 1 and "must be positive" in capsys.readouterr().err


@pytest.mark.parametrize("local", [False, True])
def test_plan_create_links_the_prd_it_found(repo, monkeypatch, capsys, tmp_path, local):
    prd = agent("prd", 1, 1)
    plan_c = comment(OWNER, sdlc.render_comment("plan", 1, sdlc.render_plan(plan_for_12()), CONFIG), 3)
    fake = PrdGitHub(monkeypatch, repo, [prd, agent("approval", 1, 2, by=POSTER, via="comment"), plan_c,
                                         agent("plan-review", 1, 4, verdict="approve", round="1")], labels=["sdlc:approved"])
    linked = PRD_12.replace("comment-1)", f"comment-{anchor(prd)})")
    _merge_on_origin(fake, {"docs/prd/12-a-first.md": PRD_12, "docs/prd/12-b-approved.md": linked})
    if local:   # a checkout that has a PRD: its link is what it always was
        (repo / "docs" / "prd").mkdir(parents=True)
        (repo / "docs" / "prd" / "12-local.md").write_text(PRD_12)
    monkeypatch.setattr(sdlc, "ROOT", repo)
    monkeypatch.setattr(sdlc, "fetch_plan_issues", lambda *a: [])
    monkeypatch.setattr(sdlc, "fetch_links", lambda *a: ({}, {}))
    monkeypatch.setattr(sdlc, "gh_pages", lambda path: [])
    urls, render = [], sdlc.render_leaf_body
    monkeypatch.setattr(sdlc, "render_leaf_body", lambda *a: urls.append(a[-1]) or render(*a))
    calls = _recording_git(monkeypatch)
    assert sdlc.main(["plan-create", "12", "--dry-run"]) == 0
    name = "12-local.md" if local else "12-b-approved.md"    # at the ref, the one whose header links the approval
    assert urls == [f"https://github.com/o/r/blob/main/docs/prd/{name}"]
    assert [c for c in calls if c[0] == "fetch"] == ([] if local else [["fetch", "-q", "origin", "main"]])


def test_a_feature_with_no_prd_anywhere_still_fails_and_says_where_it_looked(repo, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sdlc, "ROOT", repo)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_for_12()))
    assert sdlc.main(["plan-validate", str(plan)]) == 1
    assert "docs/prd/12-*.md (nor on origin/main)" in capsys.readouterr().out
    bug = {**plan_for_12(), "kind": "bug"}
    assert sdlc.check_plan(bug, "origin/main") == []                   # a bug with no PRD validates as before


def test_from_file_never_fetches_and_reads_only_the_working_tree(repo, monkeypatch, capsys, tmp_path):
    fake = PrdGitHub(monkeypatch, repo, escalated())
    assert sdlc.main(["commit-prd", "12"]) == 0      # origin/main has it (and this checkout fetched it)
    monkeypatch.setattr(sdlc, "ROOT", repo)
    bundle, plan = tmp_path / "bundle.json", tmp_path / "plan.json"
    bundle.write_text(json.dumps(fake.bundle()))
    plan.write_text(json.dumps(plan_for_12()))
    calls = _recording_git(monkeypatch)
    assert sdlc.main(["plan-post", "12", str(plan), "--from-file", str(bundle), "--dry-run"]) == 1
    assert "no numbered acceptance criteria found in docs/prd/12-*.md (merge" in capsys.readouterr().err
    assert calls == []


def test_prd_at_prefers_the_file_that_links_the_approved_comment(repo, monkeypatch):
    fake = PrdGitHub(monkeypatch, repo, [], labels=["sdlc:approved"])
    _merge_on_origin(fake, {"docs/prd/12-a.md": PRD_12, "docs/prd/12-b.md": PRD_12.replace("comment-1)", "comment-9)"),
                            "docs/prd/120-c.md": PRD_12})
    _git("fetch", "-q", "origin", "main", cwd=repo)
    assert sdlc.prd_at(12, "origin/main", repo, "9")[0] == "docs/prd/12-b.md"
    assert sdlc.prd_at(12, "origin/main", repo)[0] == "docs/prd/12-a.md"          # none known: the first by name
    assert sdlc.prd_at(12, "origin/main", repo, "5")[0] == "docs/prd/12-a.md"     # none links it: the first too
    assert sdlc.prd_at(13, "origin/main", repo) is None and sdlc.prd_at(12, "origin/nope", repo) is None


@pytest.mark.parametrize("files", [
    {"docs/prd/notes.md": "# notes\n"},                                                  # unrelated docs
    {"docs/prd/47-other.md": "# PRD\n\n[PRD](x#issuecomment-{a})\n\n## Problem\n"},      # another issue's PRD
    {"docs/prd/12-x.md": "# PRD\n\n[PRD](x#issuecomment-{a})\n", "docs/prd/assets/a.png": "png"},   # two files
    {"docs/prd/12-x.md": "# PRD\n\n[PRD](x#issuecomment-{stale})\n\n## Problem\n"},      # an earlier revision
    {"docs/prd/12-x.md": "# PRD\n\nno link\n\n## Problem\n[PRD](x#issuecomment-{a})\n"},  # linked below the header
])
def test_merge_prd_refuses_anything_but_this_brief_s_file(repo, monkeypatch, capsys, files):
    fake = PrdGitHub(monkeypatch, repo, escalated(rev2=True))
    a, stale = anchor(fake.comments[3]), anchor(fake.comments[0])
    pr = fake.open_pr("docs-x", {p: t.format(a=a, stale=stale) for p, t in files.items()})
    assert sdlc.main(["merge-prd", "12", str(pr)]) == 1
    assert "pr merge" not in " ".join(" ".join(c) for c in fake.calls) and fake.posted == []
    assert fake.state()["action"] == "commit_prd"


def test_merge_prd_takes_this_brief_s_file_and_records_it(repo, monkeypatch, capsys):
    fake = PrdGitHub(monkeypatch, repo, escalated(rev2=True))
    pr = fake.open_pr("docs-x", {"docs/prd/12-x.md": f"# PRD\n\n[PRD](x#issuecomment-{anchor(fake.comments[3])})\n\n## P\n"})
    assert sdlc.main(["merge-prd", "12", str(pr)]) == 0
    assert fake.state()["action"] == "plan"


def test_commit_prd_after_an_unmerged_pr_reuses_it(repo, monkeypatch, capsys):
    fake = PrdGitHub(monkeypatch, repo, escalated())
    fake.fail.add("merge")
    assert sdlc.main(["commit-prd", "12"]) == 1 and fake.on_main() == []
    assert fake.state()["action"] == "commit_prd"
    assert sdlc.main(["commit-prd", "12"]) == 0
    assert len(fake.created()) == 1 and fake.on_main() == ["docs/prd/12-add-a-station.md"] and len(fake.posted) == 1


def test_commit_prd_after_a_merge_that_was_not_recorded_only_records_it(repo, monkeypatch, capsys):
    fake = PrdGitHub(monkeypatch, repo, escalated())
    fake.fail.add("post")
    assert sdlc.main(["commit-prd", "12"]) == 1
    assert "Rerun `commit-prd 12` to record it" in capsys.readouterr().err
    assert fake.on_main() == ["docs/prd/12-add-a-station.md"] and fake.state()["action"] == "commit_prd"
    calls = len(fake.calls)
    assert sdlc.main(["commit-prd", "12"]) == 0
    assert len(fake.created()) == 1 and fake.on_main() == ["docs/prd/12-add-a-station.md"] and len(fake.posted) == 1
    assert not any(c[:2] in (["pr", "create"], ["pr", "merge"]) for c in fake.calls[calls:])
    assert fake.state()["action"] == "plan"
    assert sdlc.main(["commit-prd", "12"]) == 0 and "already" in capsys.readouterr().out   # a third run: nothing
    assert len(fake.posted) == 1


def test_commit_prd_reuses_a_file_already_there_rather_than_adding_one(repo, monkeypatch, capsys):
    fake = PrdGitHub(monkeypatch, repo, escalated(rev2=True))
    # an earlier escalation's file (rev 1) is on main: the new one overwrites it, never sits beside it
    pr = fake.open_pr("old", {"docs/prd/12-old-name.md": f"# PRD\n\n[PRD](x#issuecomment-{anchor(fake.comments[0])})\n"})
    fake.gh(["pr", "merge", str(pr), "--match-head-commit", fake.sha("old")])
    fake.calls.clear()
    assert sdlc.main(["commit-prd", "12"]) == 0
    assert fake.on_main() == ["docs/prd/12-old-name.md"]
    assert sdlc.header_links(_git("show", "origin/main:docs/prd/12-old-name.md", cwd=repo), anchor(fake.comments[3]))


def test_merge_prd_on_a_normal_prd_does_what_it_always_did(monkeypatch, capsys):
    calls, posted = [], []
    bundle = {"issue": issue(["sdlc:approved"]), "trusted": [OWNER],
              "comments": [agent("prd", 1, 1), comment(POSTER, "/approve", 2), agent("approval", 1, 3, by=POSTER)]}
    monkeypatch.setattr(sdlc, "repo_of", lambda config: "o/r")
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, config, trusted=None: bundle)
    monkeypatch.setattr(sdlc, "post_comment", lambda *a: posted.append(a))
    monkeypatch.setattr(sdlc, "git", lambda *a, **k: pytest.fail("a normal merge-prd runs no git"))
    monkeypatch.setattr(sdlc, "gh_json", lambda args: calls.append(args) or {
        "state": "OPEN", "mergeable": "MERGEABLE", "baseRefName": "main", "files": [{"path": "docs/prd/12-x.md"}]})
    monkeypatch.setattr(sdlc, "gh", lambda args, **k: calls.append(args) or "")
    assert sdlc.main(["merge-prd", "12", "7"]) == 0
    assert calls == [["pr", "view", "7", "--repo", "o/r", "--json", "state,mergeable,baseRefName,files"],
                     ["pr", "merge", "7", "--repo", "o/r", "--squash", "--delete-branch"]]
    assert posted == [] and capsys.readouterr().out == "merged PR #7\n"


@pytest.mark.parametrize("comments,labels", [
    (approved_quick(), [ROUTINE, "sdlc:approved", "lane:quick"]),                       # still quick
    ([agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER, via="comment")], ["sdlc:approved"]),   # full lane
])
def test_commit_prd_refuses_an_issue_that_never_left_the_quick_lane(repo, monkeypatch, capsys, comments, labels):
    fake = PrdGitHub(monkeypatch, repo, comments, labels)
    assert sdlc.main(["commit-prd", "12"]) == 1
    assert "never left the quick lane" in capsys.readouterr().err and fake.calls == [] and fake.posted == []


def test_commit_prd_dry_run_writes_pushes_and_posts_nothing(repo, monkeypatch, capsys):
    fake = PrdGitHub(monkeypatch, repo, escalated())
    refs = _git("for-each-ref", cwd=fake.origin), _git("for-each-ref", cwd=repo)
    trees = _git("worktree", "list", cwd=repo)
    assert sdlc.main(["commit-prd", "12", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would write docs/prd/12-add-a-station.md" in out and f"#issuecomment-{anchor(fake.comments[0])}" in out
    assert (_git("for-each-ref", cwd=fake.origin), _git("for-each-ref", cwd=repo)) == refs
    assert _git("worktree", "list", cwd=repo) == trees and _git("status", "--porcelain", cwd=repo) == ""
    assert fake.posted == [] and all(c[:2] == ["pr", "list"] for c in fake.calls)


def test_wait_mergeable_is_bounded(monkeypatch):
    monkeypatch.setattr(sdlc, "repo_of", lambda config: "o/r")
    monkeypatch.setattr(sdlc, "gh_json", lambda args: {"mergeable": "UNKNOWN"})
    slept = []
    monkeypatch.setattr(sdlc.time, "sleep", slept.append)
    assert sdlc.wait_mergeable(5, CONFIG, wait=4, poll=2) == "UNKNOWN" and slept == [2, 2]
    with pytest.raises(sdlc.SdlcError):
        sdlc.wait_mergeable(5, CONFIG, wait=4, poll=0)


def test_a_revision_posted_while_the_pr_is_made_stops_the_merge(repo, monkeypatch, capsys):
    fake = PrdGitHub(monkeypatch, repo, escalated())
    fake.after_create = [agent("prd", 2, 30, "## Outcome\nbigger", lane="quick")]
    assert sdlc.main(["commit-prd", "12"]) == 1
    assert "changed while PR" in capsys.readouterr().err
    assert not any(c[:2] == ["pr", "merge"] for c in fake.calls) and fake.posted == [] and fake.on_main() == []


# --- escalate-to-full: a quick change that outgrew the lane, by hand or on a trigger ------------------------

class EscalateGitHub:
    """`gh` for an escalation: the issue's labels follow `issue edit`, posts land on the issue (12) or the PR (50),
    and `commit-prd` (T5.2's, tested above) is recorded rather than run. `drop` loses the next post on the issue."""

    def __init__(self, monkeypatch, b):
        self.b, self.issue = b, {**b["issue"], "labels": list(b["issue"]["labels"]), "body": ""}
        self.comments, self.pr, self.pr_comments = list(b["comments"]), b["pr"], list(b["pr_comments"])
        self.calls, self.posted, self.committed, self.drop, self.meanwhile = [], [], [], False, None
        monkeypatch.setattr(sdlc, "repo_of", lambda cfg: "o/r")
        monkeypatch.setattr(sdlc, "fetch_trusted", lambda cfg: set(b["trusted"]))
        monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, cfg, trusted=None: self.bundle())
        monkeypatch.setattr(sdlc, "fetch_slice", lambda n, cfg: (dict(self.issue, labels=list(self.issue["labels"])),
                                                                 list(self.comments)))
        monkeypatch.setattr(sdlc, "fetch_pr", lambda n, cfg: (self.pr, list(self.pr_comments)))
        monkeypatch.setattr(sdlc, "quick_gate_bundle", lambda n, cfg: {**self.b, **self.bundle(),
                                                                        "pr_comments": list(self.pr_comments)})
        monkeypatch.setattr(sdlc, "gh", self.gh)
        monkeypatch.setattr(sdlc, "gh_json", lambda args: self.calls.append(args) or {"behind_by": 0})
        monkeypatch.setattr(sdlc, "post_comment", self.post)
        monkeypatch.setattr(sdlc, "commit_prd", lambda n, cfg, dry_run=False, root=None: self.committed.append(n) or 0)

    def bundle(self):
        return {"issue": dict(self.issue, labels=list(self.issue["labels"])), "comments": list(self.comments),
                "trusted": list(self.b["trusted"])}

    def post(self, n, body, cfg):
        self.posted.append((n, body))
        if n == 12 and self.drop:
            return {}
        (self.comments if n == 12 else self.pr_comments).append(comment(OWNER, body, 50 + len(self.posted)))
        if n == 12 and self.meanwhile:   # what a person does on the issue while the record is being posted
            self.meanwhile(self)
        return {"html_url": "u"}

    def gh(self, args, check=True):
        self.calls.append(args)
        if args[:2] == ["issue", "edit"]:
            gone = {args[i + 1] for i, a in enumerate(args) if a == "--remove-label"}
            added = [args[i + 1] for i, a in enumerate(args) if a == "--add-label"]
            kept = [l for l in self.issue["labels"] if l not in gone]
            self.issue["labels"] = kept + [l for l in added if l not in kept]
        return ""

    def state(self):
        return sdlc.bundle_state(self.bundle(), CONFIG)

    def lane_records(self):
        return [body for n, body in self.posted if n == 12 and "kind=lane" in body.splitlines()[0]]

    def merged(self):
        return any(c[:2] == ["pr", "merge"] for c in self.calls)

    def assert_escalated(self, trigger):
        assert sorted(self.issue["labels"]) == [ROUTINE, "sdlc:approved"]          # one state label, no lane:quick
        (record,) = self.lane_records()
        assert record.splitlines()[0] == f"<!-- sdlc:v1 kind=lane lane=full trigger={trigger} -->"
        st = self.state()
        assert (st["state"], st["lane"], st["action"], st["conflicts"]) == ("approved", None, "commit_prd", [])
        assert self.committed == [12] and not self.merged()
        return record

    def assert_untouched(self):
        assert self.lane_records() == [] and self.committed == [] and not self.merged()
        assert not any(c[:2] == ["issue", "edit"] for c in self.calls)


ESC_LABELS = [ROUTINE, "sdlc:in-progress", "lane:quick"]


@pytest.mark.parametrize("labels", [ESC_LABELS, [ROUTINE, "sdlc:approved", "lane:quick"]])
def test_escalate_to_full_records_moves_the_label_and_commits_the_brief(monkeypatch, capsys, labels):
    fake = EscalateGitHub(monkeypatch, gate_bundle(labels=labels))
    assert sdlc.main(["escalate-to-full", "12", "--reason", "it needs a new fetcher"]) == 0
    record = fake.assert_escalated("manual")
    assert "it needs a new fetcher" in record
    # once the brief is merged (`prd-merged`, which commit-prd posts), planning is next
    fake.comments.append(agent("prd-merged", None, 90, comment="1"))
    st = fake.state()
    assert (st["action"], sdlc.next_command(st)) == ("plan", "/plan-issue 12")


def test_escalate_to_full_dry_run_posts_nothing(monkeypatch, capsys):
    fake = EscalateGitHub(monkeypatch, gate_bundle(labels=ESC_LABELS))
    assert sdlc.main(["escalate-to-full", "12", "--reason", "bigger", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert fake.posted == [] and fake.calls == [] and fake.committed == []
    assert "kind=lane lane=full trigger=manual" in out and "then: commit-prd 12" in out
    assert "remove ['sdlc:in-progress', 'lane:quick']" in out


def test_escalate_to_full_dry_run_on_the_demo_fixture(monkeypatch, capsys):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("--from-file --dry-run reads nothing else"))
    monkeypatch.setattr(sdlc, "post_comment", lambda *a, **k: pytest.fail("a dry run posts nothing"))
    f = str(FIXTURES / "denied_path.json")
    assert sdlc.main(["escalate-to-full", "12", "--reason", "touches play.py", "--dry-run", "--from-file", f]) == 0
    assert "kind=lane lane=full trigger=manual" in capsys.readouterr().out
    assert sdlc.main(["escalate-to-full", "12", "--reason", "x", "--from-file", f]) == 1     # never for real
    assert "with --dry-run only" in capsys.readouterr().err


@pytest.mark.parametrize("case,needle", [
    ("closed", "is closed"),
    ("slice", "is a plan:slice issue"),
    ("already", "already in the full lane"),
    ("full-lane", "is not a quick change"),
    ("human", "escalated to a human"),
    ("in-review", "the latest is rev 2"),
    ("needs-info", "is needs-info, not approved or in-progress"),
])
def test_escalate_to_full_refuses(monkeypatch, capsys, case, needle):
    moved = ready_quick(GATE_BRIEF) + [CLAIM, agent("lane", None, 9, "moved", lane="full")]
    b = {"closed": lambda: gate_bundle(labels=ESC_LABELS, state="closed"),
         "slice": lambda: gate_bundle(labels=["plan:slice", ROUTINE]),
         "already": lambda: gate_bundle(labels=[ROUTINE, "sdlc:approved"], comments=moved),
         "full-lane": lambda: gate_bundle(labels=[ROUTINE, "sdlc:approved"],
                                          comments=[agent("prd", 1, 1, GATE_BRIEF), agent("approval", 1, 2, by=POSTER)]),
         "human": lambda: gate_bundle(labels=[ROUTINE, "sdlc:escalated", "lane:quick"]),
         "in-review": lambda: gate_bundle(labels=[ROUTINE, "sdlc:prd-review", "lane:quick"],
                                          comments=ready_quick(GATE_BRIEF) + [quick_brief(ts=9, rev=2, body=GATE_BRIEF)]),
         "needs-info": lambda: gate_bundle(labels=[ROUTINE, "sdlc:needs-info", "lane:quick"],
                                           comments=ready_quick(GATE_BRIEF) + [agent("question", None, 9)]),
         }[case]()
    fake = EscalateGitHub(monkeypatch, b)
    for dry in ([], ["--dry-run"]):
        assert sdlc.main(["escalate-to-full", "12", "--reason", "x", *dry]) == 1
        assert needle in capsys.readouterr().err
        assert fake.posted == [] and fake.calls == [] and fake.committed == []


def test_a_half_done_escalation_is_finished_not_refused(monkeypatch, capsys):
    # the record was posted but the label move failed: derive_state calls that a conflict; a rerun clears it
    moved = ready_quick(GATE_BRIEF) + [CLAIM, agent("lane", None, 9, "moved", lane="full", trigger="rounds")]
    fake = EscalateGitHub(monkeypatch, gate_bundle(labels=ESC_LABELS, comments=moved))
    assert fake.state()["action"] == "fix_conflict"
    assert sdlc.main(["escalate-to-full", "12", "--reason", "rerun"]) == 0
    assert fake.posted == [] and sorted(fake.issue["labels"]) == [ROUTINE, "sdlc:approved"]
    assert fake.state()["action"] == "commit_prd" and fake.committed == [12]


def test_a_half_done_escalation_finishes_without_a_new_reason(monkeypatch, capsys):
    moved = ready_quick(GATE_BRIEF) + [CLAIM, agent("lane", None, 9, "moved", lane="full", trigger="diff")]
    fake = EscalateGitHub(monkeypatch, gate_bundle(labels=ESC_LABELS, comments=moved))
    assert sdlc.main(["escalate-to-full", "12"]) == 0          # exactly what the recovery message says to run
    assert sorted(fake.issue["labels"]) == [ROUTINE, "sdlc:approved"] and fake.committed == [12]
    fresh = EscalateGitHub(monkeypatch, gate_bundle(labels=ESC_LABELS))
    assert sdlc.main(["escalate-to-full", "12"]) == 1           # a new escalation still needs its reason
    assert "--reason is required" in capsys.readouterr().err and fresh.posted == []


def handed_to_a_human(fake):
    fake.issue["labels"] = [l for l in fake.issue["labels"] if l != "sdlc:in-progress"] + ["sdlc:escalated"]


def handed_alongside(fake):
    fake.issue["labels"] = fake.issue["labels"] + ["sdlc:escalated"]


def approved_meanwhile(fake):   # rev 2 posted and approved just before the record lands: the record counts on it
    record = fake.comments.pop()
    fake.comments += [quick_brief(ts=40, rev=2, body=GATE_BRIEF), agent("approval", 2, 41, by=POSTER, via="comment"),
                      record]


@pytest.mark.parametrize("meanwhile,needle", [(handed_to_a_human, "is now escalated"),
                                              (handed_alongside, "multiple sdlc:* labels"),
                                              (approved_meanwhile, "the approved brief changed")])
def test_what_changes_while_the_record_is_posted_stops_the_label_move(monkeypatch, capsys, meanwhile, needle):
    fake = EscalateGitHub(monkeypatch, gate_bundle(labels=ESC_LABELS))
    fake.meanwhile = meanwhile
    assert sdlc.main(["escalate-to-full", "12", "--reason", "x"]) == 1
    err = capsys.readouterr().err
    assert needle in err and "No label moved" in err, err
    assert len(fake.lane_records()) == 1 and fake.committed == []
    assert not any(c[:2] == ["issue", "edit"] for c in fake.calls)


def test_a_record_the_ledger_ignores_moves_no_label(monkeypatch, capsys):
    fake = EscalateGitHub(monkeypatch, gate_bundle(labels=ESC_LABELS))
    fake.drop = True   # e.g. posted by an account that isn't trusted: derive_state never counts it
    assert sdlc.main(["escalate-to-full", "12", "--reason", "x"]) == 1
    assert "doesn't count it" in capsys.readouterr().err
    assert len(fake.posted) == 1 and fake.committed == [] and fake.issue["labels"] == ESC_LABELS


def test_a_commit_prd_failure_leaves_the_escalation_and_says_how_to_finish(monkeypatch, capsys):
    fake = EscalateGitHub(monkeypatch, gate_bundle(labels=ESC_LABELS))
    monkeypatch.setattr(sdlc, "commit_prd", lambda *a, **k: (_ for _ in ()).throw(sdlc.SdlcError("push failed")))
    assert sdlc.main(["escalate-to-full", "12", "--reason", "x"]) == 1
    err = capsys.readouterr().err
    assert "push failed" in err and "`commit-prd 12` is safe to rerun" in err
    assert sorted(fake.issue["labels"]) == [ROUTINE, "sdlc:approved"] and fake.state()["action"] == "commit_prd"


def test_a_failed_label_move_says_to_rerun(monkeypatch, capsys):
    fake = EscalateGitHub(monkeypatch, gate_bundle(labels=ESC_LABELS))
    edit = fake.gh

    def failing(args, check=True):
        if args[:2] == ["issue", "edit"]:
            raise sdlc.SdlcError("gh issue edit failed: boom")
        return edit(args, check)
    monkeypatch.setattr(sdlc, "gh", failing)
    assert sdlc.main(["escalate-to-full", "12", "--reason", "x"]) == 1
    assert "Rerun `escalate-to-full 12` to finish it" in capsys.readouterr().err
    assert len(fake.posted) == 1 and fake.committed == [] and fake.issue["labels"] == ESC_LABELS
    st = fake.state()   # what `state 12` shows meanwhile names the way out
    assert st["action"] == "fix_conflict" and any("escalate-to-full 12" in c for c in st["conflicts"])


class EscalatePrd(PrdGitHub):
    """PrdGitHub (a real `commit-prd` over a scratch origin) that also applies `issue edit` to the labels."""

    def gh(self, args, **k):
        if args[:2] == ["issue", "edit"]:
            self.calls.append(args)
            gone = {args[i + 1] for i, a in enumerate(args) if a == "--remove-label"}
            added = [args[i + 1] for i, a in enumerate(args) if a == "--add-label"]
            kept = [l for l in self.issue["labels"] if l not in gone]
            self.issue = {**self.issue, "labels": kept + [l for l in added if l not in kept]}
            return ""
        return super().gh(args, **k)


def test_escalate_to_full_runs_commit_prd_and_planning_starts(repo, monkeypatch, capsys):
    fake = EscalatePrd(monkeypatch, repo, escalated(lane=False), labels=ESC_LABELS)
    assert sdlc.main(["escalate-to-full", "12", "--reason", "it needs a new fetcher"]) == 0
    assert sorted(fake.issue["labels"]) == [ROUTINE, "sdlc:approved"]
    assert [p.splitlines()[0].split()[2] for p in fake.posted] == ["kind=lane", "kind=prd-merged"]
    assert fake.on_main() == ["docs/prd/12-add-a-station.md"]
    capsys.readouterr()
    assert sdlc.main(["state", "12", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert (st["state"], st["lane"], st["action"], st["next"]) == ("approved", None, "plan", "/plan-issue 12")


# the triggers: `pr-review` on one change round too many, `merge` on a diff that broke a quick rule

def review_report(tmp_path, verdict, brief):
    r = tmp_path / f"codex-{verdict}.md"
    r.write_text(f"HEAD: {HEAD}\nBrief: {brief}\n\n1. **blocking** — something.\n\nVERDICT: {verdict}\n")
    return str(r)


def changes_rounds_before(n):
    return [marked("tests", 6, sha=HEAD, result="pass", passed="1300")] + [
        marked("pr-review", 7 + i, sha="c" * 40, verdict="changes", round=str(i + 1)) for i in range(n)]


def review_fake(monkeypatch, before):
    b = gate_bundle(records=changes_rounds_before(before))
    fake = EscalateGitHub(monkeypatch, b)
    return fake, sdlc.approved_brief_id(fake.state())


def test_a_third_change_round_escalates(tmp_path, monkeypatch, capsys):
    fake, brief = review_fake(monkeypatch, 2)
    assert sdlc.main(["pr-review", "50", "--report", review_report(tmp_path, "changes", brief)]) == 1
    assert [n for n, _ in fake.posted] == [50, 12]                   # the round is recorded first, then the move
    record = fake.assert_escalated("rounds")
    assert "Codex asked for changes in 3 rounds, more than the quick lane's 2" in record


def test_a_tight_slice_round_budget_never_blocks_the_quick_trigger(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({**CONFIG, "max_pr_rounds": 2}))
    fake, brief = review_fake(monkeypatch, 2)
    assert sdlc.main(["--config", str(cfg), "pr-review", "50", "--report", review_report(tmp_path, "changes", brief)]) == 1
    assert [n for n, _ in fake.posted] == [50, 12]
    fake.assert_escalated("rounds")


@pytest.mark.parametrize("before,verdict", [(1, "changes"), (2, "approve"), (0, "changes")])
def test_a_round_within_the_limit_does_not_escalate(tmp_path, monkeypatch, capsys, before, verdict):
    fake, brief = review_fake(monkeypatch, before)
    assert sdlc.main(["pr-review", "50", "--report", review_report(tmp_path, verdict, brief)]) == 0
    assert [n for n, _ in fake.posted] == [50]
    fake.assert_untouched()
    assert "sdlc:in-progress" in fake.issue["labels"]


def test_a_third_change_round_in_a_dry_run_or_offline_escalates_nothing(tmp_path, monkeypatch, capsys):
    fake, brief = review_fake(monkeypatch, 2)
    report = review_report(tmp_path, "changes", brief)
    assert sdlc.main(["pr-review", "50", "--report", report, "--dry-run"]) == 1
    assert "a dry run escalates nothing" in capsys.readouterr().err and fake.posted == []
    f = tmp_path / "offline.json"
    f.write_text(json.dumps({"pr": fake.pr, "pr_comments": fake.pr_comments, "trusted": [OWNER],
                             "slice": fake.issue, "comments": fake.comments}))
    assert sdlc.main(["pr-review", "50", "--report", report, "--from-file", str(f)]) == 1
    assert "nothing escalated" in capsys.readouterr().err
    assert [n for n, _ in fake.posted] == [50]
    fake.assert_untouched()


def test_a_slice_pr_s_rounds_never_escalate(tmp_path, monkeypatch, capsys):
    fake, _ = review_fake(monkeypatch, 2)
    fake.issue["labels"] = ["plan:slice", "complexity:judgment"]
    r = tmp_path / "slice.md"
    r.write_text(f"HEAD: {HEAD}\n\n1. x\n\nVERDICT: changes\n")
    assert sdlc.main(["pr-review", "50", "--report", str(r)]) == 0
    fake.assert_untouched()


BROKEN = {
    "denied": [changed(STATION), changed("src/twiddle/play.py", status="modified")],
    "sixth": [changed(f"src/twiddle/stations/catalog/s{i}.toml", 1) for i in range(6)],
    "unlisted": [changed(STATION), changed("src/twiddle/stations/model.py", status="modified")],
}


@pytest.mark.parametrize("rule,needle", [("denied", "may not (src/twiddle/play.py)"),
                                         ("sixth", "changes 6 files, more than the quick lane's 5"),
                                         ("unlisted", "outside the brief's `## Files` list")])
def test_a_merge_refused_for_a_diff_rule_escalates(monkeypatch, capsys, rule, needle):
    brief = GATE_BRIEF.replace("- tests/test_stations.py", "- tests/test_stations.py\n- `src/twiddle/play.py`")
    fake = EscalateGitHub(monkeypatch, gate_bundle(comments=ready_quick(brief) + [CLAIM], files=BROKEN[rule]))
    code, _, err = run_merge(capsys)
    assert code == 1 and needle in err
    record = fake.assert_escalated("diff")
    assert needle in record
    # the same PR merged again is now no quick change: the slice gate refuses it, and main is never touched
    code, _, err = run_merge(capsys)
    assert code == 1 and "not an open plan:slice issue" in err and not fake.merged()


@pytest.mark.parametrize("rule", list(BROKEN))
def test_a_dry_run_or_the_gate_reports_a_diff_rule_and_changes_nothing(tmp_path, monkeypatch, capsys, rule):
    brief = GATE_BRIEF.replace("- tests/test_stations.py", "- tests/test_stations.py\n- `src/twiddle/play.py`")
    b = gate_bundle(comments=ready_quick(brief) + [CLAIM], files=BROKEN[rule])
    fake = EscalateGitHub(monkeypatch, b)
    code, _, err = run_merge(capsys, "--dry-run")
    assert code == 1 and "escalate-to-full 12" in err
    fake.assert_untouched()
    assert fake.posted == []
    assert sdlc.main(["quick-gate", "50"]) == 1                 # read-only, always
    assert "refused: " in capsys.readouterr().out
    fake.assert_untouched()
    assert fake.posted == []


@pytest.mark.parametrize("case,needle", [
    ("no-tests", "no recorded test run"),
    ("behind", "behind main"),
    ("unread", "could not read the PR's changed files"),
    ("rounds", "Codex asked for changes in 3 rounds"),     # pr-review's trigger, not merge's
])
def test_a_merge_refused_for_another_reason_does_not_escalate(monkeypatch, capsys, case, needle):
    b = {"no-tests": lambda: gate_bundle(records=lambda brief: [marked("pr-review", 8, sha=HEAD, verdict="approve",
                                                                       round="1", brief=brief)]),
         "behind": lambda: gate_bundle(behind_by=2),
         "unread": lambda: gate_bundle(files=[]),
         "rounds": lambda: gate_bundle(records=lambda brief: changes_rounds_before(3) + [
             marked("pr-review", 20, sha=HEAD, verdict="approve", round="4", brief=brief)]),
         }[case]()
    fake = EscalateGitHub(monkeypatch, b)
    code, _, err = run_merge(capsys)
    assert code == 1 and needle in err
    fake.assert_untouched()
    assert fake.posted == []


# --- a quick change's tech debt: debt by what it is, waiting in the open tech-debt list -----------

EPIC_LEAD = "Tech debt from #12 (review): not blocking, budgeted into a cleanup pass after a milestone is accepted."
QUICK_LEAD = ("Tech debt from quick change #12 (review): not blocking. A quick change has no epic and no cleanup pass, "
              "so it waits in the open `tech-debt` list (`gh issue list --label tech-debt`).")
EPIC_DEBT_NEXT = "nothing: #77 is tech debt, planned by its epic's cleanup pass (remove `tech-debt` to triage it alone)"
QUICK_DEBT_NEXT = ("nothing: #77 is tech debt from quick change #12, waiting in the open `tech-debt` list "
                   "(a quick change has no epic and no cleanup pass; remove `tech-debt` to triage it alone)")
ESCALATED = approved_quick() + [agent("lane", None, 3, "Moved to the full lane: too big.", lane="full")]


def file_debt(tmp_path, capsys, monkeypatch, labels, comments, state="open"):
    """`file-issue 12 --debt --dry-run` against issue #12 as given: the marker line and the lead line it would post."""
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("a dry run from a bundle must not call gh"))
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue(labels, state=state), "comments": list(comments), "trusted": [OWNER]}))
    body = tmp_path / "debt.md"
    body.write_text("`alarm list` sorts rooms case-sensitively.")
    assert sdlc.main(["file-issue", "12", "--title", "alarm list: room order", "--body-file", str(body), "--debt",
                      "--source", "review", "--dry-run", "--from-file", str(f)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("would run: gh issue create") and "--label tech-debt" in out[0]
    return out[1], out[2], "\n".join(out[1:])


@pytest.mark.parametrize("labels,state", [([ROUTINE, "sdlc:in-progress"], "open"), ([ROUTINE, "sdlc:done"], "closed")])
def test_quick_debt_names_the_quick_change_and_the_open_list(tmp_path, capsys, monkeypatch, labels, state):
    mk, lead, _ = file_debt(tmp_path, capsys, monkeypatch, labels, approved_quick(), state=state)
    assert mk == "<!-- sdlc:v1 kind=debt epic=12 source=review lane=quick -->"
    assert lead == QUICK_LEAD and "cleanup pass after" not in lead


def test_an_epics_debt_reads_as_before(tmp_path, capsys, monkeypatch):
    mk, lead, _ = file_debt(tmp_path, capsys, monkeypatch, ["sdlc:in-progress"],
                            [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER, via="comment")])
    assert (mk, lead) == ("<!-- sdlc:v1 kind=debt epic=12 source=review -->", EPIC_LEAD)


def test_an_issue_escalated_from_quick_to_full_reads_as_an_epic(tmp_path, capsys, monkeypatch):
    st = derive([ROUTINE, "sdlc:approved"], ESCALATED)
    assert st["lane"] is None and not sdlc.is_quick(st)   # the ledger's latest lane record, not the original brief
    mk, lead, full = file_debt(tmp_path, capsys, monkeypatch, [ROUTINE, "sdlc:approved"], ESCALATED)
    assert (mk, lead) == ("<!-- sdlc:v1 kind=debt epic=12 source=review -->", EPIC_LEAD)
    assert "quick" not in full


def debt_state(tmp_path, capsys, body):
    f = tmp_path / "debt.json"
    f.write_text(json.dumps({"issue": {"number": 77, "title": "alarm list: room order", "state": "open",
                                       "author": OWNER, "labels": ["tech-debt"], "body": body},
                             "comments": [], "trusted": [OWNER]}))
    assert sdlc.main(["state", "--from-file", str(f)]) == 0
    return capsys.readouterr().out


def test_state_on_a_debt_issue_says_where_it_waits(tmp_path, capsys, monkeypatch):
    _, _, quick = file_debt(tmp_path, capsys, monkeypatch, [ROUTINE, "sdlc:done"], approved_quick(), state="closed")
    _, _, epic = file_debt(tmp_path, capsys, monkeypatch, ["sdlc:in-progress"],
                           [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER, via="comment")])
    _, _, escalated = file_debt(tmp_path, capsys, monkeypatch, [ROUTINE, "sdlc:approved"], ESCALATED)
    assert f"next: {QUICK_DEBT_NEXT}\n" in debt_state(tmp_path, capsys, quick)
    for body in (epic, escalated, "no marker at all"):
        assert f"next: {EPIC_DEBT_NEXT}\n" in debt_state(tmp_path, capsys, body)


def test_state_reads_the_debt_issues_own_body_from_github(monkeypatch):
    """Live `state` must fetch the issue's body, or a quick change's debt would read as an epic's."""
    def gh_json(args):
        assert args[:2] == ["issue", "view"] and "body" in args[-1].split(",")
        return {"number": 77, "title": "t", "state": "OPEN", "author": {"login": OWNER},
                "labels": [{"name": "tech-debt"}], "body": "<!-- sdlc:v1 kind=debt epic=12 source=review lane=quick -->"}
    monkeypatch.setattr(sdlc, "gh_json", gh_json)
    monkeypatch.setattr(sdlc, "gh_pages", lambda path: [])
    b = sdlc.fetch_bundle(77, {**CONFIG, "repository": "o/r"}, trusted={OWNER})
    assert sdlc.next_command(sdlc.bundle_state(b, CONFIG)) == QUICK_DEBT_NEXT


def test_file_issue_from_a_bundle_only_dry_runs(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("refused before any gh call"))
    f, body = bundle_file(tmp_path, [ROUTINE, "sdlc:in-progress"], approved_quick())
    assert sdlc.main(["file-issue", "12", "--title", "t", "--body-file", body, "--debt", "--from-file", f]) == 1
    assert "use it with --dry-run" in capsys.readouterr().err
