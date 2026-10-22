"""tools/sdlc.py: the debt lane — the debt epic (`lane:debt`), `debt-epic`, its state, and `next`. No network:
a fake `gh` answers from dicts."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tools import sdlc

CONFIG = sdlc.load_config()
REPO = CONFIG["repository"]
OWNER, STRANGER = "owner", "stranger"
DEBT = sdlc.DEBT_LABEL
EPIC_LABEL = sdlc.DEBT_EPIC_LABEL


class FakeGitHub:
    """Issues, sub-issue links and comments, answered as `gh` would; every write is kept in `writes`."""

    def __init__(self, labels=(EPIC_LABEL, "sdlc:in-progress", DEBT)):
        self.issues, self.subs, self.comments = {}, {}, {}
        self.labels = set(labels)
        self.writes, self.reject = [], set()   # reject: tickets GitHub refuses to attach (they have a parent)
        self.next_n = 100
        self.on_main = {}   # full commit SHA -> GitHub's compare status against main ("behind": on main)
        self.pulls = {}     # base branch -> [(PR number, [files])], the open PRs into it
        self.cid = 0

    def issue(self, title, labels=(), author=OWNER, body="", state="open"):
        self.next_n += 1
        n = self.next_n
        self.issues[n] = {"number": n, "id": 9000 + n, "title": title, "labels": list(labels), "author": author,
                          "body": body, "state": state, "state_reason": None, "pr": False}
        self.comments[n] = []
        return n

    def ticket(self, title="debt", **kw):
        return self.issue(title, [DEBT], body="<!-- sdlc:v1 kind=debt epic=12 source=review generation=1 -->\nTech debt.", **kw)

    def debt_epic(self, author=OWNER, state="open"):
        return self.issue("Tech debt", [EPIC_LABEL, "sdlc:in-progress"], author=author, state=state,
                          body=sdlc.marker(sdlc.DEBT_EPIC_KIND) + "\nThe debt epic.")

    def intake(self, n, verdict="debt", author=OWNER, **extra):
        self.comment(n, sdlc.marker(sdlc.INTAKE_KIND, None, verdict=verdict, **extra) + "\nIt is debt.", author)

    def comment(self, n, body, author=OWNER):
        self.cid += 1
        self.comments[n].append({"id": self.cid, "user": {"login": author}, "body": body,
                                 "created_at": f"2026-10-21T00:00:{self.cid:02d}Z", "html_url": f"u{n}"})

    def say(self, n, kind, rev=None, **extra):
        """A marked comment by the owner on #n, as the tool posts it."""
        self.comment(n, sdlc.render_comment(kind, rev, "text", CONFIG, **extra))

    def feature_epic(self, created=("M1",), shipped=()):
        """A feature epic being built: its milestones' issues created, some shipped to main."""
        n = self.issue("a feature", ["sdlc:in-progress", "enhancement"], author=STRANGER)
        self.say(n, "prd", 1)
        self.say(n, "approval", 1, by=STRANGER, via="comment")
        if created:
            self.say(n, "plan-created", created=",".join(created))
        for m in shipped:
            self.say(n, "shipped", milestone=m, sha="a" * 40)
        return n

    def quick_change(self):
        n = self.issue("a station", ["sdlc:in-progress", "complexity:routine"], author=STRANGER)
        self.say(n, "prd", 1, lane="quick")
        self.say(n, "approval", 1, by=STRANGER, via="comment")
        return n

    def rest(self, i):
        out = {"number": i["number"], "id": i["id"], "title": i["title"], "state": i["state"], "body": i["body"],
               "state_reason": i["state_reason"], "user": {"login": i["author"]},
               "labels": [{"name": l} for l in i["labels"]]}
        return {**out, "pull_request": {}} if i["pr"] else out

    def __call__(self, args, *, check=True):
        args = list(args)
        if args[0] == "api":
            return self.api(args[1:])
        if args[:2] == ["label", "list"]:
            return json.dumps([{"name": l} for l in sorted(self.labels)])
        if args[:2] == ["issue", "list"]:
            return json.dumps([{"number": i["number"], "title": i["title"], "state": "OPEN", "author": {"login": i["author"]},
                                "labels": [{"name": l} for l in i["labels"]], "createdAt": ""}
                               for i in self.issues.values() if i["state"] == "open"])
        if args[:2] == ["issue", "create"]:
            labels = [args[k + 1] for k, a in enumerate(args) if a == "--label"]
            assert not [l for l in labels if l not in self.labels], f"a label that doesn't exist: {labels}"
            body = Path(args[args.index("--body-file") + 1]).read_text()
            n = self.issue(args[args.index("--title") + 1], labels, body=body)
            self.writes.append(("issue create", {"number": n, "labels": labels, "body": body}))
            return f"https://github.com/{REPO}/issues/{n}\n"
        if args[:2] == ["issue", "edit"] and "--body-file" in args:
            self.issues[int(args[2])]["body"] = Path(args[args.index("--body-file") + 1]).read_text()
            self.writes.append(("issue edit", int(args[2])))
            return ""
        if args[:2] == ["issue", "edit"]:
            self.writes.append(("edit", args))
            labels = self.issues[int(args[2])]["labels"]
            for flag, name in zip(args, args[1:]):
                if flag == "--remove-label" and name in labels:
                    labels.remove(name)
                elif flag == "--add-label" and name not in labels:
                    labels.append(name)
            return ""
        if args[:2] == ["issue", "close"]:
            self.writes.append(("close", args))
            i = self.issues[int(args[2])]
            i["state"] = "closed"
            i["state_reason"] = "duplicate" if "--duplicate-of" in args else args[args.index("--reason") + 1]
            return ""
        if args[:2] == ["issue", "view"]:
            i = self.issues[int(args[2])]
            return json.dumps({"number": i["number"], "title": i["title"], "state": i["state"].upper(),
                               "author": {"login": i["author"]}, "labels": [{"name": l} for l in i["labels"]],
                               "createdAt": "", "body": i["body"]})
        pytest.fail(f"gh {args}: the fake doesn't know this call")

    def api(self, rest):
        method, path = (rest[1], rest[2]) if rest[0] == "-X" else ("GET", rest[0])
        url = urlsplit(path)
        tail, query = url.path.split("/")[3:], parse_qs(url.query)
        assert url.path.startswith(f"repos/{REPO}/"), path
        if method == "POST" and "-F" in rest:   # post_comment: -F body=@file
            body = Path(rest[rest.index("-F") + 1].split("=@", 1)[1]).read_text()
            self.writes.append((path, body))
            self.comment(int(tail[1]), body)
            return json.dumps({"id": self.cid})
        if method == "DELETE":
            payload = json.loads(Path(rest[rest.index("--input") + 1]).read_text())
            self.writes.append((path, payload))
            assert tail[0] == "issues" and tail[2:] == ["sub_issue"], path   # singular, as GitHub's REST API has it
            child = next(i["number"] for i in self.issues.values() if i["id"] == payload["sub_issue_id"])
            self.subs[int(tail[1])].remove(child)
            return json.dumps({})
        if method == "POST":
            payload = json.loads(Path(rest[rest.index("--input") + 1]).read_text())
            self.writes.append((path, payload))
            if tail == ["issues"]:
                missing = [l for l in payload["labels"] if l not in self.labels]
                assert not missing, f"a label that doesn't exist: {missing}"
                n = self.issue(payload["title"], payload["labels"], body=payload["body"])
                return json.dumps(self.rest(self.issues[n]))
            if tail[0] == "issues" and tail[2:] == ["sub_issues"]:
                child = next(i for i in self.issues.values() if i["id"] == payload["sub_issue_id"])
                if child["number"] in self.reject or "replace_parent" in payload:
                    raise sdlc.SdlcError("gh api -X POST failed: HTTP 422: Sub issue may only have one parent")
                self.subs.setdefault(int(tail[1]), []).append(child["number"])
                return json.dumps({})
            pytest.fail(f"POST {path}: the fake doesn't know this call")
        page = lambda data: json.dumps([data])   # noqa: E731  (--paginate --slurp)
        if tail == ["collaborators"]:
            return page([{"login": OWNER, "permissions": {"push": True}}])
        if tail == ["issues"]:
            want = set(query.get("labels", [""])[0].split(",")) - {""}
            state = query.get("state", ["open"])[0]
            return page([self.rest(i) for i in self.issues.values()
                         if want <= set(i["labels"]) and state in ("all", i["state"])])
        if tail[0] == "issues" and tail[2:] == ["sub_issues"]:
            return page([self.rest(self.issues[c]) for c in self.subs.get(int(tail[1]), [])])
        if tail == ["pulls"]:
            assert query.get("state") == ["open"], path
            return page([{"number": pr} for pr, _ in self.pulls.get(query["base"][0], [])])
        if tail[0] == "pulls" and tail[2:] == ["files"]:
            files = next(f for prs in self.pulls.values() for pr, f in prs if pr == int(tail[1]))
            return page([{"filename": f} for f in files])
        if tail[0] == "issues" and tail[2:] == ["comments"]:
            return page(self.comments[int(tail[1])])
        if tail[0] == "issues" and len(tail) == 2:
            if int(tail[1]) not in self.issues:
                raise sdlc.SdlcError("gh api repos/... failed: HTTP 404: Not Found")
            return json.dumps(self.rest(self.issues[int(tail[1])]))
        if tail[0] == "compare":
            sha = tail[1].split("...", 1)[1]
            full = next((f for f in self.on_main if f.startswith(sha)), None)
            if full is None:
                raise sdlc.SdlcError("gh api repos/... failed: HTTP 404: No commit found")
            status = self.on_main[full]
            return json.dumps({"status": status, "merge_base_commit": {"sha": full if status in ("behind", "identical")
                                                                           else "0" * 40}})
        pytest.fail(f"gh api {rest}: the fake doesn't know this call")

    def created(self):
        return [p for path, p in self.writes if path == f"repos/{REPO}/issues"]

    def attached(self):
        return [p["sub_issue_id"] - 9000 for path, p in self.writes if path.endswith("/sub_issues")]


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(sdlc, "gh", fake)
    monkeypatch.setattr(sdlc.time, "sleep", lambda s: None)
    return fake


def run(capsys, *argv):
    code = sdlc.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# --- the config: the `debt` section and the label ------------------------------------------------

