"""tools/sdlc.py `wait N --on E[:Mk] --reason R`: placing a wait, and every refusal. A faked `gh`; no network.

`sdlc:waiting` is not in `.sdlc/config.json` yet, so these tests inject a config with it added (as
tests/test_sdlc_wait.py does)."""
import copy
import json

import pytest

from tools import sdlc
from tests.test_sdlc_wait import CONFIG as WAIT_CONFIG, OWNER, POSTER, STRANGER, TRUSTED, agent, comment, target, \
    wait_comment

CONFIG = {**copy.deepcopy(WAIT_CONFIG), "repository": "o/r"}
N, E = 308, 263
LABELS = [l["name"] for l in CONFIG["labels"]]


def issue(number=N, labels=("sdlc:triage", "enhancement"), state="open", title="reword the speaker-write rule"):
    return {"number": number, "title": title, "state": state, "author": POSTER, "labels": list(labels),
            "createdAt": "", "body": ""}


def raw_issue(i):
    return {**i, "author": {"login": i["author"]}, "labels": [{"name": l} for l in i["labels"]]}


def raw_comment(c):
    return {"id": c["id"], "user": {"login": c["author"]}, "body": c["body"], "created_at": c["created_at"],
            "html_url": c["url"]}


class FakeGitHub:
    """Answers the reads `wait` makes (an issue, its comments, the collaborators, the labels) and records every
    write, reading a posted body from its temp file while the call runs (post_comment deletes it after)."""

    def __init__(self, issues, comments, labels=LABELS):
        self.issues, self.comments, self.labels = issues, comments, labels
        self.writes, self.posted = [], []

    def __call__(self, args, check=True):
        args = list(args)
        if args[:2] == ["issue", "view"]:
            return json.dumps(raw_issue(self.issues[int(args[2])]))
        if args[:2] == ["label", "list"]:
            return json.dumps([{"name": l} for l in self.labels])
        if args[0] == "api" and "-X" not in args:
            path = args[1]
            if path.endswith("/collaborators"):
                return json.dumps([[{"login": OWNER, "permissions": {"push": True}}]])
            if path.endswith("/comments"):
                n = int(path.split("/")[-2])
                return json.dumps([[raw_comment(c) for c in self.comments.get(n, [])]])
        if args[0] == "api" and "-X" in args or args[:2] in (["issue", "edit"], ["issue", "close"]) \
                or args[:2] == ["label", "create"]:
            self.writes.append(args)
            if "-F" in args and (f := args[args.index("-F") + 1]).startswith("body=@"):
                with open(f[len("body=@"):]) as fh:
                    self.posted.append(fh.read())
            return json.dumps({"html_url": "https://example/308#posted"})
        pytest.fail(f"unexpected gh {args}")


@pytest.fixture
def cfg(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps(CONFIG))
    return f


def fake(monkeypatch, n_issue=None, n_comments=(), tgt=None, labels=LABELS, extra=None):
    tgt = tgt if tgt is not None else target()
    issues = {N: n_issue or issue(), E: tgt["issue"], **{k: v[0] for k, v in (extra or {}).items()}}
    comments = {N: list(n_comments), E: tgt["comments"], **{k: v[1] for k, v in (extra or {}).items()}}
    gh = FakeGitHub(issues, comments, labels)
    monkeypatch.setattr(sdlc, "gh", gh)
    return gh


def run(capsys, cfg, *argv):
    code = sdlc.main(["--config", str(cfg), "wait", *map(str, argv)])
    out = capsys.readouterr()
    return code, out.out, out.err


REASON = "rewords the rule #263's bracket introduces"


# --- placing a wait ---------------------------------------------------------------------------------------------

