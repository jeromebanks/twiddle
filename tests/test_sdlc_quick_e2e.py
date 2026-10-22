"""tools/sdlc.py end to end: a quick change from its approved brief to main, the refusals on the way, the ways it
leaves for the full lane, and a full-lane slice beside it. Every step is a real `sdlc.main` command, run against a
scratch repo with a bare origin and a stateful fake of `gh` (the one function through which sdlc.py reaches GitHub).
No network, no real gh or codex, no speaker, no Spotify: a guard fails the test if any of them is reached.
`uv run pytest tests/test_sdlc_quick_e2e.py -v` is the quick lane's milestone demo."""
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.fake_codex import APPROVES, NO_VERDICT, SETTINGS, install, no_real_codex_home  # noqa: F401
from tests.test_sdlc_quick import _git, repo  # noqa: F401   (a scratch repo with a bare origin, cwd in it)
from tools import sdlc, sdlc_quick

ROOT = Path(__file__).resolve().parent.parent
REPO = "o/r"                     # never the real repository, even if a guard slipped
OWNER, POSTER = "owner", "poster"   # OWNER is who gh is logged in as: the one collaborator with push
ROUTINE = "complexity:routine"
STATION = "src/twiddle/stations/catalog/psyched.toml"
STATION_TOML = 'key = "psyched"\nname = "Psyched FM"\nstream = "https://example.invalid/psyched.mp3"\n'
CHANGES = {"out": "1. **blocking** — the station has no tags.\n\nVERDICT: changes\n"}


# --- the guard: nothing real is reached ---------------------------------------------------------------------

