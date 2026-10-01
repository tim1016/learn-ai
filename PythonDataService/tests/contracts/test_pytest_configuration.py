from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPOSITORY_ROOT / "PythonDataService"
CI_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/ci.yml"
DAILY_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/daily-tests.yml"
E2E_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/frontend-e2e.yml"
FRONTEND_BUDGET_RUNNER = REPOSITORY_ROOT / "Frontend/scripts/run-test-budget.cjs"
FRONTEND_CI_CONFIG = REPOSITORY_ROOT / "Frontend/vitest.ci.config.ts"


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


def test_python_pr_suite_has_a_hard_two_minute_budget() -> None:
    from scripts.run_fast_tests import DAILY_ONLY_PATHS, TEST_BUDGET_SECONDS, pytest_command

    command = pytest_command((), shard_index=1, shard_count=4)

    assert TEST_BUDGET_SECONDS == 120
    assert command[0:3] == [sys.executable, "-m", "pytest"]
    marker_index = len(command) - 1 - command[::-1].index("-m")
    assert command[marker_index + 1] == "not slow"
    for path in DAILY_ONLY_PATHS:
        assert f"--ignore={path}" in command
    assert command[-4:] == ["--pr-shard-index", "1", "--pr-shard-count", "4"]
    assert "python -m scripts.run_fast_tests" in CI_WORKFLOW.read_text(encoding="utf-8")


def test_python_pr_shards_balance_by_measured_duration() -> None:
    from scripts.pytest_shard import assign_shards, hash_shard

    durations = {
        **{f"tests/test_ten.py::test_case[{index}]": 10.0 for index in range(12)},
        **{f"tests/test_five.py::test_case[{index}]": 5.0 for index in range(12)},
        **{f"tests/test_one.py::test_case[{index}]": 1.0 for index in range(12)},
    }
    unknown = [f"tests/test_new.py::test_case[{index}]" for index in range(7)]
    nodeids = [*durations, *unknown]
    assignments = assign_shards(nodeids, shard_count=4, durations=durations)

    # Complete, disjoint, and independent of the collection order presented.
    assert sorted(assignments) == sorted(nodeids)
    assert set(assignments.values()) == {1, 2, 3, 4}
    assert assignments == assign_shards(
        list(reversed(nodeids)), shard_count=4, durations=durations
    )
    # Longest-first dealing equalizes the measured time exactly here.
    loads = [0.0] * 4
    for nodeid, shard in assignments.items():
        loads[shard - 1] += durations.get(nodeid, 0.0)
    assert loads == [48.0, 48.0, 48.0, 48.0]
    # Tests missing from the durations file keep the stable hash shard.
    for nodeid in unknown:
        assert assignments[nodeid] == hash_shard(nodeid, shard_count=4)


def test_committed_pr_shard_durations_drive_the_balance() -> None:
    from scripts.pytest_shard import load_pr_shard_durations

    durations = load_pr_shard_durations()

    assert len(durations) >= 1000
    assert all(duration > 0 for duration in durations.values())


# A tiny project the shard plugin deals: six measured tests whose
# longest-first deal over three shards is 8+3 / 7+4 / 6+5 seconds, and three
# unmeasured tests that must keep their hash shard.
_SHARD_PLUGIN_MEASURED = {"8s": 1, "7s": 2, "6s": 3, "5s": 3, "4s": 2, "3s": 1}
_SHARD_PLUGIN_UNMEASURED = ("new-a", "new-b", "new-c")


def _shard_plugin_nodeid(case: str) -> str:
    return f"test_generated.py::test_case[{case}]"


_SHARD_PLUGIN_DURATIONS = {
    _shard_plugin_nodeid(case): float(case.removesuffix("s"))
    for case in _SHARD_PLUGIN_MEASURED
}