def test_the_debt_config_loads_with_its_defaults():
    assert sdlc.load_debt(CONFIG) == sdlc.DebtRules(pass_size=8, max_generation=2, max_per_pr=4)
    without = {k: v for k, v in CONFIG.items() if k != "debt"}
    assert sdlc.load_debt(without) == sdlc.DebtRules(**sdlc.DEBT_DEFAULTS)
    assert sdlc.load_debt({"debt": {"pass_size": 5}}) == sdlc.DebtRules(pass_size=5, max_generation=2, max_per_pr=4)


@pytest.mark.parametrize("debt,named", [({"pass_size": 0}, "debt.pass_size"), ({"max_generation": "2"}, "debt.max_generation"),
                                        ({"max_per_pr": True}, "debt.max_per_pr"), ({"pass_size": 2.5}, "debt.pass_size"),
                                        ({"pass_sise": 8}, "pass_sise"), ({"max_per_pr": 9}, "debt.max_per_pr (9)"),
                                        ([8, 2, 4], "config `debt` must be an object")])
def test_a_bad_debt_value_is_a_clear_error(debt, named):
    with pytest.raises(sdlc.SdlcError, match=None) as exc:
        sdlc.load_debt({"debt": debt})
    assert named in str(exc.value)


def test_debt_epic_refuses_a_bad_debt_section_before_any_write(tmp_path, gh, capsys):
    gh.ticket()
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({**CONFIG, "debt": {"pass_size": -1}}))
    code, _, err = run(capsys, "--config", bad, "debt-epic")
    assert code == 1 and "debt.pass_size must be a positive whole number" in err and gh.writes == []


