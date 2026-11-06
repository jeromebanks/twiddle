"""tools/sdlc_watch_github.py: change detection and which issues come into care (#263 T1.2).

Offline: a fake `gh` and a fake `claude` first on PATH, tmp git repos, and a fake `sdlc.py state`."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools import sdlc, sdlc_watch as w, sdlc_watch_github as gw

FIXTURES = Path(__file__).parent / "fixtures"
REPO = "jeromebanks/twiddle"
OWNER, COLLAB, STRANGER = "jeromebanks", "helper", "stranger"
T0 = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)     # the watcher's start
BEFORE, AFTER, LATER = "2026-11-05T11:00:00Z", "2026-11-05T12:05:00Z", "2026-11-05T12:09:00Z"
SLICE_STATE = json.loads((FIXTURES / "sdlc_state_slice_324.json").read_text())
# #321's comment 6617548465, trimmed: a plain agent note posted under the owner's login
AGENT_NOTE = ("> 🤖 **twiddle agent** — a note, not a reply from the poster\n\nF3 (#328) covers this. "
              "It was merged into `epic/100` in PR #339 and reaches `main` when #100's M2 ships.\n\n"
              "I'm not closing this issue; that decision is left to its triage or to the debt lane.")

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
here = Path(os.environ["FAKE_GH_DIR"])
args = sys.argv[1:]
with open(here / "calls.jsonl", "a") as fh:
    fh.write(json.dumps(args) + "\n")
scene = json.loads((here / "scene.json").read_text())
writes = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
if args[:1] != ["api"] or writes & set(args):
    print("fake gh: not a read: %r" % (args,), file=sys.stderr); sys.exit(97)
rest = args[1:]
if rest[:1] == ["-i"]:
    rest = rest[1:]
    sent = None
    if rest[:1] == ["-H"]:
        sent = rest[1].split(": ", 1)[1]; rest = rest[2:]
    path = rest[0]
    probe = scene["probes"][path]
    if sent is not None and sent.removeprefix("W/") == probe["etag"]:
        sys.stdout.write((here / "304.txt").read_text().replace("ETAG", probe["etag"]))
        print("gh: HTTP 304", file=sys.stderr); sys.exit(1)
    sys.stdout.write("HTTP/2.0 200 OK\nContent-Type: application/json\r\nEtag: W/" + probe["etag"]
                     + "\r\n\r\n" + json.dumps(probe.get("body", [])))
    sys.exit(0)
path = rest[0]
if "/collaborators" in path:
    if scene.get("collaborators_fail"):
        print("gh: HTTP 503", file=sys.stderr); sys.exit(1)
    print(json.dumps([[{"login": l, "permissions": {"push": True}} for l in scene["collaborators"]]])); sys.exit(0)
base, _, query = path.partition("?")
edit = scene.get("edit")   # a person edits a comment between two pages of one read
if edit and base.endswith("/issues/comments") and "since=" in query:
    count = here / "reads"
    n = int(count.read_text()) + 1 if count.exists() else 1
    count.write_text(str(n))
    if n == edit["after"] + 1:
        for c in scene["get"][base]:
            if c["id"] == edit["id"]:
                c["updated_at"] = edit["updated_at"]
        (here / "scene.json").write_text(json.dumps(scene))
if base in scene["get"]:
    got = scene["get"][base]
    if isinstance(got, list):   # updated at or after since, oldest first, one page of per_page
        q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
        if "since" in q:
            key = q.get("sort", "updated") + "_at"
            got = sorted((g for g in got if g["updated_at"] >= q["since"]), key=lambda g: (g[key], g.get("id") or g.get("number")))
        size, page = int(q.get("per_page", 30)), int(q.get("page", 1))
        got = got[(page - 1) * size:page * size]
    print(json.dumps(got)); sys.exit(0)
print("fake gh: unexpected %r" % (args,), file=sys.stderr); sys.exit(98)
'''

FAKE_CLAUDE = "#!/bin/sh\necho ran >> \"$FAKE_GH_DIR/claude-ran\"\nexit 1\n"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A clean primary checkout pushed to a bare origin: the watcher can take its pin there."""
    origin, root = tmp_path / "origin.git", tmp_path / "primary"
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git("config", k, v, cwd=root)
    (root / ".sdlc").mkdir()
    (root / ".sdlc" / "config.json").write_text(json.dumps({"schema": 1, "repository": REPO}))
    (root / ".gitignore").write_text("logs/\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "main", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("push", "-q", "-u", "origin", "main", cwd=root)
    return root


class Fakes:
    """The fake `gh` (every call logged), the fake `claude` (fails the test if run) and `state`."""

    def __init__(self, tmp_path, monkeypatch):
        self.dir = tmp_path / "fakebin"
        self.dir.mkdir()
        for name, text in (("gh", FAKE_GH), ("claude", FAKE_CLAUDE)):
            (self.dir / name).write_text(text)
            (self.dir / name).chmod(0o755)
        shutil.copy(FIXTURES / "gh_api_304.txt", self.dir / "304.txt")
        fixture = (self.dir / "304.txt").read_bytes()
        etag_line = next(l for l in fixture.split(b"\r\n") if l.lower().startswith(b"etag:"))
        (self.dir / "304.txt").write_bytes(fixture.replace(etag_line, b"Etag: ETAG"))
        monkeypatch.setenv("PATH", f"{self.dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_GH_DIR", str(self.dir))
        monkeypatch.delenv(w.WATCH_RUN_ENV, raising=False)
        self.states: dict[int, dict] = {}
        self.asked: list[int] = []
        self.scene(etags=("e-issues-1", "e-comments-1"))

    def scene(self, etags, issues=(), comments=(), get=None, collaborators=(OWNER, COLLAB), fail_trust=False,
              edit=None):
        probes = {f"repos/{REPO}/{gw.PROBES['issues']}": {"etag": f'"{etags[0]}"', "body": list(issues)[-1:]},
                  f"repos/{REPO}/{gw.PROBES['comments']}": {"etag": f'"{etags[1]}"', "body": list(comments)[-1:]}}
        gets = {f"repos/{REPO}/issues": list(issues), f"repos/{REPO}/issues/comments": list(comments), **(get or {})}
        (self.dir / "scene.json").write_text(json.dumps({"probes": probes, "get": gets,
                                                         "collaborators": list(collaborators),
                                                         "collaborators_fail": fail_trust, "edit": edit}))
        (self.dir / "reads").unlink(missing_ok=True)
        (self.dir / "calls.jsonl").write_text("")

    def calls(self):
        return [json.loads(l) for l in (self.dir / "calls.jsonl").read_text().splitlines()]

    def state(self, n):
        self.asked.append(n)
        return self.states[n]


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    return Fakes(tmp_path, monkeypatch)


def started(repo):
    w.start(where=repo, at=T0)


def poll(repo, fakes):
    return w.poll_dry_run(where=repo, state=fakes.state)


def record(repo):
    return w.read_record(repo / "logs" / "sdlc_watch")


def issue(n, by=OWNER, at=BEFORE, body="", labels=(), pr=False, updated=None):
    out = {"number": n, "user": {"login": by}, "created_at": at, "updated_at": updated or at, "body": body,
           "labels": [{"name": l} for l in labels], "state": "open"}
    if pr:
        out["pull_request"] = {"url": "x"}
    return out


_ids = iter(range(1000, 10**6))


def comment(n, by=OWNER, at=AFTER, body="a reply"):
    return {"id": next(_ids), "issue_url": f"https://api.github.com/repos/{REPO}/issues/{n}",
            "user": {"login": by}, "created_at": at, "updated_at": at, "body": body}


def epic_state(n, turn="agent", action="work_slices", nxt=None, state="in-progress"):
    return {"number": n, "state": state, "turn": turn, "action": action, "next": nxt or f"/work-slice {n + 1}"}


# --- parsing gh's answers -----------------------------------------------------------------

def test_the_real_304_is_recognised_from_gh_output():
    """The fixture is gh 2.102's real stdout for a 304 (exit 1, `gh: HTTP 304` on stderr): status line LF,
    headers CRLF, no body. GitHub didn't count it: X-Ratelimit-Used was 100 before and after."""
    raw = (FIXTURES / "gh_api_304.txt").read_bytes()
    assert raw.startswith(b"HTTP/2.0 304 Not Modified\n") and b"\r\n" in raw
    status, headers, body = gw.parse_response(raw.decode())
    assert status == 304 and body == "" and headers["etag"].startswith('"')
    assert headers["x-ratelimit-used"] == "100"


def test_a_200_carries_its_etag_and_body():
    status, headers, body = gw.parse_response('HTTP/2.0 200 OK\nEtag: W/"abc"\r\nX: y\r\n\r\n[{"a": 1}]')
    assert (status, headers["etag"], json.loads(body)) == (200, 'W/"abc"', [{"a": 1}])


@pytest.mark.parametrize("out", ["", "not http", "HTTP/2.0 abc"])
def test_output_with_no_status_is_an_error(out):
    with pytest.raises(gw.GitHubError):
        gw.parse_response(out)


def test_an_error_status_is_never_taken_for_no_change(monkeypatch):
    monkeypatch.setattr(gw, "run_gh", lambda args: subprocess.CompletedProcess(
        args, 1, "HTTP/2.0 401 Unauthorized\nX: y\r\n\r\n{}", "gh: HTTP 401"))
    with pytest.raises(gw.GitHubError, match="401"):
        gw.conditional_get("repos/x/y/issues", '"e"')


def test_gh_is_bounded(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("gh", gw.GH_TIMEOUT_S)
    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(gw.GitHubError, match="timed out"):
        gw.run_gh(["api", "x"])
    assert 0 < gw.GH_TIMEOUT_S < 600 and 0 < gw.STATE_TIMEOUT_S <= 600 and 0 < gw.MAX_PAGES <= 50
    assert 0 < gw.RETRY_BASE_S <= gw.RETRY_MAX_S <= 24 * 3600


def test_requests_per_hour_stay_under_github_s_limit():
    """Criterion 2: a quiet poll is the two probes; at the 60s interval that is 120 an hour, even counted."""
    interval = w.DEFAULT_WATCH["interval_s"]
    assert gw.REQUESTS_PER_QUIET_POLL == 2
    assert gw.REQUESTS_PER_QUIET_POLL * 3600 // interval < gw.GITHUB_HOURLY_LIMIT / 10


# --- criterion 2: a quiet poll ------------------------------------------------------------------

def test_a_poll_after_no_change_makes_only_the_counted_conditional_requests(repo, fakes):
    started(repo)
    out1 = poll(repo, fakes)                      # the first poll: no ETag yet, so both read
    assert "changed: issues, comments" in out1
    fakes.scene(etags=("e-issues-1", "e-comments-1"))   # nothing new on GitHub
    out2 = poll(repo, fakes)
    calls = fakes.calls()
    assert len(calls) == gw.REQUESTS_PER_QUIET_POLL
    assert all(c[:3] == ["api", "-i", "-H"] and c[3].startswith("If-None-Match: ") for c in calls)
    assert sorted(c[4] for c in calls) == sorted(f"repos/{REPO}/{p}" for p in gw.PROBES.values())
    assert "nothing changed: no state read, nothing to run" in out2
    assert fakes.asked == [] and not (fakes.dir / "claude-ran").exists()
    line = [json.loads(l) for l in (repo / "logs" / "sdlc_watch" / w.LOG_NAME).read_text().splitlines()][-1]
    assert line["event"] == "poll" and line["requests"] == 2 and line["changed"] == [] and line["would_run"] == []


def test_a_quiet_poll_reads_no_state_even_with_issues_in_care(repo, fakes):
    started(repo)
    fakes.states[40] = epic_state(40)
    fakes.scene(etags=("a", "b"), comments=[comment(40)], get={f"repos/{REPO}/issues/40": issue(40)})
    poll(repo, fakes)
    assert fakes.asked == [40]
    fakes.asked.clear()
    fakes.scene(etags=("a", "b"))
    poll(repo, fakes)
    assert fakes.asked == [] and len(fakes.calls()) == 2 and "40" in record(repo)["care"]


def test_a_failed_read_writes_nothing_so_the_change_is_seen_again(repo, fakes):
    started(repo)
    poll(repo, fakes)
    before = record(repo)["github"]
    scene = {"probes": {}, "get": {}, "collaborators": []}   # every probe path unknown: the fake gh fails
    (fakes.dir / "scene.json").write_text(json.dumps(scene))
    with pytest.raises(w.WatchError):
        poll(repo, fakes)
    assert record(repo)["github"] == before


def test_a_capped_read_keeps_the_old_etag_and_carries_on(repo, fakes, monkeypatch):
    started(repo)
    monkeypatch.setattr(gw, "PER_PAGE", 1)
    monkeypatch.setattr(gw, "MAX_PAGES", 1)
    many = [comment(7, by=STRANGER, at=f"2026-11-05T12:0{i}:00Z") for i in range(1, 4)]
    fakes.scene(etags=("a", "b"), comments=many)
    out = poll(repo, fakes)
    rec = record(repo)["github"]
    assert "read capped" in out and "comments" not in rec["etags"]       # no old ETag: read again next poll
    assert rec["since"]["comments"] == {"at": "2026-11-05T12:01:00Z"}     # the last item read


# --- criterion 3: who brings an issue into care ----------------------------------------------

def _care_after(repo, fakes, issues=(), comments=(), get=None):
    started(repo)
    fakes.scene(etags=("x", "y"), issues=issues, comments=comments, get=get)
    out = poll(repo, fakes)
    return set(record(repo)["care"]), out


@pytest.mark.parametrize("who", [OWNER, COLLAB])
@pytest.mark.parametrize("body", ["I think the second option", "/approve", "/changes make it blue"])
def test_a_person_s_comment_approve_or_changes_brings_the_issue_into_care(repo, fakes, who, body):
    fakes.states[50] = epic_state(50, turn="agent", action="respond_to_reply", nxt="/triage-issue 50")
    care, out = _care_after(repo, fakes, comments=[comment(50, by=who, body=body)],
                            get={f"repos/{REPO}/issues/50": issue(50, by=STRANGER)})
    assert care == {"50"} and "into care: #50" in out and "would run `/triage-issue 50`" in out
    assert not (fakes.dir / "claude-ran").exists()


@pytest.mark.parametrize("who", [OWNER, COLLAB])
def test_a_person_s_new_issue_comes_into_care(repo, fakes, who):
    fakes.states[60] = epic_state(60, action="triage", nxt="/triage-issue 60")
    care, out = _care_after(repo, fakes, issues=[issue(60, by=who, at=AFTER)])
    assert care == {"60"} and "new issue by" in out


def test_the_backlog_at_start_never_comes_into_care(repo, fakes):
    """`next`'s list on install: everything opened or commented on before the start."""
    care, out = _care_after(repo, fakes, issues=[issue(n, at=BEFORE, updated=AFTER) for n in (1, 2, 3)],
                            comments=[comment(2, at=BEFORE)])
    assert care == set() and fakes.asked == [] and "nothing in care" in out


@pytest.mark.parametrize("body", [
    sdlc.marker("question", 1) + "\nWhat colour?",
    sdlc.marker("note", None) + "\nclaimed",
    "Some words\n<!-- sdlc:v1 kind=note -->",
])
def test_a_marked_comment_never_brings_an_issue_into_care(repo, fakes, body):
    care, _ = _care_after(repo, fakes, comments=[comment(70, body=body)],
                          get={f"repos/{REPO}/issues/70": issue(70)})
    assert care == set() and fakes.asked == []


def test_a_marked_new_issue_never_comes_into_care(repo, fakes):
    """A file-issue finding or a tech-debt issue, opened under the owner's login (#263 itself)."""
    body = "<!-- sdlc:v1 kind=debt epic=100 lane=full generation=1 -->\nA finding"
    care, _ = _care_after(repo, fakes, issues=[issue(80, at=AFTER, body=body, labels=["tech-debt"])])
    assert care == set() and fakes.asked == []


def test_a_stranger_s_issue_and_their_own_comment_and_approve_on_it_stay_out(repo, fakes):
    care, _ = _care_after(repo, fakes, issues=[issue(90, by=STRANGER, at=AFTER)],
                          comments=[comment(90, by=STRANGER, body="please"),
                                    comment(90, by=STRANGER, body="/approve", at=LATER)])
    assert care == set() and fakes.asked == []


def test_a_stranger_s_comment_on_the_owner_s_issue_stays_out(repo, fakes):
    care, _ = _care_after(repo, fakes, comments=[comment(91, by=STRANGER, body="/approve")],
                          get={f"repos/{REPO}/issues/91": issue(91)})
    assert care == set()


def test_a_comment_on_a_pull_request_is_not_an_issue(repo, fakes):
    care, _ = _care_after(repo, fakes, comments=[comment(333)],
                          get={f"repos/{REPO}/issues/333": issue(333, pr=True)})
    assert care == set()


# --- a plain agent note ------------------------------------------------------------------------------

@pytest.mark.parametrize("body,note", [
    (AGENT_NOTE, True), (AGENT_NOTE.replace("\n", "\r\n"), True),
    ("> 🤖 **twiddle agent** — one line, no newline", True),
    ("I'm a person\n> 🤖 **twiddle agent** — quoted", False), ("> a quote", False), ("", False), (None, False),
])
def test_is_agent_note(body, note):
    assert sdlc.is_agent_note(body) is note


def test_a_plain_agent_note_brings_nothing_into_care(repo, fakes):
    care, out = _care_after(repo, fakes, comments=[comment(321, body=AGENT_NOTE)],
                            get={f"repos/{REPO}/issues/321": issue(321)})
    assert care == set() and fakes.asked == [] and not (fakes.dir / "claude-ran").exists()


def _derive(comments, labels=("sdlc:needs-info",)):
    iss = {"number": 321, "title": "t", "state": "open", "author": OWNER, "labels": list(labels), "body": ""}
    cfg = {"labels": [{"name": f"sdlc:{s}"} for s in ("needs-info", "in-progress", "planned", "approved")]}
    return sdlc.derive_state(iss, comments, {OWNER}, cfg)


def _c(i, body, at):
    return {"id": i, "author": OWNER, "body": body, "created_at": at, "url": f"u{i}"}


def test_state_does_not_count_a_plain_agent_note_as_a_reply():
    question = _c(1, sdlc.marker("question", 1) + "\nWhat colour?", "2026-11-01T00:00:00Z")
    st = _derive([question, _c(2, AGENT_NOTE, "2026-11-01T01:00:00Z")])
    assert st["replies"] == [] and st["turn"] == "poster"
    st = _derive([question, _c(2, AGENT_NOTE, "2026-11-01T01:00:00Z"), _c(3, "blue", "2026-11-01T02:00:00Z")])
    assert [r["id"] for r in st["replies"]] == [3] and st["action"] == "respond_to_reply"


def test_a_plain_agent_note_is_no_answer_to_a_demo_request():
    request = _c(1, sdlc.marker("demo-request", None, milestone="M1") + "\nRun this", "2026-11-01T00:00:00Z")
    st = _derive([request, _c(2, AGENT_NOTE, "2026-11-01T01:00:00Z")], labels=("sdlc:in-progress",))
    assert st["demo_request"]["replies"] == [] and st["action"] != "demo_heard"
    st = _derive([request, _c(3, "heard it", "2026-11-01T02:00:00Z")], labels=("sdlc:in-progress",))
    assert st["action"] == "demo_heard"


# --- slices and subtasks: their epic comes into care -------------------------------------------------

def _slice_issue():
    body = sdlc.marker("slice", None, epic="263", key="T0.1") + "\nEpic: #263 · Parent: #323\n\n## Outcome\n\nx"
    return issue(324, body=body, labels=["plan:slice", "complexity:judgment"])


def _subtask_issue():
    body = sdlc.render_container_body(263, {"key": "T1", "summary": "The watcher"})
    return issue(342, body=body, labels=["plan:subtask"])


@pytest.mark.parametrize("make,n", [(_slice_issue, 324), (_subtask_issue, 342)])
def test_a_person_s_comment_on_a_slice_or_subtask_brings_its_epic_into_care(repo, fakes, make, n):
    fakes.states[324] = SLICE_STATE        # the real `state 324 --json`: slice-status, no turn
    fakes.states[263] = epic_state(263, nxt="/work-slice 348 (opus: judgment)")
    care, out = _care_after(repo, fakes, comments=[comment(n, body="why is this slow?")],
                            get={f"repos/{REPO}/issues/{n}": make()})
    assert care == {"263"} and fakes.asked == [263]
    assert f"into care: #263 (a comment by {OWNER} on #{n})" in out
    assert "would run `/work-slice 348 (opus: judgment)`" in out
    entry = record(repo)["care"]["263"]
    assert entry["last_person_at"].startswith("2026-11-05T12:05")


def test_a_forged_marker_without_the_plan_label_is_a_plain_issue():
    body = sdlc.marker("slice", None, epic="263", key="T0.1")
    assert gw.care_target(issue(500, body=body)) == 500
    assert gw.care_target(issue(501, body="no marker", labels=["plan:slice"])) is None
    assert gw.care_target(issue(502, body=sdlc.marker("subtask", None, epic="263"), labels=["plan:slice"])) is None


def test_a_slice_already_in_care_is_dropped_and_never_run(repo, fakes):
    started(repo)
    root = repo / "logs" / "sdlc_watch"
    rec = w.read_record(root)
    rec["care"] = {"324": {"since": "2026-11-05T12:01:00+00:00", "why": "old"}}
    w.write_record(root, rec)
    fakes.states[324] = SLICE_STATE
    fakes.scene(etags=("x", "y"))
    out = poll(repo, fakes)
    assert record(repo)["care"] == {} and "left care: #324 (not an epic or a top-level issue)" in out
    assert "would run" not in out


# --- leaving, other turns, pauses --------------------------------------------------------------------

def test_done_leaves_care_and_the_poster_s_move_runs_nothing(repo, fakes):
    fakes.states[10] = epic_state(10, turn="later", action="none", state="done")
    fakes.states[11] = epic_state(11, turn="poster", action="wait_for_poster")
    fakes.states[12] = epic_state(12, nxt="/plan-issue 12")
    gets = {f"repos/{REPO}/issues/{n}": issue(n) for n in (10, 11, 12)}
    care, out = _care_after(repo, fakes, comments=[comment(n) for n in (10, 11, 12)], get=gets)
    assert care == {"11", "12"}
    assert "left care: #10 (done)" in out and "#11: poster's move (wait_for_poster): nothing" in out
    assert "would run `/plan-issue 12`" in out


def test_a_paused_issue_would_run_nothing(repo, fakes):
    started(repo)
    w.set_pause(repo / "logs" / "sdlc_watch", 12, True)
    fakes.states[12] = epic_state(12, nxt="/plan-issue 12")
    fakes.scene(etags=("x", "y"), comments=[comment(12)], get={f"repos/{REPO}/issues/12": issue(12)})
    out = poll(repo, fakes)
    assert "#12: paused: nothing" in out and "would run" not in out
    assert "12" in record(repo)["paused"]["issues"]      # the poll kept the pause it didn't write


# --- nothing here writes to GitHub -----------------------------------------------------------------

def test_every_gh_call_is_a_read(repo, fakes):
    fakes.states[263] = epic_state(263)
    fakes.states[60] = epic_state(60)
    _care_after(repo, fakes, issues=[issue(60, at=AFTER)], comments=[comment(342)],
                get={f"repos/{REPO}/issues/342": _subtask_issue()})
    calls = fakes.calls()
    assert calls and all(c[0] == "api" for c in calls)
    flags = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
    assert not any(flags & set(c) for c in calls)


def test_the_module_has_no_write_to_github():
    text = Path(gw.__file__).read_text()
    for needle in ('"-X"', '"--method"', '"-f"', '"-F"', '"--input"', "post_comment", "set_labels",
                   '"comment"', '"edit"', "transition("):
        assert needle not in text, needle


def test_run_state_asks_sdlc_py_for_json(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "sdlc.py").write_text(textwrap.dedent("""
        import json, sys
        print(json.dumps({"argv": sys.argv[1:], "turn": "poster"}))
    """))
    assert gw.run_state(tmp_path, 263)["argv"] == ["state", "263", "--json"]
    (tmp_path / "tools" / "sdlc.py").write_text("import sys; sys.exit(2)\n")
    with pytest.raises(gw.GitHubError, match="state 263 failed"):
        gw.run_state(tmp_path, 263)


def test_poll_requires_dry_run(capsys):
    with pytest.raises(SystemExit):
        w.main(["poll"])
    assert "--dry-run" in capsys.readouterr().err


def test_one_issue_s_failing_state_does_not_wedge_the_poll(repo, fakes):
    fakes.states[13] = epic_state(13, nxt="/plan-issue 13")

    def state(n):
        if n == 14:
            raise gw.GitHubError("sdlc.py state 14 failed: gone")
        return fakes.state(n)
    started(repo)
    fakes.scene(etags=("x", "y"), comments=[comment(13), comment(14)],
                get={f"repos/{REPO}/issues/{n}": issue(n) for n in (13, 14)})
    out = w.poll_dry_run(where=repo, state=state)
    assert "#14: state failed (sdlc.py state 14 failed: gone): nothing" in out
    assert "would run `/plan-issue 13`" in out
    rec = record(repo)
    assert set(rec["care"]) == {"13", "14"} and rec["github"]["since"]      # the poll still recorded what it read
    assert rec["github"]["etags"] == {} and rec["github"]["state_retry"]["failures"] == 1   # unacknowledged


def test_a_debt_ticket_s_shape_never_stays_in_care():
    st = {"number": 70, "state": "debt", "turn": "none", "action": "none", "plan_kind": "debt"}
    assert gw.decide(70, st, False)["leave"] is True


def test_the_since_read_reaches_back_a_second():
    assert gw.shifted("2026-11-05T12:00:00Z") == "2026-11-05T11:59:59Z"
    assert gw.shifted("2026-11-05T12:00:00+00:00") == "2026-11-05T11:59:59Z"


def test_a_failed_collaborator_lookup_writes_nothing_and_the_comment_is_read_after(repo, fakes):
    """Never fall back to the owner alone: that would read past a collaborator's comment for good."""
    started(repo)
    fakes.states[44] = epic_state(44, nxt="/triage-issue 44")
    gets = {f"repos/{REPO}/issues/44": issue(44)}
    fakes.scene(etags=("x", "y"), comments=[comment(44, by=COLLAB)], get=gets, fail_trust=True)
    with pytest.raises(w.WatchError, match="collaborators"):
        poll(repo, fakes)
    assert record(repo).get("github") is None and record(repo)["care"] == {}
    fakes.scene(etags=("x", "y"), comments=[comment(44, by=COLLAB)], get=gets)
    assert "into care: #44" in poll(repo, fakes)


@pytest.mark.parametrize("same_second", [False, True])
def test_capped_reads_reach_the_last_comment_over_several_polls(repo, fakes, monkeypatch, same_second):
    """A backlog bigger than a capped read: each poll carries on, by time or, within one second, by page."""
    started(repo)
    monkeypatch.setattr(gw, "PER_PAGE", 3)
    monkeypatch.setattr(gw, "MAX_PAGES", 3)
    at = (lambda i: AFTER) if same_second else (lambda i: f"2026-11-05T12:{10 + i:02d}:00Z")
    backlog = [comment(7, by=STRANGER, at=at(i)) for i in range(7)] + [comment(45, at=at(7))]
    fakes.states[45] = epic_state(45, nxt="/triage-issue 45")
    gets = {f"repos/{REPO}/issues/45": issue(45)}
    for polls in range(1, 9):
        fakes.scene(etags=("x", "y"), comments=backlog, get=gets)
        poll(repo, fakes)
        if "45" in record(repo)["care"]:
            break
    assert "45" in record(repo)["care"] and polls <= len(backlog)   # every poll moves on


def test_an_edit_between_two_pages_of_a_read_skips_no_comment(repo, fakes, monkeypatch):
    """Codex round 2: paged by position, editing the first comment after page 1 moved the owner's unread
    comment onto page 1, and page 2 missed it. Paged by time, it is read."""
    started(repo)
    monkeypatch.setattr(gw, "PER_PAGE", 2)
    first = comment(7, by=STRANGER, at="2026-11-05T12:01:00Z")
    rest = [comment(7, by=STRANGER, at="2026-11-05T12:02:00Z"), comment(46, at="2026-11-05T12:03:00Z"),
            comment(7, by=STRANGER, at="2026-11-05T12:04:00Z"), comment(7, by=STRANGER, at="2026-11-05T12:05:00Z")]
    fakes.states[46] = epic_state(46, nxt="/triage-issue 46")
    fakes.scene(etags=("x", "y"), comments=[first, *rest], get={f"repos/{REPO}/issues/46": issue(46)},
                edit={"after": 1, "id": first["id"], "updated_at": "2026-11-05T12:30:00Z"})
    poll(repo, fakes)
    assert "46" in record(repo)["care"]
    assert record(repo)["github"]["since"]["comments"] == {"at": "2026-11-05T12:30:00Z"}


def test_a_failed_state_leaves_the_change_unacknowledged_and_is_asked_again(repo, fakes, monkeypatch):
    """Codex rounds 2 and 4: the retry can't wait for another change on GitHub, a quiet poll never reads
    state, and an outage longer than any count of retries strands nothing."""
    clock = {"t": dt.datetime(2026, 11, 5, 12, 10, tzinfo=dt.timezone.utc)}
    monkeypatch.setattr(gw, "now", lambda: clock["t"])
    started(repo)
    fakes.states[15] = epic_state(15, nxt="/plan-issue 15")
    broken = {"on": True}

    def state(n):
        if broken["on"]:
            raise gw.GitHubError("sdlc.py state 15 failed: network")
        return fakes.state(n)
    fakes.scene(etags=("x", "y"), comments=[comment(15)], get={f"repos/{REPO}/issues/15": issue(15)})
    assert "state failed" in w.poll_dry_run(where=repo, state=state)
    waits = []
    for _ in range(8):                         # a long outage: each wait doubles, up to RETRY_MAX_S
        before = len(fakes.asked)
        out = w.poll_dry_run(where=repo, state=state)          # before the wait ends: no state read
        assert "state reads wait until" in out and fakes.asked[before:] == []
        retry = record(repo)["github"]["state_retry"]
        waits.append((gw.parse_time(retry["at"]) - clock["t"]).total_seconds())
        clock["t"] = gw.parse_time(retry["at"])
        assert "state failed" in w.poll_dry_run(where=repo, state=state)   # asked again, failed again
    assert waits[:3] == [60, 120, 240] and max(waits) == gw.RETRY_MAX_S
    clock["t"] = gw.parse_time(record(repo)["github"]["state_retry"]["at"])
    broken["on"] = False                       # it recovers, with nothing new on GitHub
    out = w.poll_dry_run(where=repo, state=state)
    assert "would run `/plan-issue 15`" in out and "state_retry" not in record(repo)["github"]
    fakes.asked.clear()
    fakes.scene(etags=("x", "y"))
    out = w.poll_dry_run(where=repo, state=state)            # acknowledged now: quiet, and no state read
    assert "nothing changed: no state read" in out and fakes.asked == [] and len(fakes.calls()) == 2


def test_an_edit_while_a_one_second_backlog_is_read_skips_no_comment(repo, fakes, monkeypatch):
    """Codex round 3: a page or more all in one second, the owner's comment last, the first comment
    edited just before the read falls back from time to position: it is still read."""
    started(repo)
    monkeypatch.setattr(gw, "PER_PAGE", 3)
    same = [comment(7, by=STRANGER, at=AFTER) for _ in range(3)] + [comment(47, at=AFTER)]
    fakes.states[47] = epic_state(47, nxt="/triage-issue 47")
    fakes.scene(etags=("x", "y"), comments=same, get={f"repos/{REPO}/issues/47": issue(47)},
                edit={"after": 2, "id": same[0]["id"], "updated_at": "2026-11-05T12:30:00Z"})
    poll(repo, fakes)
    reads = [c[1] for c in fakes.calls() if "issues/comments?" in c[1] and "since=" in c[1]]
    assert any("sort=created" in r for r in reads)            # the fallback was taken
    assert "47" in record(repo)["care"]
    assert record(repo)["github"]["since"]["comments"] == {"at": "2026-11-05T12:30:00Z"}


def test_a_hung_or_garbled_collaborator_list_fails_the_poll(monkeypatch):
    monkeypatch.setattr(gw, "run_gh", lambda args: subprocess.CompletedProcess(args, 0, "not json", ""))
    with pytest.raises(gw.GitHubError):
        gw.fetch_trusted({}, REPO)
    monkeypatch.setattr(gw, "run_gh", lambda args: subprocess.CompletedProcess(args, 0, '[["x"]]', ""))
    with pytest.raises(gw.GitHubError):
        gw.fetch_trusted({}, REPO)


def test_a_page_that_is_not_a_list_of_objects_is_an_error(monkeypatch):
    monkeypatch.setattr(gw, "get_json", lambda path, *f: [1, 2])
    with pytest.raises(gw.GitHubError, match="list of objects"):
        gw.read_since(REPO, "comments", {"at": AFTER})


class FakeGitHub:
    """GitHub's since-lists in process: `since` keeps items updated at or after it, `sort` orders them
    (ties by id), pages by position. Between any two requests `churn` may add, edit, delete, or edit an
    old item into the set, as people do while a read is under way."""

    def __init__(self, rng, items, clock):
        self.rng, self.items, self.clock, self.next_id = rng, items, clock, 10**6

    def stamp(self):
        return self.clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    def churn(self):
        if self.rng.random() < 0.5:
            self.clock += dt.timedelta(seconds=self.rng.choice([0, 0, 1]))
        op, live = self.rng.random(), [i for i in self.items if not i.get("deleted")]
        if op < 0.3:
            self.next_id += 1
            self.items.append({"id": self.next_id, "created_at": self.stamp(), "updated_at": self.stamp()})
        elif op < 0.5 and live:
            self.rng.choice(live)["updated_at"] = self.stamp()
        elif op < 0.65 and live:
            self.rng.choice(live)["deleted"] = True

    def get_json(self, path, *flags):
        self.churn()
        q = dict(kv.split("=", 1) for kv in path.partition("?")[2].split("&"))
        key = q["sort"] + "_at"
        rows = sorted((i for i in self.items if not i.get("deleted") and i["updated_at"] >= q["since"]),
                      key=lambda i: (i[key], i["id"]))
        size, page = int(q["per_page"]), int(q["page"])
        return [dict(i) for i in rows[(page - 1) * size:page * size]]


@pytest.mark.parametrize("seed", range(300))
def test_since_reads_miss_no_new_item_whatever_happens_between_requests(monkeypatch, seed):
    """The invariant behind Codex's rounds 1-3, as intake needs it: every item created after the start and
    still there is read at some poll, whatever new items, edits, deletions, old items edited into the set
    and one-second backlogs happen between any two requests."""
    import random
    rng = random.Random(seed)
    monkeypatch.setattr(gw, "PER_PAGE", rng.choice([5, 6, 8]))
    monkeypatch.setattr(gw, "MAX_PAGES", rng.choice([1, 2, 4]))
    clock = dt.datetime(2026, 11, 5, 12, 0, 10, tzinfo=dt.timezone.utc)
    start = "2026-11-05T12:00:05Z"
    items = [{"id": i, "created_at": "2026-11-05T11:00:00Z", "updated_at": "2026-11-05T11:00:00Z"}
             for i in range(8)]
    burst = rng.choice(["2026-11-05T12:00:06Z", "2026-11-05T12:00:10Z"])   # a backlog in one second
    items += [{"id": 100 + i, "created_at": burst, "updated_at": burst} for i in range(rng.randrange(0, 30))]
    fake = FakeGitHub(rng, items, clock)
    monkeypatch.setattr(gw, "get_json", fake.get_json)
    seen, mark, cut = set(), {"at": start}, True
    for poll_n in range(400):
        if poll_n == 40:
            fake.churn = lambda: None          # people stop: the reads must settle having read everything
        got, mark, cut = gw.read_since(REPO, "comments", mark)
        seen |= {i["id"] for i in got}
        if poll_n > 40 and not cut:
            break
    assert not cut
    missed = [i["id"] for i in fake.items if not i.get("deleted") and i["created_at"] > start and i["id"] not in seen]
    assert missed == []
