from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.bot_lifecycle_state import (
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.engine.live.identity import strategy_instance_artifact_dir
from app.engine.live.run_ledger import LiveRunLedger, write_ledger
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    alpaca_v1_action_plan,
)
from app.services.bot_control_plane import ControlPlaneRefusedError
from app.services.ibkr_lifecycle_guard import (
    IbkrLifecycleMutationCapability,
    ibkr_lifecycle_capability,
)

SID = "plane-guard-1"


def _write_ibkr_ledger(artifacts_root: Path) -> None:
    run_dir = artifacts_root / "live_runs" / "run-ibkr"
    run_dir.mkdir(parents=True)
    write_ledger(
        run_dir / "run_ledger.json",
        LiveRunLedger(
            run_id="run-ibkr",
            code_sha="abc123",
            strategy_instance_id=SID,
            strategy_spec_path="spec.json",
            strategy_spec_sha256="spec-sha",
            qc_audit_copy_path="qc.py",
            qc_audit_copy_sha256="qc-sha",
            qc_cloud_backtest_id="qc-1",
            account_id="DU123",
            start_date_ms=1_780_000_000_000,
            live_config={},
        ),
    )


def _write_runner_binding(artifacts_root: Path, *, broker: str = "alpaca") -> None:
    BotBindingRepository(
        artifacts_root,
        instance_dir_for=lambda sid: strategy_instance_artifact_dir(
            artifacts_root,
            "live_state",
            sid,
        ),
    ).record_launch(
        BrokerBotBinding(
            strategy_instance_id=SID,
            broker=broker,
            run_id="run-alpaca",
            strategy_key="deployment_validation",
            symbol="SPY",
            use_rth=True,
            mode="log_only",
            quantity=1,
            action_plan=alpaca_v1_action_plan("SPY"),
            created_at_ms=1_780_000_000_000,
        ),
        launch_reason="deploy",
    )


def test_ibkr_evaluator_requires_positive_ibkr_ledger_identity(tmp_path: Path) -> None:
    _write_ibkr_ledger(tmp_path)

    capability = ibkr_lifecycle_capability(tmp_path, SID)

    assert isinstance(capability, IbkrLifecycleMutationCapability)


def test_ibkr_evaluator_accepts_positive_ibkr_broker_binding(tmp_path: Path) -> None:
    _write_runner_binding(tmp_path, broker="ibkr")

    capability = ibkr_lifecycle_capability(tmp_path, SID)

    assert isinstance(capability, IbkrLifecycleMutationCapability)


def test_ibkr_capability_revalidates_plane_at_each_mutation(tmp_path: Path) -> None:
    _write_ibkr_ledger(tmp_path)
    capability = ibkr_lifecycle_capability(tmp_path, SID)
    _write_runner_binding(tmp_path, broker="alpaca")

    with pytest.raises(ControlPlaneRefusedError, match="ambiguous"):
        capability.retire_bot(
            now_ms=1_780_000_000_001,
            updated_by="test",
            reason="plane_changed",
        )

    assert BotLifecycleStateRepo(stable_bot_lifecycle_state_path(tmp_path, SID)).read() is None


@pytest.mark.parametrize(
    "identity",
    ["missing", "alpaca", "ambiguous", "unknown_broker", "corrupt_ibkr"],
)
def test_ibkr_evaluator_refuses_non_ibkr_or_unreadable_identity(
    tmp_path: Path,
    identity: str,
) -> None:
    if identity == "alpaca":
        _write_runner_binding(tmp_path)
    elif identity == "ambiguous":
        _write_runner_binding(tmp_path)
        _write_ibkr_ledger(tmp_path)
    elif identity == "unknown_broker":
        _write_runner_binding(tmp_path, broker="unknown")
    elif identity == "corrupt_ibkr":
        ledger = tmp_path / "live_runs" / "run-ibkr" / "run_ledger.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            '{"strategy_instance_id":"plane-guard-1","run_id":7}',
            encoding="utf-8",
        )

    with pytest.raises(ControlPlaneRefusedError):
        ibkr_lifecycle_capability(tmp_path, SID).retire_bot(
            now_ms=1_780_000_000_001,
            updated_by="test",
            reason="identity_refused",
        )
