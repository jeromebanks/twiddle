#!/usr/bin/env python3
"""The SDLC watcher's change detection, and which issues come into its care (#263 T1.2).

`tools/sdlc_watch.py poll --dry-run` calls `poll()` here. A poll asks GitHub, conditionally,
whether anything changed; only when something did does it read what changed, bring the issues
a person acted on into care, and ask `sdlc.py state N --json` whose move each one is. It reports
what it would run and starts nothing. Nothing here writes to GitHub: every request is a `gh api`
GET with its query in the URL (no `-f`/`-F`, which would make it a POST).

**The two probes, every poll.** Each is `gh api -i -H 'If-None-Match: <etag>' <path>`; when
nothing changed GitHub answers `304 Not Modified`, which costs no tokens and, sent
authenticated, doesn't count against the rate limit (GitHub's "use conditional requests").

- `repos/R/issues?state=all&sort=updated&direction=desc&per_page=1`: the most recently updated
  issue or PR, open or closed. Its ETag moves on a new issue, a label change, a close or reopen,
  an edit, and a new comment (a comment updates its issue). `state=all`, so closing an issue in
  care is seen too.
- `repos/R/issues/comments?sort=updated&direction=desc&per_page=1`: the most recently updated
  comment on any issue or PR, so a new or edited comment is seen even if its issue's update were
  missed.

`gh` prints a 304 as the status line on stdout and exits 1 with `gh: HTTP 304` on stderr; the
status is read from stdout's first line whatever the exit code (a fixture keeps the real output,
`tests/fixtures/gh_api_304.txt`). Two requests a poll is 120 an hour at the 60s interval, against
GitHub's 5000 an hour for an authenticated client, even if the 304s did count.

**Only when a probe says something changed** does it read, unconditionally:

- the issues and the comments updated since the last poll's watermark
  (`...?since=<watermark less 1s>&sort=updated&direction=asc&per_page=100`, at most MAX_PAGES requests
  each, paged by time: every request is page 1 from the newest item read, so nothing shifts past it; a
  page or more inside one window is read on in `created_at` order, each page anchored on the last item
  taken (`read_since`); a capped read keeps the old ETag and the next poll carries on from its mark);
- who counts as a person: the repo's owner and its push collaborators (`sdlc.fetch_trusted`'s rule,
  but a failed lookup fails the poll rather than falling back to the owner alone);
- `sdlc.py state N --json` for each issue in care. On an epic that fans out to its slices' reads,
  so a change poll costs more than a quiet one: it is bounded by the care set, which only a person
  grows.

The ETags, the watermark and the care set are written to the watcher's record together, and only
when the whole poll succeeded: a failed read leaves the record as it was, so the next poll sees the
change again. A `state` read that fails is that issue's "nothing" for the poll (the others are still
decided), and the change is left unacknowledged: the old ETags are kept, and once the backoff is over
(RETRY_BASE_S doubling to RETRY_MAX_S, never giving up) the probes are sent without `If-None-Match`,
so that poll is a change poll even if GitHub is back at the cached ETags, and asks every issue in care
once more. A quiet poll, with nothing left unacknowledged, never reads `state`.

**Intake.** An issue comes into care when, after the watcher's start time, the repo's owner or a
push collaborator (never merely the issue's author) posts an unmarked comment on it (`/approve` and
`/changes` are comments), or opens an unmarked issue. Out, whoever posted it: anything carrying an
`sdlc:v1` marker, a plain agent note (`sdlc.is_agent_note`: posted under the owner's login, titled
`> 🤖 **<name>** — ...`), a pull request, everything from before the start (`next`'s list at start
among it) and anyone else's comment or issue. Care holds epics and top-level issues only: a
person's comment on a slice or subtask (labelled `plan:slice` / `plan:subtask`, with the matching
`sdlc:v1 kind=slice|subtask epic=N` marker) brings epic N into care and counts as a person's comment
on it; the slice itself is never asked for a turn. An issue leaves care when `state` says `done`
(or gives a slice's, a subtask's or a debt ticket's shape, which never belongs there). A `state` that
fails is that issue's answer for this poll ("nothing"); the others are still decided.

Stdlib only, and no `twiddle` import, like the rest of the SDLC tooling.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from tools import sdlc   # imported as tools.sdlc_watch_github (the tests)
except ImportError:
    import sdlc              # run beside tools/sdlc_watch.py

GH_TIMEOUT_S = 60          # one gh api request
STATE_TIMEOUT_S = 300      # one `sdlc.py state N --json`: an epic reads all its slices
PER_PAGE = 100
GITHUB_MAX_PER_PAGE = 100
MAX_PAGES = 10             # requests per since-read
GITHUB_HOURLY_LIMIT = 5000  # an authenticated REST client's primary rate limit
PROBES = {   # name -> the conditional request's path, under repos/<repo>/
    "issues": "issues?state=all&sort=updated&direction=desc&per_page=1",
    "comments": "issues/comments?sort=updated&direction=desc&per_page=1",
}
SINCE = {    # name -> the read made only when that probe (or the other) changed
    "issues": "issues?state=all&sort={sort}&direction=asc&per_page={per_page}&since={since}&page={page}",
    "comments": "issues/comments?sort={sort}&direction=asc&per_page={per_page}&since={since}&page={page}",
}
REQUESTS_PER_QUIET_POLL = len(PROBES)
RETRY_BASE_S = 60          # after a poll whose `state` reads failed: wait this long, doubling each time,
RETRY_MAX_S = 3600         # up to this, and ask them all again (never giving up)
STATUS_RE = re.compile(r"\AHTTP/[0-9.]+ ([0-9]{3})\b")
MARKER_ANY_RE = re.compile(r"<!--\s*" + re.escape(sdlc.MARKER_TAG) + r"\b")
CONTAINER_LABELS = {"plan:slice": "slice", "plan:subtask": "subtask"}
ISSUE_NUMBER_RE = re.compile(r"/issues/([1-9][0-9]*)\Z")


class GitHubError(Exception):
    """A read that failed: the poll stops and writes nothing."""


# --- talking to gh ----------------------------------------------------------------------

def run_gh(args: list[str]) -> subprocess.CompletedProcess:
    """One `gh` call, bounded. Every call this module makes goes through here (GETs only)."""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise GitHubError(f"gh {' '.join(args[:3])} timed out after {GH_TIMEOUT_S}s") from None
    except OSError as exc:
        raise GitHubError(f"cannot run gh: {exc}") from None


def parse_response(stdout: str) -> tuple[int, dict[str, str], str]:
    """`gh api -i` output: (status, headers with lower-case names, body). The status line ends LF and
    the headers CRLF, as `gh` prints them; a 304 has no body."""
    head, sep, body = stdout.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    m = STATUS_RE.match(lines[0] if lines else "")
    if not m:
        raise GitHubError(f"gh api printed no HTTP status line: {stdout[:80]!r}")
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    return int(m.group(1)), headers, body if sep else ""


def conditional_get(path: str, etag: str | None) -> tuple[int, str | None, str]:
    """GET with If-None-Match: (200 or 304, the ETag to keep, the body). Any other answer raises."""
    args = ["api", "-i", *(["-H", f"If-None-Match: {etag}"] if etag else []), path]
    proc = run_gh(args)
    status, headers, body = parse_response(proc.stdout or "")
    if status == 304:
        return 304, etag, ""
    if status != 200 or proc.returncode != 0:
        raise GitHubError(f"gh api {path}: HTTP {status} ({(proc.stderr or '').strip()})")
    return 200, headers.get("etag"), body


def get_json(path: str, *flags: str) -> Any:
    proc = run_gh(["api", path, *flags])
    if proc.returncode != 0:
        raise GitHubError(f"gh api {path} failed: {(proc.stderr or '').strip()}")
    try:
        return json.loads(proc.stdout or "null")
    except ValueError as exc:
        raise GitHubError(f"gh api {path} returned no JSON ({exc})") from None


def _page(repo: str, name: str, sort: str, since: str, per_page: int, page: int) -> list[dict[str, Any]]:
    path = f"repos/{repo}/" + SINCE[name].format(sort=sort, per_page=per_page, since=since, page=page)
    got = get_json(path)
    if not isinstance(got, list) or not all(isinstance(it, dict) for it in got):
        raise GitHubError(f"gh api {path}: expected a list of objects")
    return got


def _key(it: dict[str, Any]) -> Any:
    """An item's id (an issue's or a comment's: they rise with creation), else its number."""
    return it["id"] if it.get("id") is not None else it.get("number")


