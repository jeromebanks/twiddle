"""tools/sdlc.py: reading a wait (`sdlc:waiting`, a `kind=wait` marker) from recorded JSON. No network.

`sdlc:waiting` is not in `.sdlc/config.json` yet, so these tests inject a config with it added."""
import copy
import json

import pytest

from tools import sdlc

REAL = sdlc.load_config()
CONFIG = copy.deepcopy(REAL)
if not any(l["name"] == "sdlc:waiting" for l in CONFIG["labels"]):
    CONFIG["labels"].append({"name": "sdlc:waiting", "color": "cccccc", "description": "waits for another epic"})
OWNER, POSTER, STRANGER = "owner", "poster", "stranger"
TRUSTED = {OWNER}
N, E = 308, 263

_ids = iter(range(1, 10_000))


def comment(author, body, ts):
    return {"id": next(_ids), "author": author, "body": body, "created_at": f"2026-11-06T00:{ts:02d}:00Z",
            "url": f"https://example/{N}#c{ts}"}


def agent(kind, ts, body="text", rev=None, author=OWNER, **extra):
    return comment(author, sdlc.render_comment(kind, rev, body, CONFIG, **extra), ts)


def wait_comment(ts=1, waits=f"{E}:M2", reason="rewords the rule #263's bracket introduces", author=OWNER, **extra):
    return agent("wait", ts, f"Waiting for this to ship first.\n\nReason: {reason}", author=author,
                 waits=waits, **{"from": "triage", **extra})


def issue(labels=("sdlc:waiting", "enhancement"), state="open", number=N):
    return {"number": number, "title": "reword the speaker-write rule", "state": state, "author": POSTER,
            "labels": list(labels)}


def derive(comments, labels=("sdlc:waiting", "enhancement"), config=CONFIG):
    return sdlc.derive_state(issue(labels), comments, TRUSTED, config)


def plan_json(*milestones):
    plan = {"milestones": [{"key": m, "title": m} for m in milestones]}
    return f"<!-- plan-json -->\n```json\n{json.dumps(plan)}\n```"


def target(milestones=("M1", "M2"), shipped=(), state="open", label="sdlc:in-progress", plan=True):
    """The target epic's bundle: a plan (its plan-json naming `milestones`), its issues created, some shipped."""
    ts = iter(range(10, 60))
    comments = [comment(OWNER, sdlc.marker("prd", 1) + "\nPRD", next(ts)),
                comment(OWNER, sdlc.marker("approval", 1, by=POSTER, via="comment") + "\nok", next(ts))]
    if plan:
        comments.append(comment(OWNER, sdlc.marker("plan", 1) + "\n" + plan_json(*milestones), next(ts)))
    comments.append(comment(OWNER, sdlc.marker("plan-created", 1, created=",".join(milestones)) + "\nmade", next(ts)))
    for m in shipped:
        comments.append(comment(OWNER, sdlc.render_comment("shipped", None, "x", CONFIG, milestone=m, sha="c" * 40),
                                next(ts)))
    return {"issue": {"number": E, "title": "the bracket", "state": state, "author": POSTER, "labels": [label]},
            "comments": comments}


def resolved(comments, tgt=None, labels=("sdlc:waiting", "enhancement")):
    """`with_next` offline, the target epic read from the bundle's `target`."""
    st = derive(comments, labels)
    b = {"issue": issue(labels), "comments": comments, "trusted": sorted(TRUSTED)}
    if tgt is not None:
        b["target"] = tgt
    return sdlc.with_next(st, CONFIG, offline=True, trusted=TRUSTED, bundle=b)


# --- the ledger -----------------------------------------------------------------------------------------------

def test_a_trusted_wait_is_read_into_the_ledger():
    w = derive([wait_comment()])["wait"]
    assert (w["epic"], w["milestone"], w["from"], w["then"]) == (E, "M2", "triage", None)
    assert w["reason"] == "rewords the rule #263's bracket introduces"
    assert w["since"] == "2026-11-06" and w["url"].startswith("https://example/")


def test_then_close_is_recorded():
    assert derive([wait_comment(then="close")])["wait"]["then"] == "close"


def test_a_wait_lifted_after_it_ends_it():
    c = [wait_comment(1), agent("wait-lifted", 2)]
    st = derive(c, labels=("sdlc:triage",))
    assert st["wait"] is None and st["wait_lifted"]["milestone"] == "M2"
    assert (st["state"], st["action"]) == ("triage", "triage")


