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
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_CASH_EXCEEDED, LIVE_ENVELOPE_UNOBSERVED
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.program_leg import PRICING_BAR_MISMATCH, ProgramLegPolicy
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade, _order_pricing_bar
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide, OrderType
from app.marketdata.feed import MarketDataBar
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services import market_liveness
from app.services.bot_binding_authority import SyntheticBindingAuthority
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from tests._helpers.market_quotes import publish_quotes
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

        # With no QQQ bar offered at all there is still nothing to fill at.
        unoffered = await decide(EffectPurpose.EXIT, "exit-0", retained_source_bar=spy_at_exit)
        assert unoffered.state is EffectOperationState.REJECTED
        assert unoffered.explanation.startswith("PRICING_BAR_UNAVAILABLE"), unoffered.explanation
        assert [position.symbol for position in await ports.read.list_positions()] == [TRADED]

        sold = await decide(EffectPurpose.EXIT, "exit-1", retained_source_bar=spy_at_exit, pricing_bar=qqq_at_exit)
        assert sold.state is EffectOperationState.FLAT, sold.explanation
        # An EXIT's receipt names the entry it closed, then its own sale.
        assert _fill_prices(repo, sold) == [40.10, 40.60]
        assert await ports.read.list_positions() == []
    finally:
        repo.close()
        evidence.close()


async def test_a_live_shadow_sell_with_no_bar_of_its_minute_fills_at_the_newest_earlier_price(
    tmp_path: Path,
) -> None:
    """A sell is never held back for want of a price (#2912).

    QQQ has no bar for the minute SPY decided EXIT in. The sale fills at
    QQQ's newest earlier close, stamped when it was decided, and its record
    says so: an older bar's close, and that bar's own time.
    """
    ports = compose_shadow_ports(live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path)
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id=f"shadow-evidence:{_SHADOW_SID}")
    enter_ms = et_minute_of_day_ms(DAY, 601)
    exit_ms = enter_ms + 3 * MINUTE_MS
    spy_at_enter = evidence.append(_minute_bar(SIGNAL, close="100.25", end_ms=enter_ms), run_id="run-1")
    qqq_at_enter = evidence.append(_minute_bar(TRADED, close="40.10", end_ms=enter_ms), run_id="run-1")
    spy_at_exit = evidence.append(_minute_bar(SIGNAL, close="100.50", end_ms=exit_ms), run_id="run-1")
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
    try:
        await facade.execute_for_instance(
            strategy_instance_id=_SHADOW_SID, run_id="run-1", decision_id="enter-1", purpose=EffectPurpose.ENTER,
            action_plan=_TRADES_QQQ, quantity=binding.quantity, use_rth=True,
            retained_source_bar=spy_at_enter, pricing_bar=qqq_at_enter,
        )

        sold = await facade.execute_for_instance(
            strategy_instance_id=_SHADOW_SID, run_id="run-1", decision_id="exit-1", purpose=EffectPurpose.EXIT,
            action_plan=_TRADES_QQQ, quantity=binding.quantity, use_rth=True,
            retained_source_bar=spy_at_exit, pricing_bar=qqq_at_enter,
        )

        assert sold.state is EffectOperationState.FLAT, sold.explanation
        assert await ports.read.list_positions() == []
        sale = next(order for order in await ports.read.list_orders() if order.side == "sell")
        assert (sale.filled_avg_price, sale.filled_at_ms) == (40.10, exit_ms)
        assert sale.client_order_id is not None
        record = ports.book.record(sale.client_order_id)
        assert record is not None and record.anchor is not None
        assert (record.anchor.fill_model, record.anchor.decision_bar_end_ms) == ("older_bar_close", enter_ms)
    finally:
        repo.close()
        evidence.close()


async def _shadow_holding_qqq(tmp_path: Path, *, exit_ms: int, clock: _Clock) -> tuple[Any, ...]:
    """A Live Shadow that listens to SPY, bought QQQ at 10:01 for $40.10, and decides EXIT at ``exit_ms``."""
    ports = compose_shadow_ports(live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path)
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id=f"shadow-evidence:{_SHADOW_SID}")
    enter_ms = et_minute_of_day_ms(DAY, 601)
    spy_at_enter = evidence.append(_minute_bar(SIGNAL, close="100.25", end_ms=enter_ms), run_id="run-1")
    qqq_at_enter = evidence.append(_minute_bar(TRADED, close="40.10", end_ms=enter_ms), run_id="run-1")
    spy_at_exit = evidence.append(_minute_bar(SIGNAL, close="100.50", end_ms=exit_ms), run_id="run-1")
    repo = ClerkSqliteRepository.initialize(
        account_id=_SHADOW_ACCOUNT, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=24 * 3_600_000
    )
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=ports.read, trade=ports.trade, authority_kind="shadow", account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
    )
    binding = _binding(use_rth=True).model_copy(
        update={"sealed_account_id": _SHADOW_ACCOUNT, "action_plan": _TRADES_QQQ}
    )
    await facade.register_strategy_run(binding)
    bought = await facade.execute_for_instance(
        strategy_instance_id=_SHADOW_SID, run_id="run-1", decision_id="enter-1", purpose=EffectPurpose.ENTER,
        action_plan=_TRADES_QQQ, quantity=binding.quantity, use_rth=True,
        retained_source_bar=spy_at_enter, pricing_bar=qqq_at_enter,
    )
    assert bought.state is not EffectOperationState.REJECTED, bought.explanation
    return ports, evidence, repo, facade, binding, spy_at_exit, qqq_at_enter


