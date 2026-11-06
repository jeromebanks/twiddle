#!/usr/bin/env python3
"""The SDLC watcher's billing check and the environment a run gets (#263 T1.3).

Every Claude run the watcher starts must be on the Max subscription, never API billing.
Two things stand between an unattended run and an API bill, and both are here:

- `run_env()`: the environment a run gets, built from an allowlist (`ALLOW_ENV`), never
  inherited. Whatever the watcher's own environment holds, a run sees only those names,
  and never one in `NEVER_ENV` (a test keeps the two apart).
- `billing_check()`: before every run (and `--install`, M2), it refuses unless
  1. no API-billing route is in place (`find_routes`): none of `ROUTE_ENV` in the watcher's
     own environment, and no `apiKeyHelper` and none of `ROUTE_ENV` under `env` in any
     Claude Code settings file `claude -p` would read here (`settings_files`: managed, user
     from the run's `HOME`, and project and local for each checkout given). Settings are
     read, never written; one that can't be read refuses, since it can't be cleared;
  2. then, and only then, `claude auth status --json`, run with exactly `run_env()` and in
     the directory the run starts in, reports `loggedIn` true, `authMethod` `claude.ai`,
     `apiProvider` `firstParty` and `subscriptionType` `max`. Anything else refuses: a
     Console sign-in bills the API with no key in sight.
  A route found runs nothing at all, not even `claude auth status`. The watcher never
  passes `--bare`, which needs an API key.

`ROUTE_ENV` is the brief's five names. `OPENAI_API_KEY` is among them because, in a
settings `env` block, `claude` would hand it to the skills' Codex reviews, which must stay on
the Codex sign-in; in the watcher's own environment it would be dropped by `run_env()`
anyway, but it refuses there too, so `status` names it. Other providers' switches and a
custom base URL aren't scanned for: `apiProvider` `firstParty` is the backstop for them.

The allowlist keeps what the keychain sign-ins of `claude`, `gh` and `codex`, `git push`
over ssh and `uv` need: the home, the user, the path, the locale, the temporary directory,
`SSH_AUTH_SOCK`, and the config-dir overrides of `gh` and `codex`. `CLAUDE_CONFIG_DIR` is
not kept, so the user settings scanned are `$HOME/.claude/settings.json`, the ones the run
reads. `CLAUDE_CODE_DISABLE_ADVISOR_TOOL` and `DISABLE_TELEMETRY` are never passed: either
one turns the advisor off, and every SDLC skill calls it (#345).
`CLAUDE_CODE_OAUTH_TOKEN` (a `claude setup-token` token, the PRD's other Max sign-in) is
kept from the watcher's own environment; M2's plist must not be written from `run_env()`,
since the plist carries no Anthropic credential.

`status` (tools/sdlc_watch.py) shows `Billing.line`: the sign-in, or each route found.

Stdlib only, no `twiddle` import, and no import of `sdlc_watch` (it imports this).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

WATCH_RUN_ENV = "TWIDDLE_SDLC_WATCH_RUN"   # = tools/sdlc.py's and tools/sdlc_watch.py's (a test holds them together)
AUTH_TIMEOUT_S = 30                        # `claude auth status` reads a local sign-in: seconds, not minutes
AUTH_ARGV = ("auth", "status", "--json")   # after the resolved `claude`; never `--bare`

# Names a run may get from the watcher's environment. Nothing else is passed.
ALLOW_ENV = (
    "HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING",   # the locale; macOS's text encoding
    "SSH_AUTH_SOCK",                                          # `git push` over ssh
    "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "CODEX_HOME",         # where `gh` and `codex` keep their config
    "CLAUDE_CODE_OAUTH_TOKEN",                                # a `claude setup-token` Max sign-in
)
# The API-billing routes: present in the watcher's environment or a settings `env`, every run refuses.
ROUTE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
             "OPENAI_API_KEY")
# Never in a run's environment: the routes, the user-settings move, and what turns the advisor off.
NEVER_ENV = (*ROUTE_ENV, "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_DISABLE_ADVISOR_TOOL", "DISABLE_TELEMETRY")

# The managed settings file `claude` reads on this platform, before user, project and local.
MANAGED_SETTINGS = (Path("/Library/Application Support/ClaudeCode/managed-settings.json")
                    if sys.platform == "darwin" else Path("/etc/claude-code/managed-settings.json"))
# What `claude auth status --json` must say, field by field (its observed shape:
# {"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max", ...}).
WANT_AUTH = (("loggedIn", True), ("authMethod", "claude.ai"), ("apiProvider", "firstParty"),
             ("subscriptionType", "max"))
OK_LINE = "claude.ai sign-in, Max; no API key, helper or Bedrock/Vertex switch found"


@dataclass(frozen=True)
class Billing:
    """One billing check: ok, or why every run is refused. `ran_claude` says whether `claude` was started."""
    ok: bool
    routes: tuple[str, ...] = ()       # each API-billing route found, in words, with where it is
    reason: str = ""                   # why the sign-in was refused (when no route was found)
    ran_claude: bool = False
    auth: dict[str, Any] | None = field(default=None, compare=False)

    @property
    def line(self) -> str:
        """`status`'s billing line."""
        if self.ok:
            return OK_LINE
        return "REFUSING every run: " + ("; ".join(self.routes) if self.routes else self.reason)


class BillingRefused(Exception):
    """Raised by `require_billing`: a run must not start. Its text is `Billing.line`."""

    def __init__(self, billing: Billing):
        super().__init__(billing.line)
        self.billing = billing


# --- the run's environment --------------------------------------------------------------

