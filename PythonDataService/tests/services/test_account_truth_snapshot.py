"""Tests for cached Account Truth readiness and submit-gate projections."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.broker.ibkr.client import BrokerError
from app.broker.ibkr.models import IbkrConnectionHealth
from app.schemas.account_truth import AccountTruthMessage, AccountTruthResponse
from app.services import account_truth_refresh
from app.services.account_truth_refresh import refresh_account_truth_and_update_cache
from app.services.account_truth_snapshot import (
    AccountTruthSnapshotProvider,
    account_truth_gate_result,
    assess_account_truth,
)


def _truth(
    *,
    account_id: str = "DU123",
    final_verdict: str = "clean",
    generated_at_ms: int = 1_700_000_000_000,
    blockers: list[AccountTruthMessage] | None = None,
) -> AccountTruthResponse:
    severity = "ok" if final_verdict == "clean" else "critical"
    return AccountTruthResponse(
        account_id=account_id,
        final_verdict=final_verdict,  # type: ignore[arg-type]
        final_severity=severity,  # type: ignore[arg-type]
        status_label="Clean" if final_verdict == "clean" else "Not proven",
        status_detail="Account Truth is clean." if final_verdict == "clean" else "Account Truth has blockers.",
        generated_at_ms=generated_at_ms,
        health=IbkrConnectionHealth(
            mode="paper",
            host="127.0.0.1",
            port=4002,
            client_id=7,
            connected=True,
            account_id=account_id,
            is_paper=True,
            fetched_at_ms=generated_at_ms,
            connection_state="connected",
            last_transition_ms=generated_at_ms,
        ),
        invariants=[],
        blockers=blockers or [],
    )


@pytest.mark.asyncio
async def test_refresh_service_remembers_successful_account_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    truth = _truth()
    monkeypatch.setattr(
        account_truth_refresh,
        "fetch_account_truth",
        AsyncMock(return_value=truth),
    )

    result = await refresh_account_truth_and_update_cache(
        object(),  # type: ignore[arg-type]
        health=truth.health,
        account_instance_bindings=[],
        snapshot_provider=provider,
    )

    assert result is truth
    assert provider.get("DU123").truth is truth  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_refresh_service_marks_failure_for_any_account_truth_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    provider.remember(_truth(), cached_at_ms=1_000)
    monkeypatch.setattr(
        account_truth_refresh,
        "fetch_account_truth",
        AsyncMock(side_effect=BrokerError("broker sweep timed out")),
    )

    with pytest.raises(BrokerError):
        await refresh_account_truth_and_update_cache(
            object(),  # type: ignore[arg-type]
            health=_truth().health,
            account_instance_bindings=[],
            snapshot_provider=provider,
        )

    assessment = assess_account_truth(provider.get("DU123"), now_ms=3_000)
    assert assessment.status == "block"
    assert assessment.reason_codes == ("ACCOUNT_TRUTH_REFRESH_FAILED",)
    assert assessment.explanation == "broker sweep timed out"


def test_refresh_failure_replaces_prior_clean_snapshot() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    provider.remember(_truth(), cached_at_ms=1_000)

    provider.mark_refresh_failed("du123", detail="gateway read timed out", attempted_at_ms=2_000)

    assessment = assess_account_truth(provider.get("DU123"), now_ms=3_000)
    gate = account_truth_gate_result(provider.get("DU123"), now_ms=3_000)
    assert assessment.status == "block"
    assert assessment.reason_codes == ("ACCOUNT_TRUTH_REFRESH_FAILED",)
    assert assessment.explanation == "gateway read timed out"
    assert assessment.evidence_at_ms == 2_000
    assert gate.status == "block"
    assert gate.operator_reason == "ACCOUNT_TRUTH_REFRESH_FAILED"


def test_staleness_uses_cached_at_ms_not_broker_generated_at_ms() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=600)
    snapshot = provider.remember(_truth(generated_at_ms=0), cached_at_ms=1_000)

    assessment = assess_account_truth(snapshot, now_ms=1_500)

    assert assessment.status == "pass"
    assert assessment.age_ms == 500


def test_unclean_snapshot_assessment_is_shared_with_gate_projection() -> None:
    blocker = AccountTruthMessage(
        code="unknown_positions",
        severity="critical",
        title="Unknown current broker positions",
        message="At least one current IBKR position is not explained by known bot/manual evidence.",
    )
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    snapshot = provider.remember(
        _truth(final_verdict="not_proven", generated_at_ms=1_000, blockers=[blocker]),
        cached_at_ms=1_100,
    )

    assessment = assess_account_truth(snapshot, now_ms=1_500)
    gate = account_truth_gate_result(snapshot, now_ms=1_500)

    assert assessment.reason_codes == (
        "ACCOUNT_TRUTH_NOT_PROVEN",
        "ACCOUNT_TRUTH_UNKNOWN_POSITIONS",
    )
    assert assessment.primary_reason_code == "ACCOUNT_TRUTH_UNKNOWN_POSITIONS"
    assert gate.operator_reason == assessment.primary_reason_code
