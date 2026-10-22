"""tools/sdlc.py end to end: the debt lane, from a finding filed as tech debt to the debt epic closing. Every step is
a real `sdlc.main` command (the ones the skills run), against a scratch repo with a bare origin and the stateful fake
of `gh` the quick lane's end-to-end test uses, extended by the few calls the debt lane makes that the quick lane
doesn't. No network, no real gh or codex, no speaker, no Spotify: a guard fails the test if any of them is reached.
`uv run pytest tests/test_sdlc_debt_e2e.py -v` is the debt lane's milestone demo.

It also holds the one rule the merge permission rests on: no skill and no tool merges a PR with a bare `gh pr merge`.
Every merge goes through `sdlc.py merge`, whose gate has the review the permission check is looking for."""
import ast
import html
import json
import re
from pathlib import Path

import pytest

from tests.fake_codex import APPROVES, no_real_codex_home  # noqa: F401
from tests.test_sdlc_quick import _git, repo  # noqa: F401   (a scratch repo with a bare origin, cwd in it)
from tests.test_sdlc_quick_e2e import (CHANGES, CLOSING, FakeGitHub, GhError, Lane, OWNER, POSTER, REPO, _runs_main_gate_code,  # noqa: F401
                                       flag, offline, sdlc_labels)
from tools import sdlc

ROOT = Path(__file__).resolve().parent.parent
FILE = "code.py"                       # the one file of the scratch repo's main the debt PRs change


class DebtGitHub(FakeGitHub):
    """The quick lane's fake, plus what the debt lane asks of GitHub that a quick change never does: the issues a PR
    will close (`closingIssuesReferences`), whether a branch is still on GitHub, the open PRs into a base (the pass's
    overlap check) and taking a sub-issue off its parent (intake sending a ticket to triage)."""

    def pr_graphql(self, p):
        refs = [{"number": n, "repository": {"name": REPO.split("/")[1], "owner": {"login": REPO.split("/")[0]}}}
                for n in sorted({int(x) for x in CLOSING.findall(p["body"])} & set(self.issues))]
        return {**super().pr_graphql(p), "closingIssuesReferences": refs}

    def answer(self, args):
        if args[:2] == ["issue", "close"] and "--duplicate-of" in args:   # intake closes a duplicate naming the other
            self.issue(args[2]).update(state="closed", state_reason="duplicate")
            return ""
        if args[:2] == ["issue", "list"]:   # `next` reads the open issues
            want = flag(args, "--json").split(",")
            return json.dumps([{k: self.issue_graphql(i)[k] for k in want} for i in self.issues.values()
                               if i["state"] == flag(args, "--state", "open")])
        return super().answer(args)

    def api(self, rest):
        method, path = (rest[1], rest[2]) if rest[0] == "-X" else ("GET", rest[0])
        tail = path.split("?")[0].split("/")[3:]
        if method == "GET" and tail[:3] == ["git", "ref", "heads"]:
            if not self.sha("refs/heads/" + "/".join(tail[3:])):
                raise GhError("HTTP 404: Not Found")
            return json.dumps({"ref": "refs/heads/" + "/".join(tail[3:])})
        if method == "GET" and tail == ["pulls"]:
            base = path.split("base=")[1].split("&")[0]
            return self.page(rest, [{"number": p["number"]} for p in self.prs.values()
                                    if p["state"] == "OPEN" and p["base"] == base])
        if method == "DELETE" and tail[0] == "issues" and tail[2:] == ["sub_issue"]:
            payload = json.loads(Path(flag(rest, "--input")).read_text())
            child = next(i["number"] for i in self.issues.values() if i["id"] == payload["sub_issue_id"])
            self.sub_issues[int(tail[1])].remove(child)
            return json.dumps({})
        out = super().api(rest)
        if method == "GET" and tail[:1] == ["compare"] and "..." in tail[1]:   # `fixed` reads the merge base
            a, b = (self.sha(x) for x in "/".join(tail[1:]).split("..."))
            data = json.loads(out)
            data["merge_base_commit"] = {"sha": self.origin_git("merge-base", a, b)}
            return json.dumps(data)
        return out


