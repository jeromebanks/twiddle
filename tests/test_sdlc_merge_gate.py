"""tools/sdlc.py `merge` runs only main's gate code (#239), and `slice-status` / `state` name `merge PR` once a PR's
records allow it. Scratch git repos with a bare origin and fake `gh` JSON only: no network, no speaker."""
import argparse
import json
import subprocess
from pathlib import Path

import pytest

from tests.test_sdlc_quick import CLAIM, PR as QUICK_PR, RECORDS, ROUTINE, quick_bundle, ready_quick
from tests.test_sdlc_slice import agent, pr as slice_pr, ran_on, review_on, slice_issue
from tools import sdlc

CONFIG = sdlc.load_config()
REAL = {p: f.read_bytes() for p, f in sdlc.GATE_CODE.items()}   # this checkout's copies, whatever they say
HEAD = "a" * 40


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def primary(tmp_path, monkeypatch):
    """A primary checkout on main whose origin/main holds byte copies of the gate's two files, run from itself."""
    origin, root = tmp_path / "origin.git", tmp_path / "repo"
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git("config", k, v, cwd=root)
    for p, data in REAL.items():
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_bytes(data)
    (root / ".gitignore").write_text(".worktrees/\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "main", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("push", "-q", "origin", "main", cwd=root)
    monkeypatch.chdir(root)
    run_from(monkeypatch, root)
    return root


def run_from(monkeypatch, checkout: Path):
    """As if this process were `uv run python <checkout>/tools/sdlc.py`."""
    monkeypatch.setattr(sdlc, "ROOT", checkout)
    monkeypatch.setattr(sdlc, "GATE_CODE", {p: checkout / p for p in REAL})


def worktree(root: Path, monkeypatch, base="origin/main") -> Path:
    wt = root / ".worktrees" / "slice-9"
    _git("worktree", "add", "-q", "-b", "slice/9", str(wt), base, cwd=root)
    run_from(monkeypatch, wt)
    return wt


def errors():
    return sdlc.gate_code_errors(CONFIG, 50)


# --- the check itself -------------------------------------------------------------------------------------

def test_main_s_own_gate_code_passes(primary):
    assert errors() == []


def test_a_worktree_equal_to_main_passes(primary, monkeypatch):
    worktree(primary, monkeypatch)    # the rest of #243's slices merge from a checkout like this
    assert errors() == []


@pytest.mark.parametrize("path", sorted(REAL))
@pytest.mark.parametrize("change", [b"\n", b"# loosened\n"])   # a trailing newline too: the bytes, not stripped text
def test_a_worktree_whose_gate_code_differs_is_sent_to_the_primary(primary, monkeypatch, path, change):
    wt = worktree(primary, monkeypatch)
    (wt / path).write_bytes(REAL[path] + change)
    [err] = errors()
    assert f"{path} in {wt.resolve()} differs from origin/main's" in err
    assert f"primary checkout ({primary.resolve()})" in err
    assert f"`cd {primary.resolve()} && uv run python tools/sdlc.py merge 50`" in err
    assert "pull --ff-only" not in err      # the primary is up to date: just go there


def test_a_committed_change_on_the_branch_is_refused_too(primary, monkeypatch):
    wt = worktree(primary, monkeypatch)
    (wt / "tools/sdlc.py").write_bytes(REAL["tools/sdlc.py"].replace(b"def ", b"def  ", 1))
    _git("commit", "-q", "-am", "loosen the gate", cwd=wt)
    assert "tools/sdlc.py in" in errors()[0]


def test_a_stale_primary_is_named_with_how_to_update_it(primary, monkeypatch, tmp_path):
    other = tmp_path / "other"
    _git("clone", "-q", str(tmp_path / "origin.git"), str(other), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git("config", k, v, cwd=other)
    (other / "tools/sdlc_quick.py").write_bytes(REAL["tools/sdlc_quick.py"] + b"# main moved on\n")
    _git("commit", "-q", "-am", "main moves", cwd=other)
    _git("push", "-q", "origin", "main", cwd=other)
    [err] = errors()                       # run from the primary itself, now behind origin/main
    assert f"tools/sdlc_quick.py in {primary.resolve()} differs" in err
    assert f"Update this, the primary checkout: `git -C {primary.resolve()} pull --ff-only`" in err
    worktree(primary, monkeypatch, "main")   # from a worktree of the old main: go to the primary, and update it first
    [err] = errors()
    assert f"update it first with `git -C {primary.resolve()} pull --ff-only`" in err
    _git("merge", "-q", "--ff-only", "origin/main", cwd=primary)   # what `pull --ff-only` does
    assert "update it first" not in errors()[0]   # the primary is current; the worktree still isn't


def test_a_failed_fetch_is_a_refusal(primary):
    _git("remote", "set-url", "origin", str(primary.parent / "gone.git"), cwd=primary)
    [err] = errors()
    assert "could not read origin/main's gate code" in err and "nothing was merged" in err
    assert str(primary.resolve()) in err


def test_a_file_missing_on_main_is_a_refusal(primary):
    _git("rm", "-q", "tools/sdlc_quick.py", cwd=primary)
    _git("commit", "-q", "-m", "drop it", cwd=primary)
    _git("push", "-q", "origin", "main", cwd=primary)
    (primary / "tools/sdlc_quick.py").write_bytes(REAL["tools/sdlc_quick.py"])   # still running a copy
    [err] = errors()
    assert "could not read origin/main's gate code" in err and "tools/sdlc_quick.py" in err


def test_a_missing_running_copy_is_a_difference():
    assert sdlc.gate_code_drift({"tools/sdlc.py": b"x"}, {"tools/sdlc.py": b"x", "tools/sdlc_quick.py": b"y"}) \
        == ["tools/sdlc_quick.py"]
    assert sdlc.gate_code_drift({p: b"x" for p in REAL}, {p: b"x" for p in REAL}) == []


def test_the_fetch_bound_is_validated(primary, monkeypatch):
    monkeypatch.setattr(sdlc, "GATE_FETCH_SECONDS", 0)
    with pytest.raises(sdlc.SdlcError, match="GATE_FETCH_SECONDS must be positive"):
        errors()


# --- `merge` runs the check first, for every lane, --dry-run too --------------------------------------------

def no_github(monkeypatch):
    for name in ("gh", "gh_json", "gh_pages", "fetch_pr", "post_comment"):
        monkeypatch.setattr(sdlc, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} reached before the check"))


@pytest.mark.parametrize("flags", [[], ["--dry-run"]])
def test_merge_from_a_differing_checkout_reads_and_merges_nothing(primary, monkeypatch, capsys, flags):
    wt = worktree(primary, monkeypatch)
    (wt / "tools/sdlc.py").write_bytes(REAL["tools/sdlc.py"] + b"\n")
    no_github(monkeypatch)   # neither a slice's PR nor a quick one is read, so neither lane can merge
    assert sdlc.main(["merge", "50", *flags]) == 1
    err = capsys.readouterr().err
    assert "differs from origin/main's" in err and f"cd {primary.resolve()} && uv run python tools/sdlc.py merge 50" in err


@pytest.mark.parametrize("flags", [[], ["--dry-run"]])
def test_merge_from_a_failed_fetch_merges_nothing(primary, monkeypatch, capsys, flags):
    _git("remote", "set-url", "origin", str(primary.parent / "gone.git"), cwd=primary)
    no_github(monkeypatch)
    assert sdlc.main(["merge", "50", *flags]) == 1
    assert "could not read origin/main's gate code" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [[], ["--dry-run"]])
def test_merge_from_main_s_code_goes_on_to_the_gate(primary, monkeypatch, capsys, flags):
    reached = []

    def fetch_pr(n, config):
        reached.append(n)
        raise sdlc.SdlcError("the PR gate was reached")
    monkeypatch.setattr(sdlc, "fetch_pr", fetch_pr)
    assert sdlc.main(["merge", "50", *flags]) == 1
    assert reached == [50] and "the PR gate was reached" in capsys.readouterr().err


# --- `merge PR` is named when it is due ---------------------------------------------------------------------

def slice_bundle(labels=("plan:slice",), pr_comments=()):
    return {"issue": slice_issue(labels=labels), "comments": [agent("claim", 1, branch="slice/28")], "trusted": ["owner"],
            "blockers": [], "pr": slice_pr(), "pr_comments": list(pr_comments)}


def slice_status(monkeypatch, capsys, b, gate, *flags):
    monkeypatch.setattr(sdlc, "slice_bundle", lambda n, config: b)
    seen = []
    monkeypatch.setattr(sdlc, "slice_merge_errors",
                        lambda pr, comments, issue, epic, config, trusted: seen.append((pr["number"], epic)) or (gate, None))
    assert sdlc.main(["slice-status", str(b["issue"]["number"]), *flags]) == 0
    out = capsys.readouterr().out
    return out, seen


def test_slice_status_names_the_merge_with_the_gate_s_refusals(monkeypatch, capsys):
    b = slice_bundle(pr_comments=[ran_on(HEAD), review_on(HEAD, "approve", 6)])
    out, seen = slice_status(monkeypatch, capsys, b, ["the head is 1 commit(s) behind epic/12: rebase"])
    assert seen == [(50, 12)]
    assert ("next: uv run python tools/sdlc.py merge 50 (from the primary checkout, on an up-to-date main; "
            "gate still refuses: the head is 1 commit(s) behind epic/12: rebase)") in out
    st = json.loads(slice_status(monkeypatch, capsys, b, [], "--json")[0])
    assert st["merge_due"] and st["gate"] == [] and st["next"].endswith("gate: ok)")


def test_a_routine_slice_with_its_review_owed_is_due_too(monkeypatch, capsys):
    b = slice_bundle(labels=("plan:slice", "complexity:routine"), pr_comments=[ran_on(HEAD)])
    out, _ = slice_status(monkeypatch, capsys, b, [])
    assert "next: uv run python tools/sdlc.py merge 50 (" in out and "gate: ok" in out


@pytest.mark.parametrize("records", [[ran_on(HEAD)],                                        # no review, not owed
                                     [ran_on(HEAD, "fail"), review_on(HEAD, "approve", 6)],  # failed tests
                                     [ran_on(HEAD), review_on(HEAD, "changes", 6)],          # changes asked
                                     [ran_on("b" * 40), review_on("b" * 40, "approve", 6)]])  # an older head
def test_a_slice_without_both_records_on_its_head_is_still_being_worked(monkeypatch, capsys, records):
    out, seen = slice_status(monkeypatch, capsys, slice_bundle(pr_comments=records), [])
    assert "next: /work-slice 28" in out and seen == []


def quick_in_review(pr_comments=RECORDS):
    return quick_bundle([ROUTINE, "sdlc:in-progress"], ready_quick() + [CLAIM], pr=QUICK_PR, pr_comments=pr_comments)


def stub_quick_gate(monkeypatch, gate):
    seen = []
    monkeypatch.setattr(sdlc, "quick_gate_bundle", lambda n, config: seen.append(n) or {"read": n})
    monkeypatch.setattr(sdlc, "quick_gate_errors", lambda b, config: gate if b == {"read": 50} else pytest.fail(b))
    return seen


def test_slice_status_of_a_quick_pr_names_the_merge_with_the_quick_gate(monkeypatch, capsys):
    seen = stub_quick_gate(monkeypatch, ["GitHub is still computing mergeability (UNKNOWN)"])
    monkeypatch.setattr(sdlc, "slice_bundle", lambda n, config: quick_in_review())
    monkeypatch.setattr(sdlc, "slice_merge_errors", lambda *a: pytest.fail("a quick PR has no slice gate"))
    assert sdlc.main(["slice-status", "12"]) == 0
    out = capsys.readouterr().out
    assert seen == [50]
    assert "next: uv run python tools/sdlc.py merge 50 (from the primary checkout, on an up-to-date main; " \
           "gate still refuses: GitHub is still computing mergeability (UNKNOWN))" in out


def test_a_quick_pr_never_counts_a_deferral(monkeypatch, capsys):
    stub_quick_gate(monkeypatch, [])
    defer = agent("review-deferred", 8, sha=HEAD)
    monkeypatch.setattr(sdlc, "slice_bundle", lambda n, config: quick_in_review([RECORDS[0], defer]))
    assert sdlc.main(["slice-status", "12"]) == 0
    assert "next: /work-slice 12 --quick" in capsys.readouterr().out


def quick_state(monkeypatch, capsys, b, *flags):
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, config, trusted=None: b)
    monkeypatch.setattr(sdlc, "find_slice_pr", lambda n, config: b["pr"])
    monkeypatch.setattr(sdlc, "fetch_pr", lambda n, config: (b["pr"], b["pr_comments"]))
    args = argparse.Namespace(number=12, json="--json" in flags, from_file=None)
    assert sdlc.command_state(args, CONFIG) == 0
    return capsys.readouterr().out


def test_state_of_a_quick_issue_names_the_merge_with_the_gate(monkeypatch, capsys):
    seen = stub_quick_gate(monkeypatch, [])
    out = quick_state(monkeypatch, capsys, quick_in_review())
    assert seen == [50]
    assert "next: uv run python tools/sdlc.py merge 50 (from the primary checkout, on an up-to-date main; gate: ok)" in out
    stub_quick_gate(monkeypatch, ["the tests failed on aaaaaaaaaaaa"])
    st = json.loads(quick_state(monkeypatch, capsys, quick_in_review(), "--json"))
    assert st["next"].endswith("gate still refuses: the tests failed on aaaaaaaaaaaa)")


def test_state_of_a_quick_issue_still_in_review_is_built(monkeypatch, capsys):
    stub_quick_gate(monkeypatch, [])
    out = quick_state(monkeypatch, capsys, quick_in_review([RECORDS[0]]))   # tests, no review yet
    assert "next: /work-slice 12 --quick" in out
