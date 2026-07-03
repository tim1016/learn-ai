"""Tests for cached Account Truth readiness and submit-gate projections."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import AccountTruthCollectionContext
from app.broker.ibkr.client import BrokerError
from app.broker.ibkr.models import IbkrConnectionHealth
from app.schemas.account_truth import (
    AccountTruthMessage,
    AccountTruthResponse,
    AccountTruthSourceFreshness,
)
from app.services import account_truth_refresh
from app.services.account_truth_refresh import (
    DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS,
    AccountTruthRefreshLoop,
    refresh_account_truth_and_update_cache,
    validate_account_truth_refresh_cadence,
)
from app.services.account_truth_snapshot import (
    AccountTruthSnapshotProvider,
    account_truth_gate_result,
    assess_account_truth,
)
from tests._helpers.account_truth import fresh_account_truth_source_freshness


def _truth(
    *,
    account_id: str = "DU123",
    final_verdict: str = "clean",
    generated_at_ms: int = 1_700_000_000_000,
    blockers: list[AccountTruthMessage] | None = None,
    source_freshness: list[AccountTruthSourceFreshness] | None = None,
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
        source_freshness=source_freshness
        if source_freshness is not None
        else fresh_account_truth_source_freshness(generated_at_ms),
    )


def _collection_context(account_id: str = "DU123") -> AccountTruthCollectionContext:
    return AccountTruthCollectionContext(
        account_instance_bindings=(),
        evidence_gaps=(),
        account_recovery_state=AccountRecoveryState.clear(account_id),
    )


def _health(
    *,
    account_id: str | None = "DU123",
    connected: bool = True,
    connection_state: str = "connected",
    fetched_at_ms: int = 1_700_000_000_000,
) -> IbkrConnectionHealth:
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=7,
        connected=connected,
        account_id=account_id,
        is_paper=True if account_id else None,
        fetched_at_ms=fetched_at_ms,
        connection_state=connection_state,  # type: ignore[arg-type]
        last_transition_ms=fetched_at_ms,
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
        collection_context=_collection_context(),
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
            collection_context=_collection_context(),
            snapshot_provider=provider,
        )

    assessment = assess_account_truth(provider.get("DU123"), now_ms=3_000)
    assert assessment.status == "block"
    assert assessment.reason_codes == ("ACCOUNT_TRUTH_REFRESH_FAILED",)
    assert assessment.explanation == "broker sweep timed out"


def test_refresh_cadence_has_margin_under_readiness_ttl() -> None:
    validate_account_truth_refresh_cadence(
        DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS,
        hard_ttl_ms=60_000,
    )

    with pytest.raises(ValueError, match="less than half"):
        validate_account_truth_refresh_cadence(30_000, hard_ttl_ms=60_000)


@pytest.mark.asyncio
async def test_refresh_loop_running_keeps_snapshot_fresh() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    generated_times = iter((1_000, 2_000))
    refreshed_twice = asyncio.Event()
    call_count = 0

    async def fake_refresh(
        _client,
        *,
        health: IbkrConnectionHealth,
        collection_context: AccountTruthCollectionContext,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse:
        nonlocal call_count
        assert health.account_id == "DU123"
        assert collection_context.account_recovery_state.account_id == "DU123"
        call_count += 1
        generated_at_ms = next(generated_times, 2_000)
        truth = _truth(generated_at_ms=generated_at_ms)
        assert snapshot_provider is not None
        snapshot_provider.remember(truth, cached_at_ms=generated_at_ms)
        if call_count >= 2:
            refreshed_twice.set()
        return truth

    loop = AccountTruthRefreshLoop(
        client=object(),  # type: ignore[arg-type]
        artifacts_root=Path("/tmp/account-truth"),
        interval_ms=1,
        snapshot_provider=provider,
        refresh=fake_refresh,
        health_builder=lambda _client, _monitor: _health(fetched_at_ms=1_000),
        monitor_provider=lambda: None,
        collection_context_builder=lambda *, artifacts_root, account_id, context: _collection_context(account_id or ""),
    )

    loop.start()
    try:
        await asyncio.wait_for(refreshed_twice.wait(), timeout=1.0)
    finally:
        await loop.stop()

    assessment = assess_account_truth(provider.get("DU123"), now_ms=2_500)
    assert assessment.status == "pass"
    assert assessment.age_ms == 500


@pytest.mark.asyncio
async def test_refresh_loop_marks_last_account_failed_when_broker_disconnects() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    healths = iter(
        (
            _health(account_id="DU123", connected=True, connection_state="connected", fetched_at_ms=1_000),
            _health(account_id=None, connected=False, connection_state="disconnected", fetched_at_ms=2_000),
        )
    )

    async def fake_refresh(
        _client,
        *,
        health: IbkrConnectionHealth,
        collection_context: AccountTruthCollectionContext,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse:
        assert collection_context.account_recovery_state.account_id == health.account_id
        truth = _truth(generated_at_ms=health.fetched_at_ms)
        assert snapshot_provider is not None
        snapshot_provider.remember(truth, cached_at_ms=health.fetched_at_ms)
        return truth

    loop = AccountTruthRefreshLoop(
        client=object(),  # type: ignore[arg-type]
        artifacts_root=Path("/tmp/account-truth"),
        snapshot_provider=provider,
        refresh=fake_refresh,
        health_builder=lambda _client, _monitor: next(healths),
        monitor_provider=lambda: None,
        collection_context_builder=lambda *, artifacts_root, account_id, context: _collection_context(account_id or ""),
    )

    assert await loop.refresh_once() is not None
    assert await loop.refresh_once() is None

    assessment = assess_account_truth(provider.get("DU123"), now_ms=2_500)
    assert assessment.status == "block"
    assert assessment.reason_codes == ("ACCOUNT_TRUTH_REFRESH_FAILED",)
    assert "requires a connected broker session" in assessment.explanation
    assert assessment.evidence_at_ms == 2_000


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


def test_missing_source_freshness_rows_fail_closed() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    snapshot = provider.remember(
        _truth(source_freshness=[]),
        cached_at_ms=1_100,
    )

    assessment = assess_account_truth(snapshot, now_ms=1_500)
    gate = account_truth_gate_result(snapshot, now_ms=1_500)

    assert assessment.status == "block"
    assert assessment.reason_codes == (
        "ACCOUNT_TRUTH_SOURCE_MISSING_BROKER_CONNECTION",
        "ACCOUNT_TRUTH_SOURCE_MISSING_ACCOUNT_SUMMARY",
        "ACCOUNT_TRUTH_SOURCE_MISSING_POSITIONS",
        "ACCOUNT_TRUTH_SOURCE_MISSING_OPEN_ORDERS",
    )
    assert assessment.primary_reason_code == "ACCOUNT_TRUTH_SOURCE_MISSING_BROKER_CONNECTION"
    assert gate.operator_reason == "ACCOUNT_TRUTH_SOURCE_MISSING_BROKER_CONNECTION"


def test_partial_source_freshness_rows_fail_closed_for_absent_critical_source() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    source_freshness = [
        row for row in fresh_account_truth_source_freshness(1_700_000_000_000) if row.source != "positions"
    ]
    snapshot = provider.remember(
        _truth(source_freshness=source_freshness),
        cached_at_ms=1_100,
    )

    assessment = assess_account_truth(snapshot, now_ms=1_500)

    assert assessment.status == "block"
    assert assessment.reason_codes == ("ACCOUNT_TRUTH_SOURCE_MISSING_POSITIONS",)


def test_critical_source_freshness_block_is_shared_with_gate_projection() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    source_freshness = [
        row for row in fresh_account_truth_source_freshness(1_700_000_000_000) if row.source != "positions"
    ]
    source_freshness.append(
        AccountTruthSourceFreshness(
            source="positions",
            label="Positions",
            status="stale",
            severity="critical",
            fetched_at_ms=1_000,
            age_ms=60_001,
            hard_ttl_ms=60_000,
            reason_code="ACCOUNT_TRUTH_SOURCE_STALE_POSITIONS",
            message="Positions evidence is 60001 ms old; hard freshness threshold is 60000 ms.",
        )
    )
    snapshot = provider.remember(
        _truth(source_freshness=source_freshness),
        cached_at_ms=1_100,
    )

    assessment = assess_account_truth(snapshot, now_ms=1_500)
    gate = account_truth_gate_result(snapshot, now_ms=1_500)

    assert assessment.status == "block"
    assert assessment.reason_codes == ("ACCOUNT_TRUTH_SOURCE_STALE_POSITIONS",)
    assert assessment.primary_reason_code == "ACCOUNT_TRUTH_SOURCE_STALE_POSITIONS"
    assert assessment.explanation.startswith("Positions evidence is")
    assert assessment.evidence_at_ms == 1_000
    assert gate.operator_reason == "ACCOUNT_TRUTH_SOURCE_STALE_POSITIONS"


def test_restamped_cache_cannot_fake_stale_source_freshness() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    source_freshness = [
        row for row in fresh_account_truth_source_freshness(1_700_000_000_000) if row.source != "positions"
    ]
    source_freshness.append(
        AccountTruthSourceFreshness(
            source="positions",
            label="Positions",
            status="stale",
            severity="critical",
            fetched_at_ms=1_000,
            age_ms=60_001,
            hard_ttl_ms=60_000,
            reason_code="ACCOUNT_TRUTH_SOURCE_STALE_POSITIONS",
            message="Positions evidence is 60001 ms old; hard freshness threshold is 60000 ms.",
        )
    )

    snapshot = provider.remember(
        _truth(source_freshness=source_freshness),
        cached_at_ms=61_001,
    )

    assessment = assess_account_truth(snapshot, now_ms=61_001)

    assert assessment.status == "block"
    assert assessment.reason_codes == ("ACCOUNT_TRUTH_SOURCE_STALE_POSITIONS",)
    assert assessment.age_ms == 60_001


def test_warning_source_freshness_does_not_block_clean_snapshot() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    source_freshness = [
        row for row in fresh_account_truth_source_freshness(1_700_000_000_000) if row.source != "executions"
    ]
    source_freshness.append(
        AccountTruthSourceFreshness(
            source="executions",
            label="Executions",
            status="missing",
            severity="warning",
            fetched_at_ms=None,
            age_ms=None,
            hard_ttl_ms=60_000,
            reason_code="ACCOUNT_TRUTH_SOURCE_MISSING_EXECUTIONS",
            message="IBKR execution sweep unavailable: timed out",
        )
    )
    snapshot = provider.remember(
        _truth(source_freshness=source_freshness),
        cached_at_ms=1_100,
    )

    assessment = assess_account_truth(snapshot, now_ms=1_500)

    assert assessment.status == "pass"
    assert assessment.reason_codes == ()


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