def _write_shard_plugin_project(root: Path, durations: dict[str, float]) -> None:
    cases = [*_SHARD_PLUGIN_MEASURED, *_SHARD_PLUGIN_UNMEASURED]
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "durations.json").write_text(json.dumps(durations), encoding="utf-8")
    (root / "conftest.py").write_text(
        "from pathlib import Path\n\n"
        "import scripts.pytest_shard\n\n"
        "scripts.pytest_shard.DURATIONS_PATH = "
        'Path(__file__).with_name("durations.json")\n',
        encoding="utf-8",
    )
    (root / "test_generated.py").write_text(
        "import pytest\n\n\n"
        f"@pytest.mark.parametrize('case', {cases!r})\n"
        "def test_case(case):\n"
        "    assert case\n",
        encoding="utf-8",
    )


def _run_shard_plugin(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(SERVICE_ROOT), env.get("PYTHONPATH")) if path
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "scripts.pytest_shard",
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _expected_shard_plugin_deal() -> dict[str, int]:
    from scripts.pytest_shard import hash_shard

    return {
        **{
            _shard_plugin_nodeid(case): shard
            for case, shard in _SHARD_PLUGIN_MEASURED.items()
        },
        **{
            _shard_plugin_nodeid(case): hash_shard(
                _shard_plugin_nodeid(case), shard_count=3
            )
            for case in _SHARD_PLUGIN_UNMEASURED
        },
    }


