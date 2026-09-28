"""Trader-facing fee API consumes the same custody projection as admission."""
from __future__ import annotations

from datetime import date

from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerActivity, BrokerPosition
from app.utils.session_anchors import et_midnight_ms
from tests.broker.alpaca.clerk.sqlite.conftest import (
    DAY_PNL_SID,
    NOON,
    TODAY_OPEN,
    YESTERDAY_NOON,
    YESTERDAY_OPEN,
    _accept_day_pnl_enter,
    _append_day_pnl_slice,
    _observe_foreign_order,
)
from tests.broker.alpaca.clerk.sqlite.test_fee_evidence import _activity, _seed


async def test_account_api_reports_exact_fee_ownership_from_custody(day_pnl_repo, monkeypatch) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
    from app.routers import brokers
    from app.services import alpaca_fee_reconciliation

    repo = day_pnl_repo
    _seed(repo)
    record_fee_evidence(repo, [_activity("fee", "FEE", YESTERDAY_NOON, -0.05)], checked_at_ms=NOON, history_complete=True)
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=object(), trade=object())
    monkeypatch.setattr(alpaca_fee_reconciliation, "active_sqlite_facade", lambda _: facade)
    monkeypatch.setattr(brokers, "_resolve_port", lambda _: object())
    app = FastAPI()
    app.include_router(brokers.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/fees/attribution")
    assert response.status_code == 200
    result = response.json()
    assert result["known"] is True
    assert result["authority_revision"] == repo.control_meta_snapshot().control_revision
    assert result["rows"][0]["strategy_instance_id"] == DAY_PNL_SID
    assert result["rows"][0]["observed_usd"] == "0.05"
    assert result["rows"][0]["total_usd"] == "0.05"
    assert result["account_unattributed_usd"] == "0"


async def test_bot_fee_read_selects_its_synthetic_authority(day_pnl_repo, tmp_path, monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
    from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
    from app.services import alpaca_fee_reconciliation, bot_runner
    from app.services.broker_v2_panel import panel_data_source
    from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

    record_fee_evidence(day_pnl_repo, [_activity("real-fee", "FEE", YESTERDAY_NOON, -500)], checked_at_ms=NOON, history_complete=True)
    real = SqliteAlpacaClerkFacade(account_mode="paper", repo=day_pnl_repo, read=object(), trade=object())
    simulated_repo = ClerkSqliteRepository.initialize(account_id="sim:dry", artifacts_root=tmp_path / "dry", clock=_clock_at(NOON))
    simulated = SqliteAlpacaClerkFacade(account_mode="paper", repo=simulated_repo, read=object(), trade=object(), authority_kind="synthetic")
    binding = SimpleNamespace(mode="dry_run", strategy_instance_id="dry")
    registry = SimpleNamespace(binding_for_control=lambda broker, sid: binding)

    @asynccontextmanager
    async def select(selected_registry, selected_binding):
        assert selected_registry is registry and selected_binding is binding
        yield simulated

    monkeypatch.setattr(alpaca_fee_reconciliation, "active_sqlite_facade", lambda _: real)
    monkeypatch.setattr(bot_runner, "get_bot_task_registry", lambda: registry)
    monkeypatch.setattr(panel_data_source, "_panel_authority_for_binding", select)
    try:
        view = await alpaca_fee_reconciliation.deployment_fee_attribution("dry")
        assert view.account_id == "sim:dry"
        assert view.known and view.rows == []
        assert view.account_unattributed_usd == "0"
    finally:
        simulated_repo.close()


class _PositionsPort:
    """The one broker read a period statement makes: current prices."""

    def __init__(self, positions: list[BrokerPosition] | None) -> None:
        self._positions = positions

    async def list_positions(self) -> list[BrokerPosition]:
        if self._positions is None:
            raise BrokerUnavailable("positions are down", broker="alpaca")
        return self._positions


def _spy_at(price: float) -> BrokerPosition:
    return BrokerPosition(
        broker="alpaca", symbol="SPY", asset_id=None, asset_class="us_equity", quantity=1.0, side="long",
        average_entry_price=410.0, market_value=price, cost_basis=410.0, current_price=price,
        unrealized_pl=price - 410.0, unrealized_plpc=None, observed_at_ms=NOON,
    )


def _period_account(repo, monkeypatch, positions: list[BrokerPosition] | None):
    """A day of trading: Friday's buy sold today, one share bought and held, an outside MSFT order Friday."""
    from fastapi import FastAPI

    from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
    from app.routers import brokers
    from app.services import alpaca_fee_reconciliation

    accepted = _accept_day_pnl_enter(repo, decision_id="period-slices")
    _append_day_pnl_slice(repo, accepted, execution_id="buy-friday", side="BUY", quantity=0.125, price=400,
                          occurred_at_ms=YESTERDAY_NOON)
    _append_day_pnl_slice(repo, accepted, execution_id="sell-friday-lot", side="SELL", quantity=0.125,
                          price=404, occurred_at_ms=TODAY_OPEN + 1_000)
    _append_day_pnl_slice(repo, accepted, execution_id="buy-held", side="BUY", quantity=1, price=410,
                          occurred_at_ms=TODAY_OPEN + 2_000)
    _observe_foreign_order(repo, observed_at_ms=YESTERDAY_OPEN)
    outside = BrokerActivity(
        broker="alpaca", activity_id="outside-fill", native_order_id="external-order-1", activity_type="FILL",
        category="trade_activity", symbol="MSFT", side="buy", quantity=3.0, price=50.0,
        net_amount=None, occurred_at_ms=YESTERDAY_OPEN, observed_at_ms=NOON,
    )
    record_fee_evidence(repo, [_activity("fee", "FEE", YESTERDAY_NOON, -0.05), outside],
                        checked_at_ms=NOON, history_complete=True)
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=object(), trade=object())
    monkeypatch.setattr(alpaca_fee_reconciliation, "active_sqlite_facade", lambda _: facade)
    monkeypatch.setattr(brokers, "_resolve_port", lambda _: _PositionsPort(positions))
    app = FastAPI()
    app.include_router(brokers.router)
    return app


async def _get_fees(app, query: str) -> dict:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/fees/attribution{query}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_today_reads_only_todays_fee_day_and_authors_its_statement(day_pnl_repo, monkeypatch) -> None:
    app = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0)])

    result = await _get_fees(app, "?period=today")

    assert result["period"] == "today"
    assert result["period_start_ms"] == et_midnight_ms(date(2026, 9, 8))
    # Friday's fee day -- its observed 0.05 charge and the outside order -- is
    # not today; today's two bot fills carry their estimated fees.
    assert [(row["label"], row["order_id"], row["total_usd"]) for row in result["rows"]] == [
        (DAY_PNL_SID, None, "0.03"),
    ]
    assert result["statement"] == {
        "state": "ready",
        "detail": None,
        "realized_usd": "0.50",  # 0.125 x (404 - 400)
        "fees_usd": "0.03",
        "open_usd": "2.00",  # 1 x (412 - 410)
        "net_usd": "2.47",
    }


