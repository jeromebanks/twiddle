#!/usr/bin/env python3
"""The deterministic half of twiddle's issue SDLC (stdlib only, no twiddle imports).

Skills do the thinking and writing; this script does everything that must not
drift: which state an issue is in, whose move it is, whether a sign-off counts,
and every label change and agent comment. Config is `.sdlc/config.json`.

    uv run python tools/sdlc.py bootstrap-labels
    uv run python tools/sdlc.py state 12
    uv run python tools/sdlc.py next
    uv run python tools/sdlc.py transition 12 prd-review --kind prd --body-file prd.md --dry-run
    uv run python tools/sdlc.py reconcile 12
    uv run python tools/sdlc.py plan-validate plan.json
    uv run python tools/sdlc.py plan-post 12 plan.json --dry-run
    uv run python tools/sdlc.py plan-review 12 --plan plan.json --report codex.md --response response.md
    uv run python tools/sdlc.py plan-create 12 --dry-run
    uv run python tools/sdlc.py ready --epic 12
    uv run python tools/sdlc.py slice-check 28 && uv run python tools/sdlc.py claim 28
    uv run python tools/sdlc.py test-record 50 && uv run python tools/sdlc.py pr-review 50 --report codex.md
    uv run python tools/sdlc.py merge 50 && uv run python tools/sdlc.py cleanup 28

Two rules hold the process together (see SDLC.md):

* The agent posts through `gh` as the maintainer, so the login cannot tell its
  comments from a human's. Every agent comment starts with a marker,
  `<!-- sdlc:v1 kind=prd rev=2 -->`. A *reply* is an unmarked comment newer
  than the latest marked one; a marker only counts when its author can write
  to the repo.
* A sign-off binds to one revision. `/approve` (at the start of a line, outside
  quotes and code, from the issue author or a collaborator) approves the latest
  PRD/diagnosis revision only; posting a newer revision voids it.

Planning (after `approved`) needs no human: a plan revision is reviewed by Codex
until it says `VERDICT: approve` (consensus), and only then are the epic's
sub-issues created. A plan revision is not a PRD revision, so it never voids
the PRD's sign-off. Without consensus in `max_plan_rounds`, the agent asks the
poster a plain-language question (`kind=question phase=plan`).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / ".sdlc" / "config.json"

PREFIX = "sdlc:"
MARKER_TAG = "sdlc:v1"
MARKER_RE = re.compile(r"\A\s*<!--\s*sdlc:v1\s+([^>]*?)\s*-->")
ATTR_RE = re.compile(r"(\w+)=(\S+)")
KEYWORD_RE = re.compile(r"^ {0,3}/(approve|changes)\b[ \t]*(.*)$", re.IGNORECASE)

DOC_KINDS = {"prd", "diagnosis"}
ROUND_KINDS = {"question", "prd", "diagnosis"}
KINDS = {"question", "prd", "diagnosis", "approval", "escalation", "note"}
# The state each kind of agent comment moves the issue to (note: any allowed one).
KIND_TARGET = {"question": "needs-info", "prd": "prd-review", "diagnosis": "diagnosis-review",
               "approval": "approved", "escalation": "escalated", "note": None}
REVIEW_STATES = {"needs-info", "prd-review", "diagnosis-review"}
REVIEW_DOC = {"prd-review": "prd", "diagnosis-review": "diagnosis"}
LATER_STATES = {"demo-review", "done"}
BUILD_STATES = {"planned", "in-progress"}     # the epic's slices are being built (`work-slice`)
TRIAGE_ACTIONS = {"triage", "respond_to_reply", "record_approval", "reconcile_label"}
PLAN_ACTIONS = {"plan", "continue_plan", "create_plan_issues", "ask_poster", "replan"}
# Planning comments: posted by plan-post / plan-review / plan-create, never by `transition`.
PLAN_KINDS = {"plan", "plan-review", "plan-created"}
# from-state -> states the triage skill may move to. `escalated` is open from anywhere.
TRANSITIONS = {
    "untriaged": {"triage", "needs-info", "prd-review", "diagnosis-review"},
    "triage": {"triage", "needs-info", "prd-review", "diagnosis-review"},
    "needs-info": {"needs-info", "prd-review", "diagnosis-review"},
    "prd-review": {"prd-review", "needs-info", "approved"},
    "diagnosis-review": {"diagnosis-review", "needs-info", "approved"},
    "approved": {"prd-review", "diagnosis-review",    # a later revision reopens review
                 "needs-info"},                       # planning can't reach consensus: ask the poster
    "escalated": {"triage"},                           # a human releases it
}


class SdlcError(Exception):
    pass


# --- config ---------------------------------------------------------------

def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SdlcError(f"cannot read config {path}: {exc}")


def state_labels(config: dict[str, Any]) -> list[str]:
    return [l["name"] for l in config["labels"]]


# --- pure state derivation -------------------------------------------------

def parse_marker(body: str) -> dict[str, str] | None:
    m = MARKER_RE.match(body or "")
    return dict(ATTR_RE.findall(m.group(1))) if m else None


def keyword_lines(body: str) -> list[tuple[str, str]]:
    """`/approve` / `/changes <text>` lines, ignoring quotes, fences and indented code."""
    found, fenced = [], False
    for line in (body or "").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or line.startswith(">"):
            continue
        m = KEYWORD_RE.match(line)
        if m:
            found.append((m.group(1).lower(), m.group(2).strip()))
    return found


def derive_state(issue: dict[str, Any], comments: list[dict[str, Any]],
                 trusted: set[str], config: dict[str, Any]) -> dict[str, Any]:
    """Everything the skill needs to know about one issue, from fetched JSON alone.

    `trusted` is who can write to the repo: their markers count, and (with the
    issue author) their keywords count.
    """
    plan_kind = next((k for l, k in (("plan:slice", "slice"), ("plan:subtask", "subtask"))
                      if l in issue.get("labels", [])), None)
    if plan_kind:
        # made by plan-create: built by work-slice (`slice-status`), never triaged
        return {"number": issue.get("number"), "title": issue.get("title"), "state": plan_kind, "turn": "none",
                "action": "none", "plan_kind": plan_kind, "conflicts": []}
    all_labels = state_labels(config)
    found = [l for l in issue.get("labels", []) if l in all_labels]
    conflicts: list[str] = []
    stale: list[str] = []
    approved_label = f"{PREFIX}approved"
    if approved_label in found and len(found) == 2 and any(l[len(PREFIX):] in REVIEW_DOC for l in found):
        # a maintainer ticked sdlc:approved in the GitHub UI and left the review label behind
        stale = [l for l in found if l != approved_label]
        state = "approved"
    elif len(found) > 1:
        conflicts.append("multiple sdlc:* labels: " + ", ".join(sorted(found)))
        state = "conflict"
    else:
        state = found[0][len(PREFIX):] if found else "untriaged"

    ordered = sorted(comments, key=lambda c: (c.get("created_at", ""), c.get("id", 0)))
    latest_doc: dict[str, Any] | None = None
    approved_rev: int | None = None
    approval_by: str | None = None
    last_marked = -1
    rounds = 0
    marked_ids: set[Any] = set()
    latest_plan: dict[str, Any] | None = None
    plan_verdict: str | None = None
    plan_rounds = 0
    for i, c in enumerate(ordered):
        mk = parse_marker(c.get("body", "")) if c.get("author") in trusted else None
        if not mk:
            continue
        marked_ids.add(c.get("id"))
        last_marked = i
        kind = mk.get("kind", "")
        rev = int(mk["rev"]) if mk.get("rev", "").isdigit() else None
        if kind in ROUND_KINDS and mk.get("phase") != "plan":
            rounds += 1
        if kind == "question" and mk.get("phase") == "plan":
            plan_rounds, plan_verdict = 0, None   # the poster's answer buys a fresh set of Codex rounds
        if kind in DOC_KINDS and rev is not None:
            latest_doc = {"kind": kind, "rev": rev, "id": c.get("id"), "url": c.get("url")}
            approved_rev = None
        elif kind == "approval" and latest_doc and rev == latest_doc["rev"]:
            approved_rev, approval_by = rev, mk.get("by")
        elif kind == "plan" and rev is not None:
            latest_plan = {"rev": rev, "id": c.get("id"), "url": c.get("url")}
            plan_verdict = None
        elif kind == "plan-review":
            plan_rounds += 1
            if latest_plan and rev == latest_plan["rev"]:
                plan_verdict = mk.get("verdict")

    replies = [c for c in ordered[last_marked + 1:] if c.get("id") not in marked_ids]
    allowed = trusted | {issue.get("author", "")}
    decision: dict[str, Any] | None = None
    ignored: list[dict[str, str]] = []
    for c in replies:
        for action, text in keyword_lines(c.get("body", "")):
            if c.get("author") in allowed:
                decision = {"action": action, "by": c.get("author"), "text": text, "comment_id": c.get("id")}
            else:
                ignored.append({"by": c.get("author", ""), "action": action})
    if decision and decision["action"] == "approve":
        ok_state = REVIEW_DOC.get(state) is not None and latest_doc and latest_doc["kind"] == REVIEW_DOC[state]
        decision["valid"] = bool(ok_state)

    doc_approved = bool(latest_doc) and approved_rev == latest_doc["rev"]
    max_plan_rounds = config.get("max_plan_rounds", 3)
    reconcile = None
    if state in REVIEW_DOC and latest_doc and latest_doc["kind"] != REVIEW_DOC[state]:
        conflicts.append(f"label {PREFIX}{state} but the latest revision is a {latest_doc['kind']}")
    if state == "approved":
        if not latest_doc:
            conflicts.append("sdlc:approved but there is no PRD or diagnosis to approve")
        elif approved_rev != latest_doc["rev"]:
            reconcile = "label_approval"
        elif stale:
            reconcile = "stale_labels"
    elif approved_rev is not None and state in REVIEW_DOC:
        conflicts.append(f"rev {approved_rev} is approved on record but the label is {PREFIX}{state}")

    if issue.get("state", "open").lower() == "closed":
        turn, action = "none", "none"
    elif conflicts:
        turn, action = "human", "fix_conflict"
    elif state == "escalated":
        turn, action = "human", "human"
    elif state in LATER_STATES:
        turn, action = "later", "none"
    elif state in BUILD_STATES:
        turn, action = "agent", "work_slices"   # whether a slice is ready needs GitHub: see epic_progress
    elif state in ("untriaged", "triage"):
        turn, action = "agent", "triage"
    elif state == "approved":
        turn = "agent"
        if reconcile:
            action = "reconcile_label"
        elif latest_plan is None:
            action = "plan"
        elif plan_verdict == "approve":
            action = "create_plan_issues"
        elif plan_rounds >= max_plan_rounds:
            action = "ask_poster"
        else:
            action = "continue_plan"
    elif replies:
        turn = "agent"
        if state == "needs-info" and doc_approved:
            action = "replan"
        elif decision and decision["action"] == "approve" and decision.get("valid"):
            action = "record_approval"
        else:
            action = "respond_to_reply"
    else:
        turn, action = "poster", "wait_for_poster"

    type_label = next((t for t in ("bug", "enhancement") if t in issue.get("labels", [])), None)
    return {
        "number": issue.get("number"), "title": issue.get("title"), "state": state, "turn": turn,
        "action": action, "type": type_label, "latest_doc": latest_doc, "approved_rev": approved_rev,
        "approval_by": approval_by, "rounds": rounds, "max_rounds": config.get("max_rounds", 5),
        "replies": [{"id": c.get("id"), "by": c.get("author"), "url": c.get("url")} for c in replies],
        "decision": decision, "ignored_keywords": ignored, "reconcile": reconcile, "stale_labels": stale,
        "conflicts": conflicts, "doc_approved": doc_approved, "latest_plan": latest_plan,
        "plan_verdict": plan_verdict, "plan_rounds": plan_rounds, "max_plan_rounds": max_plan_rounds,
    }


def next_command(st: dict[str, Any], progress: dict[str, Any] | None = None) -> str:
    """The one thing to run next for this issue, so no session has to remember the workflow.

    `progress` (from epic_progress) is needed for an epic whose slices are being built.
    """
    n, a = st["number"], st["action"]
    if st.get("plan_kind"):
        return f"`uv run python tools/sdlc.py slice-status {n}`"
    if a in TRIAGE_ACTIONS:
        return f"/triage-issue {n}"
    if a in PLAN_ACTIONS:
        return f"/plan-issue {n}"
    if a == "wait_for_poster":
        return f"nothing: waiting on the poster to reply on #{n}"
    if a in ("human", "fix_conflict"):
        return f"nothing for an agent: #{n} needs a human" + (f" ({'; '.join(st['conflicts'])})" if st["conflicts"] else "")
    if a == "work_slices":
        if progress is None:
            return f"`uv run python tools/sdlc.py state {n}` (needs the slices' progress)"
        if progress["ready"]:
            return f"/work-slice {progress['ready'][0]}"
        if progress["in_flight"]:
            return ("nothing new to start: in progress " + ", ".join(f"#{x}" for x in progress["in_flight"])
                    + f" (`/work-slice {progress['in_flight'][0]}` resumes one)")
        if progress["open"] == 0:
            return f"/milestone-demo {n} (not built yet): every unit of work is merged"
        if progress["escalated"]:
            return "nothing for an agent: escalated " + ", ".join(f"#{x}" for x in progress["escalated"])
        return f"nothing ready: the open units of work on #{n} are all blocked"
    if st["state"] in LATER_STATES:
        return f"nothing yet: #{n} is {st['state']} (that stage's skill isn't built yet)"
    return "nothing: the issue is closed" if a == "none" else f"? (action {a})"


def check_transition(st: dict[str, Any], to: str, kind: str, config: dict[str, Any]) -> list[str]:
    """Reasons a transition is not allowed (empty list = fine)."""
    errs: list[str] = []
    if st.get("plan_kind"):
        return [f"#{st['number']} is a plan:{st['plan_kind']} issue: work-slice drives it, not transition"]
    if f"{PREFIX}{to}" not in state_labels(config):
        errs.append(f"unknown state {to!r}")
        return errs
    if st["conflicts"]:
        errs.append("fix conflicts first: " + "; ".join(st["conflicts"]))
    if kind not in KINDS:
        errs.append(f"unknown kind {kind!r}")
    want = KIND_TARGET.get(kind)
    if want and want != to:
        errs.append(f"kind {kind} moves to {want}, not {to}")
    if kind == "note" and to not in (st["state"], "triage"):
        errs.append("a note keeps the current state (or starts triage); post a question, prd or diagnosis to move on")
    if to != "escalated" and to not in TRANSITIONS.get(st["state"], set()):
        errs.append(f"{st['state']} -> {to} is not an allowed transition")
    if to == "approved":
        d = st["decision"]
        if not (d and d["action"] == "approve" and d.get("valid")):
            errs.append("no valid /approve from the issue author or a collaborator on the latest revision")
    if (to not in ("escalated", "approved") and st["rounds"] >= st["max_rounds"] and kind in ROUND_KINDS
            and not st.get("doc_approved")):   # planning questions have their own budget
        errs.append(f"round budget spent ({st['rounds']}/{st['max_rounds']}): escalate to a human")
    return errs


# --- rendering -------------------------------------------------------------

def marker(kind: str, rev: int | None = None, **extra: str) -> str:
    attrs = [f"kind={kind}"] + ([f"rev={rev}"] if rev is not None else []) + [f"{k}={v}" for k, v in extra.items()]
    return f"<!-- {MARKER_TAG} {' '.join(attrs)} -->"


HEADERS = {
    "question": "questions",
    "prd": "PRD rev {rev}",
    "diagnosis": "diagnosis rev {rev}",
    "approval": "approval recorded",
    "escalation": "needs a human",
    "note": "note",
    "plan": "plan rev {rev}",
    "plan-review": "Codex review of plan rev {rev}",
    "plan-created": "plan created",
    "claim": "claimed",
    "release": "released",
    "tests": "test run",
    "pr-review": "Codex review",
}
FOOTERS = {
    "question": "Reply in a comment. Anything you leave unanswered, I will assume the stated default.",
    "prd": "Reply `/approve` to sign off on this revision, `/changes <what>` to ask for changes, or just comment.",
    "diagnosis": "Reply `/approve` to sign off on this diagnosis, `/changes <what>` to ask for changes, or just comment.",
    "plan": ("Nothing for you to sign off: Claude and Codex review this plan until they agree. "
             "If they can't, I'll ask you here in plain terms."),
}


def render_comment(kind: str, rev: int | None, body: str, config: dict[str, Any], **extra: str) -> str:
    head = f"> 🤖 **{config.get('agent_name', 'sdlc')}** — {HEADERS[kind].format(rev=rev)}"
    foot = FOOTERS.get(kind)
    return "\n".join([marker(kind, rev, **extra), head, "", body.strip(), "", f"---\n{foot}" if foot else ""]).rstrip() + "\n"


def render_superseded(old_body: str, new_rev: int, new_url: str) -> str:
    mk = parse_marker(old_body) or {}
    kind, rev = mk.get("kind", "prd"), int(mk.get("rev", 0) or 0)
    inner = MARKER_RE.sub("", old_body, count=1).strip()
    return (f"{marker(kind, rev, superseded=new_rev)}\n> Superseded by [rev {new_rev}]({new_url}).\n\n"
            f"<details><summary>{kind} rev {rev} (superseded)</summary>\n\n{inner}\n\n</details>\n")


# --- GitHub access ---------------------------------------------------------

def gh(args: list[str], *, check: bool = True) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SdlcError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout


def gh_json(args: list[str]) -> Any:
    return json.loads(gh(args) or "null")


def gh_pages(path: str) -> list[Any]:
    pages = gh_json(["api", path, "--paginate", "--slurp"]) or []
    return [item for page in pages for item in (page if isinstance(page, list) else [page])]


def repo_of(config: dict[str, Any]) -> str:
    return config.get("repository") or gh_json(["repo", "view", "--json", "nameWithOwner"])["nameWithOwner"]


def fetch_trusted(config: dict[str, Any]) -> set[str]:
    repo = repo_of(config)
    try:
        return {c["login"] for c in gh_pages(f"repos/{repo}/collaborators") if (c.get("permissions") or {}).get("push")}
    except SdlcError as exc:
        print(f"warning: cannot list collaborators ({exc}); only the repo owner is trusted", file=sys.stderr)
        return {repo.split("/")[0]}


def norm_issue(raw: dict[str, Any]) -> dict[str, Any]:
    return {"number": raw["number"], "title": raw.get("title", ""), "state": raw.get("state", "open"),
            "author": (raw.get("author") or {}).get("login", ""), "createdAt": raw.get("createdAt", ""),
            "labels": [l["name"] for l in raw.get("labels", [])]}


def norm_comment(raw: dict[str, Any]) -> dict[str, Any]:
    return {"id": raw["id"], "author": (raw.get("user") or {}).get("login", ""), "body": raw.get("body", ""),
            "created_at": raw.get("created_at", ""), "url": raw.get("html_url", "")}


def fetch_bundle(n: int, config: dict[str, Any], trusted: set[str] | None = None) -> dict[str, Any]:
    repo = repo_of(config)
    issue = norm_issue(gh_json(["issue", "view", str(n), "--repo", repo, "--json",
                                "number,title,state,author,labels,createdAt"]))
    comments = [norm_comment(c) for c in gh_pages(f"repos/{repo}/issues/{n}/comments")]
    return {"issue": issue, "comments": comments,
            "trusted": sorted(trusted if trusted is not None else fetch_trusted(config))}


def load_bundle(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    if getattr(args, "from_file", None):
        return json.loads(Path(args.from_file).read_text())
    return fetch_bundle(args.number, config)


def bundle_state(bundle: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    return derive_state(bundle["issue"], bundle["comments"], set(bundle["trusted"]), config)


def post_comment(n: int, body: str, config: dict[str, Any]) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".md") as fh:
        fh.write(body)
        fh.flush()
        return gh_json(["api", "-X", "POST", f"repos/{repo_of(config)}/issues/{n}/comments", "-F", f"body=@{fh.name}"])


def patch_comment(comment_id: int, body: str, config: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md") as fh:
        fh.write(body)
        fh.flush()
        gh(["api", "-X", "PATCH", f"repos/{repo_of(config)}/issues/comments/{comment_id}", "-F", f"body=@{fh.name}"])


def set_state_label(n: int, current: list[str], to: str, config: dict[str, Any]) -> None:
    args = ["issue", "edit", str(n), "--repo", repo_of(config), "--add-label", f"{PREFIX}{to}"]
    for old in current:
        if old in state_labels(config) and old != f"{PREFIX}{to}":
            args += ["--remove-label", old]
    gh(args)


# --- commands --------------------------------------------------------------

def print_state(st: dict[str, Any]) -> None:
    if st.get("plan_kind"):
        print(f"#{st['number']} {st['title']}\n  a plan:{st['plan_kind']} issue\n  next: {st.get('next')}")
        return
    doc = st["latest_doc"]
    print(f"#{st['number']} {st['title']}")
    print(f"  state: {st['state']}   turn: {st['turn']}   action: {st['action']}   type: {st['type'] or '?'}")
    print(f"  latest revision: {('%s rev %s' % (doc['kind'], doc['rev'])) if doc else 'none'}"
          f"   approved: {st['approved_rev'] or 'no'}   rounds: {st['rounds']}/{st['max_rounds']}")
    if st["replies"]:
        print("  replies since the last agent comment: " + ", ".join(f"{r['by']} ({r['url']})" for r in st["replies"]))
    if st["decision"]:
        d = st["decision"]
        print(f"  decision: /{d['action']} by {d['by']}" + ("" if d.get("valid", True) else " (not valid in this state)")
              + (f": {d['text']}" if d["text"] else ""))
    for ig in st["ignored_keywords"]:
        print(f"  ignored /{ig['action']} from {ig['by']} (not the issue author or a collaborator)")
    for c in st["conflicts"]:
        print(f"  CONFLICT: {c}")
    if st["reconcile"]:
        print(f"  needs reconcile ({st['reconcile']}): run `reconcile`")
    if st.get("latest_plan"):
        print(f"  plan: rev {st['latest_plan']['rev']}   codex: {st['plan_verdict'] or 'not yet'}"
              f"   plan rounds: {st['plan_rounds']}/{st['max_plan_rounds']}")
    for m in (st.get("progress") or {}).get("milestones", []):
        print(f"  milestone {m['title']}: {m['done']}/{m['total']} merged" + ("  (complete)" if m["done"] == m["total"] else ""))
    print(f"  next: {st.get('next')}")


def command_bootstrap_labels(args: argparse.Namespace, config: dict[str, Any]) -> int:
    repo = repo_of(config)
    for l in config["labels"] + config.get("plan_labels", []):
        cmd = ["label", "create", l["name"], "--repo", repo, "--color", l["color"],
               "--description", l["description"], "--force"]
        if args.dry_run:
            print("would run: gh " + " ".join(cmd))
        else:
            gh(cmd)
            print(f"label {l['name']}")
    return 0


def with_next(st: dict[str, Any], config: dict[str, Any], offline: bool = False,
              trusted: set[str] | None = None) -> dict[str, Any]:
    """`state` plus the command to run next (an epic being built looks at its slices on GitHub)."""
    progress = None
    if st["action"] == "work_slices" and not offline:
        progress = epic_progress(st["number"], config, trusted if trusted is not None else fetch_trusted(config))
    return {**st, "progress": progress, "next": next_command(st, progress)}


def command_state(args: argparse.Namespace, config: dict[str, Any]) -> int:
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    if st.get("plan_kind") == "slice" and not getattr(args, "from_file", None):
        return command_slice_status(argparse.Namespace(number=st["number"], json=args.json, from_file=None), config)
    st = with_next(st, config, offline=bool(getattr(args, "from_file", None)), trusted=set(bundle["trusted"]))
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        print_state(st)
    return 0


def command_next(args: argparse.Namespace, config: dict[str, Any]) -> int:
    repo = repo_of(config)
    issues = gh_json(["issue", "list", "--repo", repo, "--state", "open", "--limit", "200", "--json",
                      "number,title,state,author,labels,createdAt"])
    trusted = fetch_trusted(config)
    out = []
    for raw in sorted(issues, key=lambda i: i["number"]):
        if any(l["name"] in ("plan:slice", "plan:subtask") for l in raw.get("labels", [])):
            continue   # an epic's units of work: reached through the epic's `next`
        st = bundle_state(fetch_bundle(raw["number"], config, trusted), config)
        if st["turn"] == "agent":
            out.append(with_next(st, config, trusted=trusted))
    if args.json:
        print(json.dumps(out, indent=2))
    elif not out:
        print("nothing waiting on the agent")
    else:
        for st in out:
            print(f"#{st['number']:<4} {st['state']:<17} {st['title']}\n      next: {st['next']}")
    return 0


def command_transition(args: argparse.Namespace, config: dict[str, Any]) -> int:
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    kind = args.kind
    errs = check_transition(st, args.to, kind, config)
    doc = st["latest_doc"]
    if kind in DOC_KINDS:
        rev = (doc["rev"] if doc else 0) + 1
        if args.rev and args.rev != rev:
            errs.append(f"next revision is {rev}, not {args.rev}")
    elif kind == "approval":
        rev = doc["rev"] if doc else None
    else:
        rev = None
    if errs:
        raise SdlcError("; ".join(errs))
    if args.body_file:
        body = Path(args.body_file).read_text()
    elif kind == "approval":
        body = (f"{doc['kind'].upper()} rev {rev} approved by @{st['decision']['by']}. "
                "Next: planning (`plan-issue`).")
    elif kind == "note" and args.to == "triage":
        body = "Triaging this now: reading the issue and the code it touches."
    elif kind == "escalation":
        body = "Handing this to a human: " + (args.reason or "no consensus or the agent is stuck.")
    else:
        raise SdlcError("--body-file is required")
    extra = {"by": st["decision"]["by"], "via": "comment"} if kind == "approval" else {}
    if kind == "question" and st.get("doc_approved"):
        extra = {"phase": "plan"}
    comment = render_comment(kind, rev, body, config, **extra)
    current = [l for l in bundle["issue"]["labels"]]
    if args.dry_run:
        print(f"DRY RUN #{st['number']}: {st['state']} -> {args.to}")
        print(f"labels: add {PREFIX}{args.to}, remove {[l for l in current if l in state_labels(config) and l != PREFIX + args.to]}")
        print("----- comment -----")
        print(comment)
        return 0
    n = st["number"]
    have = {l["name"] for l in gh_json(["label", "list", "--repo", repo_of(config), "--limit", "200", "--json", "name"])}
    if f"{PREFIX}{args.to}" not in have:
        raise SdlcError(f"label {PREFIX}{args.to} does not exist: run `bootstrap-labels` first (nothing was posted)")
    posted = post_comment(n, comment, config)
    if kind in DOC_KINDS and doc:
        old = next(c for c in bundle["comments"] if c["id"] == doc["id"])
        patch_comment(old["id"], render_superseded(old["body"], rev, posted.get("html_url", "")), config)
    set_state_label(n, current, args.to, config)
    print(f"#{n}: {st['state']} -> {args.to}  {posted.get('html_url', '')}")
    return 0


def prd_pr_errors(pr: dict[str, Any], default_branch: str) -> list[str]:
    """Why a PRD pull request may not be merged by the SDLC (empty list = fine)."""
    errs = []
    if pr.get("state") != "OPEN":
        errs.append(f"PR is {pr.get('state')}, not OPEN")
    if pr.get("baseRefName") != default_branch:
        errs.append(f"PR targets {pr.get('baseRefName')}, not {default_branch}")
    paths = [f["path"] for f in pr.get("files", [])]
    stray = [f for f in paths if not f.startswith("docs/prd/")]
    if not paths or stray:
        errs.append("a PRD PR may only change files under docs/prd/" + (f" (also changes: {', '.join(stray[:5])})" if stray else ""))
    if pr.get("mergeable") != "MERGEABLE":
        errs.append(f"PR is not mergeable yet ({pr.get('mergeable')})")
    return errs


def command_merge_prd(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Squash-merge the docs-only PR that carries an approved PRD."""
    repo = repo_of(config)
    st = bundle_state(fetch_bundle(args.number, config), config)
    errs = [] if st["state"] == "approved" else [f"#{args.number} is {st['state']}, not approved"]
    pr = gh_json(["pr", "view", str(args.pr), "--repo", repo, "--json", "state,mergeable,baseRefName,files"])
    errs += prd_pr_errors(pr, config.get("default_branch", "main"))
    if errs:
        raise SdlcError("; ".join(errs))
    if args.dry_run:
        print(f"would squash-merge PR #{args.pr} (docs/prd only) for approved #{args.number}")
        return 0
    gh(["pr", "merge", str(args.pr), "--repo", repo, "--squash", "--delete-branch"])
    print(f"merged PR #{args.pr}")
    return 0


