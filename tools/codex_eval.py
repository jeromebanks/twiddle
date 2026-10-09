"""The review logic's fingerprint, and the eval results recorded against it: stdlib only, beside `codex_review`.

A Codex review behaves as its prompts, its rules, its settings, the code that fills and runs it, and the `codex`
binary decide. The repo owns all but the last, and `fingerprint` hashes what it owns: a sorted, named list of
inputs (`inputs`), read from a worktree or from a commit (`Worktree`, `Commit`), so a change to any of them is a
new fingerprint, and an edit anywhere else (`sdlc.py`, `CLAUDE.md`) is not. `codex --version` is the other half
of the pair an eval result is recorded for.

A result is a marked `codex-eval` comment on the issue `.sdlc/config.json`'s `codex_eval_issue` names. `sdlc.py`
reads and posts them; `latest_results` picks the latest per (fingerprint, version) pair, so a pass for an older
`codex` never counts for a newer one.

    uv run python tools/sdlc.py codex-eval --status     # the fingerprint, `codex --version`, and that pair's result

Nothing here runs `codex exec`.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

try:
    from tools import codex_review          # imported as tools.codex_eval (the tests)
except ModuleNotFoundError:
    import codex_review                     # run beside tools/sdlc.py

ROOT = codex_review.ROOT
RULES = ROOT / ".agents" / "skills" / "review-rules.md"
PLAN_SCHEMA = ROOT / ".agents" / "skills" / "plan-issue" / "references" / "plan-schema.md"
CONFIG = ROOT / ".sdlc" / "config.json"
FIXTURES = ROOT / "tests" / "codex_eval"
AGENTS_DIR = ROOT / ".agents"         # the skills' rules, prompts and schema: what Codex is pointed at

# every file whose text decides how a review behaves, fixed by name: present or not, each is an input
FILES = tuple(p.relative_to(ROOT).as_posix() for p in (
    codex_review.PR_PROMPT, codex_review.PLAN_PROMPT, codex_review.MILESTONE_PROMPT, RULES, PLAN_SCHEMA,
    Path(codex_review.__file__).resolve(), Path(__file__).resolve()))
# of those, what Codex reads from the checkout it reviews in, by the path a prompt gives it
CHECKOUT_FILES = tuple(p.relative_to(ROOT).as_posix() for p in (RULES, PLAN_SCHEMA))
CONFIG_INPUT = "config:codex"          # `.sdlc/config.json`'s `codex` section, as canonical JSON
# repository instruction files Codex loads on its own, wherever they are: inputs whether or not one exists
INSTRUCTION_FILES = codex_review.INSTRUCTION_FILES
# what a prompt names, or the runner reads, that is deliberately not an input, and why
EXCLUDED = {
    "CLAUDE.md": "knowledge of the code under review: the plan prompt names it for its Layout table only, and "
                 "the safety rules a reviewer checks are `review-rules.md`'s",
    ".sdlc/config.json": f"only its `codex` section decides a review, and that is an input (`{CONFIG_INPUT}`)",
    "docs/prd/": "the evidence a plan round is held to (`codex_review.approved_requirements` reads it), not review logic",
}
OUTCOMES = ("pass", "fail", "deferred")
UNKNOWN = "unknown"                    # what `codex_review.codex_version` says when `codex --version` doesn't: no pair
SHORT = 12                             # a full 64-hex hash looks like an identifier to `demo-post` and `demo_shot`


# --- where the inputs are read from --------------------------------------------------

class Tree:
    """A repo's files as one revision has them: `names` (every path) and `read(path)` (its bytes, or None)."""

    def names(self) -> list[str]:
        raise NotImplementedError

    def read(self, path: str) -> bytes | None:
        raise NotImplementedError

    def links(self, names: list[str]) -> list[str]:
        """Which of these listed names are symbolic links."""
        raise NotImplementedError

    def hidden(self) -> codex_review.ReviewError | None:
        """Why something under `GUARDED_DIRS` would reach Codex without git listing it, or None. A commit holds
        nothing git doesn't list."""
        return None


def _git_bytes(args: list[str], cwd: Path) -> bytes:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if proc.returncode != 0:
        raise codex_review.ReviewError(f"git {' '.join(args[:3])} failed in {cwd}: {proc.stderr.decode().strip()}")
    return proc.stdout


