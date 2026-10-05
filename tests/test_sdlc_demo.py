"""tools/sdlc.py milestone demos: the pause, the poster's answer, publishing checks. No network."""
import json
import subprocess

import pytest

from tools import sdlc

CONFIG = sdlc.load_config()
OWNER, POSTER, STRANGER = "owner", "poster", "stranger"
TRUSTED = {OWNER}
_ids = iter(range(1, 10_000))


def issue(labels=(), state="open"):
    return {"number": 12, "title": "Alarm manager", "state": state, "author": POSTER, "labels": list(labels)}


def comment(author, body, ts):
    return {"id": next(_ids), "author": author, "body": body, "created_at": f"2026-10-04T00:{ts:02d}:00Z",
            "url": f"https://example/c{ts}"}


def agent(kind, rev, ts, body="text", **extra):
    return comment(OWNER, sdlc.marker(kind, rev, **extra) + "\n" + body, ts)


# a feature that went through triage and planning: PRD rev 1 approved, plan rev 1 created
HISTORY = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER), agent("plan", 1, 3),
           agent("plan-review", 1, 4, verdict="approve", round="1"), agent("plan-created", 1, 5)]


def derive(labels, comments=()):
    return sdlc.derive_state(issue(labels), HISTORY + list(comments), TRUSTED, CONFIG)


def ms(key, done, total):
    return {"title": f"#12 {key}: x", "key": key, "done": done, "total": total}


def progress(*milestones, open_=1, ready=(30,)):
    return {"ready": list(ready), "in_flight": [], "escalated": [], "waiting": [], "open": open_,
            "milestones": list(milestones)}


# --- the pause --------------------------------------------------------------

def test_a_complete_milestone_comes_before_ready_slices():
    st = derive(["sdlc:in-progress"])
    p = progress(ms("M1", 3, 3), ms("M2", 1, 4))
    assert sdlc.next_command(st, p).startswith("/milestone-demo 12: #12 M1: x is complete")
    accepted = derive(["sdlc:in-progress"], [agent("demo", 1, 10, milestone="M1", sha="a" * 40),
                                             agent("demo-approval", 1, 12, milestone="M1", by=POSTER)])
    # accepted is not enough: until M1 ships to main, nothing of M2 may land on epic/12
    assert sdlc.next_command(accepted, p).startswith("/milestone-demo 12: M1 is accepted; ship it")
    assert sdlc.claim_pause_errors(accepted, p) and "not shipped" in sdlc.claim_pause_errors(accepted, p)[0]
    shipped = derive(["sdlc:in-progress"], [agent("demo", 1, 10, milestone="M1", sha="a" * 40),
                                            agent("demo-approval", 1, 12, milestone="M1", by=POSTER),
                                            agent("shipped", None, 13, milestone="M1", sha="c" * 40)])
    assert shipped["shipped"] == {"M1": "c" * 40}
    assert sdlc.next_command(shipped, p) == "/work-slice 30"


def test_milestones_in_natural_order():
    leaves = [{"number": i, "key": f"T{i}", "state": "closed", "state_reason": "completed", "assignees": [],
               "labels": [], "milestone": f"#12 M{m}: x"} for i, m in ((1, 10), (2, 2))]
    p = sdlc.summarise_progress(leaves, {})
    assert [m["key"] for m in p["milestones"]] == ["M2", "M10"]
    assert sdlc.due_milestone({}, p)["key"] == "M2"


def test_an_epic_without_milestones_has_one_demo():
    assert sdlc.milestone_key(None) == sdlc.milestone_key("(no milestone)") == "all"
    assert sdlc.milestone_key("#47 M3: Something: else") == "M3"
    st = derive(["sdlc:in-progress"])
    assert "every unit of work is merged" in sdlc.next_command(st, progress(ms("all", 5, 5) | {"title": "(no milestone)"}, open_=0, ready=()))