async def test_an_older_price_never_buys_on_an_account_that_fills_its_own_orders(tmp_path: Path) -> None:
    """Only a sell may be priced from an earlier minute. A Live Shadow's entry offered one is refused."""
    ports = compose_shadow_ports(live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path)
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id=f"shadow-evidence:{_SHADOW_SID}")
    decided_ms = et_minute_of_day_ms(DAY, 604)
    qqq_earlier = evidence.append(_minute_bar(TRADED, close="40.10", end_ms=decided_ms - 3 * MINUTE_MS), run_id="run-1")
    spy_decides = evidence.append(_minute_bar(SIGNAL, close="100.25", end_ms=decided_ms), run_id="run-1")
    repo = ClerkSqliteRepository.initialize(
        account_id=_SHADOW_ACCOUNT, artifacts_root=tmp_path, clock=_Clock(decided_ms + 1_000)
    )
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=ports.read, trade=ports.trade, authority_kind="shadow", account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
    )
    binding = _binding(use_rth=True).model_copy(
        update={"sealed_account_id": _SHADOW_ACCOUNT, "action_plan": _TRADES_QQQ}
    )
    await facade.register_strategy_run(binding)
    try:
        refused = await facade.execute_for_instance(
            strategy_instance_id=_SHADOW_SID, run_id="run-1", decision_id="enter-1", purpose=EffectPurpose.ENTER,
            action_plan=_TRADES_QQQ, quantity=binding.quantity, use_rth=True,
            retained_source_bar=spy_decides, pricing_bar=qqq_earlier,
        )

        assert refused.state is EffectOperationState.REJECTED
        assert refused.explanation.startswith("PRICING_BAR_MISMATCH"), refused.explanation
        assert await ports.read.list_orders() == []
    finally:
        repo.close()
        evidence.close()


@pytest.mark.parametrize(
    ("exit_sells", "priced"), [(True, True), (False, False)], ids=["closes-a-long", "covers-a-short"],
)
def test_an_older_price_fills_only_an_exit_that_sells(exit_sells: bool, priced: bool) -> None:
    """A short position's EXIT buys to cover, and an older price never buys."""
    decision = _retained(SIGNAL, close="100", end_ms=NOON, account_id="shadow-evidence:any")
    older = _retained(TRADED, close="40", end_ms=NOON - 3 * MINUTE_MS, account_id="shadow-evidence:any")

    resolved = _order_pricing_bar(
        TRADED, purpose=EffectPurpose.EXIT, synthesized=True, exit_sells=exit_sells,
        decision_bar=decision, pricing_bar=older,
    )

    assert (resolved is older) is priced
    assert priced or resolved is PRICING_BAR_MISMATCH


async def test_a_sale_that_reaches_the_clerk_after_the_close_is_not_filled_at_an_older_price(tmp_path: Path) -> None:
    """An older price fills a market order only, and after the close none can go out.

    The sale would be re-priced as a limit to rest until the next session, and
    no limit is anchored to an older price. It is refused before anything is
    accepted, where it used to raise after the EXIT was already on the books.
    """
    exit_ms = et_minute_of_day_ms(DAY, 958)
    clock = _Clock(et_minute_of_day_ms(DAY, 601) + 1_000)
    ports, evidence, repo, facade, binding, spy_at_exit, qqq_at_enter = await _shadow_holding_qqq(
        tmp_path, exit_ms=exit_ms, clock=clock
    )
    try:
        clock.now_ms = et_minute_of_day_ms(DAY, 960) + 30_000  # 16:00:30 ET
        publish_quotes(clock.now_ms, QQQ=40.0)  # a resting limit could be priced, so only the refusal stops it

        late = await facade.execute_for_instance(
            strategy_instance_id=_SHADOW_SID, run_id="run-1", decision_id="exit-1", purpose=EffectPurpose.EXIT,
            action_plan=_TRADES_QQQ, quantity=binding.quantity, use_rth=True,
            retained_source_bar=spy_at_exit, pricing_bar=qqq_at_enter,
        )

        assert late.state is EffectOperationState.REJECTED
        assert late.explanation.startswith("PRICING_BAR_UNAVAILABLE"), late.explanation
        assert "can no longer take a market order" in late.explanation, "it says why, not only that no bar was found"
        assert [order.side for order in await ports.read.list_orders()] == ["buy"], "no sale was synthesized"
        assert [position.symbol for position in await ports.read.list_positions()] == [TRADED]
        assert await facade.unresolved_effect_count(subject_id=f"bot:{_SHADOW_SID}") == 0
    finally:
        repo.close()
        evidence.close()
        market_liveness.reset_market_liveness_store_for_testing()


