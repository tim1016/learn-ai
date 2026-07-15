"""Tests for cached Account Truth readiness and submit-gate projections."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import AccountTruthCollectionContext, compose_account_truth
from app.broker.ibkr.client import BrokerError
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrConnectionHealth,
    IbkrOrderEvent,
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_artifacts import (
    ACCOUNT_OWNER_GENERATION_FILENAME,
    AccountFreezeEvidence,
    AccountInstanceBinding,
    AccountOwnerGeneration,
    account_artifacts_root,
    write_account_freeze,
    write_account_owner_generation,
)
from app.engine.live.account_observation_lease import assess_account_observation_lease
from app.schemas.account_truth import (
    AccountTruthMessage,
    AccountTruthResponse,
    AccountTruthSourceFreshness,
)
from app.services import account_truth_refresh
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_truth_refresh import (
    DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS,
    AccountTruthRefreshLoop,
    _refresh_sleep_seconds,
    refresh_account_truth_and_update_cache,
    refresh_account_truth_now,
    validate_account_truth_refresh_cadence,
)
from app.services.account_truth_snapshot import (
    AccountTruthSnapshot,
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


def _composed_truth_with_position(
    *,
    owned: bool,
    account_summary_id: str = "DU123",
) -> AccountTruthResponse:
    account_id = "DU123"
    position = IbkrPosition(
        account_id=account_id,
        con_id=42,
        symbol="SPY",
        sec_type="STK",
        quantity=1.0,
        avg_cost=500.0,
        fetched_at_ms=1_000,
    )
    executions = (
        [
            IbkrOrderEvent(
                account_id=account_id,
                order_id=7,
                perm_id=70,
                con_id=42,
                event_type="fill",
                status="Filled",
                order_ref="learn-ai/bot-a/v1:intent-a",
                symbol="SPY",
                side="BUY",
                order_type="MKT",
                exec_id="exec-a",
                client_id=7,
                fill_quantity=1.0,
                avg_fill_price=500.0,
                cumulative_filled=1.0,
                remaining=0.0,
                last_fill_price=500.0,
                exec_time_ms=900,
                ts_ms=1_000,
            )
        ]
        if owned
        else []
    )
    bindings = (
        [
            AccountInstanceBinding(
                account_id=account_id,
                strategy_instance_id="bot-a",
                run_id="run-a",
                bot_order_namespace="learn-ai/bot-a/v1",
                lifecycle_state="ACTIVE",
                recorded_at_ms=900,
                source="test",
            )
        ]
        if owned
        else []
    )
    return compose_account_truth(
        health=_health(account_id=account_id, fetched_at_ms=1_000),
        account_instance_bindings=bindings,
        account_recovery_state=AccountRecoveryState.clear(account_id),
        account=IbkrAccountSummary(
            account_id=account_summary_id,
            is_paper=True,
            base_currency="USD",
            net_liquidation=100_000.0,
            buying_power=50_000.0,
            fetched_at_ms=1_000,
        ),
        positions_snapshot=IbkrPositionsSnapshot(
            account_id=account_id,
            is_paper=True,
            positions=[position],
            fetched_at_ms=1_000,
        ),
        open_orders=[],
        completed_orders=[],
        executions=executions,
        generated_at_ms=1_000,
    )


@pytest.mark.parametrize(
    "connection_state,connected",
    [
        ("disconnected", False),
        ("hard_down", False),
        ("reconnecting", False),
        ("soft_lost", True),
    ],
)
def test_account_truth_refresh_unavailable_states(
    connection_state: str, connected: bool
) -> None:
    health = _health(connection_state=connection_state, connected=connected)

    assert account_truth_refresh.account_truth_refresh_session_unavailable(health) is True


class _FakeClient:
    def __init__(self, *health_results: IbkrConnectionHealth | Exception) -> None:
        self._health_results = list(health_results)
        self._index = 0

    def health(self) -> IbkrConnectionHealth:
        if not self._health_results:
            raise AssertionError("fake client needs at least one health result")
        index = min(self._index, len(self._health_results) - 1)
        self._index += 1
        result = self._health_results[index]
        if isinstance(result, Exception):
            raise result
        return result


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


@pytest.mark.asyncio
async def test_refresh_service_notifies_keyword_only_failure_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        account_truth_refresh,
        "fetch_account_truth",
        AsyncMock(side_effect=BrokerError("broker sweep timed out")),
    )

    def observe_failure(
        *,
        account_id: str | None,
        detail: str,
        attempted_at_ms: int,
    ) -> None:
        observed["account_id"] = account_id
        observed["detail"] = detail
        observed["attempted_at_ms"] = attempted_at_ms

    with pytest.raises(BrokerError):
        await refresh_account_truth_and_update_cache(
            object(),  # type: ignore[arg-type]
            health=_truth(generated_at_ms=2_000).health,
            collection_context=_collection_context(),
            snapshot_provider=provider,
            account_truth_failure_observer=observe_failure,
        )

    assert observed == {
        "account_id": "DU123",
        "detail": "broker sweep timed out",
        "attempted_at_ms": 2_000,
    }


@pytest.mark.asyncio
async def test_refresh_now_builds_context_once_and_remembers_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    truth = _truth(account_id="DU123")
    captured: dict[str, str | None] = {}

    def fake_collection_context(
        *,
        artifacts_root,
        account_id: str | None,
        context: str,
    ) -> AccountTruthCollectionContext:
        captured["account_id"] = account_id
        captured["context"] = context
        return _collection_context(account_id or "")

    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)
    monkeypatch.setattr(
        account_truth_refresh,
        "build_account_truth_collection_context",
        fake_collection_context,
    )
    monkeypatch.setattr(
        account_truth_refresh,
        "fetch_account_truth",
        AsyncMock(return_value=truth),
    )

    result = await refresh_account_truth_now(
        _FakeClient(_health(account_id="DU999")),  # type: ignore[arg-type]
        account_id="DU123",
        context="account reconciliation",
        snapshot_provider=provider,
    )

    assert result is truth
    assert captured == {"account_id": "DU123", "context": "account reconciliation"}
    assert provider.get("DU123").truth is truth  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_refresh_now_captures_owner_generation_before_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    truth = _truth(account_id="DU123")
    observed: dict[str, object] = {}
    write_account_owner_generation(
        tmp_path,
        AccountOwnerGeneration(
            account_id="DU123",
            generation=3,
            phase="accepting",
            recorded_at_ms=1_700_000_000_000,
            source="test",
        ),
    )

    def fake_collection_context(
        *,
        artifacts_root,
        account_id: str | None,
        context: str,
    ) -> AccountTruthCollectionContext:
        write_account_owner_generation(
            tmp_path,
            AccountOwnerGeneration(
                account_id="DU123",
                generation=4,
                phase="accepting",
                recorded_at_ms=1_700_000_000_001,
                source="test",
            ),
        )
        return _collection_context(account_id or "")

    def observe_success(
        account_truth: AccountTruthResponse,
        *,
        owner_generation_before: tuple[int, str] | None,
        owner_generation_captured: bool,
    ) -> None:
        observed["truth"] = account_truth
        observed["owner_generation_before"] = owner_generation_before
        observed["owner_generation_captured"] = owner_generation_captured

    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)
    monkeypatch.setattr(
        account_truth_refresh,
        "build_account_truth_collection_context",
        fake_collection_context,
    )
    monkeypatch.setattr(
        account_truth_refresh,
        "fetch_account_truth",
        AsyncMock(return_value=truth),
    )

    result = await refresh_account_truth_now(
        _FakeClient(_health(account_id="DU123")),  # type: ignore[arg-type]
        account_id="DU123",
        artifacts_root=tmp_path,
        context="account truth",
        snapshot_provider=provider,
        account_truth_observer=observe_success,
    )

    assert result is truth
    assert observed == {
        "truth": truth,
        "owner_generation_before": (3, "accepting"),
        "owner_generation_captured": True,
    }


def test_read_owner_generation_fence_fails_closed_for_malformed_artifact(tmp_path: Path) -> None:
    account_id = "DU123"
    path = account_artifacts_root(tmp_path, account_id) / ACCOUNT_OWNER_GENERATION_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text('{"generation":"not-an-integer"}', encoding="utf-8")

    assert account_truth_refresh._read_owner_generation_fence(tmp_path, account_id) is None


@pytest.mark.asyncio
async def test_refresh_now_passes_active_account_freeze_to_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU123",
            reason="restart_intensity.threshold_breached",
            source="account_restart_intensity",
            recorded_at_ms=1_780_000_002_000,
            operator_next_step="STOP_RESTARTING_AND_RECOVER_ACCOUNT",
        ),
    )
    captured: dict[str, AccountTruthCollectionContext] = {}

    async def fake_fetch_account_truth(
        _client,
        *,
        health: IbkrConnectionHealth,
        collection_context: AccountTruthCollectionContext,
    ) -> AccountTruthResponse:
        captured["collection_context"] = collection_context
        return _truth(account_id=health.account_id or "")

    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)
    monkeypatch.setattr(account_truth_refresh, "account_truth_artifacts_root", lambda: tmp_path)
    monkeypatch.setattr(account_truth_refresh, "fetch_account_truth", fake_fetch_account_truth)

    await refresh_account_truth_now(
        _FakeClient(_health(account_id="DU123")),  # type: ignore[arg-type]
        context="account truth",
        snapshot_provider=provider,
    )

    collection_context = captured["collection_context"]
    assert collection_context.account_recovery_state.status == "frozen"
    assert collection_context.evidence_gaps == ()


@pytest.mark.asyncio
async def test_refresh_now_adds_gap_when_freeze_state_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    account_root = tmp_path / "accounts" / "DU123"
    account_root.mkdir(parents=True)
    (account_root / "unresolved_exposure.flag").write_text("{not-json", encoding="utf-8")
    captured: dict[str, AccountTruthCollectionContext] = {}

    async def fake_fetch_account_truth(
        _client,
        *,
        health: IbkrConnectionHealth,
        collection_context: AccountTruthCollectionContext,
    ) -> AccountTruthResponse:
        captured["collection_context"] = collection_context
        return _truth(account_id=health.account_id or "")

    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)
    monkeypatch.setattr(account_truth_refresh, "account_truth_artifacts_root", lambda: tmp_path)
    monkeypatch.setattr(account_truth_refresh, "fetch_account_truth", fake_fetch_account_truth)

    await refresh_account_truth_now(
        _FakeClient(_health(account_id="DU123")),  # type: ignore[arg-type]
        context="account truth",
        snapshot_provider=provider,
    )

    collection_context = captured["collection_context"]
    evidence_gaps = collection_context.evidence_gaps
    assert collection_context.account_recovery_state.status == "unreadable"
    assert len(evidence_gaps) == 1
    gap = evidence_gaps[0]
    assert gap.source == "account_freeze"
    assert gap.severity == "critical"
    assert "Account freeze state unavailable" in gap.message


def test_refresh_cadence_has_margin_under_readiness_ttl() -> None:
    validate_account_truth_refresh_cadence(
        DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS,
        hard_ttl_ms=60_000,
    )

    with pytest.raises(ValueError, match="less than half"):
        validate_account_truth_refresh_cadence(30_000, hard_ttl_ms=60_000)


def test_refresh_loop_validates_cadence_against_provider_ttl() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=20_000)

    with pytest.raises(ValueError, match="less than half"):
        AccountTruthRefreshLoop(
            client=_FakeClient(_health()),  # type: ignore[arg-type]
            interval_ms=15_000,
            snapshot_provider=provider,
        )


def test_refresh_loop_sleep_uses_bounded_backoff_and_jitter() -> None:
    assert _refresh_sleep_seconds(
        1_000,
        consecutive_failures=0,
        random_fraction=0.5,
    ) == 1.0
    assert _refresh_sleep_seconds(
        1_000,
        consecutive_failures=2,
        random_fraction=0.5,
    ) == 4.0
    assert _refresh_sleep_seconds(
        1_000,
        consecutive_failures=99,
        random_fraction=0.5,
    ) == 4.0
    assert _refresh_sleep_seconds(
        1_000,
        consecutive_failures=0,
        random_fraction=0.0,
    ) == 0.85
    assert _refresh_sleep_seconds(
        1_000,
        consecutive_failures=0,
        random_fraction=1.0,
    ) == 1.15


@pytest.mark.asyncio
async def test_refresh_loop_running_keeps_snapshot_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    generated_times = iter((1_000, 2_000))
    refreshed_twice = asyncio.Event()
    call_count = 0

    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    async def fake_refresh_now(
        _client,
        *,
        context: str,
        account_id: str | None = None,
        health: IbkrConnectionHealth | None = None,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse:
        nonlocal call_count
        assert context == "account truth refresh loop"
        assert account_id == "DU123"
        assert health is not None
        call_count += 1
        generated_at_ms = next(generated_times, 2_000)
        truth = _truth(generated_at_ms=generated_at_ms)
        assert snapshot_provider is not None
        snapshot_provider.remember(truth, cached_at_ms=generated_at_ms)
        if call_count >= 2:
            refreshed_twice.set()
        return truth

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(_health(fetched_at_ms=1_000), _health(fetched_at_ms=2_000)),  # type: ignore[arg-type]
        interval_ms=1,
        snapshot_provider=provider,
        refresh_now=fake_refresh_now,
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
async def test_refresh_loop_refreshes_during_data_farm_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)
    refreshed = False

    async def fake_refresh_now(
        _client,
        *,
        context: str,
        account_id: str | None = None,
        health: IbkrConnectionHealth | None = None,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse:
        nonlocal refreshed
        assert context == "account truth refresh loop"
        assert account_id == "DU123"
        assert health is not None
        assert health.connection_state == "degraded_data_farm"
        assert snapshot_provider is provider
        refreshed = True
        truth = _truth(generated_at_ms=2_000)
        provider.remember(truth, cached_at_ms=2_000)
        return truth

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(
            _health(
                account_id="DU123",
                connected=True,
                connection_state="degraded_data_farm",
                fetched_at_ms=2_000,
            )
        ),  # type: ignore[arg-type]
        snapshot_provider=provider,
        refresh_now=fake_refresh_now,
    )

    assert await loop.refresh_once() is not None
    assert refreshed is True
    assert assess_account_truth(provider.get("DU123"), now_ms=2_500).status == "pass"


@pytest.mark.asyncio
async def test_refresh_loop_notifies_account_truth_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    observed: list[AccountTruthResponse] = []
    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    async def fake_refresh_now(
        _client,
        **_kwargs,
    ) -> AccountTruthResponse:
        return _truth(generated_at_ms=2_000)

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(_health(account_id="DU123", fetched_at_ms=2_000)),  # type: ignore[arg-type]
        snapshot_provider=provider,
        refresh_now=fake_refresh_now,
        account_truth_observer=observed.append,
    )

    result = await loop.refresh_once()

    assert result is not None
    assert observed == [result]


@pytest.mark.asyncio
async def test_refresh_loop_runs_account_journal_observer_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    observed: list[str] = []
    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    async def fake_refresh_now(_client, **_kwargs) -> AccountTruthResponse:
        return _truth(generated_at_ms=2_000)

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(_health(account_id="DU123", fetched_at_ms=2_000)),  # type: ignore[arg-type]
        snapshot_provider=provider,
        refresh_now=fake_refresh_now,
        account_journal_observer=observed.append,
    )

    assert await loop.refresh_once() is not None
    assert observed == ["DU123"]


@pytest.mark.asyncio
async def test_refresh_loop_skips_journal_observer_when_refresh_cannot_prove_account_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    observed: list[str] = []
    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    async def fake_refresh_now(_client, **_kwargs) -> AccountTruthResponse:
        return _truth(generated_at_ms=2_000).model_copy(update={"account_id": None})

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(_health(account_id="DU123", fetched_at_ms=2_000)),  # type: ignore[arg-type]
        snapshot_provider=provider,
        refresh_now=fake_refresh_now,
        account_journal_observer=observed.append,
    )

    assert await loop.refresh_once() is not None
    assert observed == []


@pytest.mark.asyncio
async def test_refresh_loop_marks_last_account_failed_when_broker_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)

    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    async def fake_refresh_now(
        _client,
        *,
        context: str,
        account_id: str | None = None,
        health: IbkrConnectionHealth | None = None,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse:
        assert context == "account truth refresh loop"
        assert account_id == "DU123"
        assert health is not None
        truth = _truth(generated_at_ms=1_000)
        assert snapshot_provider is not None
        snapshot_provider.remember(truth, cached_at_ms=1_000)
        return truth

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(
            _health(account_id="DU123", connected=True, connection_state="connected", fetched_at_ms=1_000),
            _health(account_id=None, connected=False, connection_state="disconnected", fetched_at_ms=2_000),
        ),  # type: ignore[arg-type]
        snapshot_provider=provider,
        refresh_now=fake_refresh_now,
    )

    assert await loop.refresh_once() is not None
    assert await loop.refresh_once() is None

    assessment = assess_account_truth(provider.get("DU123"), now_ms=2_500)
    assert assessment.status == "block"
    assert assessment.reason_codes == ("ACCOUNT_TRUTH_REFRESH_FAILED",)
    assert "requires an available account/order broker session" in assessment.explanation
    assert assessment.evidence_at_ms == 2_000


@pytest.mark.asyncio
async def test_refresh_loop_notifies_failure_observer_when_broker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    observed_failures: list[tuple[str | None, str, int]] = []
    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(
            _health(
                account_id="DU123",
                connected=False,
                connection_state="disconnected",
                fetched_at_ms=2_000,
            )
        ),  # type: ignore[arg-type]
        snapshot_provider=provider,
        account_truth_failure_observer=lambda account_id, detail, attempted_at_ms: observed_failures.append(
            (account_id, detail, attempted_at_ms)
        ),
    )

    assert await loop.refresh_once() is None
    assert observed_failures == [
        (
            "DU123",
            "Account Truth refresh requires an available account/order broker session; "
            "current broker state is disconnected.",
            2_000,
        )
    ]


@pytest.mark.asyncio
async def test_refresh_loop_failure_observer_accepts_real_bound_reconciliation_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    service = AccountReconciliationService(artifacts_root=tmp_path)
    service.observe_account_truth(_truth(), now_ms=1_000)
    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    async def failing_refresh_now(
        _client,
        *,
        context: str,
        account_id: str | None = None,
        health: IbkrConnectionHealth | None = None,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse:
        assert context == "account truth refresh loop"
        assert account_id == "DU123"
        assert health is not None
        assert snapshot_provider is provider
        raise BrokerError("broker sweep timed out")

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(_health(account_id="DU123", fetched_at_ms=2_000)),  # type: ignore[arg-type]
        snapshot_provider=provider,
        refresh_now=failing_refresh_now,
        account_truth_failure_observer=service.observe_account_truth_failure,
    )

    assert await loop.refresh_once() is None

    assessment = assess_account_observation_lease(tmp_path, "DU123", now_ms=2_001)
    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_TRUTH_REFRESH_FAILED"


@pytest.mark.asyncio
async def test_refresh_loop_does_not_backoff_unavailable_broker_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    observed_failures: list[int] = []
    observed_twice = asyncio.Event()
    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    def fake_sleep_seconds(
        interval_ms: int,
        *,
        consecutive_failures: int,
        random_fraction: float | None = None,
    ) -> float:
        observed_failures.append(consecutive_failures)
        if len(observed_failures) >= 2:
            observed_twice.set()
        return 0.001

    async def failing_refresh_now(
        _client,
        *,
        context: str,
        account_id: str | None = None,
        health: IbkrConnectionHealth | None = None,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse:
        assert context == "account truth refresh loop"
        assert account_id == "DU123"
        assert health is not None
        assert snapshot_provider is provider
        raise BrokerError("broker sweep timed out")

    monkeypatch.setattr(account_truth_refresh, "_refresh_sleep_seconds", fake_sleep_seconds)
    loop = AccountTruthRefreshLoop(
        client=_FakeClient(
            _health(account_id="DU123", connected=False, connection_state="disconnected", fetched_at_ms=1_000),
            _health(account_id="DU123", connected=True, connection_state="connected", fetched_at_ms=2_000),
        ),  # type: ignore[arg-type]
        interval_ms=1,
        snapshot_provider=provider,
        refresh_now=failing_refresh_now,
    )

    loop.start()
    try:
        await asyncio.wait_for(observed_twice.wait(), timeout=1.0)
    finally:
        await loop.stop()

    assert observed_failures[:2] == [0, 1]


@pytest.mark.asyncio
async def test_refresh_loop_marks_broker_error_failed_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    provider.remember(_truth(), cached_at_ms=1_000)
    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    async def fake_refresh_now(
        _client,
        *,
        context: str,
        account_id: str | None = None,
        health: IbkrConnectionHealth | None = None,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse:
        assert context == "account truth refresh loop"
        assert account_id == "DU123"
        assert health is not None
        assert snapshot_provider is provider
        raise BrokerError("broker sweep timed out")

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(_health(account_id="DU123", fetched_at_ms=2_000)),  # type: ignore[arg-type]
        snapshot_provider=provider,
        refresh_now=fake_refresh_now,
    )

    assert await loop.refresh_once() is None

    assessment = assess_account_truth(provider.get("DU123"), now_ms=2_500)
    assert assessment.status == "block"
    assert assessment.reason_codes == ("ACCOUNT_TRUTH_REFRESH_FAILED",)
    assert assessment.explanation == "broker sweep timed out"
    assert assessment.evidence_at_ms == 2_000


@pytest.mark.asyncio
async def test_refresh_loop_marks_last_account_failed_when_iteration_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    async def fake_refresh_now(
        _client,
        *,
        context: str,
        account_id: str | None = None,
        health: IbkrConnectionHealth | None = None,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse:
        assert health is not None
        truth = _truth(generated_at_ms=1_000)
        assert snapshot_provider is not None
        snapshot_provider.remember(truth, cached_at_ms=1_000)
        return truth

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(
            _health(account_id="DU123", fetched_at_ms=1_000),
            RuntimeError("health read broke"),
        ),  # type: ignore[arg-type]
        snapshot_provider=provider,
        refresh_now=fake_refresh_now,
    )

    assert await loop.refresh_once() is not None
    assert await loop.refresh_once() is None

    assessment = assess_account_truth(provider.get("DU123"), now_ms=2_500)
    assert assessment.status == "block"
    assert assessment.reason_codes == ("ACCOUNT_TRUTH_REFRESH_FAILED",)
    assert "health read broke" in assessment.explanation


@pytest.mark.asyncio
async def test_refresh_loop_continues_after_unexpected_iteration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=60_000)
    refreshed_after_error = asyncio.Event()
    monkeypatch.setattr(account_truth_refresh, "get_monitor", lambda: None)

    async def fake_refresh_now(
        _client,
        *,
        context: str,
        account_id: str | None = None,
        health: IbkrConnectionHealth | None = None,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse:
        assert health is not None
        truth = _truth(generated_at_ms=2_000)
        assert snapshot_provider is not None
        snapshot_provider.remember(truth, cached_at_ms=2_000)
        refreshed_after_error.set()
        return truth

    loop = AccountTruthRefreshLoop(
        client=_FakeClient(
            RuntimeError("transient health read failed"),
            _health(account_id="DU123", fetched_at_ms=2_000),
        ),  # type: ignore[arg-type]
        interval_ms=1,
        snapshot_provider=provider,
        refresh_now=fake_refresh_now,
    )

    loop.start()
    try:
        await asyncio.wait_for(refreshed_after_error.wait(), timeout=1.0)
    finally:
        await loop.stop()

    assessment = assess_account_truth(provider.get("DU123"), now_ms=2_500)
    assert assessment.status == "pass"
    assert assessment.age_ms == 500


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


def test_assessment_passes_composed_owned_nonzero_position() -> None:
    truth = _composed_truth_with_position(owned=True)
    snapshot = AccountTruthSnapshot(truth=truth, cached_at_ms=1_000)

    assessment = assess_account_truth(snapshot, now_ms=1_500)

    assert truth.final_verdict == "clean"
    assert truth.positions[0].quantity == 1.0
    assert truth.positions[0].owner.owner_class == "bot"
    assert assessment.status == "pass"


def test_assessment_blocks_composed_unattributed_nonzero_position() -> None:
    truth = _composed_truth_with_position(owned=False)
    snapshot = AccountTruthSnapshot(truth=truth, cached_at_ms=1_000)

    assessment = assess_account_truth(snapshot, now_ms=1_500)

    assert truth.final_verdict == "not_proven"
    assert truth.positions[0].owner.owner_class == "foreign_or_unclaimed"
    assert assessment.status == "block"
    assert assessment.reason_codes[0] == "ACCOUNT_TRUTH_NOT_PROVEN"


def test_assessment_blocks_composed_connected_account_mismatch() -> None:
    truth = _composed_truth_with_position(owned=False, account_summary_id="DU999")
    snapshot = AccountTruthSnapshot(truth=truth, cached_at_ms=1_000)

    assessment = assess_account_truth(snapshot, now_ms=1_500)

    assert truth.account_id is None
    assert truth.final_verdict == "not_proven"
    assert assessment.status == "block"
    assert assessment.reason_codes[0] == "ACCOUNT_TRUTH_NOT_PROVEN"


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


def test_fresh_critical_source_is_rechecked_at_gate_read_time() -> None:
    provider = AccountTruthSnapshotProvider(hard_ttl_ms=120_000)
    snapshot = provider.remember(
        _truth(source_freshness=fresh_account_truth_source_freshness(1_000)),
        cached_at_ms=1_000,
    )

    assessment = assess_account_truth(snapshot, now_ms=61_001)
    gate = account_truth_gate_result(snapshot, now_ms=61_001)

    assert assessment.status == "block"
    assert assessment.primary_reason_code == "ACCOUNT_TRUTH_SOURCE_STALE_BROKER_CONNECTION"
    assert assessment.explanation == "Broker connection evidence is 60001 ms old; hard freshness threshold is 60000 ms."
    assert gate.operator_reason == "ACCOUNT_TRUTH_SOURCE_STALE_BROKER_CONNECTION"


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