def test_bootstrap_labels_creates_lane_debt(capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    code, out, _ = run(capsys, "bootstrap-labels", "--dry-run")
    assert code == 0 and f"gh label create {EPIC_LABEL} " in out


# --- debt-epic: open one, attach every open ticket, once -------------------------------------------

def test_debt_epic_opens_one_and_attaches_every_ticket_once(gh, capsys):
    a, b = gh.ticket("a"), gh.ticket("b", author=STRANGER)   # the label is the gate, not who filed it
    gh.issue("a feature", ["enhancement"])
    code, out, _ = run(capsys, "debt-epic")
    assert code == 0, out
    (made,) = gh.created()
    assert made["labels"] == [EPIC_LABEL, "sdlc:in-progress"]
    assert sdlc.parse_marker(made["body"]) == {"kind": sdlc.DEBT_EPIC_KIND}
    epic = max(gh.issues)
    assert gh.subs[epic] == [a, b] and f"debt epic #{epic}: attached 2" in out
    # a rerun creates and attaches nothing
    before = list(gh.writes)
    code, out, _ = run(capsys, "debt-epic")
    assert code == 0 and gh.writes == before and f"debt epic #{epic} already holds all 2" in out


def test_debt_epic_with_no_debt_creates_nothing(gh, capsys):
    gh.issue("a feature", ["enhancement"])
    code, out, _ = run(capsys, "debt-epic")
    assert code == 0 and gh.writes == [] and "no open tech-debt issues" in out and "no debt epic is opened" in out


def test_debt_epic_never_opens_a_second(gh, capsys):
    epic = gh.debt_epic()
    held, new = gh.ticket("held"), gh.ticket("new")
    gh.subs[epic] = [held]
    closed = gh.debt_epic(state="closed")   # a closed one is history, not the open one
    code, out, _ = run(capsys, "debt-epic")
    assert code == 0 and gh.created() == [] and gh.attached() == [new] and gh.subs[epic] == [held, new]
    assert closed not in gh.subs


def test_an_untrusted_lane_debt_issue_is_not_the_debt_epic(gh, capsys):
    fake = gh.debt_epic(author=STRANGER)
    t = gh.ticket()
    code, _, _ = run(capsys, "debt-epic")
    assert code == 0 and len(gh.created()) == 1 and fake not in gh.subs and gh.subs[max(gh.issues)] == [t]


def test_two_open_debt_epics_are_for_a_person(gh, capsys):
    a, b = gh.debt_epic(), gh.debt_epic()
    gh.ticket()
    code, _, err = run(capsys, "debt-epic")
    assert code == 1 and f"#{a}, #{b}" in err and "close all but one" in err and gh.writes == []


def test_debt_epic_needs_its_label_first(gh, capsys):
    gh.labels.discard(EPIC_LABEL)
    gh.ticket()
    code, _, err = run(capsys, "debt-epic")
    assert code == 1 and "bootstrap-labels" in err and gh.writes == []


def test_a_ticket_github_wont_attach_fails_the_read_back(gh, capsys):
    epic = gh.debt_epic()
    ok, parented = gh.ticket("ok"), gh.ticket("has a parent")
    gh.reject.add(parented)
    code, _, err = run(capsys, "debt-epic")
    assert code == 1 and f"doesn't hold #{parented}" in err and "one parent" in err
    assert gh.subs[epic] == [ok]
    assert all("replace_parent" not in p for _, p in gh.writes)   # never moved from another parent silently


def test_debt_epic_dry_run_writes_nothing(gh, capsys):
    a = gh.ticket()
    code, out, _ = run(capsys, "debt-epic", "--dry-run")
    assert code == 0 and gh.writes == []
    assert f"would open the debt epic 'Tech debt' [{EPIC_LABEL}, sdlc:in-progress]" in out
    assert f"would attach #{a} (debt) to debt epic #new" in out


# --- the debt epic's state ----------------------------------------------------------------------------

def epic_issue(labels=(EPIC_LABEL, "sdlc:in-progress"), number=250):
    return {"number": number, "title": "Tech debt", "state": "open", "author": OWNER, "labels": list(labels),
            "body": sdlc.marker(sdlc.DEBT_EPIC_KIND)}


def state_from_file(tmp_path, capsys, bundle):
    f = tmp_path / "b.json"
    f.write_text(json.dumps(bundle))
    code, out, err = run(capsys, "state", 250 if bundle["issue"]["number"] == 250 else bundle["issue"]["number"],
                         "--json", "--from-file", f)
    assert code == 0, err
    return json.loads(out)


def test_state_of_the_debt_epic_is_intake_while_a_ticket_has_no_verdict(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("--from-file reads the bundle only"))
    tickets = [{"number": 101, "title": "a", "intake": "debt"}, {"number": 102, "title": "b", "intake": None}]
    st = state_from_file(tmp_path, capsys, {"issue": epic_issue(), "comments": [], "trusted": [OWNER],
                                            "debt": {"tickets": tickets}})
    assert (st["state"], st["lane"], st["turn"], st["action"]) == ("in-progress", "debt", "agent", "debt_intake")
    assert st["next"].startswith("intake on debt epic #250: 1 ticket(s) without a verdict (#102)")
    tickets[1]["intake"] = "debt"
    st = state_from_file(tmp_path, capsys, {"issue": epic_issue(), "comments": [], "trusted": [OWNER],
                                            "debt": {"tickets": tickets}})
    # intake done, no pass open, both eligible: a pass is due (T5), no longer "nothing"
    assert (st["turn"], st["action"]) == ("agent", "plan_pass")
    assert st["next"].startswith("/plan-issue 250: plan debt pass 1 from 2 eligible ticket(s) (#101, #102;")


@pytest.mark.parametrize("labels", [[EPIC_LABEL], [EPIC_LABEL, "sdlc:triage"], [EPIC_LABEL, "sdlc:planned"]])
def test_the_debt_epic_is_never_triaged_or_planned(labels):
    st = sdlc.derive_state(epic_issue(labels), [], {OWNER}, CONFIG)
    assert st["lane"] == "debt" and (st["turn"], st["action"]) == ("agent", "none")
    assert sdlc.next_command(st) == "`uv run python tools/sdlc.py state 250` (needs the debt epic's tickets)"
    # sdlc:approved with nothing approved is a label for a person to fix, never a plan
    odd = sdlc.derive_state(epic_issue([EPIC_LABEL, "sdlc:approved"]), [], {OWNER}, CONFIG)
    assert odd["action"] == "fix_conflict" and sdlc.next_command(odd).startswith("nothing for an agent")


def test_the_debt_epic_is_not_a_feature_epic():
    st = sdlc.derive_state(epic_issue(), [], {OWNER}, CONFIG)
    assert sdlc.is_debt_epic(st) and not sdlc.epic_building(st) and not sdlc.is_quick(st)
    with pytest.raises(sdlc.SdlcError, match="#250 is the debt epic, not a feature epic: it has no epic branch"):
        sdlc.refuse_quick(st, "epic branch")


def test_an_escalated_or_closed_debt_epic_keeps_its_state():
    esc = sdlc.derive_state(epic_issue([EPIC_LABEL, "sdlc:escalated"]), [], {OWNER}, CONFIG)
    assert (esc["turn"], esc["action"]) == ("human", "human")
    closed = sdlc.derive_state({**epic_issue(), "state": "closed"}, [], {OWNER}, CONFIG)
    assert (closed["turn"], closed["action"]) == ("none", "none")
    view = sdlc.with_next(esc, CONFIG, offline=True, bundle={"debt": {"tickets": [{"number": 1, "intake": None}]}})
    assert view["action"] == "human" and "debt" not in view


def test_state_of_the_debt_epic_reads_its_tickets_on_github(gh, capsys):
    epic = gh.debt_epic()
    a, b, gone = gh.ticket("a"), gh.ticket("b"), gh.ticket("fixed", state="closed")
    gh.subs[epic] = [a, b, gone]
    gh.intake(a)
    gh.intake(b, author=STRANGER)   # a verdict counts only from a collaborator
    code, out, _ = run(capsys, "state", epic, "--json")
    st = json.loads(out)
    assert code == 0 and st["action"] == "debt_intake" and st["debt"]["intake_pending"] == [b]
    assert [t["number"] for t in st["debt"]["tickets"]] == [a, b]
    code, out, _ = run(capsys, "state", epic)
    assert f"debt epic: 2 open ticket(s), without an intake verdict: #{b}" in out
    assert f"next: intake on debt epic #{epic}" in out


# --- a ticket names its debt epic ---------------------------------------------------------------------

def test_state_of_a_ticket_names_its_debt_epic(gh, capsys):
    epic, t, loose = gh.debt_epic(), gh.ticket(), gh.ticket()
    gh.subs[epic] = [t]
    code, out, _ = run(capsys, "state", t)
    assert code == 0 and f"in debt epic #{epic}" in out
    assert f"next: nothing for #{t} alone: it is tech debt in debt epic #{epic}" in out
    code, out, _ = run(capsys, "state", loose, "--json")
    st = json.loads(out)
    assert st["debt_epic"] is None and st["next"].startswith("`uv run python tools/sdlc.py debt-epic` (")


def test_state_of_a_ticket_offline_takes_its_epic_from_the_bundle(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("--from-file reads the bundle only"))
    ticket = {"number": 77, "title": "t", "state": "open", "author": OWNER, "labels": [DEBT],
              "body": "<!-- sdlc:v1 kind=debt epic=12 source=review -->"}
    st = state_from_file(tmp_path, capsys, {"issue": ticket, "comments": [], "trusted": [OWNER], "debt_epic": 250})
    assert st["debt_epic"] == 250 and "debt epic #250" in st["next"]


# --- next ---------------------------------------------------------------------------------------------------

def next_listed(capsys):
    code, out, err = run(capsys, "next", "--json")
    assert code == 0, err
    return json.loads(out)


def test_next_names_debt_epic_while_debt_is_unattached(gh, capsys):
    a = gh.ticket()
    (only,) = next_listed(capsys)
    assert only["number"] is None and only["unattached"] == [a] and only["next"] == "`uv run python tools/sdlc.py debt-epic`"
    code, out, _ = run(capsys, "next")
    assert f"-     debt              open tech debt in no debt epic: #{a}\n      next: `uv run python tools/sdlc.py debt-epic`" in out
    assert run(capsys, "debt-epic")[0] == 0
    epic = max(gh.issues)
    listed = next_listed(capsys)   # attached: debt-epic is no longer named, and the epic's intake is
    assert [(s["number"], s["action"]) for s in listed] == [(epic, "debt_intake")]
    gh.intake(a)
    listed = next_listed(capsys)   # through intake, no pass yet: a pass is due
    assert [(s["number"], s["action"]) for s in listed] == [(epic, "plan_pass")]
    b = gh.ticket()   # new debt, not attached yet
    listed = next_listed(capsys)
    assert [s["number"] for s in listed] == [epic, None] and listed[1]["unattached"] == [b]


def test_next_reads_no_sub_issues_without_debt(monkeypatch, capsys):
    calls = []
    fake = FakeGitHub()
    fake.issue("a feature", ["enhancement"])
    monkeypatch.setattr(sdlc, "gh", lambda args, **k: calls.append(list(args)) or fake(args, **k))
    next_listed(capsys)
    assert not any("sub_issues" in " ".join(c) or "lane%3Adebt" in " ".join(c) for c in calls)


def test_next_with_two_open_debt_epics_names_them_and_still_runs(gh, capsys):
    a, b = gh.debt_epic(), gh.debt_epic()
    t1, t2 = gh.ticket(), gh.ticket()
    gh.subs[a], gh.subs[b] = [t1], [t2]
    gh.intake(t1)
    gh.intake(t2)
    listed = [s for s in next_listed(capsys) if s["number"] is None]
    assert [(s["number"], s["debt_epics"]) for s in listed] == [(None, [a, b])]
    assert "a person closes all but one" in listed[0]["next"]


@pytest.mark.parametrize("with_epic", [False, True])
def test_next_finds_old_debt_behind_a_full_issue_list(gh, monkeypatch, capsys, with_epic):
    """Codex on #266: an old debt ticket, or an old debt epic with intake due, behind NEXT_LIMIT newer issues is
    still found (all pages are read)."""
    epic = gh.debt_epic() if with_epic else None
    old = gh.ticket("old debt")
    if epic:
        gh.subs[epic] = [old]
    for k in range(sdlc.NEXT_LIMIT):
        gh.issue(f"slice {k}", ["plan:slice"])
    full = gh.__call__

    def capped(args, *, check=True):   # `gh issue list` honours --limit, newest first, as GitHub does
        if list(args[:2]) == ["issue", "list"]:
            limit = int(args[list(args).index("--limit") + 1])
            return json.dumps(sorted(json.loads(full(args, check=check)), key=lambda i: -i["number"])[:limit])
        return full(args, check=check)
    monkeypatch.setattr(sdlc, "gh", capped)
    listed = next_listed(capsys)
    if epic:
        assert [(s["number"], s["action"]) for s in listed] == [(epic, "debt_intake")]
    else:
        assert [(s["number"], s["unattached"]) for s in listed] == [(None, [old])]


# --- file-issue --debt: joins the debt epic, with a generation and what it waits for ----------------------

@pytest.fixture
def body_file(tmp_path):
    f = tmp_path / "debt.md"
    f.write_text("The thing the feature needed to support itself.")
    return f


def file_it(capsys, n, body_file, *extra):
    return run(capsys, "file-issue", n, "--debt", "--source", "review", "--title", "a debt", "--body-file", body_file,
               *extra)


def filed(gh):
    (w,) = [p for path, p in gh.writes if path == "issue create"]
    return w["number"], sdlc.parse_marker(w["body"]), w["body"]


@pytest.mark.parametrize("source", ["feature", "quick", "demo"])
def test_debt_joins_the_open_debt_epic_with_generation_1(gh, capsys, body_file, source):
    epic = gh.debt_epic()
    n = gh.quick_change() if source == "quick" else gh.feature_epic(created=("M1",), shipped=("M1",))
    code, out, err = run(capsys, "file-issue", n, "--debt", "--source", "demo" if source == "demo" else "review",
                         "--title", "a debt", "--body-file", body_file)
    assert code == 0, err
    t, mk, body = filed(gh)
    assert gh.issues[t]["labels"] == [DEBT] and gh.subs[epic] == [t] and gh.created() == []
    assert (mk["epic"], mk["generation"], mk.get("waits")) == (str(n), "1", None)
    assert (mk.get("lane") == "quick") == (source == "quick")
    assert f"It joins debt epic #{epic}, whose passes work it into `main`." in body
    assert "cleanup pass" not in body and "open `tech-debt` list" not in body
    assert out.splitlines()[0].endswith(f"/issues/{t}") and f"#{t} joined debt epic #{epic}" in out


def test_debt_opens_a_debt_epic_when_none_is_open(gh, capsys, body_file):
    n = gh.feature_epic(shipped=("M1",))
    code, out, err = file_it(capsys, n, body_file)
    assert code == 0, err
    (made,) = gh.created()
    epic = next(k for k, i in gh.issues.items() if i["title"] == "Tech debt")
    t, _, body = filed(gh)
    assert made["labels"] == [EPIC_LABEL, "sdlc:in-progress"] and gh.subs[epic] == [t]
    assert f"debt epic #{epic}" in body.splitlines()[1] and f"opened debt epic #{epic}" in out


@pytest.mark.parametrize("label", [EPIC_LABEL, DEBT])
def test_debt_without_its_labels_is_refused_before_anything_is_written(gh, capsys, body_file, label):
    gh.labels.discard(label)
    n = gh.feature_epic()
    code, _, err = file_it(capsys, n, body_file)
    assert code == 1 and "bootstrap-labels" in err and label in err and gh.writes == []


def test_debt_with_two_open_debt_epics_is_refused_before_anything_is_written(gh, capsys, body_file):
    a, b = gh.debt_epic(), gh.debt_epic()
    code, _, err = file_it(capsys, gh.feature_epic(), body_file)
    assert code == 1 and f"#{a}, #{b}" in err and gh.writes == []


def test_debt_github_wont_attach_is_filed_and_names_debt_epic(gh, capsys, body_file):
    epic = gh.debt_epic()
    n = gh.feature_epic(shipped=("M1",))
    gh.reject.add(gh.next_n + 1)   # the ticket about to be filed
    code, out, err = file_it(capsys, n, body_file)
    t, _, _ = filed(gh)
    assert code == 1 and f"/issues/{t}" in out and gh.subs.get(epic, []) == []
    assert f"#{t} is filed, but debt epic #{epic} doesn't hold it" in err and "debt-epic" in err


def test_debt_of_a_debt_ticket_is_its_generation_plus_1_until_max(gh, capsys, body_file):
    epic = gh.debt_epic()
    first = gh.ticket()   # generation 1, from #12
    code, _, err = file_it(capsys, first, body_file)
    assert code == 0, err
    second, mk, body = filed(gh)
    assert (mk["epic"], mk["generation"], mk.get("waits")) == (str(first), "2", None)   # epic= is its source
    assert f"Tech debt from debt ticket #{first} (review), generation 2" in body and gh.subs[epic] == [second]
    # max_generation is 2: debt of a generation-2 ticket is refused, and names the ordinary file-issue
    before = list(gh.writes)
    code, _, err = file_it(capsys, second, body_file)
    assert code == 1 and gh.writes == before
    assert f"#{second} is generation-2 tech debt and debt.max_generation is 2" in err
    assert f"`uv run python tools/sdlc.py file-issue {second} --title ... --body-file ...` (without --debt)" in err
    code, _, err = run(capsys, "file-issue", second, "--title", "x", "--body-file", body_file)   # that one works
    assert code == 0, err


def test_an_old_debt_ticket_is_generation_1(gh, capsys, body_file):
    gh.debt_epic()
    old = gh.issue("old debt", [DEBT], body="<!-- sdlc:v1 kind=debt epic=12 source=review -->\nTech debt.")
    assert file_it(capsys, old, body_file)[0] == 0
    assert filed(gh)[1]["generation"] == "2"


def test_a_bad_generation_is_refused_not_guessed(gh, capsys, body_file):
    gh.debt_epic()
    bad = gh.issue("bad", [DEBT], body="<!-- sdlc:v1 kind=debt epic=12 source=review generation=0 -->\nTech debt.")
    code, _, err = file_it(capsys, bad, body_file)
    assert code == 1 and "bad generation" in err and gh.writes == []


@pytest.mark.parametrize("what", ["debt epic", "slice"])
def test_debt_against_the_debt_epic_or_a_slice_is_refused(gh, capsys, body_file, what):
    n = (gh.debt_epic() if what == "debt epic" else
         gh.issue("a slice", ["plan:slice"], body=sdlc.marker("slice", None, epic="12", key="A1")))
    code, _, err = file_it(capsys, n, body_file)
    assert code == 1 and gh.writes == []
    assert ("the debt ticket whose PR found it" if what == "debt epic" else "file its debt against the epic") in err


# --- what it waits for: the feature milestone being built, until it ships to main --------------------------

def ticket_state(capsys, t):
    code, out, err = run(capsys, "state", t, "--json")
    assert code == 0, err
    return json.loads(out)


def test_debt_of_an_unshipped_milestone_waits_for_it_to_ship(gh, capsys, body_file):
    epic = gh.debt_epic()
    n = gh.feature_epic(created=("M1",))
    assert file_it(capsys, n, body_file)[0] == 0
    t, mk, body = filed(gh)
    assert mk["waits"] == f"{n}:M1" and body.splitlines()[1].endswith(f"once #{n}'s M1 has shipped to `main`.")
    st = ticket_state(capsys, t)
    assert (st["debt_of"], st["debt_generation"], st["debt_epic"]) == (str(n), 1, epic)
    assert st["waiting_for"] == {"epic": n, "milestone": "M1"}
    assert f"waiting for #{n}'s M1 to ship to main" in st["next"]
    code, out, _ = run(capsys, "state", t)
    assert f"tech debt of #{n}, generation 1; waits for #{n}'s M1 to ship to main" in out
    gh.say(n, "shipped", milestone="M1", sha="b" * 40)   # recorded shipped: it no longer waits
    st = ticket_state(capsys, t)
    assert st["waiting_for"] is None and "waiting" not in st["next"]


def test_two_milestones_one_shipped(gh, capsys, body_file):
    """Codex on the plan: with M1 shipped and M2 created, new debt waits for M2; a ticket that waited for M1 no
    longer waits; an old marker (no generation) is derived to wait for M2."""
    gh.debt_epic()
    n = gh.feature_epic(created=("M1", "M2"), shipped=("M1",))
    assert file_it(capsys, n, body_file)[0] == 0
    new, mk, _ = filed(gh)
    assert mk["waits"] == f"{n}:M2"
    on_m1 = gh.issue("m1 debt", [DEBT], body=sdlc.debt_head(n, "review", waits="M1", debt_epic=1))
    old = gh.issue("old debt", [DEBT], body=f"<!-- sdlc:v1 kind=debt epic={n} source=review -->\nTech debt.")
    assert ticket_state(capsys, new)["waiting_for"] == {"epic": n, "milestone": "M2"}
    assert ticket_state(capsys, on_m1)["waiting_for"] is None
    st = ticket_state(capsys, old)
    assert (st["debt_generation"], st["debt_of"], st["waiting_for"]) == (1, str(n), {"epic": n, "milestone": "M2"})


def test_an_old_debt_ticket_reports_its_source_and_waits_only_when_derived(gh, capsys):
    epic = gh.debt_epic()
    done = gh.feature_epic(created=("M1",), shipped=("M1",))
    building = gh.feature_epic(created=("M1",))
    quick = gh.quick_change()
    old = {k: gh.issue("old", [DEBT], body=f"<!-- sdlc:v1 kind=debt epic={src} source=review{lane} -->\nTech debt.")
           for k, src, lane in [("done", done, ""), ("building", building, ""), ("quick", quick, " lane=quick")]}
    gh.subs[epic] = list(old.values())
    for k, t in old.items():
        st = ticket_state(capsys, t)
        src = {"done": done, "building": building, "quick": quick}[k]
        assert (st["debt_generation"], st["debt_of"], st["debt_epic"]) == (1, str(src), epic)   # its source, not #epic
        assert st["waiting_for"] == ({"epic": building, "milestone": "M1"} if k == "building" else None)


def test_debt_of_a_quick_change_or_a_shipped_epic_never_waits(gh, capsys, body_file):
    gh.debt_epic()
    for n in (gh.quick_change(), gh.feature_epic(created=()), gh.feature_epic(shipped=("M1",))):
        gh.writes.clear()
        assert file_it(capsys, n, body_file)[0] == 0
        assert "waits" not in filed(gh)[1]


def test_state_of_a_waiting_ticket_offline_reads_its_source_from_the_bundle(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("--from-file reads the bundle only"))
    ticket = {"number": 77, "title": "t", "state": "open", "author": OWNER, "labels": [DEBT],
              "body": sdlc.debt_head(12, "review", waits="M1", debt_epic=250)}
    source = {"issue": {"number": 12, "title": "e", "state": "open", "author": OWNER, "labels": ["sdlc:in-progress"]},
              "comments": []}
    b = {"issue": ticket, "comments": [], "trusted": [OWNER], "debt_epic": 250}
    assert state_from_file(tmp_path, capsys, b)["waiting_for"] == {"epic": 12, "milestone": "M1"}   # can't see a ship
    st = state_from_file(tmp_path, capsys, {**b, "source": source})
    assert st["waiting_for"] == {"epic": 12, "milestone": "M1"}
    source["comments"] = [{"id": 1, "author": OWNER, "created_at": "t", "url": "u",
                           "body": sdlc.render_comment("shipped", None, "x", CONFIG, milestone="M1", sha="c" * 40)}]
    assert state_from_file(tmp_path, capsys, {**b, "source": source})["waiting_for"] is None


# --- --dry-run: the issue and the attachment, nothing written ----------------------------------------------

def test_file_issue_dry_run_shows_the_issue_and_the_attachment(gh, capsys, body_file):
    epic = gh.debt_epic()
    n = gh.feature_epic(created=("M1",))
    code, out, err = file_it(capsys, n, body_file, "--dry-run")
    assert code == 0, err
    assert gh.writes == []
    lines = out.splitlines()
    assert lines[0].startswith("would run: gh issue create") and "--label tech-debt" in lines[0]
    assert lines[1] == f"<!-- sdlc:v1 kind=debt epic={n} source=review generation=1 waits={n}:M1 -->"
    assert f"It joins debt epic #{epic}" in lines[2] and lines[-1] == f"would attach the new issue to debt epic #{epic}"
    del gh.issues[epic]   # none open: it says it would open one
    code, out, _ = file_it(capsys, n, body_file, "--dry-run")
    assert code == 0 and gh.writes == [] and "would open the debt epic 'Tech debt'" in out
    assert out.splitlines()[-1] == "would attach the new issue to debt epic #new"


def test_readdressed_quick_debt_keeps_its_generation_and_names_its_debt_epic(gh, capsys):
    """An escalated quick change's debt: `lane=quick` goes, `generation=` stays (written even on a pre-#247 marker,
    so nothing is derived from the escalated epic), and the lead is rewritten unless someone edited it by hand."""
    epic = gh.debt_epic()
    new = gh.issue("new", [DEBT], body=sdlc.debt_head(12, "review", quick=True, debt_epic=epic) + "\n\nWhy.")
    pre = gh.issue("pre", [DEBT], body="<!-- sdlc:v1 kind=debt epic=12 source=demo lane=quick -->\n"
                                       "Tech debt from quick change #12 (demo): not blocking. A quick change has no "
                                       "epic.\n\nWhy.")
    edited = gh.issue("edited", [DEBT], body=sdlc.marker("debt", None, epic="12", source="review", lane="quick",
                                                          generation="1") + "\nA lead written by hand.\n\nWhy.")
    gh.subs[epic] = [new, pre]
    sdlc.readdress_quick_debt(12, CONFIG, {OWNER})
    assert gh.issues[new]["body"] == sdlc.debt_head(12, "review", debt_epic=epic) + "\n\nWhy."
    assert gh.issues[pre]["body"] == sdlc.debt_head(12, "demo", debt_epic=epic) + "\n\nWhy."
    assert gh.issues[edited]["body"] == ("<!-- sdlc:v1 kind=debt epic=12 source=review generation=1 -->\n"
                                         "A lead written by hand.\n\nWhy.")
    assert capsys.readouterr().out.count("is now an epic's debt") == 3


# --- debt-intake: an agent's verdict, applied and read back ------------------------------------------------

SHA = "a" * 40


def attached(gh, n=1):
    """A debt epic holding n fresh tickets: (epic, [tickets])."""
    epic = gh.debt_epic()
    ts = [gh.ticket(f"t{k}") for k in range(n)]
    gh.subs[epic] = list(ts)
    return epic, ts


def records(gh, n):
    return [sdlc.parse_marker(c["body"]) for c in gh.comments[n]
            if (sdlc.parse_marker(c["body"]) or {}).get("kind") == sdlc.INTAKE_KIND]


@pytest.mark.parametrize("verdict", ["not-debt", "novel"])
def test_not_debt_or_novel_goes_to_triage(gh, capsys, verdict):
    epic, (t,) = attached(gh)
    code, out, err = run(capsys, "debt-intake", t, "--verdict", verdict, "--reason", "a new feature, not support")
    assert code == 0, err
    assert DEBT not in gh.issues[t]["labels"] and "sdlc:triage" in gh.issues[t]["labels"]
    assert t not in gh.subs[epic] and gh.issues[t]["state"] == "open"
    (rec,) = records(gh, t)
    assert rec["verdict"] == verdict and rec["epic"] == str(epic)
    assert "a new feature, not support" in gh.comments[t][-1]["body"] and "ordinary triage" in gh.comments[t][-1]["body"]
    assert f"/triage-issue {t}" in out
    listed = next_listed(capsys)
    assert {"number": t, "next": f"/triage-issue {t}"}.items() <= next(s for s in listed if s["number"] == t).items()


def test_fixed_closes_as_completed_naming_the_commit(gh, capsys):
    epic, (t,) = attached(gh)
    gh.on_main[SHA] = "behind"
    code, out, err = run(capsys, "debt-intake", t, "--verdict", "fixed", "--commit", SHA[:10], "--reason", "done in #200")
    assert code == 0, err
    assert (gh.issues[t]["state"], gh.issues[t]["state_reason"]) == ("closed", "completed")
    (rec,) = records(gh, t)
    assert rec["commit"] == SHA and SHA in gh.comments[t][-1]["body"]   # the full SHA, named
    assert ("close", ["issue", "close", str(t), "--repo", REPO, "--reason", "completed"]) in gh.writes


@pytest.mark.parametrize("status", ["ahead", "diverged", None])
def test_fixed_is_refused_for_a_commit_not_on_main(gh, capsys, status):
    epic, (t,) = attached(gh)
    if status:
        gh.on_main[SHA] = status
    code, _, err = run(capsys, "debt-intake", t, "--verdict", "fixed", "--commit", SHA, "--reason", "x")
    assert code == 1 and ("is not on origin/main" in err if status else "isn't known to GitHub" in err)
    assert gh.writes == [] and gh.issues[t]["state"] == "open"


@pytest.mark.parametrize("argv,named", [
    (["--verdict", "fixed", "--reason", "x"], "--commit SHA goes with --verdict fixed"),
    (["--verdict", "debt", "--commit", SHA, "--reason", "x"], "--commit SHA goes with --verdict fixed"),
    (["--verdict", "fixed", "--commit", "main", "--reason", "x"], "isn't a commit SHA"),
    (["--verdict", "duplicate", "--reason", "x"], "--of M goes with --verdict duplicate"),
    (["--verdict", "novel", "--of", "5", "--reason", "x"], "--of M goes with --verdict duplicate"),
    (["--verdict", "debt", "--reason", " "], "--reason says why"),
    (["--verdict", "debt", "--reason", "seen at 192.168.1.20"], "the repo is public"),
])
def test_bad_arguments_are_refused_before_any_read(monkeypatch, capsys, argv, named):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("refused before GitHub is read"))
    code, _, err = run(capsys, "debt-intake", 7, *argv)
    assert code == 1 and named in err


