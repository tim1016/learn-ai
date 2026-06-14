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

from app.engine.live.live_state_sidecar import LiveStateEnvelope, LiveStateSidecarRepo
from app.engine.live.run import _build_live_state_writer, _read_owned_perm_ids, build_parser, main

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
            "--repo-root",
            "/tmp/x",
            "--strategy-spec-path",
            "/tmp/spec.json",
            "--qc-audit-copy-path",
            "/tmp/qc.py",
            "--qc-cloud-backtest-id",
            "bt-1",
            "--account-id",
            "DU111",
            "--start-date-ms",
            "1700000000000",
        ]
    )
    assert init_args.command == "init-ledger"
    assert init_args.account_id == "DU111"
    # UI-0: --strategy-instance-id is optional, defaults to empty (legacy).
    assert init_args.strategy_instance_id == ""

    pre_args = parser.parse_args(
        [
            "pre-flight",
            "--repo-root",
            "/tmp/x",
            "--run-dir",
            "/tmp/run",
            "--skip-ntp",
        ]
    )
    assert pre_args.command == "pre-flight"
    assert pre_args.skip_ntp is True


def test_live_state_writer_preserves_order_trail(tmp_path: Path) -> None:
    class _Client:
        settings = type("_Settings", (), {"client_id": 42})()

    artifacts_root = tmp_path / "artifacts"
    path = artifacts_root / "live_state" / "spy_ema_crossover" / "live_state.json"
    repo = LiveStateSidecarRepo(path)
    repo.write(
        LiveStateEnvelope(
            strategy_instance_id="spy_ema_crossover",
            run_id="run-fixture",
            bot_order_namespace="learn-ai/spy_ema_crossover/v1",
            ib_client_id=42,
            submitted_orders={"recovery-flatten-SPY-1": {"perm_id": 1176469133}},
            known_perm_ids=[1176469133],
            known_exec_ids=["exec-1"],
            last_processed_bar_ms=1_780_000_000_000,
            last_artifact_flush_ms=1_780_000_000_500,
        )
    )
    writer = _build_live_state_writer(
        strategy_instance_id="spy_ema_crossover",
        run_id="run-fixture",
        client=_Client(),
        artifacts_root=artifacts_root,
    )
    assert writer is not None

    portfolio = type("_Portfolio", (), {"positions": {}})()
    writer(portfolio, 1_780_000_060_000)

    loaded = repo.read()
    assert loaded is not None
    assert loaded.submitted_orders == {"recovery-flatten-SPY-1": {"perm_id": 1176469133}}
    assert loaded.known_perm_ids == [1176469133]
    assert loaded.known_exec_ids == ["exec-1"]


def test_read_owned_perm_ids_hydrates_from_live_state_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "live_state.json"
    LiveStateSidecarRepo(path).write(
        LiveStateEnvelope(
            strategy_instance_id="spy_ema_crossover",
            run_id="run-fixture",
            bot_order_namespace="learn-ai/spy_ema_crossover/v1",
            ib_client_id=42,
            known_perm_ids=[1176469133, 1176469134],
            last_processed_bar_ms=1_780_000_000_000,
            last_artifact_flush_ms=1_780_000_000_500,
        )
    )

    assert _read_owned_perm_ids(path) == {1176469133, 1176469134}


