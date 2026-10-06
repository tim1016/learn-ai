"""Every price an order uses comes from the traded instrument's own bar (#2912).

A bot listens to one symbol, its Signal stream, and its Action Plan names the
asset it trades. Each test here gives the Clerk a plan that trades QQQ on a
SPY decision bar and proves which bar a price was read from: the cash check at
an ENTER, the fill a Live Shadow and a Dry Run synthesize, and what happens
when the traded asset has no bar for the decision's minute.

Before #2912 the Clerk priced from the decision bar whatever the plan traded:
the cash check used SPY's close for a QQQ order, and a synthesized fill raised
an error the entry code does not catch.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_CASH_EXCEEDED, LIVE_ENVELOPE_UNOBSERVED
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide, OrderType
from app.marketdata.feed import MarketDataBar
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services import market_liveness
from app.services.bot_binding_authority import SyntheticBindingAuthority
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _FakeReadPort, _FakeTradePort, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS
from tests.broker.alpaca.clerk.sqlite.test_exit import _broker_order, _FakeTrade
from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import _binding
from tests.broker.alpaca.clerk.sqlite.test_two_bots_one_symbol import (
    _ON_TIME_UNTIL_MS,
    _decision_of,
    _OneAccount,
    one_account,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.test_shadow_broker import DAY, MINUTE_MS, _Clock, _LiveRead
from tests.broker.v2panel.test_dry_run_recovery import _publish_quote

# #2596: these ENTERs run on the default clock; keep it inside a session.
pytestmark = pytest.mark.usefixtures("wall_clock_in_session")

SIGNAL = "SPY"
TRADED = "QQQ"
_TRADES_QQQ = alpaca_v1_action_plan(TRADED)


def _minute_bar(symbol: str, *, close: str, end_ms: int = NOON) -> MarketDataBar:
    price = Decimal(close)
    return MarketDataBar(
        feed_id="ibkr", symbol=symbol, start_ms=end_ms - MINUTE_MS, end_ms=end_ms, open=price,
        high=price, low=price, close=price, volume=100, fetched_at_ms=end_ms, session_phase="RTH",
    )


def _retained(symbol: str, *, close: str, end_ms: int = NOON, account_id: str = "BUDGET-PAPER") -> RetainedSourceBar:
    """A retained bar the way a runner hands it to a broker-backed Clerk, which verifies no ledger."""
    return RetainedSourceBar.from_market_bar(
        seq=1, account_id=account_id, bar=_minute_bar(symbol, close=close, end_ms=end_ms)
    )


class _EchoingTradePort(_FakeTradePort):
    """The shared fake answers every submit as SPY; an order's own symbol must come back here."""

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        order = await super().submit(leg, client_order_id=client_order_id)
        return order.model_copy(update={"symbol": leg.symbol})


# ── the cash check at an ENTER ──────────────────────────────────────────


async def _b_decides_enter(
    account: _OneAccount, *, quantity: int, pricing_bar: RetainedSourceBar | None, with_decision_bar: bool = True
) -> EffectOperationReceipt:
    """Bot B, whose budget is $500, decides ENTER on SPY's $100 noon bar and trades QQQ."""
    decision_bar = _retained(SIGNAL, close="100")
    decision = _decision_of("b")
    return await account.clerk.execute_for_instance(
        strategy_instance_id="b", run_id="run-b", decision_id=decision, purpose=EffectPurpose.ENTER,
        action_plan=_TRADES_QQQ, quantity=quantity,
        retained_source_bar=decision_bar if with_decision_bar else None, pricing_bar=pricing_bar,
        decision_evidence=EffectDecisionEvidence(
            evaluation_id=decision, bar_ref=decision_bar.bar_ref, symbol=SIGNAL, outcome="enter_intent",
            observed_at_ms=NOON, decision_bar_close_ms=NOON, decision_valid_until_ms=_ON_TIME_UNTIL_MS,
        ),
    )


