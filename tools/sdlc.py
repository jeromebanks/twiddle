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

Two rules hold the process together (see SDLC.md):

* The agent posts through `gh` as the maintainer, so the login cannot tell its
  comments from a human's. Every agent comment starts with a marker,
  `<!-- sdlc:v1 kind=prd rev=2 -->`. A *reply* is an unmarked comment newer
  than the latest marked one; a marker only counts when its author can write
  to the repo.
* A sign-off binds to one revision. `/approve` (at the start of a line, outside
  quotes and code, from the issue author or a collaborator) approves the latest
  PRD/diagnosis revision only; posting a newer revision voids it.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
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
LATER_STATES = {"planned", "in-progress", "demo-review", "done"}
# from-state -> states the triage skill may move to. `escalated` is open from anywhere.
TRANSITIONS = {
    "untriaged": {"triage", "needs-info", "prd-review", "diagnosis-review"},
    "triage": {"triage", "needs-info", "prd-review", "diagnosis-review"},
    "needs-info": {"needs-info", "prd-review", "diagnosis-review"},
    "prd-review": {"prd-review", "needs-info", "approved"},
    "diagnosis-review": {"diagnosis-review", "needs-info", "approved"},
    "approved": {"prd-review", "diagnosis-review"},   # a later revision reopens review
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
    for i, c in enumerate(ordered):
        mk = parse_marker(c.get("body", "")) if c.get("author") in trusted else None
        if not mk:
            continue
        marked_ids.add(c.get("id"))
        last_marked = i
        kind = mk.get("kind", "")
        rev = int(mk["rev"]) if mk.get("rev", "").isdigit() else None
        if kind in ROUND_KINDS:
            rounds += 1
        if kind in DOC_KINDS and rev is not None:
            latest_doc = {"kind": kind, "rev": rev, "id": c.get("id"), "url": c.get("url")}
            approved_rev = None
        elif kind == "approval" and latest_doc and rev == latest_doc["rev"]:
            approved_rev, approval_by = rev, mk.get("by")

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
    elif state in ("untriaged", "triage"):
        turn, action = "agent", "triage"
    elif state == "approved":
        turn, action = ("agent", "reconcile_label") if reconcile else ("agent", "plan")
    elif replies:
        turn = "agent"
        action = "record_approval" if decision and decision["action"] == "approve" and decision.get("valid") else "respond_to_reply"
    else:
        turn, action = "poster", "wait_for_poster"

    type_label = next((t for t in ("bug", "enhancement") if t in issue.get("labels", [])), None)
    return {
        "number": issue.get("number"), "title": issue.get("title"), "state": state, "turn": turn,
        "action": action, "type": type_label, "latest_doc": latest_doc, "approved_rev": approved_rev,
        "approval_by": approval_by, "rounds": rounds, "max_rounds": config.get("max_rounds", 5),
        "replies": [{"id": c.get("id"), "by": c.get("author"), "url": c.get("url")} for c in replies],
        "decision": decision, "ignored_keywords": ignored, "reconcile": reconcile, "stale_labels": stale,
        "conflicts": conflicts,
    }


def check_transition(st: dict[str, Any], to: str, kind: str, config: dict[str, Any]) -> list[str]:
    """Reasons a transition is not allowed (empty list = fine)."""
    errs: list[str] = []
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
    if to not in ("escalated", "approved") and st["rounds"] >= st["max_rounds"] and kind in ROUND_KINDS:
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
}
FOOTERS = {
    "question": "Reply in a comment. Anything you leave unanswered, I will assume the stated default.",
    "prd": "Reply `/approve` to sign off on this revision, `/changes <what>` to ask for changes, or just comment.",
    "diagnosis": "Reply `/approve` to sign off on this diagnosis, `/changes <what>` to ask for changes, or just comment.",
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


def command_bootstrap_labels(args: argparse.Namespace, config: dict[str, Any]) -> int:
    repo = repo_of(config)
    for l in config["labels"]:
        cmd = ["label", "create", l["name"], "--repo", repo, "--color", l["color"],
               "--description", l["description"], "--force"]
        if args.dry_run:
            print("would run: gh " + " ".join(cmd))
        else:
            gh(cmd)
            print(f"label {l['name']}")
    return 0


def command_state(args: argparse.Namespace, config: dict[str, Any]) -> int:
    st = bundle_state(load_bundle(args, config), config)
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
        st = bundle_state(fetch_bundle(raw["number"], config, trusted), config)
        if st["turn"] == "agent":
            out.append(st)
    if args.json:
        print(json.dumps(out, indent=2))
    elif not out:
        print("nothing waiting on the agent")
    else:
        for st in out:
            print(f"#{st['number']:<4} {st['state']:<17} {st['action']:<16} {st['title']}")
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
                "Next: planning (`plan-issue`, not built yet).")
    elif kind == "note" and args.to == "triage":
        body = "Triaging this now: reading the issue and the code it touches."
    elif kind == "escalation":
        body = "Handing this to a human: " + (args.reason or "no consensus or the agent is stuck.")
    else:
        raise SdlcError("--body-file is required")
    extra = {"by": st["decision"]["by"], "via": "comment"} if kind == "approval" else {}
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

    args = ap.parse_args(argv)
    try:
        return args.fn(args, load_config(args.config))
    except SdlcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
