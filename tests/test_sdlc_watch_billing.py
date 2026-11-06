"""tools/sdlc_watch_billing.py: the Max-only billing check and the run's environment (#263 T1.3).

Offline: a fake `claude` on a temp PATH (it records every call and the environment it saw, and
answers from a file beside it), a temp HOME and temp checkouts. No real `claude`, no real sign-in."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import sdlc, sdlc_watch as w, sdlc_watch_billing as b

MAX = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max",
       "email": "someone@example.com", "orgName": "x"}
KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")

FAKE = """#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}}) + "\\n")
answer = open(os.path.join(here, "answer")).read()
if answer == "sleep":
    time.sleep(30)
code = int(open(os.path.join(here, "exit")).read() or 0) if os.path.exists(os.path.join(here, "exit")) else 0
sys.stdout.write(answer)
sys.exit(code)
"""


class Fake:
    """A fake `claude` in its own bin directory."""

    def __init__(self, bindir: Path):
        self.bindir = bindir
        bindir.mkdir(parents=True, exist_ok=True)
        exe = bindir / "claude"
        exe.write_text(FAKE.format(python=sys.executable))
        exe.chmod(0o755)
        self.answer(MAX)

    def answer(self, data, exit_code=0):
        (self.bindir / "answer").write_text(data if isinstance(data, str) else json.dumps(data))
        (self.bindir / "exit").write_text(str(exit_code))

    @property
    def calls(self):
        path = self.bindir / "calls.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def fake(tmp_path):
    return Fake(tmp_path / "bin")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    return h


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "checkout"
    (p / ".claude").mkdir(parents=True)
    return p


@pytest.fixture
def parent(fake, home):
    """The watcher's own environment: a fake claude first on PATH, a temp HOME, no route."""
    return {"HOME": str(home), "PATH": f"{fake.bindir}{os.pathsep}/usr/bin{os.pathsep}/bin", "USER": "someone",
            "LANG": "C"}


@pytest.fixture
def managed(tmp_path):
    return tmp_path / "managed" / "managed-settings.json"


def check(project, parent, managed, **kw):
    return b.billing_check([project], parent=parent, managed=managed, **kw)


# --- the run's environment ------------------------------------------------------------------

def test_run_env_is_the_allowlist_and_nothing_else():
    parent = {k: f"v-{k}" for k in (*b.ALLOW_ENV, *b.NEVER_ENV, "VIRTUAL_ENV", "ANTHROPIC_BASE_URL", "GH_TOKEN")}
    env = b.run_env(parent)
    assert env == {**{k: f"v-{k}" for k in b.ALLOW_ENV}, b.WATCH_RUN_ENV: "1"}


def test_the_allowlist_never_keeps_a_route_or_what_turns_the_advisor_off():
    assert not set(b.ALLOW_ENV) & set(b.NEVER_ENV)
    assert set(b.ROUTE_ENV) <= set(b.NEVER_ENV)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_DISABLE_ADVISOR_TOOL",
                 "DISABLE_TELEMETRY"):
        assert name in b.NEVER_ENV
        assert name not in b.run_env({name: "1", "HOME": "/h"})


def test_an_allowlist_edit_that_would_leak_a_route_fails(monkeypatch):
    monkeypatch.setattr(b, "ALLOW_ENV", (*b.ALLOW_ENV, "ANTHROPIC_API_KEY"))
    with pytest.raises(AssertionError, match="ANTHROPIC_API_KEY"):
        b.run_env({"ANTHROPIC_API_KEY": "sk-x"})


def test_run_env_defaults_to_the_watchers_own_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setenv("HOME", "/somewhere")
    env = b.run_env()
    assert env["HOME"] == "/somewhere" and "ANTHROPIC_API_KEY" not in env


def test_the_watch_run_marker_is_the_same_everywhere():
    assert b.WATCH_RUN_ENV == w.WATCH_RUN_ENV == sdlc.WATCH_RUN_ENV


