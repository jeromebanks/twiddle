"""tools/sdlc_watch.py: the watcher's record, config, pause/resume and status (#263 T1.1). Offline: tmp git repos only."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools import sdlc, sdlc_watch as w, sdlc_watch_billing as billing

SOURCE = Path(w.__file__).resolve()
BILLING = Path(billing.__file__).resolve()


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A primary checkout with a bare origin holding main, and the watcher's code committed in it."""
    origin, root = tmp_path / "origin.git", tmp_path / "primary"
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git("config", k, v, cwd=root)
    (root / "tools").mkdir()
    shutil.copy(SOURCE, root / "tools" / "sdlc_watch.py")
    shutil.copy(BILLING, root / "tools" / "sdlc_watch_billing.py")
    (root / ".sdlc").mkdir()
    (root / ".sdlc" / "config.json").write_text(json.dumps({"schema": 1}))
    (root / ".gitignore").write_text(".worktrees/\nlogs/\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "main", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("push", "-q", "-u", "origin", "main", cwd=root)
    return root


FAKE_CLAUDE = f"""#!{sys.executable}
import json
print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                  "subscriptionType": "max"}}))
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No test reaches the real checkout's logs/sdlc_watch/, and none starts inside a watcher run.
    `status` runs the billing check: a fake `claude` comes first on PATH, under a temp HOME, so no
    test (nor a copy run as a script, which inherits this environment) reaches a real one."""
    monkeypatch.delenv(w.WATCH_RUN_ENV, raising=False)
    for name in billing.NEVER_ENV:
        monkeypatch.delenv(name, raising=False)
    fake_bin, fake_home = tmp_path / "fake-bin", tmp_path / "fake-home"
    fake_bin.mkdir()
    fake_home.mkdir()
    (fake_bin / "claude").write_text(FAKE_CLAUDE)
    (fake_bin / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(billing, "MANAGED_SETTINGS", tmp_path / "no-managed-settings.json")
    fallback = tmp_path / "unused-repo"
    monkeypatch.setattr(w, "HERE", fallback)    # a test that forgets to pass its repo fails, rather than writing here


def here(monkeypatch, where):
    monkeypatch.setattr(w, "HERE", Path(where))


def root_of(repo):
    return repo / "logs" / "sdlc_watch"


def log_lines(repo):
    path = root_of(repo) / w.LOG_NAME
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def record(repo):
    return json.loads((root_of(repo) / w.RECORD_NAME).read_text())


def test_the_watch_run_variable_is_sdlc_pys():
    assert w.WATCH_RUN_ENV == sdlc.WATCH_RUN_ENV


# --- config ----------------------------------------------------------------------------------

def test_watch_config_defaults_and_overrides():
    cfg = w.watch_config({})
    assert cfg == w.DEFAULT_WATCH
    assert cfg["interval_s"] == 60 and cfg["hang_timeout_s"] == 14400 and cfg["max_failures"] == 2
    assert w.watch_config({"watch": {"interval_s": 120, "max_failures": 3}})["interval_s"] == 120


def test_the_repos_config_has_the_watch_section_and_no_budget():
    config = json.loads((Path(__file__).resolve().parents[1] / ".sdlc" / "config.json").read_text())
    assert config["watch"] == w.DEFAULT_WATCH                    # the section, exactly the defaults the code holds
    assert w.watch_config(config) == w.DEFAULT_WATCH


def test_load_watch_config_reads_the_running_checkouts_section(repo):
    (repo / ".sdlc" / "config.json").write_text(json.dumps({"schema": 1, "watch": {"codex_backoff_s": 900}}))
    assert w.load_watch_config(repo)["codex_backoff_s"] == 900


@pytest.mark.parametrize("watch", [
    {"max_runs_per_day": 10}, {"max_budget_usd": 5}, {"max_turns": 50},      # no budgets, by any name
    {"interval_s": 0}, {"interval_s": -1}, {"interval_s": 1.5}, {"interval_s": True}, {"interval_s": "60"},
    {"hang_timeout_s": float("inf")}, {"hang_timeout_s": 10**9},
    {"tools_file": "/etc/x.json"}, {"tools_file": "../x.json"}, {"tools_file": ""},
])
def test_watch_config_refuses_unknown_keys_and_bad_values(watch):
    with pytest.raises(w.WatchError):
        w.watch_config({"watch": watch})


def test_watch_config_refuses_a_section_that_is_not_an_object():
    with pytest.raises(w.WatchError):
        w.watch_config({"watch": [1]})


# --- the first start: start time and pin --------------------------------------------------------

def test_first_start_records_the_start_time_and_the_pin(repo):
    at = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)
    rec = w.start(repo, at=at)
    head = _git("rev-parse", "HEAD", cwd=repo)
    assert rec["started_at"] == "2026-11-05T12:00:00+00:00"
    assert rec["pin"]["sha"] == head and rec["pin"]["ref"] == "origin/main"
    assert record(repo)["pin"]["sha"] == head
    assert rec["care"] == {} and rec["run"] == {"current": None, "last": None}
    # a second start keeps the first's time and pin
    assert w.start(repo, at=at + dt.timedelta(hours=1))["started_at"] == rec["started_at"]
    assert [l["event"] for l in log_lines(repo)] == ["start"]


def test_a_pin_on_an_epic_branch_is_accepted(repo):
    _git("switch", "-q", "-c", "epic/263", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "slice", cwd=repo)
    _git("push", "-q", "origin", "epic/263", cwd=repo)
    rec = w.start(repo)
    assert rec["pin"]["ref"] == "origin/epic/263"


@pytest.mark.parametrize("dirt", ["untracked", "modified", "staged"])
def test_a_dirty_checkout_is_refused_and_nothing_is_written(repo, dirt):
    if dirt == "untracked":
        (repo / "new.txt").write_text("x")
    elif dirt == "modified":
        (repo / "tools" / "sdlc_watch.py").write_text("# changed\n")
    else:
        (repo / "staged.txt").write_text("x")
        _git("add", "staged.txt", cwd=repo)
    with pytest.raises(w.WatchError, match="not clean"):
        w.start(repo)
    assert not (root_of(repo) / w.RECORD_NAME).exists()
    assert [(l["event"], l["outcome"]) for l in log_lines(repo)] == [("start", "refused")]


def test_an_unpushed_head_is_refused(repo):
    _git("commit", "-q", "--allow-empty", "-m", "local only", cwd=repo)
    with pytest.raises(w.WatchError, match="push it first"):
        w.start(repo)
    assert not (root_of(repo) / w.RECORD_NAME).exists()


@pytest.mark.parametrize("branch", ["epic/scratch", "epic/263/experiment", "epic/0", "epic/263x"])
def test_a_head_only_on_an_epic_named_branch_that_is_not_an_epic_is_refused(repo, branch):
    _git("switch", "-q", "-c", branch, cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "not an epic", cwd=repo)
    _git("push", "-q", "origin", branch, cwd=repo)
    with pytest.raises(w.WatchError, match="not on origin/main"):
        w.start(repo)


def test_a_head_on_another_pushed_branch_is_refused(repo):
    _git("switch", "-q", "-c", "slice/9", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "slice", cwd=repo)
    _git("push", "-q", "origin", "slice/9", cwd=repo)
    with pytest.raises(w.WatchError, match="not on origin/main"):
        w.start(repo)


def test_a_deleted_record_starts_empty_with_a_fresh_start_and_counts_only_new_actions(repo):
    t0 = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)
    rec = w.start(repo, at=t0)
    rec["care"] = {"243": {"since": w.iso(t0), "why": "comment"}}
    w.write_record(root_of(repo), rec)
    (root_of(repo) / w.RECORD_NAME).unlink()                       # a reboot, a wiped directory

    t1 = t0 + dt.timedelta(days=1)
    fresh = w.start(repo, at=t1)
    assert fresh["care"] == {} and fresh["started_at"] == w.iso(t1)
    assert not w.counts_as_new(fresh, t0 + dt.timedelta(hours=2))   # an action from before: nothing runs
    assert w.counts_as_new(fresh, t1 + dt.timedelta(minutes=1))     # a person acting again does count
    assert not w.counts_as_new(None, t1 + dt.timedelta(minutes=1))  # no record at all: nothing counts
    assert log_lines(repo)[-1]["fresh"] is True