def test_a_lift_cut_short_before_its_label_is_the_agents_to_finish():
    st = resolved([wait_comment(1), agent("wait-lifted", 2)])
    assert (st["turn"], st["action"]) == ("agent", "lift_wait")
    assert st["next"] == f"`uv run python tools/sdlc.py wait {N} --lift`"


def test_a_new_wait_after_a_lift_is_the_open_one():
    st = derive([wait_comment(1), agent("wait-lifted", 2), wait_comment(3, waits=f"{E}:M3")])
    assert st["wait"]["milestone"] == "M3" and st["wait_lifted"] is None


@pytest.mark.parametrize("labels", [("sdlc:triage", "enhancement"), ("enhancement",)])
def test_an_untrusted_wait_is_ignored(labels):
    spoof = wait_comment(author=STRANGER)
    with_it, without = derive([spoof], labels), derive([], labels)
    assert with_it["wait"] is None
    assert (with_it["state"], with_it["turn"], with_it["action"]) == (without["state"], without["turn"],
                                                                      without["action"])


def test_an_untrusted_wait_on_a_waiting_label_is_a_conflict():
    st = derive([wait_comment(author=STRANGER)])
    assert (st["turn"], st["action"]) == ("human", "fix_conflict")
    assert any("no wait is recorded" in c for c in st["conflicts"])


@pytest.mark.parametrize("waits", ["263", "263:", "x:M2", ""])
def test_a_wait_marker_without_a_readable_target_is_no_wait(waits):
    assert derive([wait_comment(waits=waits)])["wait"] is None


# --- derive_state + with_next: whose turn ------------------------------------------------------------------------

def test_waiting_on_an_unshipped_target_is_nobodys_move():
    st = resolved([wait_comment()], target())
    assert (st["state"], st["turn"], st["action"]) == ("waiting", "none", "none")
    assert st["wait"]["status"] == "waiting"
    assert st["next"].startswith(f"nothing yet: #{N} waits for #{E}'s M2 to ship to main (https://example/")
    assert "uv run" not in st["next"] and "/" not in st["next"].split("(")[0]


def test_offline_without_the_target_it_is_still_waiting():
    st = resolved([wait_comment()])
    assert (st["turn"], st["action"]) == ("none", "none") and st["wait"]["status"] == "waiting"


def test_a_shipped_target_is_the_agents_to_lift():
    st = resolved([wait_comment()], target(shipped=("M1", "M2")))
    assert (st["turn"], st["action"]) == ("agent", "lift_wait")
    assert st["next"] == f"`uv run python tools/sdlc.py wait {N} --lift`"


def test_another_milestone_shipping_is_not_the_target():
    st = resolved([wait_comment()], target(shipped=("M1",)))
    assert (st["turn"], st["action"]) == ("none", "none")


@pytest.mark.parametrize("tgt", [
    lambda: target(state="closed", label="sdlc:in-progress"),           # closed, M2 never shipped
    lambda: target(state="closed", shipped=("M1",)),                    # closed after M1 only
    lambda: target(milestones=("M1", "M3")),                            # the latest plan dropped M2
], ids=["closed", "closed-after-M1", "dropped"])
def test_a_target_that_will_never_ship_is_a_humans(tgt):
    st = resolved([wait_comment()], tgt())
    assert (st["turn"], st["action"]) == ("human", "wait_broken")
    assert "needs a human" in st["next"] and f"wait {N} --lift" in st["next"]


def test_a_later_plan_with_no_milestones_breaks_the_wait():
    tgt = target()
    tgt["comments"].append(comment(OWNER, sdlc.marker("plan", 2) + "\n" + plan_json(), 59))
    st = resolved([wait_comment()], tgt)
    assert (st["turn"], st["action"]) == ("human", "wait_broken")
    assert f"latest plan no longer has M2" in st["wait_broken_why"]


def test_a_later_plan_whose_json_cant_be_read_doesnt_break_the_wait():
    tgt = target()
    tgt["comments"].append(comment(OWNER, sdlc.marker("plan", 2) + "\nno plan-json here", 59))
    st = resolved([wait_comment()], tgt)
    assert (st["turn"], st["action"]) == ("none", "none")


