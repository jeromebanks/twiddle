"""tools/sdlc.py `wait N --lift`: ending a wait, finishing one cut short, and `lift_wait` as a function. A faked
`gh`; no network.

`sdlc:waiting` is not in `.sdlc/config.json` yet, so these tests inject a config with it added (as
tests/test_sdlc_wait.py does)."""
import json

import pytest

from tools import sdlc
from tests.test_sdlc_wait import OWNER, POSTER, STRANGER, TRUSTED, agent, comment, target, wait_comment
from tests.test_sdlc_wait_place import CONFIG, E, N, FakeGitHub, issue, run

REASON = "the poster asked for it now"


class FlakyGitHub(FakeGitHub):
    """FakeGitHub whose label edit (or comment post) fails, as a lift cut short by a network error would."""

    def __init__(self, *a, fail=None, **k):
        super().__init__(*a, **k)
        self.fail = fail

    def __call__(self, args, check=True):
        args = list(args)
        if self.fail == "edit" and args[:2] == ["issue", "edit"] or self.fail == "post" and args[:3] == ["api", "-X", "POST"]:
            raise sdlc.SdlcError(f"gh {' '.join(args[:3])} failed: connection reset")
        return super().__call__(args, check)


@pytest.fixture
def cfg(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps(CONFIG))
    return f


def fake(monkeypatch, comments, labels=("sdlc:waiting", "enhancement"), tgt=None, fail=None, state="open"):
    tgt = tgt if tgt is not None else target()
    gh = FlakyGitHub({N: issue(labels=labels, state=state), E: tgt["issue"]}, {N: list(comments), E: tgt["comments"]},
                     fail=fail)
    monkeypatch.setattr(sdlc, "gh", gh)
    return gh


def posts(gh):
    return [w for w in gh.writes if w[:3] == ["api", "-X", "POST"]]


def edits(gh):
    return [w for w in gh.writes if w[:2] == ["issue", "edit"]]


def label_move(edit):
    return edit[edit.index("--add-label") + 1], [edit[i + 1] for i, a in enumerate(edit) if a == "--remove-label"]


# what an issue had before it was parked, per recorded state: its comments and its label then
APPROVED = lambda: [comment(OWNER, sdlc.marker("prd", 1) + "\nPRD", 1),
                    comment(OWNER, sdlc.marker("approval", 1, by=POSTER, via="comment") + "\nok", 2)]
PLANNED = lambda: APPROVED() + [comment(OWNER, sdlc.marker("plan", 1) + "\nthe plan", 3),
                                comment(OWNER, sdlc.marker("plan-created", 1) + "\nmade", 4)]
BEFORE = {"untriaged": (list, ()), "triage": (list, ("sdlc:triage",)), "approved": (APPROVED, ("sdlc:approved",)),
          "planned": (PLANNED, ("sdlc:planned",))}


def next_of(comments, labels):
    st = sdlc.derive_state(issue(labels=labels), comments, TRUSTED, CONFIG)
    b = {"issue": issue(labels=labels), "comments": comments, "trusted": sorted(TRUSTED)}
    st = sdlc.with_next(st, CONFIG, offline=True, trusted=TRUSTED, bundle=b)
    return st["state"], st["turn"], st["action"], st["next"]


# --- lifting: the recorded state comes back -----------------------------------------------------------------------

@pytest.mark.parametrize("frm", list(BEFORE))
def test_a_lift_puts_back_the_recorded_state_and_its_usual_next_move(monkeypatch, capsys, cfg, frm):
    make, before_labels = BEFORE[frm]
    history = make()
    parked = history + [wait_comment(10, **{"from": frm})]
    gh = fake(monkeypatch, parked)
    code, out, err = run(capsys, cfg, N, "--lift")
    assert code == 0, err
    to = "triage" if frm == "untriaged" else frm
    assert len(posts(gh)) == 1 and len(gh.posted) == 1 and len(edits(gh)) == 1 and len(gh.writes) == 2
    assert label_move(edits(gh)[0]) == (f"sdlc:{to}", ["sdlc:waiting"])
    mk = sdlc.parse_marker(gh.posted[0])
    assert (mk["kind"], mk["waits"], mk["to"]) == ("wait-lifted", f"{E}:M2", to)
    assert f"goes back to `{to}`" in gh.posted[0] and f"#{N}: waiting -> {to}" in out
    # read back with the posted comment: the restored state's usual next move, as if it had never been parked
    after = parked + [comment(OWNER, gh.posted[0], 20)]
    restored_labels = (f"sdlc:{to}", "enhancement")
    control_labels = (*before_labels, "enhancement") if frm != "untriaged" else ("sdlc:triage", "enhancement")
    assert next_of(after, restored_labels) == next_of(history, control_labels)
    st = sdlc.derive_state(issue(labels=restored_labels), after, TRUSTED, CONFIG)
    assert st["state"] == to and st["wait"] is None and not st["conflicts"]