def test_duplicate_closes_naming_the_other(gh, capsys):
    epic, (t,) = attached(gh)
    other = gh.issue("the same fix", state="closed")   # a closed issue may be the original
    code, out, err = run(capsys, "debt-intake", t, "--verdict", "duplicate", "--of", other, "--reason", "same CLAUDE.md fix")
    assert code == 0, err
    assert (gh.issues[t]["state"], gh.issues[t]["state_reason"]) == ("closed", "duplicate")
    assert f"Duplicate of #{other}" in gh.comments[t][-1]["body"] and records(gh, t)[0]["of"] == str(other)
    assert ("close", ["issue", "close", str(t), "--repo", REPO, "--duplicate-of", str(other)]) in gh.writes


def test_duplicate_of_a_missing_issue_itself_or_a_pr_is_refused(gh, capsys):
    epic, (t,) = attached(gh)
    pr = gh.issue("a PR")
    gh.issues[pr]["pr"] = True
    for of, named in ((999, "#999 isn't an issue here"), (t, "a duplicate of itself"), (pr, "is a pull request")):
        code, _, err = run(capsys, "debt-intake", t, "--verdict", "duplicate", "--of", of, "--reason", "x")
        assert code == 1 and named in err, err
    assert gh.writes == []


def test_debt_on_every_ticket_ends_intake(gh, capsys):
    epic, (a, b, c) = attached(gh, 3)
    gh.on_main[SHA] = "identical"
    assert run(capsys, "debt-intake", a, "--verdict", "debt", "--reason", "the alarm server needed it")[0] == 0
    assert records(gh, a)[0]["verdict"] == "debt" and gh.issues[a]["labels"] == [DEBT] and a in gh.subs[epic]
    st = json.loads(run(capsys, "state", epic, "--json")[1])
    assert st["action"] == "debt_intake" and st["debt"]["intake_pending"] == [b, c]
    assert run(capsys, "debt-intake", b, "--verdict", "fixed", "--commit", SHA, "--reason", "x")[0] == 0
    assert run(capsys, "debt-intake", c, "--verdict", "debt", "--reason", "y")[0] == 0
    st = json.loads(run(capsys, "state", epic, "--json")[1])
    # intake is done: a pass of the two left is due (T5)
    assert (st["action"], st["turn"]) == ("plan_pass", "agent") and [x["number"] for x in st["debt"]["tickets"]] == [a, c]
    before = len(gh.writes)   # the same verdict again posts nothing
    assert run(capsys, "debt-intake", a, "--verdict", "debt", "--reason", "again")[0] == 0 and len(gh.writes) == before