def test_a_pause_made_before_the_first_start_is_kept_by_it(repo, monkeypatch):
    here(monkeypatch, repo)
    assert w.main(["pause", "7"]) == 0
    rec = w.start(repo)
    assert rec["pin"] and w.is_paused(rec, 7)


def test_an_unreadable_record_is_reported_never_replaced(repo, monkeypatch, capsys):
    root_of(repo).mkdir(parents=True)
    (root_of(repo) / w.RECORD_NAME).write_text("{not json")
    here(monkeypatch, repo)
    assert w.main(["pause"]) == 1
    assert "move it aside" in capsys.readouterr().err
    assert (root_of(repo) / w.RECORD_NAME).read_text() == "{not json"
    assert (log_lines(repo)[-1]["command"], log_lines(repo)[-1]["outcome"]) == ("pause", "error")
    assert w.main(["resume", "4"]) == 1
    assert (log_lines(repo)[-1]["command"], log_lines(repo)[-1]["issue"], log_lines(repo)[-1]["outcome"]) == \
        ("resume", 4, "error")
    with pytest.raises(w.WatchError):
        w.start(repo)


# --- pause / resume -----------------------------------------------------------------------------

def test_pause_and_resume_all_and_one_are_recorded_and_shown(repo, monkeypatch, capsys):
    here(monkeypatch, repo)
    assert w.main(["pause"]) == 0
    out = capsys.readouterr().out
    assert "paused all issues" in out and "PAUSED (all issues)" in out
    assert record(repo)["paused"]["all"] and w.is_paused(record(repo), 5)

    assert w.main(["pause", "100"]) == 0
    capsys.readouterr()
    assert w.main(["resume"]) == 0
    out = capsys.readouterr().out
    assert "resumed all issues" in out and "PAUSED" not in out.split("\n\n", 1)[1]
    assert "#100 (not in care)" in out                              # a one-issue pause outlives resuming all
    rec = record(repo)
    assert rec["paused"]["all"] is None and w.is_paused(rec, 100) and not w.is_paused(rec, 5)

    assert w.main(["resume", "100"]) == 0
    assert not w.is_paused(record(repo), 100)
    assert "resumed #100" in capsys.readouterr().out
    assert w.main(["resume", "100"]) == 0
    assert "was not paused" in capsys.readouterr().out
    # pause/resume never take the pin
    assert record(repo)["started_at"] is None and record(repo)["pin"] is None


