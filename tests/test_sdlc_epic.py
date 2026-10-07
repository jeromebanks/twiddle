"""tools/sdlc.py epic branches: epic/N, sync, ship gates, reverts. Scratch git repos only; no network."""
import json
import subprocess

import pytest

from tools import sdlc

CONFIG = sdlc.load_config()
OWNER, POSTER = "owner", "poster"
TRUSTED = {OWNER}
HEAD, OLD = "a" * 40, "b" * 40
_ids = iter(range(1, 10_000))


def comment(author, body, ts):
    return {"id": next(_ids), "author": author, "body": body, "created_at": f"2026-10-05T00:{ts:02d}:00Z",
            "url": f"https://example/c{ts}"}


def agent(kind, rev, ts, body="text", **extra):
    return comment(OWNER, sdlc.marker(kind, rev, **extra) + "\n" + body, ts)


HISTORY = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER), agent("plan", 1, 3),
           agent("plan-review", 1, 4, verdict="approve", round="1"), agent("plan-created", 1, 5)]
DEMO1 = agent("demo", 1, 10, milestone="M1", sha="d" * 40)
ACCEPT1 = agent("demo-approval", 1, 12, milestone="M1", by=POSTER)


def derive(comments=(), labels=("sdlc:in-progress",)):
    issue = {"number": 12, "title": "Alarm manager", "state": "open", "author": POSTER, "labels": list(labels)}
    return sdlc.derive_state(issue, HISTORY + list(comments), TRUSTED, CONFIG)


def ms(key, done, total):
    return {"title": f"#12 {key}: x", "key": key, "done": done, "total": total}


def progress(*milestones, open_=1, ready=(), in_flight=(), behind=None):
    return {"ready": list(ready), "in_flight": list(in_flight), "escalated": [], "waiting": [], "open": open_,
            "milestones": list(milestones), "behind_main": behind}


# --- a scratch repo with a bare origin -----------------------------------------

def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
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
    return root


def commit_on_main(root, name, text):
    (root / name).write_text(text)
    _git("add", name, cwd=root)
    _git("commit", "-q", "-m", f"main: {name}", cwd=root)
    _git("push", "-q", "origin", "main", cwd=root)
    return _git("rev-parse", "HEAD", cwd=root)


def commit_on_epic(root, name, text, n=12):
    wt = sdlc.epic_worktree(n, CONFIG, root)
    (wt / name).write_text(text)
    _git("add", name, cwd=wt)
    _git("commit", "-q", "-m", f"slice: {name}", cwd=wt)
    _git("push", "-q", "origin", f"HEAD:refs/heads/epic/{n}", cwd=wt)
    return _git("rev-parse", "HEAD", cwd=wt)


def origin_sha(root, ref):
    _git("fetch", "-q", "origin", ref, cwd=root)
    return _git("rev-parse", f"origin/{ref}", cwd=root)


PASS = lambda wt: {"result": "pass", "passed": 3, "failed": 0, "tail": "3 passed"}   # noqa: E731
FAIL = lambda wt: {"result": "fail", "passed": 2, "failed": 1, "tail": "1 failed"}   # noqa: E731


def test_epic_branch_is_created_once_then_adopted(repo):
    main = _git("rev-parse", "HEAD", cwd=repo)
    assert sdlc.ensure_epic_branch(12, CONFIG, repo, dry_run=True) == (main, True)
    assert not sdlc.remote_has("epic/12", repo)                        # a dry run pushes nothing
    assert sdlc.ensure_epic_branch(12, CONFIG, repo) == (main, True)
    assert sdlc.remote_has("epic/12", repo)
    ahead = commit_on_epic(repo, "a.py", "a = 1\n")
    commit_on_main(repo, "m.py", "m = 1\n")
    # an existing branch is adopted as it is (the #12 repair builds its own), never reset to main
    assert sdlc.ensure_epic_branch(12, CONFIG, repo) == (ahead, False)


def test_sync_merges_main_in_tests_and_pushes(repo):
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    slice_sha = commit_on_epic(repo, "a.py", "a = 1\n")
    main_sha = commit_on_main(repo, "m.py", "m = 1\n")
    out = sdlc.sync_epic(12, CONFIG, repo, test=PASS)
    assert (out["merged"], out["pushed"], out["conflict"], out["main"]) == (True, True, False, main_sha)
    head = origin_sha(repo, "epic/12")
    assert head == out["head"]
    assert sdlc.is_ancestor(main_sha, head, repo) and sdlc.is_ancestor(slice_sha, head, repo)
    assert slice_sha in _git("rev-list", f"origin/main..{head}", cwd=repo).split()   # merged, never rebased
    again = sdlc.sync_epic(12, CONFIG, repo, test=PASS)          # up to date: nothing merged, still tested
    assert (again["merged"], again["pushed"], again["head"], again["run"]["result"]) == (False, False, head, "pass")


def test_sync_pushes_nothing_when_the_suite_fails(repo):
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    before = commit_on_epic(repo, "a.py", "a = 1\n")
    commit_on_main(repo, "m.py", "m = 1\n")
    out = sdlc.sync_epic(12, CONFIG, repo, test=FAIL)
    assert out["merged"] and not out["pushed"] and origin_sha(repo, "epic/12") == before


def test_sync_conflict_aborts_and_pushes_nothing(repo):
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    before = commit_on_epic(repo, "code.py", "x = 2\n")
    commit_on_main(repo, "code.py", "x = 3\n")
    out = sdlc.sync_epic(12, CONFIG, repo, test=lambda wt: pytest.fail("a conflict runs no tests"))
    assert out["conflict"] and out["files"] == ["code.py"] and not out["pushed"]
    assert origin_sha(repo, "epic/12") == before
    wt = repo / ".worktrees" / "epic-12"
    assert _git("status", "--porcelain", cwd=wt) == ""             # the merge was aborted, not left half-done


def test_a_revert_on_main_is_seen(repo):
    sha = commit_on_main(repo, "w.py", "w = 1\n")
    assert sdlc.reverted_on_main(CONFIG, repo) == set()
    _git("revert", "--no-edit", sha, cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)
    assert sdlc.reverted_on_main(CONFIG, repo) == {sha}
    assert sdlc.is_ancestor(sha, "origin/main", repo)                 # why ancestry can't tell


# --- which milestones are already on main ---------------------------------------------

def merged_pr(oid, base="main"):
    return {"state": "MERGED", "baseRefName": base, "mergeCommit": {"oid": oid}}


def test_on_main():
    assert sdlc.on_main([merged_pr(HEAD), merged_pr(OLD)], set())                # #12 M1: built the old way
    assert not sdlc.on_main([merged_pr(HEAD), merged_pr(OLD)], {OLD})            # #12 M2 after its reverts
    assert not sdlc.on_main([merged_pr(HEAD, base="epic/12")], set())            # built on the epic branch
    assert not sdlc.on_main([merged_pr(HEAD), None], set())
    assert not sdlc.on_main([], set())


