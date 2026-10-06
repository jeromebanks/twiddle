"""tools/sdlc.py Codex reviews that move to the milestone: routine slices (`slice_review`), and slices whose
head Codex couldn't review (`review-defer`). Either way the milestone's review comes before its demo. No network."""
import json

import pytest

from tools import sdlc

CONFIG = sdlc.load_config()
OWNER, POSTER = "owner", "poster"
TRUSTED = {OWNER}
HEAD, OLD = "a" * 40, "b" * 40
_ids = iter(range(1, 10_000))


def comment(author, body, ts):
    return {"id": next(_ids), "author": author, "body": body, "created_at": f"2026-10-06T00:{ts:02d}:00Z",
            "url": f"https://example/c{ts}"}


def agent(kind, rev, ts, body="text", **extra):
    return comment(OWNER, sdlc.marker(kind, rev, **extra) + "\n" + body, ts)


HISTORY = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER), agent("plan", 1, 3),
           agent("plan-review", 1, 4, verdict="approve", round="1"), agent("plan-created", 1, 5)]
OWED = agent("review-owed", None, 20, milestone="M1", slice="T1.2", number="29", why="routine", sha=HEAD)
APPROVE = agent("ship-review", None, 21, milestone="M1", sha=HEAD, verdict="approve", round="1")
CHANGES = agent("ship-review", None, 21, milestone="M1", sha=HEAD, verdict="changes", round="1")


def issue(labels=("sdlc:in-progress",)):
    return {"number": 12, "title": "Alarm manager", "state": "open", "author": POSTER, "labels": list(labels)}


def derive(comments=()):
    return sdlc.derive_state(issue(), HISTORY + list(comments), TRUSTED, CONFIG)


def ms(key, done, total):
    return {"title": f"#12 {key}: x", "key": key, "done": done, "total": total}


def progress(*milestones, ready=()):
    return {"ready": list(ready), "in_flight": [], "escalated": [], "waiting": [], "open": 1,
            "milestones": list(milestones)}


def slice_issue(labels=("plan:slice",)):
    return {"number": 28, "title": "#12 T1.1: t", "state": "open", "state_reason": None, "labels": list(labels),
            "body": sdlc.marker("slice", None, epic="12", key="T1.1") + "\n", "assignees": [], "milestone": "#12 M1: x"}


def pr(**kw):
    return {"number": 50, "state": "OPEN", "isDraft": False, "baseRefName": "epic/12", "headRefName": "slice/28",
            "headRefOid": HEAD, "mergeable": "MERGEABLE", "body": "Closes #28", **kw}


ROUTINE = ("plan:slice", "complexity:routine")
JUDGMENT = ("plan:slice", "complexity:judgment")


# --- which slices are reviewed where --------------------------------------------------------

def test_review_mode_is_per_complexity_and_defaults_to_the_slice():
    assert sdlc.review_mode("routine", CONFIG) == "milestone"
    assert sdlc.review_mode("judgment", CONFIG) == "slice" and sdlc.review_mode("novel", CONFIG) == "slice"
    assert sdlc.review_mode(None, CONFIG) == "slice"                     # unrated (#12's slices): as before
    assert sdlc.review_mode("routine", {}) == "slice"                    # no `slice_review`: every slice reviewed
    assert sdlc.review_mode("routine", {"slice_review": {"routine": "nonsense"}}) == "slice"
    assert CONFIG["max_pr_rounds"] == 5


def gate(labels=JUDGMENT, reviews=(), deferrals=(), the_pr=None):
    the_pr = the_pr or pr()
    mode = sdlc.review_mode(sdlc.complexity_of(list(labels)), CONFIG)
    return sdlc.merge_gate_errors(the_pr, slice_issue(labels), [{"sha": HEAD, "result": "pass"}], list(reviews),
                                  "epic/12", 0, list(deferrals), mode)