def test_a_cut_short_verdict_keeps_intake_due_and_a_rerun_finishes_it(gh, capsys):
    epic, (t,) = attached(gh)
    gh.intake(t, "not-debt", epic=str(epic))   # recorded, then the run died before the labels and the detach
    st = json.loads(run(capsys, "state", epic, "--json")[1])
    assert st["action"] == "debt_intake" and st["debt"]["intake_pending"] == [t] and "cut short" in st["next"]
    code, _, err = run(capsys, "debt-intake", t, "--verdict", "not-debt", "--reason", "x")
    assert code == 0, err
    assert len(records(gh, t)) == 1 and t not in gh.subs[epic] and "sdlc:triage" in gh.issues[t]["labels"]
    # labels moved but still attached: a rerun still finishes it, though the ticket is no longer tech-debt
    u = gh.ticket("u")
    gh.subs[epic].append(u)
    gh.intake(u, "novel")
    gh.issues[u]["labels"] = ["sdlc:triage"]
    assert run(capsys, "debt-intake", u, "--verdict", "novel", "--reason", "x")[0] == 0 and u not in gh.subs[epic]


def test_a_fresh_verdict_needs_tech_debt_in_the_debt_epic(gh, capsys):
    epic, (t,) = attached(gh)
    loose, plain = gh.ticket("loose"), gh.issue("a feature", ["enhancement"])
    gone = gh.ticket("closed", state="closed")
    gh.subs[epic] += [plain, gone]
    for n, named in ((loose, "run `debt-epic` first"), (plain, "isn't labelled `tech-debt`"), (gone, "is closed")):
        code, _, err = run(capsys, "debt-intake", n, "--verdict", "debt", "--reason", "x")
        assert code == 1 and named in err, err
    assert gh.writes == []


@pytest.mark.parametrize("argv", [["--verdict", "debt"], ["--verdict", "not-debt"], ["--verdict", "novel"],
                                  ["--verdict", "fixed", "--commit", SHA], ["--verdict", "duplicate", "--of", "OTHER"]])
def test_dry_run_writes_nothing(gh, capsys, argv):
    epic, (t,) = attached(gh)
    other = gh.issue("other")
    gh.on_main[SHA] = "behind"
    argv = [str(other) if a == "OTHER" else a for a in argv]
    code, out, err = run(capsys, "debt-intake", t, *argv, "--reason", "why", "--dry-run")
    assert code == 0, err
    assert gh.writes == [] and gh.comments[t] == [] and t in gh.subs[epic]
    assert out.startswith(f"would post the intake record on #{t}")


def test_an_untrusted_intake_record_does_not_count(gh, capsys):
    epic, (t,) = attached(gh)
    gh.intake(t, "not-debt", author=STRANGER)
    st = json.loads(run(capsys, "state", epic, "--json")[1])
    assert st["debt"]["intake_pending"] == [t] and "without a verdict" in st["next"]


def test_an_unknown_verdict_counts_as_none(gh, capsys):
    epic, (t,) = attached(gh)
    gh.intake(t, "not_debt")   # a hand-made marker with a verdict debt-intake doesn't take
    st = json.loads(run(capsys, "state", epic, "--json")[1])
    assert st["debt"]["intake_pending"] == [t] and "without a verdict" in st["next"] and "cut short" not in st["next"]


@pytest.mark.parametrize("verdict", ["not-debt", "novel"])
def test_a_rerun_after_triage_moved_on_changes_nothing(gh, capsys, verdict):
    """Codex on #271: a finished handoff is never pulled back to sdlc:triage."""
    epic, (t,) = attached(gh)
    assert run(capsys, "debt-intake", t, "--verdict", verdict, "--reason", "x")[0] == 0
    gh.issues[t]["labels"] = ["sdlc:prd-review"]   # triage moved on
    before = len(gh.writes)
    code, out, err = run(capsys, "debt-intake", t, "--verdict", verdict, "--reason", "x")
    assert code == 0, err
    assert len(gh.writes) == before and gh.issues[t]["labels"] == ["sdlc:prd-review"] and len(records(gh, t)) == 1
    code, out, _ = run(capsys, "debt-intake", t, "--verdict", verdict, "--reason", "x", "--dry-run")
    assert code == 0 and "already recorded" in out


# --- passes: a plan of which tickets, checked by the tool and posted with no sign-off (T5) ---------------------------

class FakeGit:
    """origin as `git` sees it after the one fetch: main's files, and what each epic branch changes since main."""

    def __init__(self, tracked=("tools/sdlc.py", "tools/sdlc_quick.py", "docs/sdlc.html", "CLAUDE.md",
                                "tests/test_sdlc_debt.py", "src/twiddle/cli.py"), branches=None):
        self.tracked, self.branches, self.calls = list(tracked), dict(branches or {}), []

    def __call__(self, args, cwd=None, check=True, timeout=None, env=None):
        args = list(args)
        self.calls.append(args)
        if args[0] == "fetch":
            assert timeout and timeout > 0, "the fetch is bounded"
            assert "+refs/heads/epic/*:refs/remotes/origin/epic/*" in args
            return ""
        if args[0] == "ls-tree":
            assert args[-1] == "origin/main"
            return "\0".join(self.tracked)
        if args[0] == "rev-parse":
            return "a" * 40 if int(args[-1].rsplit("/", 1)[1]) in self.branches else ""
        if args[0] == "diff":
            assert args[-1].startswith("origin/main...origin/epic/")
            return "\0".join(self.branches[int(args[-1].rsplit("/", 1)[1])])
        pytest.fail(f"git {args}: the fake doesn't know this call")

    def fetches(self):
        return [c for c in self.calls if c[0] == "fetch"]


@pytest.fixture
def git(monkeypatch):
    fake = FakeGit()
    monkeypatch.setattr(sdlc, "git", fake)
    return fake


def ready(gh, n=2, **kw):
    """The debt epic with `n` tickets through intake (`debt`)."""
    epic = gh.debt_epic()
    tickets = [gh.ticket(f"debt {i}", **kw) for i in range(n)]
    gh.subs[epic] = list(tickets)
    for t in tickets:
        gh.intake(t)
    return epic, tickets


def group(*tickets, files=("src/twiddle/cli.py",), scope="fix it", complexity="routine", **extra):
    return {"tickets": list(tickets), "scope": scope, "files": list(files), "complexity": complexity,
            "complexity_reason": "one function", **extra}


def pass_file(tmp_path, epic, groups, k=1, **extra):
    f = tmp_path / "pass.json"
    f.write_text(json.dumps({"epic": epic, "pass": k, "groups": groups, **extra}))
    return f


def pass_comments(gh, epic):
    return [c["body"] for c in gh.comments[epic] if f"kind={sdlc.DEBT_PASS_KIND} " in c["body"]]


