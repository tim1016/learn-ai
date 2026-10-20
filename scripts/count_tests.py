#!/usr/bin/env python3
"""Count tests per suite and per folder (#3357, plan #3356 sections 2 and 8).

The test-cut project (#3356) targets a third fewer test *functions*. This script
is the measuring stick: it reports, for Python pytest and Frontend Vitest,
per folder and per suite,

``functions``
    Test functions. A parametrized test counts once, however many cases it
    expands to. Python: every ``def test*`` / ``async def test*`` at module or
    class level (never a closure inside another function) of a ``test_*.py`` /
    ``*_test.py`` module, which is pytest's own collection rule; it matches
    ``pytest --collect-only`` file by file. TypeScript:
    every ``it(``, ``test(``, ``it.each(``, ``test.each(`` call (``.only``,
    ``.skip``, ``.todo`` and friends included, and a ``<...>`` type-argument
    list before the call, as in ``it.each<[string, () => void]>(...)``).

``lines``
    Physical lines. Python counts every ``.py`` file under the test roots, so
    ``conftest.py`` and helper modules are included (they are maintained with
    the tests); they add lines and assertions but no functions. Frontend counts
    ``*.spec.ts`` files.

``assertions``
    The fuse receipt (#3356 section 6): a fuse must leave it unchanged.
    Python: ``assert`` statements, ``pytest.raises`` / ``pytest.warns`` calls
    and ``assert_*`` / ``assertFoo`` calls (``np.testing.assert_allclose``,
    ``mock.assert_called_once()``, ``self.assertEqual``). A call to a local
    ``assert_*`` helper counts as well as the ``assert`` statements inside the
    helper; that stays stable across a fuse, which is all the receipt needs.
    TypeScript: ``expect(`` and ``expect.soft(`` calls.

Python is read with ``ast``; TypeScript with regexes run over source whose
comments and string/template-literal text are blanked, so ``// it('x')`` and
``'expect(1)'`` in a test name never count. Stdlib only. The script never
imports or runs application code and never writes to the tests it reads.

Baseline (#3356, master 23040e3b): ``PythonDataService/tests`` 468,603 lines,
and 487 Frontend spec files with 100,202 lines; both reproduce exactly. Python
here also covers ``app/engine/tests`` and ``app/engine/strategy/spec/tests``,
which the first grep left out, so its totals are slightly larger.

Needs Python 3.12 or newer (the repo's version): the test files use syntax an
older ``ast`` cannot parse, and the script stops naming the file when it hits one.

Usage::

    python scripts/count_tests.py                # markdown tables
    python scripts/count_tests.py --json         # the same numbers as JSON
    python scripts/count_tests.py --depth 2      # finer folders
    python scripts/count_tests.py --suite python # one suite
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class SuiteSpec:
    """One test suite: where its tests live and how to read them."""

    name: str
    title: str
    base: str  # project directory under the repo root; folder labels start below it
    roots: tuple[str, ...]  # test roots, relative to ``base``
    language: str  # "python" or "typescript"
    # Folder levels below each root shown in the table. Frontend sits one level
    # deeper because every spec lives under src/app, which would otherwise be
    # a single 371-file row.
    default_depth: int


SUITES: tuple[SuiteSpec, ...] = (
    SuiteSpec(
        name="python",
        title="Python pytest (PythonDataService)",
        base="PythonDataService",
        roots=("tests", "app/engine/tests", "app/engine/strategy/spec/tests"),
        language="python",
        default_depth=1,
    ),
    SuiteSpec(
        name="frontend",
        title="Frontend Vitest (Frontend/src)",
        base="Frontend",
        roots=("src",),
        language="typescript",
        default_depth=3,
    ),
)

_SKIPPED_DIRS = frozenset({"__pycache__", "node_modules"})
_PYTEST_TEST_FILE = re.compile(r"^(?:test_.*|.*_test)\.py$")
_PYTEST_FUNCTION_PREFIX = "test"
_PYTEST_ASSERTION_CALLS = frozenset({"raises", "warns"})
_ASSERT_CALL_NAME = re.compile(r"^assert(?:_|[A-Z])")


class CountError(Exception):
    """A test file could not be read or parsed; the counts would be wrong."""


@dataclass
class Counts:
    files: int = 0
    functions: int = 0
    lines: int = 0
    assertions: int = 0

    def add(self, other: Counts) -> None:
        self.files += other.files
        self.functions += other.functions
        self.lines += other.lines
        self.assertions += other.assertions

    def as_dict(self) -> dict[str, int]:
        return {
            "files": self.files,
            "functions": self.functions,
            "lines": self.lines,
            "assertions": self.assertions,
        }


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


def _is_pytest_collected_function(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(_PYTEST_FUNCTION_PREFIX)


def _collectable_scopes(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements pytest can collect from: module/class bodies, not closures.

    Descends through class bodies and control flow (``if TYPE_CHECKING``,
    ``try``, ``with``) that does not open a function scope.
    """
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.ClassDef):
            yield from _collectable_scopes(stmt.body)
        elif isinstance(stmt, (ast.If, ast.For, ast.While)):
            yield from _collectable_scopes(stmt.body)
            yield from _collectable_scopes(stmt.orelse)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            yield from _collectable_scopes(stmt.body)
        elif isinstance(stmt, ast.Try):
            yield from _collectable_scopes(stmt.body)
            for handler in stmt.handlers:
                yield from _collectable_scopes(handler.body)
            yield from _collectable_scopes(stmt.orelse)
            yield from _collectable_scopes(stmt.finalbody)