async def test_an_entry_that_fits_the_budget_at_the_traded_assets_price_is_sent(
    one_account: _OneAccount,  # noqa: F811 -- the imported fixture
) -> None:
    """Ten QQQ at $40 are $400 of a $500 budget. At SPY's $100 they would be $1,000 and refused."""
    receipt = await _b_decides_enter(one_account, quantity=10, pricing_bar=_retained(TRADED, close="40"))

    assert receipt.state is not EffectOperationState.REJECTED, receipt.explanation
    (leg,) = one_account.trade.submitted_legs
    assert (leg.symbol, leg.side, leg.quantity) == (TRADED, OrderSide.BUY, 10.0)


async def test_an_entry_past_the_budget_at_the_traded_assets_price_is_refused(
    one_account: _OneAccount,  # noqa: F811 -- the imported fixture
) -> None:
    """Three QQQ at $400 are $1,200 of a $500 budget. At SPY's $100 they would be $300 and admitted."""
    receipt = await _b_decides_enter(one_account, quantity=3, pricing_bar=_retained(TRADED, close="400"))

    assert receipt.state is EffectOperationState.REJECTED
    assert receipt.explanation.startswith(LIVE_ENVELOPE_CASH_EXCEEDED), receipt.explanation
    assert one_account.trade.submitted_legs == []


async def test_an_entry_with_no_bar_of_the_traded_asset_is_refused_and_sends_nothing(
    one_account: _OneAccount,  # noqa: F811 -- the imported fixture
) -> None:
    """SPY's bar is the only one in hand. It is not a price for QQQ, so nothing is bought."""
    receipt = await _b_decides_enter(one_account, quantity=1, pricing_bar=None)

    assert receipt.state is EffectOperationState.REJECTED
    assert receipt.explanation.startswith("PRICING_BAR_UNAVAILABLE"), receipt.explanation
    assert one_account.trade.submitted_legs == []
    assert one_account.enter_commands() == 0
    [(outcome, facts)] = one_account.receipts()
    assert (outcome, facts["reason_code"]) == ("blocked", "PRICING_BAR_UNAVAILABLE")


@pytest.mark.parametrize(
    "offered",
    [
        pytest.param(_retained("IWM", close="40"), id="another-asset"),
        pytest.param(_retained(TRADED, close="40", end_ms=NOON - MINUTE_MS), id="another-minute"),
    ],
)
async def test_a_bar_of_another_asset_or_another_minute_never_prices_the_order(
    one_account: _OneAccount,  # noqa: F811 -- the imported fixture
    offered: RetainedSourceBar,
) -> None:
    """The Clerk checks the bar it is handed; a caller's word that it prices the order is not enough."""
    receipt = await _b_decides_enter(one_account, quantity=1, pricing_bar=offered)

    assert receipt.state is EffectOperationState.REJECTED
    assert receipt.explanation.startswith("PRICING_BAR_MISMATCH"), receipt.explanation
    assert one_account.trade.submitted_legs == []


async def test_a_price_offered_with_no_decision_bar_admits_nothing(
    one_account: _OneAccount,  # noqa: F811 -- the imported fixture
) -> None:
    """With no decision bar nothing says which minute was decided, so no bar can be shown to be its price.

    The QQQ bar here closed half an hour before the decision. Offered alone,
    it must not turn the refusal a market ENTER gets with no bar at all into
    an admitted entry at a stale price.
    """
    stale = _retained(TRADED, close="40", end_ms=NOON - 30 * MINUTE_MS)
    receipt = await _b_decides_enter(one_account, quantity=10, pricing_bar=stale, with_decision_bar=False)

    assert receipt.state is EffectOperationState.REJECTED
    assert receipt.explanation.startswith(LIVE_ENVELOPE_UNOBSERVED), receipt.explanation
    assert one_account.trade.submitted_legs == []


# ── a broker-sent EXIT needs no price ───────────────────────────────────


