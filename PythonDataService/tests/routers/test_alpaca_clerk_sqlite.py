"""HTTP-level tests for the SQLite Alpaca Clerk command endpoints (#1376)."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import fields
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    ClerkStartupFailure,
    get_active_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.journal import reset_clerk_settings_for_testing
from app.broker.alpaca.clerk.sqlite.historical_execution_recovery import (
    HistoricalExecutionRecoveryPlan,
)
from app.broker.alpaca.clerk.sqlite.models import ExecutionCoverageResolutionReceipt
from app.broker.alpaca.clerk.sqlite.projection_errors import ProjectionReadError
from app.broker.alpaca.clerk.sqlite.projection_models import TimelinePage
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    RepositoryPoisoned,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.timeline_query import TimelineFilters, _encode_cursor
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerAccountSnapshot
from app.routers import alpaca_clerk_sqlite
from app.routers.alpaca_clerk_sqlite import router
from app.schemas.alpaca_clerk_sqlite import HistoricalExecutionRecoveryPlanResponse

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"


def _account(account_id: str = ACCOUNT_ID) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id=account_id,
        account_mode="paper",
        account_status="ACTIVE",
        currency="USD",
        cash=1_000.0,
        equity=1_000.0,
        buying_power=2_000.0,
        portfolio_value=1_000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=None,
        observed_at_ms=1_700_000_000_000,
    )


class FakeAlpacaPort:
    broker_id = "alpaca"

    def __init__(self, *, account_id: str = ACCOUNT_ID) -> None:
        self._account_id = account_id

    def capabilities(self):
        return None

    async def get_account(self) -> BrokerAccountSnapshot:
        return _account(self._account_id)

    async def list_positions(self):
        return []

    async def list_orders(self, *, status=None, limit=None, after_ms=None):
        return []

    async def list_activities(self, *, after_ms=None, limit=100):
        return []

    async def list_assets(self, *, status=None, limit=100):
        return []

    async def get_asset(self, symbol: str):
        return None

    async def get_clock_evidence(self):
        raise AssertionError("not called")

    async def get_portfolio_history(self, _history_range: object) -> None:
        raise AssertionError("not called")

    async def submit(self, leg, *, client_order_id):
        raise AssertionError("not called")

    async def cancel(self, order_id):
        raise AssertionError("not called")

    async def get_order_by_client_order_id(self, client_order_id):
        return None


class FakeRegistry:
    def __init__(self, port: FakeAlpacaPort | None = None) -> None:
        self._port = port or FakeAlpacaPort()

    def resolve(self, broker_id: str) -> FakeAlpacaPort:
        assert broker_id == "alpaca"
        return self._port


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    reset_clerk_settings_for_testing()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    port = FakeAlpacaPort()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=port, trade=port)
    set_active_clerk_runtime(
        ActiveClerkRuntime(authority_kind="sqlite", clerk=facade)
    )

    app = FastAPI()
    app.include_router(router)
    try:
        yield app
    finally:
        set_active_clerk_runtime(None)
        repo.close()
        reset_clerk_settings_for_testing()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_timeline_route_forwards_all_exact_evidence_filters(
    api: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    original = alpaca_clerk_sqlite.SqliteClerkProjectionReader.timeline_page

    def timeline_page(
        self: SqliteClerkProjectionReader, **kwargs: object
    ) -> TimelinePage:
        captured.update(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(
        alpaca_clerk_sqlite.SqliteClerkProjectionReader,
        "timeline_page",
        timeline_page,
    )
    async with _client(api) as client:
        response = await client.get(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/timeline",
            params={
                "strategy_instance_id": SID,
                "order_ref": "order:1",
                "effect_operation_id": "effect:1",
                "uncertainty_id": "uncertainty:7",
                "execution_id": "execution:1",
                "transition_kind": "UNCERTAINTY_RAISED",
                "sequence": 7,
            },
        )

    assert response.status_code == 200
    assert captured == {
        "strategy_instance_id": SID,
        "order_ref": "order:1",
        "effect_operation_id": "effect:1",
        "uncertainty_id": "uncertainty:7",
        "execution_id": "execution:1",
        "transition_kind": "UNCERTAINTY_RAISED",
        "sequence": 7,
        "cursor": None,
        "page_size": 25,
    }


@pytest.mark.asyncio
async def test_timeline_route_accepts_a_maximum_scope_cursor(api: FastAPI) -> None:
    filters = TimelineFilters(
        strategy_instance_id="s" * 256,
        order_ref="o" * 512,
        effect_operation_id="e" * 512,
        uncertainty_id="u" * 256,
        execution_id="x" * 256,
        transition_kind="UNCERTAINTY_RAISED",
        sequence=1,
    )
    cursor = _encode_cursor(
        account_id=ACCOUNT_ID,
        authority_generation=1,
        filters=filters,
        anchor_sequence=0,
        before_sequence=1,
    )
    assert len(cursor) > 1_024
    assert len(cursor) <= 4_096

    async with _client(api) as client:
        response = await client.get(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/timeline",
            params={
                "cursor": cursor,
                "strategy_instance_id": filters.strategy_instance_id,
                "order_ref": filters.order_ref,
                "effect_operation_id": filters.effect_operation_id,
                "uncertainty_id": filters.uncertainty_id,
                "execution_id": filters.execution_id,
                "transition_kind": filters.transition_kind,
                "sequence": filters.sequence,
            },
        )

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/timeline",
        f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/timeline",
    ],
)
async def test_timeline_routes_reject_unregistered_transition_kinds(
    api: FastAPI,
    path: str,
) -> None:
    async with _client(api) as client:
        response = await client.get(path, params={"transition_kind": "NOT_A_TRANSITION"})

    assert response.status_code == 422


def test_timeline_openapi_contract_enumerates_registered_transition_kinds(
    api: FastAPI,
) -> None:
    document = api.openapi()
    for path in (
        "/api/alpaca-clerk-sqlite/accounts/{account_id}/timeline",
        "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{strategy_instance_id}/timeline",
    ):
        parameter = next(
            item
            for item in document["paths"][path]["get"]["parameters"]
            if item["name"] == "transition_kind"
        )
        assert parameter["schema"]["anyOf"][0] == {
            "$ref": "#/components/schemas/TimelineTransitionKind"
        }

    assert document["components"]["schemas"]["TimelineTransitionKind"]["enum"] == [
        member.value for member in alpaca_clerk_sqlite.TimelineTransitionKind
    ]


def test_historical_execution_recovery_transport_plan_matches_signed_plan() -> None:
    assert {field.name for field in fields(HistoricalExecutionRecoveryPlan)} == set(
        HistoricalExecutionRecoveryPlanResponse.model_fields
    )


@pytest.mark.asyncio
async def test_failed_activated_authority_exposes_typed_account_recovery_state() -> None:
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="unavailable",
            startup_failure=ClerkStartupFailure(
                reason_code="SQLITE_CLERK_STARTUP_FAILED",
                account_id=ACCOUNT_ID,
                scope="ACCOUNT_CLERK",
                impact="Broker-mutating Alpaca Clerk capability is not installed.",
                recovery="The activated database failed integrity verification.",
                observed_at_ms=1_700_000_000_000,
                activation_detected=True,
                authority_generation=3,
                db_identity_token="db-generation-3",
            ),
        )
    )
    app = FastAPI()
    app.include_router(router)
    try:
        async with _client(app) as client:
            response = await client.get(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/snapshot"
            )
    finally:
        set_active_clerk_runtime(None)

    assert response.status_code == 200
    projection = response.json()
    assert projection["authority_health"] == "failed"
    assert projection["authority_generation"] == 3
    assert projection["db_identity_token"] == "db-generation-3"
    assert projection["guidance"]["scope"] == "ACCOUNT_CLERK"
    assert projection["guidance"]["may_create_exposure"] is False
    actions = {action["action_id"]: action for action in projection["recovery_actions"]}
    assert set(actions) == {
        "open_custody_timeline",
        "rebuild_from_mirror",
        "reset_authority",
    }
    assert all(action["available"] is False for action in actions.values())


@pytest.mark.asyncio
async def test_start_then_get_returns_the_command_resource(api: FastAPI) -> None:
    async with _client(api) as client:
        start = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        assert start.status_code == 202
        body = start.json()
        assert body["state"] == "succeeded"
        assert body["action"] == "START"
        assert body["disabled_tooltip"] is None  # terminal — nothing to disable for

        get = await client.get(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/commands/{body['command_id']}"
        )
        assert get.status_code == 200
        assert get.json() == body


@pytest.mark.asyncio
async def test_historical_execution_recovery_routes_preserve_the_signed_plan_boundary(
    api: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport never turns the two-step recovery into a generic execute."""
    runtime = get_active_clerk_runtime()
    assert runtime is not None
    assert isinstance(runtime.clerk, SqliteAlpacaClerkFacade)
    plan = HistoricalExecutionRecoveryPlan(
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        uncertainty_id="uncertainty:historical-1",
        order_ref="learn-ai/spy/v1:historical",
        broker_order_id="alpaca-order-1",
        execution_id="execution-1",
        exact_symbol="SPY",
        exact_quantity=1.0,
        exact_price=100.0,
        exact_side="BUY",
        source_event_at_ms=1_700_000_000_000,
        cumulative_fill_id="learn-ai/spy/v1:historical:1.000000000",
        cumulative_quantity=1.0,
        cumulative_price=100.0,
        cumulative_side="BUY",
        authority_generation=1,
        db_identity_token="db-token",
        control_revision=10,
        prepared_at_ms=1_700_000_000_001,
        expires_at_ms=1_700_000_120_001,
        confirmation_token="a" * 64,
    )
    prepare = AsyncMock(return_value=plan)
    confirm = AsyncMock(
        return_value=ExecutionCoverageResolutionReceipt(
            uncertainty_id=plan.uncertainty_id,
            order_ref=plan.order_ref,
            execution_id=plan.execution_id,
            receipt_id="coverage-resolution:11",
            recorded_at_ms=1_700_000_000_002,
            applied=True,
        )
    )
    monkeypatch.setattr(runtime.clerk, "prepare_historical_execution_recovery", prepare)
    monkeypatch.setattr(runtime.clerk, "confirm_historical_execution_recovery", confirm)

    async with _client(api) as client:
        prepared = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/historical-execution-recovery/prepare",
            json={"concurrency_token": "b" * 64},
        )
        confirmed = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/historical-execution-recovery/confirm",
            json={"plan": prepared.json(), "confirmation_token": plan.confirmation_token},
        )

    assert prepared.status_code == 200
    assert confirmed.status_code == 200
    prepare.assert_awaited_once()
    confirm.assert_awaited_once()
    assert prepare.await_args.kwargs["concurrency_token"] == "b" * 64
    assert confirm.await_args.kwargs["plan"] == plan
    assert confirm.await_args.kwargs["confirmation_token"] == plan.confirmation_token
    assert confirmed.json()["receipt_id"] == "coverage-resolution:11"


