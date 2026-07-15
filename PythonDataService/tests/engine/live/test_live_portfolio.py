"""Tests for LivePortfolio."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.ibkr.models import (
    IbkrConnectionHealth,
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.engine.live.account_owner import AccountOwnerSubmitIntent, AccountOwnerSubmitRejected, AccountOwnerSubmitResult
from app.engine.live.live_portfolio import (
    AccountFreezeBlockError,
    AccountRegistryBlockError,
    AccountTruthBlockError,
    LivePortfolio,
    SessionPolicyBlockError,
    SubmitUncertainHaltError,
)
from app.schemas.account_truth import (
    AccountTruthMessage,
    AccountTruthResponse,
)
from app.schemas.live_runs import GateResult
from app.services.account_truth_snapshot import AccountTruthSnapshot, account_truth_gate_result
from app.services.session_authority import SessionAuthorityState, TradingSessionPhase
from tests._helpers.account_truth import fresh_account_truth_source_freshness
from tests.engine.live.fixtures.fake_broker import FakeBroker

_NOW_MS = 1_700_000_000_000


class RealBrokerStub(FakeBroker):
    requires_durable_submit = True


def _account_truth_snapshot(
    *,
    final_verdict: str = "clean",
    generated_at_ms: int = _NOW_MS - 1_000,
    blockers: list[AccountTruthMessage] | None = None,
) -> AccountTruthSnapshot:
    severity = "ok" if final_verdict == "clean" else "critical"
    truth = AccountTruthResponse(
        account_id="DU123",
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
            account_id="DU123",
            is_paper=True,
            fetched_at_ms=generated_at_ms,
            connection_state="connected",
            last_transition_ms=generated_at_ms,
        ),
        invariants=[],
        blockers=blockers or [],
        source_freshness=fresh_account_truth_source_freshness(generated_at_ms),
    )
    return AccountTruthSnapshot(truth=truth, cached_at_ms=generated_at_ms)


def _session_state(phase: TradingSessionPhase) -> SessionAuthorityState:
    return SessionAuthorityState(
        phase=phase,
        permits_strategy_activity=phase == "RTH",
        next_transition_ms=_NOW_MS + 60_000,
        timezone="America/New_York",
        as_of_ms=_NOW_MS,
        source="nyse_calendar",
    )


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
    assert broker.orders[0].outside_rth is False
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


def test_real_broker_portfolio_requires_account_owner_submitter(tmp_path: Path) -> None:
    from app.engine.live.intent_wal import IntentWal

    with pytest.raises(ValueError, match="AccountOwner remains the sole writer"):
        LivePortfolio(
            RealBrokerStub(),
            intent_wal=IntentWal(tmp_path / "intent_events.jsonl"),
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
async def test_submit_pending_orders_blocks_when_account_truth_rejects_unexplained_position() -> None:
    broker = FakeBroker()
    blocker = AccountTruthMessage(
        code="unknown_positions",
        severity="critical",
        title="Unknown current broker positions",
        message="At least one current IBKR position is not explained by known bot/manual evidence.",
    )
    gate = account_truth_gate_result(
        _account_truth_snapshot(final_verdict="not_proven", blockers=[blocker]),
        now_ms=_NOW_MS,
    )
    portfolio = LivePortfolio(broker, account_truth_gate_provider=lambda: gate)
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(AccountTruthBlockError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.gate_result == gate
    assert gate.gate_id == "account.account_truth"
    assert gate.operator_reason == "ACCOUNT_TRUTH_UNKNOWN_POSITIONS"
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_durable_submit_truth_outage_grace_expires_at_120_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1050: a running live bot fails closed exactly after the outage grace."""

    import app.utils.timestamps as timestamps

    broker = RealBrokerStub()
    gate = GateResult(
        gate_id="account.account_truth",
        status="block",
        source="test",
        operator_reason="ACCOUNT_TRUTH_STALE",
        operator_next_step="REFRESH",
        evidence_at_ms=_NOW_MS,
    )
    now = [_NOW_MS]
    monkeypatch.setattr(timestamps, "now_ms_utc", lambda: now[0])
    async def submitter(intent: AccountOwnerSubmitIntent) -> AccountOwnerSubmitResult:
        return AccountOwnerSubmitResult(
            status="accepted",
            trace_id=intent.trace_id,
            account_id=intent.account_id,
            strategy_instance_id=intent.strategy_instance_id,
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            owner_generation=intent.owner_generation,
            order_id=1,
        )

    portfolio = LivePortfolio(
        broker,
        account_truth_gate_provider=lambda: gate,
        account_owner_submitter=submitter,
        bot_order_namespace="learn-ai/test/v1",
        account_id="DU123",
        strategy_instance_id="test",
        run_id="run",
        owner_generation_provider=lambda: 1,
    )
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))

    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))
    await portfolio.submit_pending_orders()
    now[0] += 119_999
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))
    await portfolio.submit_pending_orders()
    now[0] += 1
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(AccountTruthBlockError):
        await portfolio.submit_pending_orders()