def test_keys_in_the_parent_never_reach_claude_with_the_check_bypassed(fake, parent, project):
    """Criterion 17: the parent holds the keys; with the route check skipped, `claude` sees none of them."""
    leaky = {**parent, **{k: "secret-" + k for k in KEYS}}
    subprocess.run([str(fake.bindir / "claude"), "-p", "/work-slice 1"], env=b.run_env(leaky), cwd=project,
                   check=True, capture_output=True, timeout=30)
    problem, _ = b.check_auth(b.run_env(leaky), project)              # the auth call, with no route check first
    assert problem == ""
    assert len(fake.calls) == 2
    for call in fake.calls:
        assert not set(KEYS) & set(call["env"])
        assert not any("secret-" in v for v in call["env"].values())
        assert call["env"][b.WATCH_RUN_ENV] == "1"


# --- the routes: each one refuses with nothing run, and status names it ----------------------

@pytest.mark.parametrize("name", b.ROUTE_ENV)
@pytest.mark.parametrize("value", ["sk-ant-x", ""])
def test_a_route_in_the_watchers_environment_refuses_and_runs_nothing(fake, parent, project, managed, name, value):
    got = check(project, {**parent, name: value}, managed)
    assert not got.ok and not got.ran_claude and fake.calls == []
    assert got.routes == (f"{name} in the watcher's environment",)
    assert got.line.startswith("REFUSING every run: ") and name in got.line


def _settings(fake_scope, home, project, managed):
    return {"user": home / ".claude" / "settings.json",
            "project": project / ".claude" / "settings.json",
            "local": project / ".claude" / "settings.local.json",
            "managed": managed}[fake_scope]


@pytest.mark.parametrize("scope", ["user", "project", "local", "managed"])
def test_an_api_key_helper_in_any_settings_file_refuses(fake, parent, home, project, managed, scope):
    path = _settings(scope, home, project, managed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"apiKeyHelper": "~/bin/key.sh", "permissions": {}}))
    got = check(project, parent, managed)
    assert not got.ok and fake.calls == []
    assert got.routes == (f"apiKeyHelper in {scope} settings {path}",)


@pytest.mark.parametrize("scope", ["user", "project", "local", "managed"])
@pytest.mark.parametrize("name", b.ROUTE_ENV)
def test_a_route_under_a_settings_env_refuses(fake, parent, home, project, managed, scope, name):
    path = _settings(scope, home, project, managed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {"FOO": "bar", name: "1"}}))
    got = check(project, parent, managed)
    assert not got.ok and fake.calls == []
    assert got.routes == (f"{name} under env in {scope} settings {path}",)


def test_harmless_settings_pass(fake, parent, home, project, managed):
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {"FOO": "1"}, "model": "opus"}))
    (project / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    got = check(project, parent, managed)
    assert got.ok and got.line == b.OK_LINE and len(fake.calls) == 1


def test_the_user_settings_come_from_the_runs_home(fake, parent, tmp_path, project, managed):
    """Not from this process's HOME: the run reads the settings under the HOME it gets."""
    other = tmp_path / "other-home" / ".claude"
    other.mkdir(parents=True)
    (other / "settings.json").write_text(json.dumps({"apiKeyHelper": "x"}))
    got = check(project, {**parent, "HOME": str(other.parent)}, managed)
    assert got.routes == (f"apiKeyHelper in user settings {other / 'settings.json'}",)


def test_every_checkout_given_is_scanned_and_every_route_named(fake, parent, home, project, tmp_path, managed):
    primary = tmp_path / "primary"
    (primary / ".claude").mkdir(parents=True)
    (primary / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "t"}}))
    got = b.billing_check([project, primary], parent={**parent, "CLAUDE_CODE_USE_VERTEX": "1"}, managed=managed)
    assert got.routes == ("CLAUDE_CODE_USE_VERTEX in the watcher's environment",
                          f"ANTHROPIC_AUTH_TOKEN under env in local settings {primary / '.claude' / 'settings.local.json'}")
    assert fake.calls == []


