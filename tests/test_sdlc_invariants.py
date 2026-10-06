"""tools/sdlc.py: what `next` says and what the gates allow must agree, over every small epic history.

Each gate (claim, merge, ship, demo) and `next` look at the same epic. These tests
walk an exhaustive space of small epics (milestones, how far each got, which slices
are done, in flight or ready, tech debt, a milestone not created yet) and check:

* whatever `next` offers, the gates allow;
* an unfinished epic with nothing in flight, nothing escalated and something
  workable never ends in "nothing" (no deadlock);
* only the current milestone's slices are offered or may land.

No network: the histories are built from markers and progress dicts.
"""
import itertools
import re

import pytest

from tools import sdlc

CONFIG = sdlc.load_config()
OWNER, POSTER = "owner", "poster"
TRUSTED = {OWNER}
EPIC = 12
PAYS = "## Pays down\n\n#81: tech debt filed against this epic."


def _plan(keys):
    unit = lambda k, m: {"key": k, "title": k, "covers": [1], "outcome": "o", "scope": "s",  # noqa: E731
                         "acceptance": ["a"], "validation": "v", "demo": "d", "non_goals": "n", "context": "c",
                         "complexity": "routine", "complexity_reason": "r", "milestone": m}
    return {"issue": EPIC, "kind": "feature", "milestones": [{"key": k, "title": k, "demo": "d"} for k in keys],
            "subtasks": [unit(f"T{k}", k) for k in keys]}


class History:
    def __init__(self):
        self.comments, self.ts = [], 0

    def add(self, kind, rev=None, body="text", author=OWNER, **extra):
        self.ts += 1
        text = sdlc.marker(kind, rev, **extra) + "\n" + body if kind else body
        self.comments.append({"id": self.ts, "author": author, "body": text, "url": f"u{self.ts}",
                              "created_at": f"2026-10-05T{self.ts // 60:02d}:{self.ts % 60:02d}:00Z"})


# one milestone's story so far (markers only); "complete" comes from its slices
STORIES = ["none", "demoed", "accepted", "shipped", "voided", "found", "owed"]
SLICES = ["open", "inflight", "done", "escalated"]


def build(keys, created, stories, slice_states, debt, cleanup_in):
    """(epic state, progress, leaves) for one generated epic."""
    h = History()
    h.add("prd", 1)
    h.add("approval", 1, by=POSTER)
    h.add("plan", 1, body=sdlc.render_comment("plan", 1, sdlc.render_plan(_plan(keys)), CONFIG).split("\n", 1)[1])
    h.add("plan-review", 1, verdict="approve", round="1")
    h.add("plan-created", 1, created=",".join(created))
    for k in keys:
        s = stories[k]
        if s == "none":
            continue
        if s == "owed":   # a slice merged without its own Codex review: the milestone's review is due first
            h.add("review-owed", milestone=k, slice=f"T{k}.0", number="101", why="routine")
            continue
        h.add("demo", 1, milestone=k, sha="d" * 40)
        if s in ("accepted", "shipped", "voided", "found"):
            h.add("demo-approval", 1, milestone=k, by=POSTER)
        if s == "shipped":
            h.add("shipped", milestone=k, sha="e" * 40)
        if s == "voided":
            h.add("demo-void", milestone=k)
        if s == "found":
            h.add("demo-changes", milestone=k, found="agent")
            h.add("plan", 2)
            h.add("plan-review", 2, verdict="approve", round="1")
            h.add("plan-created", 2, created=",".join(created))
    if cleanup_in:   # the cleanup pass after M1 was planned and created (its slice sits in M2)
        h.add("plan", 3)
        h.add("plan-review", 3, verdict="approve", round="1")
        h.add("plan-created", 3, created=",".join(created), cleanup="M1")
    issue = {"number": EPIC, "title": "epic", "state": "open", "author": POSTER, "labels": ["sdlc:in-progress"]}
    st = sdlc.derive_state(issue, h.comments, TRUSTED, CONFIG)
    leaves, blockers, n = [], {}, 100
    for k in created:
        prev = None
        for i, s in enumerate(slice_states[k]):
            n += 1
            leaf = {"number": n, "key": f"T{k}.{i}", "kind": "slice", "milestone": f"#{EPIC} {k}: {k}",
                    "state": "closed" if s == "done" else "open", "state_reason": "completed" if s == "done" else None,
                    "assignees": [OWNER] if s == "inflight" else [],
                    "labels": ["plan:slice"] + (["sdlc:escalated"] if s == "escalated" else []),
                    "body": ""}
            if prev is not None:   # a chain inside the milestone: each slice after the one before
                blockers[n] = [{"number": prev["number"], "state": prev["state"],
                                "state_reason": prev["state_reason"]}]
            leaves.append(leaf)
            prev = leaf
        if k == cleanup_in:   # a cleanup slice of its own, not in the chain: ready at once
            n += 1
            # keyed to sort last: only the cleanup rule can put it first
            leaves.append({"number": n, "key": f"Z{k}", "kind": "slice", "milestone": f"#{EPIC} {k}: {k}",
                           "state": "open", "state_reason": None, "assignees": [], "labels": ["plan:slice"],
                           "body": PAYS})
    progress = {**sdlc.summarise_progress(leaves, blockers), "behind_main": 0, "debt": [81] if debt else [],
                "cleanup_budget": 1}
    return st, progress, leaves


