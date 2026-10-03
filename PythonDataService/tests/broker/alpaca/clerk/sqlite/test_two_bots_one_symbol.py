"""Two bots trading one symbol in one Alpaca account (research #2469).

What the Clerk does today when two strategy instances hold and trade the same
symbol in one account.

Passing tests pin current behaviour. Every fixture comes from the existing
Clerk suites (``conftest``, ``test_budget_commands``,
``test_envelope_reservations``, ``test_exit``, ``test_exit_send_session``,
``test_reconcile``); the only new object is Alpaca's wash-trade rejection,
built the way alpaca-py raises it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alpaca.common.exceptions import APIError

from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.live_envelope import (
    FILL_VISIBILITY_GRACE_MS,
    LIVE_ENVELOPE_CASH_EXCEEDED,
    LIVE_ENVELOPE_UNOBSERVED,
    LiveEnvelopeGate,
)
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.program_leg import LegShape, ProgramLeg
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import (
    EnterSubmission,
    EntrySubmissionRefusal,
    accept_enter,
    submit_accepted_enter,
    submit_enter,
)
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_recovery import DEFAULT_RECOVERY_INTERVAL_MS
from app.broker.alpaca.clerk.sqlite.facts import OrderSubmitFailedFacts
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import DEFAULT_ENTRY_READING_INTERVAL_S, LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import submit_manual_order_cancellation
from app.broker.alpaca.clerk.sqlite.manual_orders import submit_manual_order
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.order_projection import OrderProjectionReadError
from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryStatus
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import plan_account_reconciliation, reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    Capability,
    ReductionIntent,
    decide_capability,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
    EXIT_STUCK_REASON_CODE,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.errors import AlpacaRequest, map_api_error
from app.broker.alpaca.marketable_limit import marketable_limit_price
from app.broker.contract.errors import BrokerError, BrokerOrderNotPermitted, BrokerUnavailable
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    OrderSide,
    OrderType,
    TimeInForce,
)
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.marketdata.feed import DELIVERY_ALLOWANCE_MS, MarketDataBar
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services import market_liveness
from app.services.bot_binding_authority import SyntheticBindingAuthority
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.source_bar_ledger import RetainedSourceBar
from tests.broker.alpaca.clerk.sqlite.conftest import (
    NOON,
    _FakeTradePort,
    _fill_entry,
    _make_held_position,
    _TestClock,
    _walk_clock_to,
    complete_fee_evidence,
)
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS, _deploy, _gate, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice
from tests.broker.alpaca.clerk.sqlite.test_exit import POST_CLOSE_MS
from tests.broker.alpaca.clerk.sqlite.test_exit_send_session import _live_touch
from tests.broker.alpaca.clerk.sqlite.test_live_envelope_sync import _cash_flow, _Read
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import LEG_ID, OPERATOR_ID, TICKET_ID
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import FakeTrade as _ManualTrade
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (
    ACCOUNT_ID,
    WATCHDOG_POST_T0,
    WATCHDOG_RUN,
    WATCHDOG_SID,
    _broker_order,
    _exit_not_flat_redrive_policy,
    _FakeRead,
    _FakeTrade,
    _held_position,
    _leg,
    _position,
    _register_second_spy_lane,
    clocked_repo,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.v2panel.test_dry_run_recovery import _publish_quote

# Alpaca's documented wash-trade refusal: HTTP 403 on POST /v2/orders
# (https://docs.alpaca.markets/us/docs/user-protection). The page states only
# the status. The message is the one quoted in the title of an Alpaca community
# forum thread; Alpaca documents no numeric code for this refusal, so the code
# below is illustrative. The Clerk maps on the status alone, keeps the code, and
# names another open order only from its own records (#2621).
_ILLUSTRATIVE_CODE = 40310000
_WASH_TRADE_MESSAGE = "potential wash trade detected. use complex orders"
_OPPOSITE_ORDER_HOLD = (
    "Alpaca refused this exit while an opposite order on this symbol was open in this account; "
    "the Clerk sends it again once no such order is open."
)
"""What A's page says while the order its refusal named, or another on that side, is still open (#2622)."""


def _wash_trade_rejection(code: int | None = _ILLUSTRATIVE_CODE) -> BrokerError:
    """The error the Clerk's broker port raises for the rejection (``client.submit_order``).

    Built the way alpaca-py raises it, with ``code`` (or no code) in the body.
    """
    body = {"message": _WASH_TRADE_MESSAGE} if code is None else {"code": code, "message": _WASH_TRADE_MESSAGE}
    response = SimpleNamespace(status_code=403, headers={})
    raw = APIError(json.dumps(body), http_error=SimpleNamespace(response=response, request=None))
    return map_api_error(raw, broker="alpaca", request=AlpacaRequest.ORDER_SUBMIT)


# ── Attribution: whose shares are whose ───────────────────────────────────────


async def test_same_symbol_positions_stay_per_bot_while_reconciliation_sees_one_netted_position(
    clocked_repo,  # noqa: F811 -- the imported fixture
) -> None:
    """Each bot owns its own fills; the broker reports one net SPY position.

    Reconciliation can only compare the broker's net quantity with the sum of
    the bots' quantities. When they differ it knows the symbol drifted, never
    which bot's shares went missing.
    """
    repo, _clock = clocked_repo
    sid_b, run_b = _register_second_spy_lane(repo)
    await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                              run_id=WATCHDOG_RUN, decision_id="a-buy", execution_id="a-exec", quantity=3)
    await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b,
                              run_id=run_b, decision_id="b-buy", execution_id="b-exec", quantity=5)

    assert repo.position(WATCHDOG_SID, "SPY") == 3
    assert repo.position(sid_b, "SPY") == 5
    assert repo.attributed_positions_by_subject() == {
        (f"bot:{WATCHDOG_SID}", "SPY"): 3, (f"bot:{sid_b}", "SPY"): 5,
    }

    def verdict(broker_quantity: float) -> tuple[str, tuple[str, ...]]:
        plan = plan_account_reconciliation(
            namespaces=frozenset(), broker_orders=[],
            broker_positions=[_position("SPY", quantity=broker_quantity)],
            attributed_positions=repo.attributed_positions_by_symbol(),
            known_order_refs=repo.all_order_refs(),
        )
        return plan.verdict, plan.drifted_symbols

    assert verdict(8) == ("clean", ())
    # Three shares sold outside the Clerk: the account drifts; no bot is named.
    assert verdict(5) == ("position_drift", ("SPY",))


async def test_each_bots_exit_sells_only_its_own_attributed_shares(
    clocked_repo,  # noqa: F811
) -> None:
    """Bot A's EXIT sells A's 3 shares out of the broker's 8, never B's 5."""
    repo, _clock = clocked_repo
    sid_b, run_b = _register_second_spy_lane(repo)
    ref_a = await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                                      run_id=WATCHDOG_RUN, decision_id="a-buy", execution_id="a-exec", quantity=3)
    await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b,
                              run_id=run_b, decision_id="b-buy", execution_id="b-exec", quantity=5)
    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                           decision_id="a-sell", lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=ref_a)
    trade = _FakeTradePort()

    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade,
                       pricing=UNPRICEABLE_RECOVERY, read=None)

    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [(OrderSide.SELL, 3)]


async def test_shares_sold_outside_the_clerk_strand_whichever_bot_exits_second(
    clocked_repo,  # noqa: F811
) -> None:
    """The broker holds 5 SPY; A holds 3 and B holds 5 by attribution.

    Under ``POSITION_DRIFT`` a reduction is allowed only if it moves both the
    broker quantity and the attributed sum toward zero without crossing.
    Either bot may go first. After A's 3 shares sell, the broker holds 2 and
    B's 5-share EXIT would cross zero, so B is refused, although nobody knows
    whose shares the outside sale took.
    """
    repo, _clock = clocked_repo
    sid_b, run_b = _register_second_spy_lane(repo)
    ref_a = await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                                      run_id=WATCHDOG_RUN, decision_id="a-buy", execution_id="a-exec", quantity=3)
    await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b,
                              run_id=run_b, decision_id="b-buy", execution_id="b-exec", quantity=5)

    def may_sell(sid: str, quantity: float) -> bool:
        return decide_capability(repo, capability=Capability.REDUCE, strategy_instance_id=sid,
                                 reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=quantity)).allowed

    await reconcile_account(repo, read=_FakeRead(positions=[_position("SPY", quantity=5.0)]),
                            trade=_FakeTrade(), pricing=UNPRICEABLE_RECOVERY)
    assert may_sell(WATCHDOG_SID, 3) and may_sell(sid_b, 5)

    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                           decision_id="a-sell", lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=ref_a)
    sent = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=_FakeTrade(),
                              pricing=UNPRICEABLE_RECOVERY, read=None)
    assert sent.reducing_order_ref is not None
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=_broker_order(
        sent.reducing_order_ref, order_id="bo-a-sell", side="sell", status="filled", quantity=3.0,
        filled_quantity=3.0, filled_avg_price=100.0))
    assert repo.position(WATCHDOG_SID, "SPY") == 0
    await reconcile_account(repo, read=_FakeRead(positions=[_position("SPY", quantity=2.0)]),
                            trade=_FakeTrade(), pricing=UNPRICEABLE_RECOVERY)

    assert not may_sell(sid_b, 5)
    assert may_sell(sid_b, 2)