def test_status_shows_an_issue_in_care_as_paused(repo, monkeypatch, capsys):
    rec = w.start(repo)
    rec["care"] = {"263": {"since": rec["started_at"]}}
    w.write_record(root_of(repo), rec)
    here(monkeypatch, repo)
    assert w.main(["pause", "263"]) == 0
    capsys.readouterr()
    assert w.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "#263 since" in out and "paused by you" in out and "not in care" not in out


def test_pause_works_from_a_dirty_checkout(repo, monkeypatch):
    (repo / "scratch.txt").write_text("x")
    here(monkeypatch, repo)
    assert w.main(["pause", "3"]) == 0
    assert w.is_paused(record(repo), 3)


@pytest.mark.parametrize("value", ["", "1"])
@pytest.mark.parametrize("argv", [["pause"], ["pause", "4"], ["resume"], ["resume", "4"]])
def test_pause_and_resume_refuse_inside_a_watcher_run(repo, monkeypatch, capsys, argv, value):
    here(monkeypatch, repo)
    path = root_of(repo) / w.RECORD_NAME
    if argv[0] == "resume":
        assert w.main(["pause", *argv[1:]]) == 0
    before = path.read_bytes() if path.exists() else None
    monkeypatch.setenv(w.WATCH_RUN_ENV, value)                     # even empty counts
    assert w.main(argv) == 2
    assert "refused" in capsys.readouterr().err
    assert (path.read_bytes() if path.exists() else None) == before
    last = log_lines(repo)[-1]
    assert last["command"] == argv[0] and last["outcome"] == "refused"


def test_status_runs_inside_a_watcher_run(repo, monkeypatch):
    here(monkeypatch, repo)
    monkeypatch.setenv(w.WATCH_RUN_ENV, "1")
    assert w.main(["status"]) == 0


# --- status, and the log ----------------------------------------------------------------------

def test_status_is_read_only_for_the_record(repo, monkeypatch, capsys):
    here(monkeypatch, repo)
    assert w.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "no record yet" in out and "usage credits" in out and "in care:  none" in out
    assert not (root_of(repo) / w.RECORD_NAME).exists()
    w.main(["pause"])
    before = (root_of(repo) / w.RECORD_NAME).read_bytes()
    w.main(["status"])
    assert (root_of(repo) / w.RECORD_NAME).read_bytes() == before


def test_status_shows_runs_waits_parks_and_failures(repo, monkeypatch, capsys):
    rec = w.start(repo)
    t = rec["started_at"]
    rec["run"] = {"current": {"issue": 243, "command": "/milestone-demo 243", "start": t},
                  "last": {"issue": 161, "command": "/work-slice 161", "outcome": "timeout", "end": t}}
    rec["waits"] = {"max": {"limit": "weekly", "until": t}, "parked": {"161": {"step": "review", "until": t}}}
    rec["issues"] = {"161": {"last_command": "/work-slice 161", "fingerprint": "abc", "no_progress": 1}}
    w.write_record(root_of(repo), rec)
    here(monkeypatch, repo)
    assert w.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "running:  #243 /milestone-demo 243" in out
    assert "last run: #161 /work-slice 161, timeout" in out
    assert "waiting on the weekly limit" in out and "#161 review parked" in out
    assert "#161: 1 run(s) with no progress" in out
    assert str(root_of(repo) / w.LOG_NAME) in out
    assert "budget" not in out.lower()