def test_claims_pause_but_resume_does_not():
    st = derive(["sdlc:in-progress"])
    assert "M1" in sdlc.claim_pause_errors(st, progress(ms("M1", 3, 3), ms("M2", 0, 2)))[0]
    assert sdlc.claim_pause_errors(st, progress(ms("M1", 2, 3))) == []
    review = derive(["sdlc:demo-review"], [agent("demo", 1, 10, milestone="M1", sha="a" * 40)])
    assert "demo review" in sdlc.claim_pause_errors(review, None)[0]
    body = sdlc.marker("slice", None, epic="12", key="T2.1") + "\n"
    b = {"issue": {"number": 30, "body": body}, "trusted": [OWNER], "epic_state": st,
         "epic_progress": progress(ms("M1", 3, 3))}
    assert sdlc.epic_pause_errors(b, CONFIG, resume=False)
    assert sdlc.epic_pause_errors(b, CONFIG, resume=True) == []


# --- the poster's answer -----------------------------------------------------

DEMO = agent("demo", 1, 10, milestone="M1", sha="a" * 40)


def test_demo_review_is_the_posters_move_until_they_reply():
    st = derive(["sdlc:demo-review"], [DEMO])
    assert (st["turn"], st["action"]) == ("poster", "wait_for_poster")
    assert st["approved_rev"] == 1 and st["doc_approved"]          # a demo never voids the PRD sign-off
    q = derive(["sdlc:demo-review"], [DEMO, comment(POSTER, "what does the bell icon mean?", 11)])
    assert (q["action"], sdlc.next_command(q)) == ("demo_reply", "/milestone-demo 12")


def test_approve_binds_to_the_latest_demo():
    st = derive(["sdlc:demo-review"], [DEMO, comment(POSTER, "/approve", 11)])
    assert st["action"] == "record_demo_acceptance"
    assert (st["decision"]["milestone"], st["decision"]["rev"]) == ("M1", 1)
    spoof = derive(["sdlc:demo-review"], [DEMO, comment(STRANGER, "/approve", 11)])
    assert spoof["action"] == "demo_reply"
    # a newer revision of the demo voids an approval of the older one
    st = derive(["sdlc:in-progress"], [DEMO, agent("demo-approval", 1, 12, milestone="M1", by=POSTER),
                                       agent("demo", 2, 13, milestone="M1", sha="b" * 40)])
    assert not st["demos"]["M1"]["accepted"] and st["demos"]["M1"]["rev"] == 2


def test_finished_needs_every_milestone_accepted_and_shipped():
    demos = {"M1": {"accepted": True}, "M2": {"accepted": True}}
    both = {"M1": "a", "M2": "b"}
    assert sdlc.finished(progress(ms("M1", 1, 1), ms("M2", 1, 1), open_=0), demos, both)
    assert not sdlc.finished(progress(ms("M1", 1, 1), ms("M2", 1, 1), open_=0), demos, {"M1": "a"})
    assert not sdlc.finished(progress(ms("M1", 1, 1), ms("M2", 0, 1)), {"M1": {"accepted": True}}, both)
    assert not sdlc.finished(progress(ms("M1", 1, 1), ms("M2", 1, 1), open_=0), {"M1": {"accepted": True}}, both)


def _bundle(tmp_path, labels, comments, prog=None):
    f = tmp_path / "bundle.json"
    f.write_text(json.dumps({"issue": issue(labels), "comments": HISTORY + comments, "trusted": [OWNER],
                             "progress": prog}))
    return str(f)


