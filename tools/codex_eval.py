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
    uv run python tools/sdlc.py codex-eval              # run the fixtures, score them, post one result (once per pair)

The eval (`load_fixtures`, `score`, `run_fixtures`, `overall`) shows Codex the changes under `tests/codex_eval/`, each
with one planted bug (and one clean one), through the same runner as a real review, and checks it found them. Only
`run_fixtures` runs `codex exec`, and only on a scratch git repo it builds; `--status` and `--dry-run` never do.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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


# --- the eval: fixtures, scoring, the run ------------------------------------------------

EVAL_EPIC = 1                          # the epic the scratch repo's `origin/epic/1` stands for in the PR prompt
LINE_TOLERANCE = 3                     # a finding's line may be this far from the planted one
BASE_FILES = ("base", "change.patch", "brief.md", "expect.toml")     # what a fixture directory holds
KINDS = ("bug", "clean")
# a neutral identity: Codex reads `git log`, so nothing in the repo may say what is being measured
SCRATCH_GIT = ("-c", "user.name=dev", "-c", "user.email=dev@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false")


@dataclass
class Fixture:
    """One change to review: `base/` (the tree before), `change.patch`, the `brief` Codex is given, and what it
    must find (`expect.toml`: `kind` `bug` with `file`, `line`, `anchor`, `functions` and `keywords`; or `clean`)."""
    name: str
    path: Path
    brief: str
    kind: str
    file: str = ""
    line: int = 0
    anchor: str = ""
    also_lines: list[int] = field(default_factory=list)    # other places a finding may rightly cite (each ±LINE_TOLERANCE)
    functions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


def load_fixtures(root: Path = FIXTURES) -> list[Fixture]:
    """Every fixture under `root`, by name; a malformed one is refused (naming it), so a typo never skips a bug."""
    found = []
    for d in sorted(p for p in Path(root).iterdir() if p.is_dir()) if Path(root).is_dir() else []:
        if missing := [f for f in BASE_FILES if not (d / f).exists()]:
            raise codex_review.ReviewError(f"fixture {d.name} lacks {', '.join(missing)}")
        try:
            exp = tomllib.loads((d / "expect.toml").read_text())
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise codex_review.ReviewError(f"fixture {d.name}: expect.toml can't be read ({exc})") from exc
        if exp.get("kind") not in KINDS:
            raise codex_review.ReviewError(f"fixture {d.name}: `kind` must be one of {', '.join(KINDS)}")
        fx = Fixture(d.name, d, (d / "brief.md").read_text(), exp["kind"])
        if fx.kind == "bug":
            fx.file, fx.anchor = exp.get("file", ""), exp.get("anchor", "")
            fx.line = exp.get("line") if isinstance(exp.get("line"), int) and not isinstance(exp.get("line"), bool) else 0
            fx.functions, fx.keywords = exp.get("functions", []), exp.get("keywords", [])
            fx.also_lines = exp.get("also_lines", [])
            if not (isinstance(fx.also_lines, list) and all(isinstance(n, int) and not isinstance(n, bool) and n > 0
                                                            for n in fx.also_lines)):
                raise codex_review.ReviewError(f"fixture {d.name}: `also_lines` must be a list of line numbers")
            ok = (isinstance(fx.file, str) and fx.file and fx.line > 0 and isinstance(fx.anchor, str) and fx.anchor
                  and isinstance(fx.functions, list) and isinstance(fx.keywords, list) and fx.keywords
                  and all(isinstance(x, str) and x for x in fx.functions + fx.keywords))
            if not ok:
                raise codex_review.ReviewError(f"fixture {d.name}: a bug needs `file`, `line`, `anchor`, `keywords` "
                                               "(non-empty strings) and `functions` (a list of strings)")
            # a keyword the location contains would match any finding that names the place, whatever its defect
            place = " ".join([fx.file, *fx.functions]).lower()
            if inside := [k for k in fx.keywords if k.lower() in place]:
                raise codex_review.ReviewError(f"fixture {d.name}: keyword {inside[0]!r} is part of the file or function "
                                               "names, so it would match any finding there: pick words for the defect")
        found.append(fx)
    if not any(f.kind == "bug" for f in found) or not any(f.kind == "clean" for f in found):
        raise codex_review.ReviewError(f"{root} must hold at least one fixture with a planted bug and one clean change")
    return found