@pytest.mark.asyncio
async def test_historical_execution_recovery_prepare_maps_projection_read_failure(
    api: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_context(*_args, **_kwargs):
        raise ProjectionReadError("simulated stale projection")

    monkeypatch.setattr(
        alpaca_clerk_sqlite.SqliteClerkProjectionReader,
        "recovery_context",
        unavailable_context,
    )
    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/historical-execution-recovery/prepare",
            json={"concurrency_token": "b" * 64},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "sqlite_projection_unavailable"


@pytest.mark.asyncio
async def test_start_transport_retry_is_idempotent_over_http(api: FastAPI) -> None:
    async with _client(api) as client:
        first = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        second = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["command_id"] == second.json()["command_id"]


@pytest.mark.asyncio
async def test_start_conflict_returns_typed_409(api: FastAPI) -> None:
    async with _client(api) as client:
        await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        conflict = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1", "operator_reason": "changes the hash"},
        )
        assert conflict.status_code == 409
        detail = conflict.json()["detail"]
        assert detail["reason"] == "durable_conflict"
        assert detail["existing_command"]["action"] == "START"


@pytest.mark.asyncio
async def test_start_with_colon_in_lifecycle_run_id_returns_typed_400(api: FastAPI) -> None:
    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "a:b"},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "invalid_identity"