def test_demo_accept_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    b = _bundle(tmp_path, ["sdlc:demo-review"], [DEMO, comment(POSTER, "/approve", 11)],
                progress(ms("M1", 3, 3), ms("M2", 0, 2)))
    assert sdlc.main(["demo-accept", "12", "--from-file", b, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "demo-review -> in-progress" in out and "kind=demo-approval rev=1 milestone=M1 by=poster" in out
    last = _bundle(tmp_path, ["sdlc:demo-review"], [DEMO, comment(POSTER, "/approve", 11)],
                   progress(ms("M1", 3, 3), open_=0, ready=()))
    assert sdlc.main(["demo-accept", "12", "--from-file", last, "--dry-run"]) == 0
    out = capsys.readouterr().out    # even the last milestone ships before the epic is done
    assert "demo-review -> in-progress; then `ship 12`" in out and "and then this issue is done" in out
    nothing = _bundle(tmp_path, ["sdlc:demo-review"], [DEMO])
    assert sdlc.main(["demo-accept", "12", "--from-file", nothing, "--dry-run"]) == 1


# --- /changes become new slices ------------------------------------------------

def test_changes_send_the_epic_back_to_planning():
    changes = [DEMO, comment(POSTER, "/changes the list should show the source", 11),
               agent("demo-changes", 1, 12, milestone="M1")]
    st = derive(["sdlc:in-progress"], changes)
    assert (st["action"], st["feedback"], st["plan_rounds"]) == ("plan", True, 0)
    assert sdlc.next_command(st) == "/plan-issue 12"
    posted = derive(["sdlc:in-progress"], changes + [agent("plan", 2, 13)])
    assert posted["action"] == "continue_plan"
    agreed = derive(["sdlc:in-progress"], changes + [agent("plan", 2, 13), agent("plan-review", 2, 14, verdict="approve", round="1")])
    assert agreed["action"] == "create_plan_issues"
    built = derive(["sdlc:in-progress"], changes + [agent("plan", 2, 13), agent("plan-review", 2, 14, verdict="approve", round="1"),
                                                    agent("plan-created", 2, 15)])
    assert (built["action"], built["feedback"]) == ("work_slices", False)
    assert not built["demos"]["M1"]["accepted"]       # its next demo is rev 2


def test_an_amended_plan_keeps_what_was_created(tmp_path, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("must not call gh"))
    leaf = {"key": "T1", "title": "t", "covers": [1], "outcome": "o", "scope": "s", "acceptance": ["a"],
            "validation": "v", "demo": "d", "non_goals": "n", "context": "c"}
    old = {"issue": 12, "summary": "s", "subtasks": [leaf]}
    new = {"issue": 12, "summary": "s", "subtasks": [{**leaf, "key": "F1"}]}
    plan_comment = comment(OWNER, sdlc.render_comment("plan", 1, sdlc.render_plan(old), CONFIG), 3)
    history = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER), plan_comment,
               agent("plan-review", 1, 4, verdict="approve", round="1"), agent("plan-created", 1, 5),
               DEMO, comment(POSTER, "/changes more", 11), agent("demo-changes", 1, 12, milestone="M1")]
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue(["sdlc:in-progress"]), "comments": history, "trusted": [OWNER]}))
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(new))
    monkeypatch.setattr(sdlc, "check_plan", lambda plan: [])
    with pytest.raises(sdlc.SdlcError, match="drops T1"):
        sdlc.command_plan_post(sdlc.argparse.Namespace(number=12, file=str(p), from_file=str(f), dry_run=True), CONFIG)


def test_superseding_keeps_the_milestone():
    old = sdlc.render_comment("demo", 1, "body", CONFIG, milestone="M1", sha="a" * 40)
    mk = sdlc.parse_marker(sdlc.render_superseded(old, 2, "https://x"))
    assert (mk["kind"], mk["rev"], mk["milestone"], mk["superseded"]) == ("demo", "1", "M1", "2")


# --- publishing -------------------------------------------------------------

@pytest.mark.parametrize("text,hit", [
    ("speaker at 192.168.1.23", "IP"), ("mac 5C:AA:FD:01:02:03", "MAC"), ("serial 5C-AA-FD-01-02-03:E", "MAC"),
    ("uuid RINCON_5CAAFD010203", "player"), ("hh Sonos_abcdefghijklmnop.qrst", "household"),
    ("Authorization: Bearer abcdefghijklmnop", "bearer"), ("secret 0123456789abcdef0123456789abcdef", "hex"),
])
def test_identifier_hits(text, hit):
    assert any(hit.lower() in h.lower() for h in sdlc.identifier_hits(text)), sdlc.identifier_hits(text)


def test_harmless_text_is_not_an_identifier():
    assert sdlc.identifier_hits("version 1.2.3, 127.0.0.1, commit " + "a" * 40 + ", 07:00 weekdays") == []


# what demo_shot draws: a title, then a line of text per row of the terminal
SHOT = ('<svg><text class="t-title">twiddle alarm list</text>'
        '<text class="t-r1" x="0">$&#160;twiddle&#160;alarm&#160;list</text>'
        '<text class="t-r2" x="0">Kitchen&#160;&#160;07:00&#160;weekdays&#160;&#160;KALX&#160;&#160;on</text>'
        '<text class="t-r2" x="0">Bedroom&#160;&#160;06:30&#160;daily&#160;&#160;chime&#160;&#160;off</text></svg>')