def test_a_triage_issue_waits_on_a_milestone(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch)
    code, out, err = run(capsys, cfg, N, "--on", f"{E}:M2", "--reason", REASON)
    assert code == 0, err
    posts = [w for w in gh.writes if w[:3] == ["api", "-X", "POST"]]
    edits = [w for w in gh.writes if w[:2] == ["issue", "edit"]]
    assert len(posts) == 1 and posts[0][3] == f"repos/o/r/issues/{N}/comments" and len(gh.posted) == 1
    assert len(edits) == 1 and len(gh.writes) == 2
    edit = edits[0]
    assert edit[edit.index("--add-label") + 1] == "sdlc:waiting"
    assert [edit[i + 1] for i, a in enumerate(edit) if a == "--remove-label"] == ["sdlc:triage"]
    body = gh.posted[0]
    mk = sdlc.parse_marker(body)
    assert (mk["kind"], mk["waits"], mk["from"]) == ("wait", f"{E}:M2", "triage") and "then" not in mk
    # read back: the ledger has the wait, and with the label moved it is nobody's move
    c = comment(OWNER, body, 5)
    st = sdlc.derive_state(issue(labels=("sdlc:waiting", "enhancement")), [c], TRUSTED, CONFIG)
    w = st["wait"]
    assert (w["epic"], w["milestone"], w["from"], w["reason"]) == (E, "M2", "triage", REASON)
    assert (st["state"], st["turn"], st["action"]) == ("waiting", "none", "none")
    assert f"#{N}: triage -> waiting, waiting for #{E}'s M2 to ship to main" in out


def test_the_dry_run_prints_the_comment_and_the_label_move_and_writes_nothing(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch)
    code, out, err = run(capsys, cfg, N, "--on", f"{E}:M2", "--reason", REASON, "--dry-run")
    assert code == 0, err
    assert gh.writes == []
    assert f"DRY RUN #{N}: triage -> waiting, waiting for #{E}'s M2 to ship to main" in out
    assert "labels: add ['sdlc:waiting'], remove ['sdlc:triage']" in out
    assert f"kind=wait waits={E}:M2 from=triage" in out and f"Reason: {REASON}" in out


def test_on_an_epic_alone_is_recorded_as_all_and_worded_as_the_epic(monkeypatch, capsys, cfg, tmp_path):
    gh = fake(monkeypatch)
    assert run(capsys, cfg, N, "--on", E, "--reason", REASON)[0] == 0
    body = gh.posted[0]
    assert sdlc.parse_marker(body)["waits"] == f"{E}:all"
    bundle = {"issue": issue(labels=("sdlc:waiting",)), "comments": [comment(OWNER, body, 5)],
              "trusted": sorted(TRUSTED), "target": target()}
    f = tmp_path / "b.json"
    f.write_text(json.dumps(bundle))
    assert sdlc.main(["--config", str(cfg), "state", str(N), "--from-file", str(f)]) == 0
    out = capsys.readouterr().out
    assert f"waiting for #{E} to ship to main (since " in out and f"waits for #{E} to ship to main" in out
    assert f"#{E}'s M" not in out


@pytest.mark.parametrize("labels, frm", [((), "untriaged"), (("sdlc:approved",), "approved"),
                                         (("sdlc:planned",), "planned")])
def test_the_other_states_a_wait_takes(monkeypatch, capsys, cfg, labels, frm):
    approved = [comment(OWNER, sdlc.marker("prd", 1) + "\nPRD", 1),
                comment(OWNER, sdlc.marker("approval", 1, by=POSTER, via="comment") + "\nok", 2)]
    gh = fake(monkeypatch, issue(labels=labels), n_comments=approved if frm == "approved" else ())
    assert run(capsys, cfg, N, "--on", f"{E}:M2", "--reason", REASON)[0] == 0
    assert sdlc.parse_marker(gh.posted[0])["from"] == frm
    if frm == "untriaged":
        assert "goes back to `triage`" in gh.posted[0]


def test_a_wait_cut_short_before_its_label_is_finished_without_a_second_comment(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, n_comments=[wait_comment(1)])
    code, out, err = run(capsys, cfg, N, "--on", f"{E}:M2", "--reason", REASON)
    assert code == 0, err
    assert gh.posted == [] and [w[:2] for w in gh.writes] == [["issue", "edit"]]
    assert "moving it to sdlc:waiting" in out


