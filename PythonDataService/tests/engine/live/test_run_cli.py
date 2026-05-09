"""Tests for app.engine.live.run CLI subcommands.

Covers init-ledger and pre-flight subcommands. Both subcommands shell
out to ``git`` for the dirty-tree check; the test fixtures build a
real on-disk git repo in ``tmp_path`` so we exercise the actual
``git status`` codepath rather than mocking subprocess.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.engine.live.run import build_parser, main

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not available in this environment",
)


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)


@pytest.fixture
def repo_with_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Clean repo containing a strategy spec and a QC audit copy file.

    Returns ``(repo_root, spec_path, qc_audit_path)``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "PythonDataService").mkdir()
    spec_path = repo / "PythonDataService" / "spec.json"
    spec_path.write_text('{"strategy": "spy_ema_crossover"}', encoding="utf-8")

    (repo / "references" / "qc-shadow").mkdir(parents=True)
    qc_path = repo / "references" / "qc-shadow" / "SpyEmaCrossoverAlgorithm.py"
    qc_path.write_text("# QC audit copy\n", encoding="utf-8")

    subprocess.run(
        [
            "git",
            "add",
            "PythonDataService/spec.json",
            "references/qc-shadow/SpyEmaCrossoverAlgorithm.py",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed", "--no-gpg-sign"],
        cwd=repo,
        check=True,
    )

    return repo, spec_path, qc_path


def test_parser_supports_init_ledger_and_pre_flight() -> None:
    parser = build_parser()
    init_args = parser.parse_args(
        [
            "init-ledger",
            "--repo-root", "/tmp/x",
            "--strategy-spec-path", "/tmp/spec.json",
            "--qc-audit-copy-path", "/tmp/qc.py",
            "--qc-cloud-backtest-id", "bt-1",
            "--account-id", "DU111",
            "--start-date-ms", "1700000000000",
        ]
    )
    assert init_args.command == "init-ledger"
    assert init_args.account_id == "DU111"

    pre_args = parser.parse_args(
        [
            "pre-flight",
            "--repo-root", "/tmp/x",
            "--run-dir", "/tmp/run",
            "--skip-ntp",
        ]
    )
    assert pre_args.command == "pre-flight"
    assert pre_args.skip_ntp is True


@requires_git
def test_init_ledger_succeeds_in_clean_tree(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, spec, qc = repo_with_inputs
    rc = main(
        [
            "init-ledger",
            "--repo-root", str(repo),
            "--clean-tree-scope", "PythonDataService", "references/qc-shadow",
            "--strategy-spec-path", str(spec),
            "--qc-audit-copy-path", str(qc),
            "--qc-cloud-backtest-id", "bt-1",
            "--account-id", "DU111",
            "--start-date-ms", "1700000000000",
            "--live-config-json", '{"symbol": "SPY"}',
            "--run-root", str(tmp_path / "live_runs"),
        ]
    )
    assert rc == 0

    runs_root = tmp_path / "live_runs"
    runs = list(runs_root.iterdir())
    assert len(runs) == 1
    ledger = json.loads((runs[0] / "run_ledger.json").read_text(encoding="utf-8"))
    assert ledger["account_id"] == "DU111"
    assert ledger["live_config"] == {"symbol": "SPY"}
    assert ledger["run_id"] == runs[0].name


@requires_git
def test_init_ledger_refuses_dirty_tree(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo, spec, qc = repo_with_inputs
    # Introduce a modification in scope.
    (repo / "PythonDataService" / "extra.py").write_text("scratch\n")

    rc = main(
        [
            "init-ledger",
            "--repo-root", str(repo),
            "--clean-tree-scope", "PythonDataService",
            "--strategy-spec-path", str(spec),
            "--qc-audit-copy-path", str(qc),
            "--qc-cloud-backtest-id", "bt-1",
            "--account-id", "DU111",
            "--start-date-ms", "1700000000000",
            "--run-root", str(tmp_path / "live_runs"),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "dirty-tree halt" in err

    # No ledger written.
    assert not (tmp_path / "live_runs").exists() or not list((tmp_path / "live_runs").iterdir())


@requires_git
def test_init_ledger_refuses_existing_run_dir_without_force(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, spec, qc = repo_with_inputs
    args = [
        "init-ledger",
        "--repo-root", str(repo),
        "--clean-tree-scope", "PythonDataService", "references/qc-shadow",
        "--strategy-spec-path", str(spec),
        "--qc-audit-copy-path", str(qc),
        "--qc-cloud-backtest-id", "bt-1",
        "--account-id", "DU111",
        "--start-date-ms", "1700000000000",
        "--run-root", str(tmp_path / "live_runs"),
    ]
    assert main(args) == 0
    assert main(args) == 2


@requires_git
def test_pre_flight_passes_when_no_flags_set(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, _, _ = repo_with_inputs
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_ledger.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")

    rc = main(
        [
            "pre-flight",
            "--repo-root", str(repo),
            "--clean-tree-scope", "PythonDataService",
            "--run-dir", str(run_dir),
            "--skip-ntp",
        ]
    )
    assert rc == 0


@requires_git
def test_pre_flight_halts_when_dirty_tree(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo, _, _ = repo_with_inputs
    (repo / "PythonDataService" / "scratch.py").write_text("x\n")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_ledger.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")

    rc = main(
        [
            "pre-flight",
            "--repo-root", str(repo),
            "--clean-tree-scope", "PythonDataService",
            "--run-dir", str(run_dir),
            "--skip-ntp",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL clean_tree" in out
    assert "HALT" in out


@requires_git
def test_pre_flight_halts_when_halt_flag_present(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, _, _ = repo_with_inputs
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_ledger.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    (run_dir / "halt.flag").write_text(json.dumps({"day_n": 3, "reasons": ["x"]}), encoding="utf-8")

    rc = main(
        [
            "pre-flight",
            "--repo-root", str(repo),
            "--clean-tree-scope", "PythonDataService",
            "--run-dir", str(run_dir),
            "--skip-ntp",
        ]
    )
    assert rc == 1