def _demo_dir(tmp_path, readme="# M1\n", files=None):
    d = tmp_path / "demo"
    d.mkdir(parents=True)
    (d / "README.md").write_text(readme)
    for name, text in (files or {"list.svg": SHOT}).items():
        (d / name).write_text(text)
    return d


def test_demo_post_errors(tmp_path):
    d = _demo_dir(tmp_path, files={"list.svg": "<svg>192.168.1.9</svg>", "notes.txt": "x"})
    errs = sdlc.demo_post_errors("![list](list.svg) ![gone](missing.svg) [doc](../secret.md)", d)
    assert any("list.svg contains an IP" in e for e in errs)
    assert any("notes.txt" in e for e in errs)
    assert any("missing.svg" in e for e in errs) and any("../secret.md" in e for e in errs)
    ok = _demo_dir(tmp_path / "ok")
    assert sdlc.demo_post_errors("![list](list.svg)\n[the write-up](README.md)\n[PR](https://github.com/x/y/pull/1)", ok) == []


def test_rewrite_demo_links():
    body = '![list](list.svg) [doc](README.md) [pr](https://x/pull/1) <img src="tui.svg" width="600">'
    out = sdlc.rewrite_demo_links(body, "RAW", "BLOB")
    assert out == '![list](RAW/list.svg) [doc](BLOB/README.md) [pr](https://x/pull/1) <img src="RAW/tui.svg" width="600">'


