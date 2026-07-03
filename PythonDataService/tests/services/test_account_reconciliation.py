"""Tests for account-level reconciliation receipts."""

from __future__ import annotations

from pathlib import Path

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import IbkrConnectionHealth
from app.engine.live.account_artifacts import (
    AccountFreezeEvidence,
    AccountInstanceBinding,
    account_artifacts_root,
    write_account_freeze,
    write_account_instance_binding,
)
from app.schemas.account_truth import AccountTruthEvidenceGap
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


def _truth(
    *,
    account_id: str = "DU1234567",
    connected: bool = True,
    evidence_gaps: list[AccountTruthEvidenceGap] | None = None,
):
    return compose_account_truth(
        health=_health(account_id=account_id, connected=connected),
        account_instance_bindings=[],
        account_recovery_state=AccountRecoveryState.clear(account_id),
        account=None,
        positions_snapshot=None,
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


def test_write_receipt_wraps_clean_account_truth(tmp_path: Path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=60_000)

    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )

    assert receipt.state == "CLEAN"
    assert receipt.account_truth_verdict == "clean"
    assert receipt.final_gate_result.status == "pass"
    assert receipt.expires_at_ms == 1_780_000_062_000
    assert service.read_latest_receipt("DU1234567") == receipt


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
    assert triage.gate_rows[0].gate_id == "account.reconciliation"
    assert triage.gate_rows[0].status == "unknown"


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
            reason="restart_intensity.threshold_breached",
            source="account_restart_intensity",
            recorded_at_ms=1_780_000_002_500,
            operator_next_step="STOP_RESTARTING_AND_RECOVER_ACCOUNT",
        ),
    )

    triage = service.triage(account_id="DU1234567", now_ms=1_780_000_003_000)

    assert triage.overall_gate_result.status == "freeze"
    assert [bot.strategy_instance_id for bot in triage.affected_bots] == ["bot-a"]
    assert {row.gate_id for row in triage.gate_rows} == {
        "account.reconciliation",
        "account.unresolved_exposure",
    }