def refuse_real(args, env, tmp_path):
    """Called before every subprocess starts: a real `gh` never, `codex` only as the fake on its PATH."""
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    prog = os.fsdecode(argv[0])
    name = os.path.basename(prog)
    if name == "gh":
        pytest.fail(f"a real `gh` was about to run: {argv}")
    if name == "codex":
        found = shutil.which(prog, path=(env if env is not None else os.environ).get("PATH"))
        if not found or Path(found).resolve().parent != (tmp_path / "bin").resolve():
            pytest.fail(f"`codex` resolves to {found}, not the fake one (tests/fake_codex.py): {argv}")


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    """No real gh or codex (subprocesses are checked as they start, and sentinels first on PATH catch anything that
    gets past that), and no socket to anywhere but loopback (a speaker, Spotify and GitHub are all off the box)."""
    sentinel, bin_ = tmp_path / "sentinel.log", tmp_path / "sentinel-bin"
    bin_.mkdir()
    for name in ("gh", "codex"):
        (bin_ / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{sentinel}"\nexit 97\n')
        (bin_ / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")

    class Guarded(subprocess.Popen):
        def __init__(self, args, *a, **kw):
            refuse_real(args, kw.get("env"), tmp_path)
            super().__init__(args, *a, **kw)
    monkeypatch.setattr(subprocess, "Popen", Guarded)

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            try:
                loopback = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                loopback = address[0] == "localhost"
            if not loopback:
                pytest.fail(f"a socket to {address} was about to open: these tests are offline")
    monkeypatch.setattr(socket.socket, "connect", lambda s, a: check(s, a) or real_connect(s, a))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda s, a: check(s, a) or real_connect_ex(s, a))
    yield
    assert not sentinel.exists(), f"a real gh/codex was reached: {sentinel.read_text()}"


@pytest.fixture(autouse=True)
def _runs_main_gate_code(monkeypatch):
    """`merge` first checks it runs origin/main's gate code (a git fetch): tests/test_sdlc_merge_gate.py tests that."""
    monkeypatch.setattr(sdlc, "gate_code_errors", lambda config, pr="PR": [])


# --- GitHub, as `gh` answers it ------------------------------------------------------------------------------

class GhError(Exception):
    """What a failing `gh` prints: sdlc.gh raises it as SdlcError unless called with check=False."""


def flag(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def flags(args, name):
    return [args[i + 1] for i, a in enumerate(args) if a == name]


# GitHub's closing keywords, as GitHub reads a PR body (written here, not taken from sdlc.py)
CLOSING = re.compile(r"\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved):?\s+#(\d+)\b", re.I)


class FakeGitHub:
    """GitHub behind `sdlc.gh`: issues, comments, labels, assignees, PRs, their files, compares, squash merges and
    branch deletes, over the scratch repo's bare origin. Only `sdlc.gh` is replaced, so every reader and writer
    above it (`gh_json`, `gh_pages`, `post_comment`, `fetch_*`, `quick_gate_bundle`...) runs for real. It answers
    in gh's own shapes: `issue view`/`pr view` are GraphQL (`OPEN`, `CLOSED`, `MERGED`), `api` is REST (`open`,
    `closed`, `state_reason`), `--paginate --slurp` is a list of pages. A call it doesn't know fails the test.
    A squash into main closes the issues its PR body names, as GitHub does; a squash into another branch doesn't."""

    def __init__(self, monkeypatch, root, labels):
        self.root, self.origin = root, root.parent / "origin.git"
        self.labels = set(labels)
        self.issues, self.prs, self.comments, self.blocked_by, self.sub_issues = {}, {}, {}, {}, {}
        self.calls = []
        self._number, self._id, self._clock = 10, 900, 0
        monkeypatch.setattr(sdlc, "gh", self.gh)

    # -- the world, as a person changes it (not through the tool) --
    def now(self):
        self._clock += 1
        return f"2026-10-19T{self._clock // 3600:02d}:{self._clock // 60 % 60:02d}:{self._clock % 60:02d}Z"

    def new_issue(self, title, body="", labels=(), author=POSTER, milestone=None):
        self._number += 1
        n = self._number
        self.issues[n] = {"number": n, "id": 70000 + n, "title": title, "body": body, "state": "open",
                          "state_reason": None, "labels": list(labels), "author": author, "assignees": [],
                          "milestone": milestone, "created_at": self.now()}
        self.comments[n] = []
        return n

    def say(self, n, author, body):
        """A comment on issue or PR #n, as `author`."""
        self._id += 1
        kind = "pull" if n in self.prs else "issues"
        c = {"id": self._id, "user": {"login": author}, "body": body, "created_at": self.now(),
             "html_url": f"https://github.com/{REPO}/{kind}/{n}#issuecomment-{self._id}"}
        self.comments[n].append(c)
        return c

    def origin_git(self, *args, check=True):
        proc = subprocess.run(["git", *args], cwd=self.origin, capture_output=True, text=True)
        if check and proc.returncode:
            raise GhError(proc.stderr.strip())
        return proc.stdout.strip() if proc.returncode == 0 else None

    def sha(self, ref):
        return self.origin_git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)

    # -- gh --
    def gh(self, args, *, check=True):
        self.calls.append(list(args))
        try:
            return self.answer(list(args))
        except GhError as exc:
            if check:
                raise sdlc.SdlcError(f"gh {' '.join(args[:3])} failed: {exc}")
            return ""

    def answer(self, args):
        if args[0] == "api":
            return self.api(args[1:])
        if flag(args, "--repo") != REPO:
            pytest.fail(f"gh {args}: not --repo {REPO}")
        verb = args[:2]
        if verb == ["label", "list"]:
            return json.dumps([{"name": l} for l in sorted(self.labels)])
        if verb == ["issue", "view"]:
            want = flag(args, "--json").split(",")
            return json.dumps({k: self.issue_graphql(self.issue(args[2]))[k] for k in want})
        if verb == ["issue", "edit"]:
            return self.issue_edit(self.issue(args[2]), args)
        if verb == ["issue", "create"]:
            for l in flags(args, "--label"):
                if l not in self.labels:
                    raise GhError(f"could not add label: '{l}' not found")
            n = self.new_issue(flag(args, "--title"), Path(flag(args, "--body-file")).read_text(),
                               flags(args, "--label"), author=OWNER)
            return f"https://github.com/{REPO}/issues/{n}\n"
        if verb == ["issue", "close"]:
            i = self.issue(args[2])
            if comment := flag(args, "--comment"):
                self.say(i["number"], OWNER, comment)
            if i["state"] == "open":
                i["state"], i["state_reason"] = "closed", {"not planned": "not_planned"}.get(flag(args, "--reason"),
                                                                                            "completed")
            return ""
        if verb == ["pr", "view"]:
            want = flag(args, "--json").split(",")
            return json.dumps({k: self.pr_graphql(self.pr(args[2]))[k] for k in want})
        if verb == ["pr", "list"]:
            want, state = flag(args, "--json").split(","), flag(args, "--state", "open")
            hits = [p for p in self.prs.values() if p["head"] == flag(args, "--head", p["head"])
                    and p["base"] == flag(args, "--base", p["base"]) and state in ("all", p["state"].lower())]
            return json.dumps([{k: self.pr_graphql(p)[k] for k in want} for p in sorted(hits, key=lambda p: -p["number"])])
        if verb == ["pr", "create"]:
            return self.pr_create(args)
        if verb == ["pr", "merge"]:
            return self.pr_merge(self.pr(args[2]), args)
        pytest.fail(f"gh {args}: the fake doesn't know this call")

    def issue(self, n):
        if int(n) not in self.issues:
            raise GhError(f"Could not resolve to an issue with the number of {n}")
        return self.issues[int(n)]

    def pr(self, n):
        if int(n) not in self.prs:
            raise GhError(f"Could not resolve to a PullRequest with the number of {n}")
        return self.prs[int(n)]

    def issue_graphql(self, i):
        return {"number": i["number"], "title": i["title"], "body": i["body"], "state": i["state"].upper(),
                "author": {"login": i["author"]}, "labels": [{"name": l} for l in i["labels"]],
                "createdAt": i["created_at"], "assignees": [{"login": a} for a in i["assignees"]],
                "milestone": {"title": i["milestone"]} if i["milestone"] else None}

    def issue_rest(self, i):
        return {"number": i["number"], "id": i["id"], "title": i["title"], "body": i["body"], "state": i["state"],
                "state_reason": i["state_reason"], "user": {"login": i["author"]},
                "labels": [{"name": l} for l in i["labels"]], "assignees": [{"login": a} for a in i["assignees"]],
                "milestone": {"title": i["milestone"]} if i["milestone"] else None, "created_at": i["created_at"]}

    def issue_edit(self, i, args):
        for l in flags(args, "--add-label"):
            if l not in self.labels:
                raise GhError(f"'{l}' not found")
        drop = set(flags(args, "--remove-label"))
        i["labels"] = [l for l in i["labels"] if l not in drop]
        i["labels"] += [l for l in flags(args, "--add-label") if l not in i["labels"]]
        gone = {OWNER if a == "@me" else a for a in flags(args, "--remove-assignee")}
        i["assignees"] = [a for a in i["assignees"] if a not in gone]
        i["assignees"] += [a for a in (OWNER if a == "@me" else a for a in flags(args, "--add-assignee"))
                           if a not in i["assignees"]]
        if "--body-file" in args:
            i["body"] = Path(flag(args, "--body-file")).read_text()
        return f"https://github.com/{REPO}/issues/{i['number']}\n"

    # -- pull requests, over the bare origin --
    def head_oid(self, p):
        if p["state"] == "OPEN":
            p["oid"] = self.sha(f"refs/heads/{p['head']}") or p["oid"]
        return p["oid"]

    def mergeable(self, p):
        proc = subprocess.run(["git", "merge-tree", "--write-tree", f"refs/heads/{p['base']}", self.head_oid(p)],
                              cwd=self.origin, capture_output=True, text=True)
        return "MERGEABLE" if proc.returncode == 0 else "CONFLICTING"

    def diff(self, p):
        """GitHub's `pulls/N/files`: the change from the merge base to the head, renames found."""
        if p.get("files") is not None:   # frozen at the merge, as GitHub keeps them
            return p["files"]
        spec = f"refs/heads/{p['base']}...{self.head_oid(p)}"
        status = self.origin_git("diff", "--name-status", "-z", "-M", spec).split("\0")
        nums = self.origin_git("diff", "--numstat", "-z", "-M", spec).split("\0")
        out, i, j = [], 0, 0
        while i < len(status) and status[i]:
            code = status[i][0]
            moved = code in "RC"
            old, new = (status[i + 1], status[i + 2]) if moved else (None, status[i + 1])
            i += 3 if moved else 2
            adds, dels, _ = nums[j].split("\t", 2)
            j += 3 if moved else 1
            out.append({"filename": new, "status": {"A": "added", "M": "modified", "D": "removed", "R": "renamed",
                                                    "C": "copied", "T": "changed"}[code],
                        "additions": int(adds), "deletions": int(dels), **({"previous_filename": old} if moved else {})})
        return out

    def pr_graphql(self, p):
        merged = p["state"] == "MERGED"
        return {"number": p["number"], "title": p["title"], "body": p["body"], "state": p["state"], "isDraft": False,
                "baseRefName": p["base"], "headRefName": p["head"], "headRefOid": self.head_oid(p),
                "mergeable": self.mergeable(p) if p["state"] == "OPEN" else "UNKNOWN",
                "url": f"https://github.com/{REPO}/pull/{p['number']}",
                "mergeCommit": {"oid": p["merge_commit"]} if merged else None, "mergedAt": p.get("merged_at"),
                "files": [{"path": f["filename"], "additions": f["additions"], "deletions": f["deletions"]}
                          for f in self.diff(p)]}

    def pr_create(self, args):
        base, head = flag(args, "--base"), flag(args, "--head")
        body = Path(flag(args, "--body-file")).read_text() if "--body-file" in args else flag(args, "--body", "")
        if not self.sha(f"refs/heads/{head}") or not self.sha(f"refs/heads/{base}"):
            raise GhError(f"Head ref {head} or base ref {base} must exist on the remote")
        self._number += 1
        n = self._number
        self.prs[n] = {"number": n, "title": flag(args, "--title"), "body": body, "base": base, "head": head,
                       "state": "OPEN", "oid": None, "merge_commit": None, "files": None}
        self.comments[n] = []
        return f"https://github.com/{REPO}/pull/{n}\n"

    def pr_merge(self, p, args):
        if "--squash" not in args:
            pytest.fail(f"gh {args}: only squash merges are expected")
        if p["state"] != "OPEN":
            raise GhError(f"Pull request #{p['number']} was already merged or closed")
        head = self.head_oid(p)
        if flag(args, "--match-head-commit") != head:
            raise GhError("Head branch was modified. Review and try the merge again.")
        if self.mergeable(p) != "MERGEABLE":
            raise GhError("Pull request is not mergeable: the merge commit cannot be cleanly created.")
        base_ref = f"refs/heads/{p['base']}"
        tree = self.origin_git("merge-tree", "--write-tree", base_ref, head).splitlines()[0]
        old = self.sha(base_ref)
        commit = self.commit_tree(tree, old, f"{p['title']} (#{p['number']})")
        self.origin_git("update-ref", base_ref, commit, old)
        p["files"] = self.diff(p)
        p.update(state="MERGED", merge_commit=commit, merged_at=self.now())
        if "--delete-branch" in args:
            self.origin_git("update-ref", "-d", f"refs/heads/{p['head']}")
        if p["base"] == "main":   # the default branch: GitHub closes what the PR says it closes
            for n in sorted({int(x) for x in CLOSING.findall(p["body"])} & set(self.issues)):
                if self.issues[n]["state"] == "open":
                    self.issues[n].update(state="closed", state_reason="completed")
        return ""

    def commit_tree(self, tree, parent, message):
        env = {**os.environ, "GIT_AUTHOR_NAME": "GitHub", "GIT_AUTHOR_EMAIL": "noreply@example.invalid",
               "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@example.invalid"}
        return subprocess.run(["git", "commit-tree", tree, "-p", parent, "-m", message], cwd=self.origin, env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    # -- the REST API --
    def api(self, rest):
        raw = rest[:2] == ["-H", "Accept: application/vnd.github.raw+json"]   # a file's contents, as they are
        rest = rest[2:] if raw else rest
        method, path = (rest[1], rest[2]) if rest[0] == "-X" else ("GET", rest[0])
        if method == "GET" and ("--paginate" in rest) != ("--slurp" in rest):
            pytest.fail(f"gh api {rest}: --paginate without --slurp")
        url = urlsplit(path)
        parts, query = url.path.split("/"), parse_qs(url.query)
        if parts[:3] != ["repos", *REPO.split("/")]:
            pytest.fail(f"gh api {rest}: not repos/{REPO}")
        tail = parts[3:]
        if method == "POST" and tail[0] == "issues" and tail[2:] == ["comments"]:
            n = int(tail[1])
            if n not in self.comments:
                raise GhError("HTTP 404: Not Found")
            body = flag(rest, "-F")
            if not body.startswith("body=@"):
                pytest.fail(f"gh api {rest}: a comment body is posted from a file")
            return json.dumps(self.say(n, OWNER, Path(body[len("body=@"):]).read_text()))
        if method == "POST" and tail == ["issues"]:   # the debt epic, opened by `file-issue --debt`
            payload = json.loads(Path(flag(rest, "--input")).read_text())
            if missing := [l for l in payload["labels"] if l not in self.labels]:
                raise GhError(f"could not add label: {missing}")
            n = self.new_issue(payload["title"], payload["body"], payload["labels"], author=OWNER)
            return json.dumps(self.issue_rest(self.issues[n]))
        if method == "POST" and tail[0] == "issues" and tail[2:] == ["sub_issues"]:
            payload = json.loads(Path(flag(rest, "--input")).read_text())
            child = next(i for i in self.issues.values() if i["id"] == payload["sub_issue_id"])
            if any(child["number"] in subs for subs in self.sub_issues.values()):
                raise GhError("HTTP 422: Sub issue may only have one parent")
            self.sub_issues.setdefault(int(tail[1]), []).append(child["number"])
            return json.dumps(self.issue_rest(self.issue(tail[1])))
        if method == "PATCH" and tail[:2] == ["issues", "comments"]:
            c = next((c for cs in self.comments.values() for c in cs if c["id"] == int(tail[2])), None)
            if c is None:
                raise GhError("HTTP 404: Not Found")
            c["body"] = Path(flag(rest, "-F")[len("body=@"):]).read_text()
            return json.dumps(c)
        if method == "DELETE" and tail[:3] == ["git", "refs", "heads"]:
            ref = "refs/heads/" + "/".join(tail[3:])
            if not self.sha(ref):
                raise GhError("HTTP 422: Reference does not exist")
            self.origin_git("update-ref", "-d", ref)
            return ""
        if method != "GET":
            pytest.fail(f"gh api {rest}: the fake doesn't know this call")
        if tail[0] == "compare":
            spec = "/".join(tail[1:])        # `epic/?...` (no epic) reaches GitHub as a path cut at the `?`
            base, head = spec.split("...") if "..." in spec else (spec, "")
            a, b = self.sha(base), self.sha(head) if head else None
            if not (a and b):
                raise GhError("HTTP 404: Not Found")
            ahead = int(self.origin_git("rev-list", "--count", f"{a}..{b}"))
            behind = int(self.origin_git("rev-list", "--count", f"{b}..{a}"))
            return self.page(rest, {"ahead_by": ahead, "behind_by": behind, "total_commits": ahead,
                                    "status": "identical" if not (ahead or behind) else "diverged" if ahead and behind
                                    else "ahead" if ahead else "behind"})
        if tail == ["collaborators"]:
            return self.page(rest, [{"login": OWNER, "permissions": {"admin": True, "push": True, "pull": True}}])
        if tail == ["issues"]:
            want = set(query.get("labels", [""])[0].split(",")) - {""}
            state = query.get("state", ["open"])[0]
            hits = [self.issue_rest(i) for i in self.issues.values()
                    if want <= set(i["labels"]) and state in ("all", i["state"])]
            return self.page(rest, hits)
        if tail[0] == "issues" and len(tail) == 2:
            return self.page(rest, self.issue_rest(self.issue(tail[1])))
        if tail[0] == "issues" and tail[2:] == ["comments"]:
            n = int(tail[1])
            if n not in self.comments:
                raise GhError("HTTP 404: Not Found")
            return self.page(rest, list(self.comments[n]))
        if tail[0] == "issues" and tail[2:] == ["sub_issues"]:
            return self.page(rest, [self.issue_rest(self.issues[c]) for c in self.sub_issues.get(int(tail[1]), [])])
        if tail[0] == "issues" and tail[2:] == ["dependencies", "blocked_by"]:
            return self.page(rest, [self.issue_rest(self.issues[b]) for b in self.blocked_by.get(int(tail[1]), [])])
        if tail[0] == "pulls" and tail[2:] == ["files"]:
            return self.page(rest, self.diff(self.pr(tail[1])))
        if raw and tail[0] == "contents":
            text = self.origin_git("show", f"{query['ref'][0]}:{'/'.join(tail[1:])}", check=False)
            if text is None:
                raise GhError("HTTP 404: Not Found")
            return text
        pytest.fail(f"gh api {rest}: the fake doesn't know this call")

    @staticmethod
    def page(rest, data):
        return json.dumps([data] if "--paginate" in rest else data)

    # -- what a test reads back --
    def records(self, n, kind):
        return [c for c in self.comments[n] if c["user"]["login"] == OWNER
                and (sdlc.parse_marker(c["body"]) or {}).get("kind") == kind]

    def snapshot(self, *numbers):
        """Everything about these issues and PRs a refused command must leave as it was."""
        return json.dumps([{"issue": self.issues.get(n), "pr": {k: v for k, v in self.prs.get(n, {}).items() if k != "oid"},
                            "comments": self.comments.get(n)} for n in numbers], sort_keys=True, default=str)


# --- the lane: the commands, as the skills run them ------------------------------------------------------------

class Lane:
    """A scratch repo, its bare origin and a FakeGitHub; a temp config naming the fake repo and fake_codex's
    settings; the fake `codex` on PATH; the test suite faked for `test-record` and no waiting on GitHub."""

    def __init__(self, tmp_path, monkeypatch, capsys, root):
        self.tmp, self.mp, self.capsys, self.root = tmp_path, monkeypatch, capsys, root
        cfg = {**sdlc.load_config(), "repository": REPO, "codex": SETTINGS}
        self.config = tmp_path / "sdlc-config.json"
        self.config.write_text(json.dumps(cfg))
        self.cfg = cfg
        (root / ".sdlc").mkdir(exist_ok=True)   # main's own rules: the quick gate reads them from origin, not here
        (root / ".sdlc" / "config.json").write_text(json.dumps(cfg))
        _git("add", ".sdlc/config.json", cwd=root)
        _git("commit", "-q", "-m", "main: the sdlc config", cwd=root)
        _git("push", "-q", "origin", "main", cwd=root)
        self.gh = FakeGitHub(monkeypatch, root, {l["name"] for k in ("labels", "extra_labels", "plan_labels")
                                                 for l in cfg[k]})
        self.codex_log, self.codex_plan, _ = install(tmp_path, monkeypatch)
        self.scratch = tmp_path / "scratch"
        self.scratch.mkdir()
        self.suite_runs = []
        monkeypatch.setattr(sdlc, "run_suite", self.suite)
        for name in ("HEAD_WAIT_SECONDS", "PRD_FILE_WAIT_SECONDS"):   # the fake shows a push at once
            monkeypatch.setattr(sdlc, name, 0)

    def suite(self, cwd=None):
        self.suite_runs.append(_git("rev-parse", "HEAD", cwd=cwd or Path.cwd()))
        return {"result": "pass", "passed": 1300, "failed": 0, "tail": "1300 passed in 80.00s"}

    def run(self, *argv, cwd=None):
        self.mp.chdir(cwd or self.root)
        self.capsys.readouterr()
        code = sdlc.main(["--config", str(self.config), *map(str, argv)])
        out = self.capsys.readouterr()
        self.mp.chdir(self.root)
        return code, out.out, out.err

    def ok(self, *argv, cwd=None):
        code, out, err = self.run(*argv, cwd=cwd)
        assert code == 0, f"{argv}: {out}{err}"
        return out

    def state(self, n):
        return json.loads(self.ok("state", n, "--json"))

    # -- triage --
    def brief(self, files, outcome="Psyched FM is in the station catalog."):
        text = {"Outcome": outcome, "Scope": "One catalog file for the station.",
                "Files": "\n".join(f"- `{f}`" for f in files), "Acceptance criteria": "1. `stations` lists it.",
                "Validation": "`uv run pytest tests/test_catalog.py`", "Non-goals": "No new fetcher.",
                "Context": "skill `add-radio-station`"}
        return "\n\n".join(f"## {s}\n{text[s]}" for s in sdlc_quick.BRIEF_SECTIONS)

    def quick_issue(self, title="Add Psyched FM", files=("src/twiddle/stations/catalog/*.toml",)):
        n = self.gh.new_issue(title, "Please add Psyched FM.", [ROUTINE])
        self.post_brief(n, self.brief(files))
        self.approve(n)
        return n

    def post_brief(self, n, text):
        f = self.scratch / f"brief-{n}.md"
        f.write_text(text)
        self.ok("transition", n, "prd-review", "--kind", "prd", "--lane", "quick", "--body-file", f)

    def approve(self, n):
        self.gh.say(n, POSTER, "/approve")
        self.ok("transition", n, "approved", "--kind", "approval")

    def brief_id(self, n):
        return sdlc.approved_brief_id(self.state(n))

    # -- building --
    def claim(self, n):
        out = self.ok("claim", n, "--quick", "--model", "opus")
        return Path(re.search(r"^WORKTREE=(.+)$", out, re.M).group(1))

    def commit(self, wt, n, files, msg, fresh=False):
        """Commit `files` (path -> text) on slice/n and push it; `fresh` starts again from origin/main."""
        if fresh:
            _git("fetch", "-q", "origin", "main", cwd=wt)
            _git("reset", "-q", "--hard", "origin/main", cwd=wt)
        for path, text in files.items():
            (wt / path).parent.mkdir(parents=True, exist_ok=True)
            (wt / path).write_text(text)
        _git("add", "-A", cwd=wt)
        _git("commit", "-q", "-m", msg, cwd=wt)
        _git("push", "-q", "-f", "origin", f"HEAD:refs/heads/slice/{n}", cwd=wt)
        return _git("rev-parse", "HEAD", cwd=wt)

    def open_pr(self, n, base="main", title="Add Psyched FM"):
        """`gh pr create`, as the work-slice skill runs it."""
        out = self.gh.gh(["pr", "create", "--repo", REPO, "--base", base, "--head", f"slice/{n}", "--title", title,
                          "--body", f"{title}.\n\nCloses #{n}"])
        return int(out.rsplit("/", 1)[1])

    def codex(self, pr, wt, *steps):
        """One `codex-review --pr` round, the fake Codex answering with `steps` (the first, and its retry)."""
        done = len(self.codex_log.read_text().splitlines()) if self.codex_log.exists() else 0
        self.codex_plan.write_text(json.dumps([APPROVES] * done + list(steps or (APPROVES,))))
        out = self.scratch / f"codex-{pr}-{done}"
        code, stdout, err = self.run("codex-review", "--pr", pr, "--out", out, cwd=wt)
        return code, out / "codex.md", stdout + err

    def review(self, pr, wt, *steps, response=None):
        """A Codex round, then `pr-review` recording it (with the answer to its findings)."""
        code, report, out = self.codex(pr, wt, *steps)
        assert code == 0, out
        args = ["pr-review", pr, "--report", report]
        if response:
            (self.scratch / "response.md").write_text(response)
            args += ["--response", self.scratch / "response.md"]
        return report, self.run(*args, cwd=wt)

    # -- origin --
    def main_sha(self):
        return self.gh.sha("refs/heads/main")

    def on_main(self, path):
        return self.gh.origin_git("cat-file", "-e", f"main:{path}", check=False) is not None

    def worktrees(self):
        return [l for l in _git("worktree", "list", "--porcelain", cwd=self.root).splitlines() if l.startswith("worktree ")]


@pytest.fixture
def lane(repo, tmp_path, monkeypatch, capsys):   # noqa: F811
    return Lane(tmp_path, monkeypatch, capsys, repo)


def sdlc_labels(labels):
    return [l for l in labels if l.startswith("sdlc:")]


# --- the happy path ------------------------------------------------------------------------------------------

def test_a_quick_change_goes_from_its_approved_brief_to_main(lane):
    gh = lane.gh
    # triage: a quick brief, then the poster's /approve
    n = gh.new_issue("Add Psyched FM", "Please add Psyched FM, it's great.", [ROUTINE])
    lane.post_brief(n, lane.brief(["src/twiddle/stations/catalog/*.toml"]))
    st = lane.state(n)
    assert (st["state"], st["lane"], st["action"]) == ("prd-review", "quick", "wait_for_poster")
    assert set(gh.issues[n]["labels"]) == {ROUTINE, "sdlc:prd-review", "lane:quick"}
    lane.approve(n)
    st = lane.state(n)
    assert (st["state"], st["lane"], st["action"], st["next"]) == ("approved", "quick", "work_quick",
                                                                    f"/work-slice {n} --quick")
    assert "state: ready   lane: quick" in lane.ok("slice-status", n)
    assert f"#{n}: ok to start" in lane.ok("slice-check", n)

    # claimed: its own worktree on slice/N from origin/main, the issue in progress
    main_before = lane.main_sha()
    wt = lane.claim(n)
    assert _git("rev-parse", "HEAD", cwd=wt) == main_before
    assert sdlc_labels(gh.issues[n]["labels"]) == ["sdlc:in-progress"] and gh.issues[n]["assignees"] == [OWNER]
    (claim,) = gh.records(n, "claim")
    assert 'base=main quick=1 model=opus' in claim["body"].splitlines()[0]

    # built: one catalog file, pushed, and a PR into main
    head = lane.commit(wt, n, {STATION: STATION_TOML}, "Add Psyched FM")
    pr = lane.open_pr(n)
    assert "state: in-review   lane: quick" in lane.ok("slice-status", n)

    # gated: the suite on the head, then a Codex round against the approved brief
    out = lane.ok("test-record", pr, cwd=wt)
    assert f"tests pass on {head[:12]}" in out and lane.suite_runs == [head]
    report, (code, out, err) = lane.review(pr, wt, APPROVES)
    assert code == 0, err
    assert report.read_text().splitlines()[:2] == [f"HEAD: {head}", f"Brief: {lane.brief_id(n)}"]
    (rec,) = gh.records(pr, "pr-review")
    assert f"sha={head} verdict=approve round=1" in rec["body"] and f"brief={lane.brief_id(n)}" in rec["body"]
    assert lane.ok("quick-gate", pr) == "ok\n"
    assert f"would squash-merge quick PR #{pr} (closes #{n}) into main at {head[:12]}" in lane.ok("merge", pr, "--dry-run")
    assert lane.main_sha() == main_before

    # merged: one squash commit on main holding exactly the change
    out = lane.ok("merge", pr)
    assert f"merged PR #{pr} into main; #{n} closed and sdlc:done" in out
    assert gh.prs[pr]["state"] == "MERGED"
    squash = lane.main_sha()
    assert gh.origin_git("rev-parse", f"{squash}^") == main_before and gh.origin_git("rev-list", "--count",
                                                                                   f"{main_before}..main") == "1"
    assert gh.origin_git("show", f"main:{STATION}") == STATION_TOML.rstrip("\n")
    assert gh.origin_git("diff", "--name-only", main_before, squash) == STATION
    assert not gh.sha(f"refs/heads/slice/{n}")                        # the branch is deleted

    # closed: completed (by GitHub's `Closes #N` on main, so the tool never closes it twice), sdlc:done, one record
    assert (gh.issues[n]["state"], gh.issues[n]["state_reason"]) == ("closed", "completed")
    assert not any(c[:2] == ["issue", "close"] for c in gh.calls)
    assert sdlc_labels(gh.issues[n]["labels"]) == ["sdlc:done"]
    (merged,) = gh.records(n, "quick-merged")
    assert f"pr={pr} sha={head}" in merged["body"].splitlines()[0]
    tests, = gh.records(pr, "tests")
    assert tests["html_url"] in merged["body"] and rec["html_url"] in merged["body"]
    st = lane.state(n)
    assert (st["state"], st["lane"], st["action"]) == ("done", "quick", "none") and st["next"].startswith("nothing")

    # cleaned up: no worktree, no local branch
    assert f"#{n}: removed" in lane.ok("cleanup", n)
    assert lane.worktrees() == [f"worktree {lane.root}"]
    assert not _git("branch", "--list", f"slice/{n}", cwd=lane.root)


# --- the refusals ---------------------------------------------------------------------------------------------

def test_a_quick_pr_that_breaks_a_rule_is_refused_and_never_reaches_main(lane):
    gh = lane.gh
    n = gh.new_issue("Add Psyched FM", "Please add Psyched FM.", [ROUTINE])
    lane.post_brief(n, lane.brief(["src/twiddle/stations/catalog/*.toml"]))
    gh.say(n, POSTER, "Please add a test for it too.")
    lane.post_brief(n, lane.brief(["src/twiddle/stations/catalog/*.toml", "tests/test_psyched.py"]))
    lane.approve(n)
    rev2 = lane.brief_id(n)
    wt = lane.claim(n)
    base = lane.main_sha()
    labels = list(gh.issues[n]["labels"])

    # a denied path, a sixth file, a file outside the brief: each head is refused, and nothing escalates
    bad = {
        "may not (src/twiddle/play.py)": {STATION: STATION_TOML, "src/twiddle/play.py": "# writes to a speaker\n"},
        "changes 6 files, more than the quick lane's 5": {f"src/twiddle/stations/catalog/s{i}.toml": f"key = 's{i}'\n"
                                                          for i in range(6)},
        "outside the brief's `## Files` list (src/twiddle/stations/model.py)": {
            STATION: STATION_TOML, "src/twiddle/stations/model.py": "# a model change\n"},
    }
    pr = None
    for needle, files in bad.items():
        lane.commit(wt, n, files, "Add Psyched FM", fresh=True)
        pr = pr or lane.open_pr(n)
        code, out, _ = lane.run("quick-gate", pr)
        assert code == 1 and any(needle in l for l in out.splitlines() if l.startswith("refused: ")), out
        code, _, err = lane.run("merge", pr, "--dry-run")
        assert code == 1 and needle in err, err
        assert lane.main_sha() == base and gh.issues[n]["labels"] == labels and not gh.records(n, "lane")

    # a good head: the suite passes and Codex approves it against the approved brief (rev 2)
    head = lane.commit(wt, n, {STATION: STATION_TOML, "tests/test_psyched.py": "def test_it():\n    pass\n"},
                       "Add Psyched FM", fresh=True)
    lane.ok("test-record", pr, cwd=wt)

    # Codex can't be deferred for a quick change: refused, and the PR, lane, labels and ledger are as they were
    before = gh.snapshot(n, pr)
    code, _, err = lane.run("review-defer", pr, "--reason", "codex: You've hit your usage limit.", cwd=wt)
    assert code == 1 and "never deferred" in err and "The PR stays open" in err and f"escalate-to-full {n}" in err
    assert gh.snapshot(n, pr) == before and gh.prs[pr]["state"] == "OPEN"
    assert lane.state(n)["lane"] == "quick" and sdlc_labels(gh.issues[n]["labels"]) == ["sdlc:in-progress"]

    report, (code, _, err) = lane.review(pr, wt, APPROVES)
    assert code == 0, err
    assert lane.ok("quick-gate", pr) == "ok\n"

    # a person revises the brief by hand while it is built (`transition` refuses to: the brief is frozen), and
    # approves rev 3: the review recorded against rev 2 no longer counts, and its report can't be recorded again
    code, _, err = lane.run("transition", n, "prd-review", "--kind", "prd", "--lane", "quick", "--body-file",
                            lane.scratch / f"brief-{n}.md")
    assert code == 1 and "its brief is frozen" in err
    rev3_text = lane.brief(["src/twiddle/stations/catalog/*.toml", "tests/test_psyched.py"], "Psyched FM, tagged.")
    gh.say(n, OWNER, sdlc.render_comment("prd", 3, rev3_text, lane.cfg, lane="quick"))
    gh.say(n, POSTER, "/approve")
    gh.say(n, OWNER, sdlc.render_comment("approval", 3, "PRD rev 3 approved by @poster.", lane.cfg, by=POSTER,
                                         via="comment"))
    rev3 = lane.brief_id(n)
    assert rev3 != rev2
    code, out, _ = lane.run("quick-gate", pr)
    assert code == 1 and f"recorded against brief {rev2}, not the approved brief {rev3}" in out, out
    code, _, err = lane.run("pr-review", pr, "--report", report, cwd=wt)
    assert code == 1 and f"Codex reviewed against brief {rev2}, but #{n}'s approved brief is now {rev3}" in err
    code, _, err = lane.run("merge", pr)
    assert code == 1 and f"recorded against brief {rev2}" in err

    # through all of it: main never moved, nothing escalated, and the PR waits, open, at the good head
    assert lane.main_sha() == base and not lane.on_main(STATION)
    assert not gh.records(n, "lane") and not any(c[:2] == ["pr", "merge"] for c in gh.calls)
    assert gh.prs[pr]["state"] == "OPEN" and gh.head_oid(gh.prs[pr]) == head


def finished_escalation(lane, n, pr, trigger, base):
    """What every way out of the quick lane ends with: the lane record, the brief merged into docs/prd/ by
    commit-prd (a docs-only squash on main), `state` offering /plan-issue, and the quick PR left open, unmerged."""
    gh = lane.gh
    (record,) = gh.records(n, "lane")
    assert record["body"].splitlines()[0] == f"<!-- sdlc:v1 kind=lane lane=full trigger={trigger} -->"
    assert sorted(gh.issues[n]["labels"]) == [ROUTINE, "sdlc:approved"]              # no lane:quick
    (prd,) = [p for p in gh.prs.values() if p["head"] == f"prd/{n}"]
    assert prd["state"] == "MERGED" and gh.origin_git("rev-parse", "main^") == base
    (path,) = [f["filename"] for f in prd["files"]]
    assert re.fullmatch(rf"docs/prd/{n}-add-psyched-fm\.md", path) and lane.on_main(path)
    assert sdlc.header_links(gh.origin_git("show", f"main:{path}"), lane.brief_id(n))
    assert len(gh.records(n, "prd-merged")) == 1
    st = lane.state(n)
    assert (st["state"], st["lane"], st["action"], st["next"]) == ("approved", None, "plan", f"/plan-issue {n}")
    assert f"next: /plan-issue {n}" in lane.ok("state", n)
    # the implementation never reached main, and the quick PR can no longer merge as one
    assert not lane.on_main(STATION) and gh.prs[pr]["state"] == "OPEN"
    calls = len(gh.calls)
    code, _, err = lane.run("merge", pr)
    assert code == 1 and "not an open plan:slice issue" in err and gh.prs[pr]["state"] == "OPEN"
    # #213: one reason, no epic branch: no compare against `epic/?`, no base or behind-by noise beside it
    assert not any("compare/" in " ".join(c) for c in gh.calls[calls:])
    assert err.count(";") == 0 and "epic/?" not in err and "PR targets" not in err and "could not tell" not in err


def test_a_third_change_round_moves_the_change_to_the_full_lane_and_planning_starts(lane):
    n = lane.quick_issue()
    wt = lane.claim(n)
    base = lane.main_sha()
    lane.commit(wt, n, {STATION: STATION_TOML}, "Add Psyched FM")
    pr = lane.open_pr(n)
    for rnd in (1, 2):
        _, (code, out, err) = lane.review(pr, wt, CHANGES, response="1. Accepted: adding tags in the next commit.")
        assert code == 0 and f"round {rnd}: changes" in out, err
        assert sdlc_labels(lane.gh.issues[n]["labels"]) == ["sdlc:in-progress"]
        lane.commit(wt, n, {STATION: STATION_TOML + f'tags = ["psych"]  # round {rnd}\n'}, "Add tags")
    _, (code, out, err) = lane.review(pr, wt, CHANGES, response="1. Accepted.")
    assert code == 1 and "round 3: changes" in out
    assert "Codex asked for changes in 3 rounds, more than the quick lane's 2" in err
    assert f"#{n} has outgrown the quick lane: escalating it to the full lane" in err
    # #208: each round counts against the quick lane's budget (`quick.max_change_rounds`), not `max_pr_rounds`
    budget = lane.cfg["quick"]["max_change_rounds"]
    assert budget != lane.cfg.get("max_pr_rounds", 5)
    used = [re.search(r"\((\d+/\d+) change rounds used\)", c["body"]).group(1) for c in lane.gh.records(pr, "pr-review")]
    assert used == [f"{k}/{budget}" for k in (1, 2, 3)]
    finished_escalation(lane, n, pr, "rounds", base)


def test_a_merge_that_touches_a_denied_path_moves_the_change_to_the_full_lane_and_planning_starts(lane):
    n = lane.quick_issue()
    wt = lane.claim(n)
    base = lane.main_sha()
    lane.commit(wt, n, {STATION: STATION_TOML, "src/twiddle/play.py": "# writes to a speaker\n"}, "Add Psyched FM")
    pr = lane.open_pr(n)
    lane.ok("test-record", pr, cwd=wt)
    _, (code, _, err) = lane.review(pr, wt, APPROVES)        # a passing run and Codex's approval don't change it
    assert code == 0, err
    code, _, err = lane.run("merge", pr)
    assert code == 1 and "the diff touches a path a quick change may not (src/twiddle/play.py)" in err
    assert not lane.on_main("src/twiddle/play.py")
    finished_escalation(lane, n, pr, "diff", base)


def test_when_codex_cannot_run_the_pr_stays_open_until_a_person_escalates_it(lane):
    gh = lane.gh
    n = lane.quick_issue()
    wt = lane.claim(n)
    base = lane.main_sha()
    lane.commit(wt, n, {STATION: STATION_TOML}, "Add Psyched FM")
    pr = lane.open_pr(n)
    lane.ok("test-record", pr, cwd=wt)
    code, _, out = lane.codex(pr, wt, NO_VERDICT, NO_VERDICT)
    assert code == 3 and "Codex gave no verdict in 2 attempts" in out
    before = gh.snapshot(n, pr)
    code, _, err = lane.run("review-defer", pr, "--reason", "Codex gave no verdict in 2 attempts", cwd=wt)
    assert code == 1 and "never deferred" in err and gh.snapshot(n, pr) == before
    code, _, err = lane.run("merge", pr)
    assert code == 1 and "no Codex review of the current head" in err and gh.snapshot(n, pr) == before
    # a person decides: the full lane
    out = lane.ok("escalate-to-full", n, "--reason", "Codex can't run, and this can wait for a plan")
    assert f"#{n}: moved to the full lane" in out
    finished_escalation(lane, n, pr, "manual", base)


def test_debt_filed_on_a_quick_change_follows_it_to_the_full_lane(lane):
    """#225, #247: debt filed while the change was quick joins the debt epic (opened for it) as a quick change's;
    once the change is escalated it is an epic's debt, and its marker and lead line say so. Its generation is
    kept, so it is never derived to wait on the escalated epic's milestones."""
    gh = lane.gh
    n = lane.quick_issue()
    lane.claim(n)
    other = gh.new_issue("Other debt", sdlc.debt_head(n + 100, "review", quick=True) + "\n\nNot this change's.",
                         ["tech-debt"], author=OWNER)
    body = lane.scratch / "debt.md"
    body.write_text("The station's tags are a guess.")
    out = lane.ok("file-issue", n, "--debt", "--source", "review", "--title", "Psyched FM: tags", "--body-file", body)
    debt = int(out.splitlines()[1].strip().rsplit("/", 1)[1])   # after "opened debt epic #E"
    epic = next(k for k, i in gh.issues.items() if "lane:debt" in i["labels"])
    assert out.splitlines()[0] == f"opened debt epic #{epic}" and gh.sub_issues[epic] == [debt]
    assert gh.issues[debt]["body"].splitlines()[0].endswith("lane=quick generation=1 -->")
    assert lane.state(debt)["next"].startswith(f"nothing for #{debt} alone: it is tech debt in debt epic #{epic} (")
    out = lane.ok("escalate-to-full", n, "--reason", "it needs a plan after all")
    assert f"#{debt} (tech debt from #{n}) is now an epic's debt" in out
    assert gh.issues[debt]["body"] == (sdlc.debt_head(n, "review", debt_epic=epic) + "\n\nThe station's tags are a guess.\n")
    st = lane.state(debt)
    assert (st["debt_lane"], st["debt_generation"], st["waiting_for"]) == (None, 1, None)
    assert "lane=quick" in gh.issues[other]["body"]                 # another change's debt is left alone
    mk = sdlc.parse_marker(gh.issues[debt]["body"])                 # #n's debt now: no longer a quick change's
    assert (mk["kind"], mk["epic"], mk.get("lane")) == ("debt", str(n), None)


# --- the full lane, untouched ----------------------------------------------------------------------------------

def test_a_full_lane_slice_still_goes_from_its_plan_to_its_epic_branch(lane):
    gh = lane.gh
    marker = lambda kind, rev=None, **kw: sdlc.marker(kind, rev, **kw) + "\ntext"   # noqa: E731
    epic = gh.new_issue("Alarm manager", "Manage alarms.", ["sdlc:planned"])
    for author, body in [(OWNER, marker("prd", 1)), (POSTER, "/approve"),
                         (OWNER, marker("approval", 1, by=POSTER, via="comment")), (OWNER, marker("plan", 1)),
                         (OWNER, marker("plan-review", 1, verdict="approve", round="1")),
                         (OWNER, marker("plan-created", 1))]:
        gh.say(epic, author, body)
    sections = "\n\n".join(f"## {s}\n{s.lower()}" for s in sdlc.LEAF_SECTIONS.values())
    s = gh.new_issue(f"#{epic} T1: List alarms", sdlc.marker("slice", None, epic=str(epic), key="T1")
                     + f"\nEpic: #{epic}\n\n{sections}", ["plan:slice", "complexity:judgment"], author=OWNER,
                     milestone=f"#{epic} M1: alarms listed")
    main_before = lane.main_sha()
    lane.ok("epic-branch", epic)
    epic_before = gh.sha(f"refs/heads/epic/{epic}")
    assert f"next: /work-slice {s}" in lane.ok("state", epic)

    out = lane.ok("claim", s, "--model", "opus")
    assert f"BASE=origin/epic/{epic}" in out and f"#{epic}: planned -> in-progress" in out
    wt = Path(re.search(r"^WORKTREE=(.+)$", out, re.M).group(1))
    head = lane.commit(wt, s, {"alarms.py": "ALARMS = []\n"}, "List alarms")
    pr = lane.open_pr(s, base=f"epic/{epic}", title="T1: List alarms")
    lane.ok("test-record", pr, cwd=wt)
    report, (code, _, err) = lane.review(pr, wt, APPROVES)
    assert code == 0, err
    assert "Brief:" not in report.read_text()                      # a slice's review names no quick brief
    out = lane.ok("merge", pr)
    assert out.splitlines() == [f"merged PR #{pr} into epic/{epic}; #{s} closed",
                                f"milestone #{epic} M1: alarms listed is complete: its demo comes before any new slice",
                                f"next: /milestone-demo {epic}: #{epic} M1: alarms listed is complete, and new slices "
                                "wait for its demo"]

    # one squash on epic/E; main untouched; the slice closed by the tool (GitHub only closes on main)
    assert gh.origin_git("rev-parse", f"epic/{epic}^") == epic_before and lane.main_sha() == main_before
    assert gh.origin_git("show", f"epic/{epic}:alarms.py") == "ALARMS = []" and not lane.on_main("alarms.py")
    assert ["issue", "close", str(s), "--repo", REPO, "--reason", "completed", "--comment", f"Merged in #{pr}."] in gh.calls
    assert (gh.issues[s]["state"], gh.issues[s]["state_reason"]) == ("closed", "completed")
    assert ["pr", "merge", str(pr), "--repo", REPO, "--squash", "--match-head-commit", head] in gh.calls
    # and nothing of the quick lane on any of it
    assert all("lane:quick" not in i["labels"] for i in gh.issues.values())
    assert not any(gh.records(k, kind) for k in gh.comments for kind in ("quick-merged", "lane"))
    assert sdlc_labels(gh.issues[epic]["labels"]) == ["sdlc:in-progress"]


# --- the guard itself, and the quick lane's unit tests run under it --------------------------------------------

def test_the_guard_stops_a_real_gh_codex_or_socket(tmp_path):
    with pytest.raises(pytest.fail.Exception, match="a real `gh`"):
        subprocess.run(["gh", "--version"], capture_output=True)
    with pytest.raises(pytest.fail.Exception, match="not the fake one"):
        subprocess.run(["codex", "--version"], capture_output=True)         # only the sentinel is on PATH here
    with pytest.raises(pytest.fail.Exception, match="offline"):
        socket.create_connection(("192.0.2.1", 443), timeout=0.1)          # TEST-NET-1: never routed anyway
    assert not (tmp_path / "sentinel.log").exists()


PLUGIN = '''
import os, socket, subprocess, ipaddress
LOG = os.environ["OFFLINE_GUARD_LOG"]

def _refuse(what):
    with open(LOG, "a") as f:
        f.write(what + "\\n")
    raise RuntimeError("offline guard: " + what)

class Guarded(subprocess.Popen):
    def __init__(self, args, *a, **kw):
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        if os.path.basename(os.fsdecode(argv[0])) in ("gh", "codex"):
            _refuse("subprocess " + repr(argv))
        super().__init__(args, *a, **kw)

subprocess.Popen = Guarded
_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex

def check(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        try:
            ok = ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            ok = address[0] == "localhost"
        if not ok:
            _refuse("socket " + repr(address))

socket.socket.connect = lambda sock, address: check(sock, address) or _connect(sock, address)
socket.socket.connect_ex = lambda sock, address: check(sock, address) or _connect_ex(sock, address)
'''


@pytest.mark.parametrize("call", ["connect", "connect_ex"])
def test_the_child_guard_refuses_a_socket_off_the_box_by_either_call(tmp_path, call):
    plugin, log = tmp_path / "plugin", tmp_path / "reached.log"
    plugin.mkdir()
    (plugin / "offline_guard.py").write_text(PLUGIN)
    env = {**os.environ, "OFFLINE_GUARD_LOG": str(log), "PYTHONPATH": str(plugin)}
    code = ("import socket, sys\n"
            # the real calls are replaced first, so a guard that lets this through reaches a sentinel, not a network
            "def sentinel(sock, address):\n    print('reached the real socket call'); sys.exit(3)\n"
            "socket.socket.connect = socket.socket.connect_ex = sentinel\n"
            "import offline_guard\n"
            "s = socket.socket()\n"
            f"try:\n    s.{call}(('192.0.2.1', 9))\nexcept RuntimeError as e:\n    print('refused', e)\n"
            "else:\n    print('connected')\n")
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert proc.stdout.startswith("refused offline guard: socket ('192.0.2.1', 9)"), proc.stdout + proc.stderr
    assert "192.0.2.1" in log.read_text()


def test_the_quick_lane_unit_tests_run_offline(tmp_path):
    plugin, log = tmp_path / "plugin", tmp_path / "reached.log"
    plugin.mkdir()
    (plugin / "offline_guard.py").write_text(PLUGIN)
    env = {**os.environ, "OFFLINE_GUARD_LOG": str(log),
           "PYTHONPATH": os.pathsep.join(filter(None, [str(plugin), os.environ.get("PYTHONPATH")]))}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "offline_guard", "-p", "no:cacheprovider",
                           "tests/test_sdlc_quick.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert re.search(r"\d+ passed", proc.stdout) and not log.exists(), log.read_text() if log.exists() else ""
    assert not (tmp_path / "sentinel.log").exists()