def test_carried_slices():
    t11, t31 = {"number": 28, "key": "T1.1", "milestone": "#12 M1: x"}, {"number": 31, "key": "T3.1", "milestone": "#12 M2: y"}
    slice_of = {HEAD: t11, OLD: t31}
    one = lambda sha, body="slice", parents=1: {"sha": sha, "parents": ["p"] * parents, "body": body,  # noqa: E731
                                                 "clean": True, "from_main": True}
    assert sdlc.carried_slices([one(HEAD), one("m" * 40, "Merge main into epic/12", 2)], slice_of, {"M1"}) == []
    assert sdlc.carried_slices([one(HEAD), one(OLD)], slice_of, {"M1"}) == ["#31 T3.1 (M2)"]
    # the #12 repair: M2's slices re-applied with `cherry-pick -x` still trace to M2, and so do reverts
    picked = one("c" * 40, f"T3.1: writes\n\n(cherry picked from commit {OLD})")
    assert sdlc.carried_slices([picked], slice_of, {"M1"}) == ["#31 T3.1 (M2)"]
    assert sdlc.carried_slices([picked], slice_of, {"M1", "M2"}) == []
    assert sdlc.carried_slices([one("d" * 40, f'Revert "T3.1"\n\nThis reverts commit {OLD}.')], slice_of, {"M1"})
    assert sdlc.carried_slices([one("e" * 40, "a hand edit")], slice_of, {"M1", "M2"}) == ["eeeeeeeeeeee (a hand edit: no slice)"]


def test_unreleased_commits_reads_parents_and_messages(repo):
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    a = commit_on_epic(repo, "a.py", "a = 1\n")
    commit_on_main(repo, "m.py", "m = 1\n")
    sdlc.sync_epic(12, CONFIG, repo, test=PASS)
    sdlc.epic_worktree(12, CONFIG, repo)
    got = sdlc.unreleased_commits(12, CONFIG, repo)
    assert [len(c["parents"]) for c in got] == [2, 1] and got[1]["sha"] == a and got[1]["body"].startswith("slice: a.py")


# --- the ship gate -----------------------------------------------------------------

def test_ship_target_goes_in_order_and_needs_acceptance():
    p = progress(ms("M1", 3, 3), ms("M2", 2, 2))
    with pytest.raises(sdlc.SdlcError, match="isn't accepted"):
        sdlc.ship_target(derive([DEMO1]), p)
    st = derive([DEMO1, ACCEPT1])
    assert sdlc.ship_target(st, p)["key"] == "M1"
    with pytest.raises(sdlc.SdlcError, match="ships first"):
        sdlc.ship_target(st, p, "M2")
    done = derive([DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD)])
    with pytest.raises(sdlc.SdlcError, match="M2: x isn't accepted"):
        sdlc.ship_target(done, p)


def ready_to_ship(*extra):
    return derive([DEMO1, ACCEPT1, agent("epic-tests", None, 14, sha=HEAD, result="pass", passed="9"),
                   agent("ship-review", None, 15, milestone="M1", sha=HEAD, verdict="approve", round="1"), *extra])


def test_ship_gate_passes():
    assert sdlc.ship_gate_errors(ready_to_ship(), "M1", HEAD, True, []) == []


@pytest.mark.parametrize("st,head,synced,carried,needle", [
    (None, HEAD, False, [], "does not contain main"),
    (None, OLD, True, [], "no recorded test run on the epic head"),
    ("failed", HEAD, True, [], "tests failed"),
    ("changes", HEAD, True, [], "asks for changes"),
    ("other", HEAD, True, [], "no Codex review of M1"),
    (None, HEAD, True, ["#31 T3.1 (M2)"], "carries slices"),
])
def test_ship_gate_refusals(st, head, synced, carried, needle):
    state = {None: ready_to_ship(),
             "failed": ready_to_ship(agent("epic-tests", None, 16, sha=HEAD, result="fail", passed="8")),
             "changes": ready_to_ship(agent("ship-review", None, 16, milestone="M1", sha=HEAD, verdict="changes", round="2")),
             "other": derive([DEMO1, ACCEPT1, agent("epic-tests", None, 14, sha=HEAD, result="pass", passed="9"),
                              agent("ship-review", None, 15, milestone="M2", sha=HEAD, verdict="approve", round="1")]),
             }[st]
    errs = sdlc.ship_gate_errors(state, "M1", head, synced, carried)
    assert any(needle in e for e in errs), errs


def test_ship_review_binds_to_the_epic_head(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    b = tmp_path / "b.json"
    b.write_text(json.dumps({"issue": {"number": 12, "title": "t", "state": "open", "author": POSTER,
                                       "labels": ["sdlc:in-progress"]},
                             "comments": HISTORY + [DEMO1, ACCEPT1], "trusted": [OWNER],
                             "progress": progress(ms("M1", 3, 3)), "epic_head": HEAD}))
    r = tmp_path / "r.md"
    r.write_text(f"HEAD: {HEAD}\n1. fine\n\nVERDICT: approve\n")
    assert sdlc.main(["ship-review", "12", "--report", str(r), "--from-file", str(b), "--dry-run"]) == 0
    assert f"kind=ship-review milestone=M1 sha={HEAD} verdict=approve round=1" in capsys.readouterr().out
    r.write_text(f"HEAD: {OLD}\n\nVERDICT: approve\n")
    assert sdlc.main(["ship-review", "12", "--report", str(r), "--from-file", str(b), "--dry-run"]) == 1
    assert "record each round before pushing" in capsys.readouterr().err


# --- slices land on the epic branch ---------------------------------------------------

def test_merge_gate_wants_the_epic_branch():
    pr = {"number": 50, "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefName": "slice/28",
          "headRefOid": HEAD, "mergeable": "MERGEABLE", "body": "Closes #28"}
    issue = {"number": 28, "state": "open", "labels": ["plan:slice"], "body": ""}
    ok = [{"sha": HEAD, "result": "pass", "verdict": "approve"}]
    errs = sdlc.merge_gate_errors(pr, issue, ok, ok, "epic/12", 0)
    assert errs == ["PR targets main, not epic/12"]
    assert sdlc.merge_gate_errors({**pr, "baseRefName": "epic/12"}, issue, ok, ok, "epic/12", 0) == []
    assert any("rebase onto origin/epic/12" in e for e in sdlc.merge_gate_errors(
        {**pr, "baseRefName": "epic/12"}, issue, ok, ok, "epic/12", 3))


def test_only_the_earliest_unshipped_milestone_lands():
    building = progress(ms("M1", 1, 3), ms("M2", 0, 4))
    # even before M1 is complete (`create: all`), nothing of M2 may land: shipping M1 would carry it
    assert "M2 waits: #12 M1: x is still being built" in sdlc.milestone_hold_errors(derive(), building, "#12 M2: y")[0]
    p = progress(ms("M1", 3, 3), ms("M2", 1, 4))
    for st in (derive([DEMO1]), derive([DEMO1, ACCEPT1])):        # waiting for its demo, or for its release
        assert sdlc.milestone_hold_errors(st, p, "#12 M1: x") == []           # M1's own fix slices still land
        assert sdlc.milestone_hold_errors(st, p, "#12 M2: y")
    shipped = derive([DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD)])
    assert sdlc.milestone_hold_errors(shipped, p, "#12 M2: y") == []