def test_every_command_appends_a_log_line(repo, monkeypatch):
    here(monkeypatch, repo)
    for argv in (["status"], ["pause"], ["pause", "9"], ["resume", "9"], ["resume"], ["status"]):
        assert w.main(argv) == 0
    lines = log_lines(repo)
    assert [(l["command"], l.get("issue")) for l in lines] == [
        ("status", None), ("pause", None), ("pause", 9), ("resume", 9), ("resume", None), ("status", None)]
    assert all(l["event"] == "command" and l["outcome"] == "ok" and l["ts"] for l in lines)


# --- one state root, whichever checkout runs the code ----------------------------------------------

def run_copy(script, cwd, *argv):
    env = {k: v for k, v in os.environ.items() if k != w.WATCH_RUN_ENV}
    return subprocess.run([sys.executable, str(script), *argv], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def test_record_and_log_land_in_the_primary_from_a_worktree_and_a_pinned_checkout(repo, tmp_path):
    slice_wt = repo / ".worktrees" / "slice-9"
    _git("worktree", "add", "-q", "-b", "slice/9", str(slice_wt), "origin/main", cwd=repo)
    pinned = tmp_path / "pinned"
    _git("worktree", "add", "-q", "--detach", str(pinned), _git("rev-parse", "HEAD", cwd=repo), cwd=repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    for checkout, argv in ((slice_wt, ["pause", "11"]), (pinned, ["pause", "12"]), (repo, ["status"])):
        proc = run_copy(checkout / "tools" / "sdlc_watch.py", elsewhere, *argv)
        assert proc.returncode == 0, proc.stderr
    for checkout in (slice_wt, pinned):
        assert not (checkout / "logs").exists()
    assert set(record(repo)["paused"]["issues"]) == {"11", "12"}
    assert [l["command"] for l in log_lines(repo)] == ["pause", "pause", "status"]
    assert str(root_of(repo).resolve()) in proc.stdout or str(root_of(repo)) in proc.stdout

    # and the pinned checkout's first start pins its own HEAD, into the same record
    rec = w.start(pinned)
    assert rec["pin"]["checkout"] == str(Path(_git("rev-parse", "--show-toplevel", cwd=pinned)))
    assert record(repo)["pin"]["sha"] == _git("rev-parse", "HEAD", cwd=pinned)
    assert w.state_root(slice_wt) == w.state_root(pinned) == w.state_root(repo)


def test_the_record_write_is_atomic_and_leaves_no_temp_files(repo):
    root = root_of(repo)
    rec = w.empty_record()
    for k in range(5):
        rec["paused"]["issues"][str(k)] = {"at": "x"}
        w.write_record(root, rec)
    assert sorted(p.name for p in root.iterdir()) == [w.RECORD_NAME]
    assert len(record(repo)["paused"]["issues"]) == 5


def test_a_held_lock_is_reported_after_a_bounded_wait_and_nothing_is_written(repo, monkeypatch, capsys):
    import fcntl
    root = root_of(repo)
    root.mkdir(parents=True)
    monkeypatch.setattr(w, "LOCK_WAIT_S", 0.2)
    here(monkeypatch, repo)
    with open(root / w.LOCK_NAME, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = run_copy(repo / "tools" / "sdlc_watch.py", repo, "status")    # a reader needs no lock
        assert proc.returncode == 0
        assert w.main(["pause"]) == 1
    assert "held for over" in capsys.readouterr().err
    last = log_lines(repo)[-1]
    assert (last["command"], last["outcome"]) == ("pause", "error") and "held for over" in last["detail"]
    assert not (root / w.RECORD_NAME).exists()


@pytest.mark.parametrize("where", ["write_record", "locked"])
@pytest.mark.parametrize("argv", [["pause"], ["resume", "4"]])
def test_a_filesystem_failure_is_reported_and_logged(repo, monkeypatch, capsys, where, argv):
    def fail(*a, **k):
        raise PermissionError(13, "Permission denied", "state.json")

    here(monkeypatch, repo)
    monkeypatch.setattr(w, where, fail)
    assert w.main(argv) == 1
    assert "Permission denied" in capsys.readouterr().err
    last = log_lines(repo)[-1]
    assert (last["command"], last.get("issue"), last["outcome"]) == (argv[0], int(argv[1]) if argv[1:] else None,
                                                                       "error")
    assert not (root_of(repo) / w.RECORD_NAME).exists()