def test_a_plan_that_cant_be_read_doesnt_break_the_wait():
    st = resolved([wait_comment()], target(milestones=("M1", "M2"), plan=False))
    assert (st["turn"], st["action"]) == ("none", "none")


@pytest.mark.parametrize("tgt, want", [
    (lambda: target(), "none"),                                                       # open, being built
    (lambda: target(label="sdlc:done", state="closed", shipped=("M1", "M2")), "lift_wait"),
    (lambda: target(label="sdlc:done"), "lift_wait"),                                 # done, however it reads
    (lambda: target(state="closed", shipped=("M1",)), "lift_wait"),                   # closed after a ship
    (lambda: target(state="closed"), "wait_broken"),                                  # closed, nothing shipped
], ids=["building", "done-closed", "done-open", "closed-shipped", "closed-unshipped"])
def test_waiting_on_a_whole_epic(tgt, want):
    st = resolved([wait_comment(waits=f"{E}:all")], tgt())
    assert st["action"] == want
    assert st["wait"]["milestone"] == "all"


def test_a_whole_epic_is_worded_as_the_epic():
    st = resolved([wait_comment(waits=f"{E}:all")], target())
    assert f"waits for #{E} to ship to main" in st["next"]
    assert sdlc.wait_text(st["wait"]) == f"waiting for #{E} to ship to main"


def test_a_reply_by_the_poster_is_the_agents_and_the_wait_stays():
    c = [wait_comment(1), comment(POSTER, "actually, do this now", 2)]
    st = resolved(c, target(shipped=("M2",)))   # a reply comes before a shipped target's lift
    assert (st["state"], st["turn"], st["action"]) == ("waiting", "agent", "respond_to_reply")
    assert st["next"] == f"/triage-issue {N}"
    c.append(agent("note", 3, "Answered: it stays parked."))
    assert (derive(c)["turn"], derive(c)["action"]) == ("none", "none")
    assert resolved(c, target())["action"] == "none"


def test_a_reply_by_a_collaborator_counts():
    st = derive([wait_comment(1), comment(OWNER, "unmarked, by a collaborator", 2)])
    assert (st["turn"], st["action"]) == ("agent", "respond_to_reply")


def test_a_reply_by_anyone_else_does_not_move_the_turn():
    st = resolved([wait_comment(1), comment(STRANGER, "me too!", 2)], target())
    assert (st["turn"], st["action"]) == ("none", "none")


def test_a_reply_before_the_wait_is_not_unanswered():
    st = derive([comment(POSTER, "please fix", 1), wait_comment(2)])
    assert (st["turn"], st["action"]) == ("none", "none")


def test_a_closed_waiting_issue_is_nothing():
    st = sdlc.derive_state(issue(state="closed"), [wait_comment()], TRUSTED, CONFIG)
    st = sdlc.with_next(st, CONFIG, offline=True, trusted=TRUSTED,
                        bundle={"issue": issue(state="closed"), "comments": [], "trusted": [OWNER], "target": target()})
    assert (st["turn"], st["action"]) == ("none", "none") and "closed" in st["next"]


def test_the_wait_actions_are_known():
    assert {"lift_wait", "wait_broken"} <= sdlc.ALL_ACTIONS
    assert sdlc.TRANSITIONS["waiting"] == {"waiting"}


# --- the CLI: state --from-file, transition --dry-run ---------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("offline: --from-file reads the bundle only"))
    f = tmp_path / "config.json"
    f.write_text(json.dumps(CONFIG))
    return f


def save(tmp_path, comments, tgt=None):
    b = {"issue": issue(), "comments": comments, "trusted": sorted(TRUSTED), **({"target": tgt} if tgt else {})}
    f = tmp_path / "b.json"
    f.write_text(json.dumps(b))
    return f