async def test_a_broker_sent_exit_goes_out_with_no_bar_of_the_traded_asset(tmp_path: Path) -> None:
    """A sell is never held back for want of a price: in regular hours it is a market order."""
    decision_bar = _retained(SIGNAL, close="100", end_ms=et_minute_of_day_ms(DAY, 660), account_id="PA-TEST")
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST", artifacts_root=tmp_path, clock=lambda: decision_bar.end_ms + 1_000
    )
    trade = _EchoingTradePort()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=trade, account_mode="paper")
    binding = _binding(use_rth=True).model_copy(update={"action_plan": _TRADES_QQQ})
    await facade.register_strategy_run(binding)
    try:
        held = await submit_enter(
            repo, account_id="PA-TEST", strategy_instance_id=binding.strategy_instance_id, decision_id="enter-1",
            lifecycle_run_id=binding.run_id, leg=BrokerOrderLeg(symbol=TRADED, side="buy", quantity=10),
            trade=_FakeTrade(submit_result=_broker_order("placeholder", symbol=TRADED)),
        )
        assert held.order_ref is not None and held.effect_operation_id is not None
        fold_order_evidence(repo, effect_operation_id=held.effect_operation_id, order=_broker_order(
            held.order_ref, symbol=TRADED, status="filled", filled_quantity=10.0, filled_avg_price=40.0,
        ))

        receipt = await facade.execute_for_instance(
            strategy_instance_id=binding.strategy_instance_id, run_id=binding.run_id, decision_id="exit-1",
            purpose=EffectPurpose.EXIT, action_plan=_TRADES_QQQ, quantity=binding.quantity,
            retained_source_bar=decision_bar,
        )

        assert receipt.state is not EffectOperationState.REJECTED, receipt.explanation
        (leg,) = trade.submitted_legs
        assert (leg.symbol, leg.side, leg.order_type, leg.quantity) == (TRADED, OrderSide.SELL, OrderType.MARKET, 10.0)
    finally:
        repo.close()


async def test_a_bot_that_trades_another_asset_can_be_started_again(tmp_path: Path) -> None:
    """A Resume registers the same bot again, and it must still match its own registration.

    The Clerk registers a bot under the symbol it trades. Comparing the next
    registration against the symbol it listens to would refuse every Resume.
    """
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(), account_mode="paper")
    binding = _binding(use_rth=True).model_copy(update={"action_plan": _TRADES_QQQ})
    try:
        await facade.register_strategy_run(binding)
        await facade.register_strategy_run(binding)

        registered = repo.strategy_instance(binding.strategy_instance_id)
        assert registered is not None and registered["symbol"] == TRADED
    finally:
        repo.close()


# ── the fill a Live Shadow synthesizes ──────────────────────────────────

_SHADOW_ACCOUNT = "shadow:9LIVE0001"
_SHADOW_SID = "spy-bot"


def _fill_prices(repo: ClerkSqliteRepository, receipt: EffectOperationReceipt) -> list[float]:
    return [fill["price"] for order_ref in receipt.child_order_refs for fill in repo.fills_for_order(order_ref)]