def test_after_a_lift_triage_says_triage_issue(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1)])
    assert run(capsys, cfg, N, "--lift")[0] == 0
    after = [wait_comment(1), comment(OWNER, gh.posted[0], 2)]
    assert next_of(after, ("sdlc:triage",))[3] == f"/triage-issue {N}"


def test_after_a_lift_approved_says_plan_issue(monkeypatch, capsys, cfg):
    parked = APPROVED() + [wait_comment(10, **{"from": "approved"})]
    gh = fake(monkeypatch, parked)
    assert run(capsys, cfg, N, "--lift")[0] == 0
    assert next_of(parked + [comment(OWNER, gh.posted[0], 20)], ("sdlc:approved",))[3] == f"/plan-issue {N}"


def test_a_lift_with_a_reason_posts_it(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1)])
    assert run(capsys, cfg, N, "--lift", "--reason", REASON)[0] == 0
    assert f"Reason: {REASON}" in gh.posted[0]


def test_a_lift_after_the_target_shipped_says_so_from_a_saved_bundle(monkeypatch, capsys, cfg, tmp_path):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("--from-file reads the bundle only"))
    f = saved(tmp_path, [wait_comment(1)], tgt=target(shipped=("M2",)))
    code, out, err = run(capsys, cfg, N, "--lift", "--dry-run", "--from-file", f)
    assert code == 0, err
    assert f"#{E}'s M2 has shipped to main" in out
    f = saved(tmp_path, [wait_comment(1)], tgt=target())
    assert f"before #{E}'s M2 shipped to main" in run(capsys, cfg, N, "--lift", "--dry-run", "--from-file", f)[1]


def test_a_plain_lift_reads_no_target(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1)])
    reads = []
    real = sdlc.fetch_bundle
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda n, *a, **k: reads.append(n) or real(n, *a, **k))
    assert run(capsys, cfg, N, "--lift")[0] == 0
    assert reads == [N] and len(gh.posted) == 1


# --- dry run --------------------------------------------------------------------------------------------------------

def test_the_dry_run_prints_the_comment_and_the_label_move_and_writes_nothing(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1)])
    code, out, err = run(capsys, cfg, N, "--lift", "--dry-run", "--reason", REASON)
    assert code == 0, err
    assert gh.writes == [] and gh.posted == []
    assert f"DRY RUN #{N}: waiting -> triage" in out
    assert "labels: add ['sdlc:triage'], remove ['sdlc:waiting']" in out
    assert f"kind=wait-lifted waits={E}:M2 to=triage" in out and f"Reason: {REASON}" in out


def test_the_dry_run_of_a_lift_cut_short_shows_only_the_label(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1), agent("wait-lifted", 2)])
    code, out, err = run(capsys, cfg, N, "--lift", "--dry-run")
    assert code == 0, err
    assert gh.writes == []
    assert "would move it to sdlc:triage" in out and "labels: add ['sdlc:triage'], remove ['sdlc:waiting']" in out
    assert "kind=wait-lifted" not in out


def saved(tmp_path, comments, tgt=None, labels=("sdlc:waiting",)):
    b = {"issue": issue(labels=labels), "comments": comments, "trusted": sorted(TRUSTED),
         **({"target": tgt} if tgt else {})}
    f = tmp_path / "b.json"
    f.write_text(json.dumps(b))
    return f


def test_from_file_dry_run_needs_no_target_for_a_plain_lift(monkeypatch, capsys, cfg, tmp_path):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("--from-file reads the bundle only"))
    code, out, err = run(capsys, cfg, N, "--lift", "--dry-run", "--from-file", saved(tmp_path, [wait_comment(1)]))
    assert code == 0, err
    assert "kind=wait-lifted" in out


def test_from_file_without_dry_run_is_refused(monkeypatch, capsys, cfg, tmp_path):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("refused before GitHub is read"))
    code, _, err = run(capsys, cfg, N, "--lift", "--from-file", saved(tmp_path, [wait_comment(1)]))
    assert code == 1 and "--from-file goes with --dry-run only" in err


# --- not waiting: says so, writes nothing ----------------------------------------------------------------------------