def _overlap_size(pos: int) -> int:
    """A page size (PER_PAGE or less if it can, GitHub's 100 at most) whose page holding position `pos`
    holds at least one position after it too. Only a `pos + 1` that every size up to 100 divides has
    none, which no count of issues or comments reaches."""
    sizes = [*range(PER_PAGE, 1, -1), *range(PER_PAGE + 1, GITHUB_MAX_PER_PAGE + 1)]
    return next((q for q in sizes if pos % q != q - 1), PER_PAGE)


def read_since(repo: str, name: str, mark: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """`name` updated since `mark["at"]`, in at most MAX_PAGES requests: (items, the next mark, whether
    it stopped at the cap). What it guarantees is the intake's: every issue or comment *created* after
    the watermark's start, and still there, is read at some poll (edits are not intake, #370).

    It pages by time, not position: each request asks page 1 again (`sort=updated`), since the newest
    `updated_at` read so far (less a second). Every request is page 1, so nothing can shift past it.

    When a full page sits in one window, so time can't move on, it reads the rest of that set in
    `created_at` order (`sort=created`), anchored: each request's page is chosen to hold the position of
    the last item taken (the mark's `cursor`, at `pos`) and at least one after it, and it is used only if
    its first item is at or before `cursor` (or it starts at the top). One response is one snapshot, so
    the items after `cursor` in it are the next ones created; a deletion or an old item edited into the
    set (created before, so already read or from before the start) can't hide one. A response that
    starts after `cursor` steps back a position. (This replaces round 3's slot check, which a deletion
    and an insertion between two requests could cancel out.) It assumes GitHub orders equal
    `created_at` by id, as ids rise with creation."""
    at, by = mark.get("at") or "", mark.get("by") or "updated"
    cursor, pos = mark.get("cursor"), int(mark.get("pos", -1))
    items: dict[Any, dict[str, Any]] = {}
    for _ in range(MAX_PAGES):
        if by == "updated":
            got = _page(repo, name, "updated", shifted(at), PER_PAGE, 1)
            for it in got:
                items[_key(it)] = it
            if len(got) < PER_PAGE:
                return list(items.values()), {"at": latest(list(items.values()), at)}, False
            newest = latest(got, at)
            if newest and (parse_time(at) is None or parse_time(newest) > parse_time(at)):
                at = newest                   # time moved on: ask again from there
            else:
                by, cursor, pos = "created", None, -1   # a full page in one window: the rest by creation
            continue
        size = PER_PAGE if cursor is None else _overlap_size(pos)
        page = 1 if cursor is None else pos // size + 1
        offset = (page - 1) * size
        got = _page(repo, name, "created", shifted(at), size, page)
        if offset > 0 and (not got or _key(got[0]) > cursor):
            pos = offset - 1                  # the set shrank under the anchor: a position back
            continue
        new = [it for it in got if cursor is None or _key(it) > cursor]
        for it in new:
            items[_key(it)] = it
        if len(got) < size:
            return list(items.values()), {"at": latest(list(items.values()), at)}, False
        if new:
            cursor = max(_key(it) for it in new)
        pos = offset + len(got) - 1
    mark = {"at": at} if by == "updated" else {"at": at, "by": by, "cursor": cursor, "pos": pos}
    return list(items.values()), mark, True


def fetch_trusted(config: dict[str, Any], repo: str) -> set[str]:
    """Who is a person here: the repo's owner and its push collaborators (`sdlc.fetch_trusted`'s rule),
    through this module's bounded `gh`. Unlike that, a failed lookup raises: falling back to the owner
    alone would read past a collaborator's action and move the watermark beyond it, so the poll writes
    nothing and the next one reads it again."""
    pages = get_json(f"repos/{repo}/collaborators", "--paginate", "--slurp")
    rows = [c for page in (pages if isinstance(pages, list) else []) for c in (page if isinstance(page, list) else [])]
    if not isinstance(pages, list) or not all(isinstance(c, dict) for c in rows):
        raise GitHubError("cannot list collaborators: unexpected answer")
    return {c.get("login") for c in rows if (c.get("permissions") or {}).get("push") and c.get("login")} \
        | {repo.split("/")[0]}


# --- pure rules -------------------------------------------------------------------------

def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def is_marked(body: str | None) -> bool:
    """An `sdlc:v1` marker: the leading one `sdlc.py` writes, or one anywhere in an HTML comment."""
    return sdlc.parse_marker(body or "") is not None or bool(MARKER_ANY_RE.search(body or ""))


def labels_of(issue: dict[str, Any]) -> set[str]:
    return {(l.get("name") if isinstance(l, dict) else l) for l in issue.get("labels") or []}


def care_target(issue: dict[str, Any]) -> int | None:
    """The issue a person's comment on `issue` brings into care: itself, or a slice's or subtask's
    epic. None for a pull request, or a slice/subtask whose marker doesn't name its epic."""
    if issue.get("pull_request"):
        return None
    labels = labels_of(issue)
    kinds = {CONTAINER_LABELS[l] for l in labels if l in CONTAINER_LABELS}
    if kinds:
        mk = sdlc.parse_marker(issue.get("body") or "") or {}
        epic = mk.get("epic", "")
        if mk.get("kind") in kinds and epic.isdigit() and int(epic) > 0:
            return int(epic)
        return None   # labelled as a plan's issue but no matching marker: never a top-level issue
    n = issue.get("number")
    return n if isinstance(n, int) and n > 0 else None


def issue_number(comment: dict[str, Any]) -> int | None:
    m = ISSUE_NUMBER_RE.search(comment.get("issue_url") or "")
    return int(m.group(1)) if m else None


def login(item: dict[str, Any]) -> str:
    return (item.get("user") or {}).get("login") or ""


def person_acts(issues: list[dict[str, Any]], comments: list[dict[str, Any]], trusted: set[str],
                started: dt.datetime | None,
                lookup: Callable[[int], dict[str, Any] | None]) -> dict[int, dict[str, Any]]:
    """The issues a person's action after `started` brings into care: {target: {"at", "why"}}, the latest
    action per target. `lookup(n)` gives the issue a comment is on (from this poll's reads, else GitHub)."""
    if started is None:
        return {}
    acts: dict[int, dict[str, Any]] = {}

    def add(target: int, at: dt.datetime, why: str) -> None:
        if target not in acts or at > acts[target]["at"]:
            acts[target] = {"at": at, "why": why}

    for issue in issues:   # a new issue a person opened
        at = parse_time(issue.get("created_at"))
        if (at is None or at <= started or issue.get("pull_request") or login(issue) not in trusted
                or is_marked(issue.get("body")) or sdlc.is_agent_note(issue.get("body"))):
            continue
        if labels_of(issue) & set(CONTAINER_LABELS):
            continue
        add(issue["number"], at, f"new issue by {login(issue)}")
    for c in comments:     # a comment, `/approve` or `/changes` a person posted
        at = parse_time(c.get("created_at"))
        n = issue_number(c)
        if (at is None or at <= started or n is None or login(c) not in trusted
                or is_marked(c.get("body")) or sdlc.is_agent_note(c.get("body"))):
            continue
        issue = lookup(n)
        target = care_target(issue) if issue else None
        if target is None:
            continue
        keyword = next((f"/{a}" for a, _ in sdlc.keyword_lines(c.get("body") or "")), "a comment")
        add(target, at, f"{keyword} by {login(c)}" + (f" on #{n}" if target != n else ""))
    return acts


def latest(items: list[dict[str, Any]], current: str | None) -> str | None:
    """The newest `updated_at` among `items`, or `current`: the next since-read's watermark."""
    best = parse_time(current)
    for it in items:
        t = parse_time(it.get("updated_at"))
        if t and (best is None or t > best):
            best = t
    return best.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if best else current


def shifted(mark: str) -> str:
    """A since-read's `since`: the watermark less a second, so an update in the watermark's own second,
    after the last read, is never missed (re-reading an item is harmless: intake is idempotent)."""
    t = parse_time(mark)
    return (t - dt.timedelta(seconds=1)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else mark


# --- whose move ---------------------------------------------------------------------------

def run_state(checkout: Path, n: int) -> dict[str, Any]:
    """`sdlc.py state N --json`, from the running checkout: the turn decision is never the watcher's."""
    cmd = [sys.executable, str(checkout / "tools" / "sdlc.py"), "state", str(n), "--json"]
    try:
        proc = subprocess.run(cmd, cwd=checkout, capture_output=True, text=True, timeout=STATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise GitHubError(f"sdlc.py state {n} timed out after {STATE_TIMEOUT_S}s") from None
    if proc.returncode != 0:
        raise GitHubError(f"sdlc.py state {n} failed: {(proc.stderr or '').strip()[-300:]}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise GitHubError(f"sdlc.py state {n} printed no JSON ({exc})") from None


def decide(n: int, st: dict[str, Any], paused: bool) -> dict[str, Any]:
    """What a poll would do for one issue in care, from its `state` alone."""
    if "turn" not in st or st.get("plan_kind"):   # a slice's `slice-status` shape, a subtask, a debt ticket
        return {"issue": n, "leave": True, "why": "not an epic or a top-level issue"}
    if st.get("state") == "done":
        return {"issue": n, "leave": True, "why": "done"}
    if st.get("turn") != "agent":
        return {"issue": n, "run": None, "why": f"{st.get('turn')}'s move ({st.get('action')})"}
    if paused:
        return {"issue": n, "run": None, "why": "paused"}
    return {"issue": n, "run": st.get("next") or "", "why": f"agent's move ({st.get('action')})"}


# --- one poll -----------------------------------------------------------------------------

def check(record: dict[str, Any], repo: str) -> dict[str, Any]:
    """The probes alone: {"requests", "changed": [names], "etags": new etags}. Reads nothing else.

    When a `state_retry` is due, the probes go without `If-None-Match`: a 200 is then certain, so the
    retry is a change poll even if both probes are back at their cached ETags (an issue deleted, say)."""
    github = record.get("github") or {}
    old = github.get("etags") or {}
    due = parse_time((github.get("state_retry") or {}).get("at"))
    fresh = due is not None and now() >= due
    etags, changed = dict(old), []
    for name, path in PROBES.items():
        status, etag, _ = conditional_get(f"repos/{repo}/{path}", None if fresh else old.get(name))
        if status == 200:
            changed.append(name)
            etags[name] = etag
    return {"requests": len(PROBES), "changed": changed, "etags": etags}


def decide_all(care: dict[str, Any], keys: list[str], state: Callable[[int], dict[str, Any]],
               is_paused: Callable[[int], bool], out: dict[str, Any]) -> None:
    """Ask `state` about each of `keys` (in care) and record the answer in `out`, in place. A failed read is
    that issue's "nothing" this poll, and counted in `out["failed"]`: the poll keeps its ETags unacknowledged,
    so a later poll is a change poll again and asks once more (`poll`). One failure never stops the others."""
    for key in sorted(keys, key=int):
        try:
            st = state(int(key))
        except GitHubError as exc:
            out["failed"].append(int(key))
            out["decisions"].append({"issue": int(key), "run": None, "why": f"state failed ({exc})"})
            continue
        d = decide(int(key), st, is_paused(int(key)))
        if d.get("leave"):
            out["left"][int(key)] = d["why"]
            care.pop(key)
        else:
            out["decisions"].append(d)


def poll(record: dict[str, Any], repo: str, config: dict[str, Any], checkout: Path,
         is_paused: Callable[[int], bool],
         state: Callable[[int], dict[str, Any]] | None = None) -> dict[str, Any]:
    """One poll, read-only on GitHub. Returns what to write back to the record (`github`, `care`) and
    what it found; the caller writes it only when this returns (a raise means write nothing)."""
    state = state or (lambda n: run_state(checkout, n))
    github = dict(record.get("github") or {})
    probe = check(record, repo)
    out: dict[str, Any] = {"requests": probe["requests"], "changed": probe["changed"],
                           "github": github, "care": record.get("care") or {}, "joined": {}, "left": {},
                           "decisions": [], "truncated": [], "failed": [], "retry_at": None}
    care = {k: dict(v) for k, v in (record.get("care") or {}).items()}
    if not probe["changed"]:   # nothing new on GitHub, and nothing left unprocessed: no read, no state
        return out
    started = parse_time(record.get("started_at"))
    marks = dict(github.get("since") or {})
    reads: dict[str, list[dict[str, Any]]] = {}
    for name in SINCE:   # both, whichever probe moved: a comment's issue is in the issues read
        mark = marks.get(name) if isinstance(marks.get(name), dict) else {}
        mark = {**mark, "at": mark.get("at") or record.get("started_at") or ""}
        reads[name], marks[name], cut = read_since(repo, name, mark)
        if cut:
            out["truncated"].append(name)
    trusted = fetch_trusted(config, repo)
    by_number = {i["number"]: i for i in reads["issues"] if isinstance(i.get("number"), int)}

    def lookup(n: int) -> dict[str, Any] | None:
        if n not in by_number:
            got = get_json(f"repos/{repo}/issues/{n}")
            by_number[n] = got if isinstance(got, dict) else {}
        return by_number[n] or None

    acts = person_acts(reads["issues"], reads["comments"], trusted, started, lookup)
    for n, act in acts.items():
        at = act["at"].astimezone(dt.timezone.utc).isoformat(timespec="seconds")
        entry = care.setdefault(str(n), {"since": at, "why": act["why"]})
        if str(n) not in (record.get("care") or {}):
            out["joined"][n] = act["why"]
        prev = parse_time(entry.get("last_person_at"))
        if prev is None or act["at"] > prev:
            entry["last_person_at"], entry["last_person_why"] = at, act["why"]
    retry = github.get("state_retry") or {}
    due = parse_time(retry.get("at"))
    if due and now() < due:   # state reads failed lately: take in what people did, ask state when it's time
        out["retry_at"] = retry["at"]
        out["decisions"] = [{"issue": int(k), "run": None, "why": f"state reads wait until {retry['at']}"}
                            for k in sorted(care, key=int)]
    else:
        decide_all(care, list(care), state, is_paused, out)
    if out["failed"]:
        n = int(retry.get("failures") or 0) + 1
        wait = min(RETRY_BASE_S * 2 ** (n - 1), RETRY_MAX_S)
        out["retry_at"] = (now() + dt.timedelta(seconds=wait)).strftime("%Y-%m-%dT%H:%M:%SZ")
        retry = {"failures": n, "at": out["retry_at"]}
    elif not out["retry_at"]:
        retry = {}
    etags = dict(probe["etags"])
    if retry:   # not done with this change: send the old ETags, so the next poll is a change poll again
        etags = dict(github.get("etags") or {})
    for name in out["truncated"]:   # carry on next poll: keep the old ETag so it reads again
        old = (github.get("etags") or {}).get(name)
        if old is None:
            etags.pop(name, None)
        else:
            etags[name] = old
    out["github"] = {**{k: v for k, v in github.items() if k != "state_retry"}, "etags": etags, "since": marks,
                     **({"state_retry": retry} if retry else {})}
    out["care"] = care
    return out


def _decision_lines(out: dict[str, Any]) -> list[str]:
    lines = [f"  left care: #{n} ({why})" for n, why in sorted(out["left"].items())]
    for d in out["decisions"]:
        if d.get("run") is not None:
            lines.append(f"  #{d['issue']}: {d['why']}: would run `{d['run']}`")
        else:
            lines.append(f"  #{d['issue']}: {d['why']}: nothing")
    return lines


def render(out: dict[str, Any], dry_run: bool = True) -> str:
    head = "poll (dry run: starts nothing)" if dry_run else "poll"
    if not out["changed"]:
        return f"{head}: {out['requests']} conditional request(s), nothing changed: no state read, nothing to run"
    lines = [f"{head}: {out['requests']} conditional request(s); changed: {', '.join(out['changed'])}"]
    if out["truncated"]:
        lines.append(f"  read capped at {MAX_PAGES} pages ({', '.join(out['truncated'])}): the next poll carries on")
    for n, why in sorted(out["joined"].items()):
        lines.append(f"  into care: #{n} ({why})")
    lines += _decision_lines(out)
    if out.get("retry_at"):
        lines.append(f"  state reads failed or wait: this change stays unacknowledged; asked again from {out['retry_at']}")
    if not out["decisions"] and not out["left"]:
        lines.append("  nothing in care")
    return "\n".join(lines)
