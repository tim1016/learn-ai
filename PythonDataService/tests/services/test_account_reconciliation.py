"""Tests for account-level reconciliation receipts."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrConnectionHealth,
    IbkrOrderEvent,
    IbkrOrderSpec,
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_artifacts import (
    RESTART_INTENSITY_SOURCE,
    AccountArtifactError,
    AccountClerkLease,
    AccountFreezeEvidence,
    AccountInstanceBinding,
    AccountOwnerGeneration,
    account_artifacts_root,
    advance_account_clerk_generation,
    append_account_event,
    read_account_events,
    read_account_freeze,
    write_account_clerk_lease,
    write_account_freeze,
    write_account_instance_binding,
    write_account_owner_generation,
)
from app.engine.live.account_clerk_journal import AccountClerkJournal
from app.engine.live.account_observation_lease import assess_account_observation_lease
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.schemas.account_reconciliation import AccountConditionType, AccountCureAction
from app.schemas.account_truth import (
    AccountTruthEvidenceGap,
    AccountTruthExecutionRow,
    AccountTruthFactOwner,
    AccountTruthOwnerClass,
)
from app.services import account_reconciliation as account_reconciliation_module
from app.services.account_reconciliation import AccountReconciliationService


def _health(*, account_id: str = "DU1234567", connected: bool = True) -> IbkrConnectionHealth:
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=7,
        connected=connected,
        account_id=account_id,
        is_paper=True,
        fetched_at_ms=1_780_000_000_000,
        connection_state="connected" if connected else "disconnected",
        last_transition_ms=1_780_000_000_000,
        connection_lost=not connected,
    )


def _account_summary() -> IbkrAccountSummary:
    return IbkrAccountSummary(
        account_id="DU1234567",
        is_paper=True,
        base_currency="USD",
        net_liquidation=100_000.0,
        buying_power=50_000.0,
        fetched_at_ms=1_780_000_000_000,
    )


def _position(*, symbol: str = "SPY", quantity: float = 1.0) -> IbkrPosition:
    return IbkrPosition(
        account_id="DU1234567",
        con_id=756733,
        symbol=symbol,
        sec_type="STK",
        quantity=quantity,
        avg_cost=500.0,
        fetched_at_ms=1_780_000_000_400,
    )


def _positions_snapshot(positions: list[IbkrPosition] | None = None) -> IbkrPositionsSnapshot:
    return IbkrPositionsSnapshot(
        account_id="DU1234567",
        is_paper=True,
        positions=positions or [],
        fetched_at_ms=1_780_000_000_400,
    )


def _truth(
    *,
    account_id: str = "DU1234567",
    connected: bool = True,
    positions: list[IbkrPosition] | None = None,
    evidence_gaps: list[AccountTruthEvidenceGap] | None = None,
):
    return compose_account_truth(
        health=_health(account_id=account_id, connected=connected),
        account_instance_bindings=[],
        account_recovery_state=AccountRecoveryState.clear(account_id),
        account=_account_summary(),
        positions_snapshot=_positions_snapshot(positions),
        open_orders=[],
        completed_orders=[],
        executions=[],
        evidence_gaps=evidence_gaps or [],
        generated_at_ms=1_780_000_001_000,
    )


def _binding() -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id="DU1234567",
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace="learn-ai/bot-a/v1",
        lifecycle_state="ACTIVE",
        recorded_at_ms=1_780_000_000_000,
        source="test",
    )


def _write_accepting_clerk_generation(root: Path, *, generation: int) -> None:
    for offset in range(generation):
        advance_account_clerk_generation(
            root,
            "DU1234567",
            phase="accepting",
            recorded_at_ms=1_780_000_002_000 + offset,
            source="test",
        )
    write_account_clerk_lease(
        root,
        AccountClerkLease(
            account_id="DU1234567",
            generation=generation,
            pid=123,
            ibkr_client_id=51,
            status="RUNNING",
            started_at_ms=1_780_000_002_000,
            renewed_at_ms=1_780_000_002_000,
            valid_until_ms=1_780_000_062_000,
        ),
    )


def _retired_binding(
    *,
    sid: str = "bot-a",
    run_id: str = "run-a",
    source: str = "host_daemon.process_crashed",
    recorded_at_ms: int = 1_780_000_002_500,
) -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id="DU1234567",
        strategy_instance_id=sid,
        run_id=run_id,
        bot_order_namespace=f"learn-ai/{sid}/v1",
        lifecycle_state="RETIRED",
        recorded_at_ms=recorded_at_ms,
        source=source,
    )


def _execution(
    *,
    exec_id: str,
    observed_at_ms: int,
    owner_class: AccountTruthOwnerClass = "bot",
) -> AccountTruthExecutionRow:
    return AccountTruthExecutionRow(
        account_id="DU1234567",
        exec_id=exec_id,
        order_id=17,
        observed_at_ms=observed_at_ms,
        owner=AccountTruthFactOwner(
            owner_class=owner_class,
            owner_key="bot-a" if owner_class == "bot" else "unclaimed",
            owner_label="Bot bot-a" if owner_class == "bot" else "Foreign or unclaimed",
            evidence_tier="bot_order_ref" if owner_class == "bot" else "foreign_or_unclaimed",
            owner_binding_state="ACTIVE" if owner_class == "bot" else "UNKNOWN",
            evidence_label="Bot order reference" if owner_class == "bot" else "Unclaimed",
            severity="ok" if owner_class == "bot" else "critical",
        ),
        headline="Bot execution" if owner_class == "bot" else "Unclaimed execution",
        detail="Broker execution observed.",
    )


def test_write_receipt_wraps_clean_account_truth(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=60_000)

    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )

    assert receipt.state == "CLEAN"
    assert receipt.exposure_resolution == "flat"
    assert receipt.account_truth_verdict == "clean"
    assert receipt.final_gate_result.status == "pass"
    assert receipt.expires_at_ms == 1_780_000_062_000
    assert service.read_latest_receipt("DU1234567") == receipt


def test_default_receipt_ttl_is_five_minutes(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)

    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )

    assert receipt.ttl_ms == 300_000
    assert receipt.expires_at_ms == 1_780_000_302_000


def test_new_execution_invalidates_prior_receipt_until_reconciled(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    updated_truth = _truth().model_copy(
        update={
            "generated_at_ms": 1_780_000_003_000,
            "executions": [_execution(exec_id="exec-1", observed_at_ms=1_780_000_002_500)],
        }
    )

    assert service.observe_account_truth(updated_truth, now_ms=1_780_000_003_000) is None

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_500)
    assert triage.account_reconciliation_receipt == receipt
    assert triage.account_reconciliation_valid_until_ms == 1_780_000_003_000
    assert triage.overall_gate_result.status == "unknown"
    assert triage.conditions[0].title == "Account evidence changed"
    assert "exec-1" in triage.conditions[0].detail


def test_account_truth_observer_renews_observation_lease(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    _write_accepting_clerk_generation(tmp_path, generation=1)

    service.observe_account_truth(_truth(), now_ms=1_780_000_002_000)
    service.observe_account_truth(_truth(), now_ms=1_780_000_003_000)

    assessment = assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_003_001,
    )
    assert assessment.state == "VERIFIED"
    lease_events = [
        event
        for event in read_account_events(tmp_path, "DU1234567")
        if event["event_type"].startswith("account_observation_lease_")
    ]
    assert [event["event_type"] for event in lease_events] == [
        "account_observation_lease_verified"
    ]


def test_account_truth_observer_refuses_clean_truth_without_active_clerk(
    tmp_path: Path,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)

    service.observe_account_truth(_truth(), now_ms=1_780_000_002_000)

    assessment = assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_002_001,
    )
    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_CLERK_GENERATION_CHANGED"


def test_account_truth_observer_revokes_observation_lease_on_unattributed_exposure(
    tmp_path: Path,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    _write_accepting_clerk_generation(tmp_path, generation=1)
    service.observe_account_truth(_truth(), now_ms=1_780_000_002_000)

    service.observe_account_truth(
        _truth(positions=[_position(quantity=1.0)]),
        now_ms=1_780_000_003_000,
    )

    assessment = assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_003_001,
    )
    assert assessment.state == "REVOKED"
    assert assessment.reason_code.startswith("ACCOUNT_TRUTH_")

    service.observe_account_truth(_truth(), now_ms=1_780_000_004_000)

    recovered = assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_004_001,
    )
    assert recovered.state == "VERIFIED"
    lease_events = [
        event["event_type"]
        for event in read_account_events(tmp_path, "DU1234567")
        if event["event_type"].startswith("account_observation_lease_")
    ]
    assert lease_events == [
        "account_observation_lease_verified",
        "account_observation_lease_revoked",
        "account_observation_lease_verified",
    ]


def test_account_truth_refresh_failure_revokes_observation_lease(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    _write_accepting_clerk_generation(tmp_path, generation=1)
    service.observe_account_truth(_truth(), now_ms=1_780_000_002_000)

    service.observe_account_truth_failure(
        account_id="DU1234567",
        detail="broker sweep timed out",
        attempted_at_ms=1_780_000_003_000,
    )

    assessment = assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_003_001,
    )
    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_TRUTH_REFRESH_FAILED"

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_001)

    assert triage.account_observation.state == "REVOKED"
    assert triage.account_observation.reason_line == "broker sweep timed out"
    assert [event.state for event in triage.account_observation.history] == [
        "VERIFIED",
        "REVOKED",
    ]


def test_account_observation_triage_bounds_long_reason_lines(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    _write_accepting_clerk_generation(tmp_path, generation=1)
    long_detail = "broker sweep timed out: " + ("x" * 700)
    service.observe_account_truth(_truth(), now_ms=1_780_000_002_000)

    service.observe_account_truth_failure(
        account_id="DU1234567",
        detail=long_detail,
        attempted_at_ms=1_780_000_003_000,
    )

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_001)

    assert len(triage.account_observation.reason_line) == 512
    assert triage.account_observation.reason_line.endswith("...")
    assert len(triage.account_observation.history[-1].reason_line) == 512
    assert triage.account_observation.history[-1].reason_line.endswith("...")
    assert assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_003_001,
    ).reason == long_detail


def test_account_truth_observer_revokes_when_clerk_generation_changes_during_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_accepting_clerk_generation(tmp_path, generation=3)
    original_read = account_reconciliation_module.read_active_accepting_account_clerk_generation
    read_count = 0

    def read_then_advance_generation(*args, **kwargs):
        nonlocal read_count
        clerk = original_read(*args, **kwargs)
        read_count += 1
        if read_count == 1:
            advanced = advance_account_clerk_generation(
                tmp_path,
                "DU1234567",
                phase="accepting",
                recorded_at_ms=1_780_000_002_001,
                source="test",
            )
            write_account_clerk_lease(
                tmp_path,
                AccountClerkLease(
                    account_id="DU1234567",
                    generation=advanced.generation,
                    pid=123,
                    ibkr_client_id=51,
                    status="RUNNING",
                    started_at_ms=1_780_000_002_001,
                    renewed_at_ms=1_780_000_002_001,
                    valid_until_ms=1_780_000_062_001,
                ),
            )
        return clerk

    monkeypatch.setattr(
        account_reconciliation_module,
        "read_active_accepting_account_clerk_generation",
        read_then_advance_generation,
    )
    service = AccountReconciliationService(artifacts_root=tmp_path)

    service.observe_account_truth(_truth(), now_ms=1_780_000_002_000)

    assessment = assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_002_001,
    )
    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_CLERK_GENERATION_CHANGED"


def test_account_truth_observer_uses_pre_collection_clerk_generation_fence(
    tmp_path: Path,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    _write_accepting_clerk_generation(tmp_path, generation=4)

    service.observe_account_truth(
        _truth(),
        clerk_generation_before=(3, "accepting"),
        clerk_generation_captured=True,
        now_ms=1_780_000_002_000,
    )

    assessment = assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_002_001,
    )
    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_CLERK_GENERATION_CHANGED"


def test_account_truth_observer_renews_when_captured_clerk_generation_is_stable(
    tmp_path: Path,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    _write_accepting_clerk_generation(tmp_path, generation=3)

    service.observe_account_truth(
        _truth(),
        clerk_generation_before=(3, "accepting"),
        clerk_generation_captured=True,
        now_ms=1_780_000_002_000,
    )

    assessment = assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_002_001,
    )
    assert assessment.state == "VERIFIED"


def test_account_truth_observer_revokes_when_freeze_evidence_is_unreadable(
    tmp_path: Path,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    _write_accepting_clerk_generation(tmp_path, generation=1)
    service.observe_account_truth(_truth(), now_ms=1_780_000_002_000)
    account_root = account_artifacts_root(tmp_path, "DU1234567")
    (account_root / "unresolved_exposure.flag").write_text("{not-json", encoding="utf-8")

    service.observe_account_truth(_truth(), now_ms=1_780_000_003_000)

    assessment = assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_003_001,
    )
    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_FREEZE_UNREADABLE"


def test_account_truth_observer_revokes_when_lease_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    _write_accepting_clerk_generation(tmp_path, generation=1)
    service.observe_account_truth(_truth(), now_ms=1_780_000_002_000)
    monkeypatch.setattr(
        account_reconciliation_module,
        "assess_account_truth",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("assessment failed")),
    )

    service.observe_account_truth(_truth(), now_ms=1_780_000_003_000)

    assessment = assess_account_observation_lease(
        tmp_path,
        "DU1234567",
        now_ms=1_780_000_003_001,
    )
    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_OBSERVATION_LEASE_UPDATE_FAILED"


def test_observation_lease_revoked_transition_uses_revoked_fallback_copy(
    tmp_path: Path,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    repo = account_reconciliation_module.AccountObservationLeaseRepo(tmp_path)
    revoked = repo.revoke(
        account_id="DU1234567",
        reason_code="ACCOUNT_TRUTH_NOT_PROVEN",
        detail="",
        now_ms=1_780_000_002_000,
    )

    service._append_observation_lease_transition(before=None, after=revoked)

    event = read_account_events(tmp_path, "DU1234567")[-1]
    assert event["event_type"] == "account_observation_lease_revoked"
    assert event["reason_line"] == "Account verification was revoked."


def test_auto_reconcile_replaces_receipt_after_new_bot_execution(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    service.update_automation_policy(
        account_id="DU1234567",
        enabled=True,
        updated_by="test.operator",
        now_ms=1_780_000_002_100,
    )
    updated_truth = _truth().model_copy(
        update={
            "generated_at_ms": 1_780_000_003_000,
            "executions": [_execution(exec_id="exec-1", observed_at_ms=1_780_000_002_500)],
        }
    )

    replacement = service.observe_account_truth(updated_truth, now_ms=1_780_000_003_000)

    assert replacement is not None
    assert replacement.generated_at_ms == 1_780_000_003_000
    assert [row.exec_id for row in replacement.account_truth.executions] == ["exec-1"]
    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_500)
    assert triage.overall_gate_result.status == "pass"
    assert triage.verdict.state == "CLEAN"
    assert triage.verdict.primary_move is None
    assert triage.verdict.operator_attention_count == 0
    assert triage.account_reconciliation_valid_until_ms == replacement.expires_at_ms
    assert triage.conditions == []


def test_auto_reconcile_does_not_bless_unclaimed_execution(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    original = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    service.update_automation_policy(
        account_id="DU1234567",
        enabled=True,
        updated_by="test.operator",
        now_ms=1_780_000_002_100,
    )
    updated_truth = _truth().model_copy(
        update={
            "generated_at_ms": 1_780_000_003_000,
            "executions": [
                _execution(
                    exec_id="exec-foreign",
                    observed_at_ms=1_780_000_002_500,
                    owner_class="foreign_or_unclaimed",
                )
            ],
        }
    )

    assert service.observe_account_truth(updated_truth, now_ms=1_780_000_003_000) is None
    assert service.read_latest_receipt("DU1234567") == original
    assert service.triage(
        account_id="DU1234567",
        now_ms=1_780_000_003_500,
    ).overall_gate_result.status == "unknown"


def test_enabling_auto_reconcile_retries_an_invalidated_bot_execution(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    updated_truth = _truth().model_copy(
        update={
            "generated_at_ms": 1_780_000_003_000,
            "executions": [_execution(exec_id="exec-1", observed_at_ms=1_780_000_002_500)],
        }
    )
    assert service.observe_account_truth(updated_truth, now_ms=1_780_000_003_000) is None
    service.update_automation_policy(
        account_id="DU1234567",
        enabled=True,
        updated_by="test.operator",
        now_ms=1_780_000_003_100,
    )

    replacement = service.observe_account_truth(updated_truth, now_ms=1_780_000_004_000)

    assert replacement is not None
    assert replacement.generated_at_ms == 1_780_000_004_000
    assert service.triage(
        account_id="DU1234567",
        now_ms=1_780_000_004_500,
    ).overall_gate_result.status == "pass"


def test_automation_policy_is_durable_and_defaults_disabled(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)

    default = service.read_automation_policy("DU1234567")
    updated = service.update_automation_policy(
        account_id="du1234567",
        enabled=True,
        updated_by="test.operator",
        now_ms=1_780_000_002_000,
    )

    assert default.enabled is False
    assert updated.account_id == "DU1234567"
    assert updated.enabled is True
    assert service.read_automation_policy("DU1234567") == updated


def test_write_receipt_uses_canonical_account_id_for_storage(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=60_000)

    receipt = service.write_receipt(
        requested_account_id="du1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )

    assert receipt.account_id == "DU1234567"
    assert receipt.requested_account_id == "DU1234567"
    assert service.read_latest_receipt("DU1234567") == receipt
    assert service.read_latest_receipt("du1234567") == receipt


def test_receipt_refuses_clean_when_broker_exposure_is_not_flat(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=60_000)

    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(positions=[_position(quantity=1.0)]),
        now_ms=1_780_000_002_000,
    )

    assert receipt.state == "NOT_PROVEN"
    assert receipt.exposure_resolution == "unresolved"
    assert receipt.final_gate_result.status == "block"
    assert receipt.final_gate_result.operator_next_step == "RESOLVE_EXPOSURE"
    assert "exposure_resolution=unresolved" in receipt.final_gate_result.operator_reason


def test_receipt_blocks_connected_account_mismatch(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)

    receipt = service.write_receipt(
        requested_account_id="DU7654321",
        account_truth=_truth(account_id="DU1234567"),
        now_ms=1_780_000_002_000,
    )

    assert receipt.state == "NOT_PROVEN"
    assert receipt.final_gate_result.status == "block"
    assert "does not match requested account" in receipt.final_gate_result.operator_reason


def test_receipt_blocks_broker_liveness_failure(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)

    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(connected=False),
        now_ms=1_780_000_002_000,
    )

    assert receipt.state == "NOT_PROVEN"
    assert receipt.account_truth_verdict == "not_proven"
    assert receipt.final_gate_result.status == "block"


def test_receipt_rechecks_critical_source_freshness(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)

    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_061_001,
    )

    assert receipt.state == "NOT_PROVEN"
    assert receipt.account_truth_verdict == "clean"
    assert receipt.final_gate_result.status == "block"
    assert receipt.final_gate_result.operator_next_step == "REFRESH_ACCOUNT_TRUTH"
    assert "hard freshness threshold" in receipt.final_gate_result.operator_reason


def test_receipt_bounds_long_evidence_gap_details(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    long_message = "registry failure: " + ("x" * 700)

    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(
            evidence_gaps=[
                AccountTruthEvidenceGap(
                    source="instance_registry",
                    severity="critical",
                    message=long_message,
                )
            ]
        ),
        now_ms=1_780_000_002_000,
    )

    gap_ref = next(ref for ref in receipt.evidence_refs if ref.source == "account_truth.evidence_gap")
    assert gap_ref.detail is not None
    assert len(gap_ref.detail) == 512
    assert gap_ref.detail.endswith("...")


def test_triage_without_receipt_is_unknown(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_002_000)

    assert triage.overall_gate_result.status == "unknown"
    assert triage.verdict.state == "NOT_PROVEN"
    assert triage.verdict.primary_move is not None
    assert triage.gate_rows[0].gate_id == "account.reconciliation"
    assert triage.gate_rows[0].status == "unknown"
    assert [(row.condition_type, row.cure_action) for row in triage.conditions] == [
        ("evidence_stale", "reconcile_now")
    ]


def test_triage_authors_only_an_exact_single_instrument_recovery_flatten_move(tmp_path: Path) -> None:
    binding = _retired_binding()
    write_account_instance_binding(tmp_path, binding)
    write_account_owner_generation(
        tmp_path,
        AccountOwnerGeneration(
            account_id="DU1234567",
            generation=7,
            phase="accepting",
            recorded_at_ms=1_780_000_002_000,
            source="test",
        ),
    )
    intent_id = "source-intent"
    order_ref = f"{binding.bot_order_namespace}:{intent_id}"
    source_intent = AccountOwnerSubmitIntent(
        trace_id="source-trace",
        account_id="DU1234567",
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        bot_order_namespace=binding.bot_order_namespace,
        intent_id=intent_id,
        order_ref=order_ref,
        intent_kind="ORDER",
        order_spec=IbkrOrderSpec(
            symbol="SPY",
            sec_type="STK",
            action="BUY",
            quantity=2,
            order_type="MKT",
            confirm_paper=True,
            client_order_id="source-order",
            order_ref=order_ref,
        ).model_dump(mode="json"),
        owner_generation=7,
        created_at_ms=1_780_000_002_000,
    )
    journal = AccountClerkJournal(artifacts_root=tmp_path, account_id="DU1234567", now_ms=lambda: 1_780_000_002_000)
    journal.record_intent(source_intent, validate_intent=lambda _: None)
    journal.record_broker_event(
        IbkrOrderEvent(
            account_id="DU1234567",
            order_id=1,
            event_type="fill",
            order_ref=order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=2,
            exec_id="recovery-candidate-fill",
            ts_ms=1_780_000_002_001,
        )
    )

    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(positions=[_position(quantity=2)]),
        now_ms=1_780_000_003_000,
    )
    triage = service.triage(
        account_id="DU1234567",
        now_ms=1_780_000_003_100,
    )

    [candidate] = triage.recovery_flatten_candidates
    assert candidate.intent.intent_kind == "RECOVERY_FLATTEN"
    assert candidate.intent.owner_generation == 7
    assert candidate.intent.order_spec["action"] == "SELL"
    assert candidate.intent.order_spec["quantity"] == 2
    assert candidate.confirmation.confirm_label == "Submit recovery flatten"
    [blocker] = [
        value
        for value in triage.operator_blockers
        if value.primary_move is not None
        and value.primary_move.action.kind == "confirm_in_form"
        and value.primary_move.action.anchor == "account-recovery-flatten-action"
    ]
    assert blocker.primary_move is not None
    assert blocker.primary_move.target == candidate.intent.intent_id


def test_triage_verdict_freeze_precedes_missing_proof(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="Unresolved broker exposure requires review.",
            source="account_reconciliation",
            recorded_at_ms=1_780_000_002_100,
            operator_next_step="CHECK_IBKR",
        ),
    )

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_002_200)

    assert triage.verdict.state == "FROZEN"
    assert triage.verdict.primary_move is not None


def test_triage_marks_corrupt_instance_registry_unknown(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    account_root = account_artifacts_root(tmp_path, "DU1234567")
    account_root.mkdir(parents=True, exist_ok=True)
    (account_root / "instance_registry.jsonl").write_text("{not-json\n", encoding="utf-8")

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_000)

    assert triage.overall_gate_result.status == "unknown"
    registry_row = next(row for row in triage.gate_rows if row.gate_id == "account.instance_registry")
    assert registry_row.status == "unknown"
    assert registry_row.operator_next_step == "REPAIR_ACCOUNT_INSTANCE_REGISTRY"


def test_triage_marks_expired_receipt_unknown(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=1_000)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_001)

    assert triage.overall_gate_result.status == "unknown"
    assert triage.gate_rows[0].title == "Account reconciliation receipt stale"
    assert [(row.condition_type, row.cure_action) for row in triage.conditions] == [
        ("evidence_stale", "reconcile_now")
    ]


def test_triage_freeze_dominates_clean_receipt(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    write_account_instance_binding(tmp_path, _binding())
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_002_500,
            operator_next_step="CHECK_IBKR",
        ),
    )

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_000)

    assert triage.overall_gate_result.status == "freeze"
    assert triage.verdict.state == "FROZEN"
    assert triage.verdict.primary_move is not None
    assert triage.clear_freeze_actionable is False
    assert [bot.strategy_instance_id for bot in triage.affected_bots] == ["bot-a"]
    assert {row.gate_id for row in triage.gate_rows} == {
        "account.reconciliation",
        "account.unresolved_exposure",
    }
    assert [(row.condition_type, row.cure_action) for row in triage.conditions] == [
        ("exposure_freeze", "resolve_exposure")
    ]


def test_triage_marks_non_exposure_freeze_with_clear_action(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_003_000,
    )
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            reason="restart_intensity.threshold_breached",
            source=RESTART_INTENSITY_SOURCE,
            recorded_at_ms=1_780_000_002_500,
            operator_next_step="STOP_RESTARTING_AND_RECOVER_ACCOUNT",
        ),
    )

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_100)

    assert triage.clear_freeze_actionable is True
    assert [(row.condition_type, row.cure_action) for row in triage.conditions] == [
        ("account_freeze", "clear_freeze")
    ]


def test_triage_defaults_untyped_freeze_to_clearable_account_freeze(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_003_000,
    )
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_002_500,
            operator_next_step="CHECK_IBKR",
        ),
    )

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_100)

    assert triage.clear_freeze_actionable is True
    assert [(row.condition_type, row.cure_action) for row in triage.conditions] == [
        ("account_freeze", "clear_freeze")
    ]
    with pytest.raises(AccountArtifactError, match="only clear an exposure freeze"):
        service.accept_exposure_override(
            account_id="DU1234567",
            requested_by="operator",
            reason="Default freeze classification should be account-scoped.",
            now_ms=1_780_000_003_200,
        )


def test_triage_marks_crashed_and_no_status_retired_bots_for_retire_replace(
    tmp_path: Path,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_003_000,
    )
    write_account_instance_binding(
        tmp_path,
        _retired_binding(
            sid="crashed-bot",
            run_id="run-crashed",
            source="host_daemon.process_crashed",
            recorded_at_ms=1_780_000_002_500,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _retired_binding(
            sid="nostatus-bot",
            run_id="run-nostatus",
            source="host_daemon.ended_without_status",
            recorded_at_ms=1_780_000_002_600,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _retired_binding(
            sid="unproven-bot",
            run_id="run-unproven",
            source="host_daemon.boot_liveness_unproven",
            recorded_at_ms=1_780_000_002_700,
        ),
    )

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_100)

    assert [
        (
            row.condition_type,
            row.scope,
            row.owner.owner_id,
            row.owner.lifecycle_state,
            row.cure_action,
        )
        for row in triage.conditions
    ] == [
        ("crashed", "bot", "crashed-bot", "RETIRED", "retire_replace"),
        ("ended_without_status", "bot", "nostatus-bot", "RETIRED", "retire_replace"),
        ("liveness_unproven", "bot", "unproven-bot", "RETIRED", "retire_replace"),
    ]
    assert all(row.severity == "critical" for row in triage.conditions)
    assert triage.overall_gate_result.status == "block"
    assert triage.verdict.state == "NEEDS_ATTENTION"
    assert triage.verdict.primary_move is not None
    assert triage.verdict.primary_move.route == "/broker/accounts/DU1234567"
    assert triage.verdict.primary_move.fragment == "account-desk-recovery-controls"
    assert triage.verdict.operator_attention_count == 3
    duplicate_projection = account_reconciliation_module._account_triage_verdict(
        account_id="DU1234567",
        reconciliation_gate=triage.gate_rows[0],
        gate_rows=triage.gate_rows,
        conditions=[*triage.conditions, *triage.conditions],
        freeze=None,
    )
    assert duplicate_projection.operator_attention_count == 3
    assert triage.summary_headline == "Account recovery needs attention"
    assert "crashed-bot ended from a crash" in triage.summary_detail


def test_triage_closes_terminal_retired_conditions_after_recovery_evidence(
    tmp_path: Path,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_003_000,
    )
    write_account_instance_binding(
        tmp_path,
        _retired_binding(
            sid="crashed-bot",
            run_id="run-crashed",
            source="host_daemon.process_crashed",
            recorded_at_ms=1_780_000_002_500,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _retired_binding(
            sid="nostatus-bot",
            run_id="run-nostatus",
            source="host_daemon.ended_without_status",
            recorded_at_ms=1_780_000_002_600,
        ),
    )
    append_account_event(
        tmp_path,
        "DU1234567",
        {
            "event_type": "account_recovery_proof_recorded",
            "recovery_id": "acct-recovery-DU1234567",
            "recorded_at_ms": 1_780_000_002_700,
        },
    )

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_100)

    assert triage.conditions == []


def test_triage_exposure_freeze_names_retired_owner_when_unambiguous(
    tmp_path: Path,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_003_000,
    )
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_002_500,
            operator_next_step="CHECK_IBKR",
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _retired_binding(
            sid="retired-freezer",
            run_id="run-freeze",
            source="host_daemon.process_halted",
            recorded_at_ms=1_780_000_002_700,
        ),
    )

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_100)

    freeze = next(row for row in triage.conditions if row.condition_type == "exposure_freeze")
    assert freeze.scope == "account"
    assert freeze.owner.owner_type == "bot"
    assert freeze.owner.owner_id == "retired-freezer"
    assert freeze.owner.lifecycle_state == "RETIRED"
    assert freeze.cure_action == "resolve_exposure"


def test_condition_contract_uses_closed_type_and_single_cure_action(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path)
    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_002_000)

    condition_types = set(get_args(AccountConditionType))
    cure_actions = set(get_args(AccountCureAction))
    assert condition_types == {
        "exposure_freeze",
        "account_freeze",
        "evidence_stale",
        "daemon_unreachable",
        "evidence_missing",
        "exit_flatten_failed",
        "exit_lease_stuck",
        "crashed",
        "ended_without_status",
        "liveness_unproven",
        "repeated_unclean_start",
    }
    assert cure_actions == {
        "resolve_exposure",
        "clear_freeze",
        "reconcile_now",
        "prove_evidence",
        "retire_replace",
    }
    assert all(condition.cure_action in cure_actions for condition in triage.conditions)


def test_clear_freeze_refuses_receipt_that_predates_active_freeze(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=60_000)
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_002_500,
            operator_next_step="CHECK_IBKR",
        ),
    )

    with pytest.raises(AccountArtifactError, match="newer than the active freeze"):
        service.clear_freeze_from_latest_receipt(
            account_id="DU1234567",
            requested_by="operator",
            receipt_id=receipt.receipt_id,
            now_ms=1_780_000_003_000,
        )

    assert read_account_freeze(tmp_path, "DU1234567") is not None


def test_clear_freeze_refuses_unresolved_exposure_resolution(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=60_000)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_001_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(positions=[_position(quantity=1.0)]),
        now_ms=1_780_000_002_000,
    )

    with pytest.raises(AccountArtifactError, match="exposure resolution is unresolved"):
        service.clear_freeze_from_latest_receipt(
            account_id="DU1234567",
            requested_by="operator",
            receipt_id=receipt.receipt_id,
            now_ms=1_780_000_003_000,
        )

    assert read_account_freeze(tmp_path, "DU1234567") is not None


def test_clear_freeze_accepts_fresh_flat_receipt_newer_than_freeze(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=60_000)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_001_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )

    response = service.clear_freeze_from_latest_receipt(
        account_id="DU1234567",
        requested_by="operator",
        receipt_id=receipt.receipt_id,
        now_ms=1_780_000_003_000,
    )

    assert response.cleared is True
    assert response.receipt_id == receipt.receipt_id
    assert response.triage.overall_gate_result.status == "pass"
    assert read_account_freeze(tmp_path, "DU1234567") is None


def test_accept_exposure_override_clears_exposure_freeze_with_audit_event(
    tmp_path: Path,
) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=60_000)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_001_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _retired_binding(
            sid="retired-freezer",
            run_id="run-freeze",
            source="host_daemon.process_halted",
            recorded_at_ms=1_780_000_001_100,
        ),
    )

    response = service.accept_exposure_override(
        account_id="DU1234567",
        requested_by="operator",
        reason="Operator verified the exposure belongs to the account.",
        now_ms=1_780_000_002_000,
    )

    assert response.cleared is True
    assert response.cleared_source == "account_audited_override"
    assert response.triage.overall_gate_result.status == "unknown"
    assert read_account_freeze(tmp_path, "DU1234567") is None
    override_event = next(
        event
        for event in read_account_events(tmp_path, "DU1234567")
        if event["event_type"] == "account_audited_override_recorded"
    )
    assert override_event["approved_decision"] == "continue"
    assert override_event["strategy_instance_id"] == "retired-freezer"
    assert override_event["run_id"] == "run-freeze"
    assert override_event["prior_evidence"]["freeze_source"] == "watchdog_halt_executor"


def test_accept_exposure_override_refuses_non_exposure_freeze(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=60_000)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            reason="restart_intensity.threshold_breached",
            source=RESTART_INTENSITY_SOURCE,
            recorded_at_ms=1_780_000_001_000,
            operator_next_step="STOP_RESTARTING_AND_RECOVER_ACCOUNT",
        ),
    )

    with pytest.raises(AccountArtifactError, match="only clear an exposure freeze"):
        service.accept_exposure_override(
            account_id="DU1234567",
            requested_by="operator",
            reason="Wrong cure for this freeze.",
            now_ms=1_780_000_002_000,
        )

    assert read_account_freeze(tmp_path, "DU1234567") is not None
