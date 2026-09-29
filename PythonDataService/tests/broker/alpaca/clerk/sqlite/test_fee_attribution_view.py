"""Trader-facing fee API consumes the same custody projection as admission."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerActivity, BrokerPosition
from app.lean_sidecar.trading_calendar import session_close_ms_utc
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
    """The one broker read Today's statement makes: the positions and their prices."""

    broker_id = "alpaca"

    def __init__(self, positions: list[BrokerPosition] | None) -> None:
        self._positions = positions
        self.position_calls = 0

    async def list_positions(self) -> list[BrokerPosition]:
        self.position_calls += 1
        if self._positions is None:
            raise BrokerUnavailable("positions are down", broker="alpaca")
        return self._positions


def _spy_at(price: float, *, prior_close: float | None = 402.0) -> BrokerPosition:
    return BrokerPosition(
        broker="alpaca", symbol="SPY", asset_id=None, asset_class="us_equity", quantity=1.0, side="long",
        average_entry_price=410.0, market_value=price, cost_basis=410.0, current_price=price,
        unrealized_pl=price - 410.0, unrealized_plpc=None, observed_at_ms=NOON, prior_close_price=prior_close,
    )


#: Friday 2026-09-04's close: the last one before Tuesday (Monday is Labor Day).
FRIDAY_CLOSE = session_close_ms_utc(date(2026, 9, 4))


def _period_account(repo, monkeypatch, positions: list[BrokerPosition] | None, *, authority_kind: str = "sqlite"):
    """Friday's buy held over the weekend and sold today, one share bought and held today, an outside MSFT order Friday."""
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
    return _serve_account(repo, monkeypatch, positions, authority_kind=authority_kind)


def _serve_account(repo, monkeypatch, positions: list[BrokerPosition] | None, *, authority_kind: str = "sqlite"):
    """Serve ``repo`` as the active account behind the brokers router, positions from a fake port."""
    from fastapi import FastAPI

    from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
    from app.routers import brokers
    from app.services import account_activity, alpaca_fee_reconciliation, sqlite_account_pnl_attribution

    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=object(), trade=object())
    runtime = SimpleNamespace(authority_kind=authority_kind, clerk=facade, startup_failure=None)
    monkeypatch.setattr(alpaca_fee_reconciliation, "active_sqlite_facade", lambda _: facade)
    monkeypatch.setattr(account_activity, "active_sqlite_facade", lambda _: facade)
    monkeypatch.setattr(sqlite_account_pnl_attribution, "get_active_clerk_runtime", lambda: runtime)
    port = _PositionsPort(positions)
    monkeypatch.setattr(brokers, "_resolve_port", lambda _: port)
    app = FastAPI()
    app.include_router(brokers.router)
    return app, port


async def _get_json(app, path: str) -> dict:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca{path}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_today_counts_only_the_move_since_the_last_close(day_pnl_repo, monkeypatch) -> None:
    """Regression (#2565 review): the open line was each lot's lifetime gain."""
    app, _ = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0)])

    result = await _get_json(app, "/today-statement")

    assert result == {
        "state": "ready",
        "detail": None,  # the outside MSFT order was Friday's; today has none to leave out
        "since_ms": FRIDAY_CLOSE,
        "observed_at_ms": NOON,
        "realized_usd": "0.50",  # 0.125 x (404 - 400): the lot closed since Friday's close
        "fees_usd": "0.03",  # today's fee day only; Friday's outside order and charge are not the bots'
        # Friday's lot moved 400 -> 402 before the close, so only 0.125 x (404 - 402)
        # of its gain is today's; today's share moved 410 -> 412. Not the lifetime 2.00.
        "open_change_usd": "1.75",  # 1 x (412 - 410) - 0.125 x (402 - 400)
        "net_usd": "2.22",
    }


async def test_today_shows_fifos_exact_gains_at_a_whole_cent_boundary(day_pnl_repo, monkeypatch) -> None:
    """#2556: Today's realized and open-change figures are rounded from FIFO's
    exact totals, never from their float views.

    0.096721714 SPY bought at $100 today, half sold at $100.3101682007 and
    half still held at that price: each half gains exactly
    $0.0149999999999999999, which shows as $0.01. The float view of each is
    0.015, which half-even rounds to $0.02.
    """
    accepted = _accept_day_pnl_enter(day_pnl_repo, decision_id="whole-cent")
    _append_day_pnl_slice(day_pnl_repo, accepted, execution_id="whole-cent-buy", side="BUY", quantity=0.096721714,
                          price=100, occurred_at_ms=TODAY_OPEN + 1_000)
    _append_day_pnl_slice(day_pnl_repo, accepted, execution_id="whole-cent-sell", side="SELL", quantity=0.048360857,
                          price=100.3101682007, occurred_at_ms=TODAY_OPEN + 2_000)
    app, _ = _serve_account(day_pnl_repo, monkeypatch, [_spy_at(100.3101682007)])

    result = await _get_json(app, "/today-statement")

    # Nothing was held at Friday's close, so today's change is the whole open gain.
    assert (result["realized_usd"], result["open_change_usd"]) == ("0.01", "0.01")