def consistent(keys, created, stories, slice_states):
    """Only epics the workflow can reach."""
    for i, k in enumerate(keys):
        if k not in created:
            if stories[k] != "none" or any(k2 in created for k2 in keys[i + 1:]):
                return False
            continue
        complete = all(s == "done" for s in slice_states[k])
        if stories[k] in ("demoed", "accepted", "shipped") and not complete:
            return False
        if stories[k] in ("voided", "found") and complete:
            return False                   # reopened (a revert) or fix slices added: not complete again yet
        if stories[k] == "shipped" and any(stories[x] != "shipped" for x in keys[:i]):
            return False                   # milestones ship in order
        if any(s != "done" for s in slice_states[k]) or stories[k] != "shipped":
            # a milestone still being worked: nothing after it has moved, except #12's legacy shape (done slices)
            for later in keys[i + 1:]:
                if stories[later] != "none":
                    return False
    return True


def cases():
    keys = ["M1", "M2"]
    states = list(itertools.product(SLICES, repeat=2))
    for created in (["M1"], ["M1", "M2"]):
        for s1, s2 in itertools.product(STORIES, repeat=2):
            stories = {"M1": s1, "M2": s2}
            for sl1 in states:
                for sl2 in (states if "M2" in created else [()]):
                    slice_states = {"M1": list(sl1), "M2": list(sl2)}
                    if not consistent(keys, created, stories, slice_states):
                        continue
                    for debt, cleanup_in in ((False, None), (True, None), (True, "M2")):
                        # an open cleanup slice means its milestone isn't complete: it can't be shown or shipped yet
                        if cleanup_in and ("M2" not in created or stories["M2"] in ("demoed", "accepted", "shipped")):
                            continue
                        yield keys, created, stories, slice_states, debt, cleanup_in


ALL = list(cases())


