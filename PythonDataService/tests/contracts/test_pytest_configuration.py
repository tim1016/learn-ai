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
    from scripts.run_fast_tests import TEST_BUDGET_SECONDS, pytest_command

    command = pytest_command(shard_index=1, shard_count=4)

    assert TEST_BUDGET_SECONDS == 120
    assert command[0:3] == [sys.executable, "-m", "pytest"]
    marker_index = len(command) - 1 - command[::-1].index("-m")
    assert command[marker_index + 1] == "not slow"
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


# A tiny project the shard plugin deals: six measured test files whose
# longest-first deal over three shards is 8+3 / 7+4 / 6+5 seconds, and three
# unmeasured files that must keep their hash shard. Each file holds two tests
# that split its time, so the deal must sum by file and keep a file together.
_SHARD_PLUGIN_MEASURED = {"8s": 1, "7s": 2, "6s": 3, "5s": 3, "4s": 2, "3s": 1}
_SHARD_PLUGIN_UNMEASURED = ("new_a", "new_b", "new_c")
_SHARD_PLUGIN_TESTS = ("test_first", "test_second")


def _shard_plugin_file(case: str) -> str:
    return f"test_{case}.py"


def _shard_plugin_nodeids(case: str) -> list[str]:
    return [f"{_shard_plugin_file(case)}::{test}" for test in _SHARD_PLUGIN_TESTS]


_SHARD_PLUGIN_DURATIONS = {
    nodeid: float(case.removesuffix("s")) / len(_SHARD_PLUGIN_TESTS)
    for case in _SHARD_PLUGIN_MEASURED
    for nodeid in _shard_plugin_nodeids(case)
}


def _write_shard_plugin_project(root: Path, durations: dict[str, float]) -> None:
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "durations.json").write_text(json.dumps(durations), encoding="utf-8")
    (root / "conftest.py").write_text(
        "from pathlib import Path\n\n"
        "import scripts.pytest_shard\n\n"
        "scripts.pytest_shard.DURATIONS_PATH = "
        'Path(__file__).with_name("durations.json")\n',
        encoding="utf-8",
    )
    for case in [*_SHARD_PLUGIN_MEASURED, *_SHARD_PLUGIN_UNMEASURED]:
        (root / _shard_plugin_file(case)).write_text(
            "".join(f"def {test}():\n    pass\n\n\n" for test in _SHARD_PLUGIN_TESTS),
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

    owners = {
        **_SHARD_PLUGIN_MEASURED,
        **{
            case: hash_shard(_shard_plugin_file(case), shard_count=3)
            for case in _SHARD_PLUGIN_UNMEASURED
        },
    }
    return {
        nodeid: shard
        for case, shard in owners.items()
        for nodeid in _shard_plugin_nodeids(case)
    }


def test_pr_shard_plugin_deals_measured_files_longest_first_and_the_rest_by_hash(
    tmp_path: Path,
) -> None:
    from scripts.pytest_shard import hash_shard

    _write_shard_plugin_project(tmp_path, _SHARD_PLUGIN_DURATIONS)
    expected = _expected_shard_plugin_deal()
    # The fixture must tell the two deals apart, or it proves nothing.
    assert any(
        hash_shard(_shard_plugin_file(case), shard_count=3) != shard
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
            f"PR shard {shard}/3: 6 of 9 test files matched durations.json; "
            "the other 3 use the hash shard"
        ) in result.stdout
        selected[shard] = {
            line for line in result.stdout.splitlines()
            if line.startswith("test_") and "::" in line
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
    assert "PR shard 1/3: 6 of 9 test files matched durations.json" in result.stdout


def test_pr_shard_plugin_fails_when_the_durations_file_matches_no_collected_file(
    tmp_path: Path,
) -> None:
    _write_shard_plugin_project(tmp_path, {"test_elsewhere.py::test_gone": 1.0})

    result = _run_shard_plugin(
        tmp_path, "--collect-only", "-q", "--pr-shard-index", "1", "--pr-shard-count", "3"
    )

    assert result.returncode != 0
    assert (
        "none of the 1 test files in durations.json match the 9 collected test files"
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
        returncode = runner.run_fast_tests(shard_index=5, shard_count=16)

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

    assert runner.run_fast_tests() == 124
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
        assert runner.run_fast_tests() == 3
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