async def test_a_live_shadow_fills_at_the_traded_assets_close_and_never_without_its_bar(tmp_path: Path) -> None:
    ports = compose_shadow_ports(live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path)
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id=f"shadow-evidence:{_SHADOW_SID}")
    enter_ms = et_minute_of_day_ms(DAY, 601)
    exit_ms = enter_ms + MINUTE_MS
    spy_at_enter = evidence.append(_minute_bar(SIGNAL, close="100.25", end_ms=enter_ms), run_id="run-1")
    qqq_at_enter = evidence.append(_minute_bar(TRADED, close="40.10", end_ms=enter_ms), run_id="run-1")
    spy_at_exit = evidence.append(_minute_bar(SIGNAL, close="100.50", end_ms=exit_ms), run_id="run-1")
    qqq_at_exit = evidence.append(_minute_bar(TRADED, close="40.60", end_ms=exit_ms), run_id="run-1")
    repo = ClerkSqliteRepository.initialize(
        account_id=_SHADOW_ACCOUNT, artifacts_root=tmp_path, clock=_Clock(exit_ms + 1_000)
    )
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=ports.read, trade=ports.trade, authority_kind="shadow", account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
    )
    binding = _binding(use_rth=True).model_copy(
        update={"sealed_account_id": _SHADOW_ACCOUNT, "action_plan": _TRADES_QQQ}
    )
    await facade.register_strategy_run(binding)

    async def decide(purpose: EffectPurpose, decision_id: str, **bars: Any) -> EffectOperationReceipt:
        return await facade.execute_for_instance(
            strategy_instance_id=_SHADOW_SID, run_id="run-1", decision_id=decision_id, purpose=purpose,
            action_plan=_TRADES_QQQ, quantity=binding.quantity, use_rth=True, **bars,
        )

    try:
        unpriced = await decide(EffectPurpose.ENTER, "enter-0", retained_source_bar=spy_at_enter)
        assert unpriced.state is EffectOperationState.REJECTED
        assert unpriced.explanation.startswith("PRICING_BAR_UNAVAILABLE"), unpriced.explanation
        assert await ports.read.list_orders() == []

        bought = await decide(
            EffectPurpose.ENTER, "enter-1", retained_source_bar=spy_at_enter, pricing_bar=qqq_at_enter
        )
        assert bought.state is not EffectOperationState.REJECTED, bought.explanation
        assert _fill_prices(repo, bought) == [40.10]
        assert [position.symbol for position in await ports.read.list_positions()] == [TRADED]

        # Interim (#2912): a synthesized sell with no QQQ bar is refused, so no
        # price is invented and nothing crashes. The PRD's rule is that it
        # fills at the newest retained QQQ price, carrying that price's age;
        # that lands in a later slice and replaces these four lines.
        held = await decide(EffectPurpose.EXIT, "exit-0", retained_source_bar=spy_at_exit)
        assert held.state is EffectOperationState.REJECTED
        assert held.explanation.startswith("PRICING_BAR_UNAVAILABLE"), held.explanation
        assert [position.symbol for position in await ports.read.list_positions()] == [TRADED]

        sold = await decide(EffectPurpose.EXIT, "exit-1", retained_source_bar=spy_at_exit, pricing_bar=qqq_at_exit)
        assert sold.state is EffectOperationState.FLAT, sold.explanation
        # An EXIT's receipt names the entry it closed, then its own sale.
        assert _fill_prices(repo, sold) == [40.10, 40.60]
        assert await ports.read.list_positions() == []
    finally:
        repo.close()
        evidence.close()


# ── the fill a Dry Run synthesizes ──────────────────────────────────────

_DRY_RUN_SID = "dry-run-trades-qqq"


async def test_a_dry_run_fills_at_the_traded_assets_close(tmp_path: Path) -> None:
    clock = _TestClock(NOON)
    binding = BrokerBotBinding(
        strategy_instance_id=_DRY_RUN_SID, strategy_key="ema_crossover_signal", broker="alpaca", symbol=SIGNAL,
        mode="dry_run", quantity=1, action_plan=_TRADES_QQQ, run_id="run-dry", created_at_ms=0,
        sealed_account_id=synthetic_account_id_for_strategy(_DRY_RUN_SID), exit_terms=TERMS,
        budget_consent=DeployBudgetConsent(
            committed_cents=100_000, risk_revision=0, actor="owner", request_fingerprint="reviewed", world="synthetic",
        ),
    )
    market_liveness.reset_market_liveness_store_for_testing()
    _publish_quote(NOON, bid=100.0, ask=100.0)
    authority = SyntheticBindingAuthority(
        binding=binding, artifacts_root=tmp_path,
        lifecycle_repo_for=lambda _sid: pytest.fail("a Dry Run's entry never reads the runner's lifecycle state"),
        runtime_in_use=lambda _sid: True, brokers={}, clock=clock,
    )
    try:
        await authority.ensure_recoverable()
        runtime = get_clerk_runtime(binding.sealed_account_id or "")
        assert runtime is not None and runtime.clerk is not None
        await runtime.clerk.register_strategy_run(binding)
        bars = authority.source_bars()
        try:
            spy = bars.append(_minute_bar(SIGNAL, close="100"), run_id=binding.run_id)
            qqq = bars.append(_minute_bar(TRADED, close="40.10"), run_id=binding.run_id)
        finally:
            bars.close()
        clock.value = NOON + 1_000

        bought = await runtime.clerk.execute_for_instance(
            strategy_instance_id=_DRY_RUN_SID, run_id=binding.run_id, decision_id="enter-1",
            purpose=EffectPurpose.ENTER, action_plan=_TRADES_QQQ, quantity=1,
            retained_source_bar=spy, pricing_bar=qqq,
        )

        assert bought.state is not EffectOperationState.REJECTED, bought.explanation
        assert _fill_prices(runtime.clerk.repository, bought) == [40.10]
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()