class DebtLane(Lane):
    """The quick lane's scratch world, with the debt lane's steps as the skills run them."""

    def __init__(self, *a):
        super().__init__(*a)
        self.gh = DebtGitHub(self.mp, self.root, self.gh.labels)
        self.pass_n = 0

    # -- the world --
    def open_ticket(self, title, body="Tech debt that was filed by hand."):
        """A `tech-debt` issue as a person files it: no marker, no epic."""
        return self.gh.new_issue(title, body, ["tech-debt"], author=OWNER)

    def feature_epic(self, created=("M1",), shipped=()):
        """A feature epic being built: its first milestone created and not yet on main."""
        n = self.gh.new_issue("A feature", "Build it.", ["sdlc:in-progress"], author=POSTER)
        self.gh.say(n, OWNER, sdlc.render_comment("prd", 1, "text", self.cfg))
        self.gh.say(n, OWNER, sdlc.render_comment("approval", 1, "approved", self.cfg, by=POSTER, via="comment"))
        self.gh.say(n, OWNER, sdlc.render_comment("plan-created", None, "created", self.cfg, created=",".join(created)))
        for m in shipped:
            self.gh.say(n, OWNER, sdlc.render_comment("shipped", None, "shipped", self.cfg, milestone=m, sha="a" * 40))
        return n

    def file_debt(self, found_in, title, source="review"):
        """`file-issue N --debt`, as work-slice runs it: the new ticket's number."""
        body = self.scratch / "debt-body.md"
        body.write_text(f"{title}: where it is and what should change.\n")
        out = self.ok("file-issue", found_in, "--debt", "--source", source, "--title", title, "--body-file", body)
        return int(re.search(r"/issues/(\d+)", out).group(1))

    def debt_epic(self):
        """The newest debt epic, open or closed."""
        return max(n for n, i in self.gh.issues.items() if "lane:debt" in i["labels"])

    def verdict(self, t, verdict, reason, *extra):
        return self.ok("debt-intake", t, "--verdict", verdict, "--reason", reason, *extra)

    # -- passes --
    def pass_file(self, epic, groups, k=None, name="pass.json"):
        f = self.scratch / name
        f.write_text(json.dumps({"epic": epic, "pass": k or self.pass_n + 1, "groups": groups}))
        return f

    def group(self, *tickets, scope="Fix them in one change.", files=(FILE,), complexity="routine"):
        return {"tickets": list(tickets), "scope": scope, "files": list(files), "complexity": complexity,
                "complexity_reason": "one function"}

    def post_pass(self, epic, *groups):
        f = self.pass_file(epic, list(groups))
        assert "ok (" in self.ok("debt-pass-validate", epic, f)
        out = self.ok("debt-pass-post", epic, f)
        self.pass_n += 1
        return out

    # -- building --
    def claim_debt(self, t):
        out = self.ok("claim", t, "--debt", "--model", "sonnet")
        return Path(re.search(r"^WORKTREE=(.+)$", out, re.M).group(1))

    def open_debt_pr(self, builder, tickets, title):
        body = f"{title}.\n\n" + "\n".join(f"Closes #{t}" for t in tickets)
        out = self.gh.gh(["pr", "create", "--repo", REPO, "--base", "main", "--head", f"slice/{builder}", "--title",
                          title, "--body", body])
        return int(out.rsplit("/", 1)[1])

    def build_group(self, tickets, title, text, steps=()):
        """Claim a group by its first ticket, change a file, push and open its PR into main: (worktree, PR, head)."""
        wt = self.claim_debt(tickets[0])
        head = self.commit(wt, tickets[0], {FILE: text}, title)
        return wt, self.open_debt_pr(tickets[0], tickets, title), head

    def records_on(self, n, kind):
        return self.gh.records(n, kind)


@pytest.fixture
def lane(repo, tmp_path, monkeypatch, capsys):   # noqa: F811
    return DebtLane(tmp_path, monkeypatch, capsys, repo)


def merged_on_main(lane, before):
    """The commits main gained, oldest first, as (subject, files)."""
    shas = lane.gh.origin_git("rev-list", "--reverse", f"{before}..main").split()
    return [(lane.gh.origin_git("log", "-1", "--format=%s", s),
             lane.gh.origin_git("diff", "--name-only", f"{s}^", s).split()) for s in shas]


# --- the happy path: filed, joined, judged, planned, built, reviewed, merged, ended, closed ---------------------