def dropped_m2():
    tgt = target()
    tgt["comments"].append(comment(OWNER, sdlc.marker("plan", 2) + "\n<!-- plan-json -->\n```json\n"
                                   + json.dumps({"milestones": [{"key": "M1"}]}) + "\n```", 59))
    return tgt


@pytest.mark.parametrize("tgt, n_issue, why", [
    (lambda: target(state="closed"), None, f"#{E} is closed"),
    (lambda: target(shipped=("M2",)), None, f"#{E}'s M2 has already shipped to main"),
    (dropped_m2, None, f"M2 is not a milestone of #{E}'s latest plan"),
    (target, lambda: issue(labels=("lane:debt", "sdlc:triage")), f"#{N} is the debt epic"),
], ids=["closed", "shipped", "dropped", "debt-epic"])
def test_finishing_a_wait_cut_short_still_runs_every_other_check(monkeypatch, capsys, cfg, tgt, n_issue, why):
    gh = fake(monkeypatch, n_issue() if n_issue else None, n_comments=[wait_comment(1)], tgt=tgt())
    code, out, err = run(capsys, cfg, N, "--on", f"{E}:M2", "--reason", REASON)
    assert code == 1, out
    assert why in err and gh.writes == []


def test_a_wait_cut_short_on_another_target_is_refused(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, n_comments=[wait_comment(1)])
    code, _, err = run(capsys, cfg, N, "--on", f"{E}:M1", "--reason", REASON)
    assert code == 1 and f"already waits for #{E}'s M2" in err and gh.writes == []


# --- refusals: each names its reason and writes nothing --------------------------------------------------------

def refused(monkeypatch, capsys, cfg, *argv, **fake_kw):
    gh = fake(monkeypatch, **fake_kw)
    code, out, err = run(capsys, cfg, *argv)
    assert code == 1, out
    assert gh.writes == [] and gh.posted == []
    return err


def on(ms="M2", reason=REASON):
    return (N, "--on", f"{E}:{ms}" if ms else E, "--reason", reason)


@pytest.mark.parametrize("labels, why", [
    (("tech-debt",), "is a tech-debt ticket"),
    (("plan:slice",), "is a slice"),
    (("plan:subtask",), "is a subtask"),
    (("lane:debt", "sdlc:in-progress"), "is the debt epic"),
    (("sdlc:escalated",), "is escalated"),
    (("sdlc:needs-info",), "is needs-info (the poster's move)"),
    (("sdlc:prd-review",), "is prd-review (the poster's move)"),
    (("sdlc:diagnosis-review",), "is diagnosis-review (the poster's move)"),
    (("sdlc:demo-review",), "is demo-review (the poster's move)"),
    (("sdlc:in-progress",), "is in-progress (its slices and claims stay where they are)"),
    (("sdlc:done",), "is done"),
], ids=lambda v: v if isinstance(v, str) else ",".join(v))
def test_an_issue_a_wait_cant_take(monkeypatch, capsys, cfg, labels, why):
    err = refused(monkeypatch, capsys, cfg, *on(), n_issue=issue(labels=labels))
    assert f"#{N} {why}" in err


@pytest.mark.parametrize("dry", [False, True])
def test_dry_run_refuses_the_same(monkeypatch, capsys, cfg, dry):
    err = refused(monkeypatch, capsys, cfg, *on(), *(["--dry-run"] if dry else []),
                  n_issue=issue(labels=("sdlc:in-progress",)))
    assert "is in-progress" in err


def test_a_closed_issue(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, *on(), n_issue=issue(state="closed"))
    assert f"#{N} is closed" in err


def test_an_issue_already_waiting_says_on_what(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, *on("M1"), n_issue=issue(labels=("sdlc:waiting",)),
                  n_comments=[wait_comment(1)])
    assert f"already waits for #{E}'s M2 to ship to main (https://example/" in err and "one wait at a time" in err


def test_a_waiting_label_whose_lift_was_cut_short(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, *on(), n_issue=issue(labels=("sdlc:waiting",)),
                  n_comments=[wait_comment(1), agent("wait-lifted", 2)])
    assert f"wait {N} --lift` finishes that first" in err


