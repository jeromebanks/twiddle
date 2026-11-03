"""tools/demo_shot.py: demo pictures are masked and drawn offline (no network, no speaker)."""
import subprocess
import sys

import pytest
from textual.app import App
from textual.widgets import Static

from tools import demo_shot, sdlc


@pytest.fixture(autouse=True)
def not_a_watcher_run(monkeypatch):
    """These tests may themselves run inside a watcher run: each starts unset, and sets it itself."""
    monkeypatch.delenv(demo_shot.WATCH_ENV, raising=False)


def test_mask_hides_identifiers():
    text, n = demo_shot.mask("Roam  192.168.1.23  RINCON_5CAAFD010203  07:00")
    assert n == 2 and "192.168" not in text and "RINCON" not in text and "07:00" in text


def test_cli_shot(tmp_path):
    out = tmp_path / "shot.svg"
    rc = demo_shot.shot_cli([sys.executable, "-c", "print('Living Room  10.0.0.7  weekdays 07:00')"],
                            out, "twiddle alarm list", 80, None, 30)
    svg = out.read_text()
    assert rc == 0 and svg.startswith("<svg") and "alarm" in svg
    assert "weekdays" in svg and sdlc.identifier_hits(svg) == []


class Flip(App):
    BINDINGS = [("x", "flip", "Flip")]

    def compose(self):
        yield Static("before", id="s")

    def action_flip(self):
        self.query_one("#s").update("after")


def test_tui_steps_press_keys_and_save_screens(tmp_path):
    saved = demo_shot.drive(Flip(), ["shot:one", "x", "wait:0.05", "shot:two"], tmp_path, (40, 10))
    assert [p.name for p in saved] == ["one.svg", "two.svg"]
    assert "before" in saved[0].read_text() and "after" in saved[1].read_text()


# --- inside an unattended watcher run (TWIDDLE_SDLC_WATCH_RUN set) ---------------------------

@pytest.fixture
def watched(monkeypatch):
    """The variable set (even empty counts), and subprocess.run recorded instead of run."""
    monkeypatch.setenv(demo_shot.WATCH_ENV, "")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Living Room  PLAYING\n")

    monkeypatch.setattr(demo_shot.subprocess, "run", fake_run)
    return calls


REFUSED = [
    "uv run twiddle play --room roam",
    "uv run twiddle tune kalx --room roam",
    "uv run twiddle alarm rm 1",
    "twiddle alarm rm 1",
    "uv run twiddle snapshot --room roam",
    "uv run twiddle scene list --refresh",
    "uv run twiddle scene list --ref",                 # argparse would expand it to --refresh
    "uv run twiddle scene --dry-run list",             # a parent's option between the verbs
    "uv run twiddle alarm --json list",
    "uv run twiddle np -d",                            # discover writes once you pick a track
    "uv run twiddle np kalx -d",
    "uv run twiddle np --room -d",
    "uv run twiddle np --room=-d",
    "uv run twiddle status --json=1",
    "uv run twiddle status extra",
    "uv run twiddle status -- play",
    "uv run twiddle daemon install",
    "uv run twiddle daemon --interval 5 status",
    "uv run twiddle stations probe kalx",
    "uv run twiddle stations search kalx",
    "uv run twiddle relay start",
    "uv run twiddle spotify play x",
    "uv run --with evil twiddle status",
    "uv run python -m twiddle status",
    "uv run python tools/sdlc.py demo-accept 1",
    "uv run python tools/sdlc.py merge 12",
    "uv run python tools/sdlc.py state 1 --from-file x.json",
    "uv run python tools/sdlc.py state abc",
    "uv run python tools/sdlc_watch.py pause",
    "uv run python tools/sdlc_watch.py status --json",
    "python tools/sdlc.py state 1",
    "bash -c 'uv run twiddle status'",
    "sh -c true",
    "",
]


@pytest.mark.parametrize("cmd", REFUSED)
def test_watch_run_refuses_everything_else(cmd, watched, tmp_path, capsys):
    out = tmp_path / "d" / "shot.svg"
    rc = demo_shot.main(["cli", "--out", str(out), "--", *cmd.split()])
    assert rc == 2 and watched == [] and not out.exists() and not out.parent.exists()
    assert demo_shot.WATCH_ENV in capsys.readouterr().err


def test_watch_run_refuses_any_tui(watched, tmp_path, monkeypatch):
    monkeypatch.setattr("textual.app.App.run", lambda *a, **k: pytest.fail("a TUI started"))
    for argv in (["dial"], ["scene"], ["status"]):
        rc = demo_shot.main(["tui", "--out-dir", str(tmp_path / "d"), "--step", "shot:x", "--", *argv])
        assert rc == 2
    assert watched == [] and not (tmp_path / "d").exists()


def test_watch_run_refuses_another_checkout(watched, tmp_path):
    out = tmp_path / "shot.svg"
    rc = demo_shot.main(["cli", "--out", str(out), "--cwd", str(tmp_path), "--", "uv", "run", "twiddle", "status"])
    assert rc == 2 and watched == [] and not out.exists()


def test_watch_run_refuses_a_direct_call(watched, tmp_path):
    with pytest.raises(demo_shot.Refused):
        demo_shot.shot_cli([sys.executable, "-c", "print(1)"], tmp_path / "s.svg", None, 80, None, 30)
    assert watched == []


ALLOWED = [
    "uv run twiddle status",
    "uv run twiddle status --room roam --json",
    "uv run twiddle rooms",
    "twiddle rooms --json",
    "uv run twiddle alarm list",
    "uv run twiddle alarm list --json",
    "uv run twiddle alarm status --room Kitchen",
    "uv run twiddle np",
    "uv run twiddle np kalx -i --no-art",
    "uv run twiddle np --room=roam",
    "uv run twiddle stations",
    "uv run twiddle stations --tag jazz",
    "uv run twiddle limits",
    "uv run twiddle scene list",
    "uv run twiddle scene list --days 3 --venue Gilman --links",
    "uv run twiddle scene status",
    "uv run twiddle daemon status",
    "uv run python tools/sdlc.py state 263",
    "uv run python tools/sdlc.py next",
    "uv run python tools/sdlc.py ready --epic 263",
    "uv run python tools/sdlc.py slice-status 325 --json",
    "uv run python tools/sdlc.py demo-status 263",
    "uv run python tools/sdlc_watch.py status",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_watch_run_runs_the_allowlist(cmd, watched, tmp_path):
    out = tmp_path / "shot.svg"
    rc = demo_shot.main(["cli", "--out", str(out), "--", *cmd.split()])
    assert rc == 0 and watched == [cmd.split()]
    assert out.read_text().startswith("<svg")


def test_every_allowlist_entry_has_a_test():
    covered = set()
    for cmd in ALLOWED:
        argv = tuple(cmd.split())
        for prefixes, verb in demo_shot.WATCH_ALLOWED:
            if any(argv[:len(p + verb)] == p + verb for p in prefixes):
                covered.add((prefixes, verb))
    assert covered == set(demo_shot.WATCH_ALLOWED)