def test_pr_shard_plugin_deals_measured_tests_longest_first_and_the_rest_by_hash(
    tmp_path: Path,
) -> None:
    from scripts.pytest_shard import hash_shard

    _write_shard_plugin_project(tmp_path, _SHARD_PLUGIN_DURATIONS)
    expected = _expected_shard_plugin_deal()
    # The fixture must tell the two deals apart, or it proves nothing.
    assert any(
        hash_shard(_shard_plugin_nodeid(case), shard_count=3) != shard
        for case, shard in _SHARD_PLUGIN_MEASURED.items()
    )

    selected: dict[int, set[str]] = {}
    for shard in (1, 2, 3):
        result = _run_shard_plugin(
            tmp_path,
            "--collect-only",
            "-q",
            "--pr-shard-index",
            str(shard),
            "--pr-shard-count",
            "3",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (
            f"PR shard {shard}/3: 6 of 9 collected tests matched durations.json; "
            "the other 3 use the hash shard"
        ) in result.stdout
        selected[shard] = {
            line for line in result.stdout.splitlines()
            if line.startswith("test_generated.py::")
        }

    assert set().union(*selected.values()) == set(expected)
    assert sum(len(nodeids) for nodeids in selected.values()) == len(expected)
    assert selected == {
        shard: {nodeid for nodeid, owner in expected.items() if owner == shard}
        for shard in (1, 2, 3)
    }


def test_pr_shard_plugin_deals_and_reports_the_same_under_xdist(tmp_path: Path) -> None:
    _write_shard_plugin_project(tmp_path, _SHARD_PLUGIN_DURATIONS)
    expected = _expected_shard_plugin_deal()

    result = _run_shard_plugin(
        tmp_path, "-n", "2", "-q", "-rA", "--pr-shard-index", "1", "--pr-shard-count", "3"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert set(re.findall(r"^PASSED (\S+)$", result.stdout, flags=re.MULTILINE)) == {
        nodeid for nodeid, owner in expected.items() if owner == 1
    }
    assert "PR shard 1/3: 6 of 9 collected tests matched durations.json" in result.stdout


def test_pr_shard_plugin_fails_when_the_durations_file_matches_no_collected_test(
    tmp_path: Path,
) -> None:
    _write_shard_plugin_project(tmp_path, {"test_elsewhere.py::test_gone": 1.0})

    result = _run_shard_plugin(
        tmp_path, "--collect-only", "-q", "--pr-shard-index", "1", "--pr-shard-count", "3"
    )

    assert result.returncode != 0
    assert (
        "none of the 1 tests in durations.json match the 9 collected tests"
        in result.stdout + result.stderr
    )


@pytest.mark.parametrize(
    ("shard_args", "message"),
    [
        (
            ("--pr-shard-index", "17", "--pr-shard-count", "16"),
            "--pr-shard-index 17 must be between 1 and --pr-shard-count 16",
        ),
        (("--pr-shard-index", "1"), "both PR shard options are required"),
    ],
)
def test_pr_shard_plugin_rejects_an_unusable_shard(
    tmp_path: Path,
    shard_args: tuple[str, ...],
    message: str,
) -> None:
    _write_shard_plugin_project(tmp_path, _SHARD_PLUGIN_DURATIONS)

    result = _run_shard_plugin(tmp_path, "--collect-only", "-q", *shard_args)

    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert message in result.stderr


def _child_command(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def test_run_fast_tests_returns_the_child_exit_code_and_reports_its_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from scripts import run_fast_tests as runner

    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(
        runner, "pytest_command", lambda *_a, **_k: _child_command("raise SystemExit(3)")
    )

    with caplog.at_level(logging.INFO, logger="scripts.run_fast_tests"):
        returncode = runner.run_fast_tests((), shard_index=5, shard_count=16)

    assert returncode == 3
    line = summary.read_text(encoding="utf-8")
    assert re.fullmatch(
        r"Python PR tests \(shard 5/16\) took \d+\.\ds of the 120-second budget\n", line
    )
    assert line.strip() in caplog.text


def test_run_fast_tests_kills_an_overrun_and_reports_the_exceeded_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import run_fast_tests as runner

    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(runner, "TEST_BUDGET_SECONDS", 1)
    monkeypatch.setattr(
        runner,
        "pytest_command",
        lambda *_a, **_k: _child_command("import time; time.sleep(60)"),
    )

    assert runner.run_fast_tests(()) == 124
    assert re.fullmatch(
        r"Python PR tests \(unsharded\) took \d+\.\ds of the 1-second budget "
        r"\(exceeded\)\n",
        summary.read_text(encoding="utf-8"),
    )


def test_run_fast_tests_keeps_the_exit_code_when_the_step_summary_is_unwritable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from scripts import run_fast_tests as runner

    # A directory: appending to it raises IsADirectoryError, an OSError.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))
    monkeypatch.setattr(
        runner, "pytest_command", lambda *_a, **_k: _child_command("raise SystemExit(3)")
    )

    with caplog.at_level(logging.WARNING, logger="scripts.run_fast_tests"):
        assert runner.run_fast_tests(()) == 3
    assert "Could not append the test time to GITHUB_STEP_SUMMARY" in caplog.text


def test_parse_durations_sums_phases_and_keeps_node_ids_with_spaces() -> None:
    from scripts.update_pr_shard_durations import parse_durations

    output = "\n".join(
        (
            "=========================== slowest durations ===========================",
            "1.50s call     tests/test_a.py::test_x[with a space]",
            "0.20s setup    tests/test_a.py::test_x[with a space]",
            "0.01s teardown tests/test_a.py::test_y",
            "(3 durations < 0.005s hidden.  Use -vv to show these durations.)",
        )
    )

    assert parse_durations(output) == {
        "tests/test_a.py::test_x[with a space]": 1.7,
        "tests/test_a.py::test_y": 0.01,
    }


def test_pr_workflow_runs_bounded_python_and_frontend_shards() -> None:
    ci_contents = CI_WORKFLOW.read_text(encoding="utf-8")
    frontend_config = FRONTEND_CI_CONFIG.read_text(encoding="utf-8")
    frontend_job = ci_contents.split("  frontend-test-shard:", maxsplit=1)[1].split(
        "\n  frontend-test:", maxsplit=1
    )[0]

    assert "python-test-shard:" in ci_contents
    assert (
        "shard: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]" in ci_contents
    )
    assert (
        'python -m scripts.run_fast_tests --shard "${{ matrix.shard }}/16"' in ci_contents
    )
    assert "name: Frontend Test Shard ${{ matrix.shard }}/6" in frontend_job
    assert "shard: [1, 2, 3, 4, 5, 6]" in frontend_job
    assert 'TEST_SHARD_COUNT: "6"' in frontend_job
    assert "--runner-config=vitest.ci.config.ts" in frontend_job
    assert "shard:" in frontend_config


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