def test_the_merge_gate_and_the_review():
    assert any("no Codex review" in e and "review-defer 50" in e for e in gate())
    assert gate(reviews=[{"sha": HEAD, "verdict": "approve"}]) == []
    assert gate(labels=ROUTINE) == []                                    # reviewed with its milestone
    assert gate(deferrals=[{"sha": HEAD}]) == []                         # Codex couldn't run on this head
    assert any("no Codex review" in e for e in gate(deferrals=[{"sha": OLD}]))   # a rebase needs a new deferral
    # a review that asked for changes always blocks, routine or deferred
    changes = [{"sha": HEAD, "verdict": "changes"}]
    assert any("asks for changes" in e for e in gate(labels=ROUTINE, reviews=changes))
    assert any("asks for changes" in e for e in gate(reviews=changes, deferrals=[{"sha": HEAD}]))
    # only the review relaxes: the tests, the base and the rest still hold
    assert any("targets main" in e for e in gate(labels=ROUTINE, the_pr=pr(baseRefName="main")))


def test_owed_why():
    approve = [{"sha": HEAD, "verdict": "approve"}]
    assert sdlc.owed_why(pr(), approve, [], "milestone") is None         # reviewed anyway: nothing owed
    assert sdlc.owed_why(pr(), [], [{"sha": HEAD}], "slice") == "unavailable"
    assert sdlc.owed_why(pr(), [], [], "milestone") == "routine"
    assert sdlc.owed_why(pr(), [], [], "slice") is None


# --- review-defer -----------------------------------------------------------------------------

def _defer(tmp_path, comments=(), labels=JUDGMENT, reason="codex: command not found"):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"pr": pr(), "pr_comments": list(comments), "trusted": [OWNER],
                             "slice": slice_issue(labels)}))
    return sdlc.main(["review-defer", "50", "--reason", reason, "--from-file", str(f), "--dry-run"])


def review(sha, verdict, ts):
    return agent("pr-review", None, ts, sha=sha, verdict=verdict, round=str(ts))


def test_review_defer(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    assert _defer(tmp_path) == 0
    out = capsys.readouterr().out
    assert f"kind=review-deferred sha={HEAD}" in out and "command not found" in out
    # findings nobody checked can't be deferred, even on a later head
    assert _defer(tmp_path, [review(OLD, "changes", 1)]) == 1
    assert "asked for changes" in capsys.readouterr().err
    assert _defer(tmp_path, [review(OLD, "changes", 1), review(OLD, "approve", 2)]) == 0   # answered, then rebased
    capsys.readouterr()
    assert _defer(tmp_path, [review(HEAD, "approve", 1)]) == 1
    assert "already approved" in capsys.readouterr().err
    assert _defer(tmp_path, labels=ROUTINE) == 1
    assert "no deferral needed" in capsys.readouterr().err
    assert _defer(tmp_path, reason="  ") == 1


def test_slice_status_shows_a_deferral():
    the_pr = pr()
    st = sdlc.derive_slice(slice_issue(), [], TRUSTED, [], the_pr,
                           [agent("tests", None, 1, sha=HEAD, result="pass", passed="9"),
                            agent("review-deferred", None, 2, sha=HEAD)])
    assert st["deferred_on_head"] and st["review_on_head"] is None


# --- merge records what it owes, before the squash ------------------------------------------

class GitHub:
    def __init__(self, monkeypatch):
        self.calls, self.posted = [], []
        monkeypatch.setattr(sdlc, "gh", self.gh)
        monkeypatch.setattr(sdlc, "post_comment", lambda n, body, cfg: self.posted.append((n, body)) or {})

    def gh(self, args, check=True):
        self.calls.append(args)
        if args[:2] == ["pr", "merge"]:
            self.calls.append(["posted so far", len(self.posted)])
        return ""


def stub_merge(monkeypatch, labels, pr_comments):
    reads = iter([slice_issue(labels), {**slice_issue(labels), "state": "closed", "state_reason": "completed"}])
    monkeypatch.setattr(sdlc, "fetch_pr", lambda n, cfg: (pr(), pr_comments))
    monkeypatch.setattr(sdlc, "fetch_trusted", lambda cfg: TRUSTED)
    monkeypatch.setattr(sdlc, "fetch_slice", lambda n, cfg: (next(reads), []))
    monkeypatch.setattr(sdlc, "gh_json", lambda args: {"behind_by": 0})
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, cfg, trusted=None: {"issue": issue(), "comments": HISTORY,
                                                                            "trusted": [OWNER]})
    monkeypatch.setattr(sdlc, "epic_progress", lambda n, cfg, trusted: progress(ms("M1", 1, 3)))
    monkeypatch.setattr(sdlc, "with_next", lambda st, cfg, offline=False, trusted=None: {**st, "next": "x", "progress": None})