@pytest.mark.parametrize("anchor", ["0.5731", "99.99", "452.18"])
@pytest.mark.parametrize(("entry_bps", "exit_bps"), [("0", "0"), ("5", "5"), ("20", "50")])
def test_opposite_extended_hours_legs_on_one_decision_bar_are_always_a_wash_trade_pair(
    anchor: str, entry_bps: str, exit_bps: str,
) -> None:
    """Alpaca rejects a limit buy against an open limit sell when buy limit >= sell limit.

    Outside the regular session every program leg is a marketable limit on the
    decision bar's close: a buy rounds up from it, a sell rounds down. Two bots
    that decide opposite sides on the same bar therefore always meet that
    condition, so whichever order reaches Alpaca second is refused.
    """
    close = Decimal(anchor)
    buy = marketable_limit_price(side=OrderSide.BUY, anchor=close, allowance_bps=Decimal(entry_bps))
    sell = marketable_limit_price(side=OrderSide.SELL, anchor=close, allowance_bps=Decimal(exit_bps))

    assert buy >= close >= sell


def _fill(sid: str, side: OrderSide, price: float, at_ms: int) -> FillRecord:
    return FillRecord(account_id="PA-TEST", sid=sid, intent_id=f"i{at_ms}",
                      order_ref=f"learn-ai/{sid}/v1:i{at_ms}", event_key=f"e{at_ms}", symbol="SPY",
                      side=side, quantity=1, fill_price=price, filled_at_ms=at_ms, fee=0)


def test_account_fifo_lets_one_bots_sale_close_the_other_bots_lot() -> None:
    """Bot results use per-bot FIFO; the account view runs one FIFO over both bots.

    A buys 1 at 100, B buys 1 at 200, B sells 1 at 210. Per bot, B realizes
    +10 and A still holds its 100 lot. Across the account, B's sale closes A's
    older lot for +110 and B's 200 lot stays open. Realized P&L differs by
    100; realized plus open P&L at any mark is the same. Exact Decimal money:
    no tolerance.
    """
    fills = [_fill("a", OrderSide.BUY, 100, 1), _fill("b", OrderSide.BUY, 200, 2), _fill("b", OrderSide.SELL, 210, 3)]
    mark = {"SPY": 205.0}
    per_bot = [compute_fifo_pnl([f for f in fills if f.sid == sid], mark_prices=mark) for sid in ("a", "b")]
    account = compute_fifo_pnl(fills, mark_prices=mark)

    assert [bot.exact_realized_pnl for bot in per_bot] == [Decimal(0), Decimal(10)]
    assert account.exact_realized_pnl == Decimal(110)
    [crossed] = account.closed_lots
    assert (crossed.entry_strategy_instance_id, crossed.exit_strategy_instance_id) == ("a", "b")
    per_bot_total = sum(bot.exact_realized_pnl for bot in per_bot) + Decimal(str(sum(bot.open_pnl for bot in per_bot)))
    assert per_bot_total == account.exact_realized_pnl + Decimal(str(account.open_pnl)) == Decimal(115)


# ── Alpaca's wash-trade refusal ───────────────────────────────────────────────


def test_a_wash_trade_refusal_is_an_order_rejection_that_keeps_alpacas_code() -> None:
    """A 403 on an order submission is Alpaca refusing that order, not our credentials (#2621)."""
    error = _wash_trade_rejection()

    assert isinstance(error, BrokerOrderNotPermitted)
    assert error.message == f"Alpaca refused the order: {_WASH_TRADE_MESSAGE}"
    assert error.detail == "HTTP 403"
    assert error.code == _ILLUSTRATIVE_CODE


async def test_an_enter_refused_as_a_wash_trade_fails_and_the_bot_may_enter_again(tmp_path: Path) -> None:
    """The refusal is definitive: the ENTER folds failed, nothing is left uncertain.

    Its record keeps Alpaca's code. No other order is open on SPY, so the
    record repeats only Alpaca's own words and never names another order.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "b", 50_000)
        leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=2)
        accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="b-1",
                                lifecycle_run_id="run-b", leg=leg, reference_price=100, envelope=_gate())

        result = await submit_accepted_enter(repo, accepted=accepted, leg=leg,
                                             trade=_FakeTradePort(submit_error=_wash_trade_rejection()))

        effect = repo.effect_operation(result.effect_operation_id)
        assert effect is not None and effect.state == "failed"
        assert repo.uncertain_orders() == []
        [failed] = [row for row in repo.transitions_for_order(accepted.order_ref)
                    if row["transition_kind"] == "ORDER_SUBMIT_FAILED"]
        facts = OrderSubmitFailedFacts.from_facts_json(failed["facts_json"])
        assert facts.broker_error_code == _ILLUSTRATIVE_CODE
        assert facts.why == f"Alpaca refused the order: {_WASH_TRADE_MESSAGE}"
        assert facts.opposite_open_order_refs == []
        again = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="b-2",
                             lifecycle_run_id="run-b", leg=leg, reference_price=100, envelope=_gate())
        assert again.created
    finally:
        repo.close()


def _register_spy_lane(repo: ClerkSqliteRepository, sid: str) -> str:
    run_id = f"{sid}-run"
    repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash=f"{sid}-h")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, lifecycle_run_id=run_id)
    return run_id


def test_a_refusals_facts_omit_an_absent_code_so_every_earlier_row_hashes_the_same() -> None:
    """``broker_error_code`` joins the hash-chained facts only when Alpaca gave one.

    Every row written before #2621 -- and every failure no broker answer
    caused -- re-serializes byte-identically, so no sealed receipt moves.
    """
    plain = OrderSubmitFailedFacts(reason="The order did not reach the broker.", why="Alpaca refused the order: x")
    earlier_row = '{"reason":"The order did not reach the broker.","why":"Alpaca refused the order: x"}'
    coded = OrderSubmitFailedFacts(
        reason="The order did not reach the broker.", why="Alpaca refused the order: x", broker_error_code=40310000,
    )

    assert plain.to_facts_json() == earlier_row
    assert OrderSubmitFailedFacts.from_facts_json(earlier_row) == plain
    assert coded.to_facts_json() == (
        '{"broker_error_code":40310000,"reason":"The order did not reach the broker.",'
        '"why":"Alpaca refused the order: x"}'
    )
    assert OrderSubmitFailedFacts.from_facts_json(coded.to_facts_json()) == coded
    looked = OrderSubmitFailedFacts(
        reason="The order did not reach the broker.", why="Alpaca refused the order: x", opposite_open_order_refs=[],
    )
    assert looked.to_facts_json() == (
        '{"opposite_open_order_refs":[],"reason":"The order did not reach the broker.",'
        '"why":"Alpaca refused the order: x"}'
    )
    assert OrderSubmitFailedFacts.from_facts_json(looked.to_facts_json()) == looked


async def _a_sells(repo: ClerkSqliteRepository, *, fills: bool) -> str:
    """A holds 10 SPY and sends its 10-share sell, which Alpaca accepts and then fills or leaves working.

    Returns the sell's order reference.
    """
    ref_a = await _held_position(repo)
    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                           decision_id="a-sell", lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=ref_a)
    sent = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=_FakeTrade(),
                              pricing=UNPRICEABLE_RECOVERY, read=None)
    assert sent.reducing_order_ref is not None
    if fills:
        fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=_broker_order(
            sent.reducing_order_ref, order_id="bo-a-sell", side="sell", status="filled", quantity=10.0,
            filled_quantity=10.0, filled_avg_price=100.0))
    return sent.reducing_order_ref


async def _refused_enter_facts(
    repo: ClerkSqliteRepository, sid: str, run_id: str, refusal: BrokerError,
) -> OrderSubmitFailedFacts:
    """``sid`` sends a 5-share SPY buy that Alpaca refuses with ``refusal``; its durable record."""
    result = await submit_enter(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, decision_id=f"{sid}-buy",
                                lifecycle_run_id=run_id, leg=_leg(quantity=5),
                                trade=_FakeTradePort(submit_error=refusal))
    assert result.effect_operation_id is not None and result.order_ref is not None
    effect = repo.effect_operation(result.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    [failed] = [row for row in repo.transitions_for_order(result.order_ref)
                if row["transition_kind"] == "ORDER_SUBMIT_FAILED"]
    return OrderSubmitFailedFacts.from_facts_json(failed["facts_json"])


async def test_an_enter_refused_while_another_bots_sell_is_open_names_that_order(
    clocked_repo,  # noqa: F811
) -> None:
    """The Clerk's own records show A's sell working when B's buy is refused, so the record says so."""
    repo, _clock = clocked_repo
    a_sell = await _a_sells(repo, fills=False)
    run_b = _register_spy_lane(repo, "b")

    facts = await _refused_enter_facts(repo, "b", run_b, _wash_trade_rejection())

    assert facts.broker_error_code == _ILLUSTRATIVE_CODE
    assert facts.opposite_open_order_refs == [a_sell]
    assert facts.why == (
        "This account had an open or pending SPY sell order when Alpaca refused this buy; "
        "Alpaca refuses an order that could trade against another open order in the same account. "
        f"Alpaca refused the order: {_WASH_TRADE_MESSAGE}"
    )


def _alpaca_answer(status: int, code: int, message: str) -> BrokerError:
    """Alpaca's answer with ``status`` to an order submission, as the broker port raises it."""
    raw = APIError(
        json.dumps({"code": code, "message": message}),
        http_error=SimpleNamespace(response=SimpleNamespace(status_code=status, headers={}), request=None),
    )
    return map_api_error(raw, broker="alpaca", request=AlpacaRequest.ORDER_SUBMIT)


