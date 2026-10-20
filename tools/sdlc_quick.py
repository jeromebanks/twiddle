"""The quick lane's rules (stdlib only, no twiddle imports; `tools/sdlc.py` reads GitHub, posts and claims).

A quick change is one approved brief built straight onto main: no epic, no plan, no
milestone. Everything here is pure, over the derived state `sdlc.derive_state`
returns and the approved brief's text, so the rules can be tested without GitHub.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

QUICK = "quick"                                 # the lane of a small, safe change: one brief, no epic
COMPLEXITY_PREFIX = "complexity:"
ROUTINE = f"{COMPLEXITY_PREFIX}routine"
ESCALATED = "sdlc:escalated"
# the brief is the unit's text: a slice's sections, with the files it may touch instead of a demo
BRIEF_SECTIONS = ("Outcome", "Scope", "Files", "Acceptance criteria", "Validation", "Non-goals", "Context")
CLAIMABLE = {"approved", "in-progress"}         # approved, or being built (a resume)
QUICK_NUMBERS = ("max_files", "max_lines", "max_change_rounds")


class QuickConfigError(ValueError):
    """`.sdlc/config.json`'s `quick` section is unusable (sdlc.load_quick raises it as SdlcError)."""


@dataclass(frozen=True)
class QuickRules:
    """What a quick PR's diff may be: `.sdlc/config.json`'s `quick` section, checked."""
    max_files: int
    max_lines: int                 # added + deleted, over the whole diff
    max_change_rounds: int         # Codex rounds that asked for changes, before it goes to the full lane
    denied_paths: tuple[str, ...]  # repo-relative: a file, or a directory with every file under it


def load_quick(config: dict[str, Any]) -> QuickRules:
    """The `quick` section as QuickRules; a missing or bad value raises QuickConfigError naming its key."""
    q = config.get("quick")
    if not isinstance(q, dict):
        raise QuickConfigError("config has no `quick` section (an object)")
    for key in QUICK_NUMBERS:
        v = q.get(key)
        if type(v) is not int or v <= 0:   # not a bool, float or string: a whole number of files, lines, rounds
            raise QuickConfigError(f"config quick.{key} must be a positive whole number, not {v!r}")
    denied = q.get("denied_paths")
    if not isinstance(denied, list) or not denied:
        raise QuickConfigError(f"config quick.denied_paths must be a non-empty list, not {denied!r}")
    for d in denied:   # each must be able to match: a typo here would silently allow the path
        parts = d.rstrip("/").split("/") if isinstance(d, str) else []
        if (not parts or d.startswith("/") or d != d.strip() or "\\" in d
                or any(p in ("", ".", "..") for p in parts)):
            raise QuickConfigError(f"config quick.denied_paths has {d!r}: each is a non-empty, normalised "
                                   "repo-relative path (no leading slash, no `.`, `..` or empty part, no spaces round it)")
    return QuickRules(*(q[k] for k in QUICK_NUMBERS), denied_paths=tuple(denied))


def path_denied(path: str, rules: QuickRules) -> bool:
    """The one place a changed path is matched against the denied paths: the path itself, or under a directory
    (`tools/` and `tools` both deny `tools/sdlc.py`, never `toolsx/a.py`)."""
    return any(path == d.rstrip("/") or path.startswith(d.rstrip("/") + "/") for d in rules.denied_paths)


def brief_files(brief: str) -> list[str]:
    """The `## Files` list of a quick brief: each bullet's first `code` span, else its first word.
    An entry is an exact path or an fnmatch pattern (`dir/*`, `*.ext`; `*` crosses `/`, as fnmatch's does)."""
    m = re.search(r"^## Files\s*\n(.*?)(?=^## |\Z)", brief or "", re.MULTILINE | re.DOTALL)
    out = []
    for line in (m.group(1) if m else "").splitlines():
        item = re.match(r"\s*[-*+]\s+(.*)", line)
        if not item:
            continue
        code = re.search(r"`([^`]+)`", item.group(1))
        entry = (code.group(1) if code else (item.group(1).split() or [""])[0]).strip()
        if entry:
            out.append(entry)
    return out


def changed_paths(f: dict[str, Any]) -> list[str]:
    """A changed file's paths: its name, and where it came from when it was renamed or copied."""
    return [f["filename"]] + ([f["previous_filename"]] if f.get("previous_filename") else [])


def diff_errors(n: int, files: list[dict[str, Any]] | None, brief: str, rules: QuickRules) -> list[str]:
    """Why this diff is not a quick change, each its own message ending in what to run instead.

    `files` is GitHub's `pulls/N/files` (filename, previous_filename, status, additions, deletions); every rule
    reads a rename's source as well as its destination, so a file can't be moved out of a denied place.
    No files (unread, or an empty diff) is refused: an unread diff must never pass as a small one."""
    if not files:
        return ["could not read the PR's changed files (`pulls/N/files` gave none)"]
    to_full = f": `escalate-to-full {n}`"
    paths = [p for f in files for p in changed_paths(f)]
    errs = []
    if denied := sorted({p for p in paths if path_denied(p, rules)}):
        errs.append("the diff touches a path a quick change may not (" + ", ".join(denied[:5])
                    + (", ..." if len(denied) > 5 else "") + ")" + to_full)
    if len(files) > rules.max_files:
        errs.append(f"the diff changes {len(files)} files, more than the quick lane's {rules.max_files}" + to_full)
    lines = sum(int(f.get("additions") or 0) + int(f.get("deletions") or 0) for f in files)
    if lines > rules.max_lines:
        errs.append(f"the diff changes {lines} lines, more than the quick lane's {rules.max_lines}" + to_full)
    listed = brief_files(brief)
    if stray := sorted({p for p in paths if not any(p == e or fnmatch.fnmatchcase(p, e) for e in listed)}):
        errs.append("the diff changes files outside the brief's `## Files` list (" + ", ".join(stray[:5])
                    + (", ..." if len(stray) > 5 else "") + ")" + to_full)
    return errs


