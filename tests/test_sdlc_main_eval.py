"""tools/sdlc.py: the review eval gate holds for a quick change's and a debt group's PR into main too (#100 F3).

A quick or debt PR whose (fingerprint, `codex --version`) pair has no pass can't merge, and a deferral never lets
it through: those lanes have no milestone demo to owe an eval to. The gate runs for real here, through `merge
--dry-run`, `quick-gate`, `slice-status` and `state`: the PR's head and main come from a scratch bare origin
(`refs/pull/N/head`), `codex --version` from a fake `codex` on PATH, and only GitHub is faked. No network, no speaker.
"""
import os
import subprocess

import pytest

from tests.test_sdlc_debt import due_group, gh, main_gate, repo, run, status, ticket_state  # noqa: F401
from tests.test_sdlc_eval import only_git, result_comment
from tests.test_sdlc_quick import CLAIM, GATE_BRIEF, STATION, MergeGitHub, changed, gate_bundle, marked, ready_quick
from tools import codex_eval, sdlc

V, V2 = "9.9.9-test", "10.0.0-test"     # what the fake `codex --version` says, and an upgrade
TRUSTED = {"owner"}
EVAL_FILE = "tests/codex_eval/clean/expect.toml"   # a fingerprint input the quick lane's denied paths don't cover


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Pair:
    """A fake `codex` saying a version, and the eval results the gate reads (`eval_ledger`, GitHub's part)."""

    def __init__(self, tmp_path, monkeypatch, version=V):
        self.bin, self.results = tmp_path / "fake-codex-bin", []
        self.bin.mkdir()
        self.version(version)
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(sdlc, "eval_ledger",
                            lambda args, config, bundle=None: sdlc.eval_records(self.results, TRUSTED))

    def version(self, v):
        (self.bin / "codex").write_text(f"#!/bin/sh\necho 'codex-cli {v}'\n")
        (self.bin / "codex").chmod(0o755)

    def none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", only_git(tmp_path))            # git, and no `codex` at all

    def record(self, outcome, fp, version=V):
        self.results.append(result_comment(len(self.results) + 1, outcome, fingerprint=fp, version=version))


def fingerprints(root, head):
    fp = lambda sha: codex_eval.fingerprint(codex_eval.Commit(sha, root))   # noqa: E731
    return fp(head), fp(_git("rev-parse", "origin/main", cwd=root))


# --- a quick change ----------------------------------------------------------------------------------------

EVAL_BRIEF = GATE_BRIEF.replace("- tests/test_stations.py", f"- tests/test_stations.py\n- `{EVAL_FILE}`")


