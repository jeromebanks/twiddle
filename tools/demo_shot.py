#!/usr/bin/env python3
"""Pictures for a milestone demo: real terminal output and TUI screens, drawn as SVG.

    uv run python tools/demo_shot.py cli --out D/list.svg -- uv run twiddle alarm list
    uv run python tools/demo_shot.py tui --out-dir D --step wait:4 --step shot:dial --step down --step shot:next -- dial

`cli` runs a command and draws what it printed in a terminal frame (rich). `tui`
runs a twiddle command whose app is Textual, headless: every app's `run` is
swapped for Textual's test pilot, which presses the `--step` keys and saves a
screen at each `shot:<name>`. Steps: a key name (`down`, `enter`, `v`),
`wait:<seconds>`, `type:<text>` and `shot:<name>`.

Anything that looks like a device or household ID, an address or a secret is
masked before it is drawn: the same list `sdlc.py demo-post` refuses to
publish, because the demo branch is public. Only read-only keys belong in
`--step`. A key that writes to a speaker is a demo step the user confirms, not
a screenshot.

The "before" half of a bug fix's demo runs from a worktree at the old commit:
`cd <worktree> && uv run python <this file> ...` imports that checkout's twiddle.

Inside an unattended watcher run (`TWIDDLE_SDLC_WATCH_RUN` set, to anything,
even empty) nobody is there to stop a bad command, so `cli` runs only the
read-only commands in `WATCH_ALLOWED`, matched on the argv token by token, each
with only the flags it names; `tui` and `--cwd` (another checkout is other code
under the same argv) are refused. A refusal runs nothing, draws nothing and
exits 2. Unset, nothing changes.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdlc  # noqa: E402  (the identifier list lives with the publishing gate)

MASK = "[hidden]"

WATCH_ENV = "TWIDDLE_SDLC_WATCH_RUN"

ISSUE = re.compile(r"\d+")
NAME = re.compile(r"[^-].*")         # any value that can't be read as another flag
KEY = re.compile(r"[a-z0-9][a-z0-9_-]*")

# The read-only commands an unattended run may picture: (prefix, the leaf verb's
# tokens) -> (flags, each with the pattern of its value or None for a switch;
# the positionals allowed, in order). Nothing may sit between the tokens, so a
# parent's own options (`scene --dry-run list`, `uv run --with ...`) are refused.
# Left out on purpose: `snapshot` (it overwrites logs/snapshots/<room>.json,
# which `restore` reads), `np -d` (it writes once you pick a track),
# `scene list --refresh` (a build), `stations probe/search` (not needed).
TWIDDLE = (("uv", "run", "twiddle"), ("twiddle",))
SDLC = (("uv", "run", "python", "tools/sdlc.py"),)
WATCH = (("uv", "run", "python", "tools/sdlc_watch.py"),)
SPEAKER = {"--json": None, "--room": NAME, "--anchor": NAME}
WATCH_ALLOWED: dict[tuple[tuple[tuple[str, ...], ...], tuple[str, ...]], tuple[dict[str, Any], tuple[re.Pattern, ...]]] = {
    (TWIDDLE, ("status",)): (SPEAKER, ()),
    (TWIDDLE, ("rooms",)): (SPEAKER, ()),
    (TWIDDLE, ("alarm", "list")): ({"--json": None, "--anchor": NAME}, ()),
    (TWIDDLE, ("alarm", "status")): (SPEAKER, ()),
    (TWIDDLE, ("np",)): ({**SPEAKER, "-i": None, "--info": None, "--no-art": None}, (KEY,)),
    (TWIDDLE, ("stations",)): ({"--json": None, "--tag": NAME}, ()),
    (TWIDDLE, ("limits",)): ({"--json": None}, ()),
    (TWIDDLE, ("scene", "list")): ({"--json": None, "--venue": NAME, "--days": ISSUE, "--all-venues": None,
                                    "--links": None}, ()),
    (TWIDDLE, ("scene", "status")): ({"--json": None}, ()),
    (TWIDDLE, ("daemon", "status")): ({}, ()),
    (SDLC, ("state",)): ({"--json": None}, (ISSUE,)),
    (SDLC, ("next",)): ({"--json": None}, ()),
    (SDLC, ("ready",)): ({"--json": None, "--epic": ISSUE}, ()),
    (SDLC, ("slice-status",)): ({"--json": None}, (ISSUE,)),
    (SDLC, ("demo-status",)): ({"--json": None}, (ISSUE,)),
    (WATCH, ("status",)): ({}, ()),
}


class Refused(Exception):
    """A command an unattended watcher run may not run: nothing was run or drawn."""


def watch_run() -> bool:
    return WATCH_ENV in os.environ


def _args_ok(rest: list[str], flags: dict[str, Any], positionals: tuple[re.Pattern, ...]) -> str | None:
    """Why `rest` (what follows the leaf verb) isn't allowed, or None."""
    seen = 0
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("-"):
            name, eq, value = tok.partition("=")
            if name not in flags:
                return f"flag {tok!r} is not allowed here"
            pattern = flags[name]
            if pattern is None:
                if eq:
                    return f"flag {name!r} takes no value"
            else:
                if not eq:
                    i += 1
                    if i >= len(rest):
                        return f"flag {name!r} needs a value"
                    value = rest[i]
                if not pattern.fullmatch(value):
                    return f"value {value!r} for {name!r} is not allowed"
        else:
            if seen >= len(positionals):
                return f"argument {tok!r} is not allowed here"
            if not positionals[seen].fullmatch(tok):
                return f"argument {tok!r} is not allowed"
            seen += 1
        i += 1
    return None


