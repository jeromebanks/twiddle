"""tools/codex_eval.py: the review logic's fingerprint and the eval results recorded against it; and what
`codex-review` checks of a run's session log (`codex.skills`). Offline: a fake `codex`, tmp git repos, saved bundles."""
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.fake_codex import APPROVES, SETTINGS, calls, install, no_real_codex_home  # noqa: F401
from tests.test_sdlc_review import Codex
from tools import codex_eval, codex_review, sdlc

ROOT = Path(__file__).resolve().parent.parent
CONFIG = sdlc.load_config()
OWNER, POSTER, STRANGER = "owner", "poster", "stranger"
TRUSTED = {OWNER}
_ids = iter(range(1, 10_000))


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A tmp git repo holding a copy of this one's skills, tools, config and docs, committed."""
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "t")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "t@example.invalid")
    dst = tmp_path / "repo"
    listed = git(ROOT, "ls-files", "-co", "--exclude-standard", ".agents", "tools", ".sdlc", "CLAUDE.md", "SDLC.md")
    for name in listed.splitlines():
        if (ROOT / name).is_file():
            (dst / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, dst / name)
    git(dst, "init", "-q")
    git(dst, "add", "-A")
    git(dst, "commit", "-qm", "base")
    return dst


def fp(root):
    return codex_eval.fingerprint(codex_eval.Worktree(root))


def set_codex(root, **kw):
    cfg = root / ".sdlc" / "config.json"
    data = json.loads(cfg.read_text())
    data["codex"].update(kw)
    cfg.write_text(json.dumps(data, indent=2))


# --- the fingerprint -------------------------------------------------------------------------

def test_every_input_changes_the_fingerprint_and_nothing_else_does(repo):
    base = fp(repo)
    assert re.fullmatch(r"[0-9a-f]{64}", base) and codex_eval.short(base) == base[:12]
    for name in codex_eval.FILES:
        path = repo / name
        assert path.is_file(), f"{name} is an input the repo should have"
        old = path.read_bytes()
        path.write_bytes(old + b"\n")
        assert fp(repo) != base, f"editing {name} must change the fingerprint"
        path.write_bytes(old)
        assert fp(repo) == base
    # the `codex` section: any key
    cfg = (repo / ".sdlc" / "config.json").read_text()
    set_codex(repo, timeout_seconds=901)
    assert fp(repo) != base
    (repo / ".sdlc" / "config.json").write_text(cfg)
    # a fixture or the scorer under tests/codex_eval/, added (untracked counts), edited, removed
    fixture = repo / "tests" / "codex_eval" / "clean" / "expect.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("{}")
    with_fixture = fp(repo)
    assert with_fixture != base
    fixture.write_text('{"verdict": "approve"}')
    assert fp(repo) not in (base, with_fixture)
    shutil.rmtree(repo / "tests")
    assert fp(repo) == base
    # elsewhere: sdlc.py, CLAUDE.md, the rest of the config, a skill (suppressed), a doc
    for name, edit in (("tools/sdlc.py", "\n# an ordinary edit\n"), ("CLAUDE.md", "\nmore words\n"),
                       ("SDLC.md", "\nmore words\n"), (".agents/skills/work-slice/SKILL.md", "\nmore words\n")):
        (repo / name).write_text((repo / name).read_text() + edit)
        assert fp(repo) == base, f"an edit to {name} must not change the fingerprint"
    data = json.loads((repo / ".sdlc" / "config.json").read_text())
    data["max_pr_rounds"] = 9
    (repo / ".sdlc" / "config.json").write_text(json.dumps(data))
    assert fp(repo) == base


def test_a_commit_and_a_checkout_of_it_have_one_fingerprint(repo, tmp_path):
    (repo / "src" / "deep").mkdir(parents=True)
    (repo / "src" / "deep" / "AGENTS.md").write_text("no trailing newline")
    (repo / "tests" / "codex_eval").mkdir(parents=True)
    (repo / "tests" / "codex_eval" / "case.diff").write_bytes(b"binary\x00\r\n\n\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "more inputs")
    sha = git(repo, "rev-parse", "HEAD")
    # ignored files never count (a venv, a nested worktree's AGENTS.md)
    (repo / ".gitignore").write_text(".worktrees/\n.venv/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore")
    sha2 = git(repo, "rev-parse", "HEAD")
    (repo / ".worktrees" / "slice-1").mkdir(parents=True)
    (repo / ".worktrees" / "slice-1" / "AGENTS.md").write_text("another checkout's")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "AGENTS.md").write_text("a package's")
    here = fp(repo)
    assert codex_eval.fingerprint(codex_eval.Commit(sha2, repo)) == here
    assert codex_eval.fingerprint(codex_eval.Commit(sha, repo)) == here       # .gitignore isn't an input
    # a second checkout of the commit, elsewhere
    other = tmp_path / "other"
    git(repo, "worktree", "add", "-q", "--detach", str(other), sha2)
    assert fp(other) == here
    assert codex_eval.fingerprint(codex_eval.Commit(git(repo, "rev-parse", "HEAD~2"), repo)) != here


