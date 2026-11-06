"""A Codex review that hits Codex's usage limit (#263 T2.1): told apart from the other can't-run causes, said on a
`codex-limit: reset=...` line, and recorded in the primary checkout's `logs/sdlc_watch/codex_limits.jsonl`.
Offline: the fake `codex` of `tests/fake_codex.py` replays trimmed fixtures of what codex-cli 0.166.0 writes."""
import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from tools import codex_review
from tests.fake_codex import APPROVES, NO_VERDICT, SETTINGS, install, no_real_codex_home  # noqa: F401
from tests.test_sdlc_review import Codex

FIX = Path(__file__).resolve().parent / "fixtures" / "codex_limit"
PST = dt.timezone(dt.timedelta(hours=-8))


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A primary checkout with one commit, and a `.worktrees/slice-7` checkout of it."""
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "t")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "t@example.invalid")
    monkeypatch.delenv(codex_review.RUN_ID_ENV, raising=False)
    primary = tmp_path / "primary"
    primary.mkdir()
    git(primary, "init", "-q")
    (primary / ".gitignore").write_text("logs/\n.worktrees/\n")
    git(primary, "add", ".gitignore")
    git(primary, "commit", "-qm", "base")
    git(primary, "worktree", "add", "-q", "-b", "slice/7", str(primary / ".worktrees" / "slice-7"))
    return primary


def review(tmp_path, monkeypatch, wt, *steps, target="pr 50"):
    log, _, _ = install(tmp_path, monkeypatch, steps)
    (tmp_path / "scratch").mkdir(exist_ok=True)
    lines = []
    status, report = codex_review.run_review(["codex", "exec", "the prompt"], wt, git(wt, "rev-parse", "HEAD"),
                                             tmp_path / "scratch", SETTINGS, log=lines.append, target=target)
    return status, report, "\n".join(lines), log


def limits(primary):
    f = primary / "logs" / "sdlc_watch" / "codex_limits.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


LIMITED = [  # (the fake run's step, whether its message gives a reset time)
    ({"rollout": str(FIX / "rollout_limit_at.jsonl"), "exit": 1}, True),
    ({"rollout": str(FIX / "rollout_limit_none.jsonl"), "exit": 1}, False),
    ({"stderr": str(FIX / "stderr_limit_at.txt"), "exit": 1}, True),
    ({"stderr": str(FIX / "stderr_limit_none.txt"), "exit": 1}, False),
]


@pytest.mark.parametrize("step,timed", LIMITED, ids=["rollout-at", "rollout-none", "stderr-at", "stderr-none"])
def test_each_limit_shape_is_exit_3_with_one_record_and_its_line(tmp_path, monkeypatch, repo, step, timed):
    status, report, out, log = review(tmp_path, monkeypatch, repo, step)
    assert status == codex_review.UNAVAILABLE == 3 and report is None
    assert len(log.read_text().splitlines()) == 1             # Codex ran once: a limit isn't retried
    (rec,) = limits(repo)                                     # not retried: one run, one record
    assert set(rec) == {"ts", "target", "reset", "run_id"}
    assert rec["target"] == "pr 50" and rec["run_id"] is None
    assert dt.datetime.fromisoformat(rec["ts"]).tzinfo is not None
    if timed:
        reset = dt.datetime.fromisoformat(rec["reset"])
        assert (reset.hour, reset.minute) == (20, 36) and reset.tzinfo is not None
        assert dt.timedelta(0) < reset - dt.datetime.fromisoformat(rec["ts"]) <= dt.timedelta(days=1)
    else:
        assert rec["reset"] is None
    assert f"codex-limit: reset={rec['reset'] or 'none'}" in out.splitlines()
    assert "usage limit" in out


@pytest.mark.parametrize("step", [
    NO_VERDICT,                                                                  # no verdict twice
    {**NO_VERDICT, "rollout": str(FIX / "rollout_quoted.jsonl")},                # the message only quoted in a tool's output
    {**NO_VERDICT, "stderr": str(FIX / "stderr_quoted.txt")},                    # ... or on stderr, not as Codex's ERROR line
    {**NO_VERDICT, "stderr": str(FIX / "stderr_limit_at.txt"),                   # the rollout says the run ended otherwise
     "rollout": str(FIX / "rollout_quoted.jsonl")},
], ids=["no-verdict", "quoted-rollout", "quoted-stderr", "rollout-wins"])
def test_another_cant_run_cause_gives_no_record(tmp_path, monkeypatch, repo, step):
    status, _, out, log = review(tmp_path, monkeypatch, repo, step)
    assert status == codex_review.UNAVAILABLE and len(log.read_text().splitlines()) == 2   # retried as before
    assert limits(repo) == [] and "codex-limit:" not in out


def test_an_approving_run_that_quotes_the_limit_gives_no_record(tmp_path, monkeypatch, repo):
    step = {**APPROVES, "rollout": str(FIX / "rollout_quoted.jsonl"), "stderr": str(FIX / "stderr_limit_at.txt")}
    status, report, out, _ = review(tmp_path, monkeypatch, repo, step)
    assert status == 0 and report.exists() and limits(repo) == [] and "codex-limit:" not in out


def test_no_sign_in_or_no_codex_gives_no_record(tmp_path, monkeypatch, repo):
    install(tmp_path, monkeypatch, (APPROVES,))
    (codex_review.user_codex_home() / "auth.json").unlink()
    lines = []
    for d in ("s1", "s2"):
        (tmp_path / d).mkdir()
    status, _ = codex_review.run_review(["codex", "exec", "p"], repo, git(repo, "rev-parse", "HEAD"),
                                        tmp_path / "s1", SETTINGS, log=lines.append)
    assert status == codex_review.UNAVAILABLE and limits(repo) == []
    install(tmp_path, monkeypatch, (APPROVES,))
    status, _ = codex_review.run_review(["no-such-codex-binary", "exec", "p"], repo, git(repo, "rev-parse", "HEAD"),
                                        tmp_path / "s2", SETTINGS, log=lines.append)
    assert status == codex_review.UNAVAILABLE and limits(repo) == []
    assert not any("codex-limit:" in l for l in lines)


def test_a_review_from_a_worktree_records_in_the_primary_checkout_with_its_run_id(tmp_path, monkeypatch, repo):
    monkeypatch.setenv(codex_review.RUN_ID_ENV, "run-42")
    wt = repo / ".worktrees" / "slice-7"
    status, _, out, _ = review(tmp_path, monkeypatch, wt, LIMITED[0][0], target="milestone 263 M1")
    assert status == codex_review.UNAVAILABLE
    assert not (wt / "logs").exists()
    (rec,) = limits(repo)
    assert rec["run_id"] == "run-42" and rec["target"] == "milestone 263 M1" and rec["reset"]
    assert git(wt, "status", "--porcelain") == ""


def test_codex_review_of_a_pr_names_the_pr_as_its_target(tmp_path, monkeypatch, capsys):
    c = Codex(tmp_path, monkeypatch, plan=({"rollout": str(FIX / "rollout_limit_at.jsonl"), "exit": 1},))
    monkeypatch.delenv(codex_review.RUN_ID_ENV, raising=False)
    assert c.run() == codex_review.UNAVAILABLE and len(c.calls()) == 1
    assert "codex-limit: reset=" in capsys.readouterr().out
    (rec,) = limits(c.wt)
    assert rec["target"] == "pr 50"


@pytest.mark.parametrize("message,now,want", [
    ("... or try again at 8:36 PM.", dt.datetime(2026, 11, 5, 14, 0, tzinfo=PST), dt.datetime(2026, 11, 5, 20, 36, tzinfo=PST)),
    ("try again at 8:36 PM.", dt.datetime(2026, 11, 5, 21, 0, tzinfo=PST), dt.datetime(2026, 11, 6, 20, 36, tzinfo=PST)),
    ("try again at 12:05 AM", dt.datetime(2026, 11, 5, 23, 0, tzinfo=PST), dt.datetime(2026, 11, 6, 0, 5, tzinfo=PST)),
    ("try again at 12:30 pm", dt.datetime(2026, 11, 5, 9, 0, tzinfo=PST), dt.datetime(2026, 11, 5, 12, 30, tzinfo=PST)),
    ("You've hit your usage limit.", dt.datetime(2026, 11, 5, 9, 0, tzinfo=PST), None),
    ("try again later", dt.datetime(2026, 11, 5, 9, 0, tzinfo=PST), None),
    ("try again at 13:00 PM", dt.datetime(2026, 11, 5, 9, 0, tzinfo=PST), None),
])
def test_the_reset_time_is_the_next_time_of_day_it_names(message, now, want):
    assert codex_review.parse_reset(message, now) == want


def test_a_record_that_cannot_be_written_still_prints_the_line(tmp_path, monkeypatch, repo):
    (repo / "logs").write_text("a file where the directory would go")
    lines = []
    rec = codex_review.record_limit(repo, "plan 9", "try again at 8:36 PM", lines.append,
                                    now=dt.datetime(2026, 11, 5, 14, 0, tzinfo=PST))
    assert rec["reset"] == "2026-11-05T20:36:00-08:00"
    assert any(l.startswith("warning: couldn't record") for l in lines)
    assert lines[-1] == "codex-limit: reset=2026-11-05T20:36:00-08:00"