def test_next_offers_only_the_current_milestones_slices():
    st = derive()
    units = {30: {"key": "T1.3", "milestone": "M1"}, 35: {"key": "T4.1", "milestone": "M2"}}
    p = {**progress(ms("M1", 2, 3), ms("M2", 0, 4), ready=(35,), in_flight=(30,)), "units": units}
    assert sdlc.next_command(st, p).startswith("nothing new to start: in progress #30")
    stuck = {**p, "in_flight": []}
    assert sdlc.next_command(st, stuck).startswith("nothing ready in #12 M1: x: later milestones' slices wait")
    assert sdlc.next_command(st, {**p, "ready": [30, 35], "in_flight": []}) == "/work-slice 30"


def test_findings_after_acceptance_void_it_and_hold_the_release():
    st = derive([DEMO1, ACCEPT1, agent("demo-changes", None, 13, milestone="M1", found="agent")])
    assert not st["demos"]["M1"]["accepted"] and st["action"] == "plan"
    with pytest.raises(sdlc.SdlcError, match="changes being planned"):
        sdlc.ship_target(st, progress(ms("M1", 3, 3)))
    fixed = derive([DEMO1, ACCEPT1, agent("demo-changes", None, 13, milestone="M1", found="agent"),
                    agent("plan", 2, 14), agent("plan-review", 2, 15, verdict="approve"), agent("plan-created", 2, 16)])
    with pytest.raises(sdlc.SdlcError, match="isn't accepted yet"):               # a new demo comes first
        sdlc.ship_target(fixed, progress(ms("M1", 4, 4)))


def test_an_interrupted_ship_is_recorded_from_its_merged_pr():
    merged = [{"title": "Ship #12 M1: See every alarm", "mergeCommit": {"oid": HEAD}},
              {"title": "Ship #13 M1: other epic", "mergeCommit": {"oid": OLD}},
              {"title": "Ship #12 M2: Set alarms", "mergeCommit": {"oid": OLD}}]
    assert sdlc.unrecorded_ships(merged, 12, {"M2": OLD}) == {"M1": HEAD}
    done = derive([DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD)])
    assert sdlc.ship_target(done, progress(ms("M1", 3, 3), open_=0)) is None     # then `ship` finishes the epic


def test_next_asks_for_a_sync_only_when_idle():
    st = derive([DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD)])
    idle = progress(ms("M1", 3, 3), ms("M2", 1, 4), behind=4)
    assert sdlc.next_command(st, idle).startswith("`uv run python tools/sdlc.py sync 12`: epic/12 is 4 commit(s) behind")
    assert sdlc.next_command(st, {**idle, "ready": [35]}) == "/work-slice 35"
    assert sdlc.next_command(st, {**idle, "behind_main": 0}).startswith("nothing ready")


# --- reverting one slice --------------------------------------------------------------

def test_a_reverted_slice_voids_its_milestones_demo():
    st = derive([DEMO1, ACCEPT1, agent("demo-void", None, 14, milestone="M1")])
    assert not st["demos"]["M1"]["accepted"] and st["demos"]["M1"]["voided"] and st["demos"]["M1"]["rev"] == 1


def test_revert_slice_errors():
    issue = {"number": 28, "state": "closed", "state_reason": "completed", "body": sdlc.marker("slice", None, epic="12", key="T1")}
    pr = merged_pr(HEAD, "epic/12")
    assert sdlc.revert_slice_errors(issue, pr, True, False) == []
    assert "already shipped" in sdlc.revert_slice_errors(issue, pr, True, True)[0]
    assert "isn't on epic/12" in sdlc.revert_slice_errors(issue, pr, False, False)[0]
    assert "isn't merged" in sdlc.revert_slice_errors({**issue, "state": "open"}, pr, True, False)[0]
    assert "no merged pull request" in sdlc.revert_slice_errors(issue, None, False, False)[0]


def test_revert_on_the_epic_worktree(repo):
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    bad = commit_on_epic(repo, "bad.py", "bad = 1\n")
    wt = sdlc.epic_worktree(12, CONFIG, repo)
    assert (wt / "bad.py").exists()
    _git("revert", "--no-edit", bad, cwd=wt)                       # what revert-slice does, then tests and pushes
    _git("push", "-q", "origin", "HEAD:refs/heads/epic/12", cwd=wt)
    wt = sdlc.epic_worktree(12, CONFIG, repo)                      # a fresh checkout of what was pushed
    assert not (wt / "bad.py").exists() and sdlc.is_ancestor(bad, "origin/epic/12", repo)


# --- the commands themselves, GitHub stubbed, git real -----------------------------------

class GitHub:
    """Records every gh call and comment; answers the reads a test sets up."""

    def __init__(self, monkeypatch, root=None):
        self.calls, self.posted, self.labels = [], [], []
        self.issue_state = "OPEN"
        monkeypatch.setattr(sdlc, "gh", self.gh)
        monkeypatch.setattr(sdlc, "gh_json", self.gh_json)
        monkeypatch.setattr(sdlc, "post_comment", lambda n, body, cfg: self.posted.append((n, body)) or {"html_url": "u"})
        monkeypatch.setattr(sdlc, "set_state_label", lambda n, cur, to, cfg: self.labels.append((n, to)))
        monkeypatch.setattr(sdlc, "fetch_trusted", lambda cfg: {OWNER})
        if root:
            monkeypatch.setattr(sdlc, "primary_root", lambda: root)

    def gh(self, args, check=True):
        self.calls.append(args)
        if args[:2] == ["issue", "close"] and self.closes:
            self.issue_state = "CLOSED"
        if args[:2] == ["issue", "reopen"] and self.fail_reopen:
            self.fail_reopen = False
            raise sdlc.SdlcError("gh issue reopen failed: HTTP 502")
        return ""

    closes, fail_reopen = True, False

    def gh_json(self, args):
        if args[:2] == ["issue", "view"]:
            return {"state": self.issue_state}
        return []

    def kinds(self):
        return [(n, sdlc.parse_marker(b)["kind"]) for n, b in self.posted]


def epic_bundle(comments=(), labels=("sdlc:in-progress",)):
    issue = {"number": 12, "title": "Alarm manager", "state": "open", "author": POSTER, "labels": list(labels)}
    return {"issue": issue, "comments": HISTORY + list(comments), "trusted": [OWNER]}


SLICE_BODY = sdlc.marker("slice", None, epic="12", key="T1.1") + "\nEpic: #12\n"


def slice_issue(state="open", reason=None, body=SLICE_BODY, ms="#12 M1: x"):
    return {"number": 28, "title": "#12 T1.1: t", "state": state, "state_reason": reason, "body": body,
            "labels": ["plan:slice"], "assignees": [], "milestone": ms}