def test_the_debt_lane_runs_from_a_filing_to_the_epic_closing(lane):
    gh = lane.gh
    base = lane.main_sha()
    stale = lane.open_ticket("The picker ignores an empty list")           # filed by hand, long ago
    quick = lane.quick_issue()                                             # a quick change that found more debt

    # no debt epic yet and there is open debt: `next` says to open one; `--dry-run` writes nothing
    assert "debt-epic" in lane.ok("next")
    before = gh.snapshot(stale)
    assert "would open the debt epic" in lane.ok("debt-epic", "--dry-run")
    assert gh.snapshot(stale) == before and not gh.sub_issues
    lane.ok("debt-epic")
    epic = lane.debt_epic()
    assert set(gh.issues[epic]["labels"]) == {"lane:debt", "sdlc:in-progress"} and gh.sub_issues[epic] == [stale]
    calls = len(gh.calls)
    lane.ok("debt-epic")                                                    # a second run adds nothing
    assert gh.sub_issues[epic] == [stale] and not [c for c in gh.calls[calls:] if c[0] == "api" and "-X" in c]

    # work the quick change needed to support itself joins the open epic at once, as generation 1
    one = lane.file_debt(quick, "The station list isn't sorted")
    two = lane.file_debt(quick, "The tag filter ignores case", source="advisor")
    assert gh.sub_issues[epic] == [stale, one, two]
    first_line = gh.issues[one]["body"].splitlines()[0]
    assert "generation=1" in first_line and "lane=quick" in first_line
    st = lane.state(epic)
    assert (st["action"], st["turn"]) == ("debt_intake", "agent") and st["debt"]["intake_pending"] == [stale, one, two]
    assert lane.state(one)["debt_epic"] == epic and f"debt epic #{epic}" in lane.state(one)["next"]

    # intake, by an agent and with no poster: one is already fixed on main, two are debt
    fix = gh.origin_git("rev-parse", "main~1")      # a commit already on main, behind its tip
    out = lane.verdict(stale, "fixed", "Fixed on main since.", "--commit", fix)
    assert (gh.issues[stale]["state"], gh.issues[stale]["state_reason"]) == ("closed", "completed") and fix[:7] in out
    lane.verdict(one, "debt", "A helper the quick change needed to support itself.")
    lane.verdict(two, "debt", "The same.")
    assert not [c for n in (stale, one, two) for c in gh.comments[n] if c["user"]["login"] == POSTER]
    st = lane.state(epic)
    assert st["action"] == "plan_pass" and st["debt"]["eligible"] == [one, two]
    assert f"/plan-issue {epic}" in st["next"] and f"debt-pass-validate {epic}" in st["next"]

    # a pass is a plan of which tickets: the tool refuses a bad one and posts nothing; it asks for no /approve and
    # runs no Codex round
    bad = lane.pass_file(epic, [lane.group(one, two, stale)])
    code, _, err = lane.run("debt-pass-validate", epic, bad)
    assert code == 1 and f"#{stale} is closed" in err
    code, _, err = lane.run("debt-pass-post", epic, bad)
    assert code == 1 and "nothing was posted" in err and not gh.records(epic, "debt-pass")
    assert "kind=debt-pass " in lane.ok("debt-pass-post", epic, lane.pass_file(epic, [lane.group(one, two)]), "--dry-run")
    assert not gh.records(epic, "debt-pass")
    lane.post_pass(epic, lane.group(one, two))
    assert len(gh.records(epic, "debt-pass")) == 1 and not gh.records(epic, "approval")
    assert not lane.codex_log.exists()
    st = lane.state(epic)
    assert (st["action"], st["debt"]["open_pass"]) == ("work_debt", 1)
    assert st["next"].startswith(f"/work-slice {one} --debt (sonnet: routine)")

    # built: the whole group is claimed, in one worktree on slice/<first> from origin/main; one PR into main
    wt = lane.claim_debt(one)
    assert wt == lane.root / ".worktrees" / f"slice-{one}" and _git("rev-parse", "HEAD", cwd=wt) == base
    for t in (one, two):
        assert "debt=1 pass=1 group=debt-1-1" in gh.records(t, "claim")[0]["body"].splitlines()[0]
    head = lane.commit(wt, one, {FILE: "x = 2\n"}, "Sort the station list and match tags without case")
    pr = lane.open_debt_pr(one, [one, two], "Sort the station list and match tags without case")
    assert f"/work-slice {one} --debt" in lane.state(epic)["next"]            # claimed: its work resumes here

    # Codex is given the tickets and the group's scope and files; nothing is run for a dry run
    dry = lane.ok("codex-review", "--pr", pr, "--dry-run", "--out", lane.scratch / "dry", cwd=wt)
    assert "Fix them in one change." in dry and f"`{FILE}`" in dry and f"Ticket #{two}" in dry
    assert not lane.codex_log.exists()

    # not mergeable yet: no suite and no Codex review on the head, and `review-defer` never stands in for one
    code, _, err = lane.run("merge", pr, "--dry-run")
    assert code == 1 and "no Codex review of the current head" in err and "no recorded test run on the current head" in err
    code, _, err = lane.run("review-defer", pr, "--reason", "codex: usage limit", cwd=wt)
    assert code == 1 and "deferred" in err and not gh.records(pr, "review-defer")
    assert lane.state(epic)["action"] == "work_debt"
    lane.ok("test-record", pr, cwd=wt)
    assert lane.suite_runs == [head]

    # Codex asks for a change: the round is answered and recorded before the fix. While the PR is reviewed, a related
    # flaw turns up (debt of debt, generation 2); a third generation is not filed as debt at all
    code, report, out = lane.codex(pr, wt, CHANGES)
    assert code == 0, out
    (lane.scratch / "response.md").write_text("1. Accepted: fixing in the next commit.\n")
    lane.ok("pr-review", pr, "--report", report, "--response", lane.scratch / "response.md", cwd=wt)
    assert lane.state(epic)["action"] == "work_debt"                          # a review that asks for changes blocks
    code, _, err = lane.run("merge", pr, "--dry-run")
    assert code == 1 and "asks for changes" in err
    found = lane.file_debt(one, "The sort is case-sensitive", source="review")
    assert "generation=2" in gh.issues[found]["body"].splitlines()[0] and found in gh.sub_issues[epic]
    code, _, err = lane.run("file-issue", found, "--debt", "--title", "More", "--body-file", lane.scratch / "debt-body.md")
    assert code == 1 and "generation-2" in err and "File an ordinary issue" in err
    head2 = lane.commit(wt, one, {FILE: "x = 3\n"}, "Sort case-insensitively")
    lane.ok("test-record", pr, cwd=wt)
    report, (code, out, err) = lane.review(pr, wt, APPROVES)
    assert code == 0, err
    assert lane.suite_runs == [head, head2]

    # due: `state` names the one command, with the gate's results; nothing has merged yet
    st = lane.state(epic)
    assert st["action"] == "merge_debt" and f"uv run python tools/sdlc.py merge {pr}" in st["next"]
    assert lane.main_sha() == base

    # `merge` from inside the group's worktree is refused (the worktree goes with the merge); a dry run from the
    # primary checkout says what it would do and changes nothing
    code, _, err = lane.run("merge", pr, cwd=wt)
    assert code == 1 and "primary checkout" in err and lane.main_sha() == base and gh.prs[pr]["state"] == "OPEN"
    out = lane.ok("merge", pr, "--dry-run")
    assert f"would squash-merge debt PR #{pr}" in out and lane.main_sha() == base and wt.exists()

    # merged: one squash commit holding the change; tickets closed as completed; branch and worktree gone; records
    out = lane.ok("merge", pr)
    assert f"merged PR #{pr} into main; #{one}, #{two} closed; slice/{one} deleted on GitHub and here" in out
    assert merged_on_main(lane, base) == [(f"Sort the station list and match tags without case (#{pr})", [FILE])]
    assert gh.origin_git("show", f"main:{FILE}") == "x = 3"
    for t in (one, two):
        assert (gh.issues[t]["state"], gh.issues[t]["state_reason"]) == ("closed", "completed")
        (rec,) = gh.records(t, "debt-merged")
        assert f"pr={pr} sha={head2}" in rec["body"].splitlines()[0]
    assert len(gh.records(epic, "debt-merged")) == 1
    assert not gh.sha(f"refs/heads/slice/{one}") and not wt.exists()
    assert not _git("branch", "--list", f"slice/{one}", cwd=lane.root) and lane.worktrees() == [f"worktree {lane.root}"]

    # pass 1 ended with the merge: two closed, one joined during it, so the next pass takes the debt that joined
    (end,) = gh.records(epic, "debt-pass-end")
    assert "joined=1 closed=2" in end["body"].splitlines()[0] and gh.issues[epic]["state"] == "open"
    lane.ok("debt-epic")                                                    # nothing left to end: a no-op
    assert len(gh.records(epic, "debt-pass-end")) == 1
    st = lane.state(epic)
    assert (st["action"], st["debt"]["intake_pending"]) == ("debt_intake", [found])
    lane.verdict(found, "debt", "Inside the sort's own scope, found by its review.")
    st = lane.state(epic)
    assert st["action"] == "plan_pass" and st["debt"]["eligible"] == [found] and "pass 2" in st["next"]

    # pass 2: the same road for that ticket
    lane.post_pass(epic, lane.group(found, scope="Compare without case."))
    wt2 = lane.claim_debt(found)
    lane.commit(wt2, found, {FILE: "x = 4\n"}, "Compare case-insensitively")
    pr2 = lane.open_debt_pr(found, [found], "Compare case-insensitively")
    lane.ok("test-record", pr2, cwd=wt2)
    lane.review(pr2, wt2, APPROVES)
    assert lane.state(epic)["action"] == "merge_debt"
    main2 = lane.main_sha()
    out = lane.ok("merge", pr2)
    assert f"merged PR #{pr2} into main; #{found} closed" in out
    assert [s for s, _ in merged_on_main(lane, main2)] == [f"Compare case-insensitively (#{pr2})"]
    assert (gh.issues[found]["state"], gh.issues[found]["state_reason"]) == ("closed", "completed")

    # the epic ends with its last pass: no open debt is left, so it is closed and moved to sdlc:done
    assert len(gh.records(epic, "debt-pass-end")) == 2
    assert gh.issues[epic]["state"] == "closed" and sdlc_labels(gh.issues[epic]["labels"]) == ["sdlc:done"]
    st = lane.state(epic)
    assert st["state"] == "done" and st["next"].startswith("nothing")
    assert lane.worktrees() == [f"worktree {lane.root}"]
    assert not any(c[:2] == ["pr", "merge"] and "--delete-branch" in c for c in gh.calls)

    # and debt filed after that opens a new epic
    again = lane.file_debt(quick, "Another thing the change needed")
    assert lane.debt_epic() != epic and gh.sub_issues[lane.debt_epic()] == [again]