TESTED = [agent("tests", None, 1, sha=HEAD, result="pass", passed="9")]


def test_merge_owes_a_routine_slices_review_to_its_milestone(monkeypatch, capsys):
    gh = GitHub(monkeypatch)
    stub_merge(monkeypatch, ROUTINE, TESTED)
    assert sdlc.main(["merge", "50"]) == 0
    (n, body), = gh.posted
    assert n == 12 and f"kind=review-owed milestone=M1 slice=T1.1 number=28 why=routine sha={HEAD}" in body
    assert ["posted so far", 1] in gh.calls                    # recorded before the squash, never after


def test_merge_owes_a_deferred_review(monkeypatch):
    gh = GitHub(monkeypatch)
    stub_merge(monkeypatch, JUDGMENT, TESTED + [agent("review-deferred", None, 2, sha=HEAD)])
    assert sdlc.main(["merge", "50"]) == 0
    assert "why=unavailable" in gh.posted[0][1]


def test_merge_owes_nothing_for_an_approved_head(monkeypatch):
    gh = GitHub(monkeypatch)
    stub_merge(monkeypatch, ROUTINE, TESTED + [agent("pr-review", None, 2, sha=HEAD, verdict="approve", round="1")])
    assert sdlc.main(["merge", "50"]) == 0 and gh.posted == []


# --- the milestone's review comes before its demo -------------------------------------------

def test_an_owed_review_holds_the_milestone_before_its_demo():
    p = progress(ms("M1", 3, 3), ms("M2", 0, 2), ready=(40,))
    st = derive([OWED])
    assert st["review_owed"] == {"M1": [{"slice": "T1.2", "number": "29", "why": "routine", "sha": HEAD}]}
    view = sdlc.epic_view(st, p)
    assert view["current"]["phase"] == "review" and view["due"] == "review"
    assert view["next"].startswith("/milestone-demo 12: #12 M1: x is complete, but Codex hasn't reviewed T1.2 (routine)")
    assert "Codex review" in sdlc.claim_pause_errors(st, p)[0]
    assert sdlc.due_milestone(st["demos"], p, st["shipped"], st["review_owed"])["key"] == "M1"
    # still building: owing a review changes nothing yet
    assert sdlc.epic_view(st, progress(ms("M1", 2, 3)))["current"]["phase"] == "building"
    # an approving milestone review clears it; one asking for changes doesn't
    assert sdlc.epic_view(derive([OWED, APPROVE]), p)["current"]["phase"] == "complete"
    assert sdlc.epic_view(derive([OWED, CHANGES]), p)["current"]["phase"] == "review"
    # a slice owed after the review (a fix slice) needs another
    later = agent("review-owed", None, 22, milestone="M1", slice="F1", number="44", why="unavailable", sha=OLD)
    assert sdlc.derive_state(issue(), HISTORY + [OWED, APPROVE, later], TRUSTED, CONFIG)["review_owed"]["M1"][0]["slice"] == "F1"


def _bundle(tmp_path, comments, prog):
    f = tmp_path / "bundle.json"
    f.write_text(json.dumps({"issue": issue(), "comments": HISTORY + comments, "trusted": [OWNER], "progress": prog,
                             "epic_head": HEAD}))
    return str(f)


def test_demo_post_waits_for_the_review(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "git", lambda *a, **k: pytest.fail("dry run must not touch git"))
    d = tmp_path / "demo"
    d.mkdir()
    (d / "README.md").write_text("# M1\n" + "every alarm, by room " * 20)
    body = tmp_path / "comment.md"
    body.write_text("You can see every alarm.\n\n[the write-up](README.md)\n")
    args = ["demo-post", "12", "--milestone", "M1", "--dir", str(d), "--body-file", str(body), "--dry-run", "--from-file"]
    assert sdlc.main(args + [_bundle(tmp_path, [OWED], progress(ms("M1", 3, 3)))]) == 1
    assert "comes before its demo" in capsys.readouterr().err
    assert sdlc.main(args + [_bundle(tmp_path, [OWED, APPROVE], progress(ms("M1", 3, 3)))]) == 0