@pytest.mark.parametrize("mode", codex_review.SKILL_MODES)
def test_an_agents_md_anywhere_changes_it_in_both_modes(repo, mode):
    set_codex(repo, skills=mode)
    base = fp(repo)
    for where in ("AGENTS.md", "AGENTS.override.md", "src/twiddle/AGENTS.md", ".agents/AGENTS.override.md"):
        f = repo / where
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("be brief")
        added = fp(repo)
        assert added != base, f"adding {where}"
        f.write_text("be thorough")
        assert fp(repo) not in (base, added), f"editing {where}"
        f.unlink()
        assert fp(repo) == base, f"removing {where}"


def test_fingerprinted_mode_covers_the_repos_skills_and_switching_modes_changes_it(repo):
    suppressed = fp(repo)
    set_codex(repo, skills="fingerprinted")
    fingerprinted = fp(repo)
    assert fingerprinted != suppressed
    names = [n for n, _ in codex_eval.inputs(codex_eval.Worktree(repo))]
    skills = sorted(p.relative_to(repo).as_posix() for p in (repo / ".agents" / "skills").rglob("SKILL.md"))
    assert skills and set(skills) <= set(names)
    for name in skills:
        f = repo / name
        old = f.read_text()
        f.write_text(old + "\nmore\n")
        assert fp(repo) != fingerprinted, f"editing {name} in fingerprinted mode"
        f.write_text(old)
    new = repo / ".agents" / "skills" / "new-skill" / "SKILL.md"
    new.parent.mkdir()
    new.write_text("a new skill")
    assert fp(repo) != fingerprinted
    set_codex(repo, skills="suppressed")
    assert fp(repo) == suppressed                  # suppressed: the skills aren't offered, so they aren't inputs


def test_a_change_to_the_evidence_selection_voids_a_pass(repo):
    """What a round is handed is chosen in codex_review.py, so a change there is a change of review logic."""
    version = "0.161.0"
    before = fp(repo)
    results = [{"fingerprint": before, "codex": version, "outcome": "pass"}]
    assert codex_eval.pair_result(results, before, version)["outcome"] == "pass"
    src = repo / "tools" / "codex_review.py"
    text = src.read_text()
    for fn, edit in (("def approved_requirements(", ("sorted(p for p in listed", "sorted((p for p in listed), reverse=True")),
                     ("def milestone_slices(", ('if (l.get("milestone") or "(no milestone)") != title:',
                                               'if (l.get("milestone") or "") != title:'))):
        assert fn in text and edit[0] in text
        src.write_text(text.replace(edit[0], edit[1], 1))
        after = fp(repo)
        assert after != before and codex_eval.pair_result(results, after, version) is None
        src.write_text(text)
    assert fp(repo) == before


def test_every_file_a_review_reads_is_an_input_or_excluded_on_purpose():
    files = set(codex_eval.FILES)
    # every Path the runner and the fingerprint hold: a file is an input or excluded; a directory is a glob's root
    for module in (codex_review, codex_eval):
        for name, value in vars(module).items():
            if not isinstance(value, Path) or value == codex_review.ROOT:     # ROOT: the repo itself
                continue
            rel = value.relative_to(codex_review.ROOT).as_posix()
            if value.suffix:
                assert rel in files or rel in codex_eval.EXCLUDED, f"{module.__name__}.{name} ({rel}) is read but not fingerprinted"
                if module is codex_review:
                    assert rel in files, f"codex_review.{name} ({rel}) must be an input"
            else:
                assert codex_eval.is_input(f"{rel}/x/SKILL.md", "fingerprinted"), f"{module.__name__}.{name} ({rel}/)"
    prompts = {p.relative_to(ROOT).as_posix() for p in (ROOT / ".agents").rglob("codex-*-prompt.md")}
    assert len(prompts) == 3 and prompts <= files
    assert {".agents/skills/review-rules.md", ".agents/skills/plan-issue/references/plan-schema.md",
            "tools/codex_review.py", "tools/codex_eval.py"} <= files
    # what a prompt tells Codex to read, in backticks, below its `---` line (the header is for people)
    basenames = {f.rsplit("/", 1)[-1] for f in files}
    named = set()
    for p in sorted(prompts):
        text = (ROOT / p).read_text().split("\n---\n", 1)[1]
        for tok in re.findall(r"`([^`\s]+)`", text):
            if "<" not in tok and re.fullmatch(r"[\w./-]+\.(md|py|json|jsonl|toml)", tok):
                named.add(tok)
                assert tok in files or tok in basenames or tok in codex_eval.EXCLUDED, \
                    f"{p} names `{tok}` for Codex to read: make it an input, or say why not in codex_eval.EXCLUDED"
    assert "review-rules.md" in {n.rsplit("/", 1)[-1] for n in named} and "CLAUDE.md" in named