def test_a_valid_pass_is_posted_with_no_sign_off_and_state_names_it(gh, git, capsys, tmp_path):
    epic, (a, b, c) = ready(gh, 3)
    st = ticket_state(capsys, epic)
    assert (st["action"], st["turn"]) == ("plan_pass", "agent") and st["debt"]["eligible"] == [a, b, c]
    assert f"`uv run python tools/sdlc.py debt-pass-validate {epic} pass.json`" in st["next"]
    f = pass_file(tmp_path, epic, [group(b, c, files=("src/twiddle/cli.py", "docs/")), group(a)])
    code, out, err = run(capsys, "debt-pass-validate", epic, f)
    assert code == 0, err
    assert gh.writes == [] and len(git.fetches()) == 1   # read-only, apart from the one bounded fetch
    code, out, err = run(capsys, "debt-pass-post", epic, f)
    assert code == 0, err
    assert f"debt epic #{epic}: pass 1 posted" in out
    assert len(gh.writes) == 1 and len(git.fetches()) == 2   # the comment, and post's own one fetch
    (body,) = pass_comments(gh, epic)
    assert sdlc.parse_marker(body) == {"kind": sdlc.DEBT_PASS_KIND, "pass": "1"}
    assert "/approve" not in body and "plan-review" not in body and "sign off" not in body
    assert sdlc.extract_pass(body) == json.loads(f.read_text())
    assert f"1. `debt-1-1` · #{b}, #{c} · *routine*: fix it" in body
    st = ticket_state(capsys, epic)   # the pass is open: nothing new is planned, and no /approve is asked for
    assert (st["action"], st["turn"]) == ("none", "none") and st["debt"]["open_pass"] == 1
    assert st["debt"]["passes"][0]["tickets"] == [b, c, a]
    assert st["next"] == (f"nothing new: debt epic #{epic}'s pass 1 is open (#{b}, #{c}, #{a}); "
                          "the next pass waits until it ends")
    code, out, _ = run(capsys, "state", epic)
    assert f"pass 1: #{b}, #{c}, #{a}   (open)" in out


def test_a_pass_posted_twice_is_refused(gh, git, capsys, tmp_path):
    epic, (a, _) = ready(gh, 2)
    f = pass_file(tmp_path, epic, [group(a)])
    assert run(capsys, "debt-pass-post", epic, f)[0] == 0
    code, _, err = run(capsys, "debt-pass-post", epic, f)
    assert code != 0 and "`pass` is 1: debt epic" in err and "pass 1 is open: the next pass waits until it ends" in err
    assert f"#{a} is already in open pass 1" in err
    assert len(pass_comments(gh, epic)) == 1


@pytest.mark.parametrize("groups, named", [
    (lambda a, b: [group(a, a)], "is listed 2 times (in group 1)"),
    (lambda a, b: [group(a), group(b, a)], "is listed 2 times (in groups 1, 2)"),
    (lambda a, b: [group()], "group 1 is empty"),
    (lambda a, b: [group(a, b, 1001, 1002, 1003)], "group 1 has 5 tickets, over debt.max_per_pr (4)"),
    (lambda a, b: [group(a, scope=" ")], "group 1 has no scope"),
    (lambda a, b: [group(a, scope="one\ntwo")], "group 1's scope is more than one line"),
    (lambda a, b: [{k: v for k, v in group(a).items() if k != "scope"}], "group 1 has no scope"),
    (lambda a, b: [group(a, files=())], "group 1 has no files"),
    (lambda a, b: [group(a, files=("/etc/x", "tools/*.py"))], "aren't repo-relative paths"),
    (lambda a, b: [group(a, complexity="novel")], "rated `novel`: a pass makes no design choices"),
    (lambda a, b: [group(a, complexity="hard")], "complexity is 'hard'"),
    (lambda a, b: [], "`groups` lists the pass's PRs"),
    (lambda a, b: [group(a, size=3)], "unknown key(s) ['size']"),
])
def test_a_badly_formed_pass_is_refused_and_nothing_is_posted(gh, git, capsys, tmp_path, groups, named):
    epic, (a, b) = ready(gh, 2)
    code, _, err = run(capsys, "debt-pass-post", epic, pass_file(tmp_path, epic, groups(a, b)))
    assert code != 0 and named in err and "nothing was posted" in err
    assert gh.writes == []


def test_every_refusal_is_listed_with_its_reason_and_nothing_is_posted(gh, git, capsys, tmp_path):
    epic, ok = ready(gh, 1)
    closed = gh.ticket("closed", state="closed")
    plain = gh.issue("not debt", ["enhancement"])
    loose = gh.ticket("loose")                      # tech debt, but not in this epic
    unjudged = gh.ticket("unjudged")
    novel = gh.ticket("novel")
    feature = gh.feature_epic(created=("M1",))     # building, M1 not shipped
    waiting = gh.issue("waits", [DEBT], body=sdlc.debt_head(feature, "review", waits="M1", debt_epic=epic))
    gh.subs[epic] += [closed, plain, unjudged, novel, waiting]
    for t in (closed, plain, loose, waiting):
        gh.intake(t)
    gh.intake(novel, verdict="novel")
    gh.say(epic, sdlc.DEBT_PASS_KIND, **{"pass": "1"})   # an open pass 1 holding ok[0] (its JSON is read below)
    gh.comments[epic][-1]["body"] += "\n" + sdlc.render_pass({"epic": epic, "pass": 1, "groups": [group(ok[0])]})
    many = [gh.ticket(f"m{i}") for i in range(4)]
    gh.subs[epic] += many
    for t in many:
        gh.intake(t)
    git.branches[feature] = ["src/twiddle/cli.py"]   # the feature epic changes cli.py on its branch
    groups = [group(ok[0], closed, plain, files=("docs/sdlc.html",)), group(loose, unjudged, novel, waiting),
              group(*many, files=("src/twiddle/",))]
    code, _, err = run(capsys, "debt-pass-post", epic, pass_file(tmp_path, epic, groups, k=2))
    assert code != 0 and gh.writes == []
    for reason in ["pass 1 is open: the next pass waits until it ends",
                   f"#{ok[0]} is already in open pass 1",
                   f"#{closed} is closed",
                   f"#{plain} isn't labelled `{DEBT}`",
                   f"#{loose} isn't a sub-issue of debt epic #{epic}",
                   f"#{unjudged} has no intake verdict (`debt-intake {unjudged}` first)",
                   f"#{novel}'s intake verdict is `novel`, not `debt`",
                   f"#{waiting} waits for #{feature}'s M1 to ship to main",
                   "the pass takes 11 tickets, over debt.pass_size (8)",
                   f"group 3 (" + ", ".join(f"#{t}" for t in many) + f") overlaps epic #{feature}, which is being "
                   "built: `src/twiddle/cli.py`"]:
        assert reason in err, reason
    assert "group 1 (" not in err   # docs/sdlc.html: nobody is working on it


def test_a_group_naming_files_in_an_open_slices_scope_is_an_overlap(gh, git, capsys, tmp_path):
    """No PR, nothing on the epic's branch: the Scope alone claims its files, a new one main lacks included."""
    epic, (a, b, c) = ready(gh, 3)
    feature = gh.feature_epic(created=("M1",))
    gh.issue(f"#{feature} T1: a slice", ["plan:slice"], body=(
        f"<!-- sdlc:v1 kind=slice epic={feature} key=T1 -->\nEpic: #{feature}\n\n## Outcome\n\nIt works.\n\n## Scope\n\n"
        "`tools/sdlc.py`: a new command, tested in a new tests/test_sdlc_new.py.\n\n## Acceptance criteria\n\n"
        "- [ ] CLAUDE.md says so\n"))
    groups = [group(a, files=("tools/sdlc.py",)), group(b, files=("tests/test_sdlc_new.py",)),
              group(c, files=("CLAUDE.md", "docs/sdlc.html"))]
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, groups))
    assert code == 1 and gh.writes == []
    assert f"group 1 (#{a}) overlaps epic #{feature}, which is being built: `tools/sdlc.py`" in err
    assert f"group 2 (#{b}) overlaps epic #{feature}, which is being built: `tests/test_sdlc_new.py`" in err
    assert "group 3" not in err   # CLAUDE.md is named outside the Scope section


def test_an_open_pr_into_an_epic_branch_claims_its_files(gh, git, capsys, tmp_path):
    epic, (a, b) = ready(gh, 2)
    feature = gh.feature_epic(created=("M1",))
    gh.pulls[f"epic/{feature}"] = [(7, ["tools/sdlc_quick.py"])]
    groups = [group(a, files=("tools/",)), group(b, files=("docs/sdlc.html",))]
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, groups))
    assert code == 1 and f"group 1 (#{a}) overlaps epic #{feature}, which is being built: `tools/sdlc_quick.py`" in err
    assert "group 2" not in err


def test_shipped_or_quick_epics_claim_nothing(gh, git, capsys, tmp_path):
    epic, (a,) = ready(gh, 1)
    done = gh.feature_epic(created=("M1",), shipped=("M1",))
    gh.issues[done]["labels"] = ["sdlc:done"]
    gh.quick_change()
    git.branches[done] = ["src/twiddle/cli.py"]
    assert run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, [group(a)]))[0] == 0


def test_a_failed_fetch_checks_nothing_and_posts_nothing(gh, git, capsys, tmp_path, monkeypatch):
    epic, (a,) = ready(gh, 1)

    def offline(args, **kw):
        raise sdlc.SdlcError("git fetch -q origin timed out after 60s")
    monkeypatch.setattr(sdlc, "git", offline)
    code, _, err = run(capsys, "debt-pass-post", epic, pass_file(tmp_path, epic, [group(a)]))
    assert code != 0 and "the overlap check needs origin's main and epic branches" in err and gh.writes == []


def test_post_dry_run_writes_nothing(gh, git, capsys, tmp_path):
    epic, (a,) = ready(gh, 1)
    code, out, err = run(capsys, "debt-pass-post", epic, pass_file(tmp_path, epic, [group(a)]), "--dry-run")
    assert code == 0, err
    assert f"DRY RUN debt epic #{epic}: pass 1" in out and "<!-- pass-json -->" in out
    assert gh.writes == [] and len(git.fetches()) == 1


def test_a_pass_on_anything_but_the_open_debt_epic_is_refused(gh, git, capsys, tmp_path):
    feature = gh.feature_epic()
    code, _, err = run(capsys, "debt-pass-validate", feature, pass_file(tmp_path, feature, [group(1)]))
    assert code != 0 and "isn't the debt epic" in err and git.calls == []


# --- the debt epic's state: plan_pass, debt_waiting, and the next pass after one ends ----------------------------