@pytest.mark.parametrize("text,why", [("{not json", "not valid JSON"), ("[]", "not a JSON object"),
                                      ('{"env": ["ANTHROPIC_API_KEY"]}', "not an object")])
def test_a_settings_file_that_cant_be_checked_refuses(fake, parent, project, managed, text, why):
    (project / ".claude" / "settings.local.json").write_text(text)
    got = check(project, parent, managed)
    assert not got.ok and fake.calls == [] and why in got.line


def test_settings_are_only_read(fake, parent, home, project, managed):
    path = home / ".claude" / "settings.json"
    path.write_text('{"env": {"ANTHROPIC_API_KEY": "sk"}}')
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    check(project, parent, managed)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


# --- the sign-in: only claude.ai / firstParty / max passes ------------------------------------

def test_a_max_sign_in_passes_and_the_check_runs_with_exactly_the_runs_env(fake, parent, project, managed,
                                                                         monkeypatch):
    seen = {}

    class Spy(subprocess.Popen):
        def __init__(self, argv, **kw):
            seen.update(argv=argv, **kw)
            super().__init__(argv, **kw)

        def communicate(self, *a, **kw):
            seen.setdefault("timeout", kw.get("timeout"))
            return super().communicate(*a, **kw)

    monkeypatch.setattr(b.subprocess, "Popen", Spy)
    got = check(project, {**parent, "SOMETHING_ELSE": "x"}, managed)
    assert got.ok and got.ran_claude and got.auth["subscriptionType"] == "max"
    assert seen["env"] == b.run_env(parent)
    assert seen["argv"] == [str(fake.bindir / "claude"), "auth", "status", "--json"]
    assert "--bare" not in seen["argv"]
    assert seen["cwd"] == project and seen["stdin"] is subprocess.DEVNULL
    assert 0 < seen["timeout"] <= b.AUTH_TIMEOUT_S
    call, = fake.calls
    assert call["argv"] == ["auth", "status", "--json"]
    assert Path(call["cwd"]).resolve() == project.resolve()
    assert "SOMETHING_ELSE" not in call["env"]


def test_the_check_runs_where_the_run_starts(fake, parent, project, tmp_path, managed):
    start = tmp_path / "pinned"
    start.mkdir()
    assert b.billing_check([project], cwd=start, parent=parent, managed=managed).ok
    assert Path(fake.calls[0]["cwd"]).resolve() == start.resolve()


@pytest.mark.parametrize("answer,why", [
    ({**MAX, "authMethod": "console"}, "authMethod 'console', not 'claude.ai'"),
    ({**MAX, "authMethod": "api_key"}, "authMethod 'api_key'"),
    ({**MAX, "apiProvider": "bedrock"}, "apiProvider 'bedrock', not 'firstParty'"),
    ({**MAX, "subscriptionType": "pro"}, "subscriptionType 'pro', not 'max'"),
    ({**MAX, "subscriptionType": None}, "subscriptionType None"),
    ({**MAX, "loggedIn": False}, "loggedIn False"),
    ({**MAX, "loggedIn": "true"}, "loggedIn 'true'"),
    ({k: v for k, v in MAX.items() if k != "apiProvider"}, "apiProvider None"),
    ("Not logged in. Run claude /login", "no JSON object"),
    ("[1, 2]", "no JSON object"),
    ("", "no output"),
])
def test_anything_but_a_max_claude_ai_sign_in_refuses(fake, parent, project, managed, answer, why):
    fake.answer(answer)
    got = check(project, parent, managed)
    assert not got.ok and got.ran_claude and not got.routes
    assert why in got.reason and got.line == "REFUSING every run: " + got.reason


def test_a_failing_auth_status_refuses_even_with_a_max_answer(fake, parent, project, managed):
    fake.answer(MAX, exit_code=1)
    got = check(project, parent, managed)
    assert not got.ok and "exited 1" in got.reason


