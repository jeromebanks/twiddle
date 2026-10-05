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
    leaves = [{"number": 28, "key": "T1.1", "milestone": "#12 M1: x"}, {"number": 31, "key": "T3.1", "milestone": "#12 M2: y"}]
    prs = {28: merged_pr(HEAD, "epic/12"), 31: merged_pr(OLD, "epic/12")}
    assert sdlc.carried_slices(leaves, prs, {HEAD}, {"M1"}) == []
    assert sdlc.carried_slices(leaves, prs, {HEAD, OLD}, {"M1"}) == ["#31 T3.1 (M2)"]


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


def test_a_waiting_milestone_holds_back_other_milestones_slices():
    p = progress(ms("M1", 3, 3), ms("M2", 1, 4))
    waiting = derive([DEMO1])
    assert sdlc.milestone_hold_errors(waiting, p, "#12 M1: x") == []          # M1's own fix slices still land
    assert "waiting for its demo" in sdlc.milestone_hold_errors(waiting, p, "#12 M2: y")[0]
    unshipped = derive([DEMO1, ACCEPT1])
    assert "release to main" in sdlc.milestone_hold_errors(unshipped, p, "#12 M2: y")[0]
    shipped = derive([DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD)])
    assert sdlc.milestone_hold_errors(shipped, p, "#12 M2: y") == []


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
