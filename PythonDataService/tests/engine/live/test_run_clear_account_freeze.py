"""CLI coverage for clearing account-scoped freeze evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.live.account_artifacts import (
    AccountAuditedOverride,
    AccountFreezeEvidence,
    AccountRecoveryProof,
    read_account_events,
    read_account_freeze,
    write_account_freeze,
)
from app.engine.live.run import main
from app.schemas.live_runs import GateResult

ACCOUNT_ID = "DU123456"


def _write_payload(path: Path, payload: object) -> None:
    if hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    path.write_text(json.dumps(data), encoding="utf-8")


def _freeze() -> AccountFreezeEvidence:
    return AccountFreezeEvidence(
        account_id=ACCOUNT_ID,
        reason="watchdog.flatten_failed",
        source="watchdog_halt_executor",
        recorded_at_ms=1_700_000_000_000,
        operator_next_step="CHECK_IBKR",
    )


def _gate_result(*, status: str = "pass") -> GateResult:
    return GateResult(
        gate_id="account.unresolved_exposure",
        status=status,
        source="account_recovery",
        operator_reason="account recovery proof",
        operator_next_step="GATE_PASSING",
        evidence_at_ms=1_700_000_010_000,
    )


def _recovery_proof(
    *,
    reconciliation_result: str = "clean",
    final_gate_status: str = "pass",
) -> AccountRecoveryProof:
    return AccountRecoveryProof(
        account_id=ACCOUNT_ID,
        recovery_id="recovery-1",
        requested_action="reconcile",
        requested_by="operator",
        broker_evidence={"positions": [], "open_orders": []},
        reconciliation_result=reconciliation_result,
        final_gate_result=_gate_result(status=final_gate_status),
        recorded_at_ms=1_700_000_010_000,
    )


def _audited_override(*, valid_until_ms: int) -> AccountAuditedOverride:
    return AccountAuditedOverride(
        account_id=ACCOUNT_ID,
        override_id="override-1",
        approved_decision="continue",
        reason="operator verified IBKR account is flat",
        approved_by="risk-approver",
        approved_at_ms=1_700_000_000_000,
        valid_until_ms=valid_until_ms,
        prior_evidence={"freeze_reason": "watchdog.flatten_failed"},
        next_reconciliation_step="resume after confirming account monitor is clean",
    )


def test_clear_account_freeze_cli_clears_with_clean_recovery_proof(tmp_path: Path) -> None:
    write_account_freeze(tmp_path, _freeze())
    proof_path = tmp_path / "recovery-proof.json"
    _write_payload(proof_path, _recovery_proof())

    rc = main(
        [
            "clear-account-freeze",
            "--artifacts-root",
            str(tmp_path),
            "--recovery-proof-json",
            str(proof_path),
            "--confirm",
        ]
    )

    assert rc == 0
    assert read_account_freeze(tmp_path, ACCOUNT_ID) is None
    event_types = [event["event_type"] for event in read_account_events(tmp_path, ACCOUNT_ID)]
    assert event_types == [
        "account_freeze_recorded",
        "account_recovery_proof_recorded",
        "account_freeze_cleared",
    ]


def test_clear_account_freeze_cli_clears_with_fresh_audited_override(tmp_path: Path) -> None:
    write_account_freeze(tmp_path, _freeze())
    override_path = tmp_path / "audited-override.json"
    _write_payload(override_path, _audited_override(valid_until_ms=9_223_372_036_854_775_807))

    rc = main(
        [
            "clear-account-freeze",
            "--artifacts-root",
            str(tmp_path),
            "--audited-override-json",
            str(override_path),
            "--confirm",
        ]
    )

    assert rc == 0
    assert read_account_freeze(tmp_path, ACCOUNT_ID) is None
    events = read_account_events(tmp_path, ACCOUNT_ID)
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "account_freeze_recorded",
        "account_audited_override_recorded",
        "account_freeze_cleared",
    ]
    assert events[1]["override_id"] == "override-1"
    assert events[1]["approved_decision"] == "continue"
    assert events[2]["cleared_source"] == "account_audited_override"


def test_clear_account_freeze_cli_requires_confirm(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    write_account_freeze(tmp_path, _freeze())
    proof_path = tmp_path / "recovery-proof.json"
    _write_payload(proof_path, _recovery_proof())

    rc = main(
        [
            "clear-account-freeze",
            "--artifacts-root",
            str(tmp_path),
            "--recovery-proof-json",
            str(proof_path),
        ]
    )

    assert rc == 2
    assert "REFUSED" in capsys.readouterr().err
    assert read_account_freeze(tmp_path, ACCOUNT_ID) is not None


def test_clear_account_freeze_cli_refuses_stale_override(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    write_account_freeze(tmp_path, _freeze())
    override_path = tmp_path / "audited-override.json"
    _write_payload(override_path, _audited_override(valid_until_ms=1))

    rc = main(
        [
            "clear-account-freeze",
            "--artifacts-root",
            str(tmp_path),
            "--audited-override-json",
            str(override_path),
            "--confirm",
        ]
    )

    assert rc == 2
    assert "stale" in capsys.readouterr().err
    assert read_account_freeze(tmp_path, ACCOUNT_ID) is not None
    event_types = [event["event_type"] for event in read_account_events(tmp_path, ACCOUNT_ID)]
    assert event_types == ["account_freeze_recorded"]


def test_clear_account_freeze_cli_refuses_contradictory_recovery_proof(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    write_account_freeze(tmp_path, _freeze())
    proof_path = tmp_path / "recovery-proof.json"
    _write_payload(
        proof_path,
        _recovery_proof(reconciliation_result="contradicted", final_gate_status="freeze"),
    )

    rc = main(
        [
            "clear-account-freeze",
            "--artifacts-root",
            str(tmp_path),
            "--recovery-proof-json",
            str(proof_path),
            "--confirm",
        ]
    )

    assert rc == 2
    assert "clean reconciliation" in capsys.readouterr().err
    assert read_account_freeze(tmp_path, ACCOUNT_ID) is not None
    event_types = [event["event_type"] for event in read_account_events(tmp_path, ACCOUNT_ID)]
    assert event_types == ["account_freeze_recorded"]