@pytest.mark.parametrize("comments, labels", [
    ([], ("sdlc:triage",)),
    ([], ()),
    ([wait_comment(1), agent("wait-lifted", 2)], ("sdlc:triage",)),        # lifted, and finished
    ([wait_comment(1, author=STRANGER)], ("sdlc:triage",)),                 # a stranger's wait is none
], ids=["triage", "untriaged", "already-lifted", "stranger"])
def test_an_issue_that_isnt_waiting_says_so_and_writes_nothing(monkeypatch, capsys, cfg, comments, labels):
    gh = fake(monkeypatch, comments, labels=labels)
    code, out, err = run(capsys, cfg, N, "--lift")
    assert code == 0, err
    assert f"#{N} isn't waiting" in out and gh.writes == []


def test_a_wait_whose_label_never_moved_is_not_waiting_and_says_so(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1)], labels=("sdlc:triage",))
    code, out, _ = run(capsys, cfg, N, "--lift")
    assert code == 0 and gh.writes == []
    assert f"isn't waiting (it is triage)" in out and "its label never moved" in out


def test_a_closed_issue_has_no_wait_to_lift(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1)], state="closed")
    code, out, _ = run(capsys, cfg, N, "--lift")
    assert code == 0 and "is closed" in out and gh.writes == []


# --- a lift cut short: the rerun moves only the label ------------------------------------------------------------

def test_a_lift_cut_short_between_comment_and_label_is_finished_by_a_rerun(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1)], fail="edit")
    res = sdlc.lift_wait(N, CONFIG)   # a function: it reports, never exits
    assert (res["ok"], res["did"], res["to"]) == (False, "failed", "triage")
    assert "the lift is posted" in res["message"] and f"wait {N} --lift" in res["message"]
    assert len(gh.posted) == 1 and edits(gh) == []
    # GitHub now has the comment; the rerun finds the lift on record and only moves the label
    gh.comments[N].append(comment(OWNER, gh.posted[0], 5))
    gh.fail = None
    code, out, err = run(capsys, cfg, N, "--lift")
    assert code == 0, err
    assert len(gh.posted) == 1 and len(posts(gh)) == 1 and len(edits(gh)) == 1
    assert label_move(edits(gh)[0]) == ("sdlc:triage", ["sdlc:waiting"])
    assert "moved it to sdlc:triage" in out
    # and a third run has nothing left to do
    gh.issues[N] = issue(labels=("sdlc:triage", "enhancement"))
    assert run(capsys, cfg, N, "--lift")[0] == 0 and len(gh.writes) == 2


@pytest.mark.parametrize("frm, to", [("untriaged", "triage"), ("approved", "approved"), ("planned", "planned")])
def test_finishing_a_lift_restores_the_state_the_wait_recorded(monkeypatch, capsys, cfg, frm, to):
    gh = fake(monkeypatch, [wait_comment(1, **{"from": frm}), agent("wait-lifted", 2)])
    res = sdlc.lift_wait(N, CONFIG)
    assert (res["ok"], res["did"], res["to"]) == (True, "finished", to)
    assert gh.posted == [] and label_move(edits(gh)[0]) == (f"sdlc:{to}", ["sdlc:waiting"])


def test_a_failed_post_changes_nothing(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1)], fail="post")
    res = sdlc.lift_wait(N, CONFIG)
    assert (res["ok"], res["did"]) == (False, "failed") and "nothing was changed" in res["message"]
    assert gh.writes == [] and gh.posted == []
    assert run(capsys, cfg, N, "--lift")[0] == 1


def test_an_unreadable_issue_is_a_failure_not_an_exit(monkeypatch):
    def broken(*a, **k):
        raise sdlc.SdlcError("gh issue view failed: HTTP 502")
    monkeypatch.setattr(sdlc, "gh", broken)
    res = sdlc.lift_wait(N, CONFIG)
    assert (res["ok"], res["did"]) == (False, "failed") and "could not read" in res["message"]


# --- lift_wait as a function ----------------------------------------------------------------------------------------

def test_lift_wait_reports_what_it_did(monkeypatch):
    gh = fake(monkeypatch, [wait_comment(1)])
    res = sdlc.lift_wait(N, CONFIG, reason=REASON)
    assert (res["number"], res["ok"], res["did"], res["to"]) == (N, True, "lifted", "triage")
    assert res["url"] == "https://example/308#posted" and len(gh.writes) == 2