# a numbered finding opens a line: `1.`, `1)`, `**1.**`, `### 1.`, `- 1.`
FINDING_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?(?:#+[ \t]*)?[*_`]*\d+[.)][*_`]*[ \t]", re.MULTILINE)
RANGE = r"L?(\d+)(?:\s*(?:-|\u2013|\u2014|to)\s*L?(\d+))?"


def findings(report: str) -> list[str]:
    """The numbered findings of a report, each with its continuation lines; the `VERDICT:` line is not one."""
    body = report or ""
    starts = [m.start() for m in FINDING_RE.finditer(body)]
    out = [body[a:b].strip() for a, b in zip(starts, starts[1:] + [len(body)])]
    return [re.sub(r"\n[ \t]*[*_`]*VERDICT:.*\Z", "", t, flags=re.IGNORECASE).strip() for t in out]


def lines_named(text: str, path: str) -> list[tuple[int, int]]:
    """(first, last) for every line or line range the text gives: `base.py:12`, `base.py#L12-L14`, `line 12`,
    `lines 12-14`, `L12`."""
    base = re.escape(path.rsplit("/", 1)[-1])
    found = []
    for pat in (rf"{base}[:#(]\s*{RANGE}", rf"\blines?\s+{RANGE}", rf"\bL(\d+)(?:\s*(?:-|\u2013|\u2014)\s*L?(\d+))?\b"):
        for m in re.finditer(pat, text, re.IGNORECASE):
            a = int(m.group(1))
            found.append((a, int(m.group(2)) if m.group(2) else a))
    return found


def names_file(text: str, path: str) -> bool:
    """The text names the file by its repo path or its name (every fixture's file names are unique)."""
    return bool(re.search(rf"(?<![\w.-]){re.escape(path.rsplit('/', 1)[-1])}(?![\w])", text))


@dataclass
class Score:
    hit: bool
    reason: str


def score(fx: Fixture, report: str) -> Score:
    """Whether a report (the saved one, or just Codex's text) is what the fixture needs. A bug: `VERDICT: changes`
    and one finding that names the file, a line within `LINE_TOLERANCE` of the planted one or the enclosing
    function, and one of the keywords (case-insensitive). The clean change: `VERDICT: approve`."""
    try:
        verdict = codex_review.parse_verdict(report)
    except codex_review.ReviewError as exc:
        return Score(False, f"no verdict: {exc}")
    items = findings(report)
    if fx.kind == "clean":
        if verdict == "approve":
            return Score(True, "approved the clean change")
        first = re.sub(r"\s+", " ", items[0])[:160] if items else "no numbered finding"
        return Score(False, f"flagged the clean change (VERDICT: changes): {first}")
    if verdict != "changes":
        return Score(False, "approved the change with the planted bug (VERDICT: approve)")
    if not items:
        return Score(False, "VERDICT: changes with no numbered finding")
    stage, near = 0, ""
    for text in items:
        if not names_file(text, fx.file):
            continue
        stage = max(stage, 1)
        close = any(a - LINE_TOLERANCE <= n <= b + LINE_TOLERANCE for a, b in lines_named(text, fx.file)
                    for n in [fx.line, *fx.also_lines])
        in_function = any(re.search(rf"\b{re.escape(f)}\b", text) for f in fx.functions)
        if not (close or in_function):
            continue
        stage = max(stage, 2)
        if any(k.lower() in text.lower() for k in fx.keywords):
            return Score(True, f"found at {fx.file}")
        near = re.sub(r"\s+", " ", text)[:120]
    if stage == 0:
        return Score(False, f"no finding names {fx.file}")
    if stage == 1:
        where = f"line {fx.line}±{LINE_TOLERANCE}" + (f" or {', '.join(fx.functions)}" if fx.functions else "")
        return Score(False, f"no finding at {fx.file} names {where}")
    return Score(False, f"the finding at the right place doesn't name the defect (none of its keywords): {near}")


@dataclass
class Outcome:
    """One fixture's result: `hit`, `miss` or `unavailable` (Codex couldn't run it)."""
    name: str
    outcome: str
    detail: str = ""


@dataclass
class EvalRun:
    version: str | None = None             # `codex --version`, as the first run that started saw it
    outcomes: list[Outcome] = field(default_factory=list)


def _scratch_git(args: list[str], cwd: Path) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    proc = subprocess.run(["git", *SCRATCH_GIT, *args], cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise codex_review.ReviewError(f"git {' '.join(args[:2])} failed in {cwd}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def build_repo(fx: Fixture, repo: Path, root: Path = ROOT) -> str:
    """A fresh git repo at `repo` holding the fixture: its base tree, the real `review-rules.md` and every
    `AGENTS.md` / `AGENTS.override.md` the repo has (at the same paths) as the base commit, which is also
    `origin/epic/1`; then `slice/1` with the patch applied. Returns the head. Never touches `root`. No skills are
    copied: `codex.skills` is `suppressed`, so a review is never offered any."""
    if repo.resolve().is_relative_to(Path(root).resolve()):
        raise codex_review.ReviewError(f"the scratch repo {repo} is inside {root}: a fixture is only ever applied elsewhere")
    shutil.rmtree(repo, ignore_errors=True)
    shutil.copytree(fx.path / "base", repo, symlinks=True)
    tree = Worktree(root)
    extra = {RULES.relative_to(ROOT).as_posix(): tree.read(RULES.relative_to(ROOT).as_posix())}
    extra.update({n: tree.read(n) for n in tree.names() if codex_review.instruction_file(n.rsplit("/", 1)[-1])})
    for name, data in extra.items():
        if data is None:
            raise codex_review.ReviewError(f"{name} is missing: a fixture is reviewed under the real rules")
        if (repo / name).exists():
            raise codex_review.ReviewError(f"fixture {fx.name} holds {name}, which is copied in from the repo at run time")
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(data)
    _scratch_git(["init", "-q", "-b", "main"], repo)
    _scratch_git(["add", "-A"], repo)
    _scratch_git(["commit", "-qm", "base"], repo)
    _scratch_git(["update-ref", f"refs/remotes/origin/epic/{EVAL_EPIC}", "HEAD"], repo)
    _scratch_git(["checkout", "-q", "-b", "slice/1"], repo)
    _scratch_git(["apply", str((fx.path / "change.patch").resolve())], repo)
    _scratch_git(["add", "-A"], repo)
    _scratch_git(["commit", "-qm", "the change"], repo)
    return codex_review.git_head(repo)


def run_fixtures(fixtures: list[Fixture], settings: dict[str, Any], out: Path, log: Callable[[str], Any] = print,
                 root: Path = ROOT) -> EvalRun:
    """Review each fixture, one at a time (the sign-in copy-back assumes one run per `CODEX_HOME`), each in its own
    `<out>/<name>/` with its own scratch repo and `codex-home`, through `codex_review.run_review` with the PR
    prompt. Nothing Codex is shown names the fixture or the eval (`<out>/f1/` ... , the brief alone, a neutral git
    identity): the name only reaches the log lines this prints. A fixture Codex can't run is `unavailable` and the rest still run; a refusal (`ReviewError`) stops the lot."""
    run = EvalRun()
    template = codex_review.PR_PROMPT.read_text()
    for i, fx in enumerate(fixtures, 1):
        # the folder, its path in the prompt and Codex's working directory carry no fixture name: Codex would read it
        began, scratch = time.monotonic(), Path(out) / f"f{i}"
        scratch.mkdir(parents=True, exist_ok=True)
        head = build_repo(fx, scratch / "repo", root)
        brief = scratch / "slice.md"
        brief.write_text(fx.brief)
        prompt = codex_review.fill_pr_prompt(template, brief, f"origin/epic/{EVAL_EPIC}", EVAL_EPIC, None)
        (scratch / "prompt.md").write_text(prompt)

        def saw(version: str) -> None:
            run.version = run.version or version
        status, report = codex_review.run_review(codex_review.codex_command(settings, prompt), scratch / "repo", head,
                                                 scratch, settings, log=log, on_version=saw)
        if report is None:
            outcome = Outcome(fx.name, "unavailable", f"Codex couldn't run it (exit {status}; {scratch / 'codex.err'})")
        else:
            s = score(fx, report.read_text())
            outcome = Outcome(fx.name, "hit" if s.hit else "miss", s.reason)
        run.outcomes.append(outcome)
        log(f"[{i}/{len(fixtures)}] {fx.name}: {outcome.outcome}: {outcome.detail} ({time.monotonic() - began:.0f}s)")
    return run


def overall(outcomes: list[Outcome]) -> tuple[str | None, str]:
    """("pass" | "fail" | None, the reason). Any miss is a `fail`, listing the fixtures that couldn't run; with no
    miss, one that couldn't run leaves no result (None): Codex couldn't vouch for the review logic."""
    misses = [o for o in outcomes if o.outcome == "miss"]
    unrun = [o for o in outcomes if o.outcome == "unavailable"]
    if misses:
        text = "missed: " + "; ".join(f"{o.name} ({o.detail})" for o in misses)
        return "fail", text + (f". Not run (Codex couldn't): {', '.join(o.name for o in unrun)}" if unrun else "")
    if unrun:
        return None, f"Codex couldn't run: {', '.join(o.name for o in unrun)}"
    return "pass", ""