async def test_an_order_conflict_never_names_another_order_even_with_one_open(
    clocked_repo,  # noqa: F811
) -> None:
    """#2621: only a 403 on a submission is read for an open opposite order.

    A's sell is working when Alpaca answers B's buy with a 409 conflict. That
    is a duplicate-id or order-state conflict, never its wash-trade
    protection, so the record keeps Alpaca's words and code and records no
    look at the other orders.
    """
    repo, _clock = clocked_repo
    await _a_sells(repo, fills=False)
    run_b = _register_spy_lane(repo, "b")

    facts = await _refused_enter_facts(
        repo, "b", run_b, _alpaca_answer(409, 40910000, "client_order_id must be unique"),
    )

    assert facts.why == "Alpaca rejected the order as a conflict: client_order_id must be unique"
    assert facts.broker_error_code == 40910000
    assert facts.opposite_open_order_refs is None


@pytest.mark.parametrize(("b_buy", "counted"), [("refused", False), ("outcome_unknown", True)])
async def test_an_opposite_order_is_open_unless_it_provably_never_reached_alpaca(
    clocked_repo,  # noqa: F811
    b_buy: str,
    counted: bool,
) -> None:
    """B's buy Alpaca refused (failed, no broker id, no fill) is not open; one whose outcome is unknown is.

    #2622 re-sends A's refused exit once the other order ends, so B's order
    that never reached the book must never hold it back, while one that may
    be working at the broker must.
    """
    repo, _clock = clocked_repo
    run_b = _register_spy_lane(repo, "b")
    lost = BrokerUnavailable("response lost", broker="alpaca")
    refused = _alpaca_answer(422, 42210000, "qty must be > 0")
    sent = await submit_enter(repo, account_id=ACCOUNT_ID, strategy_instance_id="b", decision_id="b-buy",
                              lifecycle_run_id=run_b, leg=_leg(quantity=5),
                              trade=_FakeTrade(submit_error=refused if b_buy == "refused" else lost, lookup_error=lost))
    assert sent.effect_operation_id is not None and sent.order_ref is not None
    b_order, b_effect = repo.order(sent.order_ref), repo.effect_operation(sent.effect_operation_id)
    assert b_order is not None and b_order.broker_order_id is None and b_order.broker_state is None
    assert b_effect is not None and b_effect.state == ("failed" if b_buy == "refused" else "unknown")

    opposite = repo.open_opposite_side_orders(symbol="SPY", side=OrderSide.SELL)

    assert [order.order_ref for order in opposite] == ([b_order.order_ref] if counted else [])


@pytest.mark.parametrize("code", [_ILLUSTRATIVE_CODE, 40310001, None])
@pytest.mark.parametrize("other_order", ["none", "same_side_working", "opposite_side_filled"])
async def test_a_refusal_with_no_opposite_order_open_never_names_another_order(
    clocked_repo,  # noqa: F811
    code: int | None,
    other_order: str,
) -> None:
    """#2621: a 403 is described by the Clerk's evidence, never by Alpaca's code.

    Whatever the code, and even with Alpaca's wash-trade words, a refused buy
    names another order only while one on the other side is open: C's buy
    working on the same side, or A's sell that already filled, is not one.
    """
    repo, _clock = clocked_repo
    if other_order == "same_side_working":
        run_c = _register_spy_lane(repo, "c")
        await submit_enter(repo, account_id=ACCOUNT_ID, strategy_instance_id="c", decision_id="c-buy",
                           lifecycle_run_id=run_c, leg=_leg(quantity=3), trade=_FakeTrade())
    elif other_order == "opposite_side_filled":
        await _a_sells(repo, fills=True)
    run_b = _register_spy_lane(repo, "b")

    facts = await _refused_enter_facts(repo, "b", run_b, _wash_trade_rejection(code))

    assert facts.why == f"Alpaca refused the order: {_WASH_TRADE_MESSAGE}"
    assert facts.broker_error_code == code
    assert facts.opposite_open_order_refs == []


