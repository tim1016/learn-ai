"""#2117: `print()` inside a subprocess-exec'd probe string is invisible to
ruff — the probe body is only string data to the outer parser until a child
interpreter compiles it, so no lint configuration on this file can see
inside it. Six occurrences of exactly that survived undetected until found
by hand; this narrowly reproduces the one incident this repo actually
had — `print` reachable inside a probe string, directly or via an alias
(`__p = print; __p(...)`, caught by flagging any `Name`/`Attribute`
reference to `print`, not just a direct `Call`) — rather than
re-implementing a linter for probe bodies (no ruff plugin, no general
type-hint or style check, nothing outside this directory).

Known, disclosed gap: discovery only recognizes a probe assigned to a
variable literally named ``probe`` or ``_PROBE`` (see
``_PROBE_VARIABLE_NAMES``) — the naming convention every probe in this
directory already follows. A probe under a different name, or reached only
through deeper indirection than a bare ``print``/``builtins.print``
reference (e.g. ``getattr(builtins, 'print')``), is still invisible to
this check. Chasing those is general-purpose static analysis, which this
test deliberately does not attempt.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

_FLEET_TESTS_DIR = Path(__file__).resolve().parent

#: A floor, not a pin: probe strings are added and retired as the fleet test
#: suite grows, so this must never hardcode today's exact count (7, at the
#: time of writing). Its only job is to make "the walk found nothing to
#: examine" fail exactly as loudly as "found a `print(` inside a probe" —
#: this repo has repeatedly shipped a check whose offender list was empty
#: only because it walked nothing (independent review, 2026-09-15).
_MINIMUM_PROBES_EXAMINED = 4

#: The two names every probe string in this directory is assigned to.
_PROBE_VARIABLE_NAMES = ("probe", "_PROBE")


def _probe_string_literals() -> list[tuple[Path, int, str]]:
    """Every string assigned to a `probe`/`_PROBE` name anywhere (module or
    function scope) in a `tests/broker/fleet/*.py` file.

    Covers every shape used in this directory: a plain string literal, an
    implicitly-concatenated tuple of string literals (the parser folds those
    into one `Constant` before this ever sees it), and a literal passed as
    the first argument to a wrapping call such as `textwrap.dedent(...)` —
    dedented here too, exactly as the real assignment dedents it, or the
    literal's still-indented first line fails to parse as its own program
    for a reason that has nothing to do with whether it contains `print`.
    """
    literals: list[tuple[Path, int, str]] = []
    for path in sorted(_FLEET_TESTS_DIR.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id in _PROBE_VARIABLE_NAMES
                for target in node.targets
            ):
                continue
            value = node.value
            dedent_wrapped = False
            if isinstance(value, ast.Call) and value.args:
                func = value.func
                func_name = (
                    func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                )
                dedent_wrapped = func_name == "dedent"
                value = value.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                text = textwrap.dedent(value.value) if dedent_wrapped else value.value
                literals.append((path, node.lineno, text))
    return literals


def _references_print(tree: ast.AST) -> bool:
    """True if `print` is reachable anywhere in this probe's own AST — as a
    direct call (`print(...)`), an alias binding that a later call could use
    (`__p = print` — the `Name` node is caught the same way regardless of
    whether it sits in `Load`, `Store`, or any other context), or an
    attribute access naming it explicitly (`builtins.print`,
    `__builtins__.print`). Does not chase indirection through a computed
    lookup like `getattr(builtins, 'print')` — see the module docstring for
    exactly where this check's coverage ends.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "print":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "print":
            return True
    return False


def test_no_print_survives_inside_a_fleet_probe_string() -> None:
    """Parses each probe string as its own standalone program (exactly what
    the subprocess it's handed to will do) and looks for `print` anywhere in
    *that* program — not a text search of the outer file, which is precisely
    the blind spot this guards, and not only a direct `print(...)` call,
    which an alias binding would slip past.
    """
    probes = _probe_string_literals()
    assert len(probes) >= _MINIMUM_PROBES_EXAMINED, (
        f"only {len(probes)} probe string(s) were found to examine under "
        f"{_FLEET_TESTS_DIR}; the discovery walk may be broken (renamed "
        "probe variable, moved directory, ...), which would make the "
        "print-free assertion below pass having probed nothing"
    )

    offenders = [
        f"{path.name}:{lineno}"
        for path, lineno, source in probes
        if _references_print(ast.parse(source))
    ]

    assert offenders == [], (
        "print is reachable inside an exec'd probe string, invisible to "
        f"ruff, at: {offenders}. Use sys.stdout.write(...) + '\\n' instead "
        "(the parent process reads the child's stdout as its result)."
    )