@pytest.mark.asyncio
async def test_start_with_empty_lifecycle_run_id_returns_422(api: FastAPI) -> None:
    """reject_colon() only blocks ':' — an empty string would otherwise mint
    a durable command identity no client could reproduce intentionally.
    Enforced at the Pydantic boundary, not the domain layer."""
    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": ""},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_stop_without_active_run_returns_typed_404(api: FastAPI) -> None:
    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop",
            json={"lifecycle_run_id": "run-1"},
        )
        assert response.status_code == 404
        assert response.json()["detail"]["reason"] == "no_active_run"


@pytest.mark.asyncio
async def test_stop_after_start_succeeds(api: FastAPI) -> None:
    async with _client(api) as client:
        await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        stop = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop",
            json={"lifecycle_run_id": "run-1"},
        )
        assert stop.status_code == 202
        assert stop.json()["state"] == "succeeded"
        assert stop.json()["action"] == "STOP"


@pytest.mark.asyncio
async def test_stop_retry_after_run_stopped_replays_the_completed_result_over_http(
    api: FastAPI,
) -> None:
    """The HTTP-level proof of the corrective foundation slice's central
    fix: a lost-response retry of Stop, keyed by the caller-stable
    ``lifecycle_run_id``, returns the original completed command."""
    async with _client(api) as client:
        await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        first = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop",
            json={"lifecycle_run_id": "run-1"},
        )
        retry = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop",
            json={"lifecycle_run_id": "run-1"},
        )
        assert first.status_code == 202
        assert retry.status_code == 202
        assert first.json()["command_id"] == retry.json()["command_id"]