def view(tickets, comments=()):
    st = sdlc.derive_state(epic_issue(), list(comments), {OWNER}, CONFIG)
    return sdlc.with_next(st, CONFIG, offline=True, bundle={"comments": list(comments), "trusted": [OWNER],
                                                            "debt": {"tickets": tickets}})


def pass_record(k, tickets, kind=None, at=1):
    body = sdlc.render_comment(kind or sdlc.DEBT_PASS_KIND, None, sdlc.render_pass(
        {"epic": 250, "pass": k, "groups": [group(*tickets)]}) if kind is None else "ended", CONFIG, **{"pass": str(k)})
    return {"id": at, "author": OWNER, "body": body, "created_at": f"2026-10-21T00:00:{at:02d}Z", "url": f"u{at}"}


def test_plan_pass_only_when_intake_is_done_no_pass_is_open_and_a_ticket_is_eligible():
    waits = {"epic": 12, "milestone": "M1"}
    st = view([{"number": 1, "intake": "debt"}, {"number": 2, "intake": None}])
    assert st["action"] == "debt_intake"
    st = view([{"number": 1, "intake": "debt"}, {"number": 2, "intake": "debt", "waiting_for": waits}])
    assert (st["action"], st["turn"], st["debt"]["eligible"]) == ("plan_pass", "agent", [1])
    assert "plan debt pass 1 from 1 eligible ticket(s) (#1;" in st["next"]
    open_pass = [pass_record(1, [1])]
    st = view([{"number": 1, "intake": "debt"}, {"number": 3, "intake": "debt"}], open_pass)
    assert (st["action"], st["turn"]) == ("none", "none") and "pass 1 is open (#1)" in st["next"]
    ended = open_pass + [pass_record(1, [], kind=sdlc.DEBT_PASS_END_KIND, at=2)]
    st = view([{"number": 3, "intake": "debt"}], ended)   # joined during pass 1: it forms pass 2
    assert st["action"] == "plan_pass" and "plan debt pass 2 from 1 eligible ticket(s) (#3;" in st["next"]


def test_debt_waiting_names_what_each_ticket_waits_on():
    st = view([{"number": 1, "intake": "debt", "waiting_for": {"epic": 12, "milestone": "M1"}},
               {"number": 2, "intake": "debt", "waits_on_person": True}])
    assert (st["action"], st["turn"]) == ("debt_waiting", "none")
    assert st["next"] == ("nothing yet: every ticket of debt epic #250 through intake waits: "
                          "#1 waits for #12's M1 to ship to main; #2 waits on a person")


def test_state_of_the_debt_epic_reads_what_its_tickets_wait_for_on_github(gh, capsys):
    epic = gh.debt_epic()
    feature = gh.feature_epic(created=("M1",))
    t = gh.issue("waits", [DEBT], body=sdlc.debt_head(feature, "review", waits="M1", debt_epic=epic))
    gh.subs[epic] = [t]
    gh.intake(t)
    st = ticket_state(capsys, epic)
    assert st["action"] == "debt_waiting" and f"#{t} waits for #{feature}'s M1 to ship to main" in st["next"]
    assert st["turn"] == "none"   # nothing for an agent: `next` doesn't list it
    gh.say(feature, "shipped", milestone="M1", sha="b" * 40)
    assert ticket_state(capsys, epic)["action"] == "plan_pass"


def test_scope_paths_reads_the_real_scope_texts():
    tracked = {"tools/sdlc.py", "tools/sdlc_quick.py", ".sdlc/config.json", "tests/test_sdlc_epic.py", "SDLC.md",
               "CLAUDE.md", "docs/sdlc.html", ".agents/skills/plan-issue/SKILL.md", "README.md"}
    t9 = ("tools/sdlc.py: remove cleanup_owed, cleanup_due; `.sdlc/config.json` key; tests in tests/test_sdlc_epic.py "
          "and test_sdlc_plan.py that assert cleanup behaviour. `.agents/skills/plan-issue/SKILL.md` and its "
          "plan-schema reference.")
    assert sdlc.scope_paths(t9, tracked) == {"tools/sdlc.py", ".sdlc/config.json", "tests/test_sdlc_epic.py",
                                             "test_sdlc_plan.py", ".agents/skills/plan-issue/SKILL.md"}
    t10 = ("SDLC.md, docs/sdlc.html, CLAUDE.md (tools/sdlc.py row only), .agents/skills/{plan-issue (pass plans), "
           "work-slice}/SKILL.md; a new tests/test_sdlc_debt_e2e.py in the style of tests/test_sdlc_quick_e2e.py. "
           "See https://github.com/o/r/blob/main/docs/prd/243-x.md, e.g. origin/main...origin/epic/N, sdlc.py, docs")
    assert sdlc.scope_paths(t10, tracked) == {"SDLC.md", "docs/sdlc.html", "CLAUDE.md", "tools/sdlc.py",
                                              ".agents/skills/", "tests/test_sdlc_debt_e2e.py",
                                              "tests/test_sdlc_quick_e2e.py",
                                              # prose shaped like a path is claimed too: it only refuses more
                                              "origin/main...origin/epic/N", "sdlc.py", "e.g", "docs/prd/243-x.md"}
    assert sdlc.scope_paths("[helper](./tools/sdlc.py) and /CLAUDE.md", tracked) == {"tools/sdlc.py", "CLAUDE.md"}
    assert sdlc.scope_paths("tools/sdlc.py:120, SDLC.md#lanes", tracked) == {"tools/sdlc.py", "SDLC.md"}
    assert sdlc.scope_paths("[it](https://github.com/o/r/blob/main/tools/sdlc_quick.py#L3)", tracked) == {
        "tools/sdlc_quick.py"}
    # any other URL: every tail of its path (a raw file, a fork); `..`, a backslash and an escape all still name it
    assert {"tools/sdlc.py", "SDLC.md"} <= sdlc.scope_paths(
        "https://raw.githubusercontent.com/o/r/feature/x/tools/sdlc.py and https://x.org/a/SDLC.md?v=1", tracked)
    assert sdlc.scope_paths(r"[a](../tools/sdlc.py), tools\sdlc_quick.py, docs\_x/new\_file.py", tracked) == {
        "tools/sdlc.py", "tools/sdlc_quick.py", "docs_x/new_file.py"}
    # a ref with a slash: a known branch shape is taken off, else the longest tail that is on main
    for url, want in [("https://github.com/o/r/blob/epic/243/tools/sdlc.py", "tools/sdlc.py"),
                      ("https://github.com/o/r/blob/epic/243/tests/test_new_thing.py", "tests/test_new_thing.py"),
                      ("https://github.com/o/r/blob/0123abcd/SDLC.md", "SDLC.md"),
                      ("https://github.com/o/r/tree/main/tools", "tools/"),
                      ("https://github.com/o/r/blob/slice/9/docs/sdlc.html", "docs/sdlc.html"),
                      ("https://github.com/o/r/blob/slice/123/tests/test_new_debt.py", "tests/test_new_debt.py")]:
        assert sdlc.scope_paths(f"see [it]({url})", tracked) == {want}, url
    # an unknown ref and a path main lacks: the split can't be known, so every tail is claimed
    assert {"tests/new_x.py", "x/tests/new_x.py"} <= sdlc.scope_paths(
        "[n](https://github.com/o/r/blob/feature/x/tests/new_x.py)", tracked)
    # ... even when a tail is on main: it proves nothing about where the ref ends
    assert {"new-doc/docs/new-guide/README.md", "docs/new-guide/README.md", "new-guide/README.md", "README.md"} \
        <= sdlc.scope_paths("[g](https://github.com/o/r/blob/feature/new-doc/docs/new-guide/README.md)", tracked)
    assert "tools/" in sdlc.scope_paths("[t](https://github.com/o/r/tree/feature/x/y/tools/)", tracked)
    # a bare top-level directory word (no slash) claims nothing: prose `tools` or `docs` would claim too much
    assert sdlc.scope_paths("The scripts in tools and the docs", tracked) == set()
    marked = "__tools/sdlc.py__, ~~SDLC.md~~, <code>CLAUDE.md</code>, |docs/sdlc.html|, tools/sdlc_quick.py;README.md"
    assert sdlc.scope_paths(marked, tracked) == {"tools/sdlc.py", "SDLC.md", "CLAUDE.md", "docs/sdlc.html",
                                                 "tools/sdlc_quick.py", "README.md"}
    assert sdlc.scope_paths("«tests/test_sdlc_epic.py» and ‹src/new/x.py›", tracked) == {"tests/test_sdlc_epic.py",
                                                                                          "src/new/x.py"}
    named = ["tests/test_sdlc_epic.py", "src/x.py", "tools/"]   # the backstop: a pass's files, whatever surrounds them
    got = sdlc.scope_paths("«tests/test_sdlc_epic.py» and xsrc/x.py and tools", tracked, named)
    assert "tests/test_sdlc_epic.py" in got and "src/x.py" not in got and "tools/" not in got
    md = "**tools/sdlc.py** and *SDLC.md*, see [the guide](docs/sdlc.html) and [CLAUDE.md](https://x.org/CLAUDE.md)"
    assert sdlc.scope_paths(md, tracked) == {"tools/sdlc.py", "SDLC.md", "docs/sdlc.html", "CLAUDE.md"}
    assert sdlc.paths_overlap("tools/", "tools/sdlc.py") and sdlc.paths_overlap("tools/sdlc.py", "tools")
    assert not sdlc.paths_overlap("tools/sdlc.py", "tools/sdlc_quick.py") and not sdlc.paths_overlap("tool", "tools/x")


def test_a_new_path_named_without_a_directory_or_an_extension_is_still_claimed(gh, git, capsys, tmp_path):
    epic, (a, b, c) = ready(gh, 3)
    feature = gh.feature_epic(created=("M1",))
    gh.issue(f"#{feature} T2: a slice", ["plan:slice"], body=(
        f"<!-- sdlc:v1 kind=slice epic={feature} key=T2 -->\n## Scope\n\nA new tools/new-helper script and a "
        "NEW_GUIDE.md beside the README.\n\n## Non-goals\n\nnothing in docs/sdlc.html\n"))
    groups = [group(a, files=("tools/new-helper",)), group(b, files=("NEW_GUIDE.md",)), group(c, files=("docs/sdlc.html",))]
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, groups))
    assert code == 1
    assert f"group 1 (#{a}) overlaps epic #{feature}, which is being built: `tools/new-helper`" in err
    assert f"group 2 (#{b}) overlaps epic #{feature}, which is being built: `NEW_GUIDE.md`" in err
    assert "group 3" not in err   # named under Non-goals, not Scope