# --- what the lane holds back ---------------------------------------------------------------------------------

def test_the_lane_holds_back_what_is_not_ready_and_what_is_not_debt(lane):
    gh = lane.gh
    feature = lane.feature_epic()                  # its M1 is built on epic/N, not on main yet
    waits = lane.file_debt(feature, "The helper should take an empty list")
    epic = lane.debt_epic()
    ready = lane.open_ticket("A docstring is out of date")
    speaker = lane.open_ticket("The volume ramp needs checking on a real speaker")
    feature_really = lane.open_ticket("Let the picker take a pasted link")
    needs_design = lane.open_ticket("Decide how the relay should shut down")
    same = lane.open_ticket("A docstring is out of date, again")
    lane.ok("debt-epic")
    assert gh.sub_issues[epic] == [waits, ready, speaker, feature_really, needs_design, same]
    first_line = gh.issues[waits]["body"].splitlines()[0]
    assert "generation=1" in first_line and f"waits={feature}:M1" in first_line

    # intake: not debt, or a design to make, goes to ordinary triage with the reason; a duplicate closes naming the
    # other; the rest is debt
    lane.verdict(feature_really, "not-debt", "A new feature, not work something else needed.")
    lane.verdict(needs_design, "novel", "Several designs are possible.")
    lane.verdict(same, "duplicate", f"The same docstring as #{ready}.", "--of", ready)
    for t in (feature_really, needs_design):
        assert "tech-debt" not in gh.issues[t]["labels"] and sdlc_labels(gh.issues[t]["labels"]) == ["sdlc:triage"]
        assert t not in gh.sub_issues[epic]
    for t, why in ((feature_really, "A new feature, not work something else needed."),
                   (needs_design, "Several designs are possible.")):
        assert any(why in c["body"] for c in gh.records(t, "intake"))
    assert (gh.issues[same]["state"], gh.issues[same]["state_reason"]) == ("closed", "duplicate")
    for t in (waits, ready, speaker):
        lane.verdict(t, "debt", "Work the feature or the code needed to support itself.")

    # debt of a milestone that is not on main yet is in the epic and in no pass until it ships
    st = lane.state(waits)
    assert st["waiting_for"] == {"epic": feature, "milestone": "M1"} and f"#{feature}'s M1 to ship to main" in st["next"]
    st = lane.state(epic)
    assert st["action"] == "plan_pass" and st["debt"]["eligible"] == [ready, speaker]
    code, _, err = lane.run("debt-pass-validate", epic, lane.pass_file(epic, [lane.group(ready, waits)]))
    assert code == 1 and f"#{waits}" in err and "M1" in err
    code, _, err = lane.run("debt-pass-post", epic, lane.pass_file(epic, [lane.group(ready), lane.group(speaker),
                                                                           lane.group(ready)]))
    assert code == 1 and not gh.records(epic, "debt-pass")

    # a ticket that needs a real speaker leaves the pass: the agent asks on the ticket and never runs the write
    lane.post_pass(epic, lane.group(ready), lane.group(speaker, files=("src/twiddle/play.py",)))
    request = lane.scratch / "request.md"
    request.write_text("Run this with the room set to a test room, then say what you heard.\n"
                       "`uv run twiddle snapshot --room Test`\n`uv run twiddle volume --room Test 20 --dry-run`\n"
                       "`uv run twiddle volume --room Test 20`\n`uv run twiddle restore --room Test`\n")
    before = gh.snapshot(speaker)
    lane.ok("debt-request", speaker, "--body-file", request, "--dry-run")
    assert gh.snapshot(speaker) == before
    lane.ok("debt-request", speaker, "--body-file", request)
    (asked,) = gh.records(speaker, "debt-request")
    assert "twiddle snapshot" in asked["body"] and "--dry-run" in asked["body"] and "never runs this write" in asked["body"]
    assert lane.state(speaker)["person_request"] and "waiting on a person" in lane.state(speaker)["next"]

    # the pass finishes without it: the other group is built, reviewed and merged
    wt = lane.claim_debt(ready)
    lane.commit(wt, ready, {FILE: "x = 5\n"}, "Fix the docstring")
    pr = lane.open_debt_pr(ready, [ready], "Fix the docstring")
    lane.ok("test-record", pr, cwd=wt)
    lane.review(pr, wt, APPROVES)
    lane.ok("merge", pr)
    (end,) = gh.records(epic, "debt-pass-end")
    assert gh.issues[ready]["state"] == "closed" and gh.issues[epic]["state"] == "open"

    # what is left waits, and nothing is planned over it: one for a milestone, one for a person
    st = lane.state(epic)
    assert st["action"] == "debt_waiting" and f"#{waits}" in st["next"] and f"#{speaker}" in st["next"]
    # their reply puts it back; the milestone shipping releases the other
    gh.say(speaker, OWNER, "It stepped to 20 and back. Nothing odd.")
    gh.say(feature, OWNER, sdlc.render_comment("shipped", None, "shipped", lane.cfg, milestone="M1", sha="a" * 40))
    st = lane.state(epic)
    assert st["action"] == "plan_pass" and st["debt"]["eligible"] == [waits, speaker]