@pytest.mark.asyncio
async def test_presented_stop_rechecks_policy_and_replays_durable_lost_response(
    api: FastAPI,
) -> None:
    async with _client(api) as client:
        await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        snapshot = await client.get(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/snapshot"
        )
        assert snapshot.status_code == 200
        assert snapshot.json()["guidance"]["scope"] == "CUSTODY_SUBJECT"
        action = next(
            item
            for item in snapshot.json()["recovery_actions"]
            if item["action_id"] == "stop_bot_decisions"
        )
        request = {
            "action_id": action["action_id"],
            "concurrency_token": action["concurrency_token"],
            "execution_ref": action["execution_ref"],
            "reason": "Operator stopped decisions from the custody projection.",
        }

        first = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/recovery-actions/execute",
            json=request,
        )
        retry = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/recovery-actions/execute",
            json=request,
        )

    assert first.status_code == retry.status_code == 200
    assert first.json()["applied"] is True
    assert retry.json()["applied"] is False
    assert first.json()["command"]["command_id"] == retry.json()["command"]["command_id"]
    assert first.json()["receipt_id"] == first.json()["command"]["command_id"]
    assert retry.json()["receipt_id"] == first.json()["receipt_id"]
    assert first.json()["recorded_at_ms"] == first.json()["command"]["updated_at_ms"]


@pytest.mark.asyncio
async def test_presented_stop_quiesces_a_running_bot_task(api: FastAPI) -> None:
    """#1396 P1: `stop_bot_decisions` must not only record the durable
    SQLite STOP command — it must also quiesce a live `BotTaskRegistry`
    task for the same bot, or the process keeps evaluating signals after
    the operator is told it stopped."""
    from app.services.bot_runner import get_bot_task_registry, set_bot_task_registry

    class _FakeRegistry:
        def __init__(self) -> None:
            self.stop_calls: list[tuple[str, str]] = []

        async def stop_after_durable_clerk_stop(
            self,
            broker: str,
            strategy_instance_id: str,
            *,
            updated_by: str = "operator",
            reason: str | None = None,
        ) -> None:
            self.stop_calls.append((broker, strategy_instance_id))

    fake_registry = _FakeRegistry()
    assert get_bot_task_registry() is None
    set_bot_task_registry(fake_registry)  # type: ignore[arg-type]
    try:
        async with _client(api) as client:
            await client.post(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
                json={"lifecycle_run_id": "run-1"},
            )
            snapshot = await client.get(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/snapshot"
            )
            action = next(
                item
                for item in snapshot.json()["recovery_actions"]
                if item["action_id"] == "stop_bot_decisions"
            )
            response = await client.post(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/recovery-actions/execute",
                json={
                    "action_id": action["action_id"],
                    "concurrency_token": action["concurrency_token"],
                    "execution_ref": action["execution_ref"],
                    "reason": "Operator stopped decisions from the custody projection.",
                },
            )
    finally:
        set_bot_task_registry(None)

    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert fake_registry.stop_calls == [("alpaca", SID)]