class Worktree(Tree):
    """A checkout as it is on disk: what git tracks there, and what it would (untracked, not ignored)."""

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)

    def names(self) -> list[str]:
        out = _git_bytes(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], self.root)
        return sorted({n for n in out.decode().split("\0") if n})

    def links(self, names: list[str]) -> list[str]:
        return [n for n in names if (self.root / n).is_symlink()]

    def hidden(self) -> codex_review.ReviewError | None:
        """The disk, not git, says what is under `GUARDED_DIRS`: a symbolic link there (tracked, untracked or
        ignored, file or folder) or a file git ignores there would reach Codex without being an input."""
        for d in GUARDED_DIRS:
            top = self.root / d.relative_to(ROOT)
            # every directory from the root down to it first: a link above it would move the whole folder elsewhere
            if linked := [a for a in _ancestors(d.relative_to(ROOT).as_posix()) if (self.root / a).is_symlink()]:
                return _linked(linked[0])
            for here, dirs, files in os.walk(top):          # never follows a link: it is listed, and refused
                for name in dirs + files:
                    if (Path(here) / name).is_symlink():
                        return _linked((Path(here) / name).relative_to(self.root).as_posix())
        ignored = _git_bytes(["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--",
                              *(d.relative_to(ROOT).as_posix() for d in GUARDED_DIRS)], self.root)
        if names := [n for n in ignored.decode().split("\0") if n]:
            return codex_review.ReviewError(f"git ignores {names[0]}, and nothing under "
                                            + " or ".join(f"{d.relative_to(ROOT).as_posix()}/" for d in GUARDED_DIRS)
                                            + " may be ignored: Codex would read it, and no commit holds it")
        return None

    def read(self, path: str) -> bytes | None:
        if linked := [a for a in _ancestors(path) if (self.root / a).is_symlink()]:
            raise _linked(linked[0])
        try:
            return (self.root / path).read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return None


class Commit(Tree):
    """One commit's tree, read from the repo at `root` (`git ls-tree`, `git cat-file`): for a gate on a PR head."""

    def __init__(self, sha: str, root: Path = ROOT):
        self.sha, self.root = sha, Path(root)

    def names(self) -> list[str]:
        out = _git_bytes(["ls-tree", "-r", "-z", "--name-only", self.sha], self.root)
        return sorted(n for n in out.decode().split("\0") if n)

    def links(self, names: list[str]) -> list[str]:
        out = _git_bytes(["ls-tree", "-r", "-z", self.sha], self.root)
        return [e.split("\t", 1)[1] for e in out.decode().split("\0") if e.startswith("120000 ")]

    def read(self, path: str) -> bytes | None:
        if self._is_link(path):
            raise _linked(path)
        proc = subprocess.run(["git", "cat-file", "blob", f"{self.sha}:{path}"], cwd=self.root, capture_output=True)
        return proc.stdout if proc.returncode == 0 else None

    def _is_link(self, path: str) -> bool:
        """The path, or a directory on the way to it, is a symbolic link in this commit."""
        proc = subprocess.run(["git", "ls-tree", "-z", self.sha, "--", *_ancestors(path)], cwd=self.root,
                              capture_output=True)
        return any(e.startswith(b"120000 ") for e in proc.stdout.split(b"\0"))


def _ancestors(path: str) -> list[str]:
    """`a/b/c` -> `a`, `a/b`, `a/b/c`: every link on the way to a file counts as one."""
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _linked(path: str) -> codex_review.ReviewError:
    # a link reads as its target on disk but as the link's own text in a commit: two fingerprints for one tree,
    # and a commit's would miss edits to the target Codex loads
    return codex_review.ReviewError(f"{path} is a symbolic link, and the review logic's inputs must be files: "
                                    "put the content there itself")


# --- the fingerprint ------------------------------------------------------------------

def codex_section(tree: Tree) -> Any:
    """`.sdlc/config.json`'s `codex` section as the tree has it; None when there's none to read."""
    raw = tree.read(CONFIG.relative_to(ROOT).as_posix())
    try:
        return json.loads(raw).get("codex") if raw is not None else None
    except (ValueError, AttributeError):
        return None


def is_input(name: str) -> bool:
    """Whether a repo path is one of the fingerprint's inputs."""
    return (name in FILES or name.startswith(FIXTURES.relative_to(ROOT).as_posix() + "/")
            or codex_review.instruction_file(name.rsplit("/", 1)[-1]))


GUARDED_DIRS = (AGENTS_DIR, FIXTURES)     # where every file is a real one in the repo: no link, nothing ignored


def guarded(name: str) -> bool:
    """A path that may never be a symbolic link: anything under `.agents/` or `tests/codex_eval/`, or a file named
    like an instruction file anywhere."""
    return (name.startswith(tuple(d.relative_to(ROOT).as_posix() + "/" for d in GUARDED_DIRS))
            or codex_review.instruction_file(name.rsplit("/", 1)[-1]))


def read_by_codex(name: str) -> bool:
    """Whether Codex itself reads this input from the checkout it reviews in (the rules, the plan schema, the
    instruction files and skills it loads), rather than the tool reading it from the checkout it runs from."""
    return name in CHECKOUT_FILES or not (name in FILES or name.startswith(FIXTURES.relative_to(ROOT).as_posix() + "/"))