# --- no bare merge ---------------------------------------------------------------------------------------------

# the functions of tools/sdlc.py that run `gh pr merge`, each behind its own gate (or, `merge_escalated_prd`/
# `command_merge_prd`, a docs-only PR carrying an approved PRD); `ship` merges a release PR behind its own checks
GATED_MERGES = {"command_merge", "merge_quick", "merge_debt", "command_ship", "command_merge_prd", "merge_escalated_prd"}


def gh_merge_callers(source):
    """The functions that build a `gh pr merge` call, i.e. a list starting "pr", "merge", in a module's source."""
    tree = ast.parse(source)
    found = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.List) and [getattr(e, "value", None) for e in node.elts[:2]] == ["pr", "merge"]:
                    found.add(fn.name)
    return found


def bare_merges(text):
    """Where a text tells someone to run `gh pr merge`: in a fenced block, or in a line (or the one before it, since
    prose wraps) that doesn't forbid it."""
    hits, fenced, lines = [], False, text.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        near = " ".join(lines[max(0, i - 1):i + 1])
        if re.search(r"gh\s+pr\s+merge", line) and (fenced or not re.search(r"never|not |bare|refus|instead", near, re.I)):
            hits.append(line.strip())
    return hits


def test_the_checks_for_a_bare_merge_catch_one():
    assert gh_merge_callers("def sneaky(pr):\n    gh(['pr', 'merge', str(pr), '--squash'])\n") == {"sneaky"}
    assert gh_merge_callers("def fine(pr):\n    gh(['pr', 'view', str(pr)])\n") == set()
    assert bare_merges("Then run:\n```bash\ngh pr merge 12 --squash\n```\n")
    assert bare_merges("Now `gh pr merge 12` and you are done.")
    assert not bare_merges("Never merge with a bare `gh pr merge`.")