@requires_git
def test_init_ledger_succeeds_in_clean_tree(repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path) -> None:
    repo, spec, qc = repo_with_inputs
    rc = main(
        [
            "init-ledger",
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "references/qc-shadow",
            "--strategy-spec-path",
            str(spec),
            "--qc-audit-copy-path",
            str(qc),
            "--qc-cloud-backtest-id",
            "bt-1",
            "--account-id",
            "DU111",
            "--start-date-ms",
            "1700000000000",
            "--live-config-json",
            '{"symbol": "SPY"}',
            "--run-root",
            str(tmp_path / "live_runs"),
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
def test_parser_accepts_strategy_instance_id() -> None:
    args = build_parser().parse_args(
        [
            "init-ledger",
            "--repo-root",
            "/tmp/x",
            "--strategy-spec-path",
            "/tmp/spec.json",
            "--qc-audit-copy-path",
            "/tmp/qc.py",
            "--qc-cloud-backtest-id",
            "bt-1",
            "--account-id",
            "DU111",
            "--strategy-instance-id",
            "spy-ema-paper-1",
            "--start-date-ms",
            "1700000000000",
        ]
    )
    assert args.strategy_instance_id == "spy-ema-paper-1"


@requires_git
def test_init_ledger_writes_strategy_instance_id(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """UI-0: --strategy-instance-id is persisted into run_ledger.json at
    init-ledger time, with schema bumped to 1.1, while run_id is keyed off
    the existing identity inputs (not the instance id)."""
    repo, spec, qc = repo_with_inputs
    rc = main(
        [
            "init-ledger",
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "references/qc-shadow",
            "--strategy-spec-path",
            str(spec),
            "--qc-audit-copy-path",
            str(qc),
            "--qc-cloud-backtest-id",
            "bt-1",
            "--account-id",
            "DU111",
            "--strategy-instance-id",
            "spy-ema-paper-1",
            "--start-date-ms",
            "1700000000000",
            "--run-root",
            str(tmp_path / "live_runs"),
        ]
    )
    assert rc == 0

    runs = list((tmp_path / "live_runs").iterdir())
    assert len(runs) == 1
    ledger = json.loads((runs[0] / "run_ledger.json").read_text(encoding="utf-8"))
    assert ledger["strategy_instance_id"] == "spy-ema-paper-1"
    assert ledger["schema_version"] == "1.3"
    assert ledger["run_id"] == runs[0].name


@requires_git
def test_init_ledger_writes_strategy_key(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """#416: --strategy-key is persisted into run_ledger.json at init-ledger
    time so the console can default the Start card and 'start' can guard against
    a mismatched algorithm. Not part of the run_id hash."""
    repo, spec, qc = repo_with_inputs
    rc = main(
        [
            "init-ledger",
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "references/qc-shadow",
            "--strategy-spec-path",
            str(spec),
            "--qc-audit-copy-path",
            str(qc),
            "--qc-cloud-backtest-id",
            "bt-1",
            "--account-id",
            "DU111",
            "--strategy-key",
            "spy_ema_crossover",
            "--start-date-ms",
            "1700000000000",
            "--run-root",
            str(tmp_path / "live_runs"),
        ]
    )
    assert rc == 0

    runs = list((tmp_path / "live_runs").iterdir())
    assert len(runs) == 1
    ledger = json.loads((runs[0] / "run_ledger.json").read_text(encoding="utf-8"))
    assert ledger["strategy_key"] == "spy_ema_crossover"
    assert ledger["schema_version"] == "1.3"


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
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "--strategy-spec-path",
            str(spec),
            "--qc-audit-copy-path",
            str(qc),
            "--qc-cloud-backtest-id",
            "bt-1",
            "--account-id",
            "DU111",
            "--start-date-ms",
            "1700000000000",
            "--run-root",
            str(tmp_path / "live_runs"),
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
        "--repo-root",
        str(repo),
        "--clean-tree-scope",
        "PythonDataService",
        "references/qc-shadow",
        "--strategy-spec-path",
        str(spec),
        "--qc-audit-copy-path",
        str(qc),
        "--qc-cloud-backtest-id",
        "bt-1",
        "--account-id",
        "DU111",
        "--start-date-ms",
        "1700000000000",
        "--run-root",
        str(tmp_path / "live_runs"),
    ]
    assert main(args) == 0
    assert main(args) == 2


@requires_git
def test_pre_flight_passes_when_no_flags_set(repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path) -> None:
    repo, _, _ = repo_with_inputs
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_ledger.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")

    rc = main(
        [
            "pre-flight",
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "--run-dir",
            str(run_dir),
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
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "--run-dir",
            str(run_dir),
            "--skip-ntp",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL clean_tree" in out
    assert "HALT" in out


@requires_git
def test_pre_flight_halts_when_halt_flag_present(repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path) -> None:
    repo, _, _ = repo_with_inputs
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_ledger.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    (run_dir / "halt.flag").write_text(json.dumps({"day_n": 3, "reasons": ["x"]}), encoding="utf-8")

    rc = main(
        [
            "pre-flight",
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "--run-dir",
            str(run_dir),
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
    positions_json.write_text(json.dumps({"positions": [{"symbol": "QQQ", "quantity": 100}]}), encoding="utf-8")

    rc = main(
        [
            "pre-flight",
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "--run-dir",
            str(run_dir),
            "--skip-ntp",
            "--positions-json",
            str(positions_json),
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
    positions_json.write_text(json.dumps({"positions": [{"symbol": "SPY", "quantity": 200}]}), encoding="utf-8")

    rc = main(
        [
            "pre-flight",
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "--run-dir",
            str(run_dir),
            "--skip-ntp",
            "--positions-json",
            str(positions_json),
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
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "--run-dir",
            str(run_dir),
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
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "references/qc-shadow",
            "--strategy-spec-path",
            str(spec),
            "--qc-audit-copy-path",
            str(qc),
            "--qc-cloud-backtest-id",
            "bt-1",
            "--account-id",
            "DU111",
            "--start-date-ms",
            "1700000000000",
            "--live-config-json",
            "not-valid-json{{{",
            "--run-root",
            str(tmp_path / "live_runs"),
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
            "--repo-root",
            str(repo),
            "--clean-tree-scope",
            "PythonDataService",
            "references/qc-shadow",
            "--strategy-spec-path",
            str(spec),
            "--qc-audit-copy-path",
            str(qc),
            "--qc-cloud-backtest-id",
            "bt-1",
            "--account-id",
            "DU111",
            "--start-date-ms",
            "1700000000000",
            "--live-config-json",
            '"a-string-not-an-object"',
            "--run-root",
            str(tmp_path / "live_runs"),
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
            "--run-dir",
            "/tmp/run",
            "--readonly",
            "--max-orders-per-day",
            "8",
        ]
    )
    assert args.command == "start"
    assert args.readonly is True
    assert args.max_orders_per_day == 8
    assert args.strategy == "spy_ema_crossover"  # default


def test_start_returns_2_when_run_dir_missing_ledger(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Without a ledger, start can't recover identity — exit 2 is the
    operator-error path documented in the module docstring."""
    rc = main(
        [
            "start",
            "--run-dir",
            str(tmp_path),
            "--readonly",
        ]
    )
    assert rc == 2
    assert "missing run_ledger.json" in capsys.readouterr().err


def _write_ledger_with_strategy_key(tmp_path: Path, strategy_key: str) -> Path:
    """Build a run dir whose ledger pins ``strategy_key``. Returns the run dir."""
    from app.engine.live.run_ledger import build_ledger, write_ledger

    spec = tmp_path / "spec.json"
    spec.write_text('{"strategy": "spy_ema_crossover"}', encoding="utf-8")
    qc_audit = tmp_path / "qc_audit.py"
    qc_audit.write_text("# QC audit copy stub\n", encoding="utf-8")
    ledger = build_ledger(
        code_sha="deadbeef" * 5,
        strategy_spec_path=spec,
        qc_audit_copy_path=qc_audit,
        qc_cloud_backtest_id="bt-test-1",
        account_id="DU123",
        start_date_ms=1714838400000,
        live_config={},
        strategy_key=strategy_key,
    )
    run_dir = tmp_path / ledger.run_id
    write_ledger(run_dir / "run_ledger.json", ledger)
    return run_dir


def test_start_rejects_strategy_inconsistent_with_ledger_key(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """#416 foot-gun guard: when the ledger pins ``strategy_key``, starting with
    a different ``--strategy`` is rejected (exit 2) before the algorithm imports —
    it would otherwise run a different algorithm against a ledger reconciled to a
    different QC backtest.
    """
    run_dir = _write_ledger_with_strategy_key(tmp_path, "spy_ema_crossover")
    rc = main(["start", "--run-dir", str(run_dir), "--strategy", "rsi_mean_reversion", "--readonly"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "does not match" in err
    assert "spy_ema_crossover" in err


def test_start_guard_noops_when_ledger_strategy_key_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A legacy ledger (empty ``strategy_key``) is unguarded — any ``--strategy``
    passes the guard and advances to the algorithm import. Using a non-existent
    module, the run fails at *import* (exit 2, "could not import"), not at the
    guard — proving the guard let it through without executing the engine.
    """
    run_dir = _write_ledger_with_strategy_key(tmp_path, "")
    rc = main(["start", "--run-dir", str(run_dir), "--strategy", "no_such_algo_xyz", "--readonly"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "could not import strategy module" in err
    assert "does not match the ledger's strategy_key" not in err


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


def test_live_config_from_ledger_accepts_safe_canary_sizing() -> None:
    """ADR 0009 PR1 — the ``sizing`` key validates through the discriminated
    union and surfaces as a typed ``SizingPolicy`` on the ``LiveConfig``."""
    from app.engine.execution.order_sizer import FixedShares
    from app.engine.live.run import _live_config_from_ledger

    cfg = _live_config_from_ledger(
        {"symbol": "SPY", "sizing": {"kind": "FixedShares", "value": 1}}
    )
    assert isinstance(cfg.sizing, FixedShares)
    assert cfg.sizing.value == 1


def test_live_config_from_ledger_rejects_malformed_sizing() -> None:
    """A malformed ``sizing`` payload surfaces as the same ``ValueError`` the
    start gate already catches — never a silent fall-through."""
    import pytest

    from app.engine.live.run import _live_config_from_ledger

    with pytest.raises(ValueError, match=r"invalid live_config\.sizing"):
        _live_config_from_ledger({"sizing": {"kind": "FixedShares", "value": 0}})


def test_live_config_from_ledger_absence_of_sizing_is_none() -> None:
    """ADR 0009 — absence of ``sizing`` is legacy/unknown; the engine reads it
    as ``None`` and the portfolio falls back to the legacy SimpleFloor path."""
    from app.engine.live.run import _live_config_from_ledger

    cfg = _live_config_from_ledger({"symbol": "SPY"})
    assert cfg.sizing is None


def test_start_refuses_when_poisoned_flag_present(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
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
            "--run-dir",
            str(tmp_path),
            "--readonly",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "poisoned" in err.lower()
    assert "outside_mutation" in err


def test_start_refuses_when_poisoned_flag_corrupted(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A corrupted flag must NOT be silently ignored — that would let a
    contaminated run resume. Refuse with exit 1, surface the parse error."""
    from app.engine.live.halt import POISONED_FLAG_FILENAME

    (tmp_path / POISONED_FLAG_FILENAME).write_text("not json", encoding="utf-8")
    rc = main(
        [
            "start",
            "--run-dir",
            str(tmp_path),
            "--readonly",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "corrupted" in err.lower() or "unreadable" in err.lower()


def test_start_accepts_hydrate_policy_require() -> None:
    """--hydrate-policy require parses to 'require'; default also 'require'."""
    parser = build_parser()

    args_explicit = parser.parse_args(["start", "--run-dir", "/tmp/run", "--hydrate-policy", "require"])
    assert args_explicit.hydrate_policy == "require"

    args_default = parser.parse_args(["start", "--run-dir", "/tmp/run"])
    assert args_default.hydrate_policy == "require"


def test_start_accepts_allow_cold_start_alias() -> None:
    """--allow-cold-start is an alias for --hydrate-policy disabled."""
    parser = build_parser()
    args = parser.parse_args(["start", "--run-dir", "/tmp/run", "--allow-cold-start"])
    assert args.hydrate_policy == "disabled"


def test_start_default_hydrate_policy_is_require() -> None:
    """No --hydrate-policy flag => default is 'require'."""
    parser = build_parser()
    args = parser.parse_args(["start", "--run-dir", "/tmp/run"])
    assert args.hydrate_policy == "require"


def test_hydration_failure_exits_code_4(tmp_path: Path) -> None:
    """REQUIRE policy with no sidecar on disk exits 4 (distinct from 1/2/3).

    The hydrate() call inside engine.run raises IndicatorStateHydrationError
    when policy=REQUIRE and the sidecar is missing. cmd_start must catch
    it BEFORE the generic Exception handler (which returns 3) and return 4.
    """
    import argparse as _argparse
    from collections.abc import AsyncIterator

    from app.engine.live.run import cmd_start
    from app.engine.live.run_ledger import build_ledger, write_ledger
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    strategy_spec = tmp_path / "spec.json"
    strategy_spec.write_text('{"strategy": "spy_ema_crossover"}', encoding="utf-8")
    qc_audit = tmp_path / "qc_audit.py"
    qc_audit.write_text("# QC audit copy stub\n", encoding="utf-8")

    ledger = build_ledger(
        code_sha="deadbeef" * 5,
        strategy_spec_path=strategy_spec,
        qc_audit_copy_path=qc_audit,
        qc_cloud_backtest_id="bt-test-1",
        account_id="DU123",
        # A past date so NYSE calendar can find a prior session.
        start_date_ms=1714838400000,
        live_config={},
    )
    run_dir = tmp_path / ledger.run_id
    write_ledger(run_dir / "run_ledger.json", ledger)

    # Artifacts root that exists but has no sidecar inside it.
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()

    async def _empty_bars() -> AsyncIterator:  # type: ignore[override]
        return
        yield  # make it an async generator

    broker = FakeBroker()
    args = _argparse.Namespace(
        command="start",
        run_dir=run_dir,
        strategy="spy_ema_crossover",
        readonly=False,
        max_orders_per_day=4,
        hydrate_policy="require",
        artifacts_root=artifacts_root,
        broker=broker,
        bars=_empty_bars(),
        client=None,
    )
    rc = cmd_start(args)
    assert rc == 4, f"expected exit 4 (hydration failure), got {rc}"


def test_stateless_strategy_starts_under_require_without_seed_day(tmp_path: Path) -> None:
    """A stateless strategy (no warm-startable indicator state) must NOT exit 4
    under the default hydrate_policy=require.

    Regression for the dominant "zero clean deployment_validation sessions"
    blocker: deployment_validation reports no persistable state, so maybe_write
    never writes a sidecar, so under REQUIRE hydrate() previously raised
    IndicatorStateHydrationError -> exit 4 on EVERY session before the first
    bar. A strategy with nothing to warm-start cannot fail a warm-start
    requirement; hydrate now short-circuits to an accepted cold-start receipt.
    """
    import argparse as _argparse
    import json as _json
    from collections.abc import AsyncIterator

    from app.engine.live.run import cmd_start
    from app.engine.live.run_ledger import build_ledger, write_ledger
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    strategy_spec = tmp_path / "spec.json"
    strategy_spec.write_text('{"strategy": "deployment_validation"}', encoding="utf-8")
    qc_audit = tmp_path / "qc_audit.py"
    qc_audit.write_text("# QC audit copy stub\n", encoding="utf-8")

    ledger = build_ledger(
        code_sha="deadbeef" * 5,
        strategy_spec_path=strategy_spec,
        qc_audit_copy_path=qc_audit,
        qc_cloud_backtest_id="bt-stateless-1",
        account_id="DU123",
        start_date_ms=1714838400000,
        live_config={},
    )
    run_dir = tmp_path / ledger.run_id
    write_ledger(run_dir / "run_ledger.json", ledger)
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()

    async def _empty_bars() -> AsyncIterator:  # type: ignore[override]
        return
        yield  # make it an async generator

    args = _argparse.Namespace(
        command="start",
        run_dir=run_dir,
        strategy="deployment_validation",
        readonly=False,
        max_orders_per_day=4,
        hydrate_policy="require",
        artifacts_root=artifacts_root,
        broker=FakeBroker(),
        bars=_empty_bars(),
        client=None,
    )
    rc = cmd_start(args)

    assert rc == 0, f"stateless strategy must not exit 4 under require, got {rc}"
    receipt = _json.loads((run_dir / "indicator_state_hydration.json").read_text())
    assert receipt["accepted"] is True
    # No sidecar was read — the null lookup fields prove the cold-start path.
    assert receipt["sidecar_last_consolidated_bar_end_ms"] is None
    assert receipt["global_sha256"] is None


def test_deployment_validation_completes_clean_session_offline(tmp_path: Path) -> None:
    """Engine-correctness receipt: drive deployment_validation through cmd_start
    + the deterministic FakeBroker over a synthetic green-bar session and prove a
    CLEAN session — exit_reason=normal (rc 0), flat at EOD, one entry+exit
    round-trip. This is the offline proof the live decision path produces a
    clean reconciled session (the live receipt additionally needs a real IBKR
    paper Gateway), and the first integration test running this canary through
    the LiveEngine rather than the BacktestEngine unit tests.
    """
    import argparse as _argparse
    import json as _json
    from datetime import datetime, timedelta
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    import pandas as pd

    from app.engine.data.trade_bar import TradeBar
    from app.engine.live.run import cmd_start
    from app.engine.live.run_ledger import build_ledger, write_ledger
    from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars

    ny = ZoneInfo("America/New_York")

    def _bar(hour: int, minute: int, open_: str, close: str) -> TradeBar:
        # Session-time gates (09:45/15:45) read bar.end_time in ET, so bars must
        # be America/New_York-localized or the gates misfire.
        start = datetime(2026, 1, 5, hour, minute, tzinfo=ny)
        o, c = Decimal(open_), Decimal(close)
        return TradeBar(
            symbol="SPY",
            time=start,
            end_time=start + timedelta(minutes=1),
            open=o,
            high=max(o, c),
            low=min(o, c),
            close=c,
            volume=10_000,
        )

    # Two consecutive green bars from 09:45 -> enter (next-bar-open), hold 3
    # bars, exit (next-bar-open). Same validated sequence as the BacktestEngine
    # unit test, now driven through the live decision path.
    bars = [
        _bar(9, 43, "100", "101"),
        _bar(9, 44, "101", "102"),
        _bar(9, 45, "102", "103"),
        _bar(9, 46, "104", "104.5"),
        _bar(9, 47, "105", "105.5"),
        _bar(9, 48, "106", "106.5"),
        _bar(9, 49, "107", "107.5"),
        _bar(9, 50, "108", "108.5"),
    ]

    # Use the real committed spec fixture so resolve_decision_columns yields the
    # canary's core-only decision schema (a stub spec fails to load and falls
    # back to the EMA columns, which the deployment_validation snapshot lacks).
    strategy_spec = (
        Path(__file__).resolve().parents[3]
        / "app"
        / "engine"
        / "strategy"
        / "spec"
        / "fixtures"
        / "deployment_validation.spec.json"
    )
    qc_audit = tmp_path / "qc_audit.py"
    qc_audit.write_text("# QC audit copy stub\n", encoding="utf-8")

    ledger = build_ledger(
        code_sha="deadbeef" * 5,
        strategy_spec_path=strategy_spec,
        qc_audit_copy_path=qc_audit,
        qc_cloud_backtest_id="bt-clean-1",
        account_id="DU123",
        start_date_ms=1714838400000,
        live_config={},
    )
    run_dir = tmp_path / ledger.run_id
    write_ledger(run_dir / "run_ledger.json", ledger)
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()

    broker = FakeBroker()
    args = _argparse.Namespace(
        command="start",
        run_dir=run_dir,
        strategy="deployment_validation",
        readonly=False,
        max_orders_per_day=4,
        hydrate_policy="require",
        artifacts_root=artifacts_root,
        broker=broker,
        bars=iter_bars(bars),
        client=None,
    )
    rc = cmd_start(args)

    assert rc == 0, f"expected a clean exit 0, got {rc}"
    status = _json.loads((run_dir / "run_status.json").read_text())
    assert status["exit_reason"] == "normal"
    assert status["exit_code"] == 0
    # Flat at end of session: the entry and its exit both filled.
    assert broker.positions.get("SPY", 0) == 0
    # Exactly one entry + one exit fill recorded in the executions artifact.
    execs = pd.read_parquet(run_dir / "executions.parquet")
    assert len(execs) == 2


def test_connect_failure_writes_terminal_status_and_exits_3(tmp_path: Path) -> None:
    """A broker connect() failure before the session starts must record a
    terminal status sidecar (exit_code=3, exit_reason=exception) and exit 3 —
    not crash uncaught with a blank 'Why It Stopped'.

    Regression: connect() ran OUTSIDE cmd_start's try/finally, so a clientId
    collision (IbkrClientIdInUseError) propagated through asyncio.run() leaving
    the entry sidecar's exit_code=None and the instance looking stuck
    'starting' in the console.
    """
    import argparse as _argparse
    import json as _json
    from collections.abc import AsyncIterator

    from app.engine.live.run import cmd_start
    from app.engine.live.run_ledger import build_ledger, write_ledger
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _ConnectFailsClient:
        async def connect(self) -> None:
            raise RuntimeError("client id 12 is already in use by another session")

        async def disconnect(self) -> None:  # pragma: no cover - never reached
            pass

    strategy_spec = tmp_path / "spec.json"
    strategy_spec.write_text('{"strategy": "deployment_validation"}', encoding="utf-8")
    qc_audit = tmp_path / "qc_audit.py"
    qc_audit.write_text("# QC audit copy stub\n", encoding="utf-8")

    ledger = build_ledger(
        code_sha="deadbeef" * 5,
        strategy_spec_path=strategy_spec,
        qc_audit_copy_path=qc_audit,
        qc_cloud_backtest_id="bt-connect-1",
        account_id="DU123",
        start_date_ms=1714838400000,
        live_config={},
    )
    run_dir = tmp_path / ledger.run_id
    write_ledger(run_dir / "run_ledger.json", ledger)
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()

    async def _empty_bars() -> AsyncIterator:  # type: ignore[override]
        return
        yield  # make it an async generator

    args = _argparse.Namespace(
        command="start",
        run_dir=run_dir,
        strategy="deployment_validation",
        readonly=False,
        max_orders_per_day=4,
        hydrate_policy="optional",
        artifacts_root=artifacts_root,
        broker=FakeBroker(),
        bars=_empty_bars(),
        client=_ConnectFailsClient(),
    )
    rc = cmd_start(args)

    assert rc == 3, f"connect failure should exit 3, got {rc}"
    status = _json.loads((run_dir / "run_status.json").read_text())
    assert status["exit_reason"] == "exception"
    assert status["exit_code"] == 3
    # Terminal record written — not blank (stuck 'starting').
    assert status["ended_at_ms"] is not None


def test_fetch_positions_failure_writes_terminal_status_and_exits_3(tmp_path: Path) -> None:
    """A broker fetch_positions() failure (transient Gateway hiccup) must record a
    terminal status (exit 3 / exception) and exit 3 — not leave the instance
    looking stuck 'starting' with a blank 'Why It Stopped'. Same UX guarantee as
    the connect-failure path (PR #449 review, bug_010)."""
    import argparse as _argparse
    import json as _json
    from collections.abc import AsyncIterator

    from app.engine.live.run import cmd_start
    from app.engine.live.run_ledger import build_ledger, write_ledger
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _FetchFailsBroker(FakeBroker):
        async def fetch_positions(self):  # type: ignore[override]
            raise RuntimeError("gateway timeout fetching positions")

    strategy_spec = tmp_path / "spec.json"
    strategy_spec.write_text('{"strategy": "deployment_validation"}', encoding="utf-8")
    qc_audit = tmp_path / "qc_audit.py"
    qc_audit.write_text("# QC audit copy stub\n", encoding="utf-8")
    ledger = build_ledger(
        code_sha="deadbeef" * 5,
        strategy_spec_path=strategy_spec,
        qc_audit_copy_path=qc_audit,
        qc_cloud_backtest_id="bt-fetchfail-1",
        account_id="DU123",
        start_date_ms=1714838400000,
        live_config={},
    )
    run_dir = tmp_path / ledger.run_id
    write_ledger(run_dir / "run_ledger.json", ledger)
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()

    async def _empty_bars() -> AsyncIterator:  # type: ignore[override]
        return
        yield

    args = _argparse.Namespace(
        command="start",
        run_dir=run_dir,
        strategy="deployment_validation",
        readonly=False,
        max_orders_per_day=4,
        hydrate_policy="optional",
        artifacts_root=artifacts_root,
        broker=_FetchFailsBroker(),
        bars=_empty_bars(),
        client=None,
    )
    rc = cmd_start(args)

    assert rc == 3, f"fetch-positions failure should exit 3, got {rc}"
    status = _json.loads((run_dir / "run_status.json").read_text())
    assert status["exit_reason"] == "exception"
    assert status["exit_code"] == 3
    assert status["ended_at_ms"] is not None


def test_unexpected_position_halt_writes_terminal_status_and_exits_1(tmp_path: Path) -> None:
    """A contaminated-account halt (a foreign position) must record a terminal
    status (exit 1 / fatal_halt) so the console explains the refusal instead of
    showing a blank 'stuck starting' (PR #449 review, bug_010)."""
    import argparse as _argparse
    import json as _json
    from collections.abc import AsyncIterator
    from types import SimpleNamespace

    from app.engine.live.run import cmd_start
    from app.engine.live.run_ledger import build_ledger, write_ledger
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    class _ForeignPositionBroker(FakeBroker):
        async def fetch_positions(self):  # type: ignore[override]
            # A position in a symbol outside the strategy's namespace → halt.
            return SimpleNamespace(positions=[SimpleNamespace(symbol="AAPL", quantity=10)])

    strategy_spec = tmp_path / "spec.json"
    strategy_spec.write_text('{"strategy": "deployment_validation"}', encoding="utf-8")
    qc_audit = tmp_path / "qc_audit.py"
    qc_audit.write_text("# QC audit copy stub\n", encoding="utf-8")
    ledger = build_ledger(
        code_sha="deadbeef" * 5,
        strategy_spec_path=strategy_spec,
        qc_audit_copy_path=qc_audit,
        qc_cloud_backtest_id="bt-foreign-1",
        account_id="DU123",
        start_date_ms=1714838400000,
        live_config={},
    )
    run_dir = tmp_path / ledger.run_id
    write_ledger(run_dir / "run_ledger.json", ledger)
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()

    async def _empty_bars() -> AsyncIterator:  # type: ignore[override]
        return
        yield

    args = _argparse.Namespace(
        command="start",
        run_dir=run_dir,
        strategy="deployment_validation",
        readonly=False,
        max_orders_per_day=4,
        hydrate_policy="optional",
        artifacts_root=artifacts_root,
        broker=_ForeignPositionBroker(),
        bars=_empty_bars(),
        client=None,
    )
    rc = cmd_start(args)

    assert rc == 1, f"unexpected-position halt should exit 1, got {rc}"
    status = _json.loads((run_dir / "run_status.json").read_text())
    assert status["exit_reason"] == "fatal_halt"
    assert status["exit_code"] == 1
    assert status["ended_at_ms"] is not None


def test_poison_refusal_records_terminal_status_when_absent(tmp_path: Path) -> None:
    """A start refused because the run is poisoned records a terminal run_status
    (exit_reason=poisoned) when none exists, so the console "Why It Stopped"
    panel shows "fresh run_id required" instead of a blank "ended unexpectedly".
    """
    import argparse as _argparse
    import json as _json

    from app.engine.live.halt import (
        PoisonedHaltReason,
        PoisonedHaltTrigger,
        write_poisoned_flag,
    )
    from app.engine.live.run import cmd_start

    run_dir = tmp_path / "poisoned-run"
    run_dir.mkdir()
    write_poisoned_flag(
        run_dir,
        PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
            halted_at_ms=1_700_000_000_000,
            last_clean_bar_close_ms=0,
        ),
    )

    rc = cmd_start(_argparse.Namespace(command="start", run_dir=run_dir))

    assert rc == 1
    status = _json.loads((run_dir / "run_status.json").read_text())
    assert status["exit_reason"] == "poisoned"
    assert status["exit_code"] == 1
    assert status["ended_at_ms"] is not None


def test_poison_refusal_preserves_existing_terminal_status(tmp_path: Path) -> None:
    """The poison refusal never clobbers an existing run_status — that status
    carries the richer original halt reason (e.g. fatal_halt) explaining WHY the
    run poisoned. Only the genuinely-blank case is filled in.
    """
    import argparse as _argparse
    import json as _json

    from app.engine.live.halt import (
        PoisonedHaltReason,
        PoisonedHaltTrigger,
        write_poisoned_flag,
    )
    from app.engine.live.run import cmd_start
    from app.engine.live.run_status import write_run_status
    from app.schemas.live_runs import ExitReason, RunStatusSidecar

    run_dir = tmp_path / "poisoned-run-2"
    run_dir.mkdir()
    write_poisoned_flag(
        run_dir,
        PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
            halted_at_ms=1,
            last_clean_bar_close_ms=0,
        ),
    )
    # Pre-existing terminal status from the session that poisoned the run.
    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id="poisoned-run-2",
            started_at_ms=1,
            last_update_ms=2,
            ended_at_ms=3,
            exit_code=1,
            exit_reason=ExitReason.fatal_halt,
            host_pid=123,
        ),
    )

    rc = cmd_start(_argparse.Namespace(command="start", run_dir=run_dir))

    assert rc == 1
    status = _json.loads((run_dir / "run_status.json").read_text())
    # Original halt reason preserved, not overwritten by the poison refusal.
    assert status["exit_reason"] == "fatal_halt"


def test_poison_refusal_overwrites_clean_stop_status(tmp_path: Path) -> None:
    """A poison refusal MUST overwrite a CLEAN-stop status — MARK_POISONED writes
    poisoned.flag and exits gracefully (exit_reason=keyboard_interrupt), so if the
    refusal skipped over it the console would keep showing the poisoned run as
    cleanly stopped instead of 'fresh deployment required' (Codex P1 on #450)."""
    import argparse as _argparse
    import json as _json

    from app.engine.live.halt import (
        PoisonedHaltReason,
        PoisonedHaltTrigger,
        write_poisoned_flag,
    )
    from app.engine.live.run import cmd_start
    from app.engine.live.run_status import write_run_status
    from app.schemas.live_runs import ExitReason, RunStatusSidecar

    run_dir = tmp_path / "poisoned-run-3"
    run_dir.mkdir()
    write_poisoned_flag(
        run_dir,
        PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OPERATOR_DECLARED,
            halted_at_ms=1,
            last_clean_bar_close_ms=0,
        ),
    )
    # The graceful-shutdown status MARK_POISONED leaves behind (clean stop).
    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id="poisoned-run-3",
            started_at_ms=1,
            last_update_ms=2,
            ended_at_ms=3,
            exit_code=0,
            exit_reason=ExitReason.keyboard_interrupt,
            host_pid=123,
        ),
    )

    rc = cmd_start(_argparse.Namespace(command="start", run_dir=run_dir))

    assert rc == 1
    status = _json.loads((run_dir / "run_status.json").read_text())
    # Clean-stop status overwritten — the poison is now legible to the console.
    assert status["exit_reason"] == "poisoned"


def test_start_returns_2_when_strategy_module_unknown(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
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
            "--run-dir",
            str(tmp_path),
            "--strategy",
            "nonexistent_module",
            "--readonly",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "could not import strategy" in err


def test_make_ibkr_client_pins_spec_client_id() -> None:
    """A spec-declared client_id pins the Gateway clientId so two
    strategies never collide on one Gateway (PR #376 P2 / §16.3).

    Before the fix, cmd_start created IbkrClient() unconditionally and the
    spec's client_id was inert — the run used the env/default clientId
    even though the ledger pinned a different one.
    """
    from app.engine.live.run import _make_ibkr_client

    pinned = _make_ibkr_client(11)
    assert pinned.settings.client_id == 11

    # Omitted (None) ⇒ fall back to the env/default clientId, not 11.
    fallback = _make_ibkr_client(None)
    assert fallback.settings.client_id != 11
