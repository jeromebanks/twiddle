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

    def issue(self, title, labels=(), author=OWNER, body="", state="open"):
        self.next_n += 1
        n = self.next_n
        self.issues[n] = {"number": n, "id": 9000 + n, "title": title, "labels": list(labels), "author": author,
                          "body": body, "state": state}
        self.comments[n] = []
        return n

    def ticket(self, title="debt", **kw):
        return self.issue(title, [DEBT], body="<!-- sdlc:v1 kind=debt epic=12 source=review -->\nTech debt.", **kw)

    def debt_epic(self, author=OWNER, state="open"):
        return self.issue("Tech debt", [EPIC_LABEL, "sdlc:in-progress"], author=author, state=state,
                          body=sdlc.marker(sdlc.DEBT_EPIC_KIND) + "\nThe debt epic.")

    def intake(self, n, verdict="debt", author=OWNER):
        self.comments[n].append({"id": len(self.comments[n]) + 1, "user": {"login": author},
                                 "body": sdlc.marker(sdlc.INTAKE_KIND, None, verdict=verdict) + "\nIt is debt.",
                                 "created_at": "2026-10-21T00:00:00Z", "html_url": f"u{n}"})

    def rest(self, i):
        return {"number": i["number"], "id": i["id"], "title": i["title"], "state": i["state"], "body": i["body"],
                "user": {"login": i["author"]}, "labels": [{"name": l} for l in i["labels"]]}

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
        if tail[0] == "issues" and tail[2:] == ["comments"]:
            return page(self.comments[int(tail[1])])
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
    assert (st["turn"], st["action"]) == ("none", "none")
    assert st["next"] == "nothing: debt epic #250's 2 open ticket(s) are all through intake"


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
    assert st["debt_epic"] is None and st["next"].startswith(f"nothing: #{loose} is tech debt, planned by")


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
    assert next_listed(capsys) == []   # nothing due on the debt epic: it isn't listed
    b = gh.ticket()   # new debt, not attached yet
    listed = next_listed(capsys)
    assert [s["number"] for s in listed] == [None] and listed[0]["unattached"] == [b]


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
    listed = next_listed(capsys)
    assert [(s["number"], s["debt_epics"]) for s in listed] == [(None, [a, b])]
    assert "a person closes all but one" in listed[0]["next"]
