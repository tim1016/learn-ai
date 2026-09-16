"""Contract: the pre-commit hook never runs a tool the documented CI gate doesn't.

``package.json``'s ``lint-staged`` config used to run ``ruff format`` (which
rewrites pre-existing regions of any touched file) after ``ruff check --fix``
on every Python commit, while the documented CI gate — ``.claude/CLAUDE.md``
and ``PythonDataService/CLAUDE.md`` — is ``ruff check`` only, with
``ruff format`` documented as its own explicit command. The two disagreeing
made ``--no-verify`` the pragmatic default for several #2137/#2138 commits
(#2148). This pins the hook to the same tool the gate runs.
"""

from __future__ import annotations

import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_JSON = REPOSITORY_ROOT / "package.json"


def _lint_staged_python_command() -> str | list[str]:
    config = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    return config["lint-staged"]["PythonDataService/**/*.py"]


def test_the_python_pre_commit_command_never_runs_ruff_format() -> None:
    """``ruff format`` rewrites unrelated regions of a touched file; the CI
    gate (``ruff check app/ tests/`` in ``.github/workflows/ci.yml``) never
    runs it, so pre-commit must not either."""
    command = _lint_staged_python_command()
    commands = command if isinstance(command, list) else [command]

    formatting_commands = [step for step in commands if "ruff format" in step]
    assert not formatting_commands, (
        f"lint-staged still runs {formatting_commands} for Python files, which "
        "disagrees with the documented ruff check-only CI gate. Formatting is "
        "an explicit command (`ruff format PythonDataService/app/`, see "
        "PythonDataService/CLAUDE.md), not a pre-commit side effect."
    )


def test_the_python_pre_commit_command_runs_the_same_tool_as_the_ci_gate() -> None:
    """The hook must still lint — just with the gate's own tool."""
    command = _lint_staged_python_command()
    commands = command if isinstance(command, list) else [command]

    assert any(step.startswith("ruff check") for step in commands), (
        "lint-staged must run ruff check for Python files, the same tool "
        "`.github/workflows/ci.yml` and CLAUDE.md's documented lint command use."
    )
