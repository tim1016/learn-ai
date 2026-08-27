from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime, set_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.economic_projection_models import AccountPnlAttribution
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.models import ExternalOrderResource
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerOrder
from app.routers.account_pnl_attribution import router as account_pnl_attribution_router
from app.routers.clerk_transactions import router

pytestmark = pytest.mark.asyncio


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


async def test_history_endpoint_rejects_timestamp_beyond_int64() -> None:
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/accounts/DU1219/transactions",
            params={"from_ms": 2**63},
        )

    assert response.status_code == 422


async def test_transaction_history_surfaces_unavailable_without_active_authority() -> None:
    """No active SQLite authority must surface as unavailable, not silent success.

    Renamed from test_missing_alpaca_activation_never_reads_ibkr_projection
    (PR-A of #1813) — the poisoned-IBKR-store half of that proof no longer
    applies (there is no legacy store left for the router to reach for), but
    nothing else in the suite exercises the "no active authority" degraded
    response for GET /transactions.
    """
    set_active_clerk_runtime(None)
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/accounts/PA-NO-ACTIVATION/transactions")

    assert response.status_code == 200
    assert response.json()["feed_state"] == "projection_unavailable"
    assert response.json()["rows"] == []


async def test_transaction_detail_surfaces_unavailable_without_active_authority() -> None:
    """No active SQLite authority must surface as unavailable, not silent success.

    Renamed from test_missing_alpaca_activation_never_reads_ibkr_transaction_detail
    (PR-A of #1813) — same reasoning as the history test above, for
    GET /transactions/{transaction_id}.
    """
    set_active_clerk_runtime(None)
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/accounts/PA-NO-ACTIVATION/transactions/legacy-row")

    assert response.status_code == 503
    assert response.json()["detail"] == "Clerk transaction projection unavailable."


async def test_pnl_attribution_endpoint_passes_the_inclusive_window_to_c2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routers.account_pnl_attribution as account_pnl_attribution

    def attribution(*, account_id: str, from_ms: int, to_ms: int) -> AccountPnlAttribution:
        assert (account_id, from_ms, to_ms) == ("DU1219", 1_700_000_000_000, 1_700_086_400_000)
        return AccountPnlAttribution(
            account_id=account_id,
            authority_generation=1,
            control_revision=2,
            from_ms=from_ms,
            to_ms=to_ms,
            attribution_rows=(),
            realized_pnl_total=12.5,
            start_open_pnl_total=0.0,
            open_pnl_total=0.0,
            fee_total=0.0,
            fee_fidelity="reported",
            execution_coverage="complete",
            marks_complete=True,
            start_mark_observed_at_ms={},
            mark_observed_at_ms={},
        )

    monkeypatch.setattr(account_pnl_attribution, "sqlite_account_pnl_attribution", attribution)
    app = FastAPI()
    app.include_router(account_pnl_attribution_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/accounts/DU1219/pnl-attribution",
            params={"from_ms": 1_700_000_000_000, "to_ms": 1_700_086_400_000},
        )

    assert response.status_code == 200
    assert response.json()["realized_pnl_total"] == 12.5
    assert response.json()["mark_observed_at_ms"] == {}


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