@pytest.mark.asyncio
async def test_presented_stop_retry_requiesces_the_local_task(api: FastAPI) -> None:
    """#1404 review: a retry that replays an already-committed durable STOP
    must still re-drive local quiescence. If the first attempt committed the
    STOP but failed before stopping the task, the retry is the only chance to
    reap it — returning the existing command without quiescing would leave the
    process evaluating signals after the operator was told it stopped."""
    from app.services.bot_runner import set_bot_task_registry

    class _RecordingRegistry:
        def __init__(self) -> None:
            self.stop_calls: list[tuple[str, str]] = []

        async def stop_after_durable_clerk_stop(
            self,
            broker: str,
            strategy_instance_id: str,
            *,
            updated_by: str = "operator",
            reason: str | None = None,
        ) -> None:
            self.stop_calls.append((broker, strategy_instance_id))

    fake_registry = _RecordingRegistry()
    set_bot_task_registry(fake_registry)  # type: ignore[arg-type]
    try:
        async with _client(api) as client:
            await client.post(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
                json={"lifecycle_run_id": "run-1"},
            )
            snapshot = await client.get(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/snapshot"
            )
            action = next(
                item
                for item in snapshot.json()["recovery_actions"]
                if item["action_id"] == "stop_bot_decisions"
            )
            request = {
                "action_id": action["action_id"],
                "concurrency_token": action["concurrency_token"],
                "execution_ref": action["execution_ref"],
                "reason": "Operator stopped decisions from the custody projection.",
            }
            first = await client.post(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/recovery-actions/execute",
                json=request,
            )
            retry = await client.post(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/recovery-actions/execute",
                json=request,
            )
    finally:
        set_bot_task_registry(None)

    assert first.status_code == retry.status_code == 200
    assert first.json()["applied"] is True
    assert retry.json()["applied"] is False
    # The retry replayed the existing command AND re-drove quiescence.
    assert fake_registry.stop_calls == [("alpaca", SID), ("alpaca", SID)]