@pytest.fixture
def quick(tmp_path, monkeypatch, real_eval_gate):
    """(primary checkout, Pair, a function making PR #50's head edit a path): main and refs/pull/50/head on a bare
    origin, the primary checkout on main, and a quick bundle that passes every other rule of the quick gate."""
    origin, root = tmp_path / "origin.git", tmp_path / "primary"
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git("config", k, v, cwd=root)
    (root / "README.md").write_text("main\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "main", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("push", "-q", "-u", "origin", "main", cwd=root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sdlc, "primary_root", lambda *a, **k: root)
    monkeypatch.setattr(sdlc, "gate_code_errors", lambda config, pr="PR": [])   # tests/test_sdlc_merge_gate.py's
    pair = Pair(tmp_path, monkeypatch)

    def pr_editing(path):
        base = _git("rev-parse", "HEAD", cwd=root)
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("changed\n")
        _git("add", "-A", cwd=root)
        _git("commit", "-q", "-m", "the change", cwd=root)
        head = _git("rev-parse", "HEAD", cwd=root)
        _git("push", "-q", "origin", "HEAD:refs/pull/50/head", cwd=root)
        _git("reset", "-q", "--hard", base, cwd=root)
        b = gate_bundle(comments=ready_quick(EVAL_BRIEF) + [CLAIM], pr={"headRefOid": head},
                        records=lambda brief: [marked("tests", 7, sha=head, result="pass", passed="1300"),
                                               marked("pr-review", 8, sha=head, verdict="approve", round="1", brief=brief)],
                        files=[changed(STATION), changed(path, 1, 0)])
        # a fresh fake GitHub for each command (its issue read-backs are one merge's)
        github.append(lambda: MergeGitHub(monkeypatch, b, first_head=head))
        github[-1]()
        return head
    github = []
    return root, pair, pr_editing, github


def merge_dry_run(capsys, n=50, github=None):
    if github:
        github[-1]()
    code, out, err = run(capsys, "merge", n, "--dry-run")
    return code, out + err


def test_a_quick_pr_changing_the_review_logic_needs_a_pass_and_a_deferral_does_not_count(quick, capsys):
    root, pair, pr_editing, github = quick
    head_fp, base_fp = fingerprints(root, pr_editing(EVAL_FILE))
    assert head_fp != base_fp                                      # the PR edits a fingerprint input
    code, out = merge_dry_run(capsys, github=github)
    assert code == 1 and "codex-eval" in out and "this PR changes the review logic" in out
    assert "escalate-to-full 12" in out and "--defer" not in out and "no milestone demo" in out
    assert codex_eval.short(head_fp) in out and head_fp not in out
    github[-1]()
    code, out, _ = run(capsys, "quick-gate", 50)                   # the read-only view says the same
    assert code == 1 and "refused: no review eval for" in out and "escalate-to-full 12" in out
    pair.record("pass", base_fp)                                   # main's pass doesn't count for the head
    assert merge_dry_run(capsys, github=github)[0] == 1
    pair.record("pass", head_fp, V2)                               # nor another codex's
    assert merge_dry_run(capsys, github=github)[0] == 1
    pair.record("deferred", head_fp)                               # a deferral is refused, and says why
    code, out = merge_dry_run(capsys, github=github)
    assert code == 1 and "only deferred" in out and "no milestone demo to owe the eval to" in out
    pair.record("fail", head_fp)
    code, out = merge_dry_run(capsys, github=github)
    assert code == 1 and "FAILED" in out
    pair.record("pass", head_fp)                                   # a pass for the pair lets it through
    code, out = merge_dry_run(capsys, github=github)
    assert code == 0 and "would squash-merge quick PR #50 (closes #12) into main" in out, out
    assert run(capsys, "quick-gate", 50)[1].strip() == "ok"


def test_a_quick_pr_with_no_codex_to_ask_is_refused_without_offering_a_deferral(quick, capsys, monkeypatch, tmp_path):
    root, pair, pr_editing, github = quick
    head_fp, _ = fingerprints(root, pr_editing(EVAL_FILE))
    pair.none(monkeypatch, tmp_path)
    code, out = merge_dry_run(capsys, github=github)
    assert code == 1 and "can't be read here" in out and "--defer" not in out and "escalate-to-full 12" in out
    pair.record("deferred", head_fp, sdlc.EVAL_UNAVAILABLE)        # what `codex-eval --defer` records then
    code, out = merge_dry_run(capsys, github=github)
    assert code == 1 and "only deferred" in out
    pair.record("pass", head_fp, V)                                # the fingerprint's pass, as the slice lane reads it
    assert merge_dry_run(capsys, github=github)[0] == 0


def test_a_quick_pr_with_unchanged_review_logic_waits_only_after_a_codex_upgrade(quick, capsys):
    root, pair, pr_editing, github = quick
    head_fp, base_fp = fingerprints(root, pr_editing(STATION))
    assert head_fp == base_fp                                      # an ordinary quick change
    pair.record("pass", head_fp)
    code, out = merge_dry_run(capsys, github=github)
    assert code == 0, out                                          # a pair that passed: merges as before
    pair.version(V2)                                               # a Codex upgrade alone
    code, out = merge_dry_run(capsys, github=github)
    assert code == 1 and f"codex is now {V2}" in out and "codex-eval" in out and "escalate-to-full 12" in out
    pair.record("deferred", head_fp, V2)
    assert merge_dry_run(capsys, github=github)[0] == 1
    pair.record("pass", head_fp, V2)
    assert merge_dry_run(capsys, github=github)[0] == 0


# --- a debt group --------------------------------------------------------------------------------------------

@pytest.fixture
def debt(gh, repo, capsys, main_gate, tmp_path, monkeypatch, real_eval_gate):   # noqa: F811
    """(debt epic, tickets, Pair, a function making PR #300's head edit a path): the group's PR, its records on that
    head, and refs/pull/300/head on the repo's bare origin."""
    epic, tickets, wt = due_group(gh, repo, capsys, records=False)
    pair = Pair(tmp_path, monkeypatch)

    def pr_editing(path):
        (wt / path).parent.mkdir(parents=True, exist_ok=True)
        (wt / path).write_text("changed\n")
        _git("add", "-A", cwd=wt)
        _git("commit", "-q", "-m", "the debt fix", cwd=wt)
        head = _git("rev-parse", "HEAD", cwd=wt)
        _git("push", "-q", "origin", "HEAD:refs/pull/300/head", cwd=wt)
        gh.prs[300]["headRefOid"] = head
        gh.behind[head] = 0
        gh.say(300, "tests", sha=head, result="pass", passed="10")
        gh.say(300, "pr-review", sha=head, verdict="approve", round="1")
        return head
    return epic, tickets, pair, pr_editing


def test_a_debt_pr_changing_the_review_logic_needs_a_pass_and_a_deferral_does_not_count(debt, repo, capsys):  # noqa: F811
    epic, (a, b, _), pair, pr_editing = debt
    head_fp, base_fp = fingerprints(repo, pr_editing("tools/codex_review.py"))
    assert head_fp != base_fp
    code, out = merge_dry_run(capsys, 300)
    assert code == 1 and "codex-eval" in out and "this PR changes the review logic" in out
    assert "the group waits" in out and "--defer" not in out and "escalate-to-full" not in out
    # every view of the group shows the same refusal where it shows the gate
    s = status(capsys, a)
    assert s["merge_due"] and any("no review eval for" in e for e in s["gate"]), s["gate"]
    assert "gate still refuses: no review eval for" in ticket_state(capsys, epic)["next"]
    pair.record("deferred", head_fp)
    code, out = merge_dry_run(capsys, 300)
    assert code == 1 and "only deferred" in out and "no milestone demo to owe the eval to" in out
    pair.record("fail", head_fp)
    code, out = merge_dry_run(capsys, 300)
    assert code == 1 and "FAILED" in out
    pair.record("pass", head_fp, V2)
    assert merge_dry_run(capsys, 300)[0] == 1
    pair.record("pass", head_fp)
    code, out = merge_dry_run(capsys, 300)
    assert code == 0 and f"would squash-merge debt PR #300 (group `debt-1-1`: #{a}, #{b}) into main" in out, out
    assert status(capsys, a)["gate"] == []


def test_a_debt_pr_with_unchanged_review_logic_waits_only_after_a_codex_upgrade(debt, repo, capsys):  # noqa: F811
    epic, (a, b, _), pair, pr_editing = debt
    head_fp, base_fp = fingerprints(repo, pr_editing("code.py"))
    assert head_fp == base_fp
    pair.record("pass", head_fp)
    code, out = merge_dry_run(capsys, 300)
    assert code == 0, out
    pair.version(V2)
    code, out = merge_dry_run(capsys, 300)
    assert code == 1 and f"codex is now {V2}" in out and "the group waits" in out
    pair.record("deferred", head_fp, V2)
    assert merge_dry_run(capsys, 300)[0] == 1
    pair.record("pass", head_fp, V2)
    assert merge_dry_run(capsys, 300)[0] == 0


# --- the slice lane's gate is unchanged ----------------------------------------------------------------------

def test_the_slice_lane_still_takes_a_deferral():
    errs, owed = sdlc.eval_gate("f" * 64, "e" * 64, V, sdlc.eval_records([result_comment(1, "deferred", fingerprint="f" * 64,
                                                                                           version=V)], TRUSTED))
    assert errs == [] and owed == {"fp": "f" * 64, "codex": V}
    errs, _ = sdlc.eval_gate("f" * 64, "e" * 64, V, [])
    assert "codex-eval --defer" in errs[0]


def test_existing_tests_never_reach_the_real_gate(capsys):
    # without `real_eval_gate`, conftest answers for it: no test's fake GitHub serves a PR's head ref
    assert sdlc.main_eval_errors({"number": 1}, {}, "quick", 1) == []