def check(case):
    keys, created, stories, slice_states, debt, cleanup_in = case
    st, progress, leaves = build(*case)
    nxt = sdlc.next_command(st, progress)
    by_number = {l["number"]: l for l in leaves}
    problems = []

    m = re.match(r"/work-slice (\d+)", nxt)
    if m:
        leaf = by_number[int(m.group(1))]
        bundle = {"issue": {"number": leaf["number"], "milestone": leaf["milestone"],
                            "body": sdlc.marker("slice", None, epic=str(EPIC), key=leaf["key"]) + "\n" + leaf["body"]},
                  "epic_state": st, "epic_progress": progress, "trusted": [OWNER]}
        if errs := sdlc.epic_pause_errors(bundle, CONFIG, resume=False):
            problems.append(f"next offers #{leaf['number']} but claim refuses: {errs}")
        if errs := sdlc.milestone_hold_errors(st, progress, leaf["milestone"]):
            problems.append(f"next offers #{leaf['number']} but merge refuses: {errs}")
    if "ship it" in nxt:
        key = re.search(r": (\S+) is accepted; ship it", nxt).group(1)
        try:
            target = sdlc.ship_target(st, progress)
            if not target or target["key"] != key:
                problems.append(f"next ships {key} but ship_target is {target and target['key']}")
        except sdlc.SdlcError as exc:
            problems.append(f"next ships {key} but ship refuses: {exc}")
    m = re.match(r"/milestone-demo \d+: #\d+ (\S+): .* is complete", nxt)
    if m:
        ms = next(x for x in progress["milestones"] if x["key"] == m.group(1))
        if ms["done"] != ms["total"] or (st["demos"].get(ms["key"]) or {}).get("accepted"):
            problems.append(f"next demos {ms['key']}, which isn't complete-and-unaccepted")

    # the oracle, from the raw history and not from sdlc's helpers: what must come next
    cur = next((k for k in keys if k in created and stories[k] != "shipped"), None)
    if m := re.match(r"/work-slice (\d+)", nxt):
        leaf = by_number[int(m.group(1))]
        if sdlc.milestone_key(leaf["milestone"]) != cur:
            problems.append(f"next offers a slice of {sdlc.milestone_key(leaf['milestone'])}, but {cur} is current")
        if cur == cleanup_in and not leaf["body"]:
            problems.append("next offers ordinary work while the current milestone's cleanup slice is ready")
    if cur and st["action"] == "work_slices":
        complete = all(x == "done" for x in slice_states[cur])
        has_cleanup = cur == cleanup_in        # its cleanup slice is open, so it isn't complete
        if complete and not has_cleanup and stories[cur] in ("none", "voided") and "/milestone-demo" not in nxt:
            problems.append(f"{cur} is complete and unshown, but next is {nxt!r}")
        if complete and not has_cleanup and stories[cur] == "owed" and "Codex hasn't reviewed" not in nxt:
            problems.append(f"{cur} is complete with a review owed, but next is {nxt!r}")
        if complete and not has_cleanup and stories[cur] == "accepted" and "ship it" not in nxt:
            problems.append(f"{cur} is accepted and unshipped, but next is {nxt!r}")

        prev = keys[keys.index(cur) - 1] if keys.index(cur) else None
        if prev and debt and not cleanup_in and stories[prev] == "shipped" and stories[cur] == "none" \
                and not complete and "--cleanup" not in nxt:
            problems.append(f"{prev} shipped with open tech debt and no cleanup pass, but next is {nxt!r}")

    # no deadlock: work exists, nobody holds it, nothing escalated, nobody to wait for
    stuck = (st["action"] == "work_slices" and nxt.startswith("nothing") and not progress["in_flight"]
             and not progress["escalated"] and progress["open"] > 0)
    if stuck:
        problems.append(f"deadlock: {nxt!r}")
    return st, progress, nxt, problems


def test_the_space_is_not_trivial():
    assert len(ALL) > 2000


@pytest.mark.parametrize("helper,fake", [
    ("milestone_phases", lambda st, p: [{**m, "phase": "building"} for m in p["milestones"]]),   # no lifecycle
    ("offered_slices", lambda p, cur: p["ready"]),            # any milestone, no cleanup-first
    ("cleanup_owed", lambda st, p, phases, cur: None),        # no cleanup pass
    ("is_cleanup", lambda leaf: False),
])
def test_the_checks_catch_a_broken_rule(monkeypatch, helper, fake):
    """Each rule, switched off, must make some case fail: the checks aren't vacuous."""
    monkeypatch.setattr(sdlc, helper, fake)
    assert any(check(case)[3] for case in ALL)


@pytest.mark.parametrize("case", ALL, ids=lambda c: f"{','.join(c[1])}|{c[2]['M1']},{c[2]['M2']}|"
                         f"{'.'.join(c[3]['M1'])}/{'.'.join(c[3]['M2'])}|debt={c[4]}|cleanup={c[5]}")
def test_next_and_the_gates_agree(case):
    st, progress, nxt, problems = check(case)
    assert st["action"] in sdlc.ALL_ACTIONS
    assert not problems, (nxt, problems)


# --- the milestone lifecycle: every step a journey takes is an edge of MILESTONE_FLOW -----------

