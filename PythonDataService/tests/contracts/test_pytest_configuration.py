from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPOSITORY_ROOT / "PythonDataService"
CI_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/ci.yml"
DAILY_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/daily-tests.yml"
E2E_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/frontend-e2e.yml"
FRONTEND_BUDGET_RUNNER = REPOSITORY_ROOT / "Frontend/scripts/run-test-budget.cjs"
FAST_TEST_COMMAND_SOURCES = (
    CI_WORKFLOW,
    REPOSITORY_ROOT / ".claude/CLAUDE.md",
    REPOSITORY_ROOT / ".claude/commands/test-all.md",
    SERVICE_ROOT / "CLAUDE.md",
    SERVICE_ROOT / "pytest.ini",
)


def test_root_conftest_defers_fastapi_app_import() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy, sys; "
                "runpy.run_path('tests/conftest.py'); "
                "raise SystemExit('app.main' in sys.modules)"
            ),
        ],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_fast_test_commands_filter_by_marker_not_name() -> None:
    incorrect_sources: list[str] = []
    missing_sources: list[str] = []

    for path in FAST_TEST_COMMAND_SOURCES:
        contents = path.read_text(encoding="utf-8")
        relative_path = str(path.relative_to(REPOSITORY_ROOT))
        if '-k "not slow"' in contents:
            incorrect_sources.append(relative_path)
        if "run_fast_tests" not in contents and '-m "not slow"' not in contents:
            missing_sources.append(relative_path)

    assert incorrect_sources == []
    assert missing_sources == []


def test_python_pr_suite_has_a_hard_two_minute_budget() -> None:
    from scripts.run_fast_tests import DAILY_ONLY_PATHS, TEST_BUDGET_SECONDS, pytest_command

    command = pytest_command(())

    assert TEST_BUDGET_SECONDS == 120
    assert command[0:3] == [sys.executable, "-m", "pytest"]
    marker_index = len(command) - 1 - command[::-1].index("-m")
    assert command[marker_index + 1] == "not slow"
    for path in DAILY_ONLY_PATHS:
        assert f"--ignore={path}" in command
    assert "python -m scripts.run_fast_tests" in CI_WORKFLOW.read_text(encoding="utf-8")


def test_daily_workflow_owns_deferred_python_coverage() -> None:
    contents = DAILY_WORKFLOW.read_text(encoding="utf-8")

    assert "schedule:" in contents
    assert "cron:" in contents
    assert "python -m pytest tests app/engine/tests" in contents
    assert "tests/unit/data_lake tests/integration/data_lake" in contents
    assert '-m "not slow"' not in contents


def test_other_change_gating_suites_are_bounded_or_daily() -> None:
    ci_contents = CI_WORKFLOW.read_text(encoding="utf-8")
    daily_contents = DAILY_WORKFLOW.read_text(encoding="utf-8")
    e2e_contents = E2E_WORKFLOW.read_text(encoding="utf-8")
    frontend_runner = FRONTEND_BUDGET_RUNNER.read_text(encoding="utf-8")

    assert "const TEST_BUDGET_MS = 120_000;" in frontend_runner
    assert "- run: npm test" in ci_contents
    assert 'timeout --signal=KILL 120s dotnet test' in ci_contents
    assert '--filter "Category!=PostgresIntegration"' in ci_contents
    assert "dotnet test Backend.Tests/Backend.Tests.csproj" in daily_contents
    assert "schedule:" in e2e_contents
    assert "pull_request:" not in e2e_contents
