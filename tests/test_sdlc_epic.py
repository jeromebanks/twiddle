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
    prs = [{**merged_pr(HEAD, "epic/12"), "number": 55, "headRefName": "slice/28"},
           {**merged_pr(OLD, "epic/12"), "number": 60, "headRefName": "slice/31"}]   # #31 may still be open
    assert sdlc.carried_slices(prs, leaves, {HEAD}, {"M1"}) == []
    assert sdlc.carried_slices(prs, leaves, {HEAD, OLD}, {"M1"}) == ["#31 T3.1 (M2)"]
    stray = [{**merged_pr("c" * 40, "epic/12"), "number": 61, "headRefName": "hotfix"}]
    assert sdlc.carried_slices(stray, leaves, {"c" * 40}, {"M1"}) == ["PR #61 (hotfix, not a slice of this epic)"]


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
        monkeypatch.setattr(sdlc, "gh", self.gh)
        monkeypatch.setattr(sdlc, "post_comment", lambda n, body, cfg: self.posted.append((n, body)) or {"html_url": "u"})
        monkeypatch.setattr(sdlc, "set_state_label", lambda n, cur, to, cfg: self.labels.append((n, to)))
        monkeypatch.setattr(sdlc, "fetch_trusted", lambda cfg: {OWNER})
        if root:
            monkeypatch.setattr(sdlc, "primary_root", lambda: root)

    def gh(self, args, check=True):
        self.calls.append(args)
        return ""

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


def stub_revert(monkeypatch, repo, oid, comments=(), labels=("sdlc:in-progress",)):
    monkeypatch.setattr(sdlc, "fetch_slice", lambda n, cfg: (slice_issue("closed", "completed"), []))
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
    assert gh.kinds() == [(28, "revert"), (28, "release"), (12, "epic-tests"), (12, "demo-void")]


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
    return {"kind": "slice", "number": n, "key": key, "state": "closed", "state_reason": "completed",
            "labels": ["plan:slice"], "assignees": [], "milestone": m, "body": ""}


def test_ship_records_a_milestone_already_on_main_without_merging(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    stub_ship(monkeypatch, [DEMO1, ACCEPT1], [done_leaf(28, "T1.1"), done_leaf(29, "T1.2"), done_leaf(31, "T3.1", "#12 M2: y")])
    monkeypatch.setattr(sdlc, "reverted_on_main", lambda cfg, root=None: set())
    assert sdlc.main(["ship", "12"]) == 0
    assert "built straight onto main" in capsys.readouterr().out
    assert gh.kinds() == [(12, "shipped")] and "noop=1" in gh.posted[0][1]
    assert not any(c[:2] in (["pr", "merge"], ["pr", "create"]) for c in gh.calls)


def test_ship_finishes_the_epic_and_can_finish_it_again(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    shipped = [DEMO1, ACCEPT1, agent("shipped", None, 13, milestone="M1", sha=HEAD)]
    stub_ship(monkeypatch, shipped, [done_leaf(28, "T1.1")])
    assert sdlc.main(["ship", "12"]) == 0           # the last ship's cleanup was interrupted: this finishes it
    assert not sdlc.remote_has("epic/12", repo) and (12, "done") in gh.labels
    assert any(c[:3] == ["issue", "close", "12"] for c in gh.calls)


def test_ship_records_a_release_pr_that_merged_without_its_record(repo, monkeypatch, capsys):
    gh = GitHub(monkeypatch, repo)
    sdlc.ensure_epic_branch(12, CONFIG, repo)
    merged = [{"title": "Ship #12 M1: x", "mergeCommit": {"oid": HEAD}}]
    stub_ship(monkeypatch, [DEMO1, ACCEPT1], [done_leaf(28, "T1.1")], pr_base="epic/12", merged_ships=merged)
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
    answers = {"pr list": [], "branches/main": {"commit": {"sha": "f" * 40}}}
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