def slice_pr(base="epic/12"):
    return {"number": 50, "state": "OPEN", "isDraft": False, "baseRefName": base, "headRefName": "slice/28",
            "headRefOid": HEAD, "mergeable": "MERGEABLE", "body": "Closes #28"}


PR_RECORDS = [agent("tests", None, 1, sha=HEAD, result="pass", passed="9"),
              agent("pr-review", None, 2, sha=HEAD, verdict="approve", round="1")]


def stub_merge(monkeypatch, gh, reads, base="epic/12"):
    seq = iter(reads)
    monkeypatch.setattr(sdlc, "fetch_pr", lambda n, cfg: (slice_pr(base), PR_RECORDS))
    monkeypatch.setattr(sdlc, "fetch_slice", lambda n, cfg: (next(seq), []))
    monkeypatch.setattr(sdlc, "gh_json", lambda args: {"behind_by": 0})
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, cfg, trusted=None: epic_bundle())
    monkeypatch.setattr(sdlc, "epic_progress", lambda n, cfg, trusted: progress(ms("M1", 1, 3)))
    monkeypatch.setattr(sdlc, "with_next", lambda st, cfg, offline=False, trusted=None: {**st, "next": "x", "progress": None})


def test_merge_squashes_into_the_epic_then_closes_and_reads_back(monkeypatch, capsys):
    gh = GitHub(monkeypatch)
    stub_merge(monkeypatch, gh, [slice_issue(), slice_issue(), slice_issue("closed", "completed")])
    assert sdlc.main(["merge", "50"]) == 0
    assert ["pr", "merge", "50", "--repo", CONFIG["repository"], "--squash", "--match-head-commit", HEAD] in gh.calls
    assert any(c[:3] == ["issue", "close", "28"] and "completed" in c for c in gh.calls)
    assert "into epic/12; #28 closed" in capsys.readouterr().out


def test_merge_fails_loudly_when_the_slice_wont_close(monkeypatch, capsys):
    gh = GitHub(monkeypatch)
    stub_merge(monkeypatch, gh, [slice_issue(), slice_issue(), slice_issue()])
    assert sdlc.main(["merge", "50"]) == 1
    assert "reads back open" in capsys.readouterr().err


def test_merge_refuses_a_pr_aimed_at_main(monkeypatch, capsys):
    gh = GitHub(monkeypatch)
    stub_merge(monkeypatch, gh, [slice_issue()], base="main")
    assert sdlc.main(["merge", "50"]) == 1
    assert "PR targets main, not epic/12" in capsys.readouterr().err
    assert not any(c[:2] == ["pr", "merge"] for c in gh.calls)


def test_merge_closes_the_debt_a_cleanup_slice_pays_down(monkeypatch):
    gh = GitHub(monkeypatch)
    body = SLICE_BODY + "\n## Pays down\n\n#81, #84: tech debt filed against this epic.\n"
    stub_merge(monkeypatch, gh, [slice_issue(body=body), slice_issue(body=body),
                                 slice_issue("closed", "completed", body=body)])
    assert sdlc.main(["merge", "50"]) == 0
    closed = [c[2] for c in gh.calls if c[:2] == ["issue", "close"]]
    assert closed == ["28", "81", "84"]


CLAIMED = [agent("claim", None, 1, branch="slice/28")]


def stub_revert(monkeypatch, repo, oid, comments=(), labels=("sdlc:in-progress",), slice_state=("closed", "completed")):
    monkeypatch.setattr(sdlc, "fetch_slice", lambda n, cfg: (slice_issue(*slice_state), CLAIMED))
    monkeypatch.setattr(sdlc, "find_slice_pr", lambda n, cfg: {"number": 50, "state": "MERGED", "baseRefName": "epic/12",
                                                               "mergeCommit": {"oid": oid}})
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, cfg, trusted=None: epic_bundle(comments, labels))