def test_state_prints_the_wait(cfg, tmp_path, capsys):
    f = save(tmp_path, [wait_comment()], target())
    assert sdlc.main(["--config", str(cfg), "state", str(N), "--from-file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "state: waiting   turn: none   action: none" in out
    assert (f"waiting for #{E}'s M2 to ship to main (since 2026-11-06, from triage): "
            "rewords the rule #263's bracket introduces") in out
    assert f"next: nothing yet: #{N} waits for #{E}'s M2 to ship to main (https://example/" in out


def test_state_json_carries_the_wait(cfg, tmp_path, capsys):
    f = save(tmp_path, [wait_comment()], target(shipped=("M2",)))
    assert sdlc.main(["--config", str(cfg), "state", str(N), "--json", "--from-file", str(f)]) == 0
    st = json.loads(capsys.readouterr().out)
    assert (st["state"], st["turn"], st["action"]) == ("waiting", "agent", "lift_wait")
    assert st["wait"]["status"] == "shipped" and st["wait"]["url"]
    assert st["next"] == f"`uv run python tools/sdlc.py wait {N} --lift`"


def test_state_prints_a_broken_wait(cfg, tmp_path, capsys):
    f = save(tmp_path, [wait_comment()], target(state="closed"))
    assert sdlc.main(["--config", str(cfg), "state", str(N), "--from-file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "turn: human   action: wait_broken" in out and f"wait broken: #{E} closed without M2 shipping" in out


def test_the_real_config_reads_a_waiting_label_as_untriaged_until_it_has_it():
    st = sdlc.derive_state(issue(), [wait_comment()], TRUSTED, REAL)
    if any(l["name"] == "sdlc:waiting" for l in REAL["labels"]):
        assert st["state"] == "waiting"
    else:
        assert st["state"] == "untriaged"


def test_a_note_answers_a_reply_and_keeps_the_wait(cfg, tmp_path, capsys):
    f = save(tmp_path, [wait_comment(1), comment(POSTER, "why?", 2)])
    body = tmp_path / "note.md"
    body.write_text("It waits for #263's M2: the rule it rewords comes from there.")
    assert sdlc.main(["--config", str(cfg), "transition", str(N), "waiting", "--kind", "note", "--body-file",
                      str(body), "--from-file", str(f), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "waiting -> waiting" in out and "kind=note" in out


@pytest.mark.parametrize("to, kind", [("needs-info", "question"), ("triage", "note"), ("escalated", "escalation"),
                                      ("prd-review", "prd"), ("waiting", "question")])
def test_no_other_transition_moves_a_waiting_issue(cfg, tmp_path, capsys, to, kind):
    f = save(tmp_path, [wait_comment(1), comment(POSTER, "why?", 2)])
    body = tmp_path / "b.md"
    body.write_text("text")
    assert sdlc.main(["--config", str(cfg), "transition", str(N), to, "--kind", kind, "--body-file", str(body),
                      "--from-file", str(f), "--dry-run"]) == 1
    assert "only `uv run python tools/sdlc.py wait" in capsys.readouterr().err


@pytest.mark.parametrize("labels", [("sdlc:triage",), (), ("sdlc:approved",)])
def test_transition_never_moves_an_issue_into_waiting(cfg, tmp_path, capsys, labels):
    b = {"issue": issue(labels), "comments": [], "trusted": sorted(TRUSTED)}
    f = tmp_path / "b.json"
    f.write_text(json.dumps(b))
    body = tmp_path / "b.md"
    body.write_text("text")
    assert sdlc.main(["--config", str(cfg), "transition", str(N), "waiting", "--kind", "note", "--body-file",
                      str(body), "--from-file", str(f), "--dry-run"]) == 1
    assert "only `uv run python tools/sdlc.py wait" in capsys.readouterr().err


# --- claim: nothing builds a waiting issue, in any lane --------------------------------------------------------

SLICE = 400


def waiting_epic_bundle(number=N, labels=("sdlc:waiting", "enhancement")):
    return {"issue": {**issue(labels, number=number), "body": ""}, "comments": [wait_comment()],
            "trusted": sorted(TRUSTED)}


def slice_of(epic=N, label="plan:slice", number=SLICE):
    kind = "slice" if label == "plan:slice" else "subtask"
    return {"issue": {"number": number, "title": "a slice", "state": "open", "author": OWNER,
                      "labels": [label, "complexity:judgment"],
                      "body": f"<!-- sdlc:v1 kind={kind} epic={epic} key=T1.1 -->\nthe brief"},
            "comments": [], "trusted": sorted(TRUSTED), "blockers": [], "pr": None, "pr_comments": []}


class FakeClaim:
    """`gh` for a claim that must be refused: the bundle it reads, the epic's, and nothing posted or made."""

    def __init__(self, monkeypatch, bundle, epics=()):
        self.bundle, self.epics, self.read, self.posted, self.edits = bundle, {e["issue"]["number"]: e for e in epics}, [], [], []
        monkeypatch.setattr(sdlc, "slice_bundle", lambda n, config: self.bundle)
        monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, config, trusted=None: self.read.append(n) or self.epics[n])
        monkeypatch.setattr(sdlc, "post_comment", lambda n, body, config: self.posted.append(body) or {})
        monkeypatch.setattr(sdlc, "gh", lambda args, **k: self.edits.append(args) or "")
        monkeypatch.setattr(sdlc, "gh_json", lambda args: pytest.fail(f"a refused claim reads nothing else: {args}"))
        monkeypatch.setattr(sdlc, "add_slice_worktree", lambda *a, **k: pytest.fail("a refused claim makes no worktree"))


LANES = [[], ["--resume"], ["--retry", "--model", "opus"], ["--dry-run"], ["--quick"], ["--quick", "--dry-run"],
         ["--quick", "--resume"], ["--debt"], ["--debt", "--resume"]]


@pytest.mark.parametrize("flags", LANES)
@pytest.mark.parametrize("label", ["plan:slice", "plan:subtask"])
def test_claim_refuses_a_slice_of_a_waiting_epic_in_every_lane(cfg, monkeypatch, capsys, flags, label):
    fake = FakeClaim(monkeypatch, slice_of(label=label), [waiting_epic_bundle()])
    assert sdlc.main(["--config", str(cfg), "claim", str(SLICE), *flags]) == 1
    err = capsys.readouterr().err
    assert f"#{N} is waiting for #{E}'s M2 to ship to main (https://example/" in err
    assert f"nothing builds #{N}'s slices, #{SLICE} among them" in err and f"wait {N} --lift" in err
    assert fake.posted == [] and fake.edits == [] and fake.read == [N]


@pytest.mark.parametrize("flags", LANES)
def test_claim_refuses_the_waiting_issue_itself_in_every_lane(cfg, monkeypatch, capsys, flags):
    """A quick change parked before its claim: approved brief, then the wait (its label replaced sdlc:approved)."""
    b = {**waiting_epic_bundle(labels=("sdlc:waiting", "complexity:routine", "enhancement")),
         "blockers": [], "pr": None, "pr_comments": []}
    fake = FakeClaim(monkeypatch, b)
    assert sdlc.main(["--config", str(cfg), "claim", str(N), *flags]) == 1
    err = capsys.readouterr().err
    assert f"#{N} is waiting for #{E}'s M2 to ship to main" in err and f"nothing builds #{N} while it waits" in err
    assert fake.posted == [] and fake.edits == [] and fake.read == []   # its own label decides: no epic read


def test_claim_quick_from_a_saved_bundle_refuses_a_waiting_issue(cfg, monkeypatch, tmp_path, capsys):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({**waiting_epic_bundle(labels=("sdlc:waiting", "complexity:routine")),
                             "blockers": [], "pr": None, "pr_comments": []}))
    assert sdlc.main(["--config", str(cfg), "claim", str(N), "--quick", "--dry-run", "--from-file", str(f)]) == 1
    assert f"#{N} is waiting for #{E}'s M2" in capsys.readouterr().err


def test_a_waiting_label_with_no_wait_recorded_is_still_refused():
    b = {"issue": {**issue(), "body": ""}, "comments": [], "trusted": sorted(TRUSTED)}
    (err,), _ = sdlc.wait_claim_errors(b, CONFIG)
    assert f"#{N} is labelled sdlc:waiting (no wait recorded" in err


def test_a_slice_of_an_epic_that_doesnt_wait_is_not_refused_and_its_epic_is_read_once(monkeypatch):
    fake = FakeClaim(monkeypatch, slice_of(), [waiting_epic_bundle(labels=("sdlc:planned", "enhancement"))])
    errs, est = sdlc.wait_claim_errors(fake.bundle, CONFIG)
    assert errs == [] and est["state"] == "planned" and fake.read == [N]
    # the state is handed on, so a fresh claim's epic checks don't read it again
    monkeypatch.setattr(sdlc, "epic_progress", lambda *a, **k: None)
    sdlc.epic_pause_errors(fake.bundle, CONFIG, False, est)
    assert fake.read == [N]


def test_a_debt_tickets_source_epic_waiting_holds_nothing_back(monkeypatch):
    """A debt ticket's marker names the epic it came from, not one that holds it (its waits are its own)."""
    ticket = {"issue": {"number": 77, "title": "t", "state": "open", "author": OWNER, "labels": ["tech-debt"],
                        "body": f"<!-- sdlc:v1 kind=debt epic={N} source=review -->"},
              "comments": [], "trusted": sorted(TRUSTED)}
    fake = FakeClaim(monkeypatch, ticket, [waiting_epic_bundle()])
    assert sdlc.wait_claim_errors(ticket, CONFIG) == ([], None) and fake.read == []


# --- next: waiting issues on one `waiting:` line ---------------------------------------------------------------

def retarget(b, number):
    return {**b, "issue": {**b["issue"], "number": number}, "trusted": sorted(TRUSTED)}


def waiter(number, waits, *more):
    return {"issue": {**issue(number=number), "body": ""}, "comments": [wait_comment(1, waits=waits), *more],
            "trusted": sorted(TRUSTED)}


class FakeNext:
    def __init__(self, monkeypatch, bundles):
        self.bundles, self.read = {b["issue"]["number"]: b for b in bundles}, []
        self.listed = [self.bundles[n]["issue"] for n in self.bundles if n in LISTED]
        monkeypatch.setattr(sdlc, "repo_of", lambda config: "o/r")
        monkeypatch.setattr(sdlc, "fetch_trusted", lambda config: set(TRUSTED))
        monkeypatch.setattr(sdlc, "gh_json", lambda args: [{**i, "labels": [{"name": l} for l in i["labels"]]}
                                                           for i in self.listed])
        monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail(f"next reads nothing else: {a}"))
        monkeypatch.setattr(sdlc, "fetch_bundle",
                            lambda n, config, trusted=None: self.read.append(n) or self.bundles[n])


# open and listed: the waiters, and two of the epics they wait on (one numbered after its waiter)
LISTED = {308, 350, 400, 410, 420, 500, E}


def next_world(monkeypatch):
    poster_reply = comment(POSTER, "actually, do this now", 5)
    return FakeNext(monkeypatch, [
        retarget(target(label="sdlc:prd-review"), E),            # listed, read for itself and for both its waiters
        waiter(308, f"{E}:M2"), waiter(350, f"{E}:all"),          # on #263, not shipped
        waiter(400, "500:M1"), retarget(target(shipped=("M1",), label="sdlc:prd-review"), 500),   # shipped
        waiter(410, "270:M2"), retarget(target(state="closed", label="sdlc:prd-review"), 270),    # broken
        waiter(420, f"{E}:M2", poster_reply),                       # the poster replied
    ])


def test_next_prints_unshipped_waits_on_one_line_and_reads_each_epic_once(cfg, monkeypatch, capsys):
    fake = next_world(monkeypatch)
    assert sdlc.main(["--config", str(cfg), "next"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == f"waiting: #308 on #{E}'s M2; #350 on #{E}"
    work = out.splitlines()[:-1]
    assert not any(l.startswith(("#308", "#350")) for l in work)    # not agent work
    assert f"#400  waiting" in out and "next: `uv run python tools/sdlc.py wait 400 --lift`" in out
    assert "#420  waiting" in out                                    # respond_to_reply: the agent's, with its next
    assert "#410" not in out                                        # wait_broken: a human's, like any other
    assert sorted(fake.read) == sorted([E, 270, 308, 350, 400, 410, 420, 500])   # every epic read once


def test_next_json_lists_the_agents_waits_and_one_entry_for_the_rest(cfg, monkeypatch, capsys):
    next_world(monkeypatch)
    assert sdlc.main(["--config", str(cfg), "next", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    agents = [(s["number"], s["action"]) for s in listed if s["turn"] == "agent"]
    assert agents == [(400, "lift_wait"), (420, "respond_to_reply")]
    (w,) = [s for s in listed if s["turn"] != "agent"]
    assert (w["number"], w["state"], w["turn"]) == (None, "waiting", "none")
    assert [(x["number"], x["epic"], x["milestone"], x["on"]) for x in w["waiting"]] == [
        (308, E, "M2", f"#{E}'s M2"), (350, E, "all", f"#{E}")]


def test_next_with_nothing_waiting_prints_no_waiting_line(cfg, monkeypatch, capsys):
    FakeNext(monkeypatch, [retarget(target(label="sdlc:prd-review"), E)])
    assert sdlc.main(["--config", str(cfg), "next"]) == 0
    assert "waiting:" not in capsys.readouterr().out
    assert sdlc.main(["--config", str(cfg), "next", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
