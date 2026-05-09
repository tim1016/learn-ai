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


@requires_git
def test_pre_flight_halts_when_positions_json_has_foreign_symbol(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """CodeRabbit P1 fix — positions check is now actually wired into cmd_pre_flight."""
    repo, _, _ = repo_with_inputs
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_ledger.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")

    positions_json = tmp_path / "positions.json"
    positions_json.write_text(
        json.dumps({"positions": [{"symbol": "QQQ", "quantity": 100}]}), encoding="utf-8"
    )

    rc = main(
        [
            "pre-flight",
            "--repo-root", str(repo),
            "--clean-tree-scope", "PythonDataService",
            "--run-dir", str(run_dir),
            "--skip-ntp",
            "--positions-json", str(positions_json),
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL unexpected_position" in out


@requires_git
def test_pre_flight_passes_when_positions_json_matches_expected(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, _, _ = repo_with_inputs
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_ledger.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")

    positions_json = tmp_path / "positions.json"
    positions_json.write_text(
        json.dumps({"positions": [{"symbol": "SPY", "quantity": 200}]}), encoding="utf-8"
    )

    rc = main(
        [
            "pre-flight",
            "--repo-root", str(repo),
            "--clean-tree-scope", "PythonDataService",
            "--run-dir", str(run_dir),
            "--skip-ntp",
            "--positions-json", str(positions_json),
        ]
    )
    assert rc == 0


@requires_git
def test_pre_flight_skips_position_check_when_no_positions_json(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """No --positions-json ⇒ skip with a clear message; check the live runner does the wired check."""
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
    assert "skipping unexpected-position check" in capsys.readouterr().out


@requires_git
def test_init_ledger_returns_2_on_malformed_live_config_json(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """CodeRabbit P2 fix — malformed JSON returns the documented exit 2 instead of crashing."""
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
            "--live-config-json", "not-valid-json{{{",
            "--run-root", str(tmp_path / "live_runs"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err


@requires_git
def test_init_ledger_returns_2_when_live_config_json_is_not_an_object(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture
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
            "--live-config-json", '"a-string-not-an-object"',
            "--run-root", str(tmp_path / "live_runs"),
        ]
    )
    assert rc == 2
    assert "must be a JSON object" in capsys.readouterr().err


# ──────────────────────────── start subcommand ───────────────────────


def test_start_subcommand_args_parse() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "start",
            "--run-dir", "/tmp/run",
            "--readonly",
            "--max-orders-per-day", "8",
        ]
    )
    assert args.command == "start"
    assert args.readonly is True
    assert args.max_orders_per_day == 8
    assert args.strategy == "spy_ema_crossover"  # default


def test_start_returns_2_when_run_dir_missing_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Without a ledger, start can't recover identity — exit 2 is the
    operator-error path documented in the module docstring."""
    rc = main(
        [
            "start",
            "--run-dir", str(tmp_path),
            "--readonly",
        ]
    )
    assert rc == 2
    assert "missing run_ledger.json" in capsys.readouterr().err


def test_live_config_from_ledger_applies_known_fields() -> None:
    """CodeRabbit P2 fix: ledger.live_config must round-trip into LiveConfig."""
    from datetime import time as time_cls

    from app.engine.live.run import _live_config_from_ledger

    cfg = _live_config_from_ledger(
        {
            "symbol": "QQQ",
            "force_flat_at": "15:50",
            "consolidator_period_min": 30,
            "max_submit_latency_ms": 250,
        }
    )
    assert cfg.symbol == "QQQ"
    assert cfg.force_flat_at == time_cls(15, 50)
    assert cfg.consolidator_period_min == 30
    assert cfg.max_submit_latency_ms == 250


def test_live_config_from_ledger_handles_null_force_flat() -> None:
    from app.engine.live.run import _live_config_from_ledger

    cfg = _live_config_from_ledger({"force_flat_at": None})
    assert cfg.force_flat_at is None


def test_live_config_from_ledger_returns_defaults_for_empty_payload() -> None:
    from datetime import time as time_cls

    from app.engine.live.run import _live_config_from_ledger

    cfg = _live_config_from_ledger({})
    # LiveConfig defaults — pinned here so a future change to the
    # defaults can't silently change what an empty payload means.
    assert cfg.symbol == "SPY"
    assert cfg.force_flat_at == time_cls(15, 55)


def test_live_config_from_ledger_rejects_unknown_keys() -> None:
    """Unknown keys mean the ledger was written with a newer schema —
    refuse rather than silently drop them, since the dropped values
    were part of run_id."""
    import pytest

    from app.engine.live.run import _live_config_from_ledger

    with pytest.raises(ValueError, match="unknown live_config keys"):
        _live_config_from_ledger({"future_field": 1})


def test_start_refuses_when_poisoned_flag_present(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """§ 7.2 #4: a poisoned run never resumes on its own run_id.

    The cmd_start refusal exits 1 (halt) so an operator who runs
    `start` against a previously-halted run dir gets the halt
    trigger surfaced rather than silently re-entering the run.
    """

    from app.engine.live.halt import (
        PoisonedHaltReason,
        PoisonedHaltTrigger,
        write_poisoned_flag,
    )

    # Even with a valid ledger, the poisoned.flag short-circuits
    # before the ledger is even loaded.
    write_poisoned_flag(
        tmp_path,
        PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
            halted_at_ms=1_700_000_000_500,
            last_clean_bar_close_ms=1_700_000_000_000,
            details={"exec_id": "foreign-1"},
        ),
    )
    rc = main(
        [
            "start",
            "--run-dir", str(tmp_path),
            "--readonly",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "poisoned" in err.lower()
    assert "outside_mutation" in err


def test_start_refuses_when_poisoned_flag_corrupted(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A corrupted flag must NOT be silently ignored — that would let a
    contaminated run resume. Refuse with exit 1, surface the parse error."""
    from app.engine.live.halt import POISONED_FLAG_FILENAME

    (tmp_path / POISONED_FLAG_FILENAME).write_text("not json", encoding="utf-8")
    rc = main(
        [
            "start",
            "--run-dir", str(tmp_path),
            "--readonly",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "corrupted" in err.lower() or "unreadable" in err.lower()


def test_start_returns_2_when_strategy_module_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Unknown strategy module surfaces as operator error (exit 2),
    not a runtime crash."""
    # Build a minimal valid ledger so we get past the ledger-load step.
    import json as _json

    (tmp_path / "run_ledger.json").write_text(
        _json.dumps(
            {
                "schema_version": "1.0",
                "run_id": "x",
                "code_sha": "abc",
                "strategy_spec_path": "/x",
                "strategy_spec_sha256": "y",
                "qc_audit_copy_path": "/x",
                "qc_audit_copy_sha256": "z",
                "qc_cloud_backtest_id": "bt",
                "account_id": "DU111",
                "start_date_ms": 1700000000000,
                "live_config": {},
                "created_at_ms": 1700000000000,
            }
        ),
        encoding="utf-8",
    )
    rc = main(
        [
            "start",
            "--run-dir", str(tmp_path),
            "--strategy", "nonexistent_module",
            "--readonly",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "could not import strategy" in err
