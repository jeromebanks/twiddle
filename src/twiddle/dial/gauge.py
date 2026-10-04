"""The volume bar, drawn by `dial` and `scene` alike."""
from __future__ import annotations

from rich.style import Style
from rich.text import Text


def gauge(volume: int | None, muted: bool, color: str = "cyan", width: int = 20) -> Text:
    """The volume bar. It is also the mouse control: the icon toggles mute,
    and each of the bar's `width + 1` cells sets the volume it stands for."""
    if volume is None:
        return Text("♪ ?", style="dim")
    filled = round(volume / 100 * width)
    t = Text()
    t.append("✕ muted " if muted else "♪ ",
             style=Style.parse("bold red" if muted else "bold")
             + Style(meta={"@click": "app.mute"}))
    t.append(f"{volume:>3} ", style="dim" if muted else "bold")
    for cell in range(width + 1):
        char, style = (("━", "dim" if muted else f"bold {color}") if cell < filled
                       else ("●", "dim" if muted else "bold") if cell == filled
                       else ("─", "dim"))
        t.append(char, style=Style.parse(style) + Style(
            meta={"@click": f"app.set_volume({round(cell / width * 100)})"}))
    return t
