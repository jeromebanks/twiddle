"""The three scene packages keep their dependencies pointing one way.

    scene (client)  ->  scenespec  <-  scenedata (producer)

A client has only the dataset a producer published; it must run without any
producer code. These hold the line, like `test_viz_contract.py` does for
visualizers.
"""
import ast
import subprocess
import sys
from pathlib import Path

import twiddle

SRC = Path(twiddle.__file__).parent


def _imports(path: Path):
    """(module, imported at module level?) for every import in a file, with
    relative imports resolved against the file's own package."""
    tree = ast.parse(path.read_text())
    pkg = ["twiddle"] + list(path.relative_to(SRC).parent.parts)
    top = set(id(n) for n in tree.body)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, id(node) in top
        elif isinstance(node, ast.ImportFrom):
            base = pkg[:len(pkg) - (node.level - 1)] if node.level else []
            mod = ".".join(base + ([node.module] if node.module else []))
            yield mod, id(node) in top
            for a in node.names:                    # `from .. import scenedata`
                yield f"{mod}.{a.name}", id(node) in top


def _files(package: str):
    return sorted((SRC / package).rglob("*.py"))


def _under(mod: str, packages: tuple[str, ...]) -> bool:
    """`twiddle.scene` and what is inside it, but not `twiddle.scenedata`."""
    return any(mod == p or mod.startswith(p + ".") for p in packages)


def _hits(package: str, forbidden: tuple[str, ...], *, lazy_ok: tuple[str, ...] = ()):
    out = []
    for f in _files(package):
        for mod, at_top in _imports(f):
            if _under(mod, forbidden) and not (f.name in lazy_ok and not at_top):
                out.append(f"{f.relative_to(SRC)} imports {mod}")
    return out


def test_the_producer_never_imports_the_client():
    assert _hits("scenedata", ("twiddle.scene",)) == []


def test_the_contract_imports_neither_side():
    assert _hits("scenespec", ("twiddle.scene", "twiddle.scenedata")) == []


def test_the_client_imports_no_producer_code_except_to_run_a_build():
    """`scene build` / `scene list --refresh` start the producer, lazily, in cli.py."""
    assert _hits("scene", ("twiddle.scenedata",), lazy_ok=("cli.py",)) == []


def test_the_client_runs_without_loading_the_producer():
    code = ("import sys, twiddle.scene.app, twiddle.scene.cli, twiddle.scene.book\n"
            "print(sorted(m for m in sys.modules if m.startswith('twiddle.scenedata')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "[]"