def test_a_waiting_label_with_no_trusted_wait_is_a_conflict(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, *on(), n_issue=issue(labels=("sdlc:waiting",)),
                  n_comments=[wait_comment(1, author=STRANGER)])
    assert "fix conflicts first" in err and "no wait is recorded" in err and "--lift" not in err


def test_an_issue_with_conflicting_labels(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, *on(), n_issue=issue(labels=("sdlc:triage", "sdlc:approved")))
    assert "fix conflicts first" in err


def test_waiting_on_itself(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, N, "--on", f"{N}:M1", "--reason", REASON)
    assert f"#{N} can't wait on itself" in err


def quick_epic():
    t = target()
    t["comments"] = [comment(OWNER, sdlc.marker("prd", 1, lane="quick") + "\nbrief", 10),
                     comment(OWNER, sdlc.marker("approval", 1, by=POSTER, via="comment") + "\nok", 11)]
    t["issue"]["labels"] = ["sdlc:approved", "lane:quick"]
    return t


def debt_epic():
    t = target()
    t["issue"]["labels"] = ["lane:debt", "sdlc:in-progress"]
    return t


def labelled(*labels):
    t = target()
    t["issue"]["labels"] = list(labels)
    return t


@pytest.mark.parametrize("tgt, why", [
    (lambda: target(state="closed"), f"#{E} is closed"),
    (lambda: target(label="sdlc:done"), f"#{E} is done"),
    (lambda: target(plan=False), f"#{E} has no plan yet"),
    (lambda: {**target(), "comments": target()["comments"][:2]}, f"#{E} has no plan yet"),
    (quick_epic, f"#{E} is a quick change"),
    (debt_epic, f"#{E} is the debt epic"),
    (lambda: labelled("plan:slice"), f"#{E} is a slice, not a feature epic"),
    (lambda: labelled("tech-debt"), f"#{E} is a tech-debt ticket, not a feature epic"),
], ids=["closed", "done", "no-plan-json", "no-plan", "quick", "debt-epic", "slice", "debt-ticket"])
def test_a_target_that_isnt_an_open_feature_epic_with_a_plan(monkeypatch, capsys, cfg, tgt, why):
    assert why in refused(monkeypatch, capsys, cfg, *on(), tgt=tgt())


def test_a_milestone_not_in_the_latest_plan(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, *on("M3"))
    assert f"M3 is not a milestone of #{E}'s latest plan (its milestones: M1, M2)" in err


def test_a_milestone_the_latest_plan_dropped(monkeypatch, capsys, cfg):
    tgt = target()
    tgt["comments"].append(comment(OWNER, sdlc.marker("plan", 2) + "\n"
                                   + "<!-- plan-json -->\n```json\n" + json.dumps({"milestones": [{"key": "M1"}]})
                                   + "\n```", 59))
    assert f"M2 is not a milestone of #{E}'s latest plan (its milestones: M1)" in refused(
        monkeypatch, capsys, cfg, *on(), tgt=tgt)


def test_a_milestone_planned_but_not_created_can_be_waited_on(monkeypatch, capsys, cfg):
    tgt = target()
    tgt["comments"] = [c for c in tgt["comments"] if "plan-created" not in c["body"]]
    gh = fake(monkeypatch, tgt=tgt)
    assert run(capsys, cfg, *on())[0] == 0 and len(gh.posted) == 1


def test_a_milestone_already_shipped(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, *on(), tgt=target(shipped=("M1", "M2")))
    assert f"#{E}'s M2 has already shipped to main" in err


def test_a_whole_epic_already_shipped(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, *on(None), tgt=target(shipped=("M1", "M2")))
    assert f"#{E} has already shipped to main" in err


def test_then_close_is_refused_naming_378(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, *on(), "--then", "close")
    assert "#378" in err and "close verb, not built yet" in err