# --- the ledger ----------------------------------------------------------------------------------

FP, FP2 = "1" * 64, "2" * 64


def comment(author, body, ts):
    return {"id": next(_ids), "author": author, "body": body, "created_at": f"2026-10-07T00:{ts:02d}:00Z",
            "url": f"https://example/c{ts}"}


def result_comment(ts, outcome="pass", fingerprint=FP, version="0.161.0", author=OWNER, **kw):
    body = sdlc.eval_comment({"fingerprint": fingerprint, "codex": version, "outcome": outcome, "model": "gpt-6.1-sol",
                              "effort": "high", **kw}, CONFIG)
    return comment(author, body, ts)


def test_the_latest_result_per_pair_and_a_new_version_needs_its_own():
    comments = [result_comment(1, "fail"), result_comment(2, "pass"), result_comment(3, "pass", version="0.160.0"),
                result_comment(4, "pass", fingerprint=FP2), result_comment(5, "fail", author=STRANGER),
                comment(OWNER, "an ordinary comment", 6)]
    results = sdlc.eval_records(comments, TRUSTED)
    assert len(results) == 4                                       # a stranger's marker isn't a result
    latest = codex_eval.latest_results(results)
    assert latest[(FP, "0.161.0")]["outcome"] == "pass" and latest[(FP, "0.161.0")]["url"] == "https://example/c2"
    assert codex_eval.pair_result(results, FP, "0.162.0") is None  # a pass for an older codex never counts
    assert codex_eval.pair_result(results + sdlc.eval_records([result_comment(7, "deferred")], TRUSTED),
                                  FP, "0.161.0")["outcome"] == "deferred"     # the latest counts, whatever it says
    assert codex_eval.warning(FP, "0.161.0", latest[(FP, "0.161.0")]) is None
    assert "codex-eval" in codex_eval.warning(FP, "0.162.0", None)


def test_a_result_comment_carries_everything_and_refuses_what_a_marker_cant_hold():
    body = result_comment(1, "fail", fixtures=[{"name": "off-by-one", "outcome": "miss", "detail": "no line named"}])["body"]
    mk = sdlc.parse_marker(body)
    assert (mk["kind"], mk["fp"], mk["codex"], mk["outcome"], mk["model"], mk["effort"]) == \
        ("codex-eval", FP, "0.161.0", "fail", "gpt-6.1-sol", "high")
    assert "`off-by-one`: miss: no line named" in body and FP[:12] in body
    for bad in ({"outcome": "maybe"}, {"fingerprint": "abc"}, {"codex": "1.0 beta"}, {"model": "a--b"}):
        with pytest.raises(sdlc.SdlcError):
            sdlc.eval_comment({"fingerprint": FP, "codex": "1", "outcome": "pass", **bad}, CONFIG)


def test_record_eval_posts_on_the_ledger_issue(monkeypatch, capsys):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("a dry run must not call gh"))
    assert CONFIG["codex_eval_issue"] == 100
    assert sdlc.record_eval({"fingerprint": FP, "codex": "0.161.0", "outcome": "deferred", "reason": "quota"},
                            CONFIG, dry_run=True) == {}
    assert "kind=codex-eval" in capsys.readouterr().out
    posted = []
    monkeypatch.setattr(sdlc, "post_comment", lambda n, body, config: posted.append((n, body)) or {"html_url": "u"})
    sdlc.record_eval({"fingerprint": FP, "codex": "0.161.0", "outcome": "pass"}, CONFIG)
    assert posted[0][0] == 100 and sdlc.parse_marker(posted[0][1])["outcome"] == "pass"


# --- a result never hides the poster's answer -------------------------------------------------------

HISTORY = [comment(OWNER, sdlc.marker(k, r, **x) + "\ntext", ts) for k, r, ts, x in (
    ("prd", 1, 1, {}), ("approval", 1, 2, {"by": POSTER}), ("plan", 1, 3, {}),
    ("plan-review", 1, 4, {"verdict": "approve", "round": "1"}), ("plan-created", 1, 5, {}))]