def test_revert_slice_pushes_only_a_tested_revert(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    bad = commit_on_epic(repo, "bad.py", "bad = 1\n")
    stub_revert(monkeypatch, repo, bad, [DEMO1, ACCEPT1])
    monkeypatch.setattr(sdlc, "run_suite", FAIL)
    assert sdlc.main(["revert-slice", "28"]) == 1
    assert "nothing pushed" in capsys.readouterr().err
    assert origin_sha(repo, "epic/12") == bad and gh.calls == [] and gh.posted == []
    monkeypatch.setattr(sdlc, "run_suite", PASS)
    assert sdlc.main(["revert-slice", "28", "--reason", "rang twice"]) == 0
    head = origin_sha(repo, "epic/12")
    assert head != bad and sdlc.is_ancestor(bad, head, repo)
    assert not (sdlc.epic_worktree(12, CONFIG, repo) / "bad.py").exists()
    assert ["issue", "reopen", "28", "--repo", CONFIG["repository"]] in gh.calls
    # the demo is voided before the revert is pushed: an acceptance never outlives its slice
    assert gh.kinds() == [(12, "demo-void"), (28, "revert"), (28, "release"), (12, "epic-tests")]


def test_revert_slice_finishes_a_run_cut_short_after_its_push(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    bad = commit_on_epic(repo, "bad.py", "bad = 1\n")
    stub_revert(monkeypatch, repo, bad, [DEMO1, ACCEPT1])
    monkeypatch.setattr(sdlc, "run_suite", PASS)
    gh.fail_reopen = True
    assert sdlc.main(["revert-slice", "28"]) == 1                 # voided and pushed, then GitHub failed
    pushed = origin_sha(repo, "epic/12")
    assert pushed != bad and gh.kinds() == [(12, "demo-void")]
    gh.posted.clear()
    stub_revert(monkeypatch, repo, bad, [DEMO1, ACCEPT1, agent("demo-void", None, 14, milestone="M1")])
    monkeypatch.setattr(sdlc, "run_suite", lambda wt: pytest.fail("the revert is already tested and pushed"))
    assert sdlc.main(["revert-slice", "28"]) == 0                 # a rerun only finishes the records
    assert origin_sha(repo, "epic/12") == pushed                  # no second revert, no second void
    assert gh.kinds() == [(28, "revert"), (28, "release")]
    # and once the slice is open again, a third run finds nothing missing but the void it already has
    stub_revert(monkeypatch, repo, bad, [DEMO1, ACCEPT1, agent("demo-void", None, 14, milestone="M1")], slice_state=("open",))
    gh.posted.clear()
    assert sdlc.main(["revert-slice", "28"]) == 0 and gh.kinds() == [(28, "revert"), (28, "release")]


def test_revert_slice_refuses_what_has_shipped(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    on_main = _git("rev-parse", "HEAD", cwd=repo)
    stub_revert(monkeypatch, repo, on_main)
    assert sdlc.main(["revert-slice", "28"]) == 1
    assert "already shipped to main" in capsys.readouterr().err and gh.calls == []


def stub_ship(monkeypatch, comments, leaves, pr_base="main", merged_ships=()):
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, cfg, trusted=None: epic_bundle(comments))
    monkeypatch.setattr(sdlc, "fetch_plan_issues", lambda n, cfg, trusted: leaves)
    monkeypatch.setattr(sdlc, "find_slice_pr", lambda n, cfg: {"number": 50 + n, "state": "MERGED", "baseRefName": pr_base,
                                                               "mergeCommit": {"oid": f"{n:040d}"}})
    monkeypatch.setattr(sdlc, "find_ship_pr", lambda n, cfg, state="open": list(merged_ships) if state == "merged" else [])
    monkeypatch.setattr(sdlc, "with_next", lambda st, cfg, offline=False, trusted=None: {**st, "next": "x", "progress": None})


def done_leaf(n, key, m="#12 M1: x"):
    return {"kind": "slice", "number": n, "key": key, "title": key, "state": "closed", "state_reason": "completed",
            "labels": ["plan:slice"], "assignees": [], "milestone": m, "body": ""}


def test_ship_records_a_milestone_already_on_main_without_merging(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    stub_ship(monkeypatch, [DEMO1, ACCEPT1], [done_leaf(28, "T1.1"), done_leaf(29, "T1.2"), done_leaf(31, "T3.1", "#12 M2: y")])
    monkeypatch.setattr(sdlc, "reverted_on_main", lambda cfg, root=None: set())
    assert sdlc.main(["ship", "12"]) == 0
    assert "built straight onto main" in capsys.readouterr().out
    assert gh.kinds() == [(12, "shipped")] and "noop=1" in gh.posted[0][1]
    assert not any(c[:2] in (["pr", "merge"], ["pr", "create"]) for c in gh.calls)


def test_ship_review_refuses_a_milestone_already_on_main(repo, monkeypatch, capsys, tmp_path):
    # #12 M1: main...epic/12 is M2's diff, so a "review of M1" would judge the wrong code
    gh = GitHub(monkeypatch, repo)
    leaves = [done_leaf(28, "T1.1"), done_leaf(29, "T1.2"), done_leaf(31, "T3.1", "#12 M2: y")]
    stub_ship(monkeypatch, [DEMO1, ACCEPT1], leaves)
    monkeypatch.setattr(sdlc, "epic_progress", lambda n, cfg, trusted: progress(ms("M1", 2, 2), ms("M2", 1, 1)))
    monkeypatch.setattr(sdlc, "epic_head", lambda n, cfg: HEAD)
    r = tmp_path / "r.md"
    r.write_text(f"HEAD: {HEAD}\n\nVERDICT: approve\n")
    monkeypatch.setattr(sdlc, "reverted_on_main", lambda cfg, root=None: set())
    assert sdlc.main(["ship-review", "12", "--report", str(r)]) == 1
    assert "nothing of it to review; `ship 12`" in capsys.readouterr().err and gh.posted == []
    # once its slices are reverted on main (or it was built on epic/12), the review is wanted again
    monkeypatch.setattr(sdlc, "reverted_on_main", lambda cfg, root=None: {f"{28:040d}"})
    assert sdlc.main(["ship-review", "12", "--report", str(r)]) == 0
    assert gh.kinds() == [(12, "ship-review")]


def test_ship_finishes_the_epic_and_can_finish_it_again(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    shipped = [DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD)]
    stub_ship(monkeypatch, shipped, [done_leaf(28, "T1.1")])
    gh.closes = False                               # GitHub won't close it: that's an error, not "done"
    assert sdlc.main(["ship", "12"]) == 1
    assert "won't close" in capsys.readouterr().err and not sdlc.remote_has("epic/12", repo)
    gh.closes = True
    stub_ship(monkeypatch, shipped, [done_leaf(28, "T1.1")])
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, cfg, trusted=None: epic_bundle(shipped, labels=("sdlc:done",)))
    assert sdlc.main(["ship", "12"]) == 0           # done but still open: a rerun closes it
    assert gh.issue_state == "CLOSED" and (12, "done") in gh.labels


def test_ship_records_a_release_pr_that_merged_without_its_record(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    merged = [{"title": "Ship #12 M1: x", "mergeCommit": {"oid": HEAD}}]
    stub_ship(monkeypatch, [DEMO1, ACCEPT1], [done_leaf(28, "T1.1")], pr_base="epic/12", merged_ships=merged)
    monkeypatch.setattr(sdlc, "release_untested", lambda repo_, sha: False)
    assert sdlc.main(["ship", "12"]) == 0
    assert gh.kinds() == [(12, "shipped")] and f"sha={HEAD}" in gh.posted[0][1]
    assert (12, "done") in gh.labels


def test_ship_stops_when_main_moves_before_the_merge(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    head = commit_on_epic(repo, "a.py", "a = 1\n")
    records = [DEMO1, ACCEPT1, agent("epic-tests", None, 14, sha=head, result="pass", passed="9"),
               agent("ship-review", None, 15, milestone="M1", sha=head, verdict="approve", round="1")]
    stub_ship(monkeypatch, records, [done_leaf(28, "T1.1"), done_leaf(31, "T3.1", "#12 M2: y")], pr_base="epic/12")
    monkeypatch.setattr(sdlc, "reverted_on_main", lambda cfg, root=None: set())
    open_pr = {"number": 70, "headRefOid": head, "mergeable": "MERGEABLE", "title": "Ship #12 M1: x"}
    monkeypatch.setattr(sdlc, "find_ship_pr", lambda n, cfg, state="open": [] if state == "merged" else [open_pr])
    answers = {"pr list": [{"number": 60, "headRefName": "slice/28", "mergeCommit": {"oid": head}}],
               "branches/main": {"commit": {"sha": "f" * 40}}}
    monkeypatch.setattr(sdlc, "gh_json", lambda args: answers["pr list"] if args[:2] == ["pr", "list"] else answers["branches/main"])
    assert sdlc.main(["ship", "12"]) == 1
    assert "main moved during the ship" in capsys.readouterr().err
    assert not any(c[:2] == ["pr", "merge"] for c in gh.calls) and gh.posted == []


def test_ship_refuses_a_branch_carrying_a_later_milestone(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    later = commit_on_epic(repo, "m2.py", "m2 = 1\n")
    records = [DEMO1, ACCEPT1, agent("epic-tests", None, 14, sha=later, result="pass", passed="9"),
               agent("ship-review", None, 15, milestone="M1", sha=later, verdict="approve", round="1")]
    stub_ship(monkeypatch, records, [done_leaf(28, "T1.1"), {**done_leaf(31, "T3.1", "#12 M2: y"), "state": "open"}],
              pr_base="epic/12")
    monkeypatch.setattr(sdlc, "reverted_on_main", lambda cfg, root=None: set())
    into_epic = [{"number": 60, "headRefName": "slice/31", "mergeCommit": {"oid": later}}]   # its issue never closed
    monkeypatch.setattr(sdlc, "gh_json", lambda args: into_epic)
    assert sdlc.main(["ship", "12", "--dry-run"]) == 1
    assert "carries slices of milestones that aren't accepted: #31 T3.1 (M2)" in capsys.readouterr().err


def test_a_retry_gets_a_fresh_review_budget_once():
    before = [agent("pr-review", None, i, sha=OLD, verdict="changes", round=str(i)) for i in range(1, 6)]
    _, reviews = sdlc.pr_records(before + [agent("retry", None, 7, model="opus")], {OWNER})
    assert sdlc.changes_rounds(reviews) == 0 and len(reviews) == 5
    _, reviews = sdlc.pr_records(before + [agent("retry", None, 7, model="opus"),
                                           agent("pr-review", None, 8, sha=HEAD, verdict="changes", round="6")], {OWNER})
    assert sdlc.changes_rounds(reviews) == 1
    escalated = {"number": 28, "state": "open", "labels": ["plan:slice", "sdlc:escalated"]}
    assert sdlc.retry_errors(escalated, [], {OWNER}) == []
    assert "already retried" in sdlc.retry_errors(escalated, [agent("claim", None, 1, retry="1", model="opus")], {OWNER})[0]
    assert "isn't escalated" in sdlc.retry_errors({**escalated, "labels": ["plan:slice"]}, [], {OWNER})[0]


def test_one_sync_at_a_time_and_it_pushes_what_it_tested(repo):
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    commit_on_epic(repo, "a.py", "a = 1\n")
    commit_on_main(repo, "m.py", "m = 1\n")

    def overlapping(wt):     # a second sync starting while the first is testing is refused, not interleaved
        with pytest.raises(sdlc.SdlcError, match="another sync or revert"):
            sdlc.sync_epic(12, CONFIG, repo, test=PASS)
        (wt / "stray.py").write_text("x\n")                      # nothing done in the worktree now gets pushed
        _git("add", "stray.py", cwd=wt)
        _git("commit", "-q", "-m", "stray", cwd=wt)
        return PASS(wt)

    out = sdlc.sync_epic(12, CONFIG, repo, test=overlapping)
    assert out["pushed"] and origin_sha(repo, "epic/12") == out["head"]


def test_a_reopened_slice_beats_an_old_acceptance():
    st = derive([DEMO1, ACCEPT1])
    phases = sdlc.milestone_phases(st, progress(ms("M1", 2, 3)))
    assert phases[0]["phase"] == "building"
    with pytest.raises(sdlc.SdlcError, match="not complete"):
        sdlc.ship_target(st, progress(ms("M1", 2, 3)))


# --- round 5: merges, releases, dry runs, demo-status -----------------------------------------

def test_a_merge_must_be_clean_and_of_main():
    t11 = {"number": 28, "key": "T1.1", "milestone": "#12 M1: x"}
    merge = lambda **kw: {"sha": "m" * 40, "parents": ["a" * 40, "b" * 40], "body": "Merge main", **kw}  # noqa: E731
    assert sdlc.carried_slices([merge(clean=True, from_main=True)], {HEAD: t11}, {"M1"}) == []
    assert "changes of its own" in sdlc.carried_slices([merge(clean=False, from_main=True)], {}, {"M1"})[0]
    assert "other than main" in sdlc.carried_slices([merge(clean=True, from_main=False)], {}, {"M1"})[0]
    assert sdlc.carried_slices([merge(clean=False, from_main=True)], {}, {"M1"}, {"m" * 12}) == []   # a human reviewed it


def test_unreleased_commits_tells_a_clean_sync_from_an_evil_merge(repo):
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    commit_on_epic(repo, "a.py", "a = 1\n")
    commit_on_main(repo, "m.py", "m = 1\n")
    sdlc.sync_epic(12, CONFIG, repo, test=PASS)
    clean = [c for c in sdlc.unreleased_commits(12, CONFIG, repo) if len(c["parents"]) > 1][0]
    assert clean["clean"] and clean["from_main"]
    wt = sdlc.epic_worktree(12, CONFIG, repo)          # the merge amended to smuggle in a file of its own
    (wt / "extra.py").write_text("sneaky = 1\n")
    _git("add", "extra.py", cwd=wt)
    _git("commit", "-q", "--amend", "--no-edit", cwd=wt)
    _git("push", "-q", "-f", "origin", "HEAD:refs/heads/epic/12", cwd=wt)
    _git("fetch", "-q", "origin", "epic/12", cwd=repo)
    evil = [c for c in sdlc.unreleased_commits(12, CONFIG, repo) if len(c["parents"]) > 1][0]
    assert not evil["clean"]


def test_a_release_holds_exactly_the_tested_head(monkeypatch):
    trees = {"merge": "t1", "head": "t1"}
    def fake(args):
        sha = args[1].rsplit("/", 1)[1]
        if sha == "merge":
            return {"parents": [{"sha": "main"}, {"sha": "head"}], "commit": {"tree": {"sha": trees["merge"]}}}
        return {"parents": [], "commit": {"tree": {"sha": trees["head"]}}}
    monkeypatch.setattr(sdlc, "gh_json", fake)
    assert not sdlc.release_untested("o/r", "merge")
    trees["merge"] = "t2"                              # main moved: the merge holds more than was tested
    assert sdlc.release_untested("o/r", "merge")


def test_a_recovered_release_that_main_moved_under_is_escalated(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    merged = [{"title": "Ship #12 M1: x", "mergeCommit": {"oid": HEAD}}]
    stub_ship(monkeypatch, [DEMO1, ACCEPT1], [done_leaf(28, "T1.1")], pr_base="epic/12", merged_ships=merged)
    monkeypatch.setattr(sdlc, "release_untested", lambda repo_, sha: True)
    assert sdlc.main(["ship", "12"]) == 1
    assert [k for _, k in gh.kinds()] == ["shipped", "escalation"] and "untested=1" in gh.posted[0][1]
    assert (12, "escalated") in gh.labels and sdlc.remote_has("epic/12", repo)     # not finished


def test_sync_dry_run_leaves_the_worktree_alone(repo, monkeypatch, capsys):
    GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    commit_on_main(repo, "m.py", "m = 1\n")
    assert sdlc.main(["sync", "12", "--dry-run"]) == 0
    assert "1 commit(s) behind main" in capsys.readouterr().out
    assert not (repo / ".worktrees" / "epic-12").exists()


def test_demo_status_and_state_agree_on_next(monkeypatch):
    shipped = [DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD)]
    leaves = [done_leaf(28, "T1.1"), {**done_leaf(31, "T3.1", "#12 M2: y"), "state": "open", "state_reason": None}]
    p = {**sdlc.summarise_progress(leaves, {}), "behind_main": 0, "debt": [81], "cleanup_budget": 1}
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, cfg, trusted=None: epic_bundle(shipped))
    monkeypatch.setattr(sdlc, "fetch_plan_issues", lambda n, cfg, trusted: leaves)
    monkeypatch.setattr(sdlc, "epic_progress", lambda n, cfg, trusted: p)
    monkeypatch.setattr(sdlc, "gh_pages", lambda path: [])
    monkeypatch.setattr(sdlc, "find_slice_pr", lambda n, cfg: None)
    monkeypatch.setattr(sdlc, "reverted_on_main", lambda cfg, root=None: set())
    brief = sdlc.demo_brief(12, CONFIG, {OWNER})
    st = sdlc.with_next(sdlc.bundle_state(epic_bundle(shipped), CONFIG), CONFIG, trusted={OWNER})
    assert brief["next"] == st["next"] and brief["next"].startswith("/plan-issue 12 --cleanup")


def test_an_untested_release_must_be_verified_before_the_epic_finishes():
    untested = derive([DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD, untested="1")])
    assert untested["untested"] == {"M1": HEAD}
    p = progress(ms("M1", 3, 3), open_=0)
    view = sdlc.epic_view(untested, p)
    assert view["due"] == "verify" and view["next"].startswith("`uv run python tools/sdlc.py verify-main 12`")
    assert sdlc.pause_errors(view)
    assert not sdlc.finished(p, untested["demos"], untested["shipped"], [], untested["untested"])
    failed = derive([DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD, untested="1"),
                     agent("main-tests", None, 14, sha="f" * 40, result="fail", covers=HEAD)])
    assert failed["untested"]                                     # a failing run verifies nothing
    verified = derive([DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD, untested="1"),
                       agent("main-tests", None, 14, sha="f" * 40, result="pass", covers=HEAD)])
    assert verified["untested"] == {} and sdlc.finished(p, verified["demos"], verified["shipped"], [], {})


def test_an_interrupted_untested_ship_does_not_finish_on_rerun(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    stub_ship(monkeypatch, [DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD, untested="1")],
              [done_leaf(28, "T1.1")])
    assert sdlc.main(["ship", "12"]) == 1
    assert "verify-main 12" in capsys.readouterr().err and sdlc.remote_has("epic/12", repo) and gh.labels == []


def test_an_owed_cleanup_pass_holds_ordinary_claims():
    st = derive([DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD)])
    p = {**progress(ms("M1", 3, 3), ms("M2", 0, 2), ready=(35,)), "debt": [81], "cleanup_budget": 1,
         "units": {35: {"key": "T4.1", "milestone": "M2"}}}
    view = sdlc.epic_view(st, p)
    assert view["due"] == "cleanup"
    assert "cleanup pass after M1 is owed" in sdlc.claim_errors(view, 35, "#12 M2: y", cleanup=False)[0]


