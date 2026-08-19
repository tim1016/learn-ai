from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.identity import strategy_instance_artifact_dir
from app.engine.live.run_ledger import LiveRunLedger
from app.services.alpaca_bot_identity import (
    AlpacaBotIdentityGuard,
    AlpacaBotIdentityRefusedError,
)
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    alpaca_v1_action_plan,
)


def _binding(artifacts_root: Path, *, broker: str) -> None:
    repository = BotBindingRepository(
        artifacts_root,
        instance_dir_for=lambda strategy_instance_id: strategy_instance_artifact_dir(
            artifacts_root,
            "live_state",
            strategy_instance_id,
        ),
    )
    repository.record_launch(
        BrokerBotBinding(
            strategy_instance_id="paper-guard",
            broker=broker,
            symbol="SPY",
            action_plan=alpaca_v1_action_plan("SPY"),
            run_id="run-1",
            created_at_ms=1,
        ),
        launch_reason="deploy",
    )


def _legacy_run_ledger(artifacts_root: Path, *, strategy_instance_id: str) -> None:
    run_dir = artifacts_root / "live_runs" / f"legacy-{strategy_instance_id}"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        LiveRunLedger(
            run_id=run_dir.name,
            code_sha="abc123",
            strategy_instance_id=strategy_instance_id,
            strategy_spec_path="strategy.json",
            strategy_spec_sha256="strategy-sha",
            qc_audit_copy_path="audit.py",
            qc_audit_copy_sha256="audit-sha",
            qc_cloud_backtest_id="qc-backtest",
            account_id="DU123456",
            start_date_ms=1,
            live_config={},
            created_at_ms=1,
        ).model_dump_json(),
        encoding="utf-8",
    )


def test_sqlite_authority_with_no_conflicting_binding_is_alpaca(tmp_path: Path) -> None:
    AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_sqlite_authority_accepts_matching_alpaca_binding(tmp_path: Path) -> None:
    _binding(tmp_path, broker="alpaca")

    AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_missing_sqlite_authority_is_never_inferred_from_binding(tmp_path: Path) -> None:
    _binding(tmp_path, broker="alpaca")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="no active SQLite"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=False)


def test_non_alpaca_binding_conflicts_with_sqlite_authority(tmp_path: Path) -> None:
    _binding(tmp_path, broker="ibkr")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="non-Alpaca"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_legacy_ibkr_run_ledger_conflicts_with_sqlite_authority(tmp_path: Path) -> None:
    _legacy_run_ledger(tmp_path, strategy_instance_id="paper-guard")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="historical IBKR"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_unrelated_legacy_run_ledger_does_not_block_sqlite_authority(
    tmp_path: Path,
) -> None:
    _legacy_run_ledger(tmp_path, strategy_instance_id="other-paper-bot")

    AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_unreadable_legacy_run_ledger_fails_closed(tmp_path: Path) -> None:
    run_dir = tmp_path / "live_runs" / "unreadable-legacy-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable historical"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_unreadable_binding_fails_closed(tmp_path: Path) -> None:
    instance_dir = strategy_instance_artifact_dir(tmp_path, "live_state", "paper-guard")
    instance_dir.mkdir(parents=True)
    (instance_dir / "broker_binding.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)