def diff_rule_broken(files: list[dict[str, Any]] | None, brief: str, rules: QuickRules) -> bool:
    """A diff that was read breaks a rule (a denied path, the size cap, a file outside the brief): the change has
    outgrown the lane, so `merge` escalates it. A diff that couldn't be read is a refusal, never an escalation."""
    return bool(files) and bool(diff_errors(0, files, brief, rules))


def rounds_error(n: int, rounds: int, rules: QuickRules) -> str | None:
    """More Codex rounds asked for changes than a quick change gets."""
    if rounds > rules.max_change_rounds:
        return (f"Codex asked for changes in {rounds} rounds, more than the quick lane's {rules.max_change_rounds}"
                f": `escalate-to-full {n}`")
    return None


def eligibility_errors(issue: dict[str, Any], st: dict[str, Any]) -> list[str]:
    """Why the issue is not, now, a quick change being built: re-read at merge, since any of it can change
    after the claim (the brief revised or re-approved, the rating changed, an escalation, a conflict)."""
    n = issue["number"]
    errs = []
    if issue.get("state", "open").lower() != "open":
        errs.append(f"#{n} is closed")
    doc = st.get("approved_doc")
    if not doc or doc.get("lane") != QUICK:
        errs.append(f"#{n}'s approved revision is not a quick brief" + (f" (rev {doc.get('rev')})" if doc else ""))
    elif not st.get("doc_approved"):
        errs.append(f"#{n}'s approved revision is rev {doc.get('rev')} but the latest is rev "
                    f"{(st.get('latest_doc') or {}).get('rev')}: the brief changed since it was approved")
    errs += complexity_errors(n, issue.get("labels", []))
    if ESCALATED in issue.get("labels", []) or st.get("state") == "escalated":
        errs.append(f"#{n} is escalated to a human")
    elif st.get("state") != "in-progress":
        errs.append(f"#{n} is {st.get('state')}, not in-progress (a quick change merges once it is claimed)")
    if st.get("conflicts"):
        errs.append(f"#{n} needs a human first: " + "; ".join(st["conflicts"]))
    return errs


TO_FULL_FROM = ("approved", "in-progress")   # an approved quick change, or one being built
TRIGGERS = ("manual", "rounds", "diff")       # who escalated it: a person, too many change rounds, the merge gate


def escalation_errors(issue: dict[str, Any], st: dict[str, Any], labels_moved: bool) -> list[str]:
    """Why the quick change can't be moved to the full lane now (empty list = go). `labels_moved`: its labels
    already read `sdlc:approved` without `lane:quick`. An escalation recorded but left half done (the label
    still in-progress, or still `lane:quick`) is not refused: `escalate-to-full` finishes it.
    Not `eligibility_errors`: a change re-rated after its claim is exactly one that may need the full lane."""
    n = issue["number"]
    if issue.get("state", "open").lower() != "open":
        return [f"#{n} is closed"]
    if "plan:slice" in issue.get("labels", []):
        return [f"#{n} is a plan:slice issue, not a quick change (`escalate-slice {n}` hands a slice to a human)"]
    if st.get("to_full"):
        if labels_moved:
            return [f"#{n} is already in the full lane ({st['to_full'].get('url')}): next, "
                    + (f"`commit-prd {n}`" if not st.get("prd_merged") else f"`/plan-issue {n}`")]
        # half done: the label move (and commit-prd) still to come, unless something else happened since
        others = [c for c in st.get("conflicts") or [] if "moved to the full lane" not in c]
        if st.get("state") not in TO_FULL_FROM or others:
            return [f"#{n} was moved to the full lane, but is now {st.get('state')}"
                    + (f" ({'; '.join(others)})" if others else "") + ": a human decides"]
        return []
    errs = []
    doc = st.get("approved_doc")
    if not doc or doc.get("lane") != QUICK:
        return [f"#{n} is not a quick change (its approved revision is not a quick brief)"]
    if not st.get("doc_approved"):
        errs.append(f"#{n}'s approved revision is rev {doc.get('rev')} but the latest is rev "
                    f"{(st.get('latest_doc') or {}).get('rev')}: a brief in review is changed by triage, not escalated")
    if ESCALATED in issue.get("labels", []) or st.get("state") == "escalated":
        errs.append(f"#{n} is escalated to a human: they decide, not the tool")
    elif st.get("state") not in TO_FULL_FROM:
        errs.append(f"#{n} is {st.get('state')}, not approved or in-progress")
    if st.get("conflicts"):
        errs.append(f"#{n} needs a human first: " + "; ".join(st["conflicts"]))
    return errs


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
