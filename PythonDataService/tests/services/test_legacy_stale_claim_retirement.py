"""Proof seams for #1019's append-only legacy sidecar retirement cure."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import IbkrAccountSummary, IbkrConnectionHealth, IbkrPositionsSnapshot
from app.engine.live.account_artifacts import read_account_events
from app.engine.live.account_registry import AccountInstanceBinding, write_account_instance_binding
from app.engine.live.daemon_transport import DaemonResult
from app.engine.live.fleet import compute_fleet_contamination
from app.engine.live.live_state_sidecar import LiveStateEnvelope, LiveStateSidecarRepo, stable_live_state_path
from app.engine.live.run_ledger import LiveRunLedger, write_ledger
from app.services.fleet_contamination import collect_fleet_position_explanations
from app.services.legacy_stale_claim_retirement import (
    LEGACY_STALE_CLAIM_RETIRED_EVENT,
    LegacyStaleClaimRetirementError,
    LegacyStaleClaimRetirementService,
)

_ACCOUNT_ID = "DUM284968"
_RUN_ID = "legacy-run"
_SID = "legacy-spy"
_NAMESPACE = "learn-ai/legacy-spy/v1"
_NOW_MS = 1_780_000_002_000


def _truth() -> object:
    return compose_account_truth(
        health=IbkrConnectionHealth(
            mode="paper",
            host="127.0.0.1",
            port=4002,
            client_id=7,
            connected=True,
            account_id=_ACCOUNT_ID,
            is_paper=True,
            fetched_at_ms=_NOW_MS,
            connection_state="connected",
            last_transition_ms=_NOW_MS,
        ),
        account_instance_bindings=[],
        account_recovery_state=AccountRecoveryState.clear(_ACCOUNT_ID),
        account=IbkrAccountSummary(
            account_id=_ACCOUNT_ID,
            is_paper=True,
            base_currency="USD",
            fetched_at_ms=_NOW_MS,
        ),
        positions_snapshot=IbkrPositionsSnapshot(
            account_id=_ACCOUNT_ID,
            is_paper=True,
            positions=[],
            fetched_at_ms=_NOW_MS,
        ),
        open_orders=[],
        completed_orders=[],
        executions=[],
        generated_at_ms=_NOW_MS,
    )


def _seed_claim(
    root: Path,
    *,
    strategy_instance_id: str = _SID,
    run_id: str = _RUN_ID,
    symbol: str = "SPY",
    binding_state: str | None = "RETIRED",
) -> None:
    namespace = f"learn-ai/{strategy_instance_id}/v1"
    write_ledger(
        root / "live_runs" / run_id / "run_ledger.json",
        LiveRunLedger(
            run_id=run_id,
            code_sha="a" * 40,
            strategy_instance_id=strategy_instance_id,
            strategy_spec_path="spec.json",
            strategy_spec_sha256="b" * 64,
            qc_audit_copy_path="audit.py",
            qc_audit_copy_sha256="c" * 64,
            qc_cloud_backtest_id="qc-1",
            account_id=_ACCOUNT_ID,
            start_date_ms=_NOW_MS - 10_000,
            live_config={},
            created_at_ms=_NOW_MS - 10_000,
        ),
    )
    LiveStateSidecarRepo(
        stable_live_state_path(root, strategy_instance_id), trusted_root=root / "live_state"
    ).write(
        LiveStateEnvelope(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            bot_order_namespace=namespace,
            ib_client_id=7,
            expected_position_by_symbol={symbol: 1},
            last_processed_bar_ms=1,
            last_artifact_flush_ms=1,
        )
    )
    if binding_state is not None:
        write_account_instance_binding(
            root,
            AccountInstanceBinding(
                account_id=_ACCOUNT_ID,
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                bot_order_namespace=namespace,
                lifecycle_state=binding_state,  # type: ignore[arg-type]
                recorded_at_ms=_NOW_MS - 5_000,
                source="test",
            ),
        )


async def _dead_process(_run_id: str) -> tuple[DaemonResult, dict]:
    return DaemonResult.connected(), {"state": "exited", "run_id": _run_id}


async def _live_process(_run_id: str) -> tuple[DaemonResult, dict]:
    return DaemonResult.connected(), {"state": "running", "run_id": _run_id}


async def test_retire_refuses_live_process(tmp_path: Path) -> None:
    _seed_claim(tmp_path)
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    with pytest.raises(LegacyStaleClaimRetirementError, match="LEGACY_CLAIM_RUN_PROCESS_LIVE"):
        await service.retire(
            account_id=_ACCOUNT_ID,
            strategy_instance_id=_SID,
            run_id=_RUN_ID,
            symbol="SPY",
            requested_by="test.operator",
            account_truth=_truth(),
            fetch_run_process=_live_process,
        )


async def test_retire_refuses_active_binding(tmp_path: Path) -> None:
    _seed_claim(tmp_path, binding_state="ACTIVE")
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    with pytest.raises(LegacyStaleClaimRetirementError, match="LEGACY_CLAIM_BINDING_ACTIVE"):
        await service.retire(
            account_id=_ACCOUNT_ID,
            strategy_instance_id=_SID,
            run_id=_RUN_ID,
            symbol="SPY",
            requested_by="test.operator",
            account_truth=_truth(),
            fetch_run_process=_dead_process,
        )


async def test_retire_records_receipt_and_excludes_only_retired_legacy_claim(tmp_path: Path) -> None:
    _seed_claim(tmp_path)
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    receipt = await service.retire(
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        run_id=_RUN_ID,
        symbol="SPY",
        requested_by="test.operator",
        account_truth=_truth(),
        fetch_run_process=_dead_process,
    )

    assert receipt.symbol == "SPY"
    events = read_account_events(tmp_path, _ACCOUNT_ID)
    event = next(event for event in events if event["event_type"] == LEGACY_STALE_CLAIM_RETIRED_EVENT)
    assert event["receipt_id"] == receipt.receipt_id
    assert event["ts_ms"] == _NOW_MS
    assert collect_fleet_position_explanations(tmp_path / "live_runs") == {}


async def test_retiring_four_dum284968_shaped_legacy_claims_clears_flat_broker_contamination(
    tmp_path: Path,
) -> None:
    """Regression for the four protected DUM284968 acceptance-fixture claims."""

    claims = (
        ("depval-spy-jul8", "DEPVALSPYJUL8", "SPY"),
        ("june-25-spy", "JUNE-25", "SPY"),
        ("depval-tsla-jul7", "tsladepvaljul7", "TSLA"),
        ("jun26-tsla", "JUN26TSLA", "TSLA"),
    )
    for strategy_instance_id, run_id, symbol in claims:
        _seed_claim(
            tmp_path,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            symbol=symbol,
        )
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    before = compute_fleet_contamination(
        {"SPY": 0, "TSLA": 0},
        collect_fleet_position_explanations(tmp_path / "live_runs"),
    )
    assert before["residual"] == {"SPY": -2, "TSLA": -2}

    for strategy_instance_id, run_id, symbol in claims:
        await service.retire(
            account_id=_ACCOUNT_ID,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            symbol=symbol,
            requested_by="test.operator",
            account_truth=_truth(),
            fetch_run_process=_dead_process,
        )

    after = compute_fleet_contamination(
        {"SPY": 0, "TSLA": 0},
        collect_fleet_position_explanations(tmp_path / "live_runs"),
    )
    assert after["verdict"] == "clean"
    assert after["residual"] == {}