def run_env(parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a run gets: `ALLOW_ENV` from `parent` (the watcher's own, by default), and
    `WATCH_RUN_ENV`, so `sdlc.py` and `sdlc_watch.py` know no person is at the keyboard."""
    parent = os.environ if parent is None else parent
    env = {k: parent[k] for k in ALLOW_ENV if k in parent}
    env[WATCH_RUN_ENV] = "1"
    leaked = sorted(set(env) & set(NEVER_ENV))
    if leaked:                                         # only an edit of ALLOW_ENV gets here; a test stops it
        raise AssertionError(f"run_env would pass {', '.join(leaked)}")
    return env


# --- the API-billing routes -------------------------------------------------------------

def settings_files(home: str | None, projects: Iterable[Path], managed: Path | None = None) -> list[tuple[str, Path]]:
    """Every Claude Code settings file `claude -p` would read for a run with this `HOME`, started in
    one of `projects` (the pinned checkout, the primary, the one this runs from): (scope, path)."""
    files = [("managed", managed or MANAGED_SETTINGS)]
    if home:
        files.append(("user", Path(home) / ".claude" / "settings.json"))
    seen: set[Path] = set()
    for project in projects:
        project = Path(project)
        if project in seen:
            continue
        seen.add(project)
        files.append(("project", project / ".claude" / "settings.json"))
        files.append(("local", project / ".claude" / "settings.local.json"))
    return files


def routes_in_settings(scope: str, path: Path) -> list[str]:
    """The API-billing routes in one settings file; a missing file has none. Read only."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"{scope} settings {path} can't be read ({exc.strerror or exc})"]
    try:
        data = json.loads(text)
    except ValueError as exc:
        return [f"{scope} settings {path} is not valid JSON ({exc}): it can't be checked"]
    if not isinstance(data, dict):
        return [f"{scope} settings {path} is not a JSON object: it can't be checked"]
    found = []
    if "apiKeyHelper" in data:
        found.append(f"apiKeyHelper in {scope} settings {path}")
    env = data.get("env")
    if env is not None and not isinstance(env, dict):
        found.append(f"{scope} settings {path} has an `env` that is not an object: it can't be checked")
    elif env:
        found += [f"{name} under env in {scope} settings {path}" for name in ROUTE_ENV if name in env]
    return found


def find_routes(parent: Mapping[str, str], files: Iterable[tuple[str, Path]]) -> list[str]:
    """Every API-billing route in place, in words: in the watcher's environment, then each settings file.
    A name counts when it is set at all, even empty."""
    found = [f"{name} in the watcher's environment" for name in ROUTE_ENV if name in parent]
    for scope, path in files:
        found += routes_in_settings(scope, path)
    return found


# --- the sign-in ------------------------------------------------------------------------

def auth_problem(stdout: str, returncode: int) -> tuple[str, dict[str, Any] | None]:
    """Why `claude auth status --json`'s answer isn't a Max sign-in ('' when it is), and the parsed answer."""
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        tail = " ".join(stdout.split())[-200:]
        return (f"claude auth status --json gave no JSON object (exit {returncode}): {tail or 'no output'}", None)
    for key, want in WANT_AUTH:
        got = data.get(key)
        if got != want or type(got) is not type(want):
            return (f"claude auth status says {key} {got!r}, not {want!r}", data)
    if returncode != 0:
        return (f"claude auth status --json exited {returncode}", data)
    return ("", data)


def check_auth(env: Mapping[str, str], cwd: Path, timeout_s: float = AUTH_TIMEOUT_S) -> tuple[str, dict[str, Any] | None]:
    """Run `claude auth status --json` with exactly `env`, in `cwd`: ('' or why not, its answer)."""
    exe = shutil.which("claude", path=env.get("PATH", ""))
    if not exe:
        return ("claude is not on the run's PATH", None)
    try:
        proc = subprocess.run([exe, *AUTH_ARGV], cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return (f"claude auth status --json gave no answer in {timeout_s:g}s", None)
    except OSError as exc:
        return (f"claude auth status --json can't be run: {exc}", None)
    return auth_problem(proc.stdout, proc.returncode)


# --- the check --------------------------------------------------------------------------

def billing_check(projects: Iterable[Path], cwd: Path | None = None, parent: Mapping[str, str] | None = None,
                  managed: Path | None = None, timeout_s: float = AUTH_TIMEOUT_S) -> Billing:
    """Whether a run may start: no API-billing route, then a Max sign-in. Never raises for a refusal.

    `projects` are the checkouts whose project and local settings count (the run's own first);
    `cwd` is where the run starts (default: the first of them); `parent` is the watcher's own
    environment (default `os.environ`). `claude` is started only when no route is found."""
    projects = [Path(p) for p in projects]
    if not projects:
        raise ValueError("billing_check needs the checkout the run starts in")
    parent = os.environ if parent is None else parent
    env = run_env(parent)
    routes = find_routes(parent, settings_files(env.get("HOME"), projects, managed))
    if routes:
        return Billing(ok=False, routes=tuple(routes))
    problem, auth = check_auth(env, cwd or projects[0], timeout_s)
    return Billing(ok=not problem, reason=problem, ran_claude=True, auth=auth)


def require_billing(projects: Iterable[Path], cwd: Path | None = None, parent: Mapping[str, str] | None = None,
                    managed: Path | None = None) -> Billing:
    """`billing_check`, raising `BillingRefused` on a refusal: what a run calls before it starts anything."""
    billing = billing_check(projects, cwd, parent, managed)
    if not billing.ok:
        raise BillingRefused(billing)
    return billing
