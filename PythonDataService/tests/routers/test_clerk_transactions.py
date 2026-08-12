from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime, set_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.models import ExternalOrderResource
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerOrder
from app.routers.clerk_transactions import router
from app.schemas.clerk_transaction_projection import (
    ClerkTransactionRow,
    ClerkTransactionSummaryRow,
)
from app.services.clerk_transaction_projection import (
    ClerkTransactionProjectionUnavailable,
    _encode_cursor,
    custody_window_summary,
    fold_lifecycle_state,
)

pytestmark = pytest.mark.asyncio


def _summary(*, transaction_id: str, lifecycle_state: str) -> ClerkTransactionSummaryRow:
    return ClerkTransactionSummaryRow(
        transaction_id=transaction_id,
        account_id="DU1219",
        journal_seq=12,
        recorded_at_ms=1_700_000_000_000,
        transaction_kind="strategy_submission",
        strategy_instance_id="bot-1",
        run_id="run-1",
        intent_id=f"intent:{transaction_id}",
        order_ref=f"learn-ai/bot-1:{transaction_id}",
        lifecycle_state=lifecycle_state,
        event_count=1,
    )


class _NoIoStore:
    async def history_page(
        self,
        *,
        account_id: str,
        limit: int,
        after,
        origin=None,
        lifecycle_state=None,
        strategy_instance_id=None,
        run_id=None,
        from_ms=None,
        to_ms=None,
    ):
        assert account_id == "DU1219"
        assert limit == 25
        assert after is None
        assert (origin, lifecycle_state, strategy_instance_id, run_id) == (None, None, None, None)
        return [], 12, 0

    async def feed_status(self, account_id: str) -> tuple[str, str, str, int | None, int | None, bool]:
        return "live", "Live", "Durable Clerk callback projection is current.", 12, 0, False

    async def transaction_detail(self, *, account_id: str, transaction_id: str) -> ClerkTransactionRow | None:
        assert account_id == "DU1219"
        assert transaction_id == "ctxn_opaque"
        return ClerkTransactionRow(
            transaction_id=transaction_id, account_id=account_id, journal_seq=12,
            recorded_at_ms=1_700_000_000_000, transaction_kind="manual_ibkr_acknowledgement",
            strategy_instance_id="manual", run_id="manual", intent_id="intent:opaque",
            order_ref="manual/v1:opaque", lifecycle_state="submitted",
            receipt={"order_ref": "manual/v1:opaque"}, events=[],
        )


class _UnavailableStore:
    async def history_page(self, **kwargs):
        raise ClerkTransactionProjectionUnavailable("disabled")

    async def transaction_detail(self, **kwargs):
        raise ClerkTransactionProjectionUnavailable("disabled")


