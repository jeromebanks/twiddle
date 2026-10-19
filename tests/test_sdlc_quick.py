"""tools/sdlc.py: the quick lane in the ledger — the lane marker, the state, `next`, the frozen brief. No network."""
import json
import subprocess

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


def test_claim_quick_resume_reattaches_a_pushed_branch(repo, monkeypatch, capsys):
    FakeClaim(monkeypatch, quick_bundle())
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
    fake = FakeClaim(monkeypatch, quick_bundle([ROUTINE, "sdlc:approved"], ready_quick() + [CLAIM]))
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