async def test_a_refusal_whose_order_records_cannot_be_read_still_folds_definitively(
    clocked_repo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The refusal is definitive whatever the evidence read finds: an unreadable record only drops the naming."""
    repo, _clock = clocked_repo
    run_b = _register_spy_lane(repo, "b")

    def unreadable(*, symbol: str, side: OrderSide) -> tuple:
        raise OrderProjectionReadError("SQLite order 'x' has malformed ENTER_ACCEPTED facts")

    monkeypatch.setattr(repo, "open_opposite_side_orders", unreadable)

    with caplog.at_level(logging.ERROR, logger="app.broker.alpaca.clerk.sqlite.order_evidence"):
        facts = await _refused_enter_facts(repo, "b", run_b, _wash_trade_rejection())

    assert facts.why == f"Alpaca refused the order: {_WASH_TRADE_MESSAGE}"
    assert facts.broker_error_code == _ILLUSTRATIVE_CODE
    assert facts.opposite_open_order_refs is None
    [logged] = [record for record in caplog.records if getattr(record, "action", None) == "refusal_evidence_unreadable"]
    assert logged.exc_info is not None


@pytest.mark.parametrize("refusal", ["broker_wash_trade", "preflight"])
async def test_an_enter_that_never_reached_the_book_releases_its_cash_claim(
    tmp_path: Path, refusal: str,
) -> None:
    """An ENTER no order came of claims no cash, and a stopped bot holding nothing is Finished."""
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "b", 50_000)
        leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=2)
        accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="b-1",
                                lifecycle_run_id="run-b", leg=leg, reference_price=100, envelope=_gate())
        if refusal == "broker_wash_trade":
            await submit_accepted_enter(repo, accepted=accepted, leg=leg,
                                        trade=_FakeTradePort(submit_error=_wash_trade_rejection()))
        else:
            await submit_accepted_enter(repo, accepted=accepted, leg=leg, trade=_FakeTradePort(),
                                        before_submit=lambda: EntrySubmissionRefusal(
                                            summary_code="MARKET_CLOSED",
                                            why="The market closed before the order was sent.",
                                        ))
        submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="b",
                        lifecycle_run_id="run-b", clock=repo.clock)

        budget = repo.account_budget(cash=1000, seen_before_ms=NOON + 1)

        assert budget.order_claims == 0
        assert budget.deployments[0].pending_orders == 0
        assert "b" not in repo.bots_holding_money()
    finally:
        repo.close()


def _exit_stuck(repo: ClerkSqliteRepository) -> dict | None:
    return repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=EXIT_STUCK_REASON_CODE,
                                   strategy_instance_id=WATCHDOG_SID)


def _shown_recovery(repo: ClerkSqliteRepository) -> RecoveryStatus:
    """What A's bot page shows about its open exit notice: the Clerk's next automatic try."""
    reader = SqliteClerkProjectionReader.from_repository(repo)
    try:
        snapshot = reader.bot_snapshot(WATCHDOG_SID)
    finally:
        reader.close()
    assert snapshot is not None
    [notice] = [item for item in snapshot.uncertainties if item.reason_code == EXIT_NOT_FLAT_REASON_CODE]
    assert notice.recovery_status is not None
    return notice.recovery_status


async def _a_sells_into_bs_working_buy(
    repo: ClerkSqliteRepository, *, program_leg: ProgramLeg | None = None,
) -> EnterSubmission:
    """A holds 10 SPY; B's 5-share buy is working; Alpaca refuses A's sell as a potential wash trade."""
    ref_a = await _held_position(repo)
    sid_b, run_b = _register_second_spy_lane(repo)
    working_b = await submit_enter(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b, decision_id="b-buy",
                                   lifecycle_run_id=run_b, leg=_leg(quantity=5), trade=_FakeTrade())
    await _as_sell_is_refused(repo, ref_a, program_leg=program_leg)
    return working_b


async def _as_sell_is_refused(
    repo: ClerkSqliteRepository, entry_ref: str, *,
    program_leg: ProgramLeg | None = None, refusal: BrokerError | None = None,
) -> OrderSubmitFailedFacts:
    """Alpaca refuses A's 10-share sell (as a potential wash trade unless ``refusal`` says otherwise).

    A keeps its 10 SPY under an ``EXIT_NOT_FLAT`` episode. Returns the refusal's durable record.
    """
    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                           decision_id="a-sell", lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=entry_ref,
                           program_leg=program_leg)

    refusing = _FakeTradePort(submit_error=_wash_trade_rejection() if refusal is None else refusal)
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=refusing,
                       pricing=UNPRICEABLE_RECOVERY if program_leg is None else _live_touch(), read=None)

    [sent] = refusing.submitted_legs
    assert (sent.side, sent.quantity, sent.extended_hours) == (OrderSide.SELL, 10, program_leg is not None)
    exit_effect = repo.effect_operation(accepted.effect_operation_id)
    assert exit_effect is not None and exit_effect.state == "failed"
    assert repo.position(WATCHDOG_SID, "SPY") == 10
    assert repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE,
                                   strategy_instance_id=WATCHDOG_SID) is not None
    refused = repo.last_strategy_transition(strategy_instance_id=WATCHDOG_SID, transition_kind="EXIT_NOT_FLAT")
    assert refused is not None
    return OrderSubmitFailedFacts.from_facts_json(refused["facts_json"])


async def test_a_refused_exit_keeps_alpacas_code_and_names_the_other_bots_open_buy(
    clocked_repo,  # noqa: F811
) -> None:
    """A's refused sell is recorded with Alpaca's code, beside B's buy the Clerk still shows working."""
    repo, _clock = clocked_repo
    working_b = await _a_sells_into_bs_working_buy(repo)

    refused = repo.last_strategy_transition(strategy_instance_id=WATCHDOG_SID, transition_kind="EXIT_NOT_FLAT")

    assert refused is not None
    facts = OrderSubmitFailedFacts.from_facts_json(refused["facts_json"])
    assert facts.broker_error_code == _ILLUSTRATIVE_CODE
    assert facts.opposite_open_order_refs == [working_b.order_ref]
    assert facts.why == (
        "This account had an open or pending SPY buy order when Alpaca refused this sell; "
        "Alpaca refuses an order that could trade against another open order in the same account. "
        f"Alpaca refused the order: {_WASH_TRADE_MESSAGE} attributed_qty=10.0 remains for 'SPY'."
    )


@pytest.mark.parametrize("b_works_for_ms", [0, 3 * DEFAULT_RECOVERY_INTERVAL_MS])
async def test_a_refused_exit_is_sent_again_on_the_first_pass_after_the_other_bots_buy_ends(
    clocked_repo,  # noqa: F811
    b_works_for_ms: int,
) -> None:
    """The ordinary case: B's market buy fills a moment after Alpaca refuses A's market sell (#2622).

    A's refusal names B's buy, which the Clerk's own records showed working.
    While B's buy still works, every pass holds A's exit and sends nothing.
    On the first pass after it fills, nothing works SPY and the broker holds
    the bots' total (10 + 5), so the watchdog re-sends A's full sell at once
    -- not after the 120 s settle wait a refusal with no such order keeps.
    Nothing escalates.
    """
    repo, clock = clocked_repo
    working_b = await _a_sells_into_bs_working_buy(repo)
    refused_at = repo.clock()
    b_working = _broker_order(working_b.order_ref or "", order_id=f"bo-{working_b.order_ref}",
                              status="new", quantity=5.0)
    redrive = _FakeTradePort()

    while repo.clock() - refused_at < b_works_for_ms:
        await reconcile_account(repo, read=_FakeRead(orders=[b_working], positions=[_position("SPY", quantity=10.0)]),
                                trade=redrive, pricing=UNPRICEABLE_RECOVERY)
        clock.advance(DEFAULT_RECOVERY_INTERVAL_MS)
    assert redrive.submitted_legs == []
    filled_b = await _fill_entry(repo, working_b, quantity=5, execution_id="b-exec")
    await reconcile_account(repo, read=_FakeRead(orders=[filled_b], positions=[_position("SPY", quantity=15.0)]),
                            trade=redrive, pricing=UNPRICEABLE_RECOVERY)

    assert repo.clock() - refused_at == b_works_for_ms < _exit_not_flat_redrive_policy().after_ms
    assert [(leg.side, leg.quantity, leg.order_type) for leg in redrive.submitted_legs] == [
        (OrderSide.SELL, 10, OrderType.MARKET)]
    assert _exit_stuck(repo) is None


async def test_a_refused_exit_is_not_sent_into_another_buy_opened_since_the_refusal(
    clocked_repo,  # noqa: F811
) -> None:
    """The immediate re-send waits for the whole buy side of the symbol, not only the order the refusal named.

    B's buy fills, but C's buy started working in the meantime: sending A's
    sell now would be refused again. A's exit holds, and A's page says it is
    waiting for the other side to clear.
    """
    repo, _clock = clocked_repo
    working_b = await _a_sells_into_bs_working_buy(repo)
    filled_b = await _fill_entry(repo, working_b, quantity=5, execution_id="b-exec")
    run_c = _register_spy_lane(repo, "c")
    working_c = await submit_enter(repo, account_id=ACCOUNT_ID, strategy_instance_id="c", decision_id="c-buy",
                                   lifecycle_run_id=run_c, leg=_leg(quantity=3), trade=_FakeTrade())
    c_working = _broker_order(working_c.order_ref or "", order_id=f"bo-{working_c.order_ref}",
                              status="new", quantity=3.0)
    redrive = _FakeTradePort()

    await reconcile_account(repo, read=_FakeRead(orders=[filled_b, c_working], positions=[_position("SPY", quantity=15.0)]),
                            trade=redrive, pricing=UNPRICEABLE_RECOVERY)

    assert redrive.submitted_legs == []
    shown = _shown_recovery(repo)
    assert (shown.kind, shown.reason_code) == ("on_hold", "EXIT_OTHER_ORDER_WORKING")
    assert shown.explanation == _OPPOSITE_ORDER_HOLD


@pytest.mark.parametrize("refusal", ["wash_trade_with_nothing_open", "invalid_order"])
async def test_a_refused_exit_with_no_opposite_order_open_is_sent_again_after_the_120_s_settle_wait(
    clocked_repo,  # noqa: F811
    refusal: str,
) -> None:
    """#2622 changes only a refusal the Clerk's own records explain by an opposite-side order.

    Alpaca refuses A's sell while the Clerk shows nothing open on the buy side
    of SPY (an order placed outside the Clerk, say), or refuses it for another
    reason. The watchdog keeps its timing: every pass holds until the policy's
    re-drive age (120 s) has passed, and the first pass after it re-sends A's
    sell.
    """
    repo, clock = clocked_repo
    ref_a = await _held_position(repo)
    facts = await _as_sell_is_refused(repo, ref_a, refusal=(
        _wash_trade_rejection() if refusal == "wash_trade_with_nothing_open"
        else _alpaca_answer(422, 42210000, "qty must be > 0")
    ))
    assert facts.opposite_open_order_refs == ([] if refusal == "wash_trade_with_nothing_open" else None)
    refused_at = repo.clock()
    policy = _exit_not_flat_redrive_policy()
    broker = _FakeRead(positions=[_position("SPY", quantity=10.0)])
    redrive = _FakeTradePort()

    while not redrive.submitted_legs:
        assert repo.clock() - refused_at <= 2 * policy.after_ms, "never re-sent"
        await reconcile_account(repo, read=broker, trade=redrive, pricing=UNPRICEABLE_RECOVERY)
        if not redrive.submitted_legs:
            clock.advance(DEFAULT_RECOVERY_INTERVAL_MS)

    assert repo.clock() - refused_at == policy.after_ms
    assert [(leg.side, leg.quantity, leg.order_type) for leg in redrive.submitted_legs] == [
        (OrderSide.SELL, 10, OrderType.MARKET)]


async def test_a_refused_exit_escalates_only_if_the_other_bots_order_keeps_working_eight_regular_session_minutes(
    clocked_repo,  # noqa: F811
) -> None:
    """B's buy stays ``new`` at the broker for ten regular-session minutes after A's refusal.

    A's refusal names B's buy, but B's buy never ends, so #2622's immediate
    re-send never applies. The watchdog will not re-drive while any order on
    the symbol can still fill. After the 120 s settle wait, each
    regular-session pass on which B's order still works is a failure, and
    480 s of them (120 s × 4) escalate A to ``EXIT_STUCK``: automatic
    re-drives stop and the owner must flatten. Escalation lands 600 s after
    the refusal; no second order is ever sent.
    """
    repo, clock = clocked_repo
    working_b = await _a_sells_into_bs_working_buy(repo)
    refused_at = repo.clock()
    policy = _exit_not_flat_redrive_policy()
    b_working = _broker_order(working_b.order_ref or "", order_id=f"bo-{working_b.order_ref}",
                              status="new", quantity=5.0)
    broker = _FakeRead(orders=[b_working], positions=[_position("SPY", quantity=10.0)])
    redrive = _FakeTrade()

    while _exit_stuck(repo) is None:
        assert repo.clock() - refused_at <= policy.after_ms * (policy.max_count + 2), "never escalated"
        await reconcile_account(repo, read=broker, trade=redrive, pricing=UNPRICEABLE_RECOVERY)
        if _exit_stuck(repo) is None:
            clock.advance(DEFAULT_RECOVERY_INTERVAL_MS)

    stuck = _exit_stuck(repo)
    assert "work in flight that could fill under it" in stuck["explanation"]
    assert repo.clock() - refused_at == policy.after_ms + policy.after_ms * (policy.max_count + 1)
    assert redrive.submit_calls == []
    assert repo.position(WATCHDOG_SID, "SPY") == 10


async def test_an_exit_ready_at_once_never_starts_its_escalation_clock_early(
    clocked_repo,  # noqa: F811
) -> None:
    """B's buy fills, so A's exit is ready at once, but other Clerk work keeps it from going out (#2622 review).

    The owner's manual QQQ limit is still working, and the Clerk counts any
    working manual order as work in flight on every symbol. Each refused
    re-drive before the refusal has settled for the policy's re-drive age
    (120 s) is a hold, so failure time starts where it always has and
    ``EXIT_STUCK`` still lands 600 s after the refusal, never 480 s.
    """
    repo, clock = clocked_repo
    working_b = await _a_sells_into_bs_working_buy(repo)
    refused_at = repo.clock()
    owner = _ManualTrade(repo=repo)
    qqq = BrokerOrderLeg(symbol="QQQ", side="buy", quantity=1, order_type="limit", limit_price=10.0,
                         time_in_force="gtc")
    manual = await submit_manual_order(repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID,
                                       ticket_id=TICKET_ID, leg_id=LEG_ID, leg=qqq, trade=owner)
    assert manual.leg.order_ref is not None
    filled_b = await _fill_entry(repo, working_b, quantity=5, execution_id="b-exec")
    policy = _exit_not_flat_redrive_policy()
    redrive = _OneAccountTrade(owner)
    broker = _FakeRead(orders=[filled_b, owner.orders[manual.leg.order_ref]],
                       positions=[_position("SPY", quantity=15.0)])

    while _exit_stuck(repo) is None:
        assert repo.clock() - refused_at <= policy.after_ms * (policy.max_count + 2), "never escalated"
        await reconcile_account(repo, read=broker, trade=redrive, pricing=UNPRICEABLE_RECOVERY)
        if _exit_stuck(repo) is None:
            clock.advance(DEFAULT_RECOVERY_INTERVAL_MS)

    assert repo.clock() - refused_at == policy.after_ms + policy.after_ms * (policy.max_count + 1)
    assert redrive.submitted_legs == []


async def test_a_refused_exit_keeps_the_ordinary_timing_when_the_open_orders_cannot_be_read(
    clocked_repo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreadable order record never sends an exit early: the watchdog logs it and waits out the settle age."""
    repo, _clock = clocked_repo
    working_b = await _a_sells_into_bs_working_buy(repo)
    filled_b = await _fill_entry(repo, working_b, quantity=5, execution_id="b-exec")

    def unreadable(*, symbol: str, side: OrderSide) -> tuple:
        raise OrderProjectionReadError("SQLite order 'x' has malformed ENTER_ACCEPTED facts")

    monkeypatch.setattr(repo, "open_opposite_side_orders", unreadable)
    redrive = _FakeTradePort()

    with caplog.at_level(logging.ERROR, logger="app.broker.alpaca.clerk.sqlite.exit_watchdog"):
        await reconcile_account(repo, read=_FakeRead(orders=[filled_b], positions=[_position("SPY", quantity=15.0)]),
                                trade=redrive, pricing=UNPRICEABLE_RECOVERY)

    assert redrive.submitted_legs == []
    assert _shown_recovery(repo).reason_code == "RECOVERY_RETRY_WAIT"
    [logged] = [record for record in caplog.records
                if getattr(record, "action", None) == "exit_watchdog_opposite_orders_unreadable"]
    assert logged.exc_info is not None


_AFTER_HOURS_SELL = ProgramLeg(
    LegShape(order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, limit_price=99.80,
             extended_hours=True, side=OrderSide.SELL),
    valid_until_ms=POST_CLOSE_MS,
)
"""A's 17:00 after-hours limit sell, valid to that day's 20:00 close."""


async def test_a_refused_after_hours_exit_is_sent_again_in_the_same_session_once_the_other_bots_buy_fills(
    clocked_repo,  # noqa: F811
) -> None:
    """Outside the regular session too, the refused exit goes out on the first pass after B's buy ends (#2622).

    At 17:00 ET A's after-hours limit (valid to the 20:00 close) is refused
    while B's buy works; B's buy then fills. On the next pass nothing works
    SPY and the broker holds the bots' total, so the watchdog re-sends A's
    sell as an after-hours limit in the session it was priced for -- not at
    04:00 the next morning. Nothing escalates.
    """
    repo, _clock = clocked_repo
    _walk_clock_to(repo, WATCHDOG_POST_T0)
    working_b = await _a_sells_into_bs_working_buy(repo, program_leg=_AFTER_HOURS_SELL)
    filled_b = await _fill_entry(repo, working_b, quantity=5, execution_id="b-exec")
    broker = _FakeRead(orders=[filled_b], positions=[_position("SPY", quantity=15.0)])
    redrive = _FakeTradePort()

    _walk_clock_to(repo, WATCHDOG_POST_T0 + DEFAULT_RECOVERY_INTERVAL_MS)
    await reconcile_account(repo, read=broker, trade=redrive, pricing=_live_touch())

    assert [(leg.side, leg.quantity, leg.order_type, leg.extended_hours) for leg in redrive.submitted_legs] == [
        (OrderSide.SELL, 10, OrderType.LIMIT, True)]
    assert _exit_stuck(repo) is None


class _OneAccountTrade(_FakeTradePort):
    """The account's trade port for a pass: bot orders as ``_FakeTradePort``, the owner's manual orders from its book."""

    def __init__(self, owner: _ManualTrade) -> None:
        super().__init__()
        self._owner = owner

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        if client_order_id not in self._owner.orders:
            return await super().get_order_by_client_order_id(client_order_id)
        self.lookup_calls.append(client_order_id)
        return self._owner.orders[client_order_id]


async def _as_sell_is_refused_behind_the_owners_buy_limit(
    repo: ClerkSqliteRepository,
) -> tuple[_ManualTrade, str, _OneAccountTrade]:
    """The owner's GTC buy limit on SPY, placed in the morning, still rests at 17:00 when A's after-hours sell is refused.

    Returns the owner's order book, the buy limit's order reference, and the
    account's trade port for the watchdog's passes.
    """
    ref_a = await _held_position(repo)
    owner = _ManualTrade(repo=repo)
    buy_limit = BrokerOrderLeg(symbol="SPY", side="buy", quantity=5, order_type="limit",
                               limit_price=99.90, time_in_force="gtc")
    manual = await submit_manual_order(repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID,
                                       ticket_id=TICKET_ID, leg_id=LEG_ID, leg=buy_limit, trade=owner)
    manual_ref = manual.leg.order_ref
    assert manual_ref is not None
    _walk_clock_to(repo, WATCHDOG_POST_T0)

    facts = await _as_sell_is_refused(repo, ref_a, program_leg=_AFTER_HOURS_SELL)

    assert facts.opposite_open_order_refs == [manual_ref]
    return owner, manual_ref, _OneAccountTrade(owner)


async def test_a_bot_exit_refused_behind_the_owners_resting_buy_limit_is_sent_again_once_the_clerk_cancels_it(
    clocked_repo,  # noqa: F811
) -> None:
    """The owner's own buy limit blocks a bot's exit for as long as it rests (#2622).

    Alpaca's wash-trade protection spans the account, manual tickets
    included, and A's refusal names the owner's resting buy limit. While it
    rests, every pass holds A's exit: no order and, outside the regular
    session, no ``EXIT_STUCK``. The owner cancels it through the Clerk at
    18:30; on the first pass after, A's sell goes out as an after-hours limit
    in the same session -- not at 04:00 the next morning.
    """
    repo, _clock = clocked_repo
    owner, manual_ref, redrive = await _as_sell_is_refused_behind_the_owners_buy_limit(repo)
    for at_ms in (WATCHDOG_POST_T0 + 180_000, WATCHDOG_POST_T0 + 3_600_000):
        _walk_clock_to(repo, at_ms)
        await reconcile_account(repo, read=_FakeRead(orders=[owner.orders[manual_ref]],
                                                     positions=[_position("SPY", quantity=10.0)]),
                                trade=redrive, pricing=_live_touch())
    assert redrive.submitted_legs == []
    assert _exit_stuck(repo) is None
    shown = _shown_recovery(repo)
    assert (shown.kind, shown.reason_code, shown.allowed_from_ms) == ("on_hold", "EXIT_OTHER_ORDER_WORKING", None)
    assert shown.explanation == _OPPOSITE_ORDER_HOLD

    _walk_clock_to(repo, WATCHDOG_POST_T0 + 5_400_000)
    canceled = await submit_manual_order_cancellation(repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID,
                                                      order_ref=manual_ref, trade=owner,
                                                      cancel_request_id="5b0d1f1e-2c1a-4b7e-9d59-0f7f3c2f8a11")
    assert canceled.cancellation.state == "SUCCEEDED"
    await reconcile_account(repo, read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
                            trade=redrive, pricing=_live_touch())

    assert [(leg.side, leg.quantity, leg.order_type, leg.extended_hours) for leg in redrive.submitted_legs] == [
        (OrderSide.SELL, 10, OrderType.LIMIT, True)]
    assert _exit_stuck(repo) is None


class _NoReconciler:
    async def reconcile_account(self, *, trigger: str) -> None:
        raise AssertionError("a lifecycle frame must not trigger reconciliation here")


@pytest.mark.parametrize("route", ["reconcile_sweep", "trade_updates"])
async def test_a_bot_exit_refused_behind_the_owners_buy_limit_is_sent_again_once_it_is_cancelled_at_alpaca(
    clocked_repo,  # noqa: F811
    route: str,
) -> None:
    """The owner cancels the buy limit in Alpaca's own UI at 18:30 (#2622, #2647).

    The Clerk learns the order ended, by the next sweep or by its
    ``trade_updates`` frame. Either route ends the manual order's
    ``MANUAL_ORDER`` effect as well as its order record, so no Clerk work is
    left in flight and nothing is open on the buy side of SPY: A's sell goes
    out as an after-hours limit on the first pass after, in the same session.
    """
    repo, _clock = clocked_repo
    owner, manual_ref, redrive = await _as_sell_is_refused_behind_the_owners_buy_limit(repo)
    _walk_clock_to(repo, WATCHDOG_POST_T0 + 5_400_000)
    now = repo.clock()
    canceled = owner.orders[manual_ref].model_copy(update={
        "status": "canceled", "canceled_at_ms": now, "updated_at_ms": now, "observed_at_ms": now})
    owner.orders[manual_ref] = canceled
    if route == "trade_updates":
        sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler())
        await sink.record_lifecycle_event(
            client_order_id=manual_ref,
            event=BrokerOrderEvent(event_type="canceled", occurred_at_ms=now, price=None, quantity=None),
            event_key="owner-cancel-at-alpaca", order=canceled, recovery_source=None, recovery_window_limit=None,
        )

    await reconcile_account(repo, read=_FakeRead(orders=[canceled], positions=[_position("SPY", quantity=10.0)]),
                            trade=redrive, pricing=_live_touch())

    manual_order = repo.order(manual_ref)
    assert manual_order is not None and manual_order.broker_state == "canceled"
    assert repo.open_opposite_side_orders(symbol="SPY", side=OrderSide.SELL) == ()
    assert [(leg.side, leg.quantity, leg.order_type, leg.extended_hours) for leg in redrive.submitted_legs] == [
        (OrderSide.SELL, 10, OrderType.LIMIT, True)]


# ── One account reading for every bot ─────────────────────────────────────────
# A fill recorded after the last account reading refuses every bot's ENTER
# until a newer reading lands (#2543). Owner decision 2026-09-29 (#2623): such
# an ENTER is no longer dropped. The Clerk reads the account at once, outside
# its intake fence, and judges the ENTER again -- while the decision is still
# on time (its bar's close plus the 20 s delivery allowance). Every other
# refusal still drops it.
#
# The harness is the real facade and the real envelope sync over one budgeted
# paper account with bots ``a`` and ``b`` (and ``c`` where a test deploys it);
# only the broker is a double. Its clock is the repository's, and the sync's
# pause between readings advances it.

def _decision_of(sid: str) -> str:
    return hashlib.sha256(f"{sid}-decides-enter-on-the-noon-bar".encode()).hexdigest()


_B_DECISION = _decision_of("b")
_ON_TIME_UNTIL_MS = NOON + DELIVERY_ALLOWANCE_MS


class _Account(_Read):
    """The account the envelope reads: every read is counted, and each queued action runs during the next one.

    An action runs after the reading's stamp was taken and before the broker
    answers, and never changes that answer -- it is the world moving while
    the account is read.
    """

    def __init__(self) -> None:
        super().__init__(account_observed_at_ms=NOON)
        self.account_reads = 0
        self.while_read: list[Callable[[], Awaitable[None]]] = []

    async def get_account(self) -> BrokerAccountSnapshot:
        self.account_reads += 1
        if self.while_read:
            await self.while_read.pop(0)()
        return await super().get_account()


@dataclass
class _OneAccount:
    repo: ClerkSqliteRepository
    clock: _TestClock
    clerk: SqliteAlpacaClerkFacade
    sync: LiveEnvelopeSync
    account: _Account
    trade: _FakeTradePort
    # One action per coming pause between entry readings, run as it passes.
    while_paused: list[Callable[[], Awaitable[None]]]

    def deploy_c(self) -> None:
        """A third bot on SPY, deployed against a reading roomy enough for its budget."""
        self.repo.register_strategy_instance(strategy_instance_id="c", symbol="SPY", config_hash="seal-c",
                                             exit_terms=TERMS)
        submit_budgeted_deploy(self.repo, strategy_instance_id="c", lifecycle_run_id="run-c", world="real_paper",
            committed_cents=50_000, configuration_hash="seal-c",
            exit_terms_hash=canonical_sha256(TERMS.model_dump(mode="json")), risk_revision=1, actor="owner",
            envelope=_gate(cash=10_000), minimum_position_cost=Decimal("100.01"))

    def accept_a_and_fill(self, *, quantity: int = 1, filled: int = 1) -> EnterSubmission:
        """Bot A's ENTER, accepted, and ``filled`` of its shares recorded -- after the last reading."""
        entry = accept_enter(self.repo, account_id=self.repo.account_id, strategy_instance_id="a", decision_id="a-1",
            lifecycle_run_id="run-a", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=quantity),
            reference_price=100, envelope=self.clerk.live_envelope)
        _append_slice(self.repo, entry, execution_id="a-fill-1", quantity=filled, source_event_at_ms=self.clock())
        return entry

    async def decides_enter(
        self, sid: str = "b", *, quantity: int = 1, bar_close_ms: int = NOON,
        until_ms: int | None = _ON_TIME_UNTIL_MS,
    ) -> EffectOperationReceipt:
        """A bot's ENTER on the bar closing at ``bar_close_ms``, as its runner hands it to the Clerk."""
        bar = RetainedSourceBar.from_market_bar(seq=1, account_id=self.repo.account_id, bar=MarketDataBar(
            feed_id="ibkr", symbol="SPY", start_ms=bar_close_ms - 60_000, end_ms=bar_close_ms, open=Decimal(100),
            high=Decimal(100), low=Decimal(100), close=Decimal(100), volume=100, fetched_at_ms=bar_close_ms,
            session_phase="RTH",
        ))
        decision = _decision_of(sid)
        return await self.clerk.execute_for_instance(
            strategy_instance_id=sid, run_id=f"run-{sid}", decision_id=decision, purpose=EffectPurpose.ENTER,
            action_plan=alpaca_v1_action_plan("SPY"), quantity=quantity, retained_source_bar=bar,
            decision_evidence=EffectDecisionEvidence(
                evaluation_id=decision, bar_ref=bar.bar_ref, symbol="SPY", outcome="enter_intent",
                observed_at_ms=bar_close_ms, decision_bar_close_ms=bar_close_ms, decision_valid_until_ms=until_ms,
            ),
        )

    def enter_commands(self, sid: str = "b") -> int:
        return self.repo._conn.execute(
            "SELECT COUNT(*) FROM commands WHERE strategy_instance_id = ? AND action = 'ENTER'", (sid,)
        ).fetchone()[0]

    def receipts(self, sid: str = "b") -> list[tuple[str, dict[str, Any]]]:
        return [(receipt.outcome, json.loads(receipt.facts_json))
                for receipt in self.repo.decision_receipt_tail(strategy_instance_id=sid, limit=50)]


@pytest.fixture
async def one_account(tmp_path: Path) -> AsyncIterator[_OneAccount]:
    repo = _new_budget_repo(tmp_path)
    _deploy(repo, "a", 50_000)
    _deploy(repo, "b", 50_000)
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    while_paused: list[Callable[[], Awaitable[None]]] = []

    async def pause(seconds: float) -> None:
        clock.advance(round(seconds * 1000))
        if while_paused:
            await while_paused.pop(0)()

    account = _Account()
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    sync = LiveEnvelopeSync(repo=repo, read=account, envelope=gate, sleep=pause)
    trade = _FakeTradePort()
    clerk = SqliteAlpacaClerkFacade(repo=repo, read=account, trade=trade, account_mode="paper",
        live_envelope=gate, entry_reading=sync.read_for_entry)
    assert await sync.tick() == "observed"
    try:
        yield _OneAccount(repo=repo, clock=clock, clerk=clerk, sync=sync, account=account, trade=trade,
                          while_paused=while_paused)
    finally:
        await sync.stop()
        repo.close()


async def test_one_bots_fill_holds_the_other_bots_entry_until_the_next_account_reading(
    one_account: _OneAccount,
) -> None:
    """Two bots decide ENTER on the same bar; A's buy fills first.

    B's ENTER is refused only because A's fill postdates the last reading. It
    is not dropped: the Clerk reads the account at once -- one read, no
    waiting for the 15 s cadence -- and B enters well inside its 20 s.
    """
    one_account.accept_a_and_fill()

    receipt = await one_account.decides_enter()

    assert receipt.state is not EffectOperationState.REJECTED, receipt.explanation
    assert len(receipt.child_order_refs) == 1
    assert one_account.trade.submit_calls == list(receipt.child_order_refs)
    assert one_account.account.account_reads == 2  # the harness's reading, then the one B asked for
    assert one_account.clock() == NOON  # no pause between readings: the first one covered A's fill
    assert [outcome for outcome, _facts in one_account.receipts()] == ["enter_intent"]


@pytest.mark.parametrize("c_decides", ["with_b", "while_bs_reading_is_in_flight"])
async def test_every_entry_behind_the_reading_waits_and_they_share_one_reading(
    one_account: _OneAccount, c_decides: str,
) -> None:
    """A fills; B and C decide ENTER on the same bar, and both enter on one reading.

    B's refusal must not withdraw the account's last reading: C then meets the
    same refusal -- the one that waits -- rather than "not confirmed
    recently", which would drop it under a false reason. C joins the reading
    B asked for; the account is read once for both.
    """
    one_account.deploy_c()
    one_account.accept_a_and_fill()
    asked: list[asyncio.Task[EffectOperationReceipt]] = []

    async def c_decides_meanwhile() -> None:
        asked.append(asyncio.create_task(one_account.decides_enter("c")))
        # Let C be judged, and join the wait, while B's reading is still in
        # flight. No real I/O happens here, so a fixed number of turns does it.
        for _ in range(20):
            await asyncio.sleep(0)

    if c_decides == "with_b":
        b, c = await asyncio.gather(one_account.decides_enter("b"), one_account.decides_enter("c"))
    else:
        one_account.account.while_read = [c_decides_meanwhile]
        b = await one_account.decides_enter("b")
        c = await asked[0]

    assert b.state is not EffectOperationState.REJECTED, b.explanation
    assert c.state is not EffectOperationState.REJECTED, c.explanation
    assert one_account.account.account_reads == 2  # the harness's reading, then the one B and C shared
    assert len(one_account.trade.submit_calls) == 2


async def test_a_fill_during_a_cadence_reading_makes_the_next_entry_wait_not_drop(
    one_account: _OneAccount,
) -> None:
    """A's buy fills while the 15 s cadence reads the account.

    That reading cannot judge the account, but the one before it stays
    published, so B meets the refusal that waits, asks for its own reading
    and enters -- rather than finding no reading and being dropped.
    """
    repo = one_account.repo
    entry_a = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="a", decision_id="a-1",
        lifecycle_run_id="run-a", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        reference_price=100, envelope=one_account.clerk.live_envelope)

    async def a_fills_mid_cadence_read() -> None:
        _append_slice(repo, entry_a, execution_id="a-fill-1", quantity=1, source_event_at_ms=one_account.clock())

    one_account.account.while_read = [a_fills_mid_cadence_read]
    assert await one_account.sync.tick() == "superseded"

    receipt = await one_account.decides_enter()

    assert receipt.state is not EffectOperationState.REJECTED, receipt.explanation
    assert one_account.account.account_reads == 3


async def test_an_entry_deciding_during_the_pause_between_readings_waits_too(
    one_account: _OneAccount,
) -> None:
    """A's second share fills during B's reading; C decides during the pause that follows.

    Neither is dropped: both enter on the next reading, which they share.
    """
    one_account.deploy_c()
    entry_a = one_account.accept_a_and_fill(quantity=2, filled=1)
    asked: list[asyncio.Task[EffectOperationReceipt]] = []

    async def a_fills_again() -> None:
        _append_slice(one_account.repo, entry_a, execution_id="a-fill-2", quantity=1,
                      source_event_at_ms=one_account.clock())

    async def c_decides_meanwhile() -> None:
        asked.append(asyncio.create_task(one_account.decides_enter("c")))
        await asyncio.sleep(0)  # C is judged, and joins the wait, before the next reading starts

    one_account.account.while_read = [a_fills_again]
    one_account.while_paused.append(c_decides_meanwhile)

    b = await one_account.decides_enter("b")
    c = await asked[0]

    assert b.state is not EffectOperationState.REJECTED, b.explanation
    assert c.state is not EffectOperationState.REJECTED, c.explanation
    assert one_account.account.account_reads == 3  # the harness's, the overtaken one, the shared one


async def test_a_fault_in_the_shared_reading_drops_each_waiting_entry_with_a_receipt(
    one_account: _OneAccount,
) -> None:
    """An error no verdict maps must not crash every waiting bot.

    It used to escape each waiter's ``execute_for_instance`` and end its run
    as CRASHED. Each waiting ENTER is dropped with a ``blocked`` receipt
    instead, as the ENTER was refused before the wait existed.
    """
    one_account.deploy_c()
    one_account.accept_a_and_fill()

    async def the_reading_breaks() -> None:
        raise RuntimeError("an adapter or repository fault no verdict maps")

    one_account.account.while_read = [the_reading_breaks]

    results = await asyncio.gather(
        one_account.decides_enter("b"), one_account.decides_enter("c"), return_exceptions=True,
    )

    assert [type(result) for result in results] == [EffectOperationReceipt, EffectOperationReceipt]
    for sid, result in zip(("b", "c"), results, strict=True):
        assert isinstance(result, EffectOperationReceipt) and result.state is EffectOperationState.REJECTED
        ((outcome, facts),) = one_account.receipts(sid)
        assert (outcome, facts["reason_code"]) == ("blocked", LIVE_ENVELOPE_UNOBSERVED)
        assert "the account could not be read again" in facts["refusal_reason"]


async def test_a_reading_overtaken_with_no_time_left_is_recorded_as_out_of_time(
    one_account: _OneAccount,
) -> None:
    """The account was read; the reading was overtaken; no next one could land in time.

    The receipt says the decision ran out of time -- not that the account
    could not be read, which it could.
    """
    entry_a = one_account.accept_a_and_fill(quantity=2, filled=1)

    async def a_fills_again() -> None:
        _append_slice(one_account.repo, entry_a, execution_id="a-fill-2", quantity=1,
                      source_event_at_ms=one_account.clock())

    one_account.account.while_read = [a_fills_again]

    receipt = await one_account.decides_enter(until_ms=NOON + 500)  # inside the 1 s interval

    assert receipt.state is EffectOperationState.REJECTED
    ((outcome, facts),) = one_account.receipts()
    assert (outcome, facts["reason_code"]) == ("blocked", LIVE_ENVELOPE_UNOBSERVED)
    assert "no newer reading arrived while this decision was still on time" in facts["refusal_reason"]
    assert one_account.account.account_reads == 2


async def test_a_reading_still_in_flight_at_the_time_limit_drops_the_entry_then(
    one_account: _OneAccount,
) -> None:
    """A slow broker cannot hold the bot past its decision's time limit.

    The broker never answers; B's decision has 50 ms left. The wait ends at
    the limit -- the bot's loop is free again long before its next bar -- and
    the receipt says the decision ran out of time.
    """
    one_account.accept_a_and_fill()
    never = asyncio.Event()
    one_account.account.while_read = [never.wait]

    receipt = await asyncio.wait_for(one_account.decides_enter(until_ms=NOON + 50), timeout=5)

    assert receipt.state is EffectOperationState.REJECTED
    ((_outcome, facts),) = one_account.receipts()
    assert "no newer reading arrived while this decision was still on time" in facts["refusal_reason"]


async def test_the_clerk_shutting_down_mid_wait_drops_the_entry_as_such(
    one_account: _OneAccount,
) -> None:
    """The authority closes while B's reading is in flight: the reading is cancelled.

    B is dropped with a receipt saying the Clerk was shutting down, and no
    reading task is left behind with an exception nobody retrieves.
    """
    one_account.accept_a_and_fill()
    reading_started = asyncio.Event()
    never = asyncio.Event()

    async def the_broker_hangs() -> None:
        reading_started.set()
        await never.wait()

    one_account.account.while_read = [the_broker_hangs]
    waiting = asyncio.create_task(one_account.decides_enter())
    await reading_started.wait()

    await one_account.sync.stop()
    receipt = await asyncio.wait_for(waiting, timeout=5)

    assert receipt.state is EffectOperationState.REJECTED
    ((_outcome, facts),) = one_account.receipts()
    assert "the account Clerk was shutting down" in facts["refusal_reason"]
    assert one_account.trade.submit_calls == []


async def test_a_fill_recorded_while_the_account_is_read_makes_the_entry_read_again(
    one_account: _OneAccount,
) -> None:
    """A's second share fills while B's reading is in flight, so that reading is withdrawn.

    B keeps waiting: after one pause the account is read again, and B enters
    on the reading that covers both of A's fills.
    """
    entry_a = one_account.accept_a_and_fill(quantity=2, filled=1)

    async def a_fills_again() -> None:
        _append_slice(one_account.repo, entry_a, execution_id="a-fill-2", quantity=1,
                      source_event_at_ms=one_account.clock())

    one_account.account.while_read = [a_fills_again]

    receipt = await one_account.decides_enter()

    assert receipt.state is not EffectOperationState.REJECTED, receipt.explanation
    assert one_account.account.account_reads == 3
    assert one_account.clock() == NOON + round(DEFAULT_ENTRY_READING_INTERVAL_S * 1000)


async def test_an_entry_whose_reading_lands_after_its_time_limit_is_dropped_with_the_reason(
    one_account: _OneAccount,
) -> None:
    """The wait is capped by the 20 s entry-staleness rule; past it the decision is dropped.

    B's reading covers A's fill but returns 21 s after the bar closed. B is
    judged on time no longer: no order, no claim, and one ``blocked`` receipt
    saying why.
    """
    one_account.accept_a_and_fill()

    async def the_read_is_slow() -> None:
        one_account.clock.advance(DELIVERY_ALLOWANCE_MS + 1_000)

    one_account.account.while_read = [the_read_is_slow]

    receipt = await one_account.decides_enter()

    assert receipt.state is EffectOperationState.REJECTED
    assert one_account.trade.submit_calls == []
    assert one_account.enter_commands() == 0
    ((outcome, facts),) = one_account.receipts()
    assert (outcome, facts["reason_code"]) == ("blocked", LIVE_ENVELOPE_UNOBSERVED)
    assert "no newer reading arrived while this decision was still on time" in facts["refusal_reason"]


async def test_a_stop_during_the_wait_drops_the_entry_and_nothing_is_sent_after_it(
    one_account: _OneAccount,
) -> None:
    """The operator stops B while its reading is in flight.

    Stop commits through the Clerk's intake fence, which the wait does not
    hold. The reading lands, but B's run is no longer ACTIVE: B is dropped
    with no ENTER, no reservation and no order.
    """
    one_account.accept_a_and_fill()

    async def operator_stops_b() -> None:
        await one_account.clerk.stop_strategy_run(strategy_instance_id="b", run_id="run-b", reason="operator_stop")

    one_account.account.while_read = [operator_stops_b]

    receipt = await one_account.decides_enter()

    assert receipt.state is EffectOperationState.REJECTED
    assert one_account.trade.submit_calls == []
    assert one_account.enter_commands() == 0
    ((outcome, facts),) = one_account.receipts()
    assert (outcome, facts["reason_code"]) == ("blocked", LIVE_ENVELOPE_UNOBSERVED)
    assert "the bot was stopped while this entry waited" in facts["refusal_reason"]


async def test_nothing_durable_exists_while_an_entry_waits_so_a_crash_leaves_nothing_to_undo(
    one_account: _OneAccount,
) -> None:
    """A process that dies mid-wait leaves no ENTER, claim, order or receipt behind.

    So a crash discards the decision cleanly: the restarted runner's replay
    finds it uncaptured and records ``CANDIDATE_UNCAPTURED_AT_CRASH``
    (``test_candidate_uncaptured_at_crash.py``).
    """
    one_account.accept_a_and_fill()
    durable_mid_wait: list[tuple[int, list[tuple[str, dict[str, Any]]], list[str]]] = []

    async def the_process_could_die_here() -> None:
        durable_mid_wait.append(
            (one_account.enter_commands(), one_account.receipts(), list(one_account.trade.submit_calls))
        )

    one_account.account.while_read = [the_process_could_die_here]

    await one_account.decides_enter()

    assert durable_mid_wait == [(0, [], [])]


async def test_a_waiting_entry_is_accepted_once_however_often_it_is_asked(
    one_account: _OneAccount,
) -> None:
    """The retry never mints a second intent, claim or order.

    A refused-before-acceptance attempt leaves nothing durable behind, so the
    decision's own identity is free for the retry. The runner asking again
    while it waits joins the same attempt; asking after it was accepted
    answers with the existing ENTER.
    """
    one_account.accept_a_and_fill()
    asked_again: list[asyncio.Task[EffectOperationReceipt]] = []

    async def the_runner_asks_again() -> None:
        asked_again.append(asyncio.create_task(one_account.decides_enter()))

    one_account.account.while_read = [the_runner_asks_again]

    first = await one_account.decides_enter()
    second = await asked_again[0]
    replayed = await one_account.decides_enter()

    assert first.state is not EffectOperationState.REJECTED, first.explanation
    assert first.child_order_refs == second.child_order_refs == replayed.child_order_refs
    assert one_account.trade.submit_calls == list(first.child_order_refs)
    assert one_account.enter_commands() == 1
    assert len(one_account.repo.entry_orders_for_strategy("b")) == 1
    assert [outcome for outcome, _facts in one_account.receipts()] == ["enter_intent"]


async def test_an_entry_refused_for_another_reason_is_dropped_without_a_reading(
    one_account: _OneAccount,
) -> None:
    """Only the refusal a newer reading can lift waits.

    B's ten shares exceed its $500 budget. Nothing A did is involved, so no
    reading is taken and B is dropped at once, as before.
    """
    receipt = await one_account.decides_enter(quantity=10)

    assert receipt.state is EffectOperationState.REJECTED
    assert receipt.explanation.startswith(LIVE_ENVELOPE_CASH_EXCEEDED)
    assert one_account.account.account_reads == 1


async def test_a_waiting_entry_the_new_reading_refuses_is_dropped_under_that_refusal(
    one_account: _OneAccount,
) -> None:
    """A's fill makes B wait; the new reading then shows B's order does not fit.

    The first refusal was the reading, so B waited for one. Judged again, B's
    ten shares exceed its budget: B is dropped under the cash refusal, after
    exactly one reading.
    """
    one_account.accept_a_and_fill()

    receipt = await one_account.decides_enter(quantity=10)

    assert receipt.state is EffectOperationState.REJECTED
    assert receipt.explanation.startswith(LIVE_ENVELOPE_CASH_EXCEEDED)
    assert one_account.account.account_reads == 2
    assert one_account.enter_commands() == 0


async def test_the_wait_never_lets_a_fill_during_its_reading_be_spent_twice(
    one_account: _OneAccount,
) -> None:
    """#2441 inside the wait: a withdrawal, then A's fill while B's reading is in flight.

    The account holds $1,000.02, enough for both $500 budgets. A buys four
    $100 shares; one fills, so B's four-share ENTER waits. The owner then
    withdraws $200, and A's other three shares fill while B's reading is in
    flight -- the broker's answer predates them, still showing $700.02. That
    reading is superseded, so B waits one pause for another, which is slow:
    it returns after the fill-visibility grace. The broker still answers
    $700.02, but the reading is stamped when it was issued, inside the grace
    after A's fills, so they stay claimed at cost. B is refused for cash:
    $400.02 is truly left, and B needs $400.01 beside A's $99.99 free budget.
    A stamp taken on return would release A's fills and admit B (#2441).
    """
    account = one_account.account
    account.cash, account.last_equity = 1_000.02, 1_000.02
    assert await one_account.sync.tick() == "observed"
    entry_a = one_account.accept_a_and_fill(quantity=4, filled=1)
    account.cash, account.equity = 700.02, 800.02  # one share held at $100
    account.cash_flows = [_cash_flow("CSW", -200.0)]
    slow_read_ms = FILL_VISIBILITY_GRACE_MS + 1_000

    async def a_fills_the_rest() -> None:
        _append_slice(one_account.repo, entry_a, execution_id="a-fill-2", quantity=3,
                      source_event_at_ms=one_account.clock())

    async def the_read_is_slow() -> None:
        one_account.clock.advance(slow_read_ms)

    account.while_read = [a_fills_the_rest, the_read_is_slow]

    receipt = await one_account.decides_enter(quantity=4)

    assert receipt.state is EffectOperationState.REJECTED
    assert receipt.explanation.startswith(LIVE_ENVELOPE_CASH_EXCEEDED), receipt.explanation
    assert one_account.clock() == NOON + round(DEFAULT_ENTRY_READING_INTERVAL_S * 1000) + slow_read_ms
    assert one_account.trade.submit_calls == []


async def test_a_waiting_entry_whose_reading_lands_after_the_close_is_refused_as_closed(
    one_account: _OneAccount,
) -> None:
    """Every refusal is judged afresh on the retry, the market-closed one included.

    B decides on a bar that closed 15 s before the regular session's close;
    its reading returns after the close. The market ENTER could now reach
    Alpaca after the close, so it is refused as closed and never sent.
    """
    close_ms = session_close_ms_utc(date(2026, 9, 8))
    bar_close_ms = close_ms - 15_000
    _walk_clock_to(one_account.repo, bar_close_ms)
    complete_fee_evidence(one_account.repo)
    one_account.account.account_observed_at_ms = bar_close_ms
    assert await one_account.sync.tick() == "observed"
    one_account.accept_a_and_fill()

    async def the_close_passes() -> None:
        one_account.clock.advance(16_000)

    one_account.account.while_read = [the_close_passes]

    receipt = await one_account.decides_enter(
        bar_close_ms=bar_close_ms, until_ms=bar_close_ms + DELIVERY_ALLOWANCE_MS,
    )

    assert receipt.state is EffectOperationState.REJECTED
    assert receipt.explanation.startswith("MARKET_CLOSED")
    assert one_account.trade.submit_calls == []


_DRY_RUN_SID = "dry-run-spy"


def _dry_run_binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_DRY_RUN_SID, strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY",
        mode="dry_run", quantity=1, action_plan=alpaca_v1_action_plan("SPY"), run_id="run-dry", created_at_ms=0,
        sealed_account_id=synthetic_account_id_for_strategy(_DRY_RUN_SID), exit_terms=TERMS,
        budget_consent=DeployBudgetConsent(
            committed_cents=100_000, risk_revision=0, actor="owner", request_fingerprint="reviewed", world="synthetic",
        ),
    )