def check_watch_cli(cmd: list[str]) -> None:
    """Raise Refused unless `cmd` is on the watcher run's read-only allowlist."""
    shown = " ".join(cmd)
    why = "not on the read-only allowlist"
    for (prefixes, verb), (flags, positionals) in WATCH_ALLOWED.items():
        for prefix in prefixes:
            head = prefix + verb
            if tuple(cmd[:len(head)]) != head:
                continue
            reason = _args_ok(cmd[len(head):], flags, positionals)
            if reason is None:
                return
            why = reason
    raise Refused(f"{WATCH_ENV} is set: refusing {shown!r} ({why}); nothing was run")


def mask(text: str) -> tuple[str, int]:
    """`text` with every identifier replaced by MASK, and how many there were."""
    count = 0
    for _, rx in sdlc.IDENTIFIERS:
        text, k = rx.subn(MASK, text)
        count += k
    return text, count


def render_terminal(text: str, title: str, width: int) -> str:
    """ANSI text -> an SVG of a terminal window showing it."""
    from rich.console import Console
    from rich.text import Text

    console = Console(record=True, width=width, file=io.StringIO(), force_terminal=True, color_system="truecolor")
    console.print(Text.from_ansi(text), soft_wrap=False, overflow="fold")
    return console.export_svg(title=title)


def shot_cli(cmd: list[str], out: Path, title: str | None, width: int, cwd: str | None, timeout: float) -> int:
    if watch_run():
        if cwd is not None:
            raise Refused(f"{WATCH_ENV} is set: refusing --cwd (another checkout runs other code); nothing was run")
        check_watch_cli(cmd)
    env = {**os.environ, "COLUMNS": str(width), "FORCE_COLOR": "1", "TERM": "xterm-256color"}
    proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          timeout=timeout)
    shown = " ".join(c for c in cmd if c not in ("uv", "run")) if title is None else title
    text, hidden = mask(f"$ {shown}\n{proc.stdout.rstrip()}")
    svg = render_terminal(text, shown, width)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg)
    print(f"{out}  (exit {proc.returncode}, {hidden} identifier(s) hidden)")
    return proc.returncode


def drive(app: Any, steps: list[str], out_dir: Path, size: tuple[int, int]) -> list[Path]:
    """Run a Textual app headless through `steps`, saving a masked SVG at each `shot:`."""
    saved: list[Path] = []

    async def go() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            for step in steps:
                kind, _, arg = step.partition(":")
                if kind == "wait" and arg:
                    await pilot.pause(float(arg))
                elif kind == "type" and arg:
                    await pilot.press(*list(arg))
                elif kind == "shot" and arg:
                    await pilot.pause()
                    svg, _ = mask(app.export_screenshot(title=arg))
                    path = out_dir / f"{arg}.svg"
                    path.write_text(svg)
                    saved.append(path)
                else:
                    await pilot.press(step)

    out_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(go())
    return saved


def shot_tui(argv: list[str], steps: list[str], out_dir: Path, size: tuple[int, int]) -> int:
    if watch_run():
        raise Refused(f"{WATCH_ENV} is set: refusing a TUI (its keys can write); nothing was run")
    from textual.app import App

    saved: list[Path] = []

    def run(self: Any, *a: Any, **k: Any) -> None:
        saved.extend(drive(self, steps, out_dir, size))

    App.run = run   # whichever app the command builds is driven by the pilot instead
    from twiddle.cli import main as twiddle_main

    twiddle_main(argv)
    for p in saved:
        print(p)
    if not saved:
        print("no screens saved: the command never started a Textual app, or no `shot:` step", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--" not in argv:
        print("usage: demo_shot.py {cli,tui} [options] -- <command...>", file=sys.stderr)
        return 2
    split = argv.index("--")
    ap = argparse.ArgumentParser(prog="demo_shot.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("cli", help="a command's output as a terminal picture")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--title", help="the window title (default: the command)")
    p.add_argument("--width", type=int, default=100)
    p.add_argument("--cwd", help="run it here (e.g. a worktree at the old commit)")
    p.add_argument("--timeout", type=float, default=120)
    p = sub.add_parser("tui", help="screens of a twiddle TUI, driven by keys")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--step", action="append", default=[], help="key | wait:<s> | type:<text> | shot:<name>")
    p.add_argument("--size", default="120x40", help="columns x rows")
    args = ap.parse_args(argv[:split])
    rest = argv[split + 1:]
    try:
        if args.mode == "cli":
            shot_cli(rest, args.out, args.title, args.width, args.cwd, args.timeout)
            return 0   # a failing command is still worth showing; its exit code is printed
        cols, rows = (int(x) for x in args.size.lower().split("x"))
        return shot_tui(rest, args.step, args.out_dir, (cols, rows))
    except Refused as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