def test_a_malformed_files_or_tickets_value_is_reported_not_raised(gh, git, capsys, tmp_path):
    epic, (a,) = ready(gh, 1)
    feature = gh.feature_epic(created=("M1",))
    git.branches[feature] = ["src/twiddle/cli.py"]
    groups = [{**group(a), "files": 42}, {**group(), "tickets": "x"}]
    code, _, err = run(capsys, "debt-pass-post", epic, pass_file(tmp_path, epic, groups))
    assert code != 0 and "group 1 has no files" in err and "group 2 is empty" in err and gh.writes == []


def test_a_ticket_waiting_on_a_person_is_left_out_until_they_reply(gh, git, capsys, tmp_path):
    epic, (a, b) = ready(gh, 2)
    gh.comment(a, sdlc.marker(sdlc.DEBT_REQUEST_KIND) + "\nPlease run it.")   # T8b's request on the ticket
    st = ticket_state(capsys, epic)
    assert st["action"] == "plan_pass" and st["debt"]["eligible"] == [b]
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, [group(a), group(b)]))
    assert code == 1 and f"#{a} waits on a person" in err and f"#{b}" not in err
    gh.comment(b, sdlc.marker(sdlc.DEBT_REQUEST_KIND) + "\nPlease run it.")
    st = ticket_state(capsys, epic)
    assert st["action"] == "debt_waiting" and f"#{a} waits on a person; #{b} waits on a person" in st["next"]
    gh.comment(a, "I ran it: the Roam played, then I restored it.", author=STRANGER)   # not the author: still waits
    assert ticket_state(capsys, epic)["action"] == "debt_waiting"
    gh.comment(a, "Done, it works.")   # a collaborator's reply puts it back
    st = ticket_state(capsys, epic)
    assert st["action"] == "plan_pass" and st["debt"]["eligible"] == [a]


def test_a_scope_path_written_with_a_leading_dot_slash_still_overlaps(gh, git, capsys, tmp_path):
    epic, (a,) = ready(gh, 1)
    feature = gh.feature_epic(created=("M1",))
    gh.issue(f"#{feature} T3: a slice", ["plan:slice"], body=(
        f"<!-- sdlc:v1 kind=slice epic={feature} key=T3 -->\n## Scope\n\nThe [helper](./tools/sdlc.py).\n"))
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, [group(a, files=("tools/sdlc.py",))]))
    assert code == 1 and f"group 1 (#{a}) overlaps epic #{feature}, which is being built: `tools/sdlc.py`" in err


def test_a_scope_path_in_underscore_emphasis_still_overlaps(gh, git, capsys, tmp_path):
    epic, (a, b) = ready(gh, 2)
    feature = gh.feature_epic(created=("M1",))
    gh.issue(f"#{feature} T4: a slice", ["plan:slice"], body=(
        f"<!-- sdlc:v1 kind=slice epic={feature} key=T4 -->\n## Scope\n\n__tools/sdlc.py__ and «src/twiddle/cli.py».\n"))
    groups = [group(a, files=("tools/sdlc.py",)), group(b, files=("src/twiddle/cli.py",))]
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, groups))
    assert code == 1 and f"group 1 (#{a}) overlaps epic #{feature}, which is being built: `tools/sdlc.py`" in err
    assert f"group 2 (#{b}) overlaps epic #{feature}, which is being built: `src/twiddle/cli.py`" in err
    # a group naming a directory that holds them: the Scope's paths are read without the pass's own files
    groups = [group(a, files=("tools/",)), group(b, files=("src/twiddle/",))]
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, groups))
    one, two = (l for l in err.splitlines() if "overlaps" in l)
    assert code == 1 and one.startswith(f"  - group 1 (#{a}) overlaps epic #{feature}") and "`tools/sdlc.py`" in one
    assert two.startswith(f"  - group 2 (#{b}) overlaps epic #{feature}") and "`src/twiddle/cli.py`" in two


DEEP = "src/twiddle/scene_data/builder_x.py"
SPELLINGS = ["**{p}**", "__{p}__", "~~{p}~~", "`{p}`", "<code>{p}</code>", "«{p}»", '"{p}"', "({p})", "[x]({p})",
             "[x](./{p})", "[x](https://github.com/o/r/blob/main/{p})", "|{p}|", "{p}:12", "{p}#L3", "{p},", "{p}.",
             "{p};", "{esc}"]


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("path, files", [
    (DEEP, DEEP), (DEEP, "src/twiddle/scene_data/"), (DEEP, "src/twiddle/"), ("CLAUDE.md", "CLAUDE.md")])
def test_every_spelling_of_a_scope_path_overlaps_its_file_and_its_directories(gh, git, capsys, tmp_path,
                                                                            spelling, path, files):
    """However a Scope writes a path, a group naming it, or a directory holding it, is refused as an overlap; the
    slice has no PR and its epic's branch changes nothing."""
    git.tracked.append(DEEP)
    epic, (a,) = ready(gh, 1)
    feature = gh.feature_epic(created=("M1",))
    named = spelling.format(p=path, esc=path.replace("_", "\\_"))
    gh.issue(f"#{feature} T5: a slice", ["plan:slice"], body=(
        f"<!-- sdlc:v1 kind=slice epic={feature} key=T5 -->\n## Scope\n\nChanges {named} and nothing else\n"))
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, [group(a, files=(files,))]))
    assert code == 1 and f"group 1 (#{a}) overlaps epic #{feature}, which is being built: " in err, err
    assert f"`{path}`" in err.split("which is being built: ", 1)[1], err


def test_a_link_to_a_file_on_an_epic_branch_overlaps(gh, git, capsys, tmp_path):
    epic, (a, b) = ready(gh, 2)
    feature = gh.feature_epic(created=("M1",))
    gh.issue(f"#{feature} T6: a slice", ["plan:slice"], body=(
        f"<!-- sdlc:v1 kind=slice epic={feature} key=T6 -->\n## Scope\n\n"
        f"[the gate](https://github.com/{REPO}/blob/epic/{feature}/tools/sdlc.py#L10)\n"))
    groups = [group(a, files=("tools/sdlc.py",)), group(b, files=("tools/",))]
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, groups))
    hits = [l for l in err.splitlines() if "overlaps" in l]
    assert code == 1 and len(hits) == 2 and all("`tools/sdlc.py`" in l for l in hits), err


@pytest.mark.parametrize("ref", ["slice/123", "feature/x", "epic/9", "feature/new-doc"])
@pytest.mark.parametrize("path, files", [("tests/test_new_debt.py", "tests/test_new_debt.py"),
                                         ("tests/test_new_debt.py", "tests/"),
                                         ("docs/new-guide/CLAUDE.md", "docs/new-guide/CLAUDE.md"),
                                         ("docs/new-guide/CLAUDE.md", "docs/new-guide/")])
def test_a_link_to_a_new_file_on_any_branch_overlaps_it_and_its_directory(gh, git, capsys, tmp_path, ref, path,
                                                                           files):
    """A new file main lacks, linked on any branch, even when a tail of it (`CLAUDE.md`) is on main."""
    epic, (a,) = ready(gh, 1)
    feature = gh.feature_epic(created=("M1",))
    gh.issue(f"#{feature} T7: a slice", ["plan:slice"], body=(
        f"<!-- sdlc:v1 kind=slice epic={feature} key=T7 -->\n## Scope\n\n"
        f"[new](https://github.com/{REPO}/blob/{ref}/{path})\n"))
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, [group(a, files=(files,))]))
    assert code == 1 and f"`{path}`" in err, err


@pytest.mark.parametrize("body", [
    "## Scope\r\n\r\n`tools/sdlc.py` gains a command.\r\n\r\n## Non-goals\r\n\r\nNone.\r\n",   # CRLF
    "### Scope (what changes)\n\ntools/sdlc.py\n\n### Non-goals\n\nNone.\n",               # another level, more text
    "## scope\n\ntools/sdlc.py\n",                                                           # lower case
    "No sections at all: it edits tools/sdlc.py.\n",                                           # no heading: the body
    "## Scope\n\nA command.\n\n### Files\n\n- tools/sdlc.py\n\n## Non-goals\n\nNone.\n",       # its own subheading
    "## Scope\n\n```sh\n# a comment\n```\n\nthen tools/sdlc.py\n\n## Context\n\nx\n",            # a fenced `#` line
    "## Scope\n\n<code>tools&#47;sdlc.py</code>\n",                                             # HTML entities
    "## Scope\n\n[x](https://github.com/o/r/blob/main/tools%2Fsdlc.py)\n",                      # percent-encoded
])
def test_any_shape_of_scope_section_is_read(gh, git, capsys, tmp_path, body):
    epic, (a,) = ready(gh, 1)
    feature = gh.feature_epic(created=("M1",))
    gh.issue(f"#{feature} T8: a slice", ["plan:slice"], body=f"<!-- sdlc:v1 kind=slice epic={feature} key=T8 -->\n{body}")
    code, _, err = run(capsys, "debt-pass-validate", epic, pass_file(tmp_path, epic, [group(a, files=("tools/sdlc.py",))]))
    assert code == 1 and f"group 1 (#{a}) overlaps epic #{feature}" in err and "`tools/sdlc.py`" in err, err


def test_only_the_scope_section_is_read_when_there_is_one():
    body = "## Outcome\r\n\r\nCLAUDE.md\r\n\r\n## Scope\r\n\r\ntools/sdlc.py\r\n\r\n## Non-goals\r\n\r\nSDLC.md\r\n"
    assert sdlc.scope_text(body).strip() == "tools/sdlc.py"
    assert "CLAUDE.md" not in sdlc.scope_text("## Scope\n\nx\n\n# Context\n\nCLAUDE.md\n")   # a shallower heading ends it


def test_a_crlf_pass_comment_still_reads_and_a_cr_scope_is_two_lines():
    body = sdlc.render_pass({"epic": 250, "pass": 1, "groups": [group(5)]}).replace("\n", "\r\n")
    assert sdlc.pass_ticket_numbers(sdlc.extract_pass(body)) == [5]
    assert any("more than one line" in e for e in
               sdlc.pass_shape_errors({"epic": 250, "pass": 1, "groups": [group(5, scope="a\rb")]}, 250, 1,
                                      sdlc.load_debt(CONFIG)))