def test_demo_post_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "git", lambda *a, **k: pytest.fail("dry run must not touch git"))
    d = _demo_dir(tmp_path)
    body = tmp_path / "comment.md"
    body.write_text("You can now see every alarm.\n\n![list](list.svg)\n")
    b = _bundle(tmp_path, ["sdlc:in-progress"], [], progress(ms("M1", 3, 3), ms("M2", 0, 2)))
    args = ["demo-post", "12", "--milestone", "M1", "--dir", str(d), "--body-file", str(body), "--from-file", b, "--dry-run"]
    assert sdlc.main(args) == 0
    out = capsys.readouterr().out
    assert "kind=demo rev=1 milestone=M1" in out and "/<commit>/epic-12/M1/rev-1/list.svg" in out
    assert sdlc.main(args[:3] + ["M2"] + args[4:]) == 1          # M2 isn't complete
    assert "not complete" in capsys.readouterr().err


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_publish_demo_to_an_orphan_branch(tmp_path):
    origin, root = tmp_path / "origin.git", tmp_path / "repo"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git("config", k, v, cwd=root)
    (root / "code.py").write_text("x = 1\n")
    (root / ".gitignore").write_text(".worktrees/\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "main", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("push", "-q", "origin", "main", cwd=root)
    d = _demo_dir(tmp_path)
    sha1 = sdlc.publish_demo(d, "epic-12/M1/rev-1", "demo 1", CONFIG, root=root)
    sha2 = sdlc.publish_demo(d, "epic-12/M1/rev-2", "demo 2", CONFIG, root=root)
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", sha2], cwd=origin, capture_output=True, text=True).stdout
    assert files.split() == ["epic-12/M1/rev-1/README.md", "epic-12/M1/rev-1/list.svg",
                             "epic-12/M1/rev-2/README.md", "epic-12/M1/rev-2/list.svg"]   # main's files never land there
    assert sha1 != sha2
    assert subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout == ""


def test_a_question_is_answered_with_a_note_and_changes_are_recorded(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    b = _bundle(tmp_path, ["sdlc:demo-review"], [DEMO, comment(POSTER, "what is the bell for?", 11)])
    answer = tmp_path / "answer.md"
    answer.write_text("It marks an alarm that is on.")
    assert sdlc.main(["transition", "12", "demo-review", "--kind", "note", "--body-file", str(answer),
                      "--from-file", b, "--dry-run"]) == 0
    assert "kind=note" in capsys.readouterr().out
    assert sdlc.main(["demo-changes", "12", "--body-file", str(answer), "--from-file", b, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "demo-review -> in-progress" in out and "kind=demo-changes rev=1 milestone=M1" in out
    waiting = _bundle(tmp_path, ["sdlc:demo-review"], [DEMO])        # nothing from the poster to act on
    assert sdlc.main(["demo-changes", "12", "--body-file", str(answer), "--from-file", waiting, "--dry-run"]) == 1


# --- the poster is reached only through the issue (#69) -------------------------------

def test_a_blank_picture_is_refused():
    assert len(sdlc.svg_text(SHOT)) >= sdlc.SVG_MIN_TEXT
    prompt_only = '<svg><text class="t-title">np</text><text class="t-r1">$&#160;twiddle&#160;np&#160;wfmu</text></svg>'
    assert sdlc.svg_text(prompt_only) == "$twiddlenpwfmu"
    assert len(sdlc.svg_text(prompt_only)) < sdlc.SVG_MIN_TEXT


def test_blank_picture_blocks_the_post(tmp_path):
    d = _demo_dir(tmp_path, files={"np.svg": '<svg><text class="x-r1">$&#160;twiddle&#160;np</text></svg>'})
    assert any("np.svg shows almost no text" in e for e in sdlc.demo_post_errors("![np](np.svg)", d))


REQUEST = agent("demo-request", None, 9, milestone="M2")


def test_a_demo_request_waits_for_whoever_runs_the_steps():
    st = derive(["sdlc:in-progress"], [REQUEST])
    assert (st["turn"], st["action"]) == ("poster", "wait_for_poster")
    heard = derive(["sdlc:in-progress"], [REQUEST, comment(POSTER, "It rang at 7:02, quite loud.", 10)])
    assert (st := heard)["action"] == "demo_heard" and sdlc.next_command(st) == "/milestone-demo 12"
    assert heard["demo_request"]["replies"][0]["by"] == POSTER
    # an agent note after the reply doesn't hide it; a stranger's comment isn't an answer
    noted = derive(["sdlc:in-progress"], [REQUEST, comment(POSTER, "rang", 10), agent("note", None, 11)])
    assert noted["action"] == "demo_heard"
    assert derive(["sdlc:in-progress"], [REQUEST, comment(STRANGER, "nice", 10)])["action"] == "wait_for_poster"
    posted = derive(["sdlc:demo-review"], [REQUEST, comment(POSTER, "rang", 10),
                                          agent("demo", 1, 12, milestone="M2", sha="a" * 40)])
    assert posted["demo_request"] is None and posted["action"] == "wait_for_poster"


def test_demo_request_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    body = tmp_path / "steps.md"
    body.write_text("1. `! uv run twiddle alarm try 3` (dry run: rings Kitchen at 20%)")
    b = _bundle(tmp_path, ["sdlc:in-progress"], [])
    assert sdlc.main(["demo-request", "12", "--milestone", "M2", "--body-file", str(body), "--from-file", b, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "kind=demo-request milestone=M2" in out and "reply here with what you heard" in out
    body.write_text("the Roam at 192.168.1.20")
    assert sdlc.main(["demo-request", "12", "--milestone", "M2", "--body-file", str(body), "--from-file", b, "--dry-run"]) == 1
    waiting = _bundle(tmp_path, ["sdlc:in-progress"], [REQUEST])
    body.write_text("again")
    assert sdlc.main(["demo-request", "12", "--milestone", "M2", "--body-file", str(body), "--from-file", waiting,
                      "--dry-run"]) == 1


def test_the_agents_own_findings_send_the_milestone_to_fix_slices(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    f = tmp_path / "found.md"
    f.write_text("- `alarm list` misses alarms in a bonded room")
    b = _bundle(tmp_path, ["sdlc:in-progress"], [], progress(ms("M1", 3, 3)))
    assert sdlc.main(["demo-changes", "12", "--found", "--milestone", "M1", "--body-file", str(f), "--from-file", b,
                      "--dry-run"]) == 0
    assert "kind=demo-changes milestone=M1 found=agent" in capsys.readouterr().out
    st = derive(["sdlc:in-progress"], [agent("demo-changes", None, 9, milestone="M1", found="agent")])
    assert (st["action"], st["feedback"]) == ("plan", True) and sdlc.next_command(st) == "/plan-issue 12"
    assert sdlc.main(["demo-changes", "12", "--found", "--body-file", str(f), "--from-file", b, "--dry-run"]) == 1


def test_file_issue_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    f = tmp_path / "debt.md"
    f.write_text("`alarm list` sorts rooms case-sensitively.")
    assert sdlc.main(["file-issue", "12", "--title", "alarm list: room order", "--body-file", str(f), "--debt",
                      "--source", "review", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "--label tech-debt" in out and "kind=debt epic=12 source=review" in out
    assert sdlc.main(["file-issue", "12", "--title", "x", "--body-file", str(f), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "tech-debt" not in out and "kind=finding epic=12 source=demo" in out
    f.write_text("player RINCON_000E58123456 drops")
    assert sdlc.main(["file-issue", "12", "--title", "x", "--body-file", str(f), "--dry-run"]) == 1


# --- the order of work, and the cleanup budget ----------------------------------------

def test_the_earliest_milestones_slices_come_first():
    leaves = [{"number": n, "key": k, "state": "open", "assignees": [], "labels": [], "milestone": f"#12 {m}: x"}
              for n, k, m in ((40, "A1", "M2"), (41, "F1", "M1"), (42, "B1", "M10"))]
    assert sdlc.summarise_progress(leaves, {})["ready"] == [41, 40, 42]


def debt_progress(*milestones, debt=(101, 102), budget=1, **kw):
    return {**progress(*milestones, **kw), "debt": list(debt), "cleanup_budget": budget}


def test_a_cleanup_pass_comes_after_an_accepted_milestone():
    shipped_m1 = [DEMO, agent("demo-approval", 1, 12, milestone="M1", by=POSTER),
                  agent("shipped", None, 13, milestone="M1", sha="c" * 40)]
    st = derive(["sdlc:in-progress"], shipped_m1)
    p = debt_progress(ms("M1", 3, 3), ms("M2", 0, 4), ready=(35,))
    assert sdlc.next_command(st, p).startswith("/plan-issue 12 --cleanup: M1 is accepted and #12 has 2 open tech-debt")
    assert sdlc.next_command(st, {**p, "debt": []}) == "/work-slice 35"
    assert sdlc.next_command(st, {**p, "cleanup_budget": 0}) == "/work-slice 35"
    done = derive(["sdlc:in-progress"], shipped_m1 + [agent("plan", 2, 14), agent("plan-review", 2, 15, verdict="approve"),
                                                      agent("plan-created", 2, 16, cleanup="M1")])
    assert done["cleanups"] == ["M1"] and sdlc.next_command(done, p) == "/work-slice 35"
    last = debt_progress(ms("M1", 3, 3), ready=())       # after the last milestone the debt stays filed
    assert sdlc.cleanup_due(st, last) is None


def test_a_cleanup_plan_is_held_to_its_budget(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("dry run must not call gh"))
    monkeypatch.setattr(sdlc, "check_plan", lambda plan: [])
    def unit(key, ms_key):
        return {"key": key, "title": key, "covers": [1], "outcome": "o", "scope": "s", "acceptance": ["a"],
                "validation": "v", "demo": "d", "non_goals": "n", "context": "c", "complexity": "routine",
                "complexity_reason": "r", "milestone": ms_key}
    old = {"issue": 12, "kind": "feature", "create": "all",
           "milestones": [{"key": "M1", "title": "a", "demo": "d"}, {"key": "M2", "title": "b", "demo": "d"}],
           "subtasks": [unit("T1", "M1"), unit("T2", "M2")]}
    history = [agent("prd", 1, 1), agent("approval", 1, 2, by=POSTER),
               comment(OWNER, sdlc.render_comment("plan", 1, sdlc.render_plan(old), CONFIG), 3),
               agent("plan-review", 1, 4, verdict="approve", round="1"), agent("plan-created", 1, 5),
               DEMO, agent("demo-approval", 1, 12, milestone="M1", by=POSTER)]
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"issue": issue(["sdlc:in-progress"]), "comments": history, "trusted": [OWNER]}))
    pf = tmp_path / "plan.json"
    two = {**old, "cleanup_of": "M1", "subtasks": old["subtasks"] + [{**unit("C1", "M2"), "debt": [101]},
                                                                     {**unit("C2", "M2"), "debt": [102]}]}
    pf.write_text(json.dumps(two))
    assert sdlc.main(["plan-post", "12", str(pf), "--from-file", str(f), "--dry-run"]) == 1
    assert "adds at most 1 slice" in capsys.readouterr().err
    pf.write_text(json.dumps({**two, "subtasks": two["subtasks"][:3]}))
    assert sdlc.main(["plan-post", "12", str(pf), "--from-file", str(f), "--dry-run"]) == 0
    body = sdlc.render_leaf_body(12, {**unit("C1", "M2"), "debt": [101]}, None, {}, None)
    assert "## Pays down\n\n#101: tech debt" in body


def test_a_demo_request_ends_when_the_milestone_goes_back_to_being_built():
    found = [REQUEST, comment(POSTER, "it didn't ring", 10),
             agent("demo-changes", None, 11, milestone="M2", found="agent"),
             agent("plan", 2, 12), agent("plan-review", 2, 13, verdict="approve"), agent("plan-created", 2, 14)]
    st = derive(["sdlc:in-progress"], found)
    assert st["demo_request"] is None and st["action"] == "work_slices"
    voided = derive(["sdlc:in-progress"], [REQUEST, comment(POSTER, "rang", 10), agent("demo-void", None, 11, milestone="M2")])
    assert voided["demo_request"] is None and voided["action"] == "work_slices"
    other = derive(["sdlc:in-progress"], [REQUEST, comment(POSTER, "rang", 10), agent("demo-void", None, 11, milestone="M1")])
    assert other["action"] == "demo_heard"


def test_tech_debt_waits_for_its_epics_cleanup_pass():
    st = sdlc.derive_state({"number": 81, "title": "t", "state": "open", "author": OWNER, "labels": ["tech-debt"]},
                           [], TRUSTED, CONFIG)
    assert (st["turn"], st["plan_kind"]) == ("none", "debt")
    assert sdlc.next_command(st).startswith("nothing: #81 is tech debt, planned by its epic's cleanup pass")
    assert sdlc.check_transition(st, "triage", "note", CONFIG)


def test_cleanup_slices_go_first():
    pays = "## Pays down\n\n#81: tech debt filed against this epic."
    leaves = [{"number": 40, "key": "A1", "state": "open", "assignees": [], "labels": [], "milestone": "#12 M2: x", "body": ""},
              {"number": 44, "key": "C1", "state": "open", "assignees": [], "labels": [], "milestone": "#12 M2: x", "body": pays}]
    p = sdlc.summarise_progress(leaves, {})
    assert (p["ready"], p["cleanup_open"]) == ([44], [44])
    epic = derive(["sdlc:in-progress"])
    bundle = {"issue": {"number": 40, "body": sdlc.marker("slice", None, epic="12", key="A1"), "milestone": "#12 M2: x"},
              "epic_state": epic, "epic_progress": p, "trusted": [OWNER]}
    assert "the cleanup pass comes first: #44" in sdlc.epic_pause_errors(bundle, CONFIG, resume=False)
    bundle["issue"] = {"number": 44, "body": sdlc.marker("slice", None, epic="12", key="C1") + "\n" + pays,
                       "milestone": "#12 M2: x"}
    assert sdlc.epic_pause_errors(bundle, CONFIG, resume=False) == []


def test_only_svg_pictures_are_published(tmp_path):
    d = _demo_dir(tmp_path, files={"list.svg": SHOT, "photo.png": ""})
    assert any("photo.png: only .md, .svg files are published" in e for e in sdlc.demo_post_errors("![l](list.svg)", d))


def test_a_cleanup_slice_can_wait_on_ordinary_work_without_a_deadlock():
    pays = "## Pays down\n\n#81: tech debt filed against this epic."
    leaves = [{"number": 40, "key": "T2", "state": "open", "assignees": [], "labels": [], "milestone": "#12 M2: x", "body": ""},
              {"number": 41, "key": "T3", "state": "open", "assignees": [], "labels": [], "milestone": "#12 M2: x", "body": ""},
              {"number": 44, "key": "C1", "state": "open", "assignees": [], "labels": [], "milestone": "#12 M2: x", "body": pays}]
    p = sdlc.summarise_progress(leaves, {44: [{"number": 40, "state": "open"}]})
    assert (p["ready"], p["cleanup_needs"]) == ([40], [40])         # C1's prerequisite goes first; T3 still waits
    bundle = {"issue": {"number": 40, "body": sdlc.marker("slice", None, epic="12", key="T2"), "milestone": "#12 M2: x"},
              "epic_state": derive(["sdlc:in-progress"]), "epic_progress": p, "trusted": [OWNER]}
    assert sdlc.epic_pause_errors(bundle, CONFIG, resume=False) == []