def test_ship_review_runs_before_the_demo_when_a_review_is_owed(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    r = tmp_path / "r.md"
    r.write_text(f"HEAD: {HEAD}\n1. T1.2 is fine\n\nVERDICT: approve\n")
    run = ["ship-review", "12", "--report", str(r), "--dry-run", "--from-file"]
    assert sdlc.main(run + [_bundle(tmp_path, [OWED], progress(ms("M1", 3, 3)))]) == 0
    assert f"kind=ship-review milestone=M1 sha={HEAD} verdict=approve" in capsys.readouterr().out
    # nothing owed and not accepted: no milestone review yet (the demo comes first)
    assert sdlc.main(run + [_bundle(tmp_path, [], progress(ms("M1", 3, 3)))]) == 1
    assert "isn't accepted" in capsys.readouterr().err
    # the same review on the same head serves the ship too
    ready = derive([OWED, APPROVE, agent("demo", 1, 30, milestone="M1", sha=HEAD),
                    agent("demo-approval", 1, 31, milestone="M1", by=POSTER),
                    agent("epic-tests", None, 32, sha=HEAD, result="pass", passed="9")])
    assert sdlc.ship_gate_errors(ready, "M1", HEAD, True, []) == []
    owed = {**ready, "review_owed": {"M1": [{"slice": "T1.2"}]}}
    assert any("`ship-review` first" in e for e in sdlc.ship_gate_errors(owed, "M1", HEAD, True, []))


def test_an_epic_without_milestones_owes_and_clears_the_same_way(monkeypatch):
    # most bugs: no milestones, one demo `all`. merge's marker and the progress must agree on that key
    gh = GitHub(monkeypatch)
    stub_merge(monkeypatch, ROUTINE, TESTED)
    monkeypatch.setattr(sdlc, "fetch_slice", lambda n, cfg, _r=iter([{**slice_issue(ROUTINE), "milestone": None},
                                                                     {**slice_issue(ROUTINE), "milestone": None,
                                                                      "state": "closed", "state_reason": "completed"}]):
                        (next(_r), []))
    monkeypatch.setattr(sdlc, "epic_progress", lambda n, cfg, trusted: progress(
        {"title": "(no milestone)", "key": "all", "done": 0, "total": 1}))
    assert sdlc.main(["merge", "50"]) == 0
    marker = sdlc.parse_marker(gh.posted[0][1])
    assert marker["milestone"] == sdlc.NO_MILESTONE == "all" and marker["slice"] == "T1.1"
    owed = comment(OWNER, gh.posted[0][1], 20)
    leaves = [{"number": 28, "key": "T1.1", "kind": "slice", "milestone": None, "state": "closed",
               "state_reason": "completed", "assignees": [], "labels": ["plan:slice"], "body": ""}]
    p = {**sdlc.summarise_progress(leaves, {}), "debt": [], "cleanup_budget": 1}
    view = sdlc.epic_view(derive([owed]), p)
    assert view["current"]["key"] == "all" and view["current"]["phase"] == "review" and view["due"] == "review"
    cleared = agent("ship-review", None, 21, milestone="all", sha=HEAD, verdict="approve", round="1")
    assert sdlc.epic_view(derive([owed, cleared]), p)["current"]["phase"] == "complete"


# --- codex-review: the tool runs a PR round (tools/codex_review.py), on settings the repo owns --------

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tools import codex_review

ROOT = Path(__file__).resolve().parent.parent
FAKE_CODEX = f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys, time
log = pathlib.Path(os.environ["FAKE_CODEX_LOG"])
n = len(log.read_text().splitlines()) if log.exists() else 0
with log.open("a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin_tty": sys.stdin.isatty()}}) + "\\n")
plan = json.loads(pathlib.Path(os.environ["FAKE_CODEX_PLAN"]).read_text())
step = plan[min(n, len(plan) - 1)]
if step.get("edit"):
    pathlib.Path(step["edit"]).write_text("edited during the review\\n")