def test_no_claude_on_the_runs_path_refuses(fake, parent, project, managed, tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    got = check(project, {**parent, "PATH": str(empty)}, managed)
    assert not got.ok and "not on the run's PATH" in got.reason and fake.calls == []


def test_a_hung_auth_status_is_stopped_and_refuses(fake, parent, project, managed):
    fake.answer("sleep")
    got = check(project, parent, managed, timeout_s=0.5)
    assert not got.ok and "no answer in 0.5s" in got.reason


def test_require_billing_raises_on_a_refusal(fake, parent, project, managed):
    assert b.require_billing([project], parent=parent, managed=managed).ok
    fake.answer({**MAX, "authMethod": "console"})
    with pytest.raises(b.BillingRefused, match="REFUSING every run: .*console") as exc:
        b.require_billing([project], parent=parent, managed=managed)
    assert not exc.value.billing.ok


def test_billing_check_needs_a_checkout(parent):
    with pytest.raises(ValueError):
        b.billing_check([], parent=parent)


# --- status ---------------------------------------------------------------------------------

def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch, fake, home, managed):
    """A checkout for `status`, the watcher's environment pointed at the fake and the temp HOME."""
    root = tmp_path / "primary"
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    (root / ".sdlc").mkdir()
    (root / ".sdlc" / "config.json").write_text(json.dumps({"schema": 1}))
    for name in (*b.NEVER_ENV, w.WATCH_RUN_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{fake.bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(b, "MANAGED_SETTINGS", managed)
    monkeypatch.setattr(w, "HERE", root)
    return root


def test_status_shows_the_billing_line_and_the_credits_reminder(repo, fake, capsys):
    assert w.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"  billing:  {b.OK_LINE}\n            {w.CREDITS_REMINDER}" in out
    assert len(fake.calls) == 1 and fake.calls[0]["argv"] == ["auth", "status", "--json"]


def test_status_names_a_route_and_runs_nothing(repo, fake, home, capsys, monkeypatch):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"apiKeyHelper": "x"}))
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    assert w.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "billing:  REFUSING every run: CLAUDE_CODE_USE_BEDROCK in the watcher's environment; " \
           f"apiKeyHelper in local settings {repo / '.claude' / 'settings.local.json'}" in out
    assert w.CREDITS_REMINDER in out and fake.calls == []
    log = [json.loads(l) for l in (repo / "logs" / "sdlc_watch" / w.LOG_NAME).read_text().splitlines()]
    assert log[-1]["billing"] == "refused"


def test_status_says_why_a_sign_in_is_refused(repo, fake, capsys):
    fake.answer({**MAX, "authMethod": "console", "subscriptionType": None})
    assert w.main(["status"]) == 0
    assert "billing:  REFUSING every run: claude auth status says authMethod 'console'" in capsys.readouterr().out


def test_status_scans_the_pinned_checkout_and_checks_auth_there(repo, fake, tmp_path, capsys):
    pinned = tmp_path / "pinned"
    (pinned / ".claude").mkdir(parents=True)
    w.write_record(repo / "logs" / "sdlc_watch", {**w.empty_record(), "pin": {"checkout": str(pinned)}})
    assert w.main(["status"]) == 0
    assert Path(fake.calls[0]["cwd"]).resolve() == pinned.resolve()
    (pinned / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "k"}}))
    capsys.readouterr()
    assert w.main(["status"]) == 0
    assert f"ANTHROPIC_API_KEY under env in local settings {pinned}" in capsys.readouterr().out
    assert len(fake.calls) == 1


def test_a_run_env_with_no_home_refuses_and_runs_nothing(fake, parent, project, managed):
    got = check(project, {k: v for k, v in parent.items() if k != "HOME"}, managed)
    assert not got.ok and fake.calls == []
    assert got.routes == ("HOME is not set in the run's environment: the user settings can't be located",)


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), b.MAX_AUTH_TIMEOUT_S + 1, True, "30"])
def test_the_auth_timeout_is_finite_and_bounded(fake, parent, project, managed, bad):
    with pytest.raises(ValueError):
        check(project, parent, managed, timeout_s=bad)
    with pytest.raises(ValueError):
        b.check_auth(b.run_env(parent), project, timeout_s=bad)
    assert fake.calls == []