def _phase(h, slices_done, total=2, keys=("M1", "M2"), created=("M1",)):
    issue = {"number": EPIC, "title": "epic", "state": "open", "author": POSTER, "labels": ["sdlc:in-progress"]}
    st = sdlc.derive_state(issue, h.comments, TRUSTED, CONFIG)
    leaves = [{"number": 100 + i, "key": f"T{i}", "kind": "slice", "milestone": f"#{EPIC} M1: M1",
               "state": "closed" if i < slices_done else "open", "state_reason": "completed" if i < slices_done else None,
               "assignees": [], "labels": ["plan:slice"], "body": ""} for i in range(total)]
    p = {**sdlc.summarise_progress(leaves, {}), "debt": [], "cleanup_budget": 1}
    return {m["key"]: m["phase"] for m in sdlc.milestone_phases(st, p)}


def _start():
    h = History()
    h.add("prd", 1)
    h.add("approval", 1, by=POSTER)
    h.add("plan", 1, body=sdlc.render_comment("plan", 1, sdlc.render_plan(_plan(["M1", "M2"])), CONFIG).split("\n", 1)[1])
    h.add("plan-review", 1, verdict="approve", round="1")
    h.add("plan-created", 1, created="M1")
    return h


DEMO = ("demo", 1, {"milestone": "M1", "sha": "d" * 40})
ACCEPT = ("demo-approval", 1, {"milestone": "M1", "by": POSTER})
OWED = ("review-owed", None, {"milestone": "M1", "slice": "T0", "number": "100", "why": "routine"})
# each step: (a marker to post, or None), then how many of M1's slices are merged, of how many
JOURNEYS = {
    "straight through": [(None, 0, 2), (None, 1, 2), (None, 2, 2), (DEMO, 2, 2), (ACCEPT, 2, 2),
                         (("shipped", None, {"milestone": "M1", "sha": "e" * 40}), 2, 2)],
    "the poster asks for changes": [(None, 2, 2), (DEMO, 2, 2), (("demo-changes", 1, {"milestone": "M1"}), 2, 3),
                                    (None, 3, 3)],                       # a fix slice F1 is added, then merges
    "the agent finds it broken": [(None, 2, 2), (("demo-changes", None, {"milestone": "M1", "found": "agent"}), 2, 3),
                                  (None, 3, 3), (DEMO, 3, 3)],
    "a slice is reverted after acceptance": [(None, 2, 2), (DEMO, 2, 2), (ACCEPT, 2, 2),
                                             (("demo-void", None, {"milestone": "M1"}), 1, 2), (None, 2, 2)],
    # a routine slice merged without its own Codex review: the milestone's review comes before the demo
    "a review owed at the milestone": [(OWED, 1, 2), (None, 2, 2),
                                       (("ship-review", None, {"milestone": "M1", "sha": "d" * 40, "verdict": "changes"}), 2, 2),
                                       (("demo-changes", None, {"milestone": "M1", "found": "agent"}), 2, 3),
                                       (None, 3, 3),
                                       (("ship-review", None, {"milestone": "M1", "sha": "d" * 40, "verdict": "approve"}), 3, 3),
                                       (DEMO, 3, 3), (ACCEPT, 3, 3)],
}


@pytest.mark.parametrize("name", JOURNEYS)
def test_milestone_journeys_follow_the_flow(name):
    h, seen = _start(), []
    for step, done, total in JOURNEYS[name]:
        if step:
            kind, rev, extra = step
            h.add(kind, rev, **extra)
        seen.append(_phase(h, done, total)["M1"])
    for a, b in zip(seen, seen[1:]):
        assert a == b or b in sdlc.MILESTONE_FLOW[a], f"{name}: {a} -> {b} is not in MILESTONE_FLOW ({seen})"
    assert _phase(_start(), 0)["M2"] == "uncreated"


def test_every_generated_epic_only_uses_known_phases():
    for case in ALL[::37]:
        st, progress, _ = build(*case)
        assert {m["phase"] for m in sdlc.milestone_phases(st, progress)} <= set(sdlc.MILESTONE_FLOW)