if step.get("commit"):
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "moved"], check=True)
time.sleep(step.get("sleep", 0))
sys.stdout.write(step.get("out", ""))
sys.stderr.write("codex progress noise\\n")
"""
APPROVES = {"out": "1. fine (non-blocking)\n\nVERDICT: approve\n"}
NO_VERDICT = {"out": "I looked around and ran out of time.\n"}
SLOW = {"sleep": 5, "out": "VERDICT: approve\n"}
SETTINGS = {"model": "test-model", "reasoning_effort": "low", "sandbox": "read-only", "timeout_seconds": 30,
            "flags": ["--skip-git-repo-check"]}
QUICK = {**SETTINGS, "timeout_seconds": 1}


class Codex:
    """A fake `codex` on PATH, a tmp git repo as the slice's worktree, and a saved PR bundle."""

    def __init__(self, tmp_path, monkeypatch, plan=(APPROVES,), settings=SETTINGS):
        self.tmp = tmp_path
        for k in ("AUTHOR", "COMMITTER"):
            monkeypatch.setenv(f"GIT_{k}_NAME", "t")
            monkeypatch.setenv(f"GIT_{k}_EMAIL", "t@example.invalid")
        self.wt = tmp_path / "wt"
        self.wt.mkdir()
        for cmd in (["init", "-q"], ["commit", "--allow-empty", "-qm", "base"]):
            self.git(*cmd)
        self.git("update-ref", "refs/remotes/origin/epic/12", "HEAD")
        (self.wt / "x.py").write_text("x = 1\n")
        self.git("add", "x.py")
        self.git("commit", "-qm", "the slice")
        self.head = self.git("rev-parse", "HEAD")
        bin_ = tmp_path / "bin"
        bin_.mkdir()
        (bin_ / "codex").write_text(FAKE_CODEX)
        (bin_ / "codex").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
        self.log, self.plan = tmp_path / "codex.log", tmp_path / "plan.json"
        monkeypatch.setenv("FAKE_CODEX_LOG", str(self.log))
        monkeypatch.setenv("FAKE_CODEX_PLAN", str(self.plan))
        self.set_plan(*plan)
        cfg = dict(CONFIG)
        cfg.pop("codex", None)
        if settings is not None:
            cfg["codex"] = settings
        self.config = tmp_path / "config.json"
        self.config.write_text(json.dumps(cfg))
        self.out = tmp_path / "scratch"
        monkeypatch.setattr(sdlc, "worktree_path", lambda n, config: self.wt)
        monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("codex-review --from-file must not call gh"))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.wt, capture_output=True, text=True, check=True).stdout.strip()

    def set_plan(self, *steps):
        self.plan.write_text(json.dumps(list(steps)))

    def bundle(self, comments=(), **pr_kw):
        f = self.tmp / "bundle.json"
        f.write_text(json.dumps({"pr": pr(headRefOid=self.head, **pr_kw), "pr_comments": list(comments),
                                 "trusted": [OWNER], "slice": {**slice_issue(JUDGMENT), "body": "<!-- sdlc:v1 kind=slice "
                                 "epic=12 key=T1.1 -->\n## Outcome\n\nthe brief\n"}}))
        return str(f)

    def run(self, *extra, comments=()):
        return sdlc.main(["--config", str(self.config), "codex-review", "--pr", "50", "--out", str(self.out),
                          "--from-file", self.bundle(comments), *extra])

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def report(self):
        return self.out / "codex.md"


def test_the_tool_writes_the_head_line_and_pr_review_accepts_it(tmp_path, monkeypatch, capsys):
    c = Codex(tmp_path, monkeypatch)
    assert c.run() == 0
    saved = c.report().read_text()
    assert saved.splitlines()[0] == f"HEAD: {c.head}" and saved.rstrip().endswith("VERDICT: approve")
    (call,) = c.calls()
    assert call["cwd"] == str(c.wt.resolve()) and not call["stdin_tty"]
    assert "codex progress noise" in (c.out / "codex.err").read_text()    # stderr kept apart from the report
    out = capsys.readouterr().out
    assert str(c.report()) in out and "1. fine" in out
    bundle = tmp_path / "pr.json"
    bundle.write_text(json.dumps({"pr": pr(headRefOid=c.head), "pr_comments": [], "trusted": [OWNER]}))
    assert sdlc.main(["pr-review", "50", "--report", str(c.report()), "--from-file", str(bundle), "--dry-run"]) == 0
    assert f"sha={c.head} verdict=approve" in capsys.readouterr().out


