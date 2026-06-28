"""Tests for LivePortfolio."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.broker.ibkr.models import (
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.engine.live.account_owner import AccountOwnerSubmitIntent, AccountOwnerSubmitResult
from app.engine.live.live_portfolio import (
    AccountFreezeBlockError,
    AccountRegistryBlockError,
    LivePortfolio,
)
from app.schemas.live_runs import GateResult
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


def test_market_order_fails_fast_on_policy_surface_mismatch_vcr_p3_f() -> None:
    """ADR 0009 § 6 reverse / VCR-P3-F — a strategy registered as
    ``policy`` invoking ``market_order`` (the explicit surface) is the
    mirror of the forward case. ``submit_market_order(explicit_call=True)``
    must refuse on a policy-registered portfolio.

    Internal callers (``set_holdings``, ``liquidate``, engine-internal
    flatten paths) do NOT pass ``explicit_call=True`` and so remain
    unaffected — covered by the no-flag test below."""
    portfolio = LivePortfolio(FakeBroker())
    portfolio.registered_sizing_surface = "policy"
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))

    with pytest.raises(RuntimeError, match=r"Order-surface mismatch.*VCR-P3-F"):
        portfolio.submit_market_order(
            "SPY",
            100,
            datetime(2026, 5, 4, 14, 45, tzinfo=UTC),
            explicit_call=True,
        )


def test_market_order_internal_callers_unaffected_by_p3_f_guard() -> None:
    """The reverse guard fires only when ``explicit_call=True``. The
    internal callers (``set_holdings`` and the engine flatten paths)
    invoke ``submit_market_order`` without the flag and must keep
    working on a policy-registered portfolio."""
    from app.engine.execution.order_sizer import FixedShares, OrderSizer

    portfolio = LivePortfolio(FakeBroker(), order_sizer=OrderSizer(FixedShares(value=2)))
    portfolio.registered_sizing_surface = "policy"
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))

    # set_holdings is the policy surface; calling it on policy-registered
    # is the CORRECT case and must succeed even with the new guard.
    order = portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))
    assert order is not None
    assert order.quantity == 2  # FixedShares(2)

    # A direct internal submit_market_order without explicit_call=True
    # (e.g. engine recovery_flatten constructs an Order directly) must
    # also keep working.
    order2 = portfolio.submit_market_order("SPY", -1, datetime(2026, 5, 4, 14, 46, tzinfo=UTC))
    assert order2.quantity == -1


def test_market_order_explicit_call_on_explicit_surface_proceeds_p3_f() -> None:
    """Sanity inverse: a strategy CORRECTLY registered as ``explicit``
    calling ``market_order`` must succeed — the guard fires only on the
    mismatch direction."""
    portfolio = LivePortfolio(FakeBroker())
    portfolio.registered_sizing_surface = "explicit"
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))

    order = portfolio.submit_market_order(
        "SPY",
        100,
        datetime(2026, 5, 4, 14, 45, tzinfo=UTC),
        explicit_call=True,
    )
    assert order.quantity == 100


def test_market_order_unregistered_surface_proceeds_p3_f() -> None:
    """Backward-compat: a portfolio whose ``registered_sizing_surface`` is
    ``None`` (legacy callers / tests) must let ``market_order`` through
    regardless of ``explicit_call`` — the guard requires an explicit
    ``policy`` registration to fire."""
    portfolio = LivePortfolio(FakeBroker())
    # registered_sizing_surface stays None — default.
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))

    order = portfolio.submit_market_order(
        "SPY",
        100,
        datetime(2026, 5, 4, 14, 45, tzinfo=UTC),
        explicit_call=True,
    )
    assert order.quantity == 100


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

    order = portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

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


@pytest.mark.asyncio
async def test_submit_pending_orders_blocks_when_account_is_frozen() -> None:
    broker = FakeBroker()
    freeze = AccountFreezeEvidence(
        account_id="DU123",
        reason="watchdog.flatten_failed",
        source="watchdog_halt_executor",
        recorded_at_ms=1_700_000_000_000,
        operator_next_step="CHECK_IBKR",
    )
    portfolio = LivePortfolio(broker, account_freeze_provider=lambda: freeze)
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(AccountFreezeBlockError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.evidence == freeze
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_allows_reduce_only_liquidation_when_account_is_frozen() -> None:
    broker = FakeBroker()
    freeze = AccountFreezeEvidence(
        account_id="DU123",
        reason="watchdog.flatten_failed",
        source="watchdog_halt_executor",
        recorded_at_ms=1_700_000_000_000,
        operator_next_step="CHECK_IBKR",
    )
    portfolio = LivePortfolio(broker, account_freeze_provider=lambda: freeze)
    portfolio.get_position("SPY").quantity = Decimal("10")
    portfolio.liquidate("SPY", datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    acks = await portfolio.submit_pending_orders()

    assert len(acks) == 1
    assert broker.orders[0].symbol == "SPY"
    assert broker.orders[0].action == "SELL"
    assert broker.orders[0].quantity == 10


def test_account_owner_mode_rejects_run_scoped_intent_wal() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        LivePortfolio(
            FakeBroker(),
            intent_wal=object(),  # type: ignore[arg-type]
            account_owner_submitter=object(),
            bot_order_namespace="learn-ai/spy_ema_paper/v1",
        )


@pytest.mark.asyncio
async def test_submit_pending_orders_blocks_when_account_registry_rejects() -> None:
    broker = FakeBroker()
    gate = GateResult(
        gate_id="account.instance_registry",
        status="block",
        source="account_instance_registry",
        operator_reason="ACCOUNT_REGISTRY_STALE_RUN",
        operator_next_step="STOP_STALE_RUNNER",
        evidence_at_ms=1_700_000_000_000,
    )
    portfolio = LivePortfolio(broker, account_registry_gate_provider=lambda: gate)
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(AccountRegistryBlockError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.gate_result == gate
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_routes_to_account_owner_when_enabled() -> None:
    broker = FakeBroker()
    captured: list[AccountOwnerSubmitIntent] = []

    async def submitter(intent: AccountOwnerSubmitIntent) -> AccountOwnerSubmitResult:
        captured.append(intent)
        return AccountOwnerSubmitResult(
            status="accepted",
            trace_id=intent.trace_id,
            account_id=intent.account_id,
            strategy_instance_id=intent.strategy_instance_id,
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            owner_generation=intent.owner_generation,
            order_id=44,
            perm_id=90044,
        )

    portfolio = LivePortfolio(
        broker,
        account_owner_submitter=submitter,
        account_id="DU123",
        strategy_instance_id="spy_ema_paper",
        run_id="run-alpha",
        bot_order_namespace="learn-ai/spy_ema_paper/v1",
        owner_generation_provider=lambda: 3,
        trace_id_provider=lambda: "trace-owner-1",
    )
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    acks = await portfolio.submit_pending_orders()

    assert len(captured) == 1
    assert captured[0].trace_id == "trace-owner-1"
    assert captured[0].account_id == "DU123"
    assert captured[0].run_id == "run-alpha"
    assert captured[0].bot_order_namespace == "learn-ai/spy_ema_paper/v1"
    assert captured[0].owner_generation == 3
    assert captured[0].order_spec["symbol"] == "SPY"
    assert acks[0].order_id == 44
    assert broker.orders == []