@pytest.mark.parametrize("reason, why", [
    ("seen on 192.168.1.20", "an IP address"),
    ("token sk-abcdefghijklmnopqrstuv", "an API key"),
    ("secret " + "ab" * 16, "a secret-looking hex string"),
    ("", "--reason is required"),
    ("   ", "--reason is required"),
    ("two\nlines", "--reason is one line"),
])
def test_a_reason_that_cant_be_posted(monkeypatch, capsys, cfg, reason, why):
    assert why in refused(monkeypatch, capsys, cfg, *on(reason=reason))


def test_no_reason_at_all(monkeypatch, capsys, cfg):
    assert "--reason is required" in refused(monkeypatch, capsys, cfg, N, "--on", f"{E}:M2")


@pytest.mark.parametrize("bad", ["263:", "x:M2", "0:M1", "263:M 2", "263:M--2", ":M2"])
def test_an_unreadable_target(monkeypatch, capsys, cfg, bad):
    assert "--on takes an epic" in refused(monkeypatch, capsys, cfg, N, "--on", bad, "--reason", REASON)


def test_a_missing_waiting_label_on_the_repo(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, *on(), labels=[l for l in LABELS if l != "sdlc:waiting"])
    assert "label sdlc:waiting does not exist: run `bootstrap-labels` first" in err


@pytest.mark.parametrize("dry", [False, True])
def test_a_config_without_the_waiting_state(monkeypatch, capsys, tmp_path, dry):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({**CONFIG, "labels": [l for l in CONFIG["labels"] if l["name"] != "sdlc:waiting"]}))
    err = refused(monkeypatch, capsys, real, *on(), *(["--dry-run"] if dry else []))
    assert "sdlc:waiting isn't a state label in the config" in err


def test_lift_isnt_built_yet(monkeypatch, capsys, cfg):
    assert "isn't built yet" in refused(monkeypatch, capsys, cfg, N, "--lift")
    assert "takes no --on" in refused(monkeypatch, capsys, cfg, N, "--lift", "--on", E)


def test_a_wait_by_a_stranger_is_no_wait_and_doesnt_block_one(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, n_comments=[wait_comment(1, author=STRANGER)])
    assert run(capsys, cfg, *on())[0] == 0 and len(gh.posted) == 1


# --- offline: --from-file with --dry-run only -------------------------------------------------------------------

def saved(tmp_path, tgt=True, labels=("sdlc:triage",)):
    b = {"issue": issue(labels=labels), "comments": [], "trusted": sorted(TRUSTED), **({"target": target()} if tgt else {})}
    f = tmp_path / "b.json"
    f.write_text(json.dumps(b))
    return f


def test_from_file_dry_run_reads_no_github(monkeypatch, capsys, cfg, tmp_path):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("--from-file reads the bundle only"))
    code, out, err = run(capsys, cfg, *on(), "--dry-run", "--from-file", saved(tmp_path))
    assert code == 0, err
    assert f"kind=wait waits={E}:M2 from=triage" in out and "labels: add ['sdlc:waiting']" in out


def test_from_file_without_dry_run_is_refused(monkeypatch, capsys, cfg, tmp_path):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("refused before GitHub is read"))
    code, _, err = run(capsys, cfg, *on(), "--from-file", saved(tmp_path))
    assert code == 1 and "--from-file goes with --dry-run only" in err


def test_from_file_without_a_target_is_refused(monkeypatch, capsys, cfg, tmp_path):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("--from-file reads the bundle only"))
    code, _, err = run(capsys, cfg, *on(), "--dry-run", "--from-file", saved(tmp_path, tgt=False))
    assert code == 1 and "has no `target`" in err


# --- the pure check -----------------------------------------------------------------------------------------------

def test_wait_errors_is_pure_and_empty_for_a_good_wait():
    st = sdlc.derive_state(issue(), [], TRUSTED, CONFIG)
    t = target()
    tst = sdlc.derive_state(t["issue"], t["comments"], TRUSTED, CONFIG)
    assert sdlc.wait_errors(st, tst, E, "M2") == []
    assert sdlc.wait_errors(st, tst, E, "all") == []
    assert sdlc.parse_wait_on(f"{E}") == (E, "all") == sdlc.parse_wait_on(f"{E}:all")