def test_it_refuses_a_dirty_worktree_or_the_wrong_head_and_runs_nothing(tmp_path, monkeypatch, capsys):
    c = Codex(tmp_path, monkeypatch)
    (c.wt / "notes.md").write_text("scratch in the worktree")
    assert c.run() == 1                                   # a refusal, never the review-defer status
    err = capsys.readouterr().err
    assert "not clean" in err and "notes.md" in err
    (c.wt / "notes.md").unlink()
    c.git("commit", "--allow-empty", "-qm", "local, not pushed")
    assert c.run() == 1
    assert "is not the PR's head" in capsys.readouterr().err
    assert c.calls() == [] and not c.report().exists()
    # and its scratch never goes in the worktree
    assert sdlc.main(["--config", str(c.config), "codex-review", "--pr", "50", "--out", str(c.wt / "s"),
                      "--from-file", c.bundle()]) == 1
    assert "inside the worktree" in capsys.readouterr().err


def test_a_worktree_changed_during_the_run_fails_and_saves_nothing(tmp_path, monkeypatch, capsys):
    c = Codex(tmp_path, monkeypatch, plan=({"edit": "x.py", **APPROVES},))
    assert c.run() == 1
    err = capsys.readouterr().err
    assert "changed while Codex ran" in err and "x.py" in err
    assert len(c.calls()) == 1 and not c.report().exists()


def test_a_head_that_moves_during_the_run_fails_and_saves_nothing(tmp_path, monkeypatch, capsys):
    c = Codex(tmp_path, monkeypatch, plan=({"commit": True, **APPROVES},))
    c.out.mkdir()
    c.report().write_text(f"HEAD: {c.head}\n\nVERDICT: approve\n")    # a stale report from an earlier run
    assert c.run() == 1
    assert "HEAD moved" in capsys.readouterr().err
    assert len(c.calls()) == 1 and not c.report().exists()


def test_no_verdict_is_retried_once_then_it_is_the_defer_status(tmp_path, monkeypatch, capsys):
    c = Codex(tmp_path, monkeypatch, plan=(NO_VERDICT, APPROVES))
    assert c.run() == 0 and len(c.calls()) == 2 and c.report().read_text().startswith(f"HEAD: {c.head}")
    c.log.unlink()
    c.set_plan(NO_VERDICT)
    assert c.run() == codex_review.UNAVAILABLE == 3
    assert len(c.calls()) == 2 and not c.report().exists()
    assert "codex progress noise" in capsys.readouterr().out          # codex.err's tail, for review-defer's reason


def test_the_timeout_from_config_reaches_the_run_once_then_succeeds_twice_defers(tmp_path, monkeypatch):
    c = Codex(tmp_path, monkeypatch, plan=(SLOW, APPROVES), settings=QUICK)
    assert c.run() == 0 and len(c.calls()) == 2              # killed after codex.timeout_seconds (1s), not 5s
    assert c.report().read_text().startswith(f"HEAD: {c.head}") and "timed out after 1s" in (c.out / "codex.err").read_text()
    c.log.unlink()
    c.set_plan(SLOW)
    assert c.run() == codex_review.UNAVAILABLE and len(c.calls()) == 2


def test_codex_not_on_the_path_or_not_runnable_is_the_defer_status(tmp_path, monkeypatch):
    c = Codex(tmp_path, monkeypatch)
    (tmp_path / "bin" / "codex").chmod(0o644)
    assert c.run() == codex_review.UNAVAILABLE and c.calls() == []
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))
    assert c.run() == codex_review.UNAVAILABLE and c.calls() == []


