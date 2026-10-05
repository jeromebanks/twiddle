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
    uv run python tools/sdlc.py demo-status 12
    uv run python tools/sdlc.py demo-post 12 --milestone M1 --dir demo --body-file comment.md --dry-run
    uv run python tools/sdlc.py demo-accept 12      # or demo-changes 12 --body-file changes.md
    uv run python tools/sdlc.py epic-branch 12      # slices land on epic/12 (plan-create makes it)
    uv run python tools/sdlc.py sync 12 && uv run python tools/sdlc.py ship-review 12 --report codex.md
    uv run python tools/sdlc.py ship 12             # an accepted milestone: epic/12 -> main, a merge commit
    uv run python tools/sdlc.py revert-slice 28     # undo one slice on epic/12 before it ships

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
import contextlib
import fcntl
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from dataclasses import dataclass, field
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
LATER_STATES = {"done"}
BUILD_STATES = {"planned", "in-progress"}     # the epic's slices are being built (`work-slice`)
TRIAGE_ACTIONS = {"triage", "respond_to_reply", "record_approval", "reconcile_label"}
PLAN_ACTIONS = {"plan", "continue_plan", "create_plan_issues", "ask_poster", "replan", "plan_next_milestone",
                "escalate_plan"}
# Planning comments: posted by plan-post / plan-review / plan-create, never by `transition`.
PLAN_KINDS = {"plan", "plan-review", "plan-created"}
# Milestone demos (milestone-demo), also kept out of DOC_KINDS: a demo never voids the PRD's sign-off.
DEMO_KINDS = {"demo", "demo-approval", "demo-changes", "demo-request"}
DEMO_ACTIONS = {"record_demo_acceptance", "demo_reply", "demo_heard"}
# every action derive_state can return (docs/sdlc.html explains each; tests hold both to this list)
ALL_ACTIONS = {"triage", "respond_to_reply", "record_approval", "reconcile_label",
               "plan", "continue_plan", "create_plan_issues", "ask_poster", "replan", "plan_next_milestone", "escalate_plan",
               "record_demo_acceptance", "demo_reply", "demo_heard",
               "work_slices", "wait_for_poster", "human", "fix_conflict", "none"}
NO_MILESTONE = "all"     # an epic planned without milestones has one demo, for all of it
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
    "demo-review": {"demo-review"},                    # a note answering the poster's question
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


# --- the ledger: an issue's marked comments, read in order ---------------------
#
# Every agent comment carries a marker (`kind=...`). Reading them oldest first
# builds the issue's history: the latest PRD, the plan and its Codex rounds,
# each milestone's demo and release, the records bound to commits. One handler
# per kind, in MARKERS: a new kind of agent comment is one entry here, and the
# decisions in derive_state / epic_view read only the ledger.

@dataclass
class Ledger:
    latest_doc: dict[str, Any] | None = None
    approved_rev: int | None = None
    approval_by: str | None = None
    rounds: int = 0                      # triage rounds (questions and PRD/diagnosis revisions)
    latest_plan: dict[str, Any] | None = None
    plan_body: str = ""                  # the latest plan revision's comment: its milestones and create mode
    plan_verdict: str | None = None
    plan_rounds: int = 0
    plan_after_created: bool = False     # a plan revision posted since the last plan-created: an amendment
    created: set[str] | None = field(default_factory=set)   # milestones whose issues exist (None: all, legacy)
    cleanups: set[str] = field(default_factory=set)         # milestones whose tech-debt cleanup is planned
    feedback: bool = False               # demo changes (the poster's or the agent's) not yet turned into issues
    feedback_plan: bool = False          # a plan revision posted since that feedback
    demos: dict[str, dict[str, Any]] = field(default_factory=dict)   # milestone -> its latest demo
    latest_demo: dict[str, Any] | None = None
    request: dict[str, Any] | None = None    # demo steps only a person can run, asked for on the issue
    shipped: dict[str, str] = field(default_factory=dict)              # milestone -> the commit on main
    untested: dict[str, str] = field(default_factory=dict)  # releases holding commits no run saw, until verified
    ship_reviews: list[dict[str, Any]] = field(default_factory=list)   # Codex on main...epic/N, per head
    epic_tests: list[dict[str, Any]] = field(default_factory=list)     # full runs on the epic head


def _rev(mk: dict[str, str]) -> int | None:
    return int(mk["rev"]) if mk.get("rev", "").isdigit() else None


def _ms(mk: dict[str, str]) -> str:
    return mk.get("milestone", NO_MILESTONE)


def _replan(L: Ledger) -> None:
    """Changes to plan (feedback): plan-issue amends the plan, with fresh Codex rounds."""
    L.feedback, L.feedback_plan, L.plan_rounds, L.plan_verdict = True, False, 0, None


def _reopen(L: Ledger, m: str) -> None:
    """The milestone goes back to being built: its acceptance is void, and a new demo will ask again."""
    if d := L.demos.get(m):
        d["accepted"], d["voided"] = False, True
    if L.request and L.request["milestone"] == m:
        L.request = None