def test_changes_after_an_approve_void_it():
    st = derive([DEMO1, ACCEPT1, comment(POSTER, "/changes the bell is too loud", 13),
                 agent("demo-changes", 1, 14, milestone="M1")])
    assert not st["demos"]["M1"]["accepted"] and st["feedback"]
    fixed = derive([DEMO1, ACCEPT1, comment(POSTER, "/changes louder", 13), agent("demo-changes", 1, 14, milestone="M1"),
                    agent("plan", 2, 15), agent("plan-review", 2, 16, verdict="approve"), agent("plan-created", 2, 17)])
    with pytest.raises(sdlc.SdlcError, match="isn't accepted yet"):
        sdlc.ship_target(fixed, progress(ms("M1", 4, 4)))


def test_claims_wait_on_an_escalated_epic_and_an_unverified_main():
    body = sdlc.marker("slice", None, epic="12", key="T4.1")
    for labels, comments, needle in ((("sdlc:escalated",), [], "is escalated"),
                                     (("sdlc:in-progress",), [agent("shipped", None, 13, milestone="M1", sha=HEAD,
                                                                   untested="1")], "verify-main 12")):
        st = sdlc.bundle_state(epic_bundle([DEMO1, ACCEPT1, *comments], labels), CONFIG)
        b = {"issue": {"number": 35, "body": body, "milestone": "#12 M2: y"}, "epic_state": st,
             "epic_progress": None, "trusted": [OWNER]}
        errs = sdlc.epic_pause_errors(b, CONFIG, resume=False)
        assert errs and needle in errs[0], errs


