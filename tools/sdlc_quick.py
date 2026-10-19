"""The quick lane's rules (stdlib only, no twiddle imports; `tools/sdlc.py` reads GitHub, posts and claims).

A quick change is one approved brief built straight onto main: no epic, no plan, no
milestone. Everything here is pure, over the derived state `sdlc.derive_state`
returns and the approved brief's text, so the rules can be tested without GitHub.
"""
from __future__ import annotations

import re
from typing import Any

QUICK = "quick"                                 # the lane of a small, safe change: one brief, no epic
COMPLEXITY_PREFIX = "complexity:"
ROUTINE = f"{COMPLEXITY_PREFIX}routine"
# the brief is the unit's text: a slice's sections, with the files it may touch instead of a demo
BRIEF_SECTIONS = ("Outcome", "Scope", "Files", "Acceptance criteria", "Validation", "Non-goals", "Context")
CLAIMABLE = {"approved", "in-progress"}         # approved, or being built (a resume)


def is_quick(st: dict[str, Any]) -> bool:
    """A quick change (its latest or its approved revision is in the quick lane): never an epic."""
    return st.get("lane") == QUICK or (st.get("approved_doc") or {}).get("lane") == QUICK


def complexity_errors(n: int, labels: list[str]) -> list[str]:
    """Only a routine change takes the quick lane: a judgment or novel label anywhere disqualifies it."""
    rated = sorted(l for l in labels if l.startswith(COMPLEXITY_PREFIX))
    if rated == [ROUTINE]:
        return []
    return [f"only an issue labelled {ROUTINE} (and no other complexity) can take the "
            f"quick lane (#{n} is " + (", ".join(rated) if rated else "unrated") + ")"]


def missing_sections(brief: str) -> list[str]:
    """The brief's sections it lacks: a `## Name` heading line, matched as `codex_review.section` reads one."""
    return [name for name in BRIEF_SECTIONS
            if not re.search(rf"^## {re.escape(name)}\s*\n", (brief or "") + "\n", re.MULTILINE)]


def lane_error(n: int, quick_flag: bool, is_slice: bool, quick: bool) -> str | None:
    """`--quick` names the lane: refuse it on a plan:slice, and refuse a quick change claimed without it."""
    if quick_flag and is_slice:
        return f"#{n} is a plan:slice issue, not a quick change: `claim {n}` without --quick"
    if not quick_flag and quick and not is_slice:
        return f"#{n} is a quick change: `claim {n} --quick`"
    return None


def quick_check_errors(issue: dict[str, Any], st: dict[str, Any], brief: str | None,
                       claim: dict[str, Any] | None, resume: bool = False, is_slice: bool = False) -> list[str]:
    """Why a quick change may not be claimed (or resumed) now (empty list = go).

    `st` is the issue's derived state, `brief` its approved revision's text (`approved_brief`),
    `claim` the active claim on it (`claim_status`). The brief, not the issue body, is what is built."""
    n = issue["number"]
    if err := lane_error(n, True, is_slice, is_quick(st)):
        return [err]
    errs = []
    if issue.get("state", "open").lower() != "open":
        errs.append(f"#{n} is closed")
    if st.get("state") == "escalated":
        errs.append(f"#{n} is escalated to a human")
    doc = st.get("approved_doc")
    if not doc:
        errs.append(f"#{n} has no approved brief (it is {st.get('state')}): the poster approves it first")
    elif doc.get("lane") != QUICK:
        errs.append(f"#{n}'s approved revision (rev {doc.get('rev')}) is not a quick brief: `/plan-issue {n}`")
    elif not st.get("doc_approved"):
        latest = (st.get("latest_doc") or {}).get("rev")
        errs.append(f"#{n}'s approved revision is rev {doc.get('rev')} but the latest is rev {latest}: "
                    "it is built only once its latest revision is approved")
    elif st.get("state") not in CLAIMABLE and st.get("state") != "escalated":
        errs.append(f"#{n} is {st.get('state')}, not approved")
    if st.get("conflicts"):
        errs.append(f"#{n} needs a human first: " + "; ".join(st["conflicts"]))
    errs += complexity_errors(n, issue.get("labels", []))
    if doc and (missing := missing_sections(brief or "")):
        errs.append(f"#{n}'s approved brief lacks sections: {', '.join(missing)} "
                    "(post a revised brief and have it approved; don't guess)")
    if claim and not resume:
        errs.append(f"#{n} is already claimed ({claim['url']}): `claim {n} --quick --resume` to continue it")
    if resume and not claim:
        errs.append(f"#{n} has no claim to resume: `claim {n} --quick`")
    return errs