def test_nothing_merges_a_pr_but_the_gated_commands():
    callers = {}
    for path in sorted((ROOT / "tools").glob("*.py")) + sorted((ROOT / "scripts").glob("*")) + [ROOT / "src" / "twiddle" / "cli.py"]:
        text = path.read_text()
        if path.suffix == ".py":
            for name in gh_merge_callers(text):
                callers.setdefault(path.name, set()).add(name)
        assert "gh pr merge" not in text.replace("`gh pr merge`", ""), f"{path} runs `gh pr merge`"
    assert callers == {"sdlc.py": GATED_MERGES}                    # a rename or a new merge path fails here
    docs = [ROOT / "SDLC.md", ROOT / "CLAUDE.md", ROOT / "docs" / "sdlc.html",
            *sorted((ROOT / ".agents").rglob("*.md")), *sorted((ROOT / ".claude" / "skills").rglob("*.md"))]
    assert not {str(d.relative_to(ROOT)): bare_merges(d.read_text()) for d in docs if bare_merges(d.read_text())}


# --- the docs and skills say what the lane does ------------------------------------------------------------------

# the rule the poster adds to .claude/settings.json: the three places that write it out must agree, character for character
PERMISSION_RULES = ('"Bash(uv run python tools/sdlc.py merge *)"', '"Bash(uv run python tools/sdlc.py cleanup *)"')
DEBT_COMMANDS = ("debt-epic", "debt-intake", "debt-pass-validate", "debt-pass-post", "debt-request", "claim T --debt")
DEBT_ACTIONS = ("debt_intake", "plan_pass", "work_debt", "merge_debt", "pass_end", "debt_waiting")