def test_nothing_ships_while_an_earlier_release_is_unverified():
    st = derive([DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD, untested="1"),
                 agent("demo", 1, 14, milestone="M2", sha="d" * 40), agent("demo-approval", 1, 15, milestone="M2", by=POSTER)])
    with pytest.raises(sdlc.SdlcError, match="verify-main 12"):
        sdlc.ship_target(st, progress(ms("M1", 3, 3), ms("M2", 2, 2), open_=0))


# --- codex-review --milestone: Codex on main...epic/N, from .worktrees/epic-N -----------------------------

import re  # noqa: E402

from tests.fake_codex import APPROVES, NO_VERDICT, QUICK, SETTINGS, SLOW, calls, install, no_real_codex_home  # noqa: E402,F401


def brief(key, outcome):
    return (sdlc.marker("slice", None, epic="12", key=key) + f"\n## Outcome\n\n{outcome}\n\n## Acceptance criteria\n\n"
            f"- [ ] {key} works\n\n## Non-goals\n\nnone\n")


class MilestoneReview:
    """`.worktrees/epic-12` under a tmp primary checkout: main, then one squash commit per slice of M1. T1.1 had its
    own review; T1.2 is routine and T1.3's PR Codex couldn't review, so both are owed."""

    SLICES = (("T1.1", 28, 55), ("T1.2", 29, 56), ("T1.3", 30, 57))

    def __init__(self, tmp_path, monkeypatch, plan=(APPROVES,), settings=SETTINGS):
        self.tmp, root = tmp_path, tmp_path / "primary"
        self.log, _, _ = install(tmp_path, monkeypatch, plan)
        for k in ("AUTHOR", "COMMITTER"):
            monkeypatch.setenv(f"GIT_{k}_NAME", "t")
            monkeypatch.setenv(f"GIT_{k}_EMAIL", "t@example.invalid")
        self.wt = root / ".worktrees" / "epic-12"
        self.wt.mkdir(parents=True)
        _git("init", "-q", cwd=self.wt)
        _git("commit", "--allow-empty", "-qm", "main", cwd=self.wt)
        _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.wt)
        self.squash = {}
        for key, n, pr_ in self.SLICES:
            (self.wt / f"{key}.py").write_text(f"# {key}\n")
            _git("add", ".", cwd=self.wt)
            _git("commit", "-qm", f"{key} (#{pr_})", cwd=self.wt)
            self.squash[key] = _git("rev-parse", "HEAD", cwd=self.wt)
        self.head = _git("rev-parse", "HEAD", cwd=self.wt)
        monkeypatch.setattr(sdlc, "primary_root", lambda: root)
        monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("codex-review --from-file must not call gh"))
        self.config = tmp_path / "config.json"
        self.config.write_text(json.dumps({**CONFIG, "codex": settings}))
        self.out = tmp_path / "scratch"
        self.owed = [agent("review-owed", None, 20, milestone="M1", slice="T1.2", number="29", why="routine", sha=OLD),
                     agent("review-owed", None, 21, milestone="M1", slice="T1.3", number="30", why="unavailable", sha=OLD)]

    def slices(self):
        return [{"number": n, "key": key, "title": f"<{key}> the {key} part", "body": brief(key, f"{key} outcome"),
                 "pr": pr_, "merge_commit": self.squash[key]} for key, n, pr_ in self.SLICES]

    def bundle(self, comments=(), head=None, slices=None, steps="1. `alarm list` shows every alarm", key="M1"):
        f = self.tmp / "bundle.json"
        f.write_text(json.dumps({"issue": {"number": 12, "title": "Alarm manager", "state": "open", "author": POSTER,
                                           "labels": ["sdlc:in-progress"]},
                                 "comments": HISTORY + self.owed + list(comments), "trusted": [OWNER],
                                 "progress": progress(ms(key, 3, 3)), "epic_head": head or self.head,
                                 "steps": steps, "slices": self.slices() if slices is None else slices}))
        return str(f)

    def run(self, *extra, **kw):
        return sdlc.main(["--config", str(self.config), "codex-review", "--milestone", "12", "--out", str(self.out),
                          "--from-file", self.bundle(**kw), *extra])