@pytest.mark.asyncio
async def test_submit_pending_orders_blocks_outside_allowed_session_before_broker_call() -> None:
    broker = FakeBroker()
    portfolio = LivePortfolio(
        broker,
        session_gate_provider=lambda: _session_state("POST"),
        allowed_sessions=("RTH",),
    )
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(SessionPolicyBlockError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.reason == "strategy_session_not_permitted"
    assert exc.value.session_state.phase == "POST"
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_allows_rth_session() -> None:
    broker = FakeBroker()
    portfolio = LivePortfolio(
        broker,
        session_gate_provider=lambda: _session_state("RTH"),
        allowed_sessions=("RTH",),
    )
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    acks = await portfolio.submit_pending_orders()

    assert len(acks) == 1
    assert len(broker.orders) == 1
    assert broker.orders[0].symbol == "SPY"


@pytest.mark.asyncio
async def test_submit_pending_orders_uses_limit_outside_rth_for_declared_extended_session() -> None:
    broker = FakeBroker()
    portfolio = LivePortfolio(
        broker,
        session_gate_provider=lambda: _session_state("POST"),
        allowed_sessions=("RTH", "POST"),
        order_mechanism_sessions=("RTH", "POST"),
    )
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500.25"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    acks = await portfolio.submit_pending_orders()

    assert len(acks) == 1
    assert len(broker.orders) == 1
    assert broker.orders[0].order_type == "LMT"
    assert broker.orders[0].limit_price == 500.25
    assert broker.orders[0].outside_rth is True


@pytest.mark.asyncio
async def test_submit_pending_orders_blocks_extended_session_without_reference_price() -> None:
    broker = FakeBroker()
    portfolio = LivePortfolio(
        broker,
        session_gate_provider=lambda: _session_state("POST"),
        allowed_sessions=("RTH", "POST"),
        order_mechanism_sessions=("RTH", "POST"),
    )
    portfolio.submit_market_order("SPY", 1, datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(SessionPolicyBlockError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.reason == "extended_limit_price_unavailable"
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_blocks_declared_extended_session_until_mechanism_enabled() -> None:
    broker = FakeBroker()
    portfolio = LivePortfolio(
        broker,
        session_gate_provider=lambda: _session_state("POST"),
        allowed_sessions=("RTH", "POST"),
        order_mechanism_sessions=("RTH",),
    )
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(SessionPolicyBlockError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.reason == "order_mechanism_not_enabled"
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_drops_wal_intent_when_session_policy_blocks(tmp_path: Path) -> None:
    from app.engine.execution.order_sizer import FixedShares, OrderSizer
    from app.engine.live.intent_events import IntentEventType
    from app.engine.live.intent_wal import IntentWal
    from app.engine.live.order_identity import build_bot_order_namespace

    broker = FakeBroker()
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    portfolio = LivePortfolio(
        broker,
        intent_wal=wal,
        bot_order_namespace=build_bot_order_namespace("session-policy-block"),
        session_gate_provider=lambda: _session_state("PRE"),
        allowed_sessions=("RTH",),
    )
    portfolio.order_sizer = OrderSizer(FixedShares(value=10))
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(SessionPolicyBlockError):
        await portfolio.submit_pending_orders()

    events = wal.read_tail()
    assert [event.event_type for event in events] == [
        IntentEventType.SIZING_RESOLVED,
        IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT,
    ]
    assert events[1].intent_id == events[0].intent_id
    assert events[1].drop_reason == "session_policy_block"
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_drops_wal_intent_when_account_truth_blocks(tmp_path: Path) -> None:
    from app.engine.execution.order_sizer import FixedShares, OrderSizer
    from app.engine.live.intent_events import IntentEventType
    from app.engine.live.intent_wal import IntentWal
    from app.engine.live.order_identity import build_bot_order_namespace

    broker = FakeBroker()
    blocker = AccountTruthMessage(
        code="unknown_positions",
        severity="critical",
        title="Unknown current broker positions",
        message="At least one current IBKR position is not explained by known bot/manual evidence.",
    )
    gate = account_truth_gate_result(
        _account_truth_snapshot(final_verdict="not_proven", blockers=[blocker]),
        now_ms=_NOW_MS,
    )
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    portfolio = LivePortfolio(
        broker,
        intent_wal=wal,
        bot_order_namespace=build_bot_order_namespace("account-truth-block-test"),
        account_truth_gate_provider=lambda: gate,
    )
    portfolio.order_sizer = OrderSizer(FixedShares(value=10))
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(AccountTruthBlockError):
        await portfolio.submit_pending_orders()

    events = wal.read_tail()
    assert [event.event_type for event in events] == [
        IntentEventType.SIZING_RESOLVED,
        IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT,
    ]
    assert events[1].intent_id == events[0].intent_id
    assert events[1].drop_reason == "account_truth_block"
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_drops_wal_intent_when_account_registry_blocks(tmp_path: Path) -> None:
    from app.engine.execution.order_sizer import FixedShares, OrderSizer
    from app.engine.live.intent_events import IntentEventType
    from app.engine.live.intent_wal import IntentWal
    from app.engine.live.order_identity import build_bot_order_namespace

    broker = FakeBroker()
    gate = GateResult(
        gate_id="account.instance_registry",
        status="block",
        source="account_instance_registry",
        operator_reason="ACCOUNT_REGISTRY_STALE_RUN",
        operator_next_step="STOP_STALE_RUNNER",
        evidence_at_ms=1_700_000_000_000,
    )
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    portfolio = LivePortfolio(
        broker,
        intent_wal=wal,
        bot_order_namespace=build_bot_order_namespace("registry-block"),
        account_registry_gate_provider=lambda: gate,
    )
    portfolio.order_sizer = OrderSizer(FixedShares(value=10))
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(AccountRegistryBlockError):
        await portfolio.submit_pending_orders()

    events = wal.read_tail()
    assert [event.event_type for event in events] == [
        IntentEventType.SIZING_RESOLVED,
        IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT,
    ]
    assert events[1].drop_reason == "account_registry_block"
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_drops_wal_intent_when_account_freeze_blocks(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from app.engine.execution.order_sizer import FixedShares, OrderSizer
    from app.engine.live.intent_events import IntentEventType
    from app.engine.live.intent_wal import IntentWal
    from app.engine.live.order_identity import build_bot_order_namespace

    broker = FakeBroker()
    freeze = SimpleNamespace(reason="restart_intensity.threshold_breached")
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    portfolio = LivePortfolio(
        broker,
        intent_wal=wal,
        bot_order_namespace=build_bot_order_namespace("account-freeze-block-test"),
        account_freeze_provider=lambda: freeze,
    )
    portfolio.order_sizer = OrderSizer(FixedShares(value=10))
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(AccountFreezeBlockError):
        await portfolio.submit_pending_orders()

    events = wal.read_tail()
    assert [event.event_type for event in events] == [
        IntentEventType.SIZING_RESOLVED,
        IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT,
    ]
    assert events[1].drop_reason == "account_freeze_block"
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_blocks_when_account_truth_snapshot_is_stale() -> None:
    broker = FakeBroker()
    gate = account_truth_gate_result(
        _account_truth_snapshot(generated_at_ms=_NOW_MS - 60_001),
        now_ms=_NOW_MS,
    )
    portfolio = LivePortfolio(broker, account_truth_gate_provider=lambda: gate)
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(AccountTruthBlockError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.gate_result == gate
    assert gate.operator_reason == "ACCOUNT_TRUTH_STALE"
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_blocks_when_account_truth_snapshot_is_missing() -> None:
    broker = FakeBroker()
    gate = account_truth_gate_result(None, now_ms=_NOW_MS)
    portfolio = LivePortfolio(broker, account_truth_gate_provider=lambda: gate)
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(AccountTruthBlockError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.gate_result == gate
    assert gate.operator_reason == "ACCOUNT_TRUTH_NOT_AVAILABLE"
    assert broker.orders == []
    assert list(portfolio.drain_pending()) == []


@pytest.mark.asyncio
async def test_submit_pending_orders_allows_fresh_clean_account_truth_gate() -> None:
    broker = FakeBroker()
    gate = account_truth_gate_result(_account_truth_snapshot(), now_ms=_NOW_MS)
    portfolio = LivePortfolio(broker, account_truth_gate_provider=lambda: gate)
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    acks = await portfolio.submit_pending_orders()

    assert gate.status == "pass"
    assert len(acks) == 1
    assert broker.orders[0].symbol == "SPY"


@pytest.mark.asyncio
async def test_submit_pending_orders_logs_observation_lease_shadow_divergence_without_blocking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broker = FakeBroker()
    comparisons: list[tuple[GateResult, GateResult]] = []
    truth_gate = account_truth_gate_result(_account_truth_snapshot(), now_ms=_NOW_MS)
    lease_gate = GateResult(
        gate_id="account.observation_lease",
        status="block",
        source="account_observation_lease",
        operator_reason="ACCOUNT_OBSERVATION_LEASE_ABSENT",
        operator_next_step="RECONCILE_NOW",
        evidence_at_ms=_NOW_MS,
    )
    portfolio = LivePortfolio(
        broker,
        account_truth_gate_provider=lambda: truth_gate,
        account_observation_lease_gate_provider=lambda: lease_gate,
        account_observation_lease_shadow_comparison_observer=lambda truth, lease: comparisons.append(
            (truth, lease)
        ),
    )
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    acks = await portfolio.submit_pending_orders()

    assert len(acks) == 1
    assert "account observation lease shadow divergence" in caplog.text
    assert comparisons == [(truth_gate, lease_gate)]


@pytest.mark.asyncio
async def test_submit_pending_orders_ignores_observation_lease_shadow_provider_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broker = FakeBroker()
    truth_gate = account_truth_gate_result(_account_truth_snapshot(), now_ms=_NOW_MS)

    def broken_lease_gate() -> GateResult:
        raise OSError("lease artifact read failed")

    portfolio = LivePortfolio(
        broker,
        account_truth_gate_provider=lambda: truth_gate,
        account_observation_lease_gate_provider=broken_lease_gate,
    )
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    acks = await portfolio.submit_pending_orders()

    assert len(acks) == 1
    assert broker.orders[0].symbol == "SPY"
    assert "account observation lease shadow gate read failed" in caplog.text


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


@pytest.mark.asyncio
async def test_account_owner_uncertain_submit_raises_typed_halt() -> None:
    broker = FakeBroker()

    async def submitter(intent: AccountOwnerSubmitIntent) -> AccountOwnerSubmitResult:
        return AccountOwnerSubmitResult(
            status="uncertain",
            trace_id=intent.trace_id,
            account_id=intent.account_id,
            strategy_instance_id=intent.strategy_instance_id,
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            owner_generation=intent.owner_generation,
            reason="BROKER_SUBMIT_UNCERTAIN:TimeoutError",
        )

    portfolio = LivePortfolio(
        broker,
        account_owner_submitter=submitter,
        account_id="DU123",
        strategy_instance_id="spy_ema_paper",
        run_id="run-alpha",
        bot_order_namespace="learn-ai/spy_ema_paper/v1",
        owner_generation_provider=lambda: 3,
    )
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(SubmitUncertainHaltError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.probe_result == "uncertain"
    assert exc.value.reason == "BROKER_SUBMIT_UNCERTAIN:TimeoutError"
    assert broker.orders == []


@pytest.mark.asyncio
async def test_account_owner_rejected_submit_raises_typed_halt_without_broker_flatten() -> None:
    broker = FakeBroker()

    async def submitter(intent: AccountOwnerSubmitIntent) -> AccountOwnerSubmitResult:
        raise AccountOwnerSubmitRejected(reason="BROKER_STATE_UNPROVABLE", diagnostics={})

    portfolio = LivePortfolio(
        broker,
        account_owner_submitter=submitter,
        account_id="DU123",
        strategy_instance_id="spy_ema_paper",
        run_id="run-alpha",
        bot_order_namespace="learn-ai/spy_ema_paper/v1",
        owner_generation_provider=lambda: 3,
    )
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(SubmitUncertainHaltError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.probe_result == "rejected"
    assert exc.value.reason == "BROKER_STATE_UNPROVABLE"
    assert broker.orders == []