def command_reconcile(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """A maintainer added `sdlc:approved` by hand: back it with an approval record."""
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    if st["reconcile"] not in ("label_approval", "stale_labels"):
        print(f"#{st['number']}: nothing to reconcile")
        return 0
    n, rev = st["number"], st["latest_doc"]["rev"]
    if st["reconcile"] == "stale_labels":
        if args.dry_run:
            print(f"would remove {st['stale_labels']} from #{n}")
        else:
            set_state_label(n, bundle["issue"]["labels"], "approved", config)
            print(f"#{n}: removed stale {st['stale_labels']}")
        return 0
    by = args.by
    if not by and not args.from_file:
        events = gh_pages(f"repos/{repo_of(config)}/issues/{n}/events")
        labeled = [e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == "sdlc:approved"]
        by = (labeled[-1].get("actor") or {}).get("login") if labeled else None
    if not by or by not in bundle["trusted"]:
        raise SdlcError(f"cannot back this label: it was applied by {by or 'unknown'}, who is not a collaborator "
                        "(pass --by <login> only if you checked)")
    body = f"{st['latest_doc']['kind'].upper()} rev {rev} approved by @{by} (label)."
    comment = render_comment("approval", rev, body, config, by=by, via="label")
    if args.dry_run:
        print(comment)
        return 0
    posted = post_comment(n, comment, config)
    if st["stale_labels"]:
        set_state_label(n, bundle["issue"]["labels"], "approved", config)
    print(f"#{n}: recorded approval of rev {rev} by {by}  {posted.get('html_url', '')}")
    return 0


# --- planning: the plan file ------------------------------------------------
#
# A plan is JSON (format: .agents/skills/plan-issue/references/plan-schema.md).
# Its leaves -- slices, or subtasks without slices -- are the units an agent
# works in one session; `blocked_by` names leaf keys only.

KEY_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9._-]*\Z")
LEAF_SECTIONS = {"outcome": "Outcome", "scope": "Scope", "acceptance": "Acceptance criteria",
                 "validation": "Validation", "demo": "Demo", "non_goals": "Non-goals", "context": "Context"}