async def test_a_sale_accepted_before_the_close_and_sent_after_it_is_refused_at_the_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The close can fall between accepting the sale and sending it.

    The sale was accepted while a market order could still go out, and by the
    send it is re-priced as a limit to rest. The send is refused the way a
    broker would refuse it, so the EXIT fails cleanly: nothing is synthesized,
    the shares stay, and nothing raises with the EXIT already on the books.
    """
    exit_ms = et_minute_of_day_ms(DAY, 958)
    clock = _Clock(et_minute_of_day_ms(DAY, 601) + 1_000)
    ports, evidence, repo, facade, binding, spy_at_exit, qqq_at_enter = await _shadow_holding_qqq(
        tmp_path, exit_ms=exit_ms, clock=clock
    )
    try:
        clock.now_ms = et_minute_of_day_ms(DAY, 960) + 30_000  # 16:00:30 ET
        publish_quotes(clock.now_ms, QQQ=40.0)
        # The acceptance check alone still reads the session as open.
        monkeypatch.setattr("app.broker.alpaca.clerk.sqlite.runtime.market_leg_sendable", lambda _now_ms: True)

        late = await facade.execute_for_instance(
            strategy_instance_id=_SHADOW_SID, run_id="run-1", decision_id="exit-1", purpose=EffectPurpose.EXIT,
            action_plan=_TRADES_QQQ, quantity=binding.quantity, use_rth=True,
            retained_source_bar=spy_at_exit, pricing_bar=qqq_at_enter,
        )

        assert late.state is EffectOperationState.REJECTED, late.explanation
        assert late.explanation == "The SQLite Account Clerk recorded EXIT as failed."
        still_open = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT", reason_code="EXIT_NOT_FLAT", strategy_instance_id=_SHADOW_SID
        )
        assert still_open is not None, "the failed sale is on the books as a position still open"
        sale_ref = late.child_order_refs[-1]
        assert any(
            "priced this sale from an earlier minute" in transition["facts_json"]
            for transition in repo.transitions_for_order(sale_ref)
        ), "the order's evidence says why the simulation refused it"
        assert [order.side for order in await ports.read.list_orders()] == ["buy"], "no sale was synthesized"
        assert [position.symbol for position in await ports.read.list_positions()] == [TRADED]
    finally:
        repo.close()
        evidence.close()
        market_liveness.reset_market_liveness_store_for_testing()


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
    publish_quotes(NOON, SPY=100.0, QQQ=40.0)
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


async def test_a_deploy_is_committed_at_the_price_of_the_asset_the_bot_buys(tmp_path: Path) -> None:
    """One estimated position is one QQQ. SPY's price commits nothing for it: the Deploy waits for QQQ's."""
    clock = _TestClock(NOON)
    sid = "dry-commit-qqq"
    binding = BrokerBotBinding(
        strategy_instance_id=sid, strategy_key="ema_crossover_signal", broker="alpaca", symbol=SIGNAL,
        mode="dry_run", quantity=1, action_plan=_TRADES_QQQ, run_id="run-dry", created_at_ms=0,
        sealed_account_id=synthetic_account_id_for_strategy(sid), exit_terms=TERMS,
        budget_consent=DeployBudgetConsent(
            committed_cents=5_000, risk_revision=0, actor="owner", request_fingerprint="reviewed", world="synthetic",
        ),
    )
    market_liveness.reset_market_liveness_store_for_testing()
    authority = SyntheticBindingAuthority(
        binding=binding, artifacts_root=tmp_path,
        lifecycle_repo_for=lambda _sid: pytest.fail("a Dry Run's Deploy never reads the runner's lifecycle state"),
        runtime_in_use=lambda _sid: True, brokers={}, clock=clock,
    )
    try:
        await authority.ensure_recoverable()
        runtime = get_clerk_runtime(binding.sealed_account_id or "")
        assert runtime is not None and runtime.clerk is not None

        publish_quotes(NOON, SPY=30.0)
        with pytest.raises(BudgetUnavailable, match="fresh IBKR price"):
            await runtime.clerk.register_strategy_run(binding)

        # $50 covers one QQQ at $40. It would not cover one at SPY's $500.
        publish_quotes(NOON, SPY=500.0, QQQ=40.0)
        await runtime.clerk.register_strategy_run(binding)
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()