@pytest.mark.asyncio
async def test_presented_reconciliation_returns_its_durable_receipt_clock(
    api: FastAPI,
) -> None:
    async with _client(api) as client:
        snapshot = await client.get(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/snapshot"
        )
        action = next(
            item
            for item in snapshot.json()["recovery_actions"]
            if item["action_id"] == "reconcile_now"
        )
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/recovery-actions/execute",
            json={
                "action_id": action["action_id"],
                "concurrency_token": action["concurrency_token"],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["receipt_id"].startswith("reconciliation:")
    assert body["recorded_at_ms"] > 0


@pytest.mark.asyncio
async def test_start_on_unknown_bot_returns_typed_404(api: FastAPI) -> None:
    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/never-registered/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        assert response.status_code == 404
        assert response.json()["detail"]["reason"] == "unknown_strategy_instance"


@pytest.mark.asyncio
async def test_get_unknown_command_returns_typed_404(api: FastAPI) -> None:
    async with _client(api) as client:
        response = await client.get(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/commands/cmd:does-not-exist"
        )
        assert response.status_code == 404
        assert response.json()["detail"]["reason"] == "command_not_found"


@pytest.mark.asyncio
async def test_missing_active_runtime_returns_typed_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    reset_clerk_settings_for_testing()
    set_active_clerk_runtime(None)
    app = FastAPI()
    app.include_router(router)
    try:
        async with _client(app) as client:
            response = await client.get(
                "/api/alpaca-clerk-sqlite/accounts/PA-NEVER-INITIALIZED/commands/cmd:x"
            )
        assert response.status_code == 503
        assert response.json()["detail"]["reason"] == "active_clerk_not_started"
    finally:
        set_active_clerk_runtime(None)
        reset_clerk_settings_for_testing()


@pytest.mark.asyncio
async def test_blocked_repository_call_does_not_stall_an_unrelated_request(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope E: repository calls are dispatched via ``asyncio.to_thread``, so
    a slow Start does not block a concurrent, unrelated GET on the same
    event loop (open-pr-review-2026-08-05.md P2 "Synchronous SQLite/fsync
    blocks the FastAPI event loop")."""
    real_submit_start_run = alpaca_clerk_sqlite.submit_start_run
    entered = threading.Event()

    def slow_submit_start_run(*args, **kwargs):
        entered.set()
        time.sleep(0.2)
        return real_submit_start_run(*args, **kwargs)

    monkeypatch.setattr(alpaca_clerk_sqlite, "submit_start_run", slow_submit_start_run)

    async with _client(api) as client:
        start = asyncio.create_task(
            client.post(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
                json={"lifecycle_run_id": "run-1"},
            )
        )
        # Wait until the worker thread is actually inside the blocking sleep,
        # rather than guessing a fixed handoff delay (flakes under load).
        while not entered.is_set():
            await asyncio.sleep(0.005)
        began = time.monotonic()
        unrelated = await client.get(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/commands/cmd:does-not-exist"
        )
        unrelated_elapsed = time.monotonic() - began

        assert unrelated.status_code == 404
        assert unrelated_elapsed < 0.18  # completed before the slow call's 0.2s sleep ended
        assert (await start).status_code == 202


@pytest.mark.asyncio
async def test_lease_lost_returns_typed_503(api: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.broker.alpaca.clerk.sqlite.repository import ExecutionLeaseLost

    def raise_lease_lost(*_args, **_kwargs):
        raise ExecutionLeaseLost("simulated lease loss")

    monkeypatch.setattr(alpaca_clerk_sqlite, "submit_start_run", raise_lease_lost)

    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["reason"] == "sqlite_authority_unavailable"


@pytest.mark.asyncio
async def test_reconcile_now_runs_operator_pass(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    async def fake_reconcile(*, trigger):
        observed.update(trigger=trigger)
        return AccountReconciliationResult(
            verdict="position_drift",
            resolved_count=2,
            drifted_symbols=("SPY",),
        )

    runtime = get_active_clerk_runtime()
    assert runtime is not None
    assert isinstance(runtime.clerk, SqliteAlpacaClerkFacade)
    monkeypatch.setattr(alpaca_clerk_sqlite, "get_broker_registry", lambda: FakeRegistry())
    monkeypatch.setattr(runtime.clerk, "reconcile_account", fake_reconcile)

    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/reconcile"
        )

    assert response.status_code == 200
    assert response.json() == {
        "verdict": "position_drift",
        "resolved_count": 2,
        "foreign_order_count": 0,
        "drifted_symbols": ["SPY"],
    }
    assert observed["trigger"] == "OPERATOR_RECONCILE_NOW"


@pytest.mark.asyncio
async def test_reconcile_now_translates_broker_failure(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_reconcile(*_args, **_kwargs):
        raise BrokerUnavailable("broker offline")

    runtime = get_active_clerk_runtime()
    assert runtime is not None
    assert isinstance(runtime.clerk, SqliteAlpacaClerkFacade)
    monkeypatch.setattr(alpaca_clerk_sqlite, "get_broker_registry", lambda: FakeRegistry())
    monkeypatch.setattr(runtime.clerk, "reconcile_account", fake_reconcile)

    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/reconcile"
        )

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "broker_unavailable"


@pytest.mark.asyncio
async def test_reconcile_now_translates_poisoned_repository(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_reconcile(*_args, **_kwargs):
        raise RepositoryPoisoned("mirror finalize failed")

    runtime = get_active_clerk_runtime()
    assert runtime is not None
    assert isinstance(runtime.clerk, SqliteAlpacaClerkFacade)
    monkeypatch.setattr(alpaca_clerk_sqlite, "get_broker_registry", lambda: FakeRegistry())
    monkeypatch.setattr(runtime.clerk, "reconcile_account", fake_reconcile)

    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/reconcile"
        )

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "sqlite_authority_unavailable"


@pytest.mark.asyncio
async def test_reconcile_now_rejects_mismatched_broker_account_before_recovery(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def fake_reconcile(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not reconcile the wrong broker account")

    registry = FakeRegistry(FakeAlpacaPort(account_id="PA-OTHER"))
    runtime = get_active_clerk_runtime()
    assert runtime is not None
    assert isinstance(runtime.clerk, SqliteAlpacaClerkFacade)
    monkeypatch.setattr(alpaca_clerk_sqlite, "get_broker_registry", lambda: registry)
    monkeypatch.setattr(runtime.clerk, "reconcile_account", fake_reconcile)

    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/reconcile"
        )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "broker_account_mismatch"
    assert called is False