def test_an_interrupted_run_kills_codex(tmp_path, monkeypatch):
    killed = []

    class Proc:
        pid = 4242

        def wait(self, timeout=None):
            if timeout is not None:
                raise KeyboardInterrupt
            return 0

        def poll(self):
            return None if not killed else 0

        def kill(self):
            killed.append("kill")

    monkeypatch.setattr(codex_review.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(codex_review.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(KeyboardInterrupt):
        codex_review.run_once(["codex"], tmp_path, tmp_path / "o", tmp_path / "e", 30)
    assert killed[0] == (4242, codex_review.signal.SIGKILL)
    # a group it may not signal (a sandbox helper: EPERM) still kills Codex, and the interrupt isn't
    # mistaken for "codex can't be started"
    killed.clear()

    def eperm(pid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(codex_review.os, "killpg", eperm)
    with pytest.raises(KeyboardInterrupt):
        codex_review.run_once(["codex"], tmp_path, tmp_path / "o", tmp_path / "e", 30)
    assert killed == ["kill"]


def test_codex_gets_the_repos_settings_and_a_config_without_them_is_refused(tmp_path, monkeypatch, capsys):
    c = Codex(tmp_path, monkeypatch)
    assert c.run() == 0
    argv = c.calls()[0]["argv"]
    assert argv[:9] == ["exec", "-m", "test-model", "-c", 'model_reasoning_effort="low"', "--sandbox", "read-only",
                        "--skip-git-repo-check", argv[8]] and argv[8].startswith("You are an independent reviewer")
    capsys.readouterr()
    (tmp_path / "again").mkdir()
    c = Codex(tmp_path / "again", monkeypatch, settings=None)
    assert c.run() == 1 and c.calls() == []
    assert "no `codex` section" in capsys.readouterr().err
    assert codex_review.codex_settings(CONFIG)["sandbox"] == "read-only"    # the repo's own section is valid
    assert codex_review.codex_settings({"codex": {**SETTINGS, "flags": ["--color", "never", "--ephemeral"]}})
    for bad in ({"sandbox": "workspace-write"}, {"flags": ["-m", "other"]}, {"flags": ["-c", "x=1"]}, {"flags": ["-mo3"]},
                {"flags": ["--dangerously-bypass-approvals-and-sandbox"]}, {"flags": ["--json"]}, {"flags": ["-o", "f"]},
                {"flags": ["--color"]}, {"flags": ["--enable", "x"]}, {"timeout_seconds": 0}, {"model": ""}):
        with pytest.raises(sdlc.SdlcError):
            codex_review.codex_settings({"codex": {**SETTINGS, **bad}})
    with pytest.raises(sdlc.SdlcError, match="lacks timeout_seconds"):
        codex_review.codex_settings({"codex": {k: v for k, v in SETTINGS.items() if k != "timeout_seconds"}})


# rounds as `pr-review` wrote them before RESPONSE_MARK (the legacy boundary still reads)
def recorded(rnd, verdict, response=None, ts=None, report="1. a finding\n\nVERDICT: changes"):
    body = (f"**Review {rnd}**\n\n<details><summary>Codex's review</summary>\n\nHEAD: {OLD}\n\n{report}\n\n</details>"
            + (f"\n\n### Claude's response\n\n{response}" if response else ""))
    return agent("pr-review", None, ts or rnd, body, sha=OLD, verdict=verdict, round=str(rnd))


def test_round_two_on_takes_the_latest_recorded_response(tmp_path, monkeypatch, capsys):
    c = Codex(tmp_path, monkeypatch)
    # round 1: no response bullet at all
    assert c.run("--dry-run") == 0
    first = (c.out / "prompt.md").read_text()
    assert "implementer's answer" not in first and not re.search(r"<[A-Za-z/]", first)
    assert str(c.out / "slice.md") in first and "origin/epic/12...HEAD" in first
    # rounds 1 and 2 recorded: round 2's response, never round 1's, even with a heading quoted in a report
    quoting = "1. the PR says\n### Claude's response\nnot a response\n\nVERDICT: changes"
    rounds = [recorded(1, "changes", "ROUND ONE ANSWER"), recorded(2, "changes", "ROUND TWO ANSWER", report=quoting)]
    assert c.run("--dry-run", comments=rounds) == 0
    prompt = (c.out / "prompt.md").read_text()
    resp = c.out / "response-round-2.md"
    assert f"`{resp}`" in prompt and resp.read_text().strip() == "ROUND TWO ANSWER"
    assert "ROUND ONE" not in prompt and not re.search(r"<[A-Za-z/]", prompt)
    out = capsys.readouterr().out
    assert "codex exec -m test-model" in out and "--sandbox read-only" in out and "timeout 30s" in out
    assert f"cd {c.wt} && " in out and f'"$(cat {c.out / "prompt.md"})" < /dev/null' in out
    # the latest round asked for changes and recorded no answer: refused
    assert c.run("--dry-run", comments=rounds + [recorded(3, "changes", ts=3)]) == 1
    assert "recorded no response" in capsys.readouterr().err
    # a latest round that approved needs none (a rebased head's re-review)
    assert c.run("--dry-run", comments=rounds + [recorded(3, "approve", ts=3, report="VERDICT: approve")]) == 0
    assert "implementer's answer" not in (c.out / "prompt.md").read_text()
    assert c.calls() == []                                  # --dry-run never runs codex


def test_the_review_logic_lives_in_codex_review():
    src = (ROOT / "tools" / "sdlc.py").read_text()
    for name in ("def parse_verdict", "VERDICT_RE =", "HEAD_RE =", "def fill_pr_prompt", "<SLICE_FILE>"):
        assert name not in src, f"{name} belongs in tools/codex_review.py"
    assert sdlc.parse_verdict is codex_review.parse_verdict and sdlc.HEAD_RE is codex_review.HEAD_RE
    assert sdlc.SdlcError is codex_review.ReviewError
    tree = ast.parse((ROOT / "tools" / "codex_review.py").read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
    assert imported - {"__future__"} <= sys.stdlib_module_names         # stdlib only: no sdlc, no twiddle


def test_the_response_pr_review_records_is_the_one_the_next_round_reads(tmp_path, capsys):
    report, resp, bundle = tmp_path / "codex.md", tmp_path / "response.md", tmp_path / "pr.json"
    bundle.write_text(json.dumps({"pr": pr(), "pr_comments": [], "trusted": [OWNER]}))

    def record(report_text, response=None):
        """One round as `pr-review` records it, read back as `codex-review` reads it."""
        report.write_text(f"HEAD: {HEAD}\n\n{report_text}\n\nVERDICT: changes\n")
        extra = []
        if response is not None:
            resp.write_text(response)
            extra = ["--response", str(resp)]
        assert sdlc.main(["pr-review", "50", "--report", str(report), "--from-file", str(bundle), "--dry-run", *extra]) == 0
        body = capsys.readouterr().out
        mk = sdlc.parse_marker(body)
        return codex_review.latest_response([{"verdict": mk["verdict"], "body": body, "response": mk.get("response")}])

    # a report quoting both boundaries in a fenced example, a response with a <details> block of its own
    quoting = (f"1. a fenced example:\n```\n{codex_review.LEGACY_MARK}not a response\n</details>\n\n"
               f"{codex_review.RESPONSE_MARK}\n{codex_review.RESPONSE_HEADING}\n\nnor this\n```")
    answer = "1. Rebutted: see the log.\n\n<details><summary>log</summary>\n\nx\n\n</details>\n\n2. Accepted."
    assert record(quoting, answer) == (1, answer)
    assert record(quoting, f"{answer}\n\nquoting {codex_review.RESPONSE_MARK} too")[1] == f"{answer}\n\nquoting  too"
    for no_answer in (None, "  \n"):
        with pytest.raises(sdlc.SdlcError, match="recorded no response"):
            record(quoting, no_answer)
    # a round recorded before the marker said (no `response` attribute), whose response mentions the boundary
    legacy = recorded(1, "changes", f"1. Accepted: write `{codex_review.RESPONSE_MARK}` first.\n2. Accepted.")["body"]
    assert codex_review.latest_response([{"verdict": "changes", "body": legacy}])[1].startswith("1. Accepted: write")