def _on_question(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    if mk.get("phase") == "plan":
        L.plan_rounds, L.plan_verdict = 0, None   # the poster's answer buys a fresh set of Codex rounds
    else:
        L.rounds += 1


def _on_doc(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    L.rounds += 1
    if (rev := _rev(mk)) is not None:
        L.latest_doc = {"kind": mk["kind"], "rev": rev, "id": c.get("id"), "url": c.get("url")}
        L.approved_rev = None


def _on_approval(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    if L.latest_doc and _rev(mk) == L.latest_doc["rev"]:
        L.approved_rev, L.approval_by = _rev(mk), mk.get("by")


def _on_plan(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    if (rev := _rev(mk)) is not None:
        L.latest_plan = {"rev": rev, "id": c.get("id"), "url": c.get("url")}
        L.plan_body, L.plan_verdict = c.get("body", ""), None
        L.feedback_plan, L.plan_after_created = L.feedback, True


def _on_plan_review(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    L.plan_rounds += 1
    if L.latest_plan and _rev(mk) == L.latest_plan["rev"]:
        L.plan_verdict = mk.get("verdict")


def _on_plan_created(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    L.feedback = L.feedback_plan = L.plan_after_created = False
    L.plan_rounds = 0   # the next amendment (a milestone's replan, demo changes) gets fresh rounds
    if mk.get("cleanup"):
        L.cleanups.add(mk["cleanup"])
    if "created" not in mk:
        L.created = None
    elif L.created is not None:
        L.created |= {x for x in mk["created"].split(",") if x}


def _on_demo(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    if (rev := _rev(mk)) is None:
        return
    m = _ms(mk)
    if L.request and L.request["milestone"] == m:
        L.request = None
    L.latest_demo = L.demos[m] = {"milestone": m, "rev": rev, "id": c.get("id"), "url": c.get("url"),
                                  "sha": mk.get("sha"), "accepted": False, "changes": False}


def _on_demo_approval(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    d = L.demos.get(_ms(mk))
    if d and d["rev"] == _rev(mk):
        d["accepted"] = True


def _on_demo_changes(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    if L.request and L.request["milestone"] == _ms(mk):
        L.request = None
    if mk.get("found") == "agent":
        # the agent's own demo found the milestone broken: fix slices, then a new demo (an acceptance is void)
        _replan(L)
        _reopen(L, _ms(mk))
        return
    d = L.demos.get(_ms(mk))
    if d and _rev(mk) is not None and d["rev"] == _rev(mk):
        d["changes"], d["accepted"] = True, False   # changes after an /approve void it: a new demo comes first
        _replan(L)   # the poster's changes become new slices


def _on_demo_void(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    _reopen(L, _ms(mk))   # a slice of it was reverted (`revert-slice`): it needs a new demo


def _on_demo_request(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    L.request = {"milestone": _ms(mk), "index": i, "url": c.get("url")}


def _on_shipped(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    L.shipped[_ms(mk)] = mk.get("sha", "")
    if mk.get("untested"):
        L.untested[_ms(mk)] = mk.get("sha", "")


def _on_main_tests(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    if mk.get("result") == "pass":   # a passing run on a main that contains these releases
        for sha in (mk.get("covers") or "").split(","):
            for k in [k for k, v in L.untested.items() if v == sha]:
                del L.untested[k]


def _on_ship_review(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    L.ship_reviews.append({"milestone": mk.get("milestone"), "sha": mk.get("sha"), "verdict": mk.get("verdict"),
                           "url": c.get("url")})


def _on_epic_tests(L: Ledger, mk: dict[str, str], c: dict[str, Any], i: int) -> None:
    L.epic_tests.append({"sha": mk.get("sha"), "result": mk.get("result"), "passed": mk.get("passed"),
                         "main": mk.get("main"), "url": c.get("url")})


# kind -> handler. Kinds not listed (notes, escalations, claims on slices...) only end the run of replies.
MARKERS = {
    "question": _on_question, "prd": _on_doc, "diagnosis": _on_doc, "approval": _on_approval,
    "plan": _on_plan, "plan-review": _on_plan_review, "plan-created": _on_plan_created,
    "demo": _on_demo, "demo-approval": _on_demo_approval, "demo-changes": _on_demo_changes,
    "demo-void": _on_demo_void, "demo-request": _on_demo_request,
    "shipped": _on_shipped, "ship-review": _on_ship_review, "epic-tests": _on_epic_tests,
    "main-tests": _on_main_tests,
}


def read_ledger(ordered: list[dict[str, Any]], trusted: set[str]) -> tuple[Ledger, int, set[Any]]:
    """(the ledger, the index of the last marked comment, the marked comments' ids)."""
    L, last, marked = Ledger(), -1, set()
    for i, c in enumerate(ordered):
        mk = parse_marker(c.get("body", "")) if c.get("author") in trusted else None
        if not mk:
            continue
        marked.add(c.get("id"))
        last = i
        if handler := MARKERS.get(mk.get("kind", "")):
            handler(L, mk, c, i)
    return L, last, marked


def derive_state(issue: dict[str, Any], comments: list[dict[str, Any]],
                 trusted: set[str], config: dict[str, Any]) -> dict[str, Any]:
    """Everything the skill needs to know about one issue, from fetched JSON alone.

    `trusted` is who can write to the repo: their markers count, and (with the
    issue author) their keywords count.
    """
    plan_kind = next((k for l, k in (("plan:slice", "slice"), ("plan:subtask", "subtask"), (DEBT_LABEL, "debt"))
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
    L, last_marked, marked_ids = read_ledger(ordered, trusted)
    latest_doc, approved_rev, approval_by, rounds = L.latest_doc, L.approved_rev, L.approval_by, L.rounds
    latest_plan, plan_verdict, plan_rounds, plan_body = L.latest_plan, L.plan_verdict, L.plan_rounds, L.plan_body
    demos, latest_demo, request = L.demos, L.latest_demo, L.request
    feedback, feedback_plan, plan_after_created = L.feedback, L.feedback_plan, L.plan_after_created
    created, cleanups = L.created, L.cleanups
    shipped, ship_reviews, epic_tests = L.shipped, L.ship_reviews, L.epic_tests
    untested = L.untested

    replies = [c for c in ordered[last_marked + 1:] if c.get("id") not in marked_ids]
    allowed = trusted | {issue.get("author", "")}
    if request:   # what the person who ran the steps said, since the request (a later agent comment doesn't hide it)
        request["replies"] = [{"id": c.get("id"), "by": c.get("author"), "url": c.get("url")}
                              for c in ordered[request["index"] + 1:]
                              if c.get("id") not in marked_ids and c.get("author") in allowed]
    decision: dict[str, Any] | None = None
    ignored: list[dict[str, str]] = []
    for c in replies:
        for action, text in keyword_lines(c.get("body", "")):
            if c.get("author") in allowed:
                decision = {"action": action, "by": c.get("author"), "text": text, "comment_id": c.get("id")}
            else:
                ignored.append({"by": c.get("author", ""), "action": action})
    if decision and decision["action"] == "approve":
        if state == "demo-review":
            ok_state = latest_demo is not None
            if latest_demo:
                decision.update(milestone=latest_demo["milestone"], rev=latest_demo["rev"])
        else:
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
    if state == "demo-review" and not latest_demo:
        conflicts.append(f"{PREFIX}demo-review but no milestone demo has been posted")

    try:
        latest_plan_json = extract_plan(plan_body) if plan_body else {}
    except SdlcError:
        latest_plan_json = {}
    planned_ms = [m.get("key") for m in latest_plan_json.get("milestones") or []]
    created_ms = set(planned_ms) if created is None else created
    uncreated = [m for m in planned_ms if m not in created_ms] if latest_plan else []
    last_created = next((m for m in reversed(planned_ms) if m in created_ms), None)
    # the latest created milestone is accepted and the plan has more: replan, then create the next one
    next_due = bool(uncreated and last_created and (demos.get(last_created) or {}).get("accepted"))

    if issue.get("state", "open").lower() == "closed":
        turn, action = "none", "none"
    elif conflicts:
        turn, action = "human", "fix_conflict"
    elif state == "escalated":
        turn, action = "human", "human"
    elif state in LATER_STATES:
        turn, action = "later", "none"
    elif state in BUILD_STATES and (feedback or next_due or plan_after_created):
        # the plan is being amended: the poster's changes at a demo, or the next milestone's replan
        turn = "agent"
        if feedback and not feedback_plan:
            action = "plan"
        elif not plan_after_created:
            action = "plan_next_milestone"
        elif plan_verdict == "approve":
            action = "create_plan_issues"
        elif plan_rounds >= max_plan_rounds:
            action = "escalate_plan"   # the epic is being built: a human settles it, not a question to the poster
        else:
            action = "continue_plan"
    elif state in BUILD_STATES and request:
        # a demo step that writes to a speaker, asked for on the issue: whoever runs it replies there
        turn, action = ("agent", "demo_heard") if request["replies"] else ("poster", "wait_for_poster")
    elif state in BUILD_STATES:
        turn, action = "agent", "work_slices"   # whether a slice is ready needs GitHub: see epic_progress
    elif state == "demo-review":
        if not replies:
            turn, action = "poster", "wait_for_poster"
        elif decision and decision["action"] == "approve" and decision.get("valid"):
            turn, action = "agent", "record_demo_acceptance"
        else:
            turn, action = "agent", "demo_reply"
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
        "demos": demos, "latest_demo": latest_demo, "feedback": feedback,
        "shipped": shipped, "ship_reviews": ship_reviews, "epic_tests": epic_tests, "untested": untested,
        "demo_request": request, "cleanups": sorted(cleanups),
        "planned_milestones": planned_ms, "created_milestones": sorted(created_ms, key=natural_key),
        "uncreated_milestones": uncreated, "create_mode": latest_plan_json.get("create") or config.get("create", "milestone"),
    }


def next_command(st: dict[str, Any], progress: dict[str, Any] | None = None) -> str:
    """The one thing to run next for this issue, so no session has to remember the workflow.

    `progress` (from epic_progress) is needed for an epic whose slices are being built.
    """
    n, a = st["number"], st["action"]
    if st.get("plan_kind") == "debt":
        return f"nothing: #{n} is tech debt, planned by its epic's cleanup pass (remove `{DEBT_LABEL}` to triage it alone)"
    if st.get("plan_kind"):
        return f"`uv run python tools/sdlc.py slice-status {n}`"
    if a in TRIAGE_ACTIONS:
        return f"/triage-issue {n}"
    if a in DEMO_ACTIONS:
        return f"/milestone-demo {n}"
    if a == "escalate_plan":
        return (f"/plan-issue {n}: the Codex rounds on this amendment are spent; escalate it "
                f"(`transition {n} escalated --kind escalation --reason ...`)")
    if a == "plan_next_milestone":
        if progress is not None:
            return epic_view(st, progress)["next"]
        return f"/plan-issue {n}: plan {st['uncreated_milestones'][0]} with what earlier milestones taught"
    if a in PLAN_ACTIONS:
        return f"/plan-issue {n}"
    if a == "wait_for_poster":
        return f"nothing: waiting on the poster to reply on #{n}"
    if a in ("human", "fix_conflict"):
        return f"nothing for an agent: #{n} needs a human" + (f" ({'; '.join(st['conflicts'])})" if st["conflicts"] else "")
    if a == "work_slices":
        if progress is None:
            return f"`uv run python tools/sdlc.py state {n}` (needs the slices' progress)"
        return epic_view(st, progress)["next"]
    if st["state"] in LATER_STATES:
        return f"nothing: #{n} is {st['state']}"
    return "nothing: the issue is closed" if a == "none" else f"? (action {a})"


def milestone_key(title: str | None) -> str:
    """`#12 M1: See every alarm` -> `M1`; slices with no milestone share one demo, `all`."""
    m = re.match(r"#\d+\s+([^:\s]+):", title or "")
    return m.group(1) if m else NO_MILESTONE


def done_phrase(m: dict[str, Any]) -> str:
    return "every unit of work is merged" if m["key"] == NO_MILESTONE else f"{m['title']} is complete"


# --- the epic view: one answer to "where is this epic, and what may happen now?" ------
#
# Every gate (claim, merge, ship) and `next` read this view, so they can't disagree.
# It joins the ledger (derive_state: demos, releases, cleanup passes) with the
# slices' progress on GitHub. A milestone moves through MILESTONE_FLOW; the epic
# works on one milestone at a time, the earliest one not shipped, and what's due
# for it decides the next command.

MILESTONE_FLOW = {
    "uncreated": {"building"},              # plan-create makes its issues (after the previous one's demo)
    "building": {"complete"},               # its slices merge into epic/N
    "complete": {"demoed", "building"},     # demo-post; or the agent's demo finds it broken (fix slices)
    "demoed": {"accepted", "building"},     # the poster's /approve; or /changes (fix slices)
    "accepted": {"shipped", "building"},    # ship; or a revert / later findings void the acceptance
    "shipped": set(),
}


def milestone_phases(st: dict[str, Any], progress: dict[str, Any]) -> list[dict[str, Any]]:
    """Each milestone, in order, with its phase (a key of MILESTONE_FLOW). Planned but uncreated ones included."""
    demos, shipped = st.get("demos") or {}, st.get("shipped") or {}
    known = {m["key"]: m for m in progress.get("milestones", [])}
    order = list(known) + [k for k in st.get("planned_milestones") or [] if k not in known]
    out = []
    for k in order:
        m = known.get(k) or {"key": k, "title": f"#{st['number']} {k}", "done": 0, "total": 0}
        d = demos.get(k) or {}
        if k in shipped:
            phase = "shipped"
        elif k not in known:
            phase = "uncreated"
        elif not (m["total"] and m["done"] == m["total"]):
            phase = "building"           # even if accepted: a reopened slice (a revert) is being built again
        elif d.get("accepted"):
            phase = "accepted"
        elif d and not d.get("voided") and not d.get("changes"):
            phase = "demoed"
        else:
            phase = "complete"
        out.append({**m, "phase": phase})
    return out


def cleanup_owed(st: dict[str, Any], progress: dict[str, Any], phases: list[dict[str, Any]],
                 cur: dict[str, Any]) -> str | None:
    """The shipped milestone just before `cur` whose budgeted tech-debt cleanup isn't planned yet.

    Only with open debt and a budget; after the last milestone there's nowhere to put it, so it stays filed.
    """
    i = phases.index(cur)
    prev = phases[i - 1] if i else None
    if prev and prev["phase"] == "shipped" and progress.get("debt") and progress.get("cleanup_budget") \
            and prev["key"] not in (st.get("cleanups") or []):
        return prev["key"]
    return None


def offered_slices(progress: dict[str, Any], cur: dict[str, Any] | None) -> list[int]:
    """The ready slices the epic may start now: the current milestone's, its cleanup pass first
    (and whatever open work a cleanup slice itself waits on)."""
    if not cur:
        return []
    units = progress.get("units") or {}
    mine = [r for r in progress["ready"] if (units.get(r) or {}).get("milestone", cur["key"]) == cur["key"]]
    cleanup = [n for n in progress.get("cleanup_open") or [] if (units.get(n) or {}).get("milestone", cur["key"]) == cur["key"]]
    if cleanup:
        allowed = set(cleanup) | set(progress.get("cleanup_needs") or [])
        mine = [r for r in mine if r in allowed]
    return mine


def current_cleanup(progress: dict[str, Any], cur: dict[str, Any] | None) -> list[int]:
    units = progress.get("units") or {}
    return [n for n in progress.get("cleanup_open") or []
            if cur and (units.get(n) or {}).get("milestone", cur["key"]) == cur["key"]]


def epic_view(st: dict[str, Any], progress: dict[str, Any]) -> dict[str, Any]:
    """{phases, current, due, cleanup, offered, next}: due is one of verify, done, plan_next, demo, ship,
    cleanup, sync, build."""
    n = st["number"]
    phases = milestone_phases(st, progress)
    cur = next((m for m in phases if m["phase"] != "shipped"), None)
    offered = offered_slices(progress, cur)
    cleanup = cleanup_owed(st, progress, phases, cur) if cur and cur["phase"] == "building" else None
    behind = progress.get("behind_main")
    units = progress.get("units") or {}
    if st.get("untested"):
        due = "verify"
        nxt = (f"`uv run python tools/sdlc.py verify-main {n}`: {', '.join(st['untested'])} reached main with commits "
               "no recorded run saw; run the suite on main first")
    elif cur is None:
        due = "done"
        nxt = (f"`uv run python tools/sdlc.py ship {n}`: every milestone has shipped; it finishes the epic" if phases
               else f"/milestone-demo {n}: every unit of work is merged")
    elif cur["phase"] == "uncreated":
        due = "plan_next"
        debt = progress.get("debt") or []
        nxt = (f"/plan-issue {n}: plan {cur['key']} with what earlier milestones taught"
               + (f" (and up to {progress['cleanup_budget']} of its {len(debt)} tech-debt issue(s))"
                  if debt and progress.get("cleanup_budget") else ""))
    elif cur["phase"] in ("complete", "demoed"):
        due = "demo"
        nxt = f"/milestone-demo {n}: {done_phrase(cur)}, and new slices wait for its demo"
    elif cur["phase"] == "accepted":
        due = "ship"
        later = any(m["phase"] == "uncreated" for m in phases)
        nxt = f"/milestone-demo {n}: {cur['key']} is accepted; ship it to main" + (
            ", then plan the next milestone" if later and st.get("action") == "plan_next_milestone" else " (new slices wait)")
    elif cleanup:
        due = "cleanup"
        nxt = (f"/plan-issue {n} --cleanup: {cleanup} is accepted and #{n} has {len(progress['debt'])} open tech-debt "
               f"issue(s); plan up to {progress['cleanup_budget']} before the next milestone's slices")
    elif behind and not offered and not progress["in_flight"]:
        due = "sync"
        nxt = f"`uv run python tools/sdlc.py sync {n}`: {epic_branch(n)} is {behind} commit(s) behind main"
    else:
        due = "build"
        if offered:
            u = units.get(offered[0]) or {}
            nxt = f"/work-slice {offered[0]}" + (f" ({u['model']}: {u['complexity']})" if u.get("model") else "")
        elif progress["in_flight"]:
            nxt = ("nothing new to start: in progress " + ", ".join(f"#{x}" for x in progress["in_flight"])
                   + f" (`/work-slice {progress['in_flight'][0]}` resumes one)")
        elif progress["ready"]:
            nxt = (f"nothing ready in {cur['title']}: later milestones' slices wait until it ships "
                   f"(its open units are blocked or escalated)")
        elif progress["escalated"]:
            nxt = "nothing for an agent: escalated " + ", ".join(f"#{x}" for x in progress["escalated"])
        else:
            nxt = f"nothing ready: the open units of work on #{n} are all blocked"
    return {"number": n, "phases": phases, "current": cur, "due": due, "cleanup": cleanup, "offered": offered,
            "cleanup_open": current_cleanup(progress, cur), "cleanup_needs": progress.get("cleanup_needs") or [],
            "next": nxt}


def pause_errors(view: dict[str, Any]) -> list[str]:
    """Why no fresh claim may start at all: main needs verifying, or the current milestone waits for its
    demo or its release."""
    cur, n = view["current"], view["number"]
    if view["due"] == "verify":
        return [f"main holds commits no recorded run saw: `verify-main {n}` first"]
    if view["due"] == "ship":
        return [f"{cur['title']} is accepted but not shipped to main: new slices wait (`/milestone-demo {n}`)"]
    if view["due"] == "demo":
        return [f"{done_phrase(cur)}: new slices wait for its demo (`/milestone-demo {n}`)"]
    return []


def claim_errors(view: dict[str, Any], number: int, milestone: str | None, cleanup: bool) -> list[str]:
    """Why a fresh claim of this slice must wait: a paused epic, a later milestone, or the cleanup pass first."""
    errs = pause_errors(view) or merge_errors(view, milestone)
    if not errs and view["due"] == "cleanup" and not cleanup:
        errs.append(f"the cleanup pass after {view['cleanup']} is owed: `/plan-issue {view['number']} --cleanup` first")
    held = [x for x in view["cleanup_open"] if x != number]
    if not errs and held and not cleanup and number not in view["cleanup_needs"]:
        errs.append("the cleanup pass comes first: " + ", ".join(f"#{x}" for x in held))
    return errs


def merge_errors(view: dict[str, Any], milestone: str | None) -> list[str]:
    """A slice may land on epic/N only if it belongs to the current milestone (the earliest not shipped)."""
    cur = view["current"]
    if cur and milestone_key(milestone) != cur["key"]:
        return [f"{milestone_key(milestone)} waits: {cur['title']} is still being built, shown or shipped, "
                f"and only its slices land on {epic_branch(view['number'])} until it ships"]
    return []


def due_milestone(demos: dict[str, dict[str, Any]], progress: dict[str, Any],
                  shipped: dict[str, str] | None = None) -> dict[str, Any] | None:
    """The current milestone when it waits for its demo, or (`ship`: True) for its release. Else None."""
    v = epic_view({"number": 0, "demos": demos, "shipped": shipped or {}}, progress)
    return {**v["current"], "ship": v["due"] == "ship"} if v["due"] in ("demo", "ship") else None


def current_milestone(epic: dict[str, Any], progress: dict[str, Any] | None) -> dict[str, Any] | None:
    return epic_view(epic, progress)["current"] if progress else None


def cleanup_due(st: dict[str, Any], progress: dict[str, Any]) -> str | None:
    return epic_view(st, progress)["cleanup"]


def claim_pause_errors(epic: dict[str, Any], progress: dict[str, Any] | None) -> list[str]:
    """Why no new slice of this epic may be claimed now: a milestone demo or release comes first. `--resume` skips this."""
    n = epic["number"]
    if epic.get("state") == "demo-review":
        d = epic.get("latest_demo") or {}
        return [f"epic #{n} is in demo review ({d.get('milestone', '?')}): new slices wait for the poster's answer"]
    return pause_errors(epic_view(epic, progress)) if progress else []


def milestone_hold_errors(epic: dict[str, Any], progress: dict[str, Any] | None, slice_milestone: str | None) -> list[str]:
    """A slice of a later milestone may not land on epic/N until every earlier milestone has shipped."""
    return merge_errors(epic_view(epic, progress), slice_milestone) if progress else []


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
    "demo": "milestone demo rev {rev}",
    "demo-approval": "milestone accepted",
    "demo-changes": "changes requested",
    "demo-void": "demo voided",
    "demo-request": "demo steps for you to run",
    "epic-branch": "epic branch",
    "epic-tests": "test run on the epic branch",
    "ship-review": "Codex review of the milestone",
    "shipped": "shipped to main",
    "revert": "reverted",
    "main-tests": "test run on main",
    "retry": "retried on a stronger model",
}
FOOTERS = {
    "question": "Reply in a comment. Anything you leave unanswered, I will assume the stated default.",
    "prd": "Reply `/approve` to sign off on this revision, `/changes <what>` to ask for changes, or just comment.",
    "diagnosis": "Reply `/approve` to sign off on this diagnosis, `/changes <what>` to ask for changes, or just comment.",
    "demo": ("Reply `/approve` to accept this milestone, `/changes <what>` to ask for something different, "
             "or just ask a question."),
    "demo-request": ("Run them when you can, then reply here with what you heard and saw, in your own words. "
                     "The demo continues from your reply."),
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
    extra = {k: v for k, v in mk.items() if k not in ("kind", "rev", "superseded")}   # e.g. a demo's milestone
    inner = MARKER_RE.sub("", old_body, count=1).strip()
    return (f"{marker(kind, rev, **extra, superseded=new_rev)}\n> Superseded by [rev {new_rev}]({new_url}).\n\n"
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
    for d in (st.get("demos") or {}).values():
        print(f"  demo of {d['milestone']}: rev {d['rev']}   "
              + ("accepted" if d["accepted"] else "changes asked" if d["changes"] else "awaiting the poster"))
    for k, sha in (st.get("shipped") or {}).items():
        print(f"  {k} shipped to main ({sha[:10]})")
    if st.get("feedback"):
        print("  demo feedback to plan: the poster asked for changes")
    if st.get("latest_plan"):
        print(f"  plan: rev {st['latest_plan']['rev']}   codex: {st['plan_verdict'] or 'not yet'}"
              f"   plan rounds: {st['plan_rounds']}/{st['max_plan_rounds']}")
    for m in (st.get("progress") or {}).get("milestones", []):
        print(f"  milestone {m['title']}: {m['done']}/{m['total']} merged" + ("  (complete)" if m["done"] == m["total"] else ""))
    if (st.get("progress") or {}).get("debt"):
        print("  open tech debt: " + ", ".join(f"#{d}" for d in st["progress"]["debt"]))
    if st.get("demo_request"):
        r = st["demo_request"]
        print(f"  demo steps asked of a person for {r['milestone']}: {r['url']}"
              + (f" (answered by {', '.join(x['by'] for x in r['replies'])})" if r["replies"] else " (no answer yet)"))
    if (st.get("progress") or {}).get("behind_main"):
        print(f"  {epic_branch(st['number'])} is {st['progress']['behind_main']} commit(s) behind main")
    print(f"  next: {st.get('next')}")


def command_bootstrap_labels(args: argparse.Namespace, config: dict[str, Any]) -> int:
    repo = repo_of(config)
    for l in config["labels"] + config.get("plan_labels", []) + config.get("extra_labels", []):
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
    if st["action"] in ("work_slices", "plan_next_milestone") and not offline:
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
        if any(l["name"] in ("plan:slice", "plan:subtask", DEBT_LABEL) for l in raw.get("labels", [])):
            continue   # an epic's units of work, or its tech debt: reached through the epic's `next`
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
# How hard a unit of work is, rated by the planner; `.sdlc/config.json` maps it to the model that builds it.
COMPLEXITY = ("routine", "judgment", "novel")
COMPLEXITY_PREFIX = "complexity:"
COMPLEXITY_LINE_RE = re.compile(r"^Complexity: (\w+) — .*$", re.MULTILINE)


def complexity_of(labels: list[str]) -> str | None:
    return next((l[len(COMPLEXITY_PREFIX):] for l in labels if l.startswith(COMPLEXITY_PREFIX)
                 and l[len(COMPLEXITY_PREFIX):] in COMPLEXITY), None)


def model_for(level: str | None, config: dict[str, Any]) -> str | None:
    return (config.get("models") or {}).get(level) if level else None


def complexity_line(level: str, reason: str) -> str:
    return f"Complexity: {level} — {' '.join(reason.split())}"


def annotate_body(body: str, level: str, reason: str) -> str:
    """A slice body with its `Complexity:` line set (after the `Epic:` line), touching nothing else."""
    line = complexity_line(level, reason)
    if COMPLEXITY_LINE_RE.search(body):
        return COMPLEXITY_LINE_RE.sub(lambda m: line, body, count=1)
    lines = body.split("\n")
    at = next((i for i, l in enumerate(lines) if l.startswith("Epic: #")), 0)
    return "\n".join(lines[:at + 1] + [line] + lines[at + 1:])


def natural_key(key: str) -> list[Any]:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", key)]


def plan_keys(plan: dict[str, Any]) -> set[str]:
    """Every subtask and unit-of-work key in a plan (milestones aside)."""
    return {t["key"] for t in plan.get("subtasks", [])} | {l["key"] for l in plan_leaves(plan)}


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
        if l.get("complexity") not in COMPLEXITY:
            errs.append(f"{k} needs a complexity: one of {', '.join(COMPLEXITY)}")
        if not _text(l.get("complexity_reason")):
            errs.append(f"{k} needs a complexity_reason (one line: why that rating)")
        for b in l.get("blocked_by") or []:
            if b == k:
                errs.append(f"{k} is blocked by itself")
            elif b not in leaf_keys:
                errs.append(f"{k} is blocked by {b}, which is not a slice (or a subtask without slices)")
        covers = l.get("covers") or []
        if not all(isinstance(c, int) for c in covers):
            errs.append(f"{k}: covers must be acceptance-criterion numbers")
    for k in mkeys:
        if not any(l.get("_milestone") == k for l in leaves):
            errs.append(f"milestone {k} has no units of work: give it some, or drop it")
    if all(isinstance(k, str) for k in mkeys) and mkeys != sorted(mkeys, key=natural_key):
        errs.append(f"list milestones in order ({', '.join(sorted(mkeys, key=natural_key))}): "
                    "they are built and shipped in that order")
    if (co := plan.get("cleanup_of")) is not None and co not in mkeys:
        errs.append(f"`cleanup_of` names the accepted milestone whose tech debt this pays down, not {co!r}")
    for l in leaves:
        if not all(isinstance(d, int) for d in l.get("debt") or []):
            errs.append(f"{l.get('key')}: debt lists the tech-debt issue numbers it pays down")
    if plan.get("create") not in (None, "milestone", "all"):
        errs.append("`create` is `milestone` (one milestone's issues at a time) or `all`")
    if (lessons := plan.get("lessons")) is not None and not (
            isinstance(lessons, dict) and lessons.get("milestone") in mkeys and _text(lessons.get("text"))):
        errs.append("`lessons` is {\"milestone\": \"M1\", \"text\": \"what it taught, and what changed\"}")
    order = {k: i for i, k in enumerate(mkeys)}
    by_key = {l.get("key"): l for l in leaves}
    for l in leaves:
        for b in l.get("blocked_by") or []:
            if b in by_key and order.get(by_key[b].get("_milestone"), 0) > order.get(l.get("_milestone"), 0):
                errs.append(f"{l['key']} ({l['_milestone']}) is blocked by {b} of a later milestone "
                            f"({by_key[b]['_milestone']}), which may not exist yet when it starts")
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
        if l.get("complexity"):
            bits.append(f"*{l['complexity']}*")
        if l.get("covers"):
            bits.append("AC " + ", ".join(map(str, l["covers"])))
        if l.get("blocked_by"):
            bits.append("after " + ", ".join(f"`{b}`" for b in l["blocked_by"]))
        return " · ".join(bits)

    out = []
    if lessons := plan.get("lessons"):
        out += [f"## Lessons from {lessons['milestone']}", "", lessons["text"].strip(), ""]
    out += [f"**{len(plan['subtasks'])} subtasks, {len(leaves)} units of work** (each one Claude Code session)"
            + (f" in {len(milestones)} milestones" if milestones else "") + "."]
    if milestones and plan.get("create") == "all":
        out.append("Every milestone's issues are created at once (`create: all`).")
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
    if leaf.get("complexity"):
        out.append(complexity_line(leaf["complexity"], leaf.get("complexity_reason", "")))
    for field, name in LEAF_SECTIONS.items():
        v = leaf[field]
        out += ["", f"## {name}", "", "\n".join(f"- [ ] {x}" for x in v) if field == "acceptance" else v.strip()]
    if leaf.get("debt"):
        out += ["", "## Pays down", "", ", ".join(f"#{d}" for d in leaf["debt"])
                + ": tech debt filed against this epic. `merge` closes them with this slice."]
    if leaf.get("blocked_by"):
        refs = ", ".join(f"#{numbers[b]} ({b})" if b in numbers else b for b in leaf["blocked_by"])
        out += ["", "## Dependencies", "", f"Blocked by {refs}. GitHub's blocked-by links are the authority; "
                "this line only explains them."]
    return "\n".join(out) + "\n"


def render_container_body(epic: int, task: dict[str, Any]) -> str:
    return (f"{marker('subtask', epic=str(epic), key=task['key'])}\nEpic: #{epic}\n\n{task['summary'].strip()}\n\n"
            "Its slices are this issue's sub-issues; each is one session of work.\n")


def plan_create_actions(plan: dict[str, Any], existing: dict[str, int], attached: dict[int, set[int]],
                        blocked: dict[int, set[int]], milestones: set[str] | None = None) -> list[tuple[Any, ...]]:
    """What is still missing on GitHub, in the order to do it.

    `existing` maps plan keys to issue numbers already created (found by their
    markers), `attached` parent number -> sub-issue numbers, `blocked` issue
    number -> blocked_by numbers. A rerun after a failure only finishes the job.
    `milestones` limits it to those milestones' issues (None: the whole plan).
    """
    epic = plan["issue"]
    if milestones is not None and plan.get("milestones"):
        plan = {**plan, "milestones": [m for m in plan["milestones"] if m["key"] in milestones],
                "subtasks": [t for t in plan["subtasks"] if t.get("milestone") in milestones]}

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
            if b not in leaves and b not in existing:
                continue   # in a milestone not created yet (plan-validate refuses that direction)
            if not (k in existing and b in existing and existing[b] in blocked.get(existing[k], set())):
                acts.append(("block", k, b))
    return acts if any(a[0] != "milestone" for a in acts) else []


def creation_target(plan: dict[str, Any], st: dict[str, Any], config: dict[str, Any], ask: str | None = None
                    ) -> tuple[set[str] | None, list[str]]:
    """(the milestones whose issues plan-create makes now, the milestones created after it).

    `create: all` (the plan's, else the config's) or a plan without milestones: everything (None).
    Otherwise everything already created plus one more: the first milestone not yet created, when
    nothing is created yet or the latest created one is accepted. `ask` (`--milestone`) names it.
    """
    keys = [m["key"] for m in plan.get("milestones") or []]
    mode = plan.get("create") or config.get("create", "milestone")
    if not keys or mode == "all":
        if ask:
            raise SdlcError("--milestone needs a plan created one milestone at a time")
        return None, keys
    done = set(st.get("created_milestones") or []) & set(keys)
    left = [k for k in keys if k not in done]
    if ask:
        if ask not in keys:
            raise SdlcError(f"the plan has no milestone {ask} (it has {', '.join(keys)})")
        if earlier := [k for k in keys[:keys.index(ask)] if k not in done]:
            raise SdlcError(f"{ask} comes after {', '.join(earlier)}, which aren't created yet")
        nxt = ask
    else:
        last = next((k for k in reversed(keys) if k in done), None)
        accepted = not last or ((st.get("demos") or {}).get(last) or {}).get("accepted")
        nxt = left[0] if left and accepted else None
    make = done | ({nxt} if nxt else set())
    return make, [k for k in keys if k in make]


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
                        "complexity": complexity_of([l["name"] for l in raw.get("labels", [])]),
                        "milestone": (raw.get("milestone") or {}).get("title"), "body": raw.get("body") or ""})
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


def cleanup_budget_errors(plan: dict[str, Any], st: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """The whole plan's debt-paying slices fit the passes so far: `cleanup_slices_per_milestone` per pass
    (the passes already created, plus this one if the plan is one). Counted from the plan, not from one
    revision's additions, so a series of revisions can't add up past it."""
    budget = config.get("cleanup_slices_per_milestone", 1)
    passes = set(st.get("cleanups") or []) | ({plan["cleanup_of"]} if plan.get("cleanup_of") else set())
    debt = [l["key"] for l in plan_leaves(plan) if l.get("debt")]
    if len(debt) > budget * len(passes):
        return [f"the plan has {len(debt)} debt-paying slice(s) ({', '.join(debt)}) but {len(passes)} cleanup pass(es) "
                f"of {budget} (cleanup_slices_per_milestone): leave the rest filed"]
    return []


def lessons_errors(plan: dict[str, Any], st: dict[str, Any]) -> list[str]:
    """Once a milestone exists and the plan still has milestones to create, every revision says what was learnt."""
    created = [k for k in st.get("planned_milestones") or [] if k in (st.get("created_milestones") or [])]
    if not (created and st.get("uncreated_milestones")):
        return []
    lessons = plan.get("lessons") or {}
    if lessons.get("milestone") != created[-1] or not _text(lessons.get("text")):
        return [f"this revision replans after {created[-1]}: it needs `lessons` "
                f"({{\"milestone\": \"{created[-1]}\", \"text\": ...}}), shown as `Lessons from {created[-1]}`"]
    return []


def command_plan_post(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Post a plan revision (validated) for Codex to review. Collapses the previous one."""
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    plan = json.loads(Path(args.file).read_text())
    cleanup = st["action"] == "work_slices" and plan.get("cleanup_of")   # a cleanup pass between milestones
    if st["action"] not in ("plan", "continue_plan", "create_plan_issues", "replan", "plan_next_milestone") and not cleanup:
        raise SdlcError(f"#{st['number']} is {st['state']} (action {st['action']}): not the time to post a plan"
                        + (" — the Codex rounds are spent; ask the poster (`transition N needs-info --kind question`)"
                           if st["action"] == "ask_poster" else ""))
    errs = check_plan(plan)
    if plan.get("issue") != st["number"]:
        errs.append(f"the plan is for #{plan.get('issue')}, not #{st['number']}")
    if st["state"] in BUILD_STATES and st["latest_plan"]:
        # an amendment (demo feedback, the next milestone): the issues already made stay in the plan
        dropped = plan_keys(latest_plan_of(bundle, st)[1]) - plan_keys(plan)
        if dropped:
            errs.append(f"an amended plan keeps every key already created; it drops {', '.join(sorted(dropped, key=natural_key))}")
    errs += cleanup_budget_errors(plan, st, config)
    errs += lessons_errors(plan, st)
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
    if st["action"] not in ("continue_plan", "create_plan_issues", "ask_poster", "escalate_plan") or not st["latest_plan"]:
        raise SdlcError(f"#{st['number']} has no plan under review (state {st['state']}, action {st['action']})")
    if st["plan_rounds"] >= st["max_plan_rounds"]:
        raise SdlcError(f"Codex rounds spent ({st['plan_rounds']}/{st['max_plan_rounds']}) without consensus: "
                        + ("escalate it (`transition N escalated --kind escalation --reason ...`): the epic is being built"
                           if st["state"] in BUILD_STATES else "ask the poster (`transition N needs-info --kind question`)"))
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
    errs += cleanup_budget_errors(plan, st, config)
    if errs:
        raise SdlcError("the reviewed plan no longer validates: " + "; ".join(errs))
    repo = repo_of(config)
    offline = bool(getattr(args, "from_file", None))
    trusted = set(bundle["trusted"])

    target, created_after = creation_target(plan, st, config, getattr(args, "milestone", None))
    if target is not None:
        new = [k for k in created_after if k not in (st.get("created_milestones") or [])]
        print(f"#{epic}: creating {', '.join(new) or 'only new issues in ' + ', '.join(created_after)}"
              + (f"; later: {', '.join(k for k in st.get('planned_milestones') or [] if k not in created_after)}"
                 if len(created_after) < len(plan.get("milestones") or []) else ""))

    def look() -> tuple[dict[str, dict[str, Any]], list[tuple[Any, ...]]]:
        records = bundle.get("plan_issues", []) if offline else fetch_plan_issues(epic, config, trusted)
        attached, blocked = ({}, {}) if offline else fetch_links(epic, records, config)
        by_key = {r["key"]: r for r in records}
        return by_key, plan_create_actions(plan, {k: r["number"] for k, r in by_key.items()}, attached, blocked, target)

    by_key, acts = look()
    numbers: dict[str, Any] = {k: r["number"] for k, r in by_key.items()}
    ids: dict[str, Any] = {k: r["id"] for k, r in by_key.items()}
    items = {t["key"]: t for t in plan["subtasks"]} | {l["key"]: l for l in plan_leaves(plan)}
    in_scope = (lambda k: True) if target is None else (  # noqa: E731
        lambda k: (items[k].get("milestone") or items[k].get("_milestone")) in target)
    path = prd_path(epic)
    prd_url = f"https://github.com/{repo}/blob/{config.get('default_branch', 'main')}/{path.relative_to(ROOT)}" if path else None
    if acts and not offline and not args.dry_run:
        have = {l["name"] for l in gh_json(["label", "list", "--repo", repo, "--limit", "200", "--json", "name"])}
        need = [LEAF_LABEL, CONTAINER_LABEL] + [COMPLEXITY_PREFIX + c for c in COMPLEXITY]
        if missing := [l for l in need if l not in have]:
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
            if not container and item.get("complexity"):
                payload["labels"].append(COMPLEXITY_PREFIX + item["complexity"])
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
        print(f"then post the plan-created table on #{epic}, make {epic_branch(epic)} and move it to {PREFIX}planned")
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
        leaves = [l for l in plan_leaves(plan) if in_scope(l["key"])]
        rows = [f"| `{t['key']}` | #{numbers[t['key']]} | {t['title']} |" for t in plan["subtasks"]
                if t.get("slices") and in_scope(t["key"])]
        rows += [f"| `{k}` | #{numbers[k]} | {items[k]['title']} |" for k in topo_order(leaves)]
        first = [f"#{numbers[l['key']]}" for l in leaves if not l.get("blocked_by")]
        later = [k for k in st.get("planned_milestones") or [] if k not in created_after]
        body = (f"Created from [plan rev {rev}]({plan_comment.get('url', '')}) after Codex approved it.\n\n"
                "| key | issue | title |\n|---|---|---|\n" + "\n".join(rows) +
                f"\n\nReady to start: {', '.join(first) or 'see `ready`'}. Each is one session of work (`work-slice`)."
                + (f"\n\n{', '.join(later)} will be planned again with what {created_after[-1]} teaches, "
                   "then created." if later and created_after else ""))
        extra = {"created": ",".join(created_after)} if target is not None else {}
        if plan.get("cleanup_of") and plan["cleanup_of"] not in (st.get("cleanups") or []):
            extra["cleanup"] = plan["cleanup_of"]
        post_comment(epic, render_comment("plan-created", rev, body, config, **extra), config)
    if st["state"] in BUILD_STATES:   # demo feedback or the next milestone: the epic is still being built
        if not offline:
            ensure_epic_branch(epic, config)
        print(f"#{epic}: the new issues are created; it stays {PREFIX}{st['state']}")
        return 0
    if not offline:
        sha, created = ensure_epic_branch(epic, config)
        print(f"#{epic}: {'created' if created else 'adopted'} {epic_branch(epic)} at {sha[:12]}")
    set_state_label(epic, bundle["issue"]["labels"], "planned", config)
    print(f"#{epic}: approved -> planned ({len(numbers)} issues)")
    return 0


def annotate_actions(records: list[dict[str, Any]], ratings: dict[str, tuple[str, str]]) -> list[dict[str, Any]]:
    """What `plan-annotate` changes: per slice, the complexity label and body line it lacks. Idempotent."""
    out = []
    for r in records:
        if r["kind"] != "slice" or r["key"] not in ratings:
            continue
        level, reason = ratings[r["key"]]
        have = [l for l in r.get("labels", []) if l.startswith(COMPLEXITY_PREFIX)]
        body = annotate_body(r.get("body", ""), level, reason)
        add = [] if COMPLEXITY_PREFIX + level in have else [COMPLEXITY_PREFIX + level]
        drop = [l for l in have if l != COMPLEXITY_PREFIX + level]
        if add or drop or body != r.get("body", ""):
            out.append({"number": r["number"], "key": r["key"], "level": level, "add": add, "drop": drop,
                        "body": body if body != r.get("body", "") else None})
    return out


def plan_ratings(plan: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {l["key"]: (l["complexity"], l.get("complexity_reason", "")) for l in plan_leaves(plan)
            if l.get("complexity") in COMPLEXITY}


def command_plan_annotate(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Backfill complexity onto an epic's existing slices: the label and the body line, nothing else."""
    trusted = fetch_trusted(config)
    if args.map:
        raw = json.loads(Path(args.map).read_text())
        ratings = {k: (v[0], v[1]) for k, v in raw.items()}
    else:
        bundle = fetch_bundle(args.number, config, trusted)
        ratings = plan_ratings(latest_plan_of(bundle, bundle_state(bundle, config))[1])
    if bad := [k for k, (lv, why) in ratings.items() if lv not in COMPLEXITY or not why.strip()]:
        raise SdlcError(f"ratings need a level ({', '.join(COMPLEXITY)}) and a reason: {', '.join(bad)}")
    records = fetch_plan_issues(args.number, config, trusted)
    acts = annotate_actions(records, ratings)
    unrated = sorted((r["key"] for r in records if r["kind"] == "slice" and r["key"] not in ratings), key=natural_key)
    repo = repo_of(config)
    for a in acts:
        print(("would " if args.dry_run else "") + f"set #{a['number']} ({a['key']}) to {a['level']}"
              + (" (label)" if a["add"] or a["drop"] else "") + (" (body line)" if a["body"] else ""))
        if args.dry_run:
            continue
        cmd = ["issue", "edit", str(a["number"]), "--repo", repo]
        cmd += [x for l in a["add"] for x in ("--add-label", l)] + [x for l in a["drop"] for x in ("--remove-label", l)]
        if a["body"]:
            with tempfile.NamedTemporaryFile("w", suffix=".md") as fh:
                fh.write(a["body"])
                fh.flush()
                gh(cmd + ["--body-file", fh.name])
        else:
            gh(cmd)
    if not ratings:
        print(f"#{args.number}: the latest plan revision rates nothing: pass `--map ratings.json`")
    elif not acts:
        print(f"#{args.number}: every rated slice already carries its complexity")
    if unrated:
        print("no rating for: " + ", ".join(unrated))
    return 0


def command_ready(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Units of work whose blockers have all closed as completed: what `work-slice` may start."""
    leaves = [r for r in fetch_plan_issues(args.epic, config, fetch_trusted(config)) if r["kind"] == "slice"]
    blockers = {r["number"]: fetch_blockers(r["number"], config) for r in leaves if r["state"] == "open"}
    ready, waiting = ready_leaves(leaves, blockers)
    progress = summarise_progress(leaves, blockers)
    paused: list[str] = []
    if args.epic:
        trusted = fetch_trusted(config)
        paused = claim_pause_errors(bundle_state(fetch_bundle(args.epic, config, trusted), config), progress)
    for r in ready + waiting:
        r["model"] = model_for(r.get("complexity"), config)
    for u in progress["units"].values():
        u["model"] = model_for(u["complexity"], config)
    if args.json:
        strip = lambda rs: [{k: v for k, v in r.items() if k != "body"} for r in rs]  # noqa: E731
        print(json.dumps({"ready": strip(ready), "waiting": strip(waiting), "progress": progress,
                          "paused": paused}, indent=2))
        return 0
    for p in paused:
        print(f"paused: {p}; nothing new can be claimed")
    for r in ready + waiting:
        tag = ("escalated" if r["number"] in progress["escalated"] else "claimed" if r["number"] in progress["in_flight"]
               else "ready" if not r["unmet"] else "waiting")
        print(f"{tag:<9} #{r['number']:<4} {r['title']}" + (f"  [{r['model']}: {r['complexity']}]" if r["model"] else "")
              + (f"  (on {', '.join('#%d' % n for n in r['unmet'])})" if r["unmet"] else ""))
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
DEBT_LABEL = "tech-debt"


def slice_branch(n: int) -> str:
    return f"slice/{n}"


def epic_branch(n: int) -> str:
    """Where an epic's slices land (squashed), and what an accepted milestone ships to main from."""
    return f"epic/{n}"


def slice_epic(issue: dict[str, Any]) -> int | None:
    e = (parse_marker(issue.get("body", "")) or {}).get("epic", "")
    return int(e) if e.isdigit() else None


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
            active = {"branch": mk.get("branch"), "url": c.get("url"), "at": c.get("created_at"), "model": mk.get("model")}
        elif mk.get("kind") == "release":
            active = None
    return active


def pr_records(comments: list[dict[str, Any]], trusted: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(test runs, Codex reviews) recorded on a pull request, oldest first. Reviews before a `retry`
    (a stronger model taking the slice over) are kept, but no longer spend the new attempt's budget."""
    tests, reviews = [], []
    for mk, c in _marked(comments, trusted):
        if mk.get("kind") == "tests":
            tests.append({"sha": mk.get("sha"), "result": mk.get("result"), "passed": mk.get("passed"), "url": c.get("url")})
        elif mk.get("kind") == "pr-review":
            reviews.append({"sha": mk.get("sha"), "verdict": mk.get("verdict"), "round": mk.get("round"), "url": c.get("url")})
        elif mk.get("kind") == "retry":
            for r in reviews:
                r["before_retry"] = True
    return tests, reviews


def changes_rounds(reviews: list[dict[str, Any]]) -> int:
    return sum(1 for r in reviews if r.get("verdict") == "changes" and not r.get("before_retry"))


def retry_errors(issue: dict[str, Any], comments: list[dict[str, Any]], trusted: set[str]) -> list[str]:
    """Why an escalated slice can't be retried on a stronger model (once only)."""
    n = issue["number"]
    if issue.get("state") != "open":
        return [f"#{n} is closed"]
    errs = []
    if ESCALATED_LABEL not in issue.get("labels", []):
        errs.append(f"#{n} isn't escalated: `claim {n}` or `claim {n} --resume`")
    if any(mk.get("kind") == "claim" and mk.get("retry") for mk, _ in _marked(comments, trusted)):
        errs.append(f"#{n} was already retried on a stronger model: it goes to a human now")
    return errs


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
            "complexity": complexity_of(issue.get("labels", [])),
            "claim": claim, "unmet": unmet, "pr": (pr or {}).get("number"), "pr_state": (pr or {}).get("state"),
            "head": (pr or {}).get("headRefOid"), "tests_on_head": t, "review_on_head": r,
            "pr_rounds": changes_rounds(reviews), "next": nxt}


def merge_gate_errors(pr: dict[str, Any], slice_issue: dict[str, Any] | None, tests: list[dict[str, Any]],
                      reviews: list[dict[str, Any]], base: str, behind_by: int | None = 0) -> list[str]:
    """Why the agent may not merge this pull request (empty list = merge).

    `base` is the slice's epic branch (`epic/N`): slices never land on main directly.
    `behind_by` is how many commits the head lacks from it. Being behind is refused
    even when GitHub could squash cleanly: the tests and the review saw the head
    without those commits, and with parallel slices that combination was never
    tested (there is no CI to catch it).
    """
    errs = []
    if pr.get("state") != "OPEN":
        errs.append(f"PR is {pr.get('state')}, not OPEN")
    if pr.get("isDraft"):
        errs.append("PR is a draft")
    if pr.get("baseRefName") != base:
        errs.append(f"PR targets {pr.get('baseRefName')}, not {base}")
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
    if behind_by is None:
        errs.append(f"could not tell whether the head is up to date with {base}")
    elif behind_by:
        errs.append(f"the head is {behind_by} commit(s) behind {base}: rebase onto origin/{base}, "
                    "push with --force-with-lease, then `test-record` and a Codex round on the new head")
    mergeable = pr.get("mergeable")
    if mergeable == "UNKNOWN":
        errs.append("GitHub is still computing mergeability (UNKNOWN): wait a few seconds and run `merge` again")
    elif mergeable != "MERGEABLE":
        errs.append(f"GitHub says the PR conflicts ({mergeable}): rebase onto origin/{base}")
    return errs


def parse_pytest_summary(text: str) -> tuple[int, int]:
    """(passed, failed + errors) from pytest's summary line."""
    tail = "\n".join((text or "").strip().splitlines()[-3:])
    num = lambda word: sum(int(x) for x in re.findall(rf"(\d+) {word}", tail))  # noqa: E731
    return num("passed"), num("failed") + num("errors?")


def is_cleanup(leaf: dict[str, Any]) -> bool:
    return bool(leaf_section(leaf.get("body", ""), "Pays down"))


def summarise_progress(leaves: list[dict[str, Any]], blockers: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    """An epic's units of work: what can start, what is in flight, and each milestone's count."""
    ready, waiting = ready_leaves(leaves, blockers)
    escalated = [l["number"] for l in leaves if l.get("state") == "open" and ESCALATED_LABEL in l.get("labels", [])]
    # cleanup slices (they pay down tech debt) and the open work they wait on: epic_view puts them first
    cleanup_open = [l["number"] for l in leaves if l.get("state", "open") == "open" and is_cleanup(l)]
    needed, todo = set(cleanup_open), list(cleanup_open)
    while todo:
        for b in blockers.get(todo.pop(), []):
            if b.get("state") != "closed" and b["number"] not in needed:
                needed.add(b["number"])
                todo.append(b["number"])
    by_order = lambda l: (natural_key(milestone_key(l.get("milestone"))), natural_key(l["key"]))  # noqa: E731
    free = [l["number"] for l in sorted(ready, key=by_order) if not l.get("assignees") and l["number"] not in escalated]
    in_flight = [l["number"] for l in ready + waiting if l.get("assignees") and l["number"] not in escalated]
    by_ms: dict[str, list[dict[str, Any]]] = {}
    for l in leaves:
        by_ms.setdefault(l.get("milestone") or "(no milestone)", []).append(l)
    milestones = [{"title": t, "key": milestone_key(t), "total": len(ls),
                   "done": sum(1 for l in ls if l.get("state") == "closed" and l.get("state_reason") == "completed")}
                  for t, ls in sorted(by_ms.items(), key=lambda kv: natural_key(milestone_key(kv[0])))]
    return {"ready": free, "in_flight": in_flight, "escalated": escalated,
            "units": {l["number"]: {"key": l["key"], "complexity": l.get("complexity"),
                                    "milestone": milestone_key(l.get("milestone"))} for l in leaves},
            "cleanup_open": cleanup_open, "cleanup_needs": sorted(needed - set(cleanup_open)),
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
             "assignees": [a["login"] for a in raw.get("assignees") or []],
             "milestone": (raw.get("milestone") or {}).get("title")}
    return issue, [norm_comment(c) for c in gh_pages(f"repos/{repo}/issues/{n}/comments")]


def fetch_blockers(n: int, config: dict[str, Any]) -> list[dict[str, Any]]:
    return gh_pages(f"repos/{repo_of(config)}/issues/{n}/dependencies/blocked_by?per_page=100")


PR_FIELDS = "number,state,isDraft,baseRefName,headRefName,headRefOid,mergeable,body,url,mergeCommit,mergedAt"


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
    try:   # how far epic/N lags main (None: no epic branch)
        behind = gh_json(["api", f"repos/{repo_of(config)}/compare/{epic_branch(epic)}...{config.get('default_branch', 'main')}"]
                         ).get("ahead_by")
    except SdlcError:
        behind = None
    progress = summarise_progress(leaves, blockers)
    for u in progress["units"].values():
        u["model"] = model_for(u["complexity"], config)
    return {**progress, "behind_main": behind, "debt": open_debt(epic, config, trusted),
            "cleanup_budget": config.get("cleanup_slices_per_milestone", 1)}


def open_debt(epic: int, config: dict[str, Any], trusted: set[str]) -> list[int]:
    """Open tech-debt issues filed against this epic (`file-issue --debt`)."""
    raw = gh_pages(f"repos/{repo_of(config)}/issues?labels={DEBT_LABEL.replace(':', '%3A')}&state=open&per_page=100")
    return sorted(r["number"] for r in raw if "pull_request" not in r and (r.get("user") or {}).get("login") in trusted
                  and (parse_marker(r.get("body") or "") or {}).get("epic") == str(epic))


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
    st["model"] = model_for(st["complexity"], config)
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"#{st['number']} {st['title']}\n  state: {st['state']}   epic: #{st['epic']}   key: {st['key']}"
          + (f"   complexity: {st['complexity']} (model {st['model']})" if st["complexity"] else ""))
    if st["claim"]:
        print(f"  claimed: {st['claim']['url']} (branch {st['claim']['branch']}"
              + (f", model {st['claim']['model']})" if st["claim"].get("model") else ")"))
    if st["pr"]:
        t, r = st["tests_on_head"], st["review_on_head"]
        print(f"  PR #{st['pr']} ({st['pr_state']}) head {(st['head'] or '')[:12]}: tests {t['result'] if t else 'not run'}, "
              f"Codex {r['verdict'] if r else 'not yet'}, rounds {st['pr_rounds']}/{config.get('max_pr_rounds', 5)}")
    print(f"  next: {st['next']}")
    return 0


def epic_pause_errors(b: dict[str, Any], config: dict[str, Any], resume: bool) -> list[str]:
    """Why a fresh claim of this slice must wait: its epic isn't being built (escalated, done...), main holds an
    unverified release, a demo or release is due, a later milestone waits, or the cleanup pass comes first."""
    epic = (parse_marker(b["issue"].get("body", "")) or {}).get("epic", "")
    if resume or not epic.isdigit():
        return []
    if "epic_state" in b:    # an offline bundle carries the epic's state and progress
        st, progress = b["epic_state"], b.get("epic_progress")
    else:
        trusted = set(b["trusted"])
        st = bundle_state(fetch_bundle(int(epic), config, trusted), config)
        progress = epic_progress(int(epic), config, trusted) if st["state"] in BUILD_STATES else None
    if st["state"] not in BUILD_STATES | {"demo-review"}:
        return [f"epic #{epic} is {st['state']}: no new slices until it's back in progress"]
    if st.get("untested"):
        return [f"main holds a release no recorded run saw ({', '.join(st['untested'])}): `verify-main {epic}` first"]
    errs = claim_pause_errors(st, progress)
    if progress and not errs:
        errs = claim_errors(epic_view(st, progress), b["issue"]["number"], b["issue"].get("milestone"), is_cleanup(b["issue"]))
    return errs


def command_slice_check(args: argparse.Namespace, config: dict[str, Any]) -> int:
    b = json.loads(Path(args.from_file).read_text()) if args.from_file else slice_bundle(args.number, config)
    errs = slice_check_errors(b["issue"], b["blockers"], claim_status(b["comments"], set(b["trusted"])), args.resume)
    errs += epic_pause_errors(b, config, args.resume)
    for e in errs:
        print(f"  - {e}")
    print(f"#{args.number}: " + ("not workable" if errs else "ok to " + ("resume" if args.resume else "start")))
    return 1 if errs else 0


def command_claim(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Claim a ready slice and give it its own worktree on branch slice/N, from origin/epic/E."""
    n = args.number
    b = slice_bundle(n, config)
    claim = claim_status(b["comments"], set(b["trusted"]))
    if args.retry:
        if not args.model:
            raise SdlcError("--retry needs --model: the stronger model taking it over")
        errs = retry_errors(b["issue"], b["comments"], set(b["trusted"]))
        args.resume = True    # the branch, the worktree and the PR carry on
    else:
        errs = slice_check_errors(b["issue"], b["blockers"], claim, args.resume) + epic_pause_errors(b, config, args.resume)
    if errs:
        raise SdlcError("; ".join(errs))
    root, wt, branch = primary_root(), worktree_path(n, config), slice_branch(n)
    epic = int((parse_marker(b["issue"]["body"]) or {})["epic"])
    base = epic_branch(epic)
    if not remote_has(base, root):
        raise SdlcError(f"origin has no {base}: run `uv run python tools/sdlc.py epic-branch {epic}` first")
    if args.dry_run:
        print(f"would {'resume' if args.resume else 'claim'} #{n}: worktree {wt} on {branch} from origin/{base}; epic #{epic}")
        return 0
    git(["fetch", "origin", base], cwd=root)
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
        git(["worktree", "add", "-b", branch, str(wt), f"origin/{base}"], cwd=root)
    if args.retry:
        gh(["issue", "edit", str(n), "--repo", repo_of(config), "--remove-label", ESCALATED_LABEL])
        post_comment(n, render_comment("claim", None, f"Retrying on {args.model}: the slice escalated on a cheaper model. "
                                       f"Carrying on in `{branch}`.", config, branch=branch, base=base, model=args.model,
                                       retry="1"), config)
        if b["pr"] and b["pr"].get("state") == "OPEN":
            post_comment(b["pr"]["number"], render_comment("retry", None, f"{args.model} takes this over; its Codex rounds "
                                                           "start a fresh budget.", config, model=args.model), config)
        gh(["issue", "edit", str(n), "--repo", repo_of(config), "--add-assignee", "@me"], check=False)
    elif not claim:
        model = {"model": args.model} if getattr(args, "model", None) else {}
        post_comment(n, render_comment("claim", None, f"Working on this in `{branch}`, from `{base}`"
                                       + (f", with {args.model}." if model else "."), config,
                                       branch=branch, base=base, **model), config)
        gh(["issue", "edit", str(n), "--repo", repo_of(config), "--add-assignee", "@me"])
    ep = fetch_bundle(epic, config, set(b["trusted"]))["issue"]
    if f"{PREFIX}planned" in ep["labels"]:
        set_state_label(epic, ep["labels"], "in-progress", config)
        print(f"#{epic}: planned -> in-progress")
    print(f"WORKTREE={wt}\nPRIMARY={root}\nBRANCH={branch}\nBASE=origin/{base}")
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
    if dirty := git(["status", "--porcelain"]):
        raise SdlcError("the working tree is not clean, so the run would not be of the pushed head: "
                        + ", ".join(l[3:] for l in dirty.splitlines()[:8])
                        + " (commit and push, or keep scratch files outside the worktree)")
    head = git(["rev-parse", "HEAD"])
    if head != pr["headRefOid"]:
        raise SdlcError(f"HEAD {head[:12]} is not PR #{args.pr}'s head {pr['headRefOid'][:12]}: push, or check out the branch")
    run = run_suite()
    result, passed = run["result"], run["passed"]
    body = suite_body("the PR head", head, run)
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
    # only rounds that asked for changes spend the budget: re-reviewing a rebased head is routine
    if (spent := changes_rounds(reviews)) >= limit:
        raise SdlcError(f"Codex rounds spent ({spent}/{limit} asked for changes): `escalate-slice N --reason ...`")
    report = Path(args.report).read_text()
    verdict = parse_verdict(report)
    m = HEAD_RE.search(report)
    if not m:
        raise SdlcError("the Codex report has no `HEAD: <40-hex sha>` line: it can't show what it reviewed; run it again")
    if m.group(1) != pr["headRefOid"]:
        raise SdlcError(f"Codex reviewed {m.group(1)[:12]} but the PR head is {pr['headRefOid'][:12]}: "
                        "review the current head (record each round before pushing its fixes)")
    response = Path(args.response).read_text().strip() if args.response else ""
    rnd = len(reviews) + 1
    body = (f"**Review {rnd} — Codex on `{pr['headRefOid'][:12]}`: `{verdict}`** "
            f"({changes_rounds(reviews) + (verdict == 'changes')}/{limit} change rounds used)\n\n"
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
    epic = slice_epic(slice_issue) if slice_issue else None
    branch = epic_branch(epic) if epic else "epic/?"
    try:
        behind = gh_json(["api", f"repos/{repo}/compare/{branch}...{pr['headRefOid']}"]).get("behind_by")
    except SdlcError:
        behind = None
    errs = merge_gate_errors(pr, slice_issue, tests, reviews, branch, behind)
    if epic:
        est = bundle_state(fetch_bundle(epic, config, trusted), config)
        errs += milestone_hold_errors(est, epic_progress(epic, config, trusted), slice_issue.get("milestone"))
    if errs:
        raise SdlcError("; ".join(errs))
    n = refs[0]
    if args.dry_run:
        print(f"would squash-merge PR #{args.pr} (closes #{n}) at {pr['headRefOid'][:12]}")
        return 0
    gh(["pr", "merge", str(args.pr), "--repo", repo, "--squash", "--match-head-commit", pr["headRefOid"]])
    gh(["api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{pr['headRefName']}"], check=False)
    # `Closes #N` only acts on the default branch, and slices merge into epic/N: close it here, then check
    issue = fetch_slice(n, config)[0]
    if issue["state"] != "closed":
        gh(["issue", "close", str(n), "--repo", repo, "--reason", "completed", "--comment", f"Merged in #{args.pr}."])
        issue = fetch_slice(n, config)[0]
    if not (issue["state"] == "closed" and issue.get("state_reason") == "completed"):
        raise SdlcError(f"merged PR #{args.pr}, but #{n} reads back {issue['state']} ({issue.get('state_reason')}): "
                        f"close it as completed (`gh issue close {n} --reason completed`)")
    print(f"merged PR #{args.pr} into {branch}; #{n} closed")
    for d in re.findall(r"#(\d+)", leaf_section(issue["body"], "Pays down").split(":")[0]):
        gh(["issue", "close", d, "--repo", repo, "--reason", "completed", "--comment",
            f"Paid down by #{n} (PR #{args.pr})."], check=False)
        print(f"#{d} (tech debt) closed")
    if epic:
        st = with_next(bundle_state(fetch_bundle(epic, config, trusted), config), config, trusted=trusted)
        for m in (st.get("progress") or {}).get("milestones", []):
            if m["done"] == m["total"] and m["key"] not in (st.get("shipped") or {}):
                print(f"milestone {m['title']} is complete" + (": it ships to main next" if (st.get("demos") or {}).get(m["key"], {}).get("accepted")
                                                              else ": its demo comes before any new slice"))
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

# --- epic branches (epic/N) ------------------------------------------------------
#
# Every planned issue gets a branch, epic/N, cut from main. Slice PRs target it
# and land as one squash commit each, so a bad slice is one revert there. The
# milestone demo runs from epic/N, with main as the "before". Only an accepted
# milestone reaches main: `ship` merges epic/N into it through a pull request
# with a merge commit (main requires PRs), after `sync` has merged main into
# epic/N and recorded a passing run on the head, and Codex has approved the
# milestone's diff on that same head (`ship-review`). Once epic/N contains main,
# that head's tree is exactly what the merge commit will hold.

REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{40})")


def remote_has(branch: str, root: Path | None = None) -> bool:
    return bool(git(["ls-remote", "--heads", "origin", branch], cwd=root, check=False))


def is_ancestor(a: str, b: str, cwd: Path | None = None) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=cwd, capture_output=True).returncode == 0


def ensure_epic_branch(n: int, config: dict[str, Any], root: Path | None = None, dry_run: bool = False
                       ) -> tuple[str, bool]:
    """(the head of origin/epic/N, whether it was created now). An existing branch is adopted as it is."""
    root = root or primary_root()
    main, branch = config.get("default_branch", "main"), epic_branch(n)
    git(["fetch", "-q", "origin", main], cwd=root)
    if remote_has(branch, root):
        git(["fetch", "-q", "origin", branch], cwd=root)
        return git(["rev-parse", f"origin/{branch}"], cwd=root), False
    sha = git(["rev-parse", f"origin/{main}"], cwd=root)
    if not dry_run:
        git(["push", "-q", "origin", f"{sha}:refs/heads/{branch}"], cwd=root)
        git(["fetch", "-q", "origin", branch], cwd=root)
    return sha, True


@contextlib.contextmanager
def epic_lock(n: int, config: dict[str, Any], root: Path | None = None):
    """One sync or revert at a time on `.worktrees/epic-N`: they reset, test and push from it."""
    root = root or primary_root()
    lock = root / config.get("worktree_dir", ".worktrees") / f"epic-{n}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SdlcError(f"another sync or revert is using {epic_branch(n)}'s worktree: wait for it, then rerun")
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def epic_worktree(n: int, config: dict[str, Any], root: Path | None = None) -> Path:
    """`.worktrees/epic-N`, detached at a fresh origin/epic/N. It only ever holds what was pushed."""
    root = root or primary_root()
    wt = root / config.get("worktree_dir", ".worktrees") / f"epic-{n}"
    branch, main = epic_branch(n), config.get("default_branch", "main")
    git(["worktree", "prune"], cwd=root)
    git(["fetch", "-q", "origin", branch, main], cwd=root)
    if not wt.exists():
        git(["worktree", "add", "-q", "--detach", str(wt), f"origin/{branch}"], cwd=root)
    else:
        git(["checkout", "-q", "--detach", f"origin/{branch}"], cwd=wt)
        git(["reset", "-q", "--hard", f"origin/{branch}"], cwd=wt)
        git(["clean", "-q", "-fd"], cwd=wt)
    return wt


def run_suite(cwd: Path | None = None) -> dict[str, Any]:
    """`uv run pytest -q` here: {result, passed, failed, tail}."""
    proc = subprocess.run(["uv", "run", "pytest", "-q"], cwd=cwd, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    passed, failed = parse_pytest_summary(out)
    return {"result": "pass" if proc.returncode == 0 else "fail", "passed": passed, "failed": failed,
            "tail": "\n".join(out.strip().splitlines()[-12:])}


def suite_body(what: str, sha: str, run: dict[str, Any]) -> str:
    fence = _fence(run["tail"])
    return (f"`uv run pytest -q` on {what} `{sha[:12]}`: **{run['result']}** ({run['passed']} passed"
            + (f", {run['failed']} failed" if run["failed"] else "")
            + f").\n\n<details><summary>last lines</summary>\n\n{fence}\n{run['tail']}\n{fence}\n\n</details>")


def sync_epic(n: int, config: dict[str, Any], root: Path | None = None, test=run_suite) -> dict[str, Any]:
    """Merge origin/main into epic/N (never a rebase: slice branches are built on it), test, push on a pass.

    Returns {merged, conflict, files, head, main, run, pushed}. A conflict is aborted and nothing is pushed.
    Holds the epic's lock throughout, and pushes exactly the commit it tested.
    """
    with epic_lock(n, config, root):
        return _sync_epic(n, config, root, test)


def _sync_epic(n: int, config: dict[str, Any], root: Path | None, test) -> dict[str, Any]:
    wt = epic_worktree(n, config, root)
    main = f"origin/{config.get('default_branch', 'main')}"
    main_sha = git(["rev-parse", main], cwd=wt)
    out: dict[str, Any] = {"merged": False, "conflict": False, "files": [], "main": main_sha, "pushed": False}
    if not is_ancestor(main_sha, "HEAD", wt):
        proc = subprocess.run(["git", "merge", "--no-edit", "-m", f"Merge main into {epic_branch(n)}", main_sha],
                              cwd=wt, capture_output=True, text=True)
        if proc.returncode != 0:
            out["files"] = git(["diff", "--name-only", "--diff-filter=U"], cwd=wt, check=False).splitlines()
            git(["merge", "--abort"], cwd=wt, check=False)
            return {**out, "conflict": True, "head": git(["rev-parse", "HEAD"], cwd=wt)}
        out["merged"] = True
    out["head"] = git(["rev-parse", "HEAD"], cwd=wt)
    out["run"] = test(wt)
    if out["merged"] and out["run"]["result"] == "pass":
        git(["push", "-q", "origin", f"{out['head']}:refs/heads/{epic_branch(n)}"], cwd=wt)
        out["pushed"] = True
    return out


def reverted_on_main(config: dict[str, Any], root: Path | None = None) -> set[str]:
    """Commits that main has reverted (`This reverts commit <sha>` in its log): still ancestors, no longer there."""
    root = root or primary_root()
    main = config.get("default_branch", "main")
    git(["fetch", "-q", "origin", main], cwd=root)
    return set(REVERTS_RE.findall(git(["log", f"origin/{main}", "--format=%B"], cwd=root)))


def on_main(prs: list[dict[str, Any] | None], reverted: set[str], main: str = "main") -> bool:
    """A milestone built the old way: every slice PR merged into main, and none of them reverted there since.

    Ancestry can't tell: a reverted commit is still an ancestor of main.
    """
    return bool(prs) and all(p and p.get("state") == "MERGED" and p.get("baseRefName") == main
                             and ((p.get("mergeCommit") or {}).get("oid") or "") not in reverted for p in prs)


PICKED_RE = re.compile(r"\(cherry picked from commit ([0-9a-f]{40})\)")


def slice_commits(into_epic: list[dict[str, Any]], leaves: list[dict[str, Any]], config: dict[str, Any]
                  ) -> dict[str, dict[str, Any]]:
    """Squash commit -> slice, from the PRs merged into epic/N (by their slice/S branch) and each finished
    slice's own PR (which, built before epic branches, merged into main)."""
    by_number = {l["number"]: l for l in leaves}
    out = {}
    for pr in into_epic:
        m = re.fullmatch(r"slice/(\d+)", pr.get("headRefName", ""))
        if m and (oid := (pr.get("mergeCommit") or {}).get("oid")) and int(m.group(1)) in by_number:
            out[oid] = by_number[int(m.group(1))]
    for n, pr in milestone_prs(leaves, config).items():
        if oid := ((pr or {}).get("mergeCommit") or {}).get("oid"):
            out.setdefault(oid, by_number[n])
    return out


def unreleased_commits(n: int, config: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    """main..epic/N, each as {sha, parents, body}."""
    out = git(["log", "--format=%H%x1f%P%x1f%B%x1e", f"origin/{config.get('default_branch', 'main')}..origin/{epic_branch(n)}"],
              cwd=root)
    main = f"origin/{config.get('default_branch', 'main')}"
    commits = []
    for rec in out.split("\x1e"):
        if rec.strip():
            sha, parents, body = (rec.strip("\n").split("\x1f") + ["", ""])[:3]
            c = {"sha": sha, "parents": parents.split(), "body": body}
            if len(c["parents"]) > 1:
                # a merge carries nothing of its own only if it is exactly what git would make of its parents,
                # and what it brings in is main's (sync)
                redo = git(["merge-tree", "--write-tree", *c["parents"][:2]], cwd=root, check=False).split("\n")[0]
                c["clean"] = redo == git(["rev-parse", f"{sha}^{{tree}}"], cwd=root)
                c["from_main"] = all(is_ancestor(x, main, root) for x in c["parents"][1:])
            commits.append(c)
    return commits


def carried_slices(commits: list[dict[str, Any]], slice_of: dict[str, dict[str, Any]], allowed: set[str],
                   accepted_merges: set[str] = frozenset()) -> list[str]:
    """What in main..epic/N isn't the work of an accepted milestone. Every commit must trace to a slice: its
    own squash, a cherry-pick of one (`-x`, as the #12 repair re-applied its slices), or a revert of one.
    Merges (`sync` bringing main in) carry nothing new. Read from the commits, not the slice issues: a
    slice whose close failed after its squash is still on the branch."""
    out = []
    for c in commits:
        if len(c["parents"]) > 1:
            if c["sha"] in accepted_merges or any(c["sha"].startswith(a) for a in accepted_merges if len(a) >= 7):
                continue
            if not c.get("clean", False):
                out.append(f"{c['sha'][:12]} (a merge with changes of its own, e.g. a resolved conflict: review it, "
                           "then `ship --accept-merge <sha>`)")
            elif not c.get("from_main", False):
                out.append(f"{c['sha'][:12]} (a merge of something other than main)")
            continue
        m = PICKED_RE.search(c["body"]) or REVERTS_RE.search(c["body"])
        leaf = slice_of.get(c["sha"]) or (slice_of.get(m.group(1)) if m else None)
        if not leaf:
            out.append(f"{c['sha'][:12]} ({c['body'].strip().splitlines()[0][:50] if c['body'].strip() else '?'}: no slice)")
        elif milestone_key(leaf.get("milestone")) not in allowed:
            out.append(f"#{leaf['number']} {leaf['key']} ({milestone_key(leaf.get('milestone'))})")
    return out


def ship_target(st: dict[str, Any], progress: dict[str, Any], key: str | None = None) -> dict[str, Any] | None:
    """The milestone `ship` releases: the current one, once accepted (milestones ship in order).
    None when every created milestone has shipped."""
    if st.get("untested"):
        raise SdlcError(f"main holds a release no recorded run saw ({', '.join(st['untested'])}): "
                        f"`verify-main {st['number']}` before anything else ships")
    if st.get("feedback") or st.get("action") in PLAN_ACTIONS - {"plan_next_milestone"}:
        raise SdlcError(f"#{st['number']} has changes being planned or built (action {st['action']}): "
                        "they come before any release")
    cur = epic_view(st, progress)["current"]
    if not cur or cur["phase"] == "uncreated":
        return None
    if key and cur["key"] != key:
        raise SdlcError(f"{cur['title']} ships first (milestones ship in order)")
    if cur["phase"] != "accepted":
        raise SdlcError(f"{cur['title']} isn't accepted yet: its demo comes first (`/milestone-demo {st['number']}`)"
                        if cur["phase"] != "building" else f"{cur['title']} is not complete ({cur['done']}/{cur['total']} merged)")
    return cur


def ship_gate_errors(st: dict[str, Any], key: str, head: str, synced: bool, carried: list[str]) -> list[str]:
    """Why milestone `key` may not ship epic/N's `head` to main (empty list = ship)."""
    errs = []
    if not synced:
        errs.append(f"{epic_branch(st['number'])} does not contain main: `sync {st['number']}` first")
    t = [x for x in st.get("epic_tests") or [] if x["sha"] == head]
    if not t:
        errs.append(f"no recorded test run on the epic head {head[:12]} (`sync {st['number']}` runs and records it)")
    elif t[-1]["result"] != "pass":
        errs.append(f"the tests failed on the epic head {head[:12]}")
    r = [x for x in st.get("ship_reviews") or [] if x["sha"] == head and x["milestone"] == key]
    if not r:
        errs.append(f"no Codex review of {key} on the epic head {head[:12]} (`ship-review`)")
    elif r[-1]["verdict"] != "approve":
        errs.append(f"Codex's latest review of {key} on {head[:12]} asks for changes")
    if carried:
        errs.append("epic branch carries slices of milestones that aren't accepted: " + ", ".join(carried))
    return errs


def finished(progress: dict[str, Any], demos: dict[str, dict[str, Any]], shipped: dict[str, str],
             uncreated: list[str] | None = None, untested: dict[str, str] | None = None) -> bool:
    """Nothing is left to plan, build, show, release or verify."""
    return not uncreated and not untested and progress["open"] == 0 and all(
        (demos.get(m["key"]) or {}).get("accepted") and m["key"] in shipped for m in progress["milestones"])


def epic_head(n: int, config: dict[str, Any]) -> str:
    return gh_json(["api", f"repos/{repo_of(config)}/branches/{epic_branch(n).replace('/', '%2F')}"])["commit"]["sha"]


def command_epic_branch(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Create epic/N from main if it doesn't exist, or adopt the one that does. Idempotent."""
    n = args.number
    sha, created = ensure_epic_branch(n, config, dry_run=args.dry_run)
    word = ("would create" if args.dry_run else "created") if created else "adopted"
    print(f"#{n}: {word} {epic_branch(n)} at {sha[:12]}")
    if args.dry_run:
        return 0
    bundle = fetch_bundle(n, config)
    if not any((parse_marker(c["body"]) or {}).get("kind") == "epic-branch" for c in bundle["comments"]
               if c["author"] in bundle["trusted"]):
        body = (f"Slices of this issue land on `{epic_branch(n)}` (from `{sha[:12]}`), not on main. "
                "Each accepted milestone ships to main as one merge.")
        post_comment(n, render_comment("epic-branch", None, body, config, sha=sha), config)
    return 0


def escalate_epic(n: int, reason: str, config: dict[str, Any]) -> None:
    bundle = fetch_bundle(n, config)
    post_comment(n, render_comment("escalation", None, f"Needs a human: {reason}", config), config)
    set_state_label(n, bundle["issue"]["labels"], "escalated", config)


def command_sync(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Merge main into epic/N, run the suite on the result, record it on the epic, push on a pass."""
    n, root = args.number, primary_root()
    if not remote_has(epic_branch(n), root):
        raise SdlcError(f"origin has no {epic_branch(n)}: `epic-branch {n}`")
    if args.dry_run:   # reads refs only: the worktree may be in use by a running sync
        main = config.get("default_branch", "main")
        git(["fetch", "-q", "origin", epic_branch(n), main], cwd=root)
        behind = git(["rev-list", "--count", f"origin/{epic_branch(n)}..origin/{main}"], cwd=root)
        print(f"#{n}: {epic_branch(n)} is {behind} commit(s) behind main; would merge, test, record, push")
        return 0
    out = sync_epic(n, config, root)
    if out["conflict"]:
        reason = (f"merging main into `{epic_branch(n)}` conflicts in " + ", ".join(f"`{f}`" for f in out["files"])
                  + ". Nothing was pushed. Resolve it in a merge on the epic branch (never a rebase).")
        escalate_epic(n, reason, config)
        raise SdlcError(reason)
    run = out["run"]
    what = "the epic branch merged with main" if out["merged"] else "the epic branch"
    post_comment(n, render_comment("epic-tests", None, suite_body(what, out["head"], run), config, sha=out["head"],
                                   main=out["main"], result=run["result"], passed=str(run["passed"])), config)
    if run["result"] != "pass":
        escalate_epic(n, f"the suite fails on `{epic_branch(n)}` at `{out['head'][:12]}`"
                         + (" after merging main; nothing was pushed" if out["merged"] else ""), config)
        raise SdlcError(f"tests failed on {out['head'][:12]}: escalated")
    print(f"#{n}: {epic_branch(n)} at {out['head'][:12]} " + ("merged main and pushed" if out["pushed"] else "already had main")
          + f"; tests pass ({run['passed']})")
    return 0


def command_ship_review(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Record one Codex round on the milestone's diff (main...epic/N), bound to the epic head it reviewed."""
    if args.from_file:
        b = json.loads(Path(args.from_file).read_text())
        bundle, progress, head = b, b["progress"], b["epic_head"]
    else:
        bundle = fetch_bundle(args.number, config)
        progress = epic_progress(args.number, config, set(bundle["trusted"]))
        head = epic_head(args.number, config)
    st = bundle_state(bundle, config)
    if not (target := ship_target(st, progress, args.milestone)):
        raise SdlcError(f"#{st['number']}: every milestone has shipped")
    key = target["key"]
    reviews = [r for r in st["ship_reviews"] if r["milestone"] == key]
    limit = config.get("max_pr_rounds", 5)
    if (spent := sum(1 for r in reviews if r["verdict"] == "changes")) >= limit:
        raise SdlcError(f"Codex rounds on {key} spent ({spent}/{limit}): "
                        f"`transition {st['number']} escalated --kind escalation --reason ...`")
    report = Path(args.report).read_text()
    verdict = parse_verdict(report)
    m = HEAD_RE.search(report)
    if not m:
        raise SdlcError("the Codex report has no `HEAD: <40-hex sha>` line: it can't show what it reviewed; run it again")
    if m.group(1) != head:
        raise SdlcError(f"Codex reviewed {m.group(1)[:12]} but {epic_branch(st['number'])} is at {head[:12]}: "
                        "review the current head (record each round before pushing anything)")
    response = Path(args.response).read_text().strip() if args.response else ""
    rnd = len(reviews) + 1
    body = (f"**{key}, review {rnd} — Codex on `{epic_branch(st['number'])}` `{head[:12]}`: `{verdict}`**\n\n"
            f"<details><summary>Codex's review of main...{epic_branch(st['number'])}</summary>\n\n{report.strip()}\n\n</details>")
    if response:
        body += f"\n\n### Claude's response\n\n{response}"
    comment = render_comment("ship-review", None, body, config, milestone=key, sha=head, verdict=verdict, round=str(rnd))
    if len(comment) > COMMENT_LIMIT:
        raise SdlcError(f"the review comment is {len(comment)} characters, over GitHub's limit")
    if args.dry_run:
        print(comment)
        return 0
    posted = post_comment(st["number"], comment, config)
    print(f"#{st['number']} {key} round {rnd}: {verdict}  {posted.get('html_url', '')}")
    return 0


def milestone_prs(leaves: list[dict[str, Any]], config: dict[str, Any]) -> dict[int, dict[str, Any] | None]:
    return {l["number"]: find_slice_pr(l["number"], config) for l in leaves
            if l.get("state") == "closed" and l.get("state_reason") == "completed"}


def find_ship_pr(n: int, config: dict[str, Any], state: str = "open") -> list[dict[str, Any]]:
    return gh_json(["pr", "list", "--repo", repo_of(config), "--head", epic_branch(n), "--base",
                    config.get("default_branch", "main"), "--state", state, "--json", PR_FIELDS + ",title"]) or []


SHIP_TITLE_RE = re.compile(r"\AShip #(\d+) (\S+):")


def unrecorded_ships(merged: list[dict[str, Any]], n: int, shipped: dict[str, str]) -> dict[str, str]:
    """Release PRs that merged without their `shipped` record (an interrupted `ship`): milestone -> merge commit."""
    out = {}
    for pr in merged:
        m = SHIP_TITLE_RE.match(pr.get("title", ""))
        sha = (pr.get("mergeCommit") or {}).get("oid")
        if m and int(m.group(1)) == n and m.group(2) not in shipped and sha:
            out[m.group(2)] = sha
    return out


def release_untested(repo: str, sha: str) -> bool:
    """Whether a release merge holds more than the epic head it merged (main moved before it merged).

    The epic head contained main when it was tested, so a merge of it is that very tree unless main moved."""
    merge = gh_json(["api", f"repos/{repo}/commits/{sha}"])
    parents = [x["sha"] for x in merge.get("parents", [])]
    if len(parents) < 2:
        return True
    head = gh_json(["api", f"repos/{repo}/commits/{parents[1]}"])
    return merge["commit"]["tree"]["sha"] != head["commit"]["tree"]["sha"]


def finish_epic(n: int, labels: list[str], config: dict[str, Any], root: Path) -> None:
    """After the last ship: delete epic/N, drop its worktree, mark the epic done and close it. Safe to rerun."""
    if remote_has(epic_branch(n), root):
        git(["push", "-q", "origin", "--delete", epic_branch(n)], cwd=root)
    wt = root / config.get("worktree_dir", ".worktrees") / f"epic-{n}"
    if wt.exists():
        git(["worktree", "remove", "--force", str(wt)], cwd=root)
    if f"{PREFIX}done" not in labels:
        set_state_label(n, labels, "done", config)
    repo = repo_of(config)
    if gh_json(["issue", "view", str(n), "--repo", repo, "--json", "state"]).get("state") != "CLOSED":
        gh(["issue", "close", str(n), "--repo", repo, "--reason", "completed"])
        if gh_json(["issue", "view", str(n), "--repo", repo, "--json", "state"]).get("state") != "CLOSED":
            raise SdlcError(f"#{n} is done but won't close: close it by hand, or run `ship {n}` again")
    print(f"#{n}: every milestone shipped: done, {epic_branch(n)} deleted")


def command_ship(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Release an accepted milestone to main: epic/N merged through a pull request, with a merge commit."""
    n, repo, root = args.number, repo_of(config), primary_root()
    main = config.get("default_branch", "main")
    bundle = fetch_bundle(n, config)
    trusted = set(bundle["trusted"])
    st = bundle_state(bundle, config)
    if st["conflicts"] or st["state"] not in BUILD_STATES | {"done"}:
        raise SdlcError(f"#{n} is {st['state']}: nothing to ship")
    records = fetch_plan_issues(n, config, trusted)
    leaves = [r for r in records if r["kind"] == "slice"]
    progress = summarise_progress(leaves, {})
    shipped = {**st["shipped"]}
    # a release PR that merged but whose record never got posted (an interrupted run): record it now
    for key, sha in unrecorded_ships(find_ship_pr(n, config, "merged"), n, shipped).items():
        untested = release_untested(repo, sha)
        print(f"#{n}: {key} merged to {main} earlier ({sha[:12]}) without its record" + (": would record it" if args.dry_run else "")
              + (f"; {main} had moved, so it holds untested commits" if untested else ""))
        if not args.dry_run:
            post_comment(n, render_comment("shipped", None, f"**{key}** shipped to `{main}` (`{sha[:12]}`); "
                                           "recorded after an interrupted `ship`."
                                           + (f" **{main} moved before it merged**: run the suite on {main}." if untested else ""),
                                           config, milestone=key, sha=sha, **({"untested": "1"} if untested else {})), config)
            if untested:
                escalate_epic(n, f"{main} moved while {key} shipped: run the suite on {main} (`{sha[:12]}`)", config)
                return 1
        shipped[key] = sha
    st = {**st, "shipped": shipped}
    m = ship_target(st, progress, args.milestone)
    if m is None and st.get("untested"):
        raise SdlcError(f"#{n}: {', '.join(st['untested'])} reached {main} with commits no run saw: "
                        f"`verify-main {n}` before the epic can finish")
    if m is None:
        if not finished(progress, st.get("demos") or {}, shipped, st.get("uncreated_milestones"), st.get("untested")):
            raise SdlcError(f"#{n}: every created milestone has shipped; the rest isn't built yet")
        if args.dry_run:
            print(f"#{n}: every milestone has shipped: would finish the epic (delete {epic_branch(n)}, done)")
        elif (st["state"] != "done" or remote_has(epic_branch(n), root)
              or bundle["issue"].get("state", "open").lower() != "closed"):
            finish_epic(n, bundle["issue"]["labels"], config, root)
        else:
            print(f"#{n}: already done")
        return 0
    key = m["key"]
    mine = [l for l in leaves if (l.get("milestone") or "(no milestone)") == m["title"]]
    prs = milestone_prs(mine, config)
    if on_main([prs.get(l["number"]) for l in mine], reverted_on_main(config, root), main):
        sha = git(["rev-parse", f"origin/{main}"], cwd=root)
        print(f"#{n}: {m['title']} was built straight onto {main} and is all there: nothing to merge")
        body = f"**{m['title']}** was already on `{main}` (built before epic branches): recorded as shipped, no merge."
        extra = {"noop": "1"}
    else:
        if not remote_has(epic_branch(n), root):
            raise SdlcError(f"origin has no {epic_branch(n)}: `epic-branch {n}`")
        git(["fetch", "-q", "origin", main, epic_branch(n)], cwd=root)
        head = git(["rev-parse", f"origin/{epic_branch(n)}"], cwd=root)
        main_sha = git(["rev-parse", f"origin/{main}"], cwd=root)
        allowed = {k for k, d in (st.get("demos") or {}).items() if d.get("accepted") and k not in shipped}
        into_epic = gh_json(["pr", "list", "--repo", repo, "--base", epic_branch(n), "--state", "merged",
                             "--limit", "500", "--json", "number,headRefName,mergeCommit"]) or []
        carried = carried_slices(unreleased_commits(n, config, root), slice_commits(into_epic, leaves, config), allowed,
                                 set(args.accept_merge or []))
        synced = is_ancestor(main_sha, head, root)
        if errs := ship_gate_errors(st, key, head, synced, carried):
            raise SdlcError("; ".join(errs))
        if args.dry_run:
            print(f"would open (or reuse) a pull request {epic_branch(n)} -> {main} and merge {head[:12]} with a merge commit")
            return 0
        open_prs = find_ship_pr(n, config)
        if not open_prs:
            body_pr = (f"Ships **{m['title']}** of #{n} to `{main}`: accepted at its demo, tested and reviewed on "
                       f"`{head[:12]}`.\n\nPart of #{n}. Created by `tools/sdlc.py ship`.")
            gh(["pr", "create", "--repo", repo, "--base", main, "--head", epic_branch(n),
                "--title", f"Ship #{n} {key}: {m['title'].split(': ', 1)[-1]}", "--body", body_pr])
        pr: dict[str, Any] | None = None
        for _ in range(10):
            pr = (find_ship_pr(n, config) or [None])[0]
            if pr and pr.get("mergeable") != "UNKNOWN":
                break
            time.sleep(3)
        if not pr or pr["headRefOid"] != head:
            raise SdlcError(f"the release pull request isn't at {head[:12]}: run `ship {n}` again")
        if pr.get("mergeable") != "MERGEABLE":
            raise SdlcError(f"GitHub says PR #{pr['number']} is {pr.get('mergeable')}: `sync {n}`, then ship again")
        # main must not have moved since the gate: the merge would hold commits the recorded run never saw.
        # Checked again right before merging; GitHub can't hold main still, so the merge is checked after too.
        now = gh_json(["api", f"repos/{repo}/branches/{main}"])["commit"]["sha"]
        if now != main_sha:
            raise SdlcError(f"{main} moved during the ship ({main_sha[:12]} -> {now[:12]}): `sync {n}`, then ship again")
        gh(["pr", "merge", str(pr["number"]), "--repo", repo, "--merge", "--match-head-commit", head])
        merged = gh_json(["pr", "view", str(pr["number"]), "--repo", repo, "--json", "state,mergeCommit"])
        if merged.get("state") != "MERGED":
            raise SdlcError(f"PR #{pr['number']} did not merge")
        sha = merged["mergeCommit"]["oid"]
        body = f"**{m['title']}** shipped to `{main}` in #{pr['number']} (merge `{sha[:12]}` of `{epic_branch(n)}` at `{head[:12]}`)."
        extra = {}
        if release_untested(repo, sha):
            body += (f"\n\n**{main} moved during the merge** (it was `{main_sha[:12]}`), so this merge holds commits the "
                     "recorded test run never saw. Run the suite on main now.")
            extra = {"untested": "1"}
    if args.dry_run:
        print(f"would record {key} as shipped at {sha[:12]}")
        return 0
    shipped[key] = sha
    post_comment(n, render_comment("shipped", None, body, config, milestone=key, sha=sha, **extra), config)
    print(f"#{n}: {key} shipped ({sha[:12]})")
    if extra.get("untested"):
        escalate_epic(n, f"{main} moved while {key} shipped: run the suite on {main} (`{sha[:12]}`)", config)
        return 1
    if finished(summarise_progress(leaves, {}), st.get("demos") or {}, shipped, st.get("uncreated_milestones"),
                st.get("untested")):
        finish_epic(n, bundle["issue"]["labels"], config, root)
        return 0
    st = with_next(bundle_state(fetch_bundle(n, config, trusted), config), config, trusted=trusted)
    print(f"next: {st['next']}")
    return 0


def command_verify_main(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Run the full suite on main when a release reached it untested (main moved during `ship`); record it."""
    n, root = args.number, primary_root()
    main = config.get("default_branch", "main")
    st = bundle_state(fetch_bundle(n, config), config)
    if not st.get("untested"):
        print(f"#{n}: no release is waiting to be verified")
        return 0
    git(["fetch", "-q", "origin", main], cwd=root)
    main_sha = git(["rev-parse", f"origin/{main}"], cwd=root)
    covers = [sha for sha in st["untested"].values() if is_ancestor(sha, main_sha, root)]
    if args.dry_run:
        print(f"would run the suite on {main} at {main_sha[:12]}, covering {', '.join(c[:12] for c in covers) or 'nothing'}")
        return 0
    wt = root / config.get("worktree_dir", ".worktrees") / f"verify-main-{n}"
    git(["worktree", "prune"], cwd=root)
    if wt.exists():
        git(["worktree", "remove", "--force", str(wt)], cwd=root)
    git(["worktree", "add", "-q", "--detach", str(wt), main_sha], cwd=root)
    try:
        run = run_suite(wt)
    finally:
        git(["worktree", "remove", "--force", str(wt)], cwd=root, check=False)
    post_comment(n, render_comment("main-tests", None, suite_body(main, main_sha, run), config, sha=main_sha,
                                   result=run["result"], passed=str(run["passed"]), covers=",".join(covers)), config)
    if run["result"] != "pass":
        escalate_epic(n, f"the suite fails on {main} at `{main_sha[:12]}` after a release", config)
        return 1
    print(f"#{n}: {main} at {main_sha[:12]} passes ({run['passed']}); the release is verified")
    return 0


def revert_slice_errors(issue: dict[str, Any], pr: dict[str, Any] | None, on_epic: bool, on_main_: bool) -> list[str]:
    n = issue["number"]
    errs = []
    if not (issue.get("state") == "closed" and issue.get("state_reason") == "completed"):
        errs.append(f"#{n} isn't merged")
    if not pr or pr.get("state") != "MERGED" or not (pr.get("mergeCommit") or {}).get("oid"):
        errs.append(f"#{n} has no merged pull request")
    elif not on_epic:
        errs.append(f"#{n}'s commit isn't on {epic_branch(slice_epic(issue) or 0)}")
    elif on_main_:
        errs.append(f"#{n} has already shipped to main: revert it there with a pull request, not here")
    return errs


def command_revert_slice(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Undo one merged slice on epic/N (one revert commit, tested), reopen it, and void its milestone's demo.

    Safe to rerun: when the revert is already on epic/N (a run cut short after its push), it only
    finishes the records: reopen, the revert and release markers, the demo voided.
    """
    n, repo, root = args.number, repo_of(config), primary_root()
    issue, comments = fetch_slice(n, config)
    epic = slice_epic(issue)
    if not epic:
        raise SdlcError(f"#{n} is not a slice of an epic")
    pr = find_slice_pr(n, config)
    branch = epic_branch(epic)
    git(["fetch", "-q", "origin", config.get("default_branch", "main"), branch], cwd=root)
    oid = ((pr or {}).get("mergeCommit") or {}).get("oid") or ""
    done = git(["log", f"origin/{branch}", "--format=%H", "-n1", "--grep", f"This reverts commit {oid}"],
               cwd=root, check=False) if oid else ""
    if not done:
        on_epic = bool(oid) and is_ancestor(oid, f"origin/{branch}", root)
        on_main_ = bool(oid) and is_ancestor(oid, f"origin/{config.get('default_branch', 'main')}", root)
        if errs := revert_slice_errors(issue, pr, on_epic, on_main_):
            raise SdlcError("; ".join(errs))
    key = milestone_key(issue.get("milestone"))
    if args.dry_run:
        print((f"{oid[:12]} is already reverted on {branch} ({done[:12]}): would finish the records"
               if done else f"would revert {oid[:12]} (#{n}, PR #{pr['number']}) on {branch}, test, push")
              + f"; reopen #{n}; void {key}'s demo")
        return 0
    bundle = fetch_bundle(epic, config)
    st = bundle_state(bundle, config)

    def void_demo() -> None:   # before anything is published: an acceptance must never outlive its slice
        demo = (st.get("demos") or {}).get(key)
        if demo and not demo.get("voided"):
            post_comment(epic, render_comment("demo-void", None, f"#{n} was reverted, so {key} needs a new demo once "
                                              "it is built again.", config, milestone=key), config)
            demo["voided"] = True

    run = None
    if not done:
        with epic_lock(epic, config, root):
            wt = epic_worktree(epic, config, root)
            proc = subprocess.run(["git", "revert", "--no-edit", oid], cwd=wt, capture_output=True, text=True)
            if proc.returncode != 0:
                git(["revert", "--abort"], cwd=wt, check=False)
                raise SdlcError(f"reverting {oid[:12]} conflicts with later slices: {proc.stderr.strip()[:300]}")
            done = git(["rev-parse", "HEAD"], cwd=wt)
            run = run_suite(wt)
            if run["result"] != "pass":
                raise SdlcError(f"the suite fails after the revert ({run['failed']} failed): nothing pushed")
            void_demo()
            git(["push", "-q", "origin", f"{done}:refs/heads/{branch}"], cwd=wt)
    # the records, each only if missing: a rerun finishes what an interrupted run left
    trusted = fetch_trusted(config)
    marks = [mk for mk, _ in _marked(comments, trusted)]
    if issue.get("state") != "open":
        gh(["issue", "reopen", str(n), "--repo", repo])
    if not any(mk.get("kind") == "revert" and mk.get("pr") == str(pr["number"]) for mk in marks):
        why = f": {args.reason}" if args.reason else "."
        post_comment(n, render_comment("revert", None, f"Reverted on `{branch}` in `{done[:12]}` (PR #{pr['number']}){why} "
                                       "The slice is open again.", config, sha=done, pr=str(pr["number"])), config)
    if claim_status(comments, trusted):
        post_comment(n, render_comment("release", None, "The old claim ends with the revert.", config), config)
    gh(["issue", "edit", str(n), "--repo", repo, "--remove-assignee", "@me"], check=False)
    if run:
        post_comment(epic, render_comment("epic-tests", None, suite_body("the epic branch after a revert", done, run), config,
                                          sha=done, result=run["result"], passed=str(run["passed"])), config)
    void_demo()
    if st["state"] == "demo-review" and (st.get("latest_demo") or {}).get("milestone") == key:
        set_state_label(epic, bundle["issue"]["labels"], "in-progress", config)
    print(f"#{n}: reverted on {branch} at {done[:12]} and reopened\nnext: /work-slice {n}")
    return 0


# --- milestone demos (milestone-demo) -----------------------------------------
#
# When a milestone's units are all merged, the agent runs its demo and posts it
# on the epic for the poster (`kind=demo milestone=M rev=K sha=...`). Its
# pictures are committed to the orphan branch `sdlc-demos` and linked by commit
# SHA, so what the poster saw can't change under them. The repo is public: the
# identifier scan below refuses to publish anything that looks like a device or
# household ID, an address or a secret.

DEMO_BRANCH = "sdlc-demos"
DEMO_EXTS = {".svg", ".md"}   # pictures are demo_shot SVGs, whose text the blank-picture check can read
DEMO_FILE_LIMIT = 2_000_000
IDENTIFIERS = [
    ("an IP address", re.compile(r"\b(?!127\.0\.0\.1\b|0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("a MAC address or serial", re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")),
    ("a Sonos player ID", re.compile(r"RINCON_[0-9A-Fa-f]{12}")),
    ("a Sonos household ID", re.compile(r"\bSonos_[A-Za-z0-9._-]{10,}")),
    ("a bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{10,}")),
    ("an API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("a secret-looking hex string", re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{32,39}|[0-9A-Fa-f]{41,})(?![0-9A-Fa-f])")),
]
LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)")
IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.IGNORECASE)


def identifier_hits(text: str) -> list[str]:
    """What in `text` must never reach a public page: 'an IP address (192.168.…)'."""
    hits = []
    for name, rx in IDENTIFIERS:
        for m in rx.finditer(text):
            hits.append(f"{name} ({m.group(0)[:10]}…)")
    return hits


def _relative(target: str) -> bool:
    return not re.match(r"^(?:[a-z][a-z0-9+.-]*:|#|/)", target, re.IGNORECASE)


def demo_links(body: str) -> list[str]:
    """The files a demo comment points at relative to its folder (images and links)."""
    targets = [m.group(3) for m in LINK_RE.finditer(body)] + [m.group(2) for m in IMG_SRC_RE.finditer(body)]
    return [t for t in targets if _relative(t)]


def rewrite_demo_links(body: str, raw_base: str, blob_base: str) -> str:
    """Relative images -> raw file URLs (shown inline); relative links -> the file's page on GitHub."""
    def link(m: re.Match[str]) -> str:
        bang, text, target = m.groups()
        if not _relative(target):
            return m.group(0)
        return f"{bang}[{text}]({(raw_base if bang else blob_base)}/{target})"

    def src(m: re.Match[str]) -> str:
        return m.group(1) + (f"{raw_base}/{m.group(2)}" if _relative(m.group(2)) else m.group(2)) + m.group(3)

    return IMG_SRC_RE.sub(src, LINK_RE.sub(link, body))


SVG_TEXT_RE = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.DOTALL)
SVG_MIN_TEXT = 40   # a command line alone is ~25 characters: anything less shows nothing


def svg_text(svg: str) -> str:
    """What a demo_shot picture shows, whitespace removed: its text in reading order, without the window
    title or the `$ command` prompt, however many rows it wraps to (a command that printed nothing is
    still a blank picture). demo_shot titles the window with the command it ran."""
    title, rows = "", {}
    for attrs, t in SVG_TEXT_RE.findall(svg):
        text = html.unescape(re.sub(r"<[^>]+>", "", t))
        if "-title" in attrs:
            title += text
            continue
        y = (re.search(r'\by="([^"]*)"', attrs) or [None, "0"])[1]
        rows[y] = rows.get(y, "") + text
    def at(y: str) -> float:
        try:
            return float(y)
        except ValueError:
            return 0.0
    body = "".join("".join(rows[y].split()) for y in sorted(rows, key=at))
    for prompt in ("$" + "".join(title.split()), "$"):
        if body.startswith(prompt):
            body = body[len(prompt):]
            break
    return body


def demo_post_errors(body: str, folder: Path) -> list[str]:
    """Why this demo may not be published (empty list = fine). Runs before anything is pushed."""
    errs = []
    if not folder.is_dir():
        return [f"{folder} is not a folder"]
    files = sorted(f for f in folder.rglob("*") if f.is_file())
    if not (folder / "README.md").is_file():
        errs.append("the folder needs README.md: the full write-up (what was built, what changed from the plan)")
    for f in files:
        rel = f.relative_to(folder)
        if f.suffix.lower() not in DEMO_EXTS:
            errs.append(f"{rel}: only {', '.join(sorted(DEMO_EXTS))} files are published")
        elif f.stat().st_size > DEMO_FILE_LIMIT:
            errs.append(f"{rel}: {f.stat().st_size} bytes is over {DEMO_FILE_LIMIT}")
        elif f.suffix.lower() in (".svg", ".md"):
            text = f.read_text(errors="replace")
            errs += [f"{rel} contains {h}" for h in identifier_hits(text)]
            if f.suffix.lower() == ".svg" and len(svg_text(text)) < SVG_MIN_TEXT:
                errs.append(f"{rel} shows almost no text ({len(svg_text(text))} characters): a blank or mid-load "
                            "screen, or a command that printed nothing; retake it")
    errs += [f"the comment contains {h}" for h in identifier_hits(body)]
    for t in demo_links(body):
        path = (folder / t.split("#")[0]).resolve()
        if not path.is_relative_to(folder.resolve()) or not path.is_file():
            errs.append(f"the comment points at {t}, which is not in {folder}")
    return errs


def leaf_section(body: str, name: str) -> str:
    """One `## <name>` section of a slice issue's body."""
    m = re.search(rf"^## {re.escape(name)}\s*\n(.*?)(?=^## |\Z)", body or "", re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def demo_brief(epic: int, config: dict[str, Any], trusted: set[str]) -> dict[str, Any]:
    """Everything milestone-demo needs: each milestone's demo steps, slices, PRs and demo state."""
    repo = repo_of(config)
    st = bundle_state(fetch_bundle(epic, config, trusted), config)
    records = fetch_plan_issues(epic, config, trusted)
    leaves = [r for r in records if r["kind"] == "slice"]
    progress = epic_progress(epic, config, trusted)    # the same progress `state` uses, so `next` agrees
    gh_ms = {m["title"]: m for m in gh_pages(f"repos/{repo}/milestones?state=all&per_page=100")}
    main = config.get("default_branch", "main")
    reverted = reverted_on_main(config)
    out = []
    for m in progress["milestones"]:
        slices = []
        for l in sorted((l for l in leaves if (l.get("milestone") or "(no milestone)") == m["title"]),
                        key=lambda l: natural_key(l["key"])):
            pr = find_slice_pr(l["number"], config) if l["state"] == "closed" else None
            slices.append({"number": l["number"], "key": l["key"], "title": l["title"], "state": l["state"],
                           "pr": (pr or {}).get("number"), "merged_at": (pr or {}).get("mergedAt"), "_pr": pr,
                           "merge_commit": ((pr or {}).get("mergeCommit") or {}).get("oid"),
                           "demo": leaf_section(l.get("body", ""), "Demo")})
        merged = sorted((x for x in slices if x["merge_commit"]), key=lambda x: x["merged_at"] or "")
        ghm = gh_ms.get(m["title"]) or {}
        # built the old way (straight onto main): show main against the commit before its first merge
        legacy = on_main([x.pop("_pr") for x in slices], reverted, main)
        before = (f"{merged[0]['merge_commit']}^" if merged else None) if legacy else f"origin/{main}"
        after = f"origin/{main}" if legacy else f"origin/{epic_branch(epic)}"
        out.append({**m, "complete": m["total"] > 0 and m["done"] == m["total"], "milestone_number": ghm.get("number"),
                    "steps": ghm.get("description") or "\n".join(f"- {x['key']}: {x['demo']}" for x in slices if x["demo"]),
                    "slices": slices, "before": before, "after": after, "on_main": legacy,
                    "demo": (st.get("demos") or {}).get(m["key"]), "shipped": (st.get("shipped") or {}).get(m["key"])})
    due = due_milestone(st.get("demos") or {}, progress, st.get("shipped"))
    return {"epic": epic, "title": st["title"], "state": st["state"], "action": st["action"],
            "due": due["key"] if due else None, "milestones": out, "progress": progress,
            "next": next_command(st, progress if st["action"] in ("work_slices", "plan_next_milestone") else None)}


def command_demo_status(args: argparse.Namespace, config: dict[str, Any]) -> int:
    brief = demo_brief(args.number, config, fetch_trusted(config))
    if args.json:
        print(json.dumps(brief, indent=2))
        return 0
    print(f"#{brief['epic']} {brief['title']}\n  state: {brief['state']}   action: {brief['action']}"
          f"   demo due: {brief['due'] or 'none'}")
    for m in brief["milestones"]:
        d = m["demo"]
        print(f"\n{m['key']}  {m['title']}  {m['done']}/{m['total']} merged   demo: "
              + (f"rev {d['rev']} " + ("accepted" if d["accepted"] else "changes asked" if d["changes"] else "in review")
                 if d else "none"))
        print("  demo steps:\n" + "\n".join(f"    {line}" for line in m["steps"].splitlines()))
        for x in m["slices"]:
            print(f"  #{x['number']:<4} {x['key']:<6} {x['state']:<6}"
                  + (f" PR #{x['pr']} ({(x['merge_commit'] or '')[:10]})" if x["pr"] else "") + f"  {x['title']}")
        if m["before"]:
            print(f"  before: {m['before']}   after: {m['after']}" + ("   (built straight onto main)" if m["on_main"] else ""))
        if m["shipped"]:
            print(f"  shipped to main: {m['shipped'][:12]}")
    if debt := (brief["progress"] or {}).get("debt"):
        print("\nopen tech debt: " + ", ".join(f"#{d}" for d in debt))
    print(f"\nnext: {brief['next']}")
    return 0


def publish_demo(src: Path, prefix: str, message: str, config: dict[str, Any], root: Path | None = None) -> str:
    """Commit `src` to the orphan branch under `prefix` and push it; returns the commit SHA.

    Works in its own worktree (`.worktrees/sdlc-demos`), so the primary checkout is never touched.
    """
    root = root or primary_root()
    wt = root / config.get("worktree_dir", ".worktrees") / DEMO_BRANCH
    git(["worktree", "prune"], cwd=root)
    remote = bool(git(["ls-remote", "--heads", "origin", DEMO_BRANCH], cwd=root, check=False))
    if remote:
        git(["fetch", "origin", DEMO_BRANCH], cwd=root)
    if not wt.exists():
        if remote:
            git(["worktree", "add", "-B", DEMO_BRANCH, str(wt), f"origin/{DEMO_BRANCH}"], cwd=root)
        else:
            git(["worktree", "add", "--orphan", "-b", DEMO_BRANCH, str(wt)], cwd=root)
    elif remote:
        git(["reset", "-q", "--hard", f"origin/{DEMO_BRANCH}"], cwd=wt)   # this worktree only holds what was published
    dest = wt / prefix
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    git(["add", prefix], cwd=wt)
    git(["commit", "-q", "-m", message], cwd=wt)
    git(["push", "-q", "origin", DEMO_BRANCH], cwd=wt)
    return git(["rev-parse", "HEAD"], cwd=wt)


def command_demo_post(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Publish a milestone's demo pictures and post the demo on the epic for the poster."""
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    n, key, offline = st["number"], args.milestone, bool(getattr(args, "from_file", None))
    errs = []
    if st["conflicts"] or st["state"] not in BUILD_STATES | {"demo-review"} or st.get("feedback"):
        errs.append(f"#{n} is {st['state']} (action {st['action']}): not the time for a demo")
    elif st["state"] == "demo-review" and (st["latest_demo"] or {}).get("milestone") != key:
        errs.append(f"#{n} is already in review for {st['latest_demo']['milestone']}: that comes first")
    progress = bundle.get("progress") if offline else epic_progress(n, config, set(bundle["trusted"]))
    ms = {m["key"]: m for m in (progress or {}).get("milestones", [])}
    m = ms.get(key)
    if not m:
        errs.append(f"#{n} has no milestone {key} (it has {', '.join(ms) or 'none'})")
    elif m["done"] != m["total"]:
        errs.append(f"{m['title']} is not complete ({m['done']}/{m['total']} merged)")
    folder, body = Path(args.dir), Path(args.body_file).read_text()
    errs += demo_post_errors(body, folder)
    prev = (st.get("demos") or {}).get(key)
    rev = (prev["rev"] if prev else 0) + 1
    prefix = f"epic-{n}/{key}/rev-{rev}"
    repo = repo_of(config)

    def comment_for(sha: str) -> str:
        bases = (f"https://raw.githubusercontent.com/{repo}/{sha}/{prefix}", f"https://github.com/{repo}/blob/{sha}/{prefix}")
        return render_comment("demo", rev, rewrite_demo_links(body, *bases), config, milestone=key, sha=sha)

    if len(comment_for("0" * 40)) > COMMENT_LIMIT:
        errs.append("the demo comment is over GitHub's limit: move detail into README.md")
    if errs:
        raise SdlcError("; ".join(errs))
    if args.dry_run:
        print(f"DRY RUN #{n}: {key} demo rev {rev}; would publish to {DEMO_BRANCH}:{prefix}/")
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                print(f"  {f.relative_to(folder)}  ({f.stat().st_size} bytes)")
        print("----- comment -----")
        print(comment_for("<commit>"))
        return 0
    sha = publish_demo(folder, prefix, f"#{n} {key}: demo rev {rev}", config)
    posted = post_comment(n, comment_for(sha), config)
    if prev:
        old = next(c for c in bundle["comments"] if c["id"] == prev["id"])
        patch_comment(old["id"], render_superseded(old["body"], rev, posted.get("html_url", "")), config)
    if st["state"] != "demo-review":
        set_state_label(n, bundle["issue"]["labels"], "demo-review", config)
    print(f"#{n}: {key} demo rev {rev} ({sha[:10]})  {posted.get('html_url', '')}")
    print(f"next: nothing: waiting on the poster to reply on #{n}")
    return 0


def close_finished_containers(epic: int, title: str | None, config: dict[str, Any], trusted: set[str]) -> None:
    """Close a milestone's subtask issues once every slice under them is closed."""
    repo = repo_of(config)
    for r in fetch_plan_issues(epic, config, trusted):
        if r["kind"] != "subtask" or r["state"] != "open" or (title and r.get("milestone") != title):
            continue
        children = gh_pages(f"repos/{repo}/issues/{r['number']}/sub_issues?per_page=100")
        if children and all(c.get("state") == "closed" for c in children):
            gh(["issue", "close", str(r["number"]), "--repo", repo, "--reason", "completed"])
            print(f"closed #{r['number']} ({r['key']}): its slices are done")


def command_demo_accept(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Record the poster's /approve of a milestone demo. The epic stays in progress: `ship` releases it."""
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    n = st["number"]
    if st["action"] != "record_demo_acceptance":
        raise SdlcError(f"#{n} has no /approve of its latest demo to record (state {st['state']}, action {st['action']})")
    d = st["decision"]
    key, rev = d["milestone"], d["rev"]
    if args.milestone and args.milestone != key:
        raise SdlcError(f"the /approve is for {key}, not {args.milestone}")
    offline = bool(getattr(args, "from_file", None))
    progress = bundle.get("progress") if offline else epic_progress(n, config, set(bundle["trusted"]))
    target = "in-progress"    # the milestone ships to main next (`ship`); the last ship finishes the epic
    title = next((m["title"] for m in progress["milestones"] if m["key"] == key), key)
    body = (f"**{title}** accepted by @{d['by']} (demo rev {rev}). It ships to main next"
            + (", and then this issue is done. Thank you!" if progress["open"] == 0 and not st.get("uncreated_milestones")
               else "; work on the rest carries on."))
    comment = render_comment("demo-approval", rev, body, config, milestone=key, by=d["by"])
    if args.dry_run:
        print(f"DRY RUN #{n}: demo-review -> {target}; then `ship {n}`")
        print(comment)
        return 0
    repo = repo_of(config)
    posted = post_comment(n, comment, config)
    if key != NO_MILESTONE:
        for m in gh_pages(f"repos/{repo}/milestones?state=open&per_page=100"):
            if m["title"] == title:
                gh(["api", "-X", "PATCH", f"repos/{repo}/milestones/{m['number']}", "-f", "state=closed"])
                print(f"closed milestone {title!r}")
    close_finished_containers(n, None if key == NO_MILESTONE else title, config, set(bundle["trusted"]))
    set_state_label(n, bundle["issue"]["labels"], target, config)
    st = with_next(bundle_state(fetch_bundle(n, config), config), config)
    print(f"#{n}: demo-review -> {target}  {posted.get('html_url', '')}\nnext: {st['next']}")
    return 0


def command_demo_changes(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Record what the poster asked to change at a demo; plan-issue turns it into new slices."""
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    n, demo = st["number"], st["latest_demo"]
    if args.found:
        return record_agent_findings(args, bundle, st, config)
    if st["state"] != "demo-review" or not demo or st["action"] not in DEMO_ACTIONS:
        raise SdlcError(f"#{n} has no reply to a demo to act on (state {st['state']}, action {st['action']})")
    if args.milestone and args.milestone != demo["milestone"]:
        raise SdlcError(f"the demo in review is {demo['milestone']}, not {args.milestone}")
    body = Path(args.body_file).read_text().strip()
    body += "\n\nNext: these become new slices in a revised plan (Codex reviews it), then a new demo."
    comment = render_comment("demo-changes", demo["rev"], body, config, milestone=demo["milestone"])
    if args.dry_run:
        print(f"DRY RUN #{n}: demo-review -> in-progress\n{comment}")
        return 0
    posted = post_comment(n, comment, config)
    set_state_label(n, bundle["issue"]["labels"], "in-progress", config)
    print(f"#{n}: demo-review -> in-progress  {posted.get('html_url', '')}\nnext: /plan-issue {n}")
    return 0


def record_agent_findings(args: argparse.Namespace, bundle: dict[str, Any], st: dict[str, Any],
                          config: dict[str, Any]) -> int:
    """The agent's own demo run shows the milestone broken: record it, so plan-issue plans fix slices first.

    No demo is posted for a broken milestone, so this doesn't wait for one (or for the poster).
    """
    n, key = st["number"], args.milestone
    if not key:
        raise SdlcError("--found needs --milestone: the milestone the demo found broken")
    if key in (st.get("shipped") or {}):
        raise SdlcError(f"{key} has shipped to main: file what's wrong with `file-issue {n}` instead")
    if st["conflicts"] or st["state"] not in BUILD_STATES | {"demo-review"}:
        raise SdlcError(f"#{n} is {st['state']}: no milestone is being demoed")
    if st.get("feedback"):
        raise SdlcError(f"#{n} already has changes waiting to be planned: `/plan-issue {n}` first")
    body = Path(args.body_file).read_text().strip()
    if hits := identifier_hits(body):
        raise SdlcError("the findings contain " + ", ".join(hits) + ": the issue is public")
    body = (f"The demo of {key} found it doesn't do what the plan promised, so no demo is posted yet:\n\n{body}"
            "\n\nNext: fix slices (each with an offline test that reproduces the problem), then the demo again.")
    comment = render_comment("demo-changes", None, body, config, milestone=key, found="agent")
    if args.dry_run:
        print(f"DRY RUN #{n}: findings for {key}\n{comment}")
        return 0
    posted = post_comment(n, comment, config)
    if st["state"] == "demo-review":
        set_state_label(n, bundle["issue"]["labels"], "in-progress", config)
    print(f"#{n}: {key} findings recorded  {posted.get('html_url', '')}\nnext: /plan-issue {n}")
    return 0


def command_demo_request(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Ask, on the issue, for the demo steps only a person can run (real speaker writes). Their reply resumes it."""
    bundle = load_bundle(args, config)
    st = bundle_state(bundle, config)
    n = st["number"]
    if st["conflicts"] or st["state"] not in BUILD_STATES:
        raise SdlcError(f"#{n} is {st['state']}: not the time to ask for demo steps")
    if (r := st.get("demo_request")) and not r["replies"]:
        raise SdlcError(f"#{n} already has demo steps waiting for an answer: {r['url']}")
    body = Path(args.body_file).read_text().strip()
    if hits := identifier_hits(body):
        raise SdlcError("the request contains " + ", ".join(hits) + ": the issue is public")
    comment = render_comment("demo-request", None, body, config, milestone=args.milestone)
    if len(comment) > COMMENT_LIMIT:
        raise SdlcError("the request is over GitHub's comment limit")
    if args.dry_run:
        print(comment)
        return 0
    posted = post_comment(n, comment, config)
    print(f"#{n}: demo steps for {args.milestone} asked for  {posted.get('html_url', '')}\n"
          f"next: nothing: waiting on a reply on #{n}")
    return 0


def command_file_issue(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """File something found while building or demoing an epic as its own issue: tech debt, or new for triage."""
    title, body = args.title.strip(), Path(args.body_file).read_text().strip()
    if hits := identifier_hits(title + "\n" + body):
        raise SdlcError("the issue would contain " + ", ".join(hits) + ": the repo is public")
    kind = "debt" if args.debt else "finding"
    lead = (f"Tech debt from #{args.number} ({args.source}): not blocking, budgeted into a cleanup pass after a "
            "milestone is accepted." if args.debt else f"Found while working on #{args.number} ({args.source}).")
    full = f"{marker(kind, None, epic=str(args.number), source=args.source)}\n{lead}\n\n{body}\n"
    cmd = ["issue", "create", "--repo", repo_of(config), "--title", title]
    if args.debt:
        cmd += ["--label", DEBT_LABEL]
    if args.dry_run:
        print(f"would run: gh {' '.join(cmd)} --body-file <body>\n{full}")
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".md") as fh:
        fh.write(full)
        fh.flush()
        url = gh(cmd + ["--body-file", fh.name])
    print(url.strip())
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
    p.add_argument("--milestone", help="create this milestone (default: the next one due; `create: all` makes every one)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-file")
    p.set_defaults(fn=command_plan_create)

    p = sub.add_parser("plan-annotate", help="backfill complexity labels and lines onto an epic's slices")
    p.add_argument("number", type=int)
    p.add_argument("--map", help='{"T1.1": ["routine", "why"], ...} instead of the latest plan revision')
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_plan_annotate)

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
    p.add_argument("--model", help="the model building it (recorded on the claim)")
    p.add_argument("--retry", action="store_true",
                   help="once: an escalated slice taken over by a stronger --model, keeping its branch and PR")
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

    p = sub.add_parser("epic-branch", help="create epic/N from main, or adopt the existing one (idempotent)")
    p.add_argument("number", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_epic_branch)

    p = sub.add_parser("sync", help="merge main into epic/N, run the suite, record it on the epic, push on a pass")
    p.add_argument("number", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_sync)

    p = sub.add_parser("ship-review", help="record a Codex round on a milestone's diff (main...epic/N)")
    p.add_argument("number", type=int)
    p.add_argument("--milestone", help="default: the next one to ship")
    p.add_argument("--report", required=True, help="Codex's stdout: a `HEAD: <sha>` line, last line the verdict")
    p.add_argument("--response", help="Claude's answer to each finding (markdown)")
    p.add_argument("--from-file")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_ship_review)

    p = sub.add_parser("ship", help="release an accepted milestone: merge epic/N into main (a merge commit, via a PR)")
    p.add_argument("number", type=int)
    p.add_argument("--milestone", help="default: the next one to ship")
    p.add_argument("--accept-merge", action="append", metavar="SHA",
                   help="a merge on epic/N with changes of its own (a resolved conflict), reviewed by a human")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_ship)

    p = sub.add_parser("verify-main", help="run the suite on main after a release reached it untested, and record it")
    p.add_argument("number", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_verify_main)

    p = sub.add_parser("revert-slice", help="undo one merged slice on epic/N before it ships, and reopen it")
    p.add_argument("number", type=int)
    p.add_argument("--reason")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_revert_slice)

    p = sub.add_parser("ready", help="units of work whose blockers are all done")
    p.add_argument("--epic", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=command_ready)

    p = sub.add_parser("demo-status", help="each milestone's demo steps, slices, PRs and demo state (read-only)")
    p.add_argument("number", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=command_demo_status)

    p = sub.add_parser("demo-post", help="publish a milestone demo's pictures and post it on the epic")
    p.add_argument("number", type=int)
    p.add_argument("--milestone", required=True, help="M1, M2, ... (or `all` for an epic without milestones)")
    p.add_argument("--dir", required=True, help="README.md and the pictures; published to sdlc-demos")
    p.add_argument("--body-file", required=True, help="the comment; relative image links point into --dir")
    p.add_argument("--from-file")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_demo_post)

    p = sub.add_parser("demo-request", help="ask on the issue for the demo steps only a person can run")
    p.add_argument("number", type=int)
    p.add_argument("--milestone", required=True)
    p.add_argument("--body-file", required=True, help="each step: its --dry-run output, the snapshot/restore bracket, the command")
    p.add_argument("--from-file")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_demo_request)

    p = sub.add_parser("file-issue", help="file a finding as its own issue: tech debt of an epic, or new for triage")
    p.add_argument("number", type=int, help="the epic it was found in")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--debt", action="store_true", help="non-blocking work on this epic (label tech-debt), not a new problem")
    p.add_argument("--source", choices=["demo", "review", "advisor", "adjacent"], default="demo")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_file_issue)

    p = sub.add_parser("demo-accept", help="record the poster's /approve of a demo (then `ship` releases it)")
    p.add_argument("number", type=int)
    p.add_argument("--milestone")
    p.add_argument("--from-file")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_demo_accept)

    p = sub.add_parser("demo-changes", help="record the changes the poster asked for at a demo")
    p.add_argument("number", type=int)
    p.add_argument("--milestone")
    p.add_argument("--body-file", required=True, help="what they asked for, in plain words")
    p.add_argument("--found", action="store_true",
                   help="the agent's own demo found the milestone broken (before any demo is posted)")
    p.add_argument("--from-file")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=command_demo_changes)

    args = ap.parse_args(argv)
    try:
        return args.fn(args, load_config(args.config))
    except SdlcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
