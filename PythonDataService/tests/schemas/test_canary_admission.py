"""Boundary tests for canary-admission wire timestamps.

The ceiling is the end of year 9999, not the int64 maximum. ``2**63 - 1`` ms is
the year 292 million — never anyone's real bound, only the widest number the
storage type could hold — and it is not representable in a float64, so
publishing it in the OpenAPI contract stated a ceiling one higher than the one
enforced (#1936).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.canary_admission import (
    CanaryActivationEvidence,
    CanaryActivationPlan,
    CanaryAdmissionEvent,
    CanaryRollbackDecision,
)
from app.schemas.signal_program_seal import semantic_payload_hash
from app.utils.session_anchors import MAX_TIMESTAMP_MS

_SHA = "a" * 64


def _evidence_payload(*, qualified_at_ms: int) -> dict[str, object]:
    return {
        "validation_event_id": "event-1",
        "validation_snapshot_sha256": _SHA,
        "program_version": "v1",
        "golden_trace_root": _SHA,
        "running_artifact_digest": _SHA,
        "qualification_receipt_hash": _SHA,
        "qualification_suite": "qualification-suite",
        "qualified_at_ms": qualified_at_ms,
    }


def _plan_payload(*, created_at_ms: int, expires_at_ms: int) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "program_key": "ema_crossover_signal",
        "account_id": "paper-account",
        "actor": "local:test-operator",
        "reason": "Review the exact Paper pairing.",
        "created_at_ms": created_at_ms,
        "expires_at_ms": expires_at_ms,
        "ledger_path": "/tmp/canary-admission.json",
        "expected_ledger_head_hash": None,
        "evidence": _evidence_payload(qualified_at_ms=1),
    }
    identity = semantic_payload_hash(payload)
    return {"plan_id": identity, "confirmation_token": identity, **payload}


def _event_payload(*, recorded_at_ms: int) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "sequence": 1,
        "action": "activated",
        "program_key": "ema_crossover_signal",
        "account_id": "paper-account",
        "actor": "local:test-operator",
        "reason": "Review the exact Paper pairing.",
        "recorded_at_ms": recorded_at_ms,
        "evidence": _evidence_payload(qualified_at_ms=1),
        "previous_event_hash": None,
    }
    return {"event_hash": semantic_payload_hash(payload), **payload}


def test_canary_evidence_accepts_the_domain_ceiling_and_rejects_beyond_it() -> None:
    assert (
        CanaryActivationEvidence.model_validate(
            _evidence_payload(qualified_at_ms=MAX_TIMESTAMP_MS)
        ).qualified_at_ms
        == MAX_TIMESTAMP_MS
    )

    with pytest.raises(ValidationError):
        CanaryActivationEvidence.model_validate(
            _evidence_payload(qualified_at_ms=MAX_TIMESTAMP_MS + 1)
        )


def test_canary_plan_enforces_the_domain_ceiling_for_creation_and_expiry() -> None:
    plan = CanaryActivationPlan.model_validate(
        _plan_payload(created_at_ms=MAX_TIMESTAMP_MS - 1, expires_at_ms=MAX_TIMESTAMP_MS)
    )
    assert plan.created_at_ms == MAX_TIMESTAMP_MS - 1
    assert plan.expires_at_ms == MAX_TIMESTAMP_MS

    with pytest.raises(ValidationError) as created_error:
        CanaryActivationPlan.model_validate(
            _plan_payload(created_at_ms=MAX_TIMESTAMP_MS + 1, expires_at_ms=MAX_TIMESTAMP_MS)
        )
    assert ("created_at_ms",) in {
        error["loc"] for error in created_error.value.errors()
    }

    with pytest.raises(ValidationError) as expiry_error:
        CanaryActivationPlan.model_validate(
            _plan_payload(created_at_ms=MAX_TIMESTAMP_MS, expires_at_ms=MAX_TIMESTAMP_MS + 1)
        )
    assert ("expires_at_ms",) in {
        error["loc"] for error in expiry_error.value.errors()
    }
    assert ("created_at_ms",) not in {
        error["loc"] for error in expiry_error.value.errors()
    }


def test_canary_event_accepts_the_domain_ceiling_and_rejects_beyond_it() -> None:
    assert (
        CanaryAdmissionEvent.model_validate(
            _event_payload(recorded_at_ms=MAX_TIMESTAMP_MS)
        ).recorded_at_ms
        == MAX_TIMESTAMP_MS
    )

    with pytest.raises(ValidationError):
        CanaryAdmissionEvent.model_validate(
            _event_payload(recorded_at_ms=MAX_TIMESTAMP_MS + 1)
        )


def test_canary_rollback_decision_accepts_the_domain_ceiling_and_rejects_beyond_it() -> None:
    payload = {
        "strategy_instance_id": "instance-1",
        "allowed": True,
        "reason_code": "CANARY_ROLLBACK_ADMITTED",
        "explanation": "The Clerk proves a safe boundary.",
        "next_step": None,
        "stop_outcome": "STOPPED_FLAT",
    }
    assert (
        CanaryRollbackDecision.model_validate(
            {**payload, "evaluated_at_ms": MAX_TIMESTAMP_MS}
        ).evaluated_at_ms
        == MAX_TIMESTAMP_MS
    )

    with pytest.raises(ValidationError):
        CanaryRollbackDecision.model_validate(
            {**payload, "evaluated_at_ms": MAX_TIMESTAMP_MS + 1}
        )