def test_lift_wait_takes_a_bundle_already_read(monkeypatch):
    gh = fake(monkeypatch, [])
    b = {"issue": issue(labels=("sdlc:waiting",)), "comments": [wait_comment(1)], "trusted": sorted(TRUSTED)}
    monkeypatch.setattr(sdlc, "fetch_bundle", lambda *a, **k: pytest.fail("the bundle was handed over"))
    res = sdlc.lift_wait(N, CONFIG, bundle=b, dry_run=True)
    assert (res["ok"], res["did"]) == (True, "lifted") and gh.writes == []
    assert "kind=wait-lifted" in res["message"]


def test_lift_wait_reports_nothing_to_do(monkeypatch):
    fake(monkeypatch, [], labels=("sdlc:triage",))
    res = sdlc.lift_wait(N, CONFIG)
    assert (res["ok"], res["did"]) == (True, "nothing")


# --- refusals: each writes nothing -------------------------------------------------------------------------------

def refused(monkeypatch, capsys, cfg, comments, *argv, tgt=None, labels=("sdlc:waiting", "enhancement")):
    gh = fake(monkeypatch, comments, labels=labels, tgt=tgt)
    code, out, err = run(capsys, cfg, N, "--lift", *argv)
    assert code == 1, out
    assert gh.writes == [] and gh.posted == []
    assert "nothing was changed" in err
    return err


def test_then_close_on_a_shipped_target_is_refused_naming_t2(monkeypatch, capsys, cfg):
    err = refused(monkeypatch, capsys, cfg, [wait_comment(1, then="close")], tgt=target(shipped=("M2",)))
    assert "#379's M2 (T2)" in err and "stays waiting" in err


def test_then_close_lifted_early_goes_back_like_any_wait(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1, then="close")], tgt=target(shipped=("M1",)))
    code, out, err = run(capsys, cfg, N, "--lift")
    assert code == 0, err
    assert label_move(edits(gh)[0]) == ("sdlc:triage", ["sdlc:waiting"])
    assert f"before #{E}'s M2 shipped" in gh.posted[0]


def test_then_close_on_a_broken_wait_goes_back(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1, then="close")], tgt=target(state="closed"))
    assert run(capsys, cfg, N, "--lift")[0] == 0 and len(gh.posted) == 1


def test_then_close_with_an_unreadable_target_is_refused(monkeypatch, capsys, cfg):
    gh = fake(monkeypatch, [wait_comment(1, then="close")])
    real = sdlc.fetch_bundle

    def fetch(n, *a, **k):
        if n == E:
            raise sdlc.SdlcError("gh issue view failed: HTTP 502")
        return real(n, *a, **k)
    monkeypatch.setattr(sdlc, "fetch_bundle", fetch)
    code, _, err = run(capsys, cfg, N, "--lift")
    assert code == 1 and "whether it shipped isn't known" in err and gh.writes == []


def test_then_close_from_a_saved_bundle_without_a_target_is_refused(monkeypatch, capsys, cfg, tmp_path):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("--from-file reads the bundle only"))
    code, _, err = run(capsys, cfg, N, "--lift", "--dry-run", "--from-file",
                       saved(tmp_path, [wait_comment(1, then="close")]))
    assert code == 1 and "has no `target`" in err


@pytest.mark.parametrize("frm", [None, "in-progress", "waiting"])
def test_a_wait_that_recorded_no_state_to_go_back_to_is_refused(monkeypatch, capsys, cfg, frm):
    w = agent("wait", 1, "Reason: hand-made", waits=f"{E}:M2", **({"from": frm} if frm else {}))
    assert "recorded no state to go back to" in refused(monkeypatch, capsys, cfg, [w])


def test_a_waiting_label_with_no_wait_recorded_is_refused(monkeypatch, capsys, cfg):
    assert "no wait is recorded" in refused(monkeypatch, capsys, cfg, [])


@pytest.mark.parametrize("reason, why", [("two\nlines", "is one line"), ("at 192.168.1.20", "the issue is public")])
def test_a_reason_that_cant_be_posted(monkeypatch, capsys, cfg, reason, why):
    assert why in refused(monkeypatch, capsys, cfg, [wait_comment(1)], "--reason", reason)


def test_a_config_without_the_waiting_state_refuses(monkeypatch, capsys, tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({**CONFIG, "labels": [l for l in CONFIG["labels"] if l["name"] != "sdlc:waiting"]}))
    gh = fake(monkeypatch, [wait_comment(1)])
    code, _, err = run(capsys, real, N, "--lift")
    assert code == 1 and "isn't a state label in the config" in err and gh.writes == []