def read(path):
    return (ROOT / path).read_text()


def test_the_merge_permission_is_written_out_the_same_everywhere():
    for doc in ("SDLC.md", "docs/sdlc.html", ".agents/skills/work-slice/SKILL.md"):
        text = html.unescape(read(doc))
        for rule in PERMISSION_RULES:
            assert rule in text, f"{doc} doesn't write out {rule}"
        assert '"permissions"' in text and '"allow"' in text and ".claude/settings.json" in text


def test_the_docs_and_skills_name_every_debt_command_and_action():
    sdlc_md, page = read("SDLC.md"), html.unescape(read("docs/sdlc.html"))
    for word in (*DEBT_COMMANDS, *DEBT_ACTIONS[:-1], "debt.pass_size", "debt.max_generation", "debt.max_per_pr"):
        assert word in sdlc_md, f"SDLC.md doesn't mention {word}"
    for word in (*DEBT_COMMANDS[:-1], *DEBT_ACTIONS, "claim T --debt"):
        assert word in page, f"docs/sdlc.html doesn't mention {word}"
    for skill, words in {"triage-issue": ("debt-intake", "debt_intake", "--verdict not-debt", "--verdict fixed", "--verdict duplicate"),
                         "plan-issue": ("plan_pass", "debt-pass-validate", "debt-pass-post", "pass_size", "max_per_pr"),
                         "work-slice": ("claim T --debt", "debt-request", "max_generation", "merge_debt",
                                        "escalate-slice T", "never deferred")}.items():
        text = read(f".agents/skills/{skill}/SKILL.md")
        for word in words:
            assert word in text, f"the {skill} skill doesn't mention {word}"
    row = next(l for l in read("CLAUDE.md").splitlines() if l.startswith("| `tools/sdlc.py`"))
    for word in ("debt-epic", "debt-intake", "debt-pass-post", "debt-request", "debt-pass-validate", "claim --debt"):
        assert word in row, f"CLAUDE.md's tools/sdlc.py row doesn't mention {word}"
