"""Tests for LivePortfolio."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.broker.ibkr.models import (
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.live_portfolio import LivePortfolio
from tests.engine.live.fixtures.fake_broker import FakeBroker


@pytest.mark.asyncio
async def test_refresh_from_broker_loads_account_and_positions() -> None:
    broker = FakeBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=756733,
                symbol="SPY",
                sec_type="STK",
                quantity=12.0,
                avg_cost=500.25,
                fetched_at_ms=1,
            )
        ],
        fetched_at_ms=1,
    )
    portfolio = LivePortfolio(broker)

    await portfolio.refresh_from_broker()

    assert portfolio.cash == Decimal("100000.0")
    assert portfolio.total_value() == Decimal("100000.0")
    assert portfolio.get_position("SPY").quantity == 12
    assert portfolio.get_position("SPY").average_price == Decimal("500.25")


def test_set_holdings_uses_reference_price_for_integer_share_count() -> None:
    portfolio = LivePortfolio(FakeBroker())
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("501.25"))

    order = portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    assert order is not None
    assert order.quantity == 199
    assert order.tag == "SetHoldings"


def test_liquidate_submits_opposite_quantity() -> None:
    portfolio = LivePortfolio(FakeBroker())
    portfolio.get_position("SPY").quantity = 17

    order = portfolio.liquidate("SPY", datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    assert order is not None
    assert order.quantity == -17
    assert order.tag == "Liquidate"


def test_set_holdings_with_fixed_shares_policy_targets_value_directly() -> None:
    """ADR 0009 PR1 — when an OrderSizer carrying FixedShares is attached, the
    set_holdings call resolves to the policy's share count, bypassing the
    percent-based sizing_model entirely.
    """
    from app.engine.execution.order_sizer import FixedShares, OrderSizer

    portfolio = LivePortfolio(FakeBroker(), order_sizer=OrderSizer(FixedShares(value=1)))
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("501.25"))

    # A positive fraction is a "go long" intent; the FixedShares policy
    # reinterprets it as "target 1 share" — not 199 (the percent-path value).
    order = portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    assert order is not None
    assert order.quantity == 1
    assert order.tag == "SetHoldings"


def test_set_holdings_with_fixed_shares_policy_zero_fraction_is_flat() -> None:
    """A flat target through FixedShares submits no order when already flat
    (delta == 0); the sizing-skip diagnostic is logged upstream by the engine.
    """
    from app.engine.execution.order_sizer import FixedShares, OrderSizer

    portfolio = LivePortfolio(FakeBroker(), order_sizer=OrderSizer(FixedShares(value=1)))
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("501.25"))

    order = portfolio.set_holdings("SPY", Decimal("0"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    assert order is None


def test_set_holdings_fails_fast_on_explicit_surface_mismatch() -> None:
    """ADR 0009 § 6 — a strategy registered as ``explicit`` invoking
    ``set_holdings`` is a registration bug; the engine halts on the first
    entry so the ledger never carries the misleading "policy_set_holdings"
    sizing record."""
    from app.engine.execution.order_sizer import FixedShares, OrderSizer

    portfolio = LivePortfolio(FakeBroker(), order_sizer=OrderSizer(FixedShares(value=1)))
    portfolio.registered_sizing_surface = "explicit"
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))

    with pytest.raises(RuntimeError, match="Order-surface mismatch"):
        portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))


def test_set_holdings_captures_audit_row_for_each_resolution() -> None:
    """ADR 0009 § 11 — every set_holdings via the policy adapter records a
    row on portfolio.sizing_resolutions, capturing the kind/value/intended_qty/
    reference_price/sized_via the cockpit later renders."""
    from app.engine.execution.order_sizer import FixedShares, OrderSizer

    portfolio = LivePortfolio(FakeBroker(), order_sizer=OrderSizer(FixedShares(value=3)))
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))

    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    assert len(portfolio.sizing_resolutions) == 1
    row = portfolio.sizing_resolutions[0]
    assert row["symbol"] == "SPY"
    assert row["policy_kind"] == "FixedShares"
    assert row["policy_value"] == "3"
    assert row["intended_qty"] == 3
    assert row["reference_price"] == "500"
    assert row["sized_via"] == "policy_set_holdings"


def test_set_holdings_with_set_holdings_policy_routes_through_lean() -> None:
    """ADR 0009 PR2 — a SetHoldings(1.0) live policy resolves through
    LeanSetHoldingsSizing (buffered, fee-aware), producing one fewer share
    than the legacy SimpleFloor portfolio default would. The contrast is the
    point of the cutover: live runs become honestly LEAN-native.
    """
    from app.engine.execution.order_sizer import (
        OrderSizer,
        SetHoldings,
        WholeAccountPortfolioValueProvider,
    )

    portfolio = LivePortfolio(FakeBroker())
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.order_sizer = OrderSizer(
        SetHoldings(fraction=Decimal("1.0")),
        portfolio_value_provider=WholeAccountPortfolioValueProvider(portfolio.total_value),
    )

    order = portfolio.set_holdings(
        "SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC)
    )

    assert order is not None
    # Lean buffered + IBKR fee: 199 shares (not 200 the legacy SimpleFloor buys).
    assert order.quantity == 199
    assert order.tag == "SetHoldings"


@pytest.mark.asyncio
async def test_submit_pending_orders_routes_through_paper_order_spec() -> None:
    broker = FakeBroker()
    portfolio = LivePortfolio(broker)
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    acks = await portfolio.submit_pending_orders()

    assert len(acks) == 1
    assert broker.orders[0].symbol == "SPY"
    assert broker.orders[0].action == "BUY"
    assert broker.orders[0].quantity == 200
    assert broker.orders[0].order_type == "MKT"
    assert broker.orders[0].confirm_paper is True
    assert broker.orders[0].client_order_id == "live-1"
    assert list(portfolio.drain_pending()) == []