def _no_lifecycle_repo(_strategy_instance_id: str) -> Any:
    raise AssertionError("a Dry Run's entry never reads the runner's lifecycle state")


async def test_a_dry_runs_entry_after_its_own_fills_waits_for_its_projection_and_enters(
    tmp_path: Path,
) -> None:
    """A private Dry Run's reading is a projection of its own Clerk records.

    Composed as a Deploy composes it. The bot buys and sells before its sync
    reads again, so its next ENTER is refused only for executions newer than
    the reading. The synthetic Clerk projects a reading at once -- no broker
    is read -- and the bot enters.
    """
    clock = _TestClock(NOON)
    binding = _dry_run_binding()
    market_liveness.reset_market_liveness_store_for_testing()
    _publish_quote(NOON, bid=100.0, ask=100.0)  # the Deploy prices its minimum position at the ask
    authority = SyntheticBindingAuthority(
        binding=binding, artifacts_root=tmp_path, lifecycle_repo_for=_no_lifecycle_repo,
        runtime_in_use=lambda _sid: True, brokers={}, clock=clock,
    )
    try:
        await authority.ensure_recoverable()
        runtime = get_clerk_runtime(binding.sealed_account_id or "")
        assert runtime is not None and runtime.clerk is not None
        await runtime.clerk.register_strategy_run(binding)
        bars = authority.source_bars()
        try:
            bar = bars.append(MarketDataBar(
                feed_id="ibkr", symbol="SPY", start_ms=NOON - 60_000, end_ms=NOON, open=Decimal(100),
                high=Decimal(100), low=Decimal(100), close=Decimal(100), volume=100, fetched_at_ms=NOON,
                session_phase="RTH",
            ), run_id=binding.run_id)
        finally:
            bars.close()
        clock.value = NOON + 1_000

        async def decide(purpose: EffectPurpose, decision_id: str, **evidence: Any) -> Any:
            assert runtime.clerk is not None
            return await runtime.clerk.execute_for_instance(
                strategy_instance_id=binding.strategy_instance_id, run_id=binding.run_id, decision_id=decision_id,
                purpose=purpose, action_plan=binding.action_plan, quantity=1, retained_source_bar=bar, **evidence,
            )

        bought = await decide(EffectPurpose.ENTER, "enter-1")
        sold = await decide(EffectPurpose.EXIT, "exit-1")
        again = await decide(EffectPurpose.ENTER, _B_DECISION, decision_evidence=EffectDecisionEvidence(
            evaluation_id=_B_DECISION, bar_ref=bar.bar_ref, symbol="SPY", outcome="enter_intent",
            observed_at_ms=NOON, decision_bar_close_ms=NOON, decision_valid_until_ms=_ON_TIME_UNTIL_MS,
        ))

        assert bought.child_order_refs and sold.child_order_refs
        assert again.state is not EffectOperationState.REJECTED, again.explanation
        assert again.child_order_refs
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()
