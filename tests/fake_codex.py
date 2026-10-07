"""A fake `codex` for the Codex-review tests: a script on PATH that logs what it was run with (argv, cwd, its
CODEX_HOME's contents) and prints a canned report, and a fake user Codex home under a tmp HOME. Import
`no_real_codex_home` into a test module to make it autouse there: never the real ~/.codex."""
import json
import os
import pwd
import sys
from pathlib import Path

import pytest

from tools import codex_review

FAKE_CODEX = f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys, time
if sys.argv[1:] == ["--version"]:
    with open(os.environ["FAKE_CODEX_LOG"] + ".version", "a") as f:
        f.write("--version\\n")
    print("codex-cli 9.9.9-test")
    sys.exit(0)
log = pathlib.Path(os.environ["FAKE_CODEX_LOG"])
n = len(log.read_text().splitlines()) if log.exists() else 0
home = pathlib.Path(os.environ.get("CODEX_HOME", "/nonexistent"))
seen = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin_tty": sys.stdin.isatty(), "codex_home": str(home),
         "entries": {{p.name: p.is_symlink() for p in home.iterdir()}} if home.is_dir() else None,
         "config": (home / "config.toml").read_text() if (home / "config.toml").exists() else None,
         "auth_mode": (home / "auth.json").stat().st_mode & 0o777 if (home / "auth.json").exists() else None}}
with log.open("a") as f:
    f.write(json.dumps(seen) + "\\n")
plan = json.loads(pathlib.Path(os.environ["FAKE_CODEX_PLAN"]).read_text())
step = plan[min(n, len(plan) - 1)]
# the session log, in the shape codex-cli 0.161.0 writes (`codex_review.check_session` reads it): skills offered
# unless the config turns them off; a plan step's `session` overrides it: "skills" (offered whatever the config
# says), "unknown" (a shape nobody knows), "none" (no log); `agents_md` names a directory an AGENTS.md came from
kind = step.get("session", "ok" if "include_instructions = false" in (seen["config"] or "") else "skills")
if kind != "none" and home.is_dir():
    state = {{"skills": {{"includeInstructions": kind == "skills"}}, "host_skills": {{"includeInstructions": kind == "skills"}},
             "agents_md": {{"directory": step["agents_md"], "text": "x"}} if step.get("agents_md") else {{}}}}
    records = [{{"type": "session_meta", "payload": {{"cwd": os.getcwd()}}}}]
    if kind == "skills":
        records.append({{"type": "response_item", "payload": {{"type": "message", "role": "developer",
                        "content": [{{"type": "input_text", "text": "<skills_instructions> ## Skills"}}]}}}})
    records.append({{"type": "world_state", "payload": {{"full": True, "state": state}}}} if kind != "unknown"
                   else {{"type": "world_state_v9", "payload": {{"everything": "different"}}}})
    day = home / "sessions" / "2026" / "10" / "07"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"rollout-{{n}}-{{os.getpid()}}.jsonl").write_text("".join(json.dumps(r) + "\\n" for r in records))
if step.get("user_signs_in"):             # a sign-in elsewhere while the review runs
    pathlib.Path(os.environ["HOME"], ".codex", "auth.json").write_text(step["user_signs_in"])
if step.get("refresh"):                   # Codex's sign-in refreshed: a temp file renamed over auth.json
    tmp = home / "auth.json.tmp"
    tmp.write_text(step["refresh"])
    os.replace(tmp, home / "auth.json")
if step.get("refresh_in_place"):          # what Codex's file store does: open, truncate, write
    (home / "auth.json").write_text(step["refresh_in_place"])
if step.get("edit"):
    pathlib.Path(step["edit"]).write_text("edited during the review\\n")
if step.get("commit"):
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "moved"], check=True)
time.sleep(step.get("sleep", 0))
sys.stdout.write(step.get("out", ""))
sys.stderr.write("codex progress noise\\n")
sys.exit(step.get("exit", 0))
"""
APPROVES = {"out": "1. fine (non-blocking)\n\nVERDICT: approve\n"}
NO_VERDICT = {"out": "I looked around and ran out of time.\n"}
SLOW = {"sleep": 5, "out": "VERDICT: approve\n"}
SETTINGS = {"model": "test-model", "reasoning_effort": "low", "sandbox": "read-only", "timeout_seconds": 30,
            "flags": ["--skip-git-repo-check"], "skills": "suppressed"}
QUICK = {**SETTINGS, "timeout_seconds": 1}


SIGNED_IN = '{"tokens": "the original"}'
# the user's own Codex home, in the tmp HOME: everything a review must not pick up
USER_CODEX = {"config.toml": 'model = "user-model"\nmodel_reasoning_effort = "minimal"\n[mcp_servers.x]\ncommand = "x"\n',
              "AGENTS.md": "user instructions", "skills/s/SKILL.md": "a skill", "memories/m.md": "a memory",
              "rules/r.rules": "a rule", "plugins/p.json": "{}"}


@pytest.fixture(autouse=True)
def no_real_codex_home(tmp_path, monkeypatch):
    """No test here reads or writes the real ~/.codex: HOME is a tmp dir and CODEX_HOME unset."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert codex_review.user_codex_home() == tmp_path / "home" / ".codex"




def install(tmp_path, monkeypatch, plan=(APPROVES,)):
    """The fake `codex` first on PATH and a signed-in user Codex home holding everything a review must not pick up.
    Returns (the call log, the plan file, the user's auth.json)."""
    user = codex_review.user_codex_home()
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    assert not user.resolve().is_relative_to(real) and user.resolve().is_relative_to(tmp_path.resolve().parents[1]), \
        f"refusing to write a fake Codex home at {user}"
    for name, text in USER_CODEX.items():
        (user / name).parent.mkdir(parents=True, exist_ok=True)
        (user / name).write_text(text)
    auth = user / "auth.json"
    auth.write_text(SIGNED_IN)
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "codex").write_text(FAKE_CODEX)
    (bin_ / "codex").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    log, plan_file = tmp_path / "codex.log", tmp_path / "plan.json"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("FAKE_CODEX_PLAN", str(plan_file))
    plan_file.write_text(json.dumps(list(plan)))
    return log, plan_file, auth


def calls(log):
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
