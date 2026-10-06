"""Only named read-only views read the alarm list tolerantly.

A tolerant read (`clock.list_alarms(ip, tolerant=True)`) leaves out every
alarm twiddle can't parse. That's right for a view, which says what it left
out, and wrong for anything that writes, or compares lists to decide a write:
an alarm missing from the comparison could be changed, or destroyed, unseen.

So every `tolerant=` under src/twiddle/ is found by parsing the files (no
imports: Textual and numpy aren't loaded), and each call is attributed to its
innermost enclosing function by qualified name (`Class.method`,
`outer.inner`), so a handler nested in a view, or a method beside it, never
inherits the view's exemption.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "twiddle"

# Every (module, function) allowed to read the alarm list tolerantly, and why.
# A new view (the alarm TUI's list, say) is added here on purpose, with its
# reason; the scan fails until it is. Anything that writes must read strictly
# (the default `tolerant=False`, which refuses on an unreadable alarm before any
# write): the TUI's editor, toggle, delete and try too, and the scan fails if
# one of them doesn't.
ALLOWLIST = {
    ("alarm_cli", "cmd_list"):
        "`alarm list`: shows every readable alarm and names the unreadable",
    ("alarm_cli", "cmd_status"):
        "`alarm status`: only names an alarm already going off; writes nothing",
    ("monitor", "Monitor._check_alarms"):
        "the watcher: records what it saw, never writes to a speaker",
    ("alarms.sources.spotify", "_household_account"):
        "only finds the linked Spotify account serial; `alarm add`/`edit` then do "
        "their own strict read, which refuses on an unreadable alarm before any write",
}

# The one place a non-literal `tolerant=` may appear: list_alarms forwarding
# its own keyword to the parser.
FORWARDER = ("alarms.clock", "list_alarms", "tolerant")

MODULE = "<module>"
READERS = {"list_alarms", "parse_list_alarms"}


def module_name(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


class _Reads(ast.NodeVisitor):
    """Every call with a `tolerant=` keyword: (qualname, value node)."""

    def __init__(self):
        self.names: list[str] = []
        self.found: list[tuple[str, ast.expr]] = []

    def _scope(self, node):
        self.names.append(getattr(node, "name", "<lambda>"))
        self.generic_visit(node)
        self.names.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = visit_Lambda = _scope

    def visit_Call(self, node):
        where = ".".join(self.names) or MODULE
        callee = getattr(node.func, "attr", getattr(node.func, "id", None))
        for k in node.keywords:
            if k.arg == "tolerant":
                self.found.append((where, k.value))
            elif k.arg is None:                     # **spread: never a named view's read
                d = k.value
                keys = [getattr(x, "value", None) for x in d.keys] if isinstance(d, ast.Dict) else []
                if "tolerant" in keys or callee in READERS:
                    self.found.append((where, d))
        self.generic_visit(node)


def tolerant_reads(source: str, module: str) -> list[tuple[str, str, ast.expr]]:
    """(module, qualname, value) for every `tolerant=` that isn't `False`."""
    v = _Reads()
    v.visit(ast.parse(source))
    return [(module, q, val) for q, val in v.found
            if not (isinstance(val, ast.Constant) and val.value is False)]


def violations(source: str, module: str) -> list[str]:
    """What's wrong with this module's tolerant reads; empty when nothing is."""
    bad = []
    for mod, q, val in tolerant_reads(source, module):
        if isinstance(val, ast.Constant):
            if (mod, q) not in ALLOWLIST:
                bad.append(f"{mod}.{q}: tolerant={val.value!r} off the allowlist")
        elif (mod, q, getattr(val, "id", None)) != FORWARDER:
            bad.append(f"{mod}.{q}: tolerant={ast.unparse(val)} (not a literal)")
    return bad


def _sources():
    return [(module_name(p), p.read_text()) for p in sorted(SRC.rglob("*.py"))]