def _called_name(call: ast.Call) -> tuple[str | None, str | None]:
    """``(owner, name)`` of a call: ``pytest.raises`` -> ("pytest", "raises")."""
    func = call.func
    if isinstance(func, ast.Name):
        return None, func.id
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        return owner, func.attr
    return None, None


def _is_assertion_call(call: ast.Call) -> bool:
    owner, name = _called_name(call)
    if name is None:
        return False
    if owner == "pytest" and name in _PYTEST_ASSERTION_CALLS:
        return True
    return _ASSERT_CALL_NAME.match(name) is not None


def count_python_source(source: str, *, is_test_module: bool, path: str) -> Counts:
    """Count one Python file; ``is_test_module`` gates the function count."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise CountError(f"{path}: cannot parse ({exc.msg}, line {exc.lineno})") from exc
    functions = 0
    if is_test_module:
        functions = sum(1 for stmt in _collectable_scopes(tree.body) if _is_pytest_collected_function(stmt))
    assertions = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) or (isinstance(node, ast.Call) and _is_assertion_call(node)):
            assertions += 1
    return Counts(
        files=1,
        functions=functions,
        lines=len(source.splitlines()),
        assertions=assertions,
    )


# --------------------------------------------------------------------------
# TypeScript
# --------------------------------------------------------------------------

# A `/` after one of these tokens opens a regex literal, after anything else it
# is division. Good enough for spec files; a miss only hides a quote character
# inside a regex literal.
_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_PRECEDING_WORDS = frozenset({"return", "typeof", "case", "in", "of", "void", "delete"})
_IDENT_CHAR = re.compile(r"[\w$]")

_TS_TEST_CALL_HEAD = re.compile(
    r"(?<![\w$.])(?:it|test)"
    r"(?:\.(?:each|only|skip|todo|concurrent|sequential|fails|runIf|skipIf))*"
    r"\s*"
)
# How far a ``<...>`` type argument list may run (an object type spans lines).
_TS_TYPE_ARGUMENTS_REACH = 800
_TS_ASSERTION_CALL = re.compile(r"(?<![\w$.])expect(?:\.soft)?\s*\(")


def blank_ts_noncode(source: str) -> str:
    """Replace comments and string/template text with spaces, keeping newlines.

    Code inside ``${...}`` of a template literal is kept. The quote characters
    themselves stay, so ``it.each`...` `` is still recognisable.
    """
    out: list[str] = []
    n = len(source)
    i = 0
    # Stack of open template-literal `${` depths, to know which `}` closes one.
    template_braces: list[int] = []
    brace_depth = 0
    last_significant = ""  # last non-space code character, for regex detection
    last_word = ""  # identifier ending at last_significant (or just before spaces)
    prev_ident = False

    def blank(ch: str) -> str:
        return ch if ch == "\n" else " "

    def scan_template_text(start: int) -> int:
        """Blank template text from ``start`` to the closing backtick or `${`."""
        j = start
        while j < n:
            ch = source[j]
            if ch == "\\":
                out.append(" ")
                if j + 1 < n:
                    out.append(blank(source[j + 1]))
                j += 2
                continue
            if ch == "`":
                out.append("`")
                return j + 1
            if ch == "$" and j + 1 < n and source[j + 1] == "{":
                out.append("${")
                template_braces.append(brace_depth)
                return j + 2
            out.append(blank(ch))
            j += 1
        return j

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and nxt == "*":
            out.append("  ")
            i += 2
            while i < n and not (source[i] == "*" and source[i + 1 : i + 2] == "/"):
                out.append(blank(source[i]))
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue
        if ch in "'\"":
            quote = ch
            out.append(quote)
            i += 1
            while i < n and source[i] != quote and source[i] != "\n":
                if source[i] == "\\" and i + 1 < n:
                    out.append(" ")
                    out.append(blank(source[i + 1]))
                    i += 2
                    continue
                out.append(" ")
                i += 1
            if i < n and source[i] == quote:
                out.append(quote)
                i += 1
            last_significant, last_word, prev_ident = quote, "", False
            continue
        if ch == "`":
            out.append("`")
            i = scan_template_text(i + 1)
            last_significant, last_word, prev_ident = "`", "", False
            continue
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            if template_braces and template_braces[-1] == brace_depth:
                template_braces.pop()
                out.append("}")
                i = scan_template_text(i + 1)
                continue
            brace_depth -= 1
        elif ch == "/" and (
            last_significant == "" or last_significant in _REGEX_PRECEDERS or last_word in _REGEX_PRECEDING_WORDS
        ):
            out.append("/")
            i += 1
            in_class = False
            while i < n and source[i] != "\n":
                c = source[i]
                if c == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                out.append(" ")
                i += 1
            if i < n and source[i] == "/":
                out.append("/")
                i += 1
            last_significant, last_word, prev_ident = "/", "", False
            continue
        out.append(ch)
        if _IDENT_CHAR.match(ch):
            last_word = (last_word + ch) if prev_ident else ch
            prev_ident = True
            last_significant = ch
        elif not ch.isspace():
            last_word, prev_ident = "", False
            last_significant = ch
        else:
            prev_ident = False
        i += 1
    return "".join(out)


def _after_type_arguments(code: str, start: int) -> int:
    """Index past the ``<...>`` that opens at ``start``; ``start`` when there is none.

    ``it.each<[string, () => void]>(...)`` holds parentheses and an arrow, so the
    brackets are matched by depth and the ``>`` of ``=>`` is not a closer. A list
    that does not close within reach stops the run: a silent skip would hide a table.
    """
    if code[start : start + 1] != "<":
        return start
    depth = 0
    for i in range(start, min(len(code), start + _TS_TYPE_ARGUMENTS_REACH)):
        if code[i] == "<":
            depth += 1
        elif code[i] == ">" and code[i - 1] != "=":
            depth -= 1
            if depth == 0:
                return i + 1
    raise CountError(f"a type-argument list after .each is not closed within {_TS_TYPE_ARGUMENTS_REACH} characters")


def _count_ts_test_calls(code: str) -> int:
    count = 0
    for head in _TS_TEST_CALL_HEAD.finditer(code):
        # Only a table (`.each`) takes type arguments; `it < limit` is a comparison.
        is_table = head.group(0).rstrip().endswith(".each")
        i = _after_type_arguments(code, head.end()) if is_table else head.end()
        while code[i : i + 1].isspace():
            i += 1
        if code[i : i + 1] in ("(", "`"):
            count += 1
    return count


def count_typescript_source(source: str) -> Counts:
    code = blank_ts_noncode(source)
    return Counts(
        files=1,
        functions=_count_ts_test_calls(code),
        lines=len(source.splitlines()),
        assertions=len(_TS_ASSERTION_CALL.findall(code)),
    )


# --------------------------------------------------------------------------
# Walking and grouping
# --------------------------------------------------------------------------


def _iter_files(root: Path, language: str) -> Iterator[Path]:
    """Source files under ``root``, sorted, skipping caches and dot-directories."""
    if not root.is_dir():
        raise CountError(f"{root}: test root does not exist")
    suffix = ".py" if language == "python" else ".spec.ts"
    for path in sorted(root.rglob(f"*{suffix}")):
        rel_parts = path.relative_to(root).parts
        if any(part in _SKIPPED_DIRS or part.startswith(".") for part in rel_parts[:-1]):
            continue
        if path.is_file():
            yield path


def _count_file(path: Path, language: str) -> Counts:
    source = path.read_text(encoding="utf-8")
    if language == "python":
        return count_python_source(
            source,
            is_test_module=_PYTEST_TEST_FILE.match(path.name) is not None,
            path=str(path),
        )
    try:
        return count_typescript_source(source)
    except CountError as exc:
        raise CountError(f"{path}: {exc}") from exc


def folder_key(base: Path, root: Path, path: Path, depth: int) -> str:
    """Folder of ``path`` relative to ``base``, cut ``depth`` levels below ``root``."""
    parts = path.parent.relative_to(root).parts[:depth]
    return root.relative_to(base).joinpath(*parts).as_posix()


def count_suite(spec: SuiteSpec, *, repo_root: Path, depth: int) -> dict[str, Counts]:
    """Per-folder counts for one suite, keyed by folder relative to its base."""
    folders: dict[str, Counts] = {}
    base = repo_root / spec.base
    for root_name in spec.roots:
        root = base / root_name
        for path in _iter_files(root, spec.language):
            key = folder_key(base, root, path, depth)
            try:
                counts = _count_file(path, spec.language)
            except (UnicodeDecodeError, OSError) as exc:
                raise CountError(f"{path}: cannot read ({exc})") from exc
            folders.setdefault(key, Counts()).add(counts)
    return dict(sorted(folders.items()))


def total(folders: dict[str, Counts]) -> Counts:
    result = Counts()
    for counts in folders.values():
        result.add(counts)
    return result


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

_COLUMNS = ("Files", "Functions", "Lines", "Assertions")


def _row(label: str, counts: Counts) -> str:
    values = (counts.files, counts.functions, counts.lines, counts.assertions)
    return f"| {label} | " + " | ".join(f"{value:,}" for value in values) + " |"


def render_markdown(results: dict[str, tuple[SuiteSpec, dict[str, Counts]]]) -> str:
    lines: list[str] = []
    grand = Counts()
    for spec, folders in results.values():
        suite_total = total(folders)
        grand.add(suite_total)
        lines.append(f"### {spec.title}")
        lines.append("")
        lines.append("| Folder | " + " | ".join(_COLUMNS) + " |")
        lines.append("|---|" + "---:|" * len(_COLUMNS))
        for key, counts in folders.items():
            lines.append(_row(f"`{key}`", counts))
        lines.append(_row("**Total**", suite_total))
        lines.append("")
    if len(results) > 1:
        lines.append("### All suites")
        lines.append("")
        lines.append("| Suite | " + " | ".join(_COLUMNS) + " |")
        lines.append("|---|" + "---:|" * len(_COLUMNS))
        for name, (_, folders) in results.items():
            lines.append(_row(name, total(folders)))
        lines.append(_row("**Total**", grand))
        lines.append("")
    return "\n".join(lines)


def render_json(results: dict[str, tuple[SuiteSpec, dict[str, Counts]]]) -> str:
    payload: dict[str, object] = {}
    grand = Counts()
    for name, (_, folders) in results.items():
        suite_total = total(folders)
        grand.add(suite_total)
        payload[name] = {
            "total": suite_total.as_dict(),
            "folders": {key: counts.as_dict() for key, counts in folders.items()},
        }
    payload["total"] = grand.as_dict()
    return json.dumps(payload, indent=2) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Count test functions, lines and assertions per suite and folder.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    parser.add_argument(
        "--suite",
        choices=[spec.name for spec in SUITES],
        action="append",
        help="count only this suite (repeatable); default is all",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=None,
        help="folder levels below each test root (default: 1 Python, 3 Frontend)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root the suite paths are relative to (default: this checkout)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.depth is not None and args.depth < 0:
        sys.stderr.write("count_tests: --depth must be 0 or more\n")
        return 2
    selected = set(args.suite) if args.suite else {spec.name for spec in SUITES}
    results: dict[str, tuple[SuiteSpec, dict[str, Counts]]] = {}
    try:
        for spec in SUITES:
            if spec.name not in selected:
                continue
            depth = spec.default_depth if args.depth is None else args.depth
            results[spec.name] = (spec, count_suite(spec, repo_root=args.repo_root, depth=depth))
    except CountError as exc:
        sys.stderr.write(f"count_tests: {exc}\n")
        return 2
    sys.stdout.write(render_json(results) if args.json else render_markdown(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