DEMO = comment(OWNER, sdlc.marker("demo", 1, milestone="M1", sha="a" * 40) + "\ndemo", 10)


def derive(labels, comments):
    issue = {"number": 100, "title": "x", "state": "open", "author": POSTER, "labels": list(labels)}
    return sdlc.derive_state(issue, HISTORY + list(comments), TRUSTED, CONFIG)


def test_a_result_comment_leaves_the_issues_state_alone():
    for labels, comments in ((["sdlc:demo-review"], [DEMO]),
                             (["sdlc:demo-review"], [DEMO, comment(POSTER, "what is the bell for?", 11)]),
                             (["sdlc:demo-review"], [DEMO, comment(POSTER, "/approve", 11)]),
                             (["sdlc:in-progress"], [comment(OWNER, sdlc.marker("demo-request", None, milestone="M1"), 9),
                                                     comment(POSTER, "it rang", 11)])):
        without = derive(labels, comments)
        with_result = derive(labels, comments + [result_comment(12)])
        for k in ("state", "turn", "action", "replies", "decision"):
            assert with_result[k] == without[k], (labels, k)
    asked = derive(["sdlc:demo-review"], [DEMO, comment(POSTER, "what is the bell for?", 11), result_comment(12)])
    assert (asked["turn"], asked["action"]) == ("agent", "demo_reply") and asked["replies"][0]["by"] == POSTER
    quiet = derive(["sdlc:demo-review"], [DEMO, result_comment(12)])
    assert (quiet["turn"], quiet["action"], quiet["replies"]) == ("poster", "wait_for_poster", [])


# --- codex-eval --status ---------------------------------------------------------------------

def test_status_prints_the_pair_and_its_result_and_never_runs_codex_exec(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sdlc, "gh", lambda *a, **k: pytest.fail("--from-file must not call gh"))
    log, _, _ = install(tmp_path, monkeypatch)
    here = codex_eval.fingerprint(codex_eval.Worktree(ROOT))
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"eval": [], "trusted": [OWNER]}))
    assert sdlc.main(["codex-eval", "--status", "--from-file", str(ledger)]) == 0
    out = capsys.readouterr().out
    assert f"review logic: {here[:12]}" in out and here not in out and "codex:        9.9.9-test" in out
    assert "no result recorded" in out
    ledger.write_text(json.dumps({"eval": [result_comment(1, "pass", fingerprint=here, version="9.9.9-test")],
                                  "trusted": [OWNER]}))
    assert sdlc.main(["codex-eval", "--status", "--inputs", "--from-file", str(ledger)]) == 0
    out = capsys.readouterr().out
    assert "eval:         passed" in out and "config:codex" in out and "tools/codex_review.py" in out
    assert calls(log) == [] and Path(f"{log}.version").read_text().count("--version") == 2   # never `codex exec`
    only_git = tmp_path / "only-git"                                # no `codex` on the PATH at all
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))
    assert sdlc.main(["codex-eval", "--status", "--from-file", str(ledger)]) == 0
    assert "not found on PATH" in capsys.readouterr().out


# --- codex-review's warning ----------------------------------------------------------------------

def run_with_ledger(c, eval_comments):
    b = json.loads(Path(c.bundle()).read_text())
    b["eval"] = eval_comments
    f = c.tmp / "with-ledger.json"
    f.write_text(json.dumps(b))
    return sdlc.main(["--config", str(c.config), "codex-review", "--pr", "50", "--out", str(c.out), "--from-file", str(f)])


def test_codex_review_warns_when_the_pair_has_no_pass_and_still_runs(tmp_path, monkeypatch, capsys):
    c = Codex(tmp_path, monkeypatch)
    assert c.run() == 0 and c.report().is_file()                # a bundle with no `eval`: no results
    err = capsys.readouterr().err
    assert "no passing eval" in err and "codex-eval --status" in err and "9.9.9-test" in err
    here = codex_eval.fingerprint(codex_eval.Worktree(ROOT))
    for outcome in ("fail", "deferred"):
        assert run_with_ledger(c, [result_comment(1, outcome, fingerprint=here, version="9.9.9-test")]) == 0
        assert "no passing eval" in capsys.readouterr().err and c.report().is_file()
    assert run_with_ledger(c, [result_comment(1, "pass", fingerprint=here, version="0.1.0")]) == 0
    assert "no passing eval" in capsys.readouterr().err         # passed on another codex
    assert run_with_ledger(c, [result_comment(1, "pass", fingerprint=here, version="9.9.9-test")]) == 0
    assert "no passing eval" not in capsys.readouterr().err
    assert c.run("--dry-run") == 0 and "no passing eval" not in capsys.readouterr().err   # nothing runs, no warning