def inputs(tree: Tree, checkout: Tree | None = None, section: Any = ...) -> list[tuple[str, bytes | None]]:
    """(name, content) for every input, sorted by name: the fixed files (None when absent), the `codex` section,
    and whatever the globs find (every file under `tests/codex_eval/`, every `AGENTS.md`/`AGENTS.override.md`,
    and nothing else: the repo's skills are never offered to a review, `codex.skills`).

    What the tool reads (the prompts, its code, the fixtures) comes from `tree`, the code running the round;
    what Codex reads (`read_by_codex`) from `checkout`, where it runs (a slice's worktree, a scratch checkout of
    main, the epic's worktree), `tree` itself by default. `section` is the `codex` settings the round runs with
    (a `--config` of its own), by default `tree`'s."""
    checkout = checkout or tree
    section = codex_section(tree) if section is ... else section
    found = {}
    roots = [FIXTURES]
    for src, mine in ((tree, False), (checkout, True)):
        names = src.names()
        # git lists a link (or a file) where a directory of inputs should be as one name: its contents would vanish
        if stand_in := sorted({a for r in roots for a in _ancestors(r.relative_to(ROOT).as_posix())} & set(names)):
            raise codex_review.ReviewError(f"{stand_in[0]} isn't a directory (a symbolic link?), and the review "
                                           "logic's inputs under it must be files in the repo")
        # no link where review logic lives, whatever the mode: a linked folder is one name to git, so its
        # contents would reach Codex without ever being read here
        if linked := sorted(n for n in src.links(names) if guarded(n)):
            raise _linked(linked[0])
        if hidden := src.hidden():
            raise hidden
        for n in names:
            if n not in FILES and is_input(n) and read_by_codex(n) == mine and (data := src.read(n)) is not None:
                found[n] = data
    found.update({n: (checkout if read_by_codex(n) else tree).read(n) for n in FILES})
    canonical = json.dumps(section, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    found[CONFIG_INPUT] = canonical.encode() if section is not None else None
    return sorted(found.items())


def fingerprint(tree: Tree, checkout: Tree | None = None, section: Any = ...) -> str:
    """sha256 over the inputs (`inputs`), each name and content length-prefixed, an absent one marked as such."""
    h = hashlib.sha256()
    for name, data in inputs(tree, checkout, section):
        n = name.encode()
        h.update(b"%d:%s" % (len(n), n))
        h.update(b"-" if data is None else b"%d:%s" % (len(data), data))
    return h.hexdigest()


def short(fp: str) -> str:
    return fp[:SHORT]


# --- the results ---------------------------------------------------------------------

def latest_results(results: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """The latest result per (fingerprint, codex version), from results oldest first (each {fingerprint, codex,
    outcome, ...}); a result with no fingerprint, version or known outcome is no result."""
    latest = {}
    for r in results:
        if r.get("fingerprint") and r.get("codex") not in (None, "", UNKNOWN) and r.get("outcome") in OUTCOMES:
            latest[(r["fingerprint"], r["codex"])] = r
    return latest


def pair_result(results: list[dict[str, Any]], fp: str, version: str | None) -> dict[str, Any] | None:
    """The latest result recorded for exactly this fingerprint and this `codex` version, or None. An unknown
    version pairs with nothing: a result recorded under one would count for every `codex` that won't say."""
    if not version or version == UNKNOWN:
        return None
    return latest_results(results).get((fp, version))


def current_version() -> str | None:
    """`codex --version`, asked on an empty scratch `CODEX_HOME` (nothing of the user's is read); None when
    there is no `codex` to ask. It never runs `codex exec`."""
    with tempfile.TemporaryDirectory(prefix="codex-eval-") as home:
        try:
            return codex_review.codex_version({**os.environ, "CODEX_HOME": home})
        except codex_review.CannotStart:
            return None


def describe(result: dict[str, Any] | None) -> str:
    """One line for a pair's state."""
    if result is None:
        return "no result recorded for this pair: the review logic or `codex` changed since the last eval"
    when = f" ({result['url']})" if result.get("url") else ""
    return {"pass": "passed", "fail": "FAILED", "deferred": "deferred (Codex couldn't run it)"}[result["outcome"]] + when


def warning(fp: str, version: str, result: dict[str, Any] | None) -> str | None:
    """What `codex-review` says before a round when this pair has no passing eval; None when it has one."""
    if result and result.get("outcome") == "pass":
        return None
    return (f"warning: the review logic {short(fp)} on codex {version} has no passing eval ({describe(result)}). "
            "The review still runs; `uv run python tools/sdlc.py codex-eval --status` says more")