def test_only_named_views_read_the_list_tolerantly():
    files = _sources()
    assert len(files) > 50, "the scan found too few files to mean anything"
    assert [v for mod, src in files for v in violations(src, mod)] == []
    readers = {(m, q) for mod, src in files for m, q, val in tolerant_reads(src, mod)
               if isinstance(val, ast.Constant)}
    # Equal, not a subset: a reader that's gone takes its entry with it.
    assert readers == set(ALLOWLIST)


def test_tolerant_is_keyword_only_so_the_scan_sees_it():
    tree = ast.parse((SRC / "alarms" / "clock.py").read_text())
    fns = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    for name in ("list_alarms", "parse_list_alarms"):
        assert "tolerant" in [a.arg for a in fns[name].args.kwonlyargs], name


# ---- the checker itself, on inline source ------------------------------------

@pytest.mark.parametrize("module, source", [
    ("alarm_tui", "def show(ip):\n    clock.list_alarms(ip, tolerant=True)\n"),
    ("alarm_tui", "def cmd_list(ip):\n    clock.list_alarms(ip, tolerant=True)\n"),
    ("alarm_cli", "async def on_toggle(ip):\n    await list_alarms(ip, tolerant=True)\n"),
    ("alarm_cli", "found = clock.list_alarms(IP, tolerant=True)\n"),
    ("alarm_cli", "class App:\n    def cmd_list(self):\n"
                  "        clock.list_alarms(self.ip, tolerant=True)\n"),
    ("alarm_cli", "def cmd_list(ip):\n    def delete():\n"
                  "        clock.list_alarms(ip, tolerant=True)\n    delete()\n"),
    ("alarm_cli", "def cmd_list(ip):\n    async def on_save():\n"
                  "        clock.list_alarms(ip, tolerant=True)\n"),
    ("monitor", "def _check_alarms(self):\n    clock.list_alarms(self.anchor, tolerant=True)\n"),
    ("alarm_cli", "def cmd_edit(ip):\n    clock.parse_list_alarms(xml, tolerant=1)\n"),
    ("alarm_cli", "def cmd_edit(ip, flag):\n    clock.list_alarms(ip, tolerant=flag)\n"),
    ("alarm_cli", "def cmd_list(ip, flag):\n    clock.list_alarms(ip, tolerant=flag)\n"),
    ("alarms.clock", "def snapshot(ip, tolerant):\n"
                     "    return parse_list_alarms(_read(ip), tolerant=tolerant)\n"),
    ("alarms.clock", "def list_alarms(ip, *, flag):\n"
                     "    return parse_list_alarms(_read(ip), tolerant=flag)\n"),
    ("alarm_cli", "def cmd_list(ip):\n    f = lambda: clock.list_alarms(ip, tolerant=True)\n"),
    ("alarm_cli", "def cmd_list(ip):\n    clock.list_alarms(ip, **{'tolerant': True})\n"),
    ("alarm_cli", "def cmd_list(ip, opts):\n    clock.list_alarms(ip, **opts)\n"),
], ids=["new module", "allowlisted name, other module", "async def", "module level",
        "class method", "nested in a view", "async nested in a view", "function, not the method",
        "the parser, truthy", "a flag", "a flag in a view", "forwarding, other clock function",
        "list_alarms, another name",
        "a lambda in a view", "a spread dict", "a spread into the reader"])
def test_the_checker_fails(module, source):
    assert violations(source, module)


@pytest.mark.parametrize("module, source", [
    ("alarm_tui", "def save(ip):\n    clock.list_alarms(ip, tolerant=False)\n"),
    ("alarm_tui", "def save(ip):\n    clock.list_alarms(ip)\n"),
    ("alarm_cli", "def cmd_list(ip):\n    clock.list_alarms(ip, tolerant=True)\n"),
    ("monitor", "class Monitor:\n    def _check_alarms(self):\n"
                "        clock.list_alarms(self.anchor, tolerant=True)\n"),
    ("alarms.clock", "def list_alarms(ip, *, tolerant=False):\n"
                     "    return parse_list_alarms(_read(ip), tolerant=tolerant)\n"),
], ids=["tolerant=False", "the default", "a view", "the watcher's method", "the forwarder"])
def test_the_checker_passes(module, source):
    assert violations(source, module) == []