# --- skills: suppressed, and checked in the session log --------------------------------------------

def test_suppressed_mode_turns_skills_off_in_the_generated_config():
    assert tomllib.loads(codex_review.config_toml(SETTINGS))["skills"] == {"include_instructions": False}
    assert "skills" not in tomllib.loads(codex_review.config_toml({**SETTINGS, "skills": "fingerprinted"}))
    assert codex_review.codex_settings(CONFIG)["skills"] == "suppressed"
    with pytest.raises(sdlc.SdlcError, match="codex.skills"):
        codex_review.codex_settings({"codex": {**SETTINGS, "skills": "sometimes"}})


@pytest.mark.parametrize("session,says", [("skills", "offered skills"), ("unknown", "no full `world_state`"),
                                          ("none", "0 session logs")])
def test_a_run_offered_skills_or_with_a_log_nobody_knows_saves_nothing(tmp_path, monkeypatch, capsys, session, says):
    c = Codex(tmp_path, monkeypatch, plan=({**APPROVES, "session": session},))
    assert c.run() == codex_review.UNAVAILABLE
    assert not c.report().exists() and says in capsys.readouterr().out
    assert len(c.calls()) == 1                                     # offered or unknown: not retried for the same answer


def test_an_agents_md_from_outside_the_checkout_is_refused_in_both_modes(tmp_path, monkeypatch, capsys):
    c = Codex(tmp_path, monkeypatch, plan=({**APPROVES, "agents_md": str(tmp_path / "home")},))
    assert c.run() == codex_review.UNAVAILABLE and not c.report().exists()
    assert "outside the reviewed checkout" in capsys.readouterr().out
    c.set_plan({**APPROVES, "agents_md": str(c.wt / "src")})      # the repo's own: fingerprinted, so fine
    assert c.run() == 0 and c.report().is_file()
    (tmp_path / "fp").mkdir()
    f = Codex(tmp_path / "fp", monkeypatch, plan=({**APPROVES, "agents_md": "/elsewhere"},),
              settings={**SETTINGS, "skills": "fingerprinted"})
    assert f.run() == codex_review.UNAVAILABLE


def test_fingerprinted_mode_lets_skills_be_offered(tmp_path, monkeypatch):
    c = Codex(tmp_path, monkeypatch, settings={**SETTINGS, "skills": "fingerprinted"})
    assert c.run() == 0 and c.report().is_file()
    assert "include_instructions" not in c.calls()[0]["config"]


def write_log(path, records, tail=""):
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + tail)
    return path


STATE = {"skills": {"includeInstructions": False}, "host_skills": {"includeInstructions": False}, "agents_md": {}}


def test_check_session_reads_the_shapes_it_knows(tmp_path):
    wt = tmp_path / "wt"
    ok = [{"type": "session_meta", "payload": {}}, {"type": "world_state", "payload": {"full": True, "state": STATE}}]
    log = write_log(tmp_path / "a.jsonl", ok)
    assert codex_review.check_session(log, wt, "suppressed") == ("ok", "")
    on = {**STATE, "host_skills": {"includeInstructions": True}}
    assert codex_review.check_session(write_log(log, [{"type": "world_state", "payload": {"full": True, "state": on}}]),
                                      wt, "suppressed")[0] == "offered"
    assert codex_review.check_session(log, wt, "fingerprinted")[0] == "ok"
    message = {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                                    "content": [{"type": "input_text", "text": "<skills_instructions>"}]}}
    assert codex_review.check_session(write_log(log, ok + [message]), wt, "suppressed")[0] == "offered"
    no_flag = {k: v for k, v in STATE.items() if k not in ("skills", "host_skills")}
    assert codex_review.check_session(write_log(log, [{"type": "world_state", "payload": {"full": True, "state": no_flag}}]),
                                      wt, "suppressed")[0] == "unknown"
    odd = {**STATE, "skills": {"includeInstructions": "no"}}
    assert codex_review.check_session(write_log(log, [{"type": "world_state", "payload": {"full": True, "state": odd}}]),
                                      wt, "suppressed")[0] == "unknown"
    # a run killed on its timeout may leave half a line: only then is it skipped
    assert codex_review.check_session(write_log(log, ok, tail='{"type": "resp'), wt, "suppressed", cut_short=True)[0] == "ok"
    assert codex_review.check_session(log, wt, "suppressed")[0] == "unknown"