def test_a_milestone_round_names_every_slice_and_both_deferred_ones(tmp_path, monkeypatch, capsys):
    r = MilestoneReview(tmp_path, monkeypatch)
    assert r.run() == 0
    prompt = (r.out / "prompt.md").read_text()
    assert not re.search(r"<[A-Za-z/]", prompt) and "HEAD:" not in prompt and "rev-parse" not in prompt
    assert f"`{r.out / 'milestone.md'}`" in prompt and "epic/12" in prompt
    for key, n, pr_ in r.SLICES:
        b = r.out / f"slice-{n}.md"
        assert f"#{n} {key} (PR #{pr_}, squash `{r.squash[key]}`, brief `{b}`)" in prompt
        assert f"## Outcome\n\n{key} outcome" in b.read_text()
    deferred = prompt[prompt.index("merged without a review of their own"):prompt.index("This is\n  their first review")]
    assert "T1.2" in deferred and "routine" in deferred and "T1.3" in deferred and "couldn't run" in deferred
    assert "T1.1" not in deferred
    ms_file = (r.out / "milestone.md").read_text()
    assert "`alarm list` shows every alarm" in ms_file and "T1.3 outcome" in ms_file and "- [ ] T1.2 works" in ms_file
    (call,) = calls(r.log)
    assert call["cwd"] == str(r.wt.resolve()) and call["codex_home"] == str(r.out / "codex-home")
    assert call["entries"] == {"auth.json": False, "config.toml": False}
    saved = (r.out / "codex.md").read_text()
    assert saved.startswith(f"HEAD: {r.head}\nModel: test-model\n")
    capsys.readouterr()
    rep = ["ship-review", "12", "--report", str(r.out / "codex.md"), "--dry-run", "--from-file", r.bundle()]
    assert sdlc.main(rep) == 0
    out = capsys.readouterr().out
    assert f"sha={r.head} verdict=approve round=1 response=0 model=test-model" in out and "Ran `test-model`" in out
    # nothing owed: no deferred bullet at all
    r.owed = []
    assert sdlc.main(["--config", str(r.config), "codex-review", "--milestone", "12", "--out", str(r.out),
                      "--from-file", r.bundle([agent("demo", 1, 30, milestone="M1", sha=r.head),
                                               agent("demo-approval", 1, 31, milestone="M1", by=POSTER)]),
                      "--dry-run"]) == 0
    assert "without a review of their own" not in (r.out / "prompt.md").read_text()


def test_a_milestone_round_refuses_a_dirty_or_stale_epic_worktree(tmp_path, monkeypatch, capsys):
    r = MilestoneReview(tmp_path, monkeypatch)
    (r.wt / "notes.md").write_text("scratch")
    assert r.run() == 1 and "not clean" in capsys.readouterr().err
    (r.wt / "notes.md").unlink()
    assert r.run(head="c" * 40) == 1
    err = capsys.readouterr().err
    assert "is not epic/12's head" in err and "`sync 12`" in err
    assert sdlc.main(["--config", str(r.config), "codex-review", "--milestone", "12", "--out", str(r.wt / "s"),
                      "--from-file", r.bundle()]) == 1
    assert "inside the worktree" in capsys.readouterr().err
    # a slice whose squash isn't on the branch, an owed slice that isn't one of the milestone's
    gone = r.slices()
    gone[0]["merge_commit"] = OLD
    assert r.run(slices=gone) == 1 and "isn't on epic/12" in capsys.readouterr().err
    assert r.run(slices=r.slices()[:2]) == 1 and "T1.3 (#30)" in capsys.readouterr().err
    assert calls(r.log) == [] and not (r.out / "codex.md").exists()


def test_a_milestone_round_two_reads_the_recorded_response_and_retries_once(tmp_path, monkeypatch, capsys):
    r = MilestoneReview(tmp_path, monkeypatch, plan=(NO_VERDICT, APPROVES))
    body, extra = sdlc.review_body("**M1, review 1**", "Codex's review", "1. T1.2 drops an error\n\nVERDICT: changes",
                                   "1. Rebutted: it is returned at alarms/clock.py:40.")
    round1 = comment(OWNER, sdlc.render_comment("ship-review", None, body, CONFIG, milestone="M1", sha=OLD,
                                                verdict="changes", round="1", **extra), 25)
    assert r.run(comments=[round1]) == 0 and len(calls(r.log)) == 2
    prompt, resp = (r.out / "prompt.md").read_text(), r.out / "response-round-1.md"
    assert f"`{resp}`" in prompt and resp.read_text().startswith("1. Rebutted")
    assert "round 2" in capsys.readouterr().out
    bare = agent("ship-review", None, 25, milestone="M1", sha=OLD, verdict="changes", round="1", response="0")
    assert r.run("--dry-run", comments=[bare]) == 1 and "ship-review --response" in capsys.readouterr().err


def test_a_milestone_round_that_times_out_is_retried_once(tmp_path, monkeypatch):
    r = MilestoneReview(tmp_path, monkeypatch, plan=(SLOW, APPROVES), settings=QUICK)
    assert r.run() == 0 and len(calls(r.log)) == 2
    assert "timed out after 1s" in (r.out / "codex.err").read_text() and (r.out / "codex.md").exists()


def test_an_epic_without_milestones_takes_its_steps_from_the_slices_demo_sections(tmp_path, monkeypatch):
    r = MilestoneReview(tmp_path, monkeypatch)
    for o in r.owed:          # the epic's one demo is `all`
        o["body"] = o["body"].replace("milestone=M1", "milestone=all")
    slices = [{**s, "body": s["body"] + f"\n## Demo\n\n`np` shows {s['key']}\n"} for s in r.slices()]
    assert r.run("--dry-run", slices=slices, steps="", key="all") == 0
    steps = (r.out / "milestone.md").read_text().split("## Demo steps", 1)[1]
    assert "- T1.1: `np` shows T1.1" in steps and "- T1.3: `np` shows T1.3" in steps
    assert "T1.2 (PR #56" in (r.out / "prompt.md").read_text().split("merged without a review of their own")[1]