PLAN_JSON_RE = re.compile(r"<!-- plan-json -->\s*(`{3,})json\n(.*?)\n\1", re.DOTALL)
VERDICT_RE = re.compile(r"\A[*_`]*VERDICT:[*_`\s]*(approve|changes)[*_`.\s]*\Z", re.IGNORECASE)
COMMENT_LIMIT = 65000   # GitHub refuses comments over 65536 characters
LEAF_LABEL, CONTAINER_LABEL = "plan:slice", "plan:subtask"


def natural_key(key: str) -> list[Any]:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", key)]


def plan_leaves(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every unit of work, each with `_parent` (its subtask's key, or None when it hangs off the epic)
    and `_milestone`."""
    out = []
    for t in plan.get("subtasks") or []:
        if t.get("slices"):
            out += [{**s, "_parent": t.get("key"), "_milestone": t.get("milestone")} for s in t["slices"]]
        else:
            out.append({**t, "_parent": None, "_milestone": t.get("milestone")})
    return out


def find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """A dependency cycle as a path (first node repeated at the end), or None."""
    colour: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if colour.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if nxt not in colour and (found := visit(nxt)):
                return found
        stack.pop()
        colour[node] = 2
        return None

    for node in sorted(graph, key=natural_key):
        if node not in colour and (found := visit(node)):
            return found
    return None


def topo_order(leaves: list[dict[str, Any]]) -> list[str]:
    """Leaf keys with every blocker before what it blocks (ties in natural order). Assumes no cycle."""
    keys = {l["key"] for l in leaves}
    deps = {l["key"]: {b for b in l.get("blocked_by") or [] if b in keys} for l in leaves}
    order: list[str] = []
    while deps:
        free = sorted((k for k, d in deps.items() if not d), key=natural_key)
        if not free:
            raise SdlcError("dependency cycle")
        order += free
        for k in free:
            del deps[k]
        for d in deps.values():
            d.difference_update(free)
    return order


def _text(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def validate_plan(plan: Any, criteria: set[int] | None = None) -> list[str]:
    """Reasons a plan can't be posted or created (empty list = fine).

    `criteria` are the PRD's numbered acceptance criteria: each must be covered
    by some leaf, and no leaf may claim one that doesn't exist.
    """
    if not isinstance(plan, dict):
        return ["the plan is not a JSON object"]
    errs: list[str] = []
    if not isinstance(plan.get("issue"), int):
        errs.append("`issue` must be the epic's issue number")
    if plan.get("kind") not in ("feature", "bug"):
        errs.append("`kind` must be feature or bug")
    subtasks = plan.get("subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        return errs + ["the plan has no subtasks"]
    milestones = plan.get("milestones") or []
    mkeys = [m.get("key") for m in milestones]
    keys: list[Any] = list(mkeys)
    for m in milestones:
        for f in ("title", "demo"):
            if not _text(m.get(f)):
                errs.append(f"milestone {m.get('key')} has no {f}")
    for t in subtasks:
        k = t.get("key")
        keys.append(k)
        if not _text(t.get("title")):
            errs.append(f"subtask {k} has no title")
        if milestones and t.get("milestone") not in mkeys:
            errs.append(f"subtask {k} needs a milestone (one of {', '.join(map(str, mkeys))})")
        if not milestones and t.get("milestone"):
            errs.append(f"subtask {k} names milestone {t['milestone']} but the plan has no milestones")
        if t.get("slices"):
            if not _text(t.get("summary")):
                errs.append(f"subtask {k} has no summary")
            if t.get("blocked_by"):
                errs.append(f"subtask {k} has slices: put blocked_by on the slices, not the subtask")
            keys += [s.get("key") for s in t["slices"]]
    errs += [f"bad key {k!r} (letters, digits, '.', '-', '_'; starts with a letter)"
             for k in keys if not (isinstance(k, str) and KEY_RE.match(k))]
    seen: set[Any] = set()
    for k in keys:
        if k in seen and isinstance(k, str):
            errs.append(f"duplicate key {k}")
        seen.add(k)
    leaves = plan_leaves(plan)
    leaf_keys = {l.get("key") for l in leaves}
    for l in leaves:
        k = l.get("key")
        if not _text(l.get("title")):
            errs.append(f"{k} has no title")
        for field, name in LEAF_SECTIONS.items():
            v = l.get(field)
            ok = (isinstance(v, list) and v and all(_text(x) for x in v)) if field == "acceptance" else _text(v)
            if not ok:
                errs.append(f"{k} has no {name}" + (" (a non-empty list)" if field == "acceptance" else ""))
        for b in l.get("blocked_by") or []:
            if b == k:
                errs.append(f"{k} is blocked by itself")
            elif b not in leaf_keys:
                errs.append(f"{k} is blocked by {b}, which is not a slice (or a subtask without slices)")
        covers = l.get("covers") or []
        if not all(isinstance(c, int) for c in covers):
            errs.append(f"{k}: covers must be acceptance-criterion numbers")
    graph = {l["key"]: [b for b in l.get("blocked_by") or [] if b in leaf_keys] for l in leaves if isinstance(l.get("key"), str)}
    if cycle := find_cycle(graph):
        errs.append("dependency cycle (each waits on the next): " + " -> ".join(cycle))
    if criteria is not None:
        covered = {c for l in leaves for c in l.get("covers") or [] if isinstance(c, int)}
        if missing := sorted(criteria - covered):
            errs.append("acceptance criteria no slice covers: " + ", ".join(map(str, missing)))
        if unknown := sorted(covered - criteria):
            errs.append("covers criteria the PRD doesn't have: " + ", ".join(map(str, unknown)))
    return errs


def parse_criteria(text: str) -> set[int]:
    """The numbered items under `## Acceptance criteria` and any `## Addendum`."""
    found, take = set(), False
    for line in text.splitlines():
        if line.startswith("## "):
            take = bool(re.search(r"acceptance criteria|addendum", line, re.IGNORECASE))
        elif take and (m := re.match(r"(\d+)\.\s", line)):
            found.add(int(m.group(1)))
    return found


def prd_path(n: int) -> Path | None:
    found = sorted((ROOT / "docs" / "prd").glob(f"{n}-*.md"))
    return found[0] if found else None


def plan_criteria(plan: dict[str, Any]) -> tuple[set[int] | None, list[str]]:
    """The criteria a plan must cover. A feature needs its merged PRD; a bug's diagnosis may have none."""
    path = prd_path(plan.get("issue", 0)) if isinstance(plan.get("issue"), int) else None
    criteria = parse_criteria(path.read_text()) if path else set()
    if plan.get("kind") == "feature" and not criteria:
        return None, [f"no numbered acceptance criteria found in docs/prd/{plan.get('issue')}-*.md "
                      "(merge the approved PRD first)"]
    return (criteria or None), []


def check_plan(plan: Any) -> list[str]:
    if not isinstance(plan, dict):
        return ["the plan is not a JSON object"]
    criteria, errs = plan_criteria(plan)
    return errs + validate_plan(plan, criteria)


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_plan(plan: dict[str, Any]) -> str:
    """The plan comment's body: the tree, the dependency graph, coverage, and the JSON that gets created."""
    leaves = plan_leaves(plan)
    milestones = plan.get("milestones") or []

    def leaf_line(l: dict[str, Any]) -> str:
        bits = [f"`{l['key']}` {l['title']}"]
        if l.get("covers"):
            bits.append("AC " + ", ".join(map(str, l["covers"])))
        if l.get("blocked_by"):
            bits.append("after " + ", ".join(f"`{b}`" for b in l["blocked_by"]))
        return " · ".join(bits)

    out = [f"**{len(plan['subtasks'])} subtasks, {len(leaves)} units of work** (each one Claude Code session)"
           + (f" in {len(milestones)} milestones" if milestones else "") + "."]
    for m in milestones or [None]:
        out.append("")
        out.append(f"### {m['key']} · {m['title']}\nDemo: {m['demo']}\n" if m else "### Work\n")
        for t in plan["subtasks"]:
            if (t.get("milestone") if m else None) != (m["key"] if m else None):
                continue
            if t.get("slices"):
                out.append(f"- **`{t['key']}` {t['title']}** — {t['summary']}")
                out += [f"  - {leaf_line(s)}" for s in t["slices"]]
            else:
                out.append(f"- {leaf_line(t)}")
    node = lambda k: re.sub(r"\W", "_", k)  # noqa: E731
    out += ["", "### Order", "", "```mermaid", "flowchart LR"]
    for l in leaves:
        out.append(f'  {node(l["key"])}["{l["key"]} {l["title"].replace(chr(34), "#quot;")}"]')
    for l in leaves:
        out += [f"  {node(b)} --> {node(l['key'])}" for b in l.get("blocked_by") or []]
    out.append("```")
    covers: dict[int, list[str]] = {}
    for l in leaves:
        for c in l.get("covers") or []:
            covers.setdefault(c, []).append(l["key"])
    if covers:
        out += ["", "### Acceptance criteria coverage", "", "| AC | covered by |", "|---|---|"]
        out += [f"| {c} | {', '.join(f'`{k}`' for k in ks)} |" for c, ks in sorted(covers.items())]
    blob = json.dumps(plan, indent=1, ensure_ascii=False)
    fence = _fence(blob)
    out += ["", "<details><summary>plan.json (exactly what gets created)</summary>", "",
            "<!-- plan-json -->", f"{fence}json", blob, fence, "", "</details>"]
    return "\n".join(out)


def extract_plan(body: str) -> dict[str, Any]:
    m = PLAN_JSON_RE.search(body or "")
    if not m:
        raise SdlcError("the plan comment carries no plan JSON")
    try:
        return json.loads(m.group(2))
    except ValueError as exc:
        raise SdlcError(f"the plan comment's JSON does not parse: {exc}")


def parse_verdict(report: str) -> str:
    """`approve` or `changes` from the last non-empty line of a Codex report. Anything else is a failed run."""
    lines = [l.strip() for l in (report or "").splitlines() if l.strip()]
    if not lines:
        raise SdlcError("the Codex report is empty: the review did not run")
    m = VERDICT_RE.match(lines[-1])
    if not m:
        raise SdlcError("the Codex report does not end in `VERDICT: approve` or `VERDICT: changes`; "
                        "treat it as a failed run and run the review again")
    return m.group(1).lower()


# --- planning: creating the issues -----------------------------------------

def issue_title(epic: int, item: dict[str, Any]) -> str:
    return f"#{epic} {item['key']}: {item['title']}"


def render_leaf_body(epic: int, leaf: dict[str, Any], parent: int | None, numbers: dict[str, int],
                     prd_url: str | None) -> str:
    head = [f"Epic: #{epic}"] + ([f"Parent: #{parent}"] if parent else [])
    if leaf.get("covers"):
        ac = "acceptance criteria " + ", ".join(map(str, leaf["covers"]))
        head.append(f"Covers {ac} of [the PRD]({prd_url})" if prd_url else f"Covers {ac}")
    out = [marker("slice", epic=str(epic), key=leaf["key"]), " · ".join(head)]
    for field, name in LEAF_SECTIONS.items():
        v = leaf[field]
        out += ["", f"## {name}", "", "\n".join(f"- [ ] {x}" for x in v) if field == "acceptance" else v.strip()]
    if leaf.get("blocked_by"):
        refs = ", ".join(f"#{numbers[b]} ({b})" if b in numbers else b for b in leaf["blocked_by"])
        out += ["", "## Dependencies", "", f"Blocked by {refs}. GitHub's blocked-by links are the authority; "
                "this line only explains them."]
    return "\n".join(out) + "\n"


def render_container_body(epic: int, task: dict[str, Any]) -> str:
    return (f"{marker('subtask', epic=str(epic), key=task['key'])}\nEpic: #{epic}\n\n{task['summary'].strip()}\n\n"
            "Its slices are this issue's sub-issues; each is one session of work.\n")


def plan_create_actions(plan: dict[str, Any], existing: dict[str, int], attached: dict[int, set[int]],
                        blocked: dict[int, set[int]]) -> list[tuple[Any, ...]]:
    """What is still missing on GitHub, in the order to do it.

    `existing` maps plan keys to issue numbers already created (found by their
    markers), `attached` parent number -> sub-issue numbers, `blocked` issue
    number -> blocked_by numbers. A rerun after a failure only finishes the job.
    """
    epic = plan["issue"]

    def linked(parent_key: str | None, key: str) -> bool:
        p = epic if parent_key is None else existing.get(parent_key)
        k = existing.get(key)
        return bool(p and k and k in attached.get(p, set()))

    acts: list[tuple[Any, ...]] = [("milestone", m["key"]) for m in plan.get("milestones") or []]
    for t in plan["subtasks"]:
        if t.get("slices"):
            if t["key"] not in existing:
                acts.append(("create", t["key"]))
            if not linked(None, t["key"]):
                acts.append(("attach", None, t["key"]))
    leaves = {l["key"]: l for l in plan_leaves(plan)}
    for k in topo_order(list(leaves.values())):
        l = leaves[k]
        if k not in existing:
            acts.append(("create", k))
        if not linked(l["_parent"], k):
            acts.append(("attach", l["_parent"], k))
        for b in l.get("blocked_by") or []:
            if not (k in existing and b in existing and existing[b] in blocked.get(existing[k], set())):
                acts.append(("block", k, b))
    return acts if any(a[0] != "milestone" for a in acts) else []


def ready_leaves(leaves: list[dict[str, Any]], blockers: dict[int, list[dict[str, Any]]]
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Open leaves split into (ready, waiting): ready when every blocker closed as completed."""
    ready, waiting = [], []
    for l in sorted((l for l in leaves if l.get("state", "open") == "open"), key=lambda l: natural_key(l["key"])):
        unmet = [b["number"] for b in blockers.get(l["number"], [])
                 if not (b.get("state") == "closed" and b.get("state_reason") == "completed")]
        (waiting if unmet else ready).append({**l, "unmet": unmet})
    return ready, waiting


def post_json(path: str, payload: dict[str, Any]) -> Any:
    with tempfile.NamedTemporaryFile("w", suffix=".json") as fh:
        json.dump(payload, fh)
        fh.flush()
        return gh_json(["api", "-X", "POST", path, "--input", fh.name])


def fetch_plan_issues(epic: int | None, config: dict[str, Any], trusted: set[str]) -> list[dict[str, Any]]:
    """Issues created by plan-create (found by their markers), for one epic or all of them."""
    repo, out, seen = repo_of(config), [], set()
    for label in (LEAF_LABEL, CONTAINER_LABEL):
        for raw in gh_pages(f"repos/{repo}/issues?labels={label.replace(':', '%3A')}&state=all&per_page=100"):
            if "pull_request" in raw or raw["number"] in seen or (raw.get("user") or {}).get("login") not in trusted:
                continue
            mk = parse_marker(raw.get("body") or "") or {}
            if mk.get("kind") not in ("slice", "subtask") or not mk.get("key") or not mk.get("epic", "").isdigit():
                continue
            if epic is not None and int(mk["epic"]) != epic:
                continue
            seen.add(raw["number"])
            out.append({"epic": int(mk["epic"]), "key": mk["key"], "kind": mk["kind"], "number": raw["number"],
                        "id": raw["id"], "title": raw.get("title", ""), "state": raw.get("state", "open"),
                        "state_reason": raw.get("state_reason"), "labels": [l["name"] for l in raw.get("labels", [])],
                        "assignees": [a["login"] for a in raw.get("assignees") or []],
                        "milestone": (raw.get("milestone") or {}).get("title")})
    return out


def fetch_links(epic: int, records: list[dict[str, Any]], config: dict[str, Any]
                ) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    repo = repo_of(config)
    parents = [epic] + [r["number"] for r in records if r["kind"] == "subtask"]
    attached = {p: {i["number"] for i in gh_pages(f"repos/{repo}/issues/{p}/sub_issues?per_page=100")} for p in parents}
    blocked = {r["number"]: {i["number"] for i in gh_pages(f"repos/{repo}/issues/{r['number']}/dependencies/blocked_by?per_page=100")}
               for r in records if r["kind"] == "slice"}
    return attached, blocked


def latest_plan_of(bundle: dict[str, Any], st: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not st.get("latest_plan"):
        raise SdlcError(f"#{st['number']} has no plan revision yet")
    c = next(c for c in bundle["comments"] if c["id"] == st["latest_plan"]["id"])
    return c, extract_plan(c["body"])


def command_plan_validate(args: argparse.Namespace, config: dict[str, Any]) -> int:
    plan = json.loads(Path(args.file).read_text())
    errs = check_plan(plan)
    for e in errs:
        print(f"  - {e}")
    if errs:
        print(f"{args.file}: {len(errs)} problem(s)")
        return 1
    leaves = plan_leaves(plan)
    print(f"{args.file}: ok — {len(plan['subtasks'])} subtasks, {len(leaves)} units of work, "
          f"can start at once: {', '.join(l['key'] for l in leaves if not l.get('blocked_by'))}")
    return 0


def command_plan_post(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Post a plan revision (validated) for Codex to review. Collapses the previous one."""
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    if st["action"] not in ("plan", "continue_plan", "create_plan_issues", "replan"):
        raise SdlcError(f"#{st['number']} is {st['state']} (action {st['action']}): not the time to post a plan"
                        + (" — the Codex rounds are spent; ask the poster (`transition N needs-info --kind question`)"
                           if st["action"] == "ask_poster" else ""))
    plan = json.loads(Path(args.file).read_text())
    errs = check_plan(plan)
    if plan.get("issue") != st["number"]:
        errs.append(f"the plan is for #{plan.get('issue')}, not #{st['number']}")
    if errs:
        raise SdlcError("the plan does not validate (run plan-validate): " + "; ".join(errs))
    rev = (st["latest_plan"]["rev"] if st["latest_plan"] else 0) + 1
    comment = render_comment("plan", rev, render_plan(plan), config)
    if len(comment) > COMMENT_LIMIT:
        raise SdlcError(f"the plan comment is {len(comment)} characters, over GitHub's limit: tighten the slice text")
    if args.dry_run:
        print(f"DRY RUN #{st['number']}: plan rev {rev}" + (" (and needs-info -> approved)" if st["state"] == "needs-info" else ""))
        print(comment)
        return 0
    n = st["number"]
    posted = post_comment(n, comment, config)
    if st["latest_plan"]:
        old, _ = latest_plan_of(bundle, st)
        patch_comment(old["id"], render_superseded(old["body"], rev, posted.get("html_url", "")), config)
    if st["state"] == "needs-info":
        set_state_label(n, bundle["issue"]["labels"], "approved", config)
    print(f"#{n}: plan rev {rev}  {posted.get('html_url', '')}")
    return 0


def command_plan_review(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Record one Codex round on the latest plan revision, with Claude's answer to it."""
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    if st["state"] != "approved" or not st["latest_plan"]:
        raise SdlcError(f"#{st['number']} has no plan under review (state {st['state']})")
    if st["plan_rounds"] >= st["max_plan_rounds"]:
        raise SdlcError(f"Codex rounds spent ({st['plan_rounds']}/{st['max_plan_rounds']}) without consensus: "
                        "ask the poster (`transition N needs-info --kind question`)")
    _, posted_plan = latest_plan_of(bundle, st)
    if json.loads(Path(args.plan).read_text()) != posted_plan:
        raise SdlcError(f"{args.plan} is not plan rev {st['latest_plan']['rev']} as posted: Codex must review exactly "
                        "what will be created (plan-post the file first, or point Codex at the posted JSON)")
    report = Path(args.report).read_text()
    verdict = parse_verdict(report)
    response = Path(args.response).read_text().strip() if args.response else ""
    rnd = st["plan_rounds"] + 1
    body = (f"**Round {rnd}/{st['max_plan_rounds']} — Codex: `{verdict}`**\n\n"
            f"<details><summary>Codex's review</summary>\n\n{report.strip()}\n\n</details>")
    if response:
        body += f"\n\n### Claude's response\n\n{response}"
    comment = render_comment("plan-review", st["latest_plan"]["rev"], body, config, verdict=verdict, round=str(rnd))
    if len(comment) > COMMENT_LIMIT:
        raise SdlcError(f"the review comment is {len(comment)} characters, over GitHub's limit")
    if args.dry_run:
        print(comment)
        return 0
    posted = post_comment(st["number"], comment, config)
    print(f"#{st['number']}: plan rev {st['latest_plan']['rev']} round {rnd}: {verdict}  {posted.get('html_url', '')}")
    return 0


def command_plan_create(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Create the epic's milestones, sub-issues and blocked_by links from the reviewed plan. Idempotent."""
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    if st["action"] != "create_plan_issues":
        raise SdlcError(f"#{st['number']} is not ready to create: action is {st['action']} "
                        "(it needs Codex's `approve` on the latest plan revision)")
    plan_comment, plan = latest_plan_of(bundle, st)
    epic = st["number"]
    errs = check_plan(plan) + ([] if plan.get("issue") == epic else [f"the plan is for #{plan.get('issue')}"])
    if errs:
        raise SdlcError("the reviewed plan no longer validates: " + "; ".join(errs))
    repo = repo_of(config)
    offline = bool(getattr(args, "from_file", None))
    trusted = set(bundle["trusted"])

    def look() -> tuple[dict[str, dict[str, Any]], list[tuple[Any, ...]]]:
        records = bundle.get("plan_issues", []) if offline else fetch_plan_issues(epic, config, trusted)
        attached, blocked = ({}, {}) if offline else fetch_links(epic, records, config)
        by_key = {r["key"]: r for r in records}
        return by_key, plan_create_actions(plan, {k: r["number"] for k, r in by_key.items()}, attached, blocked)

    by_key, acts = look()
    numbers: dict[str, Any] = {k: r["number"] for k, r in by_key.items()}
    ids: dict[str, Any] = {k: r["id"] for k, r in by_key.items()}
    items = {t["key"]: t for t in plan["subtasks"]} | {l["key"]: l for l in plan_leaves(plan)}
    path = prd_path(epic)
    prd_url = f"https://github.com/{repo}/blob/{config.get('default_branch', 'main')}/{path.relative_to(ROOT)}" if path else None
    if acts and not offline and not args.dry_run:
        have = {l["name"] for l in gh_json(["label", "list", "--repo", repo, "--limit", "200", "--json", "name"])}
        if missing := [l for l in (LEAF_LABEL, CONTAINER_LABEL) if l not in have]:
            # without them a rerun can't find what it made and would duplicate every issue
            raise SdlcError(f"labels {missing} do not exist: run `bootstrap-labels` first (nothing was created)")
    have_ms = {} if offline or not acts else {
        m["title"]: m["number"] for m in gh_pages(f"repos/{repo}/milestones?state=all&per_page=100")}
    ms: dict[str, Any] = {}
    say = (lambda s: print("would " + s)) if args.dry_run else print  # noqa: E731
    for act in acts:
        if act[0] == "milestone":
            m = next(m for m in plan["milestones"] if m["key"] == act[1])
            title = f"#{epic} {m['key']}: {m['title']}"
            if title in have_ms:
                ms[m["key"]] = have_ms[title]
                continue
            say(f"create milestone {title!r}")
            ms[m["key"]] = None if args.dry_run else post_json(
                f"repos/{repo}/milestones", {"title": title, "description": m["demo"]})["number"]
        elif act[0] == "create":
            item = items[act[1]]
            container = bool(item.get("slices"))
            body = (render_container_body(epic, item) if container else
                    render_leaf_body(epic, item, numbers.get(item["_parent"]) if item.get("_parent") else None,
                                     numbers, prd_url))
            payload: dict[str, Any] = {"title": issue_title(epic, item), "body": body,
                                       "labels": [CONTAINER_LABEL if container else LEAF_LABEL]}
            mkey = item.get("milestone") if container else item.get("_milestone")
            if mkey and ms.get(mkey):
                payload["milestone"] = ms[mkey]
            say(f"create {payload['title']!r} [{payload['labels'][0]}]" + (f" in milestone {mkey}" if mkey else ""))
            if args.dry_run:
                numbers[act[1]] = f"new:{act[1]}"
                continue
            made = post_json(f"repos/{repo}/issues", payload)
            numbers[act[1]], ids[act[1]] = made["number"], made["id"]
            time.sleep(1)   # GitHub's secondary rate limit on content creation
        elif act[0] == "attach":
            parent = epic if act[1] is None else numbers[act[1]]
            say(f"attach #{numbers[act[2]]} as a sub-issue of #{parent}")
            if not args.dry_run:
                post_json(f"repos/{repo}/issues/{parent}/sub_issues", {"sub_issue_id": ids[act[2]]})
                time.sleep(0.5)
        elif act[0] == "block":
            say(f"mark #{numbers[act[1]]} ({act[1]}) blocked by #{numbers[act[2]]} ({act[2]})")
            if not args.dry_run:
                post_json(f"repos/{repo}/issues/{numbers[act[1]]}/dependencies/blocked_by", {"issue_id": ids[act[2]]})
                time.sleep(0.5)
    if not acts:
        print(f"#{epic}: every issue and link already exists")
    if args.dry_run:
        print(f"then post the plan-created table on #{epic} and move it to {PREFIX}planned")
        return 0
    if not offline:
        by_key, left = look()
        if left:
            raise SdlcError(f"read-back still finds {len(left)} missing step(s), e.g. {left[0]}: rerun plan-create")
        numbers = {k: r["number"] for k, r in by_key.items()}
    rev = st["latest_plan"]["rev"]
    already = any((parse_marker(c["body"]) or {}).get("kind") == "plan-created" and c["author"] in trusted
                  and (parse_marker(c["body"]) or {}).get("rev") == str(rev) for c in bundle["comments"])
    if not already:
        leaves = plan_leaves(plan)
        rows = [f"| `{t['key']}` | #{numbers[t['key']]} | {t['title']} |" for t in plan["subtasks"] if t.get("slices")]
        rows += [f"| `{k}` | #{numbers[k]} | {items[k]['title']} |" for k in topo_order(leaves)]
        first = [f"#{numbers[l['key']]}" for l in leaves if not l.get("blocked_by")]
        body = (f"Created from [plan rev {rev}]({plan_comment.get('url', '')}) after Codex approved it.\n\n"
                "| key | issue | title |\n|---|---|---|\n" + "\n".join(rows) +
                f"\n\nReady to start: {', '.join(first)}. Each is one session of work (`work-slice`).")
        post_comment(epic, render_comment("plan-created", rev, body, config), config)
    set_state_label(epic, bundle["issue"]["labels"], "planned", config)
    print(f"#{epic}: approved -> planned ({len(numbers)} issues)")
    return 0


def command_ready(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Units of work whose blockers have all closed as completed: what `work-slice` may start."""
    leaves = [r for r in fetch_plan_issues(args.epic, config, fetch_trusted(config)) if r["kind"] == "slice"]
    blockers = {r["number"]: fetch_blockers(r["number"], config) for r in leaves if r["state"] == "open"}
    ready, waiting = ready_leaves(leaves, blockers)
    progress = summarise_progress(leaves, blockers)
    if args.json:
        print(json.dumps({"ready": ready, "waiting": waiting, "progress": progress}, indent=2))
        return 0
    for r in ready + waiting:
        tag = ("escalated" if r["number"] in progress["escalated"] else "claimed" if r["number"] in progress["in_flight"]
               else "ready" if not r["unmet"] else "waiting")
        print(f"{tag:<9} #{r['number']:<4} {r['title']}" + (f"  (on {', '.join('#%d' % n for n in r['unmet'])})" if r["unmet"] else ""))
    if not ready and not waiting:
        print("no open units of work" + (f" under #{args.epic}" if args.epic else ""))
    return 0


# --- building: one slice per session (work-slice) ---------------------------
#
# A slice's state lives on GitHub: open/closed, its blocked_by, marked comments
# on the slice (`claim`, `release`, `escalation`) and on its pull request
# (`tests`, `pr-review`). Test runs and Codex reviews are bound to a head SHA,
# so a new commit voids both and the merge gate sees it.

HEAD_RE = re.compile(r"^\s*[*_`]*HEAD:[*_`\s]*([0-9a-f]{40})\b", re.MULTILINE | re.IGNORECASE)
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.IGNORECASE)
ESCALATED_LABEL = f"{PREFIX}escalated"


def slice_branch(n: int) -> str:
    return f"slice/{n}"


def closes_refs(body: str) -> list[int]:
    return sorted({int(m) for m in CLOSES_RE.findall(body or "")})


def _marked(comments: list[dict[str, Any]], trusted: set[str]) -> list[tuple[dict[str, str], dict[str, Any]]]:
    ordered = sorted(comments, key=lambda c: (c.get("created_at", ""), c.get("id", 0)))
    return [(mk, c) for c in ordered if c.get("author") in trusted and (mk := parse_marker(c.get("body", "")))]


def claim_status(comments: list[dict[str, Any]], trusted: set[str]) -> dict[str, Any] | None:
    """The active claim on a slice: the latest `claim` not followed by a `release`."""
    active = None
    for mk, c in _marked(comments, trusted):
        if mk.get("kind") == "claim":
            active = {"branch": mk.get("branch"), "url": c.get("url"), "at": c.get("created_at")}
        elif mk.get("kind") == "release":
            active = None
    return active


def pr_records(comments: list[dict[str, Any]], trusted: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(test runs, Codex reviews) recorded on a pull request, oldest first."""
    tests, reviews = [], []
    for mk, c in _marked(comments, trusted):
        if mk.get("kind") == "tests":
            tests.append({"sha": mk.get("sha"), "result": mk.get("result"), "passed": mk.get("passed"), "url": c.get("url")})
        elif mk.get("kind") == "pr-review":
            reviews.append({"sha": mk.get("sha"), "verdict": mk.get("verdict"), "round": mk.get("round"), "url": c.get("url")})
    return tests, reviews


def head_records(pr: dict[str, Any], tests: list[dict[str, Any]], reviews: list[dict[str, Any]]
                 ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The latest test run and Codex review of the PR's *current* head (None when there is none)."""
    head = pr.get("headRefOid")
    t = [x for x in tests if x["sha"] == head]
    r = [x for x in reviews if x["sha"] == head]
    return (t[-1] if t else None), (r[-1] if r else None)


def unmet_blockers(blockers: list[dict[str, Any]]) -> list[int]:
    return [b["number"] for b in blockers if not (b.get("state") == "closed" and b.get("state_reason") == "completed")]


def slice_check_errors(issue: dict[str, Any], blockers: list[dict[str, Any]], claim: dict[str, Any] | None,
                       resume: bool = False) -> list[str]:
    """Why a slice may not be started (or resumed) now (empty list = go)."""
    n = issue["number"]
    mk = parse_marker(issue.get("body", "")) or {}
    if LEAF_LABEL not in issue.get("labels", []) or mk.get("kind") != "slice":
        return [f"#{n} is not a plan:slice issue made by plan-create"]
    errs = []
    if issue.get("state") != "open":
        errs.append(f"#{n} is closed")
    if ESCALATED_LABEL in issue.get("labels", []):
        errs.append(f"#{n} is escalated to a human")
    missing = [name for name in LEAF_SECTIONS.values() if f"## {name}" not in issue.get("body", "")]
    if missing:
        errs.append(f"#{n} lacks sections: {', '.join(missing)} (fix the issue or replan; don't guess)")
    if unmet := unmet_blockers(blockers):
        errs.append(f"#{n} is blocked by {', '.join(f'#{b}' for b in unmet)} (not closed as completed)")
    if claim and not resume:
        errs.append(f"#{n} is already claimed ({claim['url']}): `claim {n} --resume` to continue it")
    if resume and not claim:
        errs.append(f"#{n} has no claim to resume: `claim {n}`")
    return errs


def derive_slice(issue: dict[str, Any], comments: list[dict[str, Any]], trusted: set[str],
                 blockers: list[dict[str, Any]], pr: dict[str, Any] | None,
                 pr_comments: list[dict[str, Any]]) -> dict[str, Any]:
    """A slice's build state and the command to run next, from fetched JSON alone."""
    n = issue["number"]
    mk = parse_marker(issue.get("body", "")) or {}
    claim = claim_status(comments, trusted)
    tests, reviews = pr_records(pr_comments, trusted) if pr else ([], [])
    t, r = head_records(pr, tests, reviews) if pr else (None, None)
    unmet = unmet_blockers(blockers)
    epic = int(mk["epic"]) if mk.get("epic", "").isdigit() else None
    if issue.get("state") == "closed":
        state = "merged" if issue.get("state_reason") == "completed" else "closed"
    elif ESCALATED_LABEL in issue.get("labels", []):
        state = "escalated"
    elif pr and pr.get("state") == "OPEN":
        state = "in-review"
    elif claim:
        state = "claimed"
    elif unmet:
        state = "blocked"
    else:
        state = "ready"
    if state in ("ready", "claimed", "in-review"):
        nxt = f"/work-slice {n}"
    elif state == "blocked":
        nxt = "nothing yet: waiting on " + ", ".join(f"#{b}" for b in unmet)
    elif state == "escalated":
        nxt = f"nothing for an agent: #{n} needs a human"
    else:
        nxt = f"`uv run python tools/sdlc.py state {epic}` (the epic's next)" if epic else "nothing"
    return {"number": n, "title": issue.get("title"), "epic": epic, "key": mk.get("key"), "state": state,
            "claim": claim, "unmet": unmet, "pr": (pr or {}).get("number"), "pr_state": (pr or {}).get("state"),
            "head": (pr or {}).get("headRefOid"), "tests_on_head": t, "review_on_head": r,
            "pr_rounds": len(reviews), "next": nxt}


def merge_gate_errors(pr: dict[str, Any], slice_issue: dict[str, Any] | None, tests: list[dict[str, Any]],
                      reviews: list[dict[str, Any]], default_branch: str) -> list[str]:
    """Why the agent may not merge this pull request (empty list = merge)."""
    errs = []
    if pr.get("state") != "OPEN":
        errs.append(f"PR is {pr.get('state')}, not OPEN")
    if pr.get("isDraft"):
        errs.append("PR is a draft")
    if pr.get("baseRefName") != default_branch:
        errs.append(f"PR targets {pr.get('baseRefName')}, not {default_branch}")
    refs = closes_refs(pr.get("body", ""))
    if len(refs) != 1:
        errs.append(f"PR must close exactly one slice (`Closes #N`); it closes {refs or 'none'}")
    elif not slice_issue or slice_issue["number"] != refs[0]:
        errs.append(f"#{refs[0]} could not be read")
    elif LEAF_LABEL not in slice_issue.get("labels", []) or slice_issue.get("state") != "open":
        errs.append(f"#{refs[0]} is not an open plan:slice issue")
    t, r = head_records(pr, tests, reviews)
    head = (pr.get("headRefOid") or "")[:12]
    if not r:
        errs.append(f"no Codex review of the current head {head}")
    elif r["verdict"] != "approve":
        errs.append(f"Codex's latest review of {head} asks for changes")
    if not t:
        errs.append(f"no recorded test run on the current head {head} (`test-record`)")
    elif t["result"] != "pass":
        errs.append(f"the tests failed on {head}")
    if pr.get("mergeable") != "MERGEABLE":
        errs.append(f"GitHub says the PR is not mergeable ({pr.get('mergeable')}): rebase onto origin/main")
    return errs


def parse_pytest_summary(text: str) -> tuple[int, int]:
    """(passed, failed + errors) from pytest's summary line."""
    tail = "\n".join((text or "").strip().splitlines()[-3:])
    num = lambda word: sum(int(x) for x in re.findall(rf"(\d+) {word}", tail))  # noqa: E731
    return num("passed"), num("failed") + num("errors?")


def summarise_progress(leaves: list[dict[str, Any]], blockers: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    """An epic's units of work: what can start, what is in flight, and each milestone's count."""
    ready, waiting = ready_leaves(leaves, blockers)
    escalated = [l["number"] for l in leaves if l.get("state") == "open" and ESCALATED_LABEL in l.get("labels", [])]
    free = [l["number"] for l in ready if not l.get("assignees") and l["number"] not in escalated]
    in_flight = [l["number"] for l in ready + waiting if l.get("assignees") and l["number"] not in escalated]
    by_ms: dict[str, list[dict[str, Any]]] = {}
    for l in leaves:
        by_ms.setdefault(l.get("milestone") or "(no milestone)", []).append(l)
    milestones = [{"title": t, "total": len(ls),
                   "done": sum(1 for l in ls if l.get("state") == "closed" and l.get("state_reason") == "completed")}
                  for t, ls in sorted(by_ms.items())]
    return {"ready": free, "in_flight": in_flight, "escalated": escalated,
            "waiting": [l["number"] for l in waiting if l["number"] not in in_flight],
            "open": sum(1 for l in leaves if l.get("state") == "open"), "milestones": milestones}


def git(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SdlcError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def primary_root() -> Path:
    """The main checkout, even when run from inside a slice's worktree."""
    return Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"])).parent


def worktree_path(n: int, config: dict[str, Any]) -> Path:
    return primary_root() / config.get("worktree_dir", ".worktrees") / f"slice-{n}"


def fetch_slice(n: int, config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    repo = repo_of(config)
    raw = gh_json(["api", f"repos/{repo}/issues/{n}"])
    issue = {"number": raw["number"], "title": raw.get("title", ""), "state": raw.get("state", "open"),
             "state_reason": raw.get("state_reason"), "body": raw.get("body") or "",
             "labels": [l["name"] for l in raw.get("labels", [])],
             "assignees": [a["login"] for a in raw.get("assignees") or []]}
    return issue, [norm_comment(c) for c in gh_pages(f"repos/{repo}/issues/{n}/comments")]


def fetch_blockers(n: int, config: dict[str, Any]) -> list[dict[str, Any]]:
    return gh_pages(f"repos/{repo_of(config)}/issues/{n}/dependencies/blocked_by?per_page=100")


PR_FIELDS = "number,state,isDraft,baseRefName,headRefName,headRefOid,mergeable,body,url"


def fetch_pr(number: int, config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    repo = repo_of(config)
    pr = gh_json(["pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS])
    return pr, [norm_comment(c) for c in gh_pages(f"repos/{repo}/issues/{number}/comments")]


def find_slice_pr(n: int, config: dict[str, Any]) -> dict[str, Any] | None:
    """The slice's pull request (branch slice/N): the open one, else the latest."""
    prs = gh_json(["pr", "list", "--repo", repo_of(config), "--head", slice_branch(n), "--state", "all",
                   "--json", PR_FIELDS]) or []
    prs.sort(key=lambda p: (p["state"] == "OPEN", p["number"]))
    return prs[-1] if prs else None


def epic_progress(epic: int, config: dict[str, Any], trusted: set[str]) -> dict[str, Any]:
    leaves = [r for r in fetch_plan_issues(epic, config, trusted) if r["kind"] == "slice"]
    blockers = {r["number"]: fetch_blockers(r["number"], config) for r in leaves if r["state"] == "open"}
    return summarise_progress(leaves, blockers)


def slice_bundle(n: int, config: dict[str, Any]) -> dict[str, Any]:
    trusted = fetch_trusted(config)
    issue, comments = fetch_slice(n, config)
    pr = find_slice_pr(n, config)
    pr_comments = fetch_pr(pr["number"], config)[1] if pr else []
    return {"issue": issue, "comments": comments, "trusted": sorted(trusted),
            "blockers": fetch_blockers(n, config) if issue["state"] == "open" else [],
            "pr": pr, "pr_comments": pr_comments}


def bundle_slice(b: dict[str, Any]) -> dict[str, Any]:
    return derive_slice(b["issue"], b["comments"], set(b["trusted"]), b["blockers"], b["pr"], b["pr_comments"])


def command_slice_status(args: argparse.Namespace, config: dict[str, Any]) -> int:
    b = json.loads(Path(args.from_file).read_text()) if args.from_file else slice_bundle(args.number, config)
    st = bundle_slice(b)
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"#{st['number']} {st['title']}\n  state: {st['state']}   epic: #{st['epic']}   key: {st['key']}")
    if st["claim"]:
        print(f"  claimed: {st['claim']['url']} (branch {st['claim']['branch']})")
    if st["pr"]:
        t, r = st["tests_on_head"], st["review_on_head"]
        print(f"  PR #{st['pr']} ({st['pr_state']}) head {(st['head'] or '')[:12]}: tests {t['result'] if t else 'not run'}, "
              f"Codex {r['verdict'] if r else 'not yet'}, rounds {st['pr_rounds']}/{config.get('max_pr_rounds', 5)}")
    print(f"  next: {st['next']}")
    return 0


def command_slice_check(args: argparse.Namespace, config: dict[str, Any]) -> int:
    b = json.loads(Path(args.from_file).read_text()) if args.from_file else slice_bundle(args.number, config)
    errs = slice_check_errors(b["issue"], b["blockers"], claim_status(b["comments"], set(b["trusted"])), args.resume)
    for e in errs:
        print(f"  - {e}")
    print(f"#{args.number}: " + ("not workable" if errs else "ok to " + ("resume" if args.resume else "start")))
    return 1 if errs else 0


def command_claim(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Claim a ready slice and give it its own worktree on branch slice/N, from origin/main."""
    n = args.number
    b = slice_bundle(n, config)
    claim = claim_status(b["comments"], set(b["trusted"]))
    if errs := slice_check_errors(b["issue"], b["blockers"], claim, args.resume):
        raise SdlcError("; ".join(errs))
    root, wt, branch = primary_root(), worktree_path(n, config), slice_branch(n)
    epic = int((parse_marker(b["issue"]["body"]) or {})["epic"])
    if args.dry_run:
        print(f"would {'resume' if args.resume else 'claim'} #{n}: worktree {wt} on {branch} from origin/main; epic #{epic}")
        return 0
    git(["fetch", "origin", "main"], cwd=root)
    local = bool(git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root, check=False))
    remote = bool(git(["ls-remote", "--heads", "origin", branch], cwd=root, check=False))
    if wt.exists():
        if not args.resume:
            raise SdlcError(f"{wt} already exists: `claim {n} --resume`, or `cleanup {n} --force` to discard it")
    elif local:
        if not args.resume:
            raise SdlcError(f"branch {branch} already exists: `claim {n} --resume`")
        git(["worktree", "add", str(wt), branch], cwd=root)
    elif remote and args.resume:
        git(["fetch", "origin", branch], cwd=root)
        git(["worktree", "add", "-b", branch, str(wt), f"origin/{branch}"], cwd=root)
    else:
        git(["worktree", "add", "-b", branch, str(wt), "origin/main"], cwd=root)
    if not claim:
        post_comment(n, render_comment("claim", None, f"Working on this in `{branch}`.", config, branch=branch), config)
        gh(["issue", "edit", str(n), "--repo", repo_of(config), "--add-assignee", "@me"])
    ep = fetch_bundle(epic, config, set(b["trusted"]))["issue"]
    if f"{PREFIX}planned" in ep["labels"]:
        set_state_label(epic, ep["labels"], "in-progress", config)
        print(f"#{epic}: planned -> in-progress")
    print(f"WORKTREE={wt}\nBRANCH={branch}\nBASE=origin/main")
    return 0


def command_release(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Give up a claim (the slice becomes ready again). The branch and worktree are left for `cleanup`."""
    body = "Releasing this slice" + (f": {args.reason}" if args.reason else ".")
    if args.dry_run:
        print(render_comment("release", None, body, config))
        return 0
    post_comment(args.number, render_comment("release", None, body, config), config)
    gh(["issue", "edit", str(args.number), "--repo", repo_of(config), "--remove-assignee", "@me"], check=False)
    print(f"#{args.number}: released")
    return 0


def command_test_record(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Run the full test suite on the PR's head, here, and record the result on the PR."""
    pr, _ = fetch_pr(args.pr, config)
    if git(["status", "--porcelain"]):
        raise SdlcError("the working tree is not clean: commit (and push) first, so the run is of a real head")
    head = git(["rev-parse", "HEAD"])
    if head != pr["headRefOid"]:
        raise SdlcError(f"HEAD {head[:12]} is not PR #{args.pr}'s head {pr['headRefOid'][:12]}: push, or check out the branch")
    proc = subprocess.run(["uv", "run", "pytest", "-q"], capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    passed, failed = parse_pytest_summary(out)
    result = "pass" if proc.returncode == 0 else "fail"
    tail = "\n".join(out.strip().splitlines()[-12:])
    fence = _fence(tail)
    body = (f"`uv run pytest -q` on `{head[:12]}`: **{result}** ({passed} passed" + (f", {failed} failed" if failed else "")
            + f").\n\n<details><summary>last lines</summary>\n\n{fence}\n{tail}\n{fence}\n\n</details>")
    comment = render_comment("tests", None, body, config, sha=head, result=result, passed=str(passed))
    if args.dry_run:
        print(comment)
    else:
        posted = post_comment(args.pr, comment, config)
        print(f"PR #{args.pr}: tests {result} on {head[:12]} ({passed} passed)  {posted.get('html_url', '')}")
    return 0 if result == "pass" else 1


def command_pr_review(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Record one Codex round on the PR's current head, with Claude's answer to it."""
    if args.from_file:
        b = json.loads(Path(args.from_file).read_text())
        pr, comments, trusted = b["pr"], b["pr_comments"], set(b["trusted"])
    else:
        (pr, comments), trusted = fetch_pr(args.pr, config), fetch_trusted(config)
    _, reviews = pr_records(comments, trusted)
    limit = config.get("max_pr_rounds", 5)
    if pr.get("state") != "OPEN":
        raise SdlcError(f"PR #{pr['number']} is {pr.get('state')}")
    if len(reviews) >= limit:
        raise SdlcError(f"Codex rounds spent ({len(reviews)}/{limit}) without approval: `escalate-slice N --reason ...`")
    report = Path(args.report).read_text()
    verdict = parse_verdict(report)
    m = HEAD_RE.search(report)
    if not m:
        raise SdlcError("the Codex report has no `HEAD: <40-hex sha>` line: it can't show what it reviewed; run it again")
    if m.group(1) != pr["headRefOid"]:
        raise SdlcError(f"Codex reviewed {m.group(1)[:12]} but the PR head is {pr['headRefOid'][:12]}: review the current head")
    response = Path(args.response).read_text().strip() if args.response else ""
    rnd = len(reviews) + 1
    body = (f"**Round {rnd}/{limit} — Codex on `{pr['headRefOid'][:12]}`: `{verdict}`**\n\n"
            f"<details><summary>Codex's review</summary>\n\n{report.strip()}\n\n</details>")
    if response:
        body += f"\n\n### Claude's response\n\n{response}"
    comment = render_comment("pr-review", None, body, config, sha=pr["headRefOid"], verdict=verdict, round=str(rnd))
    if len(comment) > COMMENT_LIMIT:
        raise SdlcError(f"the review comment is {len(comment)} characters, over GitHub's limit")
    if args.dry_run:
        print(comment)
        return 0
    posted = post_comment(pr["number"], comment, config)
    print(f"PR #{pr['number']} round {rnd}: {verdict}  {posted.get('html_url', '')}")
    return 0


def command_merge(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """The merge gate: Codex approved this head, the tests passed on it, it closes one slice. Then squash-merge."""
    repo = repo_of(config)
    pr, comments = fetch_pr(args.pr, config)
    trusted = fetch_trusted(config)
    tests, reviews = pr_records(comments, trusted)
    refs = closes_refs(pr.get("body", ""))
    slice_issue = fetch_slice(refs[0], config)[0] if len(refs) == 1 else None
    if errs := merge_gate_errors(pr, slice_issue, tests, reviews, config.get("default_branch", "main")):
        raise SdlcError("; ".join(errs))
    n = refs[0]
    if args.dry_run:
        print(f"would squash-merge PR #{args.pr} (closes #{n}) at {pr['headRefOid'][:12]}")
        return 0
    gh(["pr", "merge", str(args.pr), "--repo", repo, "--squash", "--match-head-commit", pr["headRefOid"]])
    gh(["api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{pr['headRefName']}"], check=False)
    issue = fetch_slice(n, config)[0]
    if issue["state"] != "closed":
        gh(["issue", "close", str(n), "--repo", repo, "--reason", "completed", "--comment", f"Merged in #{args.pr}."])
    print(f"merged PR #{args.pr}; #{n} closed")
    epic = (parse_marker(issue["body"]) or {}).get("epic", "")
    if epic.isdigit():
        st = with_next(bundle_state(fetch_bundle(int(epic), config, trusted), config), config, trusted=trusted)
        for m in (st.get("progress") or {}).get("milestones", []):
            if m["done"] == m["total"]:
                print(f"milestone {m['title']} is complete")
        print(f"next: {st['next']}")
    return 0


def command_escalate_slice(args: argparse.Namespace, config: dict[str, Any]) -> int:
    body = "Handing this slice to a human: " + args.reason
    if args.dry_run:
        print(render_comment("escalation", None, body, config))
        return 0
    post_comment(args.number, render_comment("escalation", None, body, config), config)
    gh(["issue", "edit", str(args.number), "--repo", repo_of(config), "--add-label", ESCALATED_LABEL])
    print(f"#{args.number}: escalated")
    return 0


def command_cleanup(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Remove a slice's worktree and local branch, from the primary checkout. Safe to rerun."""
    n, root = args.number, primary_root()
    wt, branch = worktree_path(n, config), slice_branch(n)
    if Path.cwd().resolve() == wt.resolve() or wt.resolve() in Path.cwd().resolve().parents:
        raise SdlcError(f"run cleanup from the primary checkout ({root}), not from inside {wt}")
    pr = find_slice_pr(n, config)
    if not args.force and not (pr and pr["state"] == "MERGED"):
        raise SdlcError(f"slice/{n}'s PR is not merged: `--force` discards the work")
    if wt.exists():
        git(["worktree", "remove", str(wt)] + (["--force"] if args.force else []), cwd=root)
    git(["worktree", "prune"], cwd=root)
    if git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root, check=False):
        git(["branch", "-D", branch], cwd=root)
    print(f"#{n}: removed {wt} and {branch}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bootstrap-labels", help="create the sdlc:* labels (idempotent)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_bootstrap_labels)

    p = sub.add_parser("state", help="derive an issue's state and whose move it is")
    p.add_argument("number", type=int, nargs="?", default=0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--from-file", help="a saved {issue, comments, trusted} bundle instead of GitHub")
    p.set_defaults(fn=command_state)

    p = sub.add_parser("next", help="open issues where the agent has the next move")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=command_next)

    p = sub.add_parser("transition", help="post a marked agent comment and move the state label")
    p.add_argument("number", type=int)
    p.add_argument("to", help="target state, e.g. prd-review (without the sdlc: prefix)")
    p.add_argument("--kind", required=True, choices=sorted(KINDS))
    p.add_argument("--body-file")
    p.add_argument("--rev", type=int)
    p.add_argument("--reason", help="why, for an escalation")
    p.add_argument("--dry-run", action="store_true", help="print the labels and comment, change nothing")
    p.add_argument("--from-file")
    p.set_defaults(fn=command_transition)

    p = sub.add_parser("reconcile", help="record an approval for a hand-applied sdlc:approved label")
    p.add_argument("number", type=int)
    p.add_argument("--by")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-file")
    p.set_defaults(fn=command_reconcile)

    p = sub.add_parser("merge-prd", help="merge the docs-only PR carrying an approved PRD")
    p.add_argument("number", type=int, help="the issue")
    p.add_argument("pr", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_merge_prd)

    p = sub.add_parser("plan-validate", help="check a plan file: sections, dependencies, PRD coverage")
    p.add_argument("file")
    p.set_defaults(fn=command_plan_validate)

    p = sub.add_parser("plan-post", help="post a plan revision for Codex to review")
    p.add_argument("number", type=int)
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-file")
    p.set_defaults(fn=command_plan_post)

    p = sub.add_parser("plan-review", help="record a Codex review round (its verdict) and Claude's response")
    p.add_argument("number", type=int)
    p.add_argument("--plan", required=True, help="the plan file Codex reviewed; must equal the posted revision")
    p.add_argument("--report", required=True, help="Codex's stdout; its last line is the verdict")
    p.add_argument("--response", help="Claude's answer to each finding (markdown)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-file")
    p.set_defaults(fn=command_plan_review)

    p = sub.add_parser("plan-create", help="create the reviewed plan's issues and links (idempotent)")
    p.add_argument("number", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-file")
    p.set_defaults(fn=command_plan_create)

    p = sub.add_parser("slice-status", help="a slice's build state and what to run next")
    p.add_argument("number", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--from-file")
    p.set_defaults(fn=command_slice_status)

    p = sub.add_parser("slice-check", help="may this slice be started (or resumed) now?")
    p.add_argument("number", type=int)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--from-file")
    p.set_defaults(fn=command_slice_check)

    p = sub.add_parser("claim", help="claim a ready slice and make its worktree")
    p.add_argument("number", type=int)
    p.add_argument("--resume", action="store_true", help="reattach to this slice's existing claim and branch")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_claim)

    p = sub.add_parser("release", help="give up a claim (the slice becomes ready again)")
    p.add_argument("number", type=int)
    p.add_argument("--reason")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_release)

    p = sub.add_parser("test-record", help="run the full suite on the PR head here and record it on the PR")
    p.add_argument("pr", type=int)
    p.add_argument("--dry-run", action="store_true", help="run the tests, print the record, post nothing")
    p.set_defaults(fn=command_test_record)

    p = sub.add_parser("pr-review", help="record a Codex review round of the PR's current head")
    p.add_argument("pr", type=int)
    p.add_argument("--report", required=True, help="Codex's stdout: a `HEAD: <sha>` line, last line the verdict")
    p.add_argument("--response", help="Claude's answer to each finding (markdown)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-file")
    p.set_defaults(fn=command_pr_review)

    p = sub.add_parser("merge", help="the merge gate, then squash-merge a slice's PR")
    p.add_argument("pr", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_merge)

    p = sub.add_parser("escalate-slice", help="hand a slice to a human")
    p.add_argument("number", type=int)
    p.add_argument("--reason", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_escalate_slice)

    p = sub.add_parser("cleanup", help="remove a merged slice's worktree and local branch")
    p.add_argument("number", type=int)
    p.add_argument("--force", action="store_true", help="also when the PR is not merged (discards the work)")
    p.set_defaults(fn=command_cleanup)

    p = sub.add_parser("ready", help="units of work whose blockers are all done")
    p.add_argument("--epic", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=command_ready)

    args = ap.parse_args(argv)
    try:
        return args.fn(args, load_config(args.config))
    except SdlcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