async def test_thirty_days_reach_friday_and_name_the_outside_order(day_pnl_repo, monkeypatch) -> None:
    app = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0)])

    result = await _get_fees(app, "?period=30d")

    assert result["period_start_ms"] == et_midnight_ms(date(2026, 7, 28))
    # Friday's 0.05 charge is apportioned by the fee model's weights: the
    # outside 3-share buy outweighs the bot's 0.125 share and takes every cent.
    assert [(row["label"], row["order_id"], row["total_usd"]) for row in result["rows"]] == [
        (DAY_PNL_SID, None, "0.03"),
        ("Outside order · MSFT", "external-order-1", "0.05"),
    ]
    assert result["statement"]["fees_usd"] == "0.03"
    assert result["statement"]["net_usd"] == "2.47"
    assert result["statement"]["detail"] == (
        "Orders placed outside the bots, and account charges not matched to a bot, are not counted here."
    )


async def test_unreadable_prices_keep_the_fees_and_never_show_zero_open(day_pnl_repo, monkeypatch) -> None:
    app = _period_account(day_pnl_repo, monkeypatch, None)

    result = await _get_fees(app, "?period=today")

    assert result["rows"][0]["total_usd"] == "0.03"
    assert result["statement"]["state"] == "unavailable"
    assert result["statement"]["open_usd"] is None
    assert result["statement"]["net_usd"] is None


async def test_lifetime_read_carries_no_period_or_statement(day_pnl_repo, monkeypatch) -> None:
    app = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0)])

    result = await _get_fees(app, "")

    assert (result["period"], result["period_start_ms"], result["statement"]) == (None, None, None)
    assert sum(1 for row in result["rows"] if row["order_id"] is not None) == 1


async def test_a_period_is_refused_on_one_bots_fee_read(day_pnl_repo, monkeypatch) -> None:
    from httpx import ASGITransport, AsyncClient

    app = _period_account(day_pnl_repo, monkeypatch, [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/brokers/alpaca/fees/attribution?period=today&strategy_instance_id={DAY_PNL_SID}"
        )
    assert response.status_code == 422