async def test_history_endpoint_is_bounded_projection_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(
        "app.routers.clerk_transactions.get_clerk_transaction_store", lambda: _NoIoStore()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/accounts/DU1219/transactions?limit=25")
    assert response.status_code == 200
    assert response.json() == {
        "projection_available": True, "canonical_fallback_required": False,
        "feed_state": "live", "feed_headline": "Live", "feed_detail": "Durable Clerk callback projection is current.",
        "high_water_journal_seq": 12, "lag_records": 0, "lag_is_lower_bound": False,
        "custody_summary": {
            "record_count": 0, "a0_custody_accepted_count": 0, "a1_broker_write_started_count": 0,
            "a2_broker_known_count": 0, "a3_economic_terminal_count": 0, "uncertain_count": 0,
        },
        "rows": [], "next_cursor": None,
    }


async def test_history_endpoint_passes_typed_filters_to_the_projection_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FilteredStore(_NoIoStore):
        async def history_page(
            self,
            *,
            account_id: str,
            limit: int,
            after,
            origin=None,
            lifecycle_state=None,
            strategy_instance_id=None,
            run_id=None,
            from_ms=None,
            to_ms=None,
        ):
            assert account_id == "DU1219"
            assert limit == 25
            assert after is None
            assert (origin, lifecycle_state, strategy_instance_id, run_id) == (
                "strategy",
                "submitted",
                "bot-1",
                "run-1",
            )
            return [], 12, 0

    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(
        "app.routers.clerk_transactions.get_clerk_transaction_store", lambda: _FilteredStore()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/accounts/DU1219/transactions",
            params={
                "limit": 25,
                "origin": "strategy",
                "lifecycle_state": "submitted",
                "strategy_instance_id": "bot-1",
                "run_id": "run-1",
            },
        )
    assert response.status_code == 200


async def test_history_endpoint_passes_an_inclusive_ms_window_to_the_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WindowedStore(_NoIoStore):
        async def history_page(
            self,
            *,
            account_id: str,
            limit: int,
            after,
            origin=None,
            lifecycle_state=None,
            strategy_instance_id=None,
            run_id=None,
            from_ms=None,
            to_ms=None,
        ):
            assert account_id == "DU1219"
            assert limit == 25
            assert after is None
            assert (origin, lifecycle_state, strategy_instance_id, run_id) == (None, None, None, None)
            assert (from_ms, to_ms) == (1_700_000_000_000, 1_700_086_400_000)
            return [], 12, 0

    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(
        "app.routers.clerk_transactions.get_clerk_transaction_store", lambda: _WindowedStore()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/accounts/DU1219/transactions",
            params={"limit": 25, "from_ms": 1_700_000_000_000, "to_ms": 1_700_086_400_000},
        )
    assert response.status_code == 200


async def test_history_endpoint_rejects_an_inverted_ms_window() -> None:
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/accounts/DU1219/transactions",
            params={"from_ms": 2, "to_ms": 1},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "to_ms must be greater than or equal to from_ms"


async def test_history_endpoint_reports_unavailable_without_fallback_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(
        "app.routers.clerk_transactions.get_clerk_transaction_store", lambda: _UnavailableStore()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/accounts/DU1219/transactions")
    assert response.status_code == 200
    assert response.json()["feed_state"] == "projection_unavailable"
    assert response.json()["canonical_fallback_required"] is True


@pytest.mark.parametrize(
    "cursor",
    [
        _encode_cursor((True, 1, "ctxn_opaque")),
        _encode_cursor((-1, 1, "ctxn_opaque")),
        _encode_cursor((1, 0, "ctxn_opaque")),
        _encode_cursor((1, 2**63, "ctxn_opaque")),
    ],
)
async def test_history_endpoint_rejects_non_postgres_cursor_coordinates(
    cursor: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(
        "app.routers.clerk_transactions.get_clerk_transaction_store", lambda: _NoIoStore()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/accounts/DU1219/transactions", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid transaction history cursor"


async def test_selected_transaction_endpoint_reads_only_the_requested_projection_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(
        "app.routers.clerk_transactions.get_clerk_transaction_store", lambda: _NoIoStore()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/accounts/DU1219/transactions/ctxn_opaque")
    assert response.status_code == 200
    assert response.json()["receipt"] == {"order_ref": "manual/v1:opaque"}


async def test_external_order_acknowledgement_endpoint_delegates_only_to_active_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routers.clerk_transactions as clerk_transactions_router

    calls: list[tuple[str, str, str]] = []

    def _acknowledge(*, account_id: str, external_order_id: str, operator: str) -> ExternalOrderResource:
        calls.append((account_id, external_order_id, operator))
        return ExternalOrderResource(
            external_order_id=external_order_id,
            broker_order_id="broker-external-1",
            client_order_id="alpaca-console:1",
            symbol="AAPL",
            side="BUY",
            qty=2.0,
            order_type="market",
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            observed_at_ms=1_700_000_000_000,
            acknowledged_at_ms=1_700_000_000_100,
            ack_operator=operator,
            evidence_refs=("broker-external-1",),
        )

    monkeypatch.setattr(clerk_transactions_router, "sqlite_acknowledge_external_order", _acknowledge)
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/accounts/PA-TEST/transactions/external-orders/external-1/acknowledge",
            json={"operator": "operator-1"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "external_order_id": "external-1",
        "acknowledged_at_ms": 1_700_000_000_100,
        "ack_operator": "operator-1",
    }
    assert calls == [("PA-TEST", "external-1", "operator-1")]


async def test_external_order_acknowledgement_rejects_blank_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routers.clerk_transactions as clerk_transactions_router

    def must_not_acknowledge(**_kwargs: object) -> None:
        raise AssertionError("blank acknowledgement must fail at the API boundary")

    monkeypatch.setattr(
        clerk_transactions_router,
        "sqlite_acknowledge_external_order",
        must_not_acknowledge,
    )
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/accounts/PA-TEST/transactions/external-orders/external-1/acknowledge",
            json={"operator": "   "},
        )

    assert response.status_code == 422
    assert "non-whitespace" in response.text


async def test_external_order_acknowledgement_endpoint_durably_releases_only_external_cause(
    tmp_path: Path,
) -> None:
    class _NoBroker:
        pass

    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    observed = observe_external_order(
        repo,
        order=BrokerOrder(
            broker="alpaca",
            order_id="external-1",
            client_order_id="alpaca-console:operator-order-1",
            symbol="AAPL",
            asset_class="us_equity",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=2.0,
            filled_quantity=0.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            status="accepted",
            submitted_at_ms=None,
            created_at_ms=None,
            updated_at_ms=None,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            observed_at_ms=1_700_000_000_000,
        ),
    )
    broker = _NoBroker()
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite",
            clerk=SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker),  # type: ignore[arg-type]
        )
    )
    app = FastAPI()
    app.include_router(router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/PA-TEST/transactions/external-orders/external-1/acknowledge",
                json={"operator": "operator-1"},
            )
    finally:
        set_active_clerk_runtime(None)

    try:
        assert response.status_code == 200
        assert response.json()["external_order_id"] == observed.external_order_id
        assert repo.external_order("external-1").acknowledged_at_ms is not None  # type: ignore[union-attr]
        assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER") is None
    finally:
        repo.close()


async def test_custody_window_summary_preserves_server_folded_terminal_state_after_reordered_callbacks() -> None:
    lifecycle = fold_lifecycle_state("filled", "acknowledged")

    summary = custody_window_summary(
        [
            _summary(transaction_id="recorded", lifecycle_state="recorded"),
            _summary(transaction_id="submitting", lifecycle_state="submitting"),
            _summary(transaction_id="broker-known", lifecycle_state="submitted"),
            _summary(transaction_id="partial", lifecycle_state="partial_fill"),
            _summary(transaction_id="terminal", lifecycle_state=lifecycle),
            _summary(transaction_id="error", lifecycle_state="error"),
            _summary(transaction_id="uncertain", lifecycle_state="limbo"),
        ]
    )

    assert lifecycle == "filled"
    assert summary.model_dump() == {
        "record_count": 7,
        "a0_custody_accepted_count": 1,
        "a1_broker_write_started_count": 1,
        "a2_broker_known_count": 2,
        "a3_economic_terminal_count": 1,
        "uncertain_count": 2,
    }
