"""tools/demo_shot.py: demo pictures are masked and drawn offline (no network, no speaker)."""
import sys

from textual.app import App
from textual.widgets import Static

from tools import demo_shot, sdlc


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