async def test_today_leaves_the_change_unknown_without_the_closing_price(day_pnl_repo, monkeypatch) -> None:
    app, _ = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0, prior_close=None)])

    result = await _get_json(app, "/today-statement")

    assert result["state"] == "unavailable"
    assert (result["realized_usd"], result["fees_usd"], result["open_change_usd"], result["net_usd"]) == (
        "0.50", "0.03", None, None,
    )
    assert result["detail"] == (
        "A share held at the last close has no closing price here, "
        "so the change in open gains and the net are not shown."
    )


async def test_unreadable_prices_keep_the_known_figures_and_never_show_zero(day_pnl_repo, monkeypatch) -> None:
    app, _ = _period_account(day_pnl_repo, monkeypatch, None)

    result = await _get_json(app, "/today-statement")

    assert result["state"] == "unavailable"
    assert (result["realized_usd"], result["fees_usd"], result["open_change_usd"], result["net_usd"]) == (
        "0.50", "0.03", None, None,
    )


async def test_today_is_not_computed_on_a_shadow_account(day_pnl_repo, monkeypatch) -> None:
    """Regression (#2565 review): the statement read custody the canonical P&L read refuses (#2220)."""
    app, _ = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0)], authority_kind="shadow")

    result = await _get_json(app, "/today-statement")

    assert result["state"] == "unavailable"
    assert result["detail"] == "Today's figures are not shown on a Shadow account yet."
    assert (result["realized_usd"], result["fees_usd"], result["open_change_usd"], result["net_usd"]) == (
        None, None, None, None,
    )


async def test_today_says_why_when_the_clerk_is_offline(day_pnl_repo, monkeypatch) -> None:
    from app.services import account_activity

    app, _ = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0)])
    monkeypatch.setattr(account_activity, "active_sqlite_facade", lambda _: None)

    result = await _get_json(app, "/today-statement")

    assert (result["state"], result["since_ms"], result["net_usd"]) == ("unavailable", None, None)
    assert result["detail"] == "Today's figures are unavailable while the account Clerk is offline."


@pytest.mark.parametrize("period", ["today", "30d", "60d"])
async def test_a_period_fee_read_makes_no_broker_call(day_pnl_repo, monkeypatch, period: str) -> None:
    """Regression (#2565 review): every fee period paid for a positions read and a FIFO scan."""
    app, port = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0)])

    result = await _get_json(app, f"/fees/attribution?period={period}")

    assert result["period"] == period
    assert "statement" not in result
    assert port.position_calls == 0


async def test_today_reads_only_todays_fee_day(day_pnl_repo, monkeypatch) -> None:
    app, _ = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0)])

    result = await _get_json(app, "/fees/attribution?period=today")

    assert result["period_start_ms"] == et_midnight_ms(date(2026, 9, 8))
    # Friday's fee day -- its observed 0.05 charge and the outside order -- is
    # not today; today's two bot fills carry their estimated fees.
    assert [(row["label"], row["order_id"], row["total_usd"]) for row in result["rows"]] == [
        (DAY_PNL_SID, None, "0.03"),
    ]


async def test_thirty_days_reach_friday_and_name_the_outside_order(day_pnl_repo, monkeypatch) -> None:
    app, _ = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0)])

    result = await _get_json(app, "/fees/attribution?period=30d")

    assert result["period_start_ms"] == et_midnight_ms(date(2026, 7, 28))
    # Friday's 0.05 charge is apportioned by the fee model's weights: the
    # outside 3-share buy outweighs the bot's 0.125 share and takes every cent.
    assert [(row["label"], row["order_id"], row["total_usd"]) for row in result["rows"]] == [
        (DAY_PNL_SID, None, "0.03"),
        ("Outside order · MSFT", "external-order-1", "0.05"),
    ]


async def test_lifetime_read_carries_no_period(day_pnl_repo, monkeypatch) -> None:
    app, _ = _period_account(day_pnl_repo, monkeypatch, [_spy_at(412.0)])

    result = await _get_json(app, "/fees/attribution")

    assert (result["period"], result["period_start_ms"]) == (None, None)
    assert sum(1 for row in result["rows"] if row["order_id"] is not None) == 1


async def test_a_period_is_refused_on_one_bots_fee_read(day_pnl_repo, monkeypatch) -> None:
    from httpx import ASGITransport, AsyncClient

    app, _ = _period_account(day_pnl_repo, monkeypatch, [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/brokers/alpaca/fees/attribution?period=today&strategy_instance_id={DAY_PNL_SID}"
        )
    assert response.status_code == 422
