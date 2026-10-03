"""A fill that arrives after its position was discharged is recorded and not credited (#2854).

An operator discharges a bot's position in a symbol once Alpaca is proven
not to hold it. The discharge is the Clerk's account of everything the bot's
orders in that symbol had done by then. One of those orders may have filled
without the Clerk seeing it -- that is why Alpaca did not hold the shares --
and its fill can still arrive afterwards: on the stream, in the closed orders
a reconnect replays, or from an exact lookup.

Credited then, it was counted twice: a closing order that sold 10 left the
bot attributed -10 against a flat account. The fill is now kept on the record
as observed, its order reads ended, and the position stays where the
discharge put it. These tests drive the real store through each way a fill
arrives.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.economic_projection_models import EconomicSnapshot
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import resolve_exit
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.folds import FILL_CREDIT_WITHHELD_BY_DISCHARGE
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import OrderResource, TransitionInput
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.residue_discharge import discharge_attributed_residue
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import EXIT_NOT_FLAT_REASON_CODE, raise_uncertainty
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    FAILED_ENTER_FILLED_REASON_CODE,
    ExitNotFlatCause,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.trade_updates import TradeUpdatesConsumer
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite.conftest import _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_exit_lost_closing_order_resend import (
    FIVE_MINUTES_OF_PASSES,
    RESEND_AFTER_MS,
    SWEEP_INTERVAL_MS,
    _Alpaca,
    _closing_order,
    _exit_state,
    _passes,
)
from tests.broker.alpaca.clerk.sqlite.test_failed_enter_filled_fence import _DuplicateIdTrade
from tests.broker.alpaca.clerk.sqlite.test_failed_exit_closing_order_lookup import (
    A_LATER_RUN_ID,
    REST_BETWEEN_LOOKUPS_MS,
    _its_flatten_fails_after_its_own_order_fills_unseen,
    _one_pass,
    _position_drift,
    _the_sweep,
)
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import (
    SIBLING_RUN_ID,
    SIBLING_SID,
    _context_of,
    _ExactLookup,
    _flatten,
    _stopped_bot_offered_its_flatten,
)
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    crashed_with_exposure,  # noqa: F401 -- the repository, in the regular session, these tests reuse
)
from tests.broker.alpaca.clerk.test_trade_evidence import _authorization_source, _Capture, _no_backoff

CHECKED_FLAT = "Checked Alpaca: the account holds none of it."


class _AlpacaListingItsClosedOrders(_Alpaca):
    """Alpaca whose closed-orders page lists the orders that filled there, as a reconnect reads it."""

    async def list_orders(
        self, *, status: str | None = None, limit: int | None = None, after_ms: int | None = None
    ) -> list[BrokerOrder]:
        if status != "closed":
            return await super().list_orders(status=status, limit=limit, after_ms=after_ms)
        return [order for order in self._has.values() if order.status == "filled"]


def _the_stream(repo: ClerkSqliteRepository, facade: SqliteAlpacaClerkFacade) -> SqliteTradeUpdateEvidenceSink:
    """The account's ``trade_updates`` sink, sharing the facade's intake as the running Clerk's does."""
    return SqliteTradeUpdateEvidenceSink(repo=repo, intake=facade.intake, reconciler=facade)


def _as_alpaca_filled_it(order: OrderResource, *, side: str, symbol: str = "SPY") -> BrokerOrder:
    """The order as Alpaca reports it once it filled whole: 10 shares at $100."""
    return safe_flatten._broker_order(
        order.client_order_id, order_id=order.broker_order_id or f"alpaca-{order.client_order_id}",
        symbol=symbol, status="filled", side=side, quantity=10.0, filled_quantity=10.0, filled_avg_price=100.0,
    )


async def _its_fill_arrives_on_the_stream(
    sink: SqliteTradeUpdateEvidenceSink, order: OrderResource, *, side: str = "sell", symbol: str = "SPY"
) -> None:
    """The ``trade_updates`` frame of the order's one execution: 10 shares at $100."""
    execution_id = f"exec-of-{order.client_order_id}"
    disposition = await sink.record_lifecycle_event(
        client_order_id=order.client_order_id,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=1_700_000_000_700, price=100, quantity=10, execution_id=execution_id,
        ),
        event_key=f"execution:{execution_id}",
        order=_as_alpaca_filled_it(order, side=side, symbol=symbol),
        recovery_source=None,
        recovery_window_limit=None,
    )
    assert disposition == "order_event"


async def _a_reconnect_replays_the_closed_orders(
    repo: ClerkSqliteRepository, facade: SqliteAlpacaClerkFacade, alpaca: _Alpaca
) -> TradeUpdatesConsumer:
    """The stream drops and comes back: the consumer reconciles, then re-feeds Alpaca's closed orders."""
    consumer = TradeUpdatesConsumer(
        evidence_sink=_the_stream(repo, facade),
        read=alpaca,
        frame_source=_authorization_source,
        journal=cast(CaptureJournal, _Capture()),
        backoff=lambda _attempt: _no_backoff(),
        max_reconnects=1,
    )
    await consumer.run()
    return consumer


def _fills(repo: ClerkSqliteRepository, order: OrderResource) -> list[tuple[str, float, float]]:
    """The executions on the order's record: side, quantity and price."""
    return [(fill["side"], fill["qty"], fill["price"]) for fill in repo.fills_for_order(order.order_ref)]


def _the_bots_money(repo: ClerkSqliteRepository) -> EconomicSnapshot:
    """The bot's P&L view, which reads only while its executions add up to the position it is attributed."""
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    try:
        snapshot = reader.bot_economic_snapshot(SID, session_window=None)
    finally:
        reader.close()
    assert snapshot is not None
    return snapshot


def _reads_filled(repo: ClerkSqliteRepository, order: OrderResource) -> bool:
    """Whether the Clerk's record of the order says Alpaca filled it."""
    recorded = repo.order(order.order_ref)
    assert recorded is not None
    return recorded.broker_state == "filled"


async def _a_lost_closing_order_is_written_off(
    repo: ClerkSqliteRepository, alpaca: _Alpaca
) -> tuple[SqliteAlpacaClerkFacade, OrderResource]:
    """The bot's closing order sold its 10 SPY unseen, its EXIT failed, and the operator discharged the 10.

    ``alpaca`` loses the answer to its first submit. The Clerk then
    attributes the bot nothing, Alpaca holds nothing, and the account reads
    clean, with the closing order's outcome still not on the Clerk's record.
    Returns the account's facade and that order.
    """
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)
    closing = await _its_flatten_fails_after_its_own_order_fills_unseen(repo, alpaca, facade, current_context)
    assert (await _one_pass(repo, facade)).verdict == "position_drift"
    await facade.discharge_attributed_residue(strategy_instance_id=SID, symbol="SPY", reason=CHECKED_FLAT)
    assert repo.position(SID, "SPY") == 0
    assert (await _one_pass(repo, facade)).verdict == "clean"
    return facade, closing


async def test_a_closing_orders_fill_that_arrives_on_the_stream_after_its_position_was_discharged_is_not_credited(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The regression (#2854): the late frame was credited on top of the discharge, -10 against a flat account.

    The bot stays at 0. The execution is on the order's record, the order
    reads filled, and the account still reads clean. The sale is the
    execution the discharge stood in for, so the bot's executions add up to
    its position again and its P&L reads. Nothing asks Alpaca about the order
    again, even while its symbol reads as drifted for a reason of its own,
    and nothing is sold.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    facade, closing = await _a_lost_closing_order_is_written_off(repo, alpaca)

    await _its_fill_arrives_on_the_stream(_the_stream(repo, facade), closing)

    assert repo.position(SID, "SPY") == 0
    assert _fills(repo, closing) == [("SELL", 10.0, 100.0)]
    recorded = repo.order(closing.order_ref)
    assert recorded is not None
    assert (recorded.broker_order_id, recorded.broker_state) == (f"alpaca-{closing.client_order_id}", "filled")
    assert (await _one_pass(repo, facade)).verdict == "clean"
    assert _position_drift(repo) is None
    money = _the_bots_money(repo)
    assert money.exposure == {}
    assert ("sell", 10.0, 100.0) in [(fill.side.value, fill.quantity, fill.fill_price) for fill in money.recent_fills]

    alpaca.holds = 3.0
    alpaca.answers_its_lookup(closing.client_order_id)
    _walk_clock_to(repo, repo.clock() + REST_BETWEEN_LOOKUPS_MS)
    looked_up_before = alpaca.looked_up.count(closing.client_order_id)
    await _passes(repo, _the_sweep(facade), FIVE_MINUTES_OF_PASSES)

    assert alpaca.looked_up.count(closing.client_order_id) == looked_up_before
    assert repo.position(SID, "SPY") == 0
    assert _fills(repo, closing) == [("SELL", 10.0, 100.0)]
    assert len(alpaca.sent) == 1


async def test_a_reconnects_replay_of_a_closing_order_whose_position_was_discharged_is_not_credited(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The likeliest way the fill arrives: the stream reconnects and re-feeds Alpaca's closed orders.

    A replayed order carries its total, not its executions, and is folded by
    another route than a live frame. The bot stays at 0 there too, with the
    sale on the order's record and the order read as filled.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaListingItsClosedOrders(repo, clock, submits=("arrives_unanswered",))
    facade, closing = await _a_lost_closing_order_is_written_off(repo, alpaca)

    consumer = await _a_reconnect_replays_the_closed_orders(repo, facade, alpaca)

    assert consumer.counters.gap_reconciled == 1
    assert repo.position(SID, "SPY") == 0
    assert _fills(repo, closing) == [("SELL", 10.0, 100.0)]
    assert [fill["evidence_source"] for fill in repo.fills_for_order(closing.order_ref)] == ["cumulative_recovery"]
    assert _reads_filled(repo, closing)
    assert (await _one_pass(repo, facade)).verdict == "clean"
    assert len(alpaca.sent) == 1


async def test_a_late_fill_delivered_again_changes_nothing(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The same execution arrives twice on the stream and once more in a reconnect's replay: recorded once, credited never."""
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaListingItsClosedOrders(repo, clock, submits=("arrives_unanswered",))
    facade, closing = await _a_lost_closing_order_is_written_off(repo, alpaca)
    await _its_fill_arrives_on_the_stream(_the_stream(repo, facade), closing)
    on_record = repo.transitions_for_order(closing.order_ref)

    # A sink of its own each time, as after a restart: nothing in memory remembers the frame.
    await _its_fill_arrives_on_the_stream(_the_stream(repo, facade), closing)
    await _a_reconnect_replays_the_closed_orders(repo, facade, alpaca)

    assert repo.position(SID, "SPY") == 0
    assert _fills(repo, closing) == [("SELL", 10.0, 100.0)]
    now_on_record = repo.transitions_for_order(closing.order_ref)
    assert now_on_record[: len(on_record)] == on_record
    # Neither delivery recorded the execution a second time, as a slice or as a total.
    kinds_since = [row["transition_kind"] for row in now_on_record[len(on_record) :]]
    assert "EXECUTION_SLICE_FILLED" not in kinds_since and "ORDER_FILL_OBSERVED" not in kinds_since


async def test_a_fill_of_an_order_created_after_the_discharge_is_credited(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A discharge accounts for the orders that came before it, and for no later one.

    After the discharge the bot runs again and buys 10: that entry is
    credited. It is stopped, and its next Flatten fails as the first did. Then
    both closing orders' fills arrive. The first, written off by the
    discharge, moves nothing; the second closes the 10 the bot holds.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered", "arrives_unanswered"))
    facade, written_off = await _a_lost_closing_order_is_written_off(repo, alpaca)

    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=A_LATER_RUN_ID)
    await safe_flatten._held_position(
        repo, decision_id="enter-2", lifecycle_run_id=A_LATER_RUN_ID, execution_id="exec-2"
    )
    assert repo.position(SID, "SPY") == 10
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=A_LATER_RUN_ID, operator_reason="operator_stop",
    )
    alpaca.holds = 10.0
    _walk_clock_to(repo, repo.clock() + 5_000)
    assert (await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")).verdict == "clean"
    later = await _its_flatten_fails_after_its_own_order_fills_unseen(repo, alpaca, facade, _context_of(repo, SID))
    stream = _the_stream(repo, facade)

    await _its_fill_arrives_on_the_stream(stream, written_off)
    assert repo.position(SID, "SPY") == 10

    await _its_fill_arrives_on_the_stream(stream, later)
    assert repo.position(SID, "SPY") == 0
    assert _fills(repo, written_off) == _fills(repo, later) == [("SELL", 10.0, 100.0)]
    assert (await _one_pass(repo, facade)).verdict == "clean"
    assert len(alpaca.sent) == 2


async def test_an_order_that_was_live_after_all_and_fills_after_the_discharge_leaves_a_drift_for_the_operator(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The other way round: the discharge did not account for the order, because the order had not filled.

    The Flatten's send was lost and no read showed its order. The shares left
    the account another way, so the Flatten failed uncovered and the operator
    discharged the 10. The order was at Alpaca all along and sells 10 the
    account no longer holds. The Clerk cannot tell this from a fill the
    discharge already counted, so it records the sale and credits nothing:
    the account reads as drifted until the operator acts, and the Clerk sends
    no order of its own.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)
    await _flatten(facade, current_context)
    closing = _closing_order(repo)
    alpaca.holds = 0.0
    _walk_clock_to(repo, repo.clock() + RESEND_AFTER_MS - SWEEP_INTERVAL_MS)
    assert (await _one_pass(repo, facade)).verdict == "position_drift"
    assert _exit_state(repo, closing.effect_operation_id) == "failed"
    await facade.discharge_attributed_residue(strategy_instance_id=SID, symbol="SPY", reason=CHECKED_FLAT)
    assert (await _one_pass(repo, facade)).verdict == "clean"

    alpaca.fills(closing.client_order_id)
    assert alpaca.holds == -10.0
    await _its_fill_arrives_on_the_stream(_the_stream(repo, facade), closing)

    assert repo.position(SID, "SPY") == 0
    assert _fills(repo, closing) == [("SELL", 10.0, 100.0)]
    drifted = await _one_pass(repo, facade)
    assert (drifted.verdict, drifted.drifted_symbols) == ("position_drift", ("SPY",))
    assert _position_drift(repo) is not None

    await _passes(repo, _the_sweep(facade), FIVE_MINUTES_OF_PASSES)

    assert repo.position(SID, "SPY") == 0
    assert _position_drift(repo) is not None
    assert len(alpaca.sent) == 1


async def test_a_short_bots_closing_buy_that_arrives_after_its_position_was_discharged_is_not_credited(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The other side: a bot short 10 whose closing BUY covered unseen would be left attributed +10."""
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    alpaca.holds = -10.0
    facade, current_context = await _stopped_bot_offered_its_flatten(
        repo, account=alpaca, trade=alpaca, entry_side="sell"
    )
    await _flatten(facade, current_context)
    closing = _closing_order(repo)
    alpaca.lists_among_its_open_orders(closing.client_order_id)

    async def the_exit_is_driven() -> object:
        return await resolve_exit(
            repo, effect_operation_id=closing.effect_operation_id, trade=alpaca,
            pricing=UNPRICEABLE_RECOVERY, read=alpaca,
        )

    await _passes(repo, the_exit_is_driven, RESEND_AFTER_MS // SWEEP_INTERVAL_MS)
    alpaca.fills(closing.client_order_id)
    # ``fills`` moves the account as a sale does; this order bought the short back.
    alpaca.holds = 0.0
    await _passes(repo, the_exit_is_driven, 1)
    assert _exit_state(repo, closing.effect_operation_id) == "failed"
    assert repo.position(SID, "SPY") == -10
    assert (await _one_pass(repo, facade)).verdict == "position_drift"
    await facade.discharge_attributed_residue(strategy_instance_id=SID, symbol="SPY", reason=CHECKED_FLAT)
    assert (await _one_pass(repo, facade)).verdict == "clean"

    await _its_fill_arrives_on_the_stream(_the_stream(repo, facade), closing, side="buy")

    assert repo.position(SID, "SPY") == 0
    assert _fills(repo, closing) == [("BUY", 10.0, 100.0)]
    assert _reads_filled(repo, closing)
    assert (await _one_pass(repo, facade)).verdict == "clean"
    assert len(alpaca.sent) == 1


# ── Entries, and whose discharge it is ───────────────────────────────────────
# An entry the Clerk recorded as failed while its order was live at Alpaca
# (#2304) is the order created before the discharge. The position discharged
# is one a later entry opened and the operator then closed at Alpaca by hand.


def _the_bots_stream(repo: ClerkSqliteRepository) -> SqliteTradeUpdateEvidenceSink:
    """The account's ``trade_updates`` sink, for the tests that drive the store without a facade."""
    return SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=safe_flatten._NoReconciler()
    )


async def _an_entry_fails_while_its_order_is_live(repo: ClerkSqliteRepository, *, side: str = "buy") -> OrderResource:
    """The bot's entry for 10 SPY is recorded as failed; Alpaca has the order, and it has not filled."""
    submission = await submit_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="enter-lost",
        lifecycle_run_id=RUN_ID, leg=safe_flatten._leg(quantity=10, side=side),
        trade=_DuplicateIdTrade(repo, fill_first=False),
    )
    assert submission.order_ref is not None
    entered = repo.effect_operation(submission.effect_operation_id)
    assert entered is not None and entered.state == "failed"
    order = repo.order(submission.order_ref)
    assert order is not None
    return order


async def _a_position_is_opened_and_then_discharged(
    repo: ClerkSqliteRepository,
    *,
    sid: str = SID,
    run_id: str = RUN_ID,
    symbol: str = "SPY",
    side: str = "buy",
    alpaca_holds: float = 0.0,
) -> None:
    """``sid`` enters 10 of ``symbol``, its exit leaves them attributed, and the operator discharges them.

    ``alpaca_holds`` is what the account holds of the symbol when the
    discharge reads it: what every other bot is attributed there.
    """
    submission = await submit_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, decision_id=f"enter-{symbol}",
        lifecycle_run_id=run_id, leg=safe_flatten._leg(symbol=symbol, quantity=10, side=side),
        trade=safe_flatten._FakeTrade(),
    )
    entry = repo.order(submission.order_ref or "")
    assert entry is not None
    await _its_fill_arrives_on_the_stream(_the_bots_stream(repo), entry, side=side, symbol=symbol)
    held = 10.0 if side == "buy" else -10.0
    assert repo.position(sid, symbol) == held
    raise_uncertainty(
        repo,
        strategy_instance_id=sid,
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        headline="A completed EXIT left attributed exposure",
        explanation="The closing order ended without flattening the position.",
        operator_impact="New exposure is paused for this strategy.",
        next_step="Flatten the position, or discharge it if Alpaca does not hold it.",
        evidence_refs=("exit-evidence",),
        cause_facts=ExitNotFlatCause(symbol=symbol, attributed_qty=held).to_mapping(),
        severity="error",
    )
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, lifecycle_run_id=run_id, operator_reason="operator_stop",
    )
    held_at_alpaca = [safe_flatten._position(symbol, quantity=alpaca_holds)] if alpaca_holds else []
    await discharge_attributed_residue(
        repo, read=safe_flatten._FakeRead(positions=held_at_alpaca), intake=ReentrantAsyncLock(),
        strategy_instance_id=sid, symbol=symbol, operator_reason=CHECKED_FLAT,
    )
    assert repo.position(sid, symbol) == 0


def _failed_entry_fence(repo: ClerkSqliteRepository) -> dict | None:
    """The bot's open "an entry recorded as failed has filled" notice, if it has one."""
    return repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=FAILED_ENTER_FILLED_REASON_CODE, strategy_instance_id=SID
    )


async def _a_pass_against_a_flat_account(repo: ClerkSqliteRepository) -> str:
    """One pass of the Clerk against an account that holds nothing and has nothing open; its verdict."""
    result = await reconcile_account(
        repo, read=safe_flatten._FakeRead(positions=[]), trade=safe_flatten._FakeTrade(),
        pricing=UNPRICEABLE_RECOVERY,
    )
    return result.verdict


@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_an_entrys_fill_that_arrives_after_its_position_was_discharged_is_not_credited(
    crashed_with_exposure,  # noqa: F811
    side: str,
) -> None:
    """Not closing orders alone: a discharge accounts for every order of the bot's in the symbol before it.

    An entry the Clerk recorded as failed was live and filled unseen. The
    operator closed the account's whole position at Alpaca and discharged
    what the bot was still attributed. The entry's fill then arrives: once
    credited, and fenced as a position the bot holds. It is recorded, the
    bot holds nothing, and no notice says it does -- long or short.
    """
    repo, _clock = crashed_with_exposure
    lost = await _an_entry_fails_while_its_order_is_live(repo, side=side)
    await _a_position_is_opened_and_then_discharged(repo, side=side)

    await _its_fill_arrives_on_the_stream(_the_bots_stream(repo), lost, side=side)

    assert repo.position(SID, "SPY") == 0
    assert _fills(repo, lost) == [(side.upper(), 10.0, 100.0)]
    assert _reads_filled(repo, lost)
    assert _failed_entry_fence(repo) is None
    assert await _a_pass_against_a_flat_account(repo) == "clean"
    assert _failed_entry_fence(repo) is None
    assert repo.position(SID, "SPY") == 0


async def test_an_exact_lookup_that_finds_an_entry_filled_after_its_position_was_discharged_credits_nothing(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The pass's own lookup of a failed entry in a drifted symbol folds what it finds by the same rule.

    The symbol reads as drifted for a reason of its own -- 3 shares no bot
    holds -- so the pass asks Alpaca about the failed entry, and is told it
    filled. The fill is recorded and not credited, the order reads filled,
    and the next pass does not ask again.
    """
    repo, _clock = crashed_with_exposure
    lost = await _an_entry_fails_while_its_order_is_live(repo)
    await _a_position_is_opened_and_then_discharged(repo)
    alpaca_answers = _ExactLookup()
    alpaca_answers.answer = _as_alpaca_filled_it(lost, side="buy")

    async def one_pass() -> str:
        _walk_clock_to(repo, repo.clock() + REST_BETWEEN_LOOKUPS_MS)
        result = await reconcile_account(
            repo, read=safe_flatten._FakeRead(positions=[safe_flatten._position("SPY", quantity=3.0)]),
            trade=alpaca_answers, pricing=UNPRICEABLE_RECOVERY,
        )
        return result.verdict

    assert await one_pass() == "position_drift"

    assert alpaca_answers.lookup_calls == [lost.client_order_id]
    assert repo.position(SID, "SPY") == 0
    assert _fills(repo, lost) == [("BUY", 10.0, 100.0)]
    assert _reads_filled(repo, lost)
    assert _failed_entry_fence(repo) is None

    assert await one_pass() == "position_drift"
    assert alpaca_answers.lookup_calls == [lost.client_order_id]
    assert repo.position(SID, "SPY") == 0


async def test_another_bots_discharge_in_the_symbol_withholds_nothing_of_this_bots(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A discharge accounts for its own bot's orders only.

    A sibling bot's SPY position is discharged after this bot's entry was
    created. This bot's entry then fills: it is credited, and fenced as the
    fill on a failed entry it is.
    """
    repo, _clock = crashed_with_exposure
    lost = await _an_entry_fails_while_its_order_is_live(repo)
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SIBLING_SID, symbol="SPY", config_hash="h2"
    )
    submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SIBLING_SID, lifecycle_run_id=SIBLING_RUN_ID
    )
    await _a_position_is_opened_and_then_discharged(repo, sid=SIBLING_SID, run_id=SIBLING_RUN_ID)

    await _its_fill_arrives_on_the_stream(_the_bots_stream(repo), lost, side="buy")

    assert repo.position(SID, "SPY") == 10
    assert repo.position(SIBLING_SID, "SPY") == 0
    assert _failed_entry_fence(repo) is not None


async def test_another_bots_discharge_does_not_end_the_lookups_of_this_bots_closing_order(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lookup list drops a closing order for its own bot's discharge, not for a sibling's.

    This bot's Flatten failed after its lost order filled unseen, and a
    sibling's SPY position is then discharged. The pass still asks Alpaca
    about this bot's closing order, and credits the fill it finds.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)
    closing = await _its_flatten_fails_after_its_own_order_fills_unseen(repo, alpaca, facade, current_context)
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SIBLING_SID, symbol="SPY", config_hash="h2"
    )
    submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SIBLING_SID, lifecycle_run_id=SIBLING_RUN_ID
    )
    await _a_position_is_opened_and_then_discharged(
        repo, sid=SIBLING_SID, run_id=SIBLING_RUN_ID, alpaca_holds=10.0
    )
    alpaca.answers_its_lookup(closing.client_order_id)
    looked_up_before = alpaca.looked_up.count(closing.client_order_id)

    found = await _one_pass(repo, facade)

    assert alpaca.looked_up.count(closing.client_order_id) == looked_up_before + 1
    assert found.verdict == "clean"
    assert repo.position(SID, "SPY") == 0
    assert _fills(repo, closing) == [("SELL", 10.0, 100.0)]
    assert len(alpaca.sent) == 1


async def test_the_bots_discharge_in_another_symbol_withholds_nothing_in_this_one(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A discharge accounts for its own symbol only.

    The bot's QQQ position is discharged after its SPY entry was created. The
    SPY entry then fills: it is credited, and fenced.
    """
    repo, _clock = crashed_with_exposure
    lost = await _an_entry_fails_while_its_order_is_live(repo)
    await _a_position_is_opened_and_then_discharged(repo, symbol="QQQ")

    await _its_fill_arrives_on_the_stream(_the_bots_stream(repo), lost, side="buy")

    assert repo.position(SID, "SPY") == 10
    assert repo.position(SID, "QQQ") == 0
    assert _failed_entry_fence(repo) is not None


# ── The record ───────────────────────────────────────────────────────────────


async def test_a_rebuild_from_the_mirror_withholds_the_same_credit_and_keeps_the_same_record(
    crashed_with_exposure,  # noqa: F811
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """What was withheld is on the ledger itself, so a rebuild decides it again the same way.

    The late fill is recorded and not credited; the bot then runs again and
    buys 10, which is. A database rebuilt from the mirror alone holds the
    same transitions, the same 10, the same fills and the same orders.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    facade, closing = await _a_lost_closing_order_is_written_off(repo, alpaca)
    await _its_fill_arrives_on_the_stream(_the_stream(repo, facade), closing)
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=A_LATER_RUN_ID)
    await safe_flatten._held_position(
        repo, decision_id="enter-2", lifecycle_run_id=A_LATER_RUN_ID, execution_id="exec-2"
    )
    assert repo.position(SID, "SPY") == 10
    withheld = [
        row for row in repo.custody_transitions() if row["summary_code"] == FILL_CREDIT_WITHHELD_BY_DISCHARGE
    ]
    assert [(row["transition_kind"], row["order_ref"]) for row in withheld] == [
        ("EXECUTION_SLICE_FILLED", closing.order_ref)
    ]

    # Everything the account has on disk but its database: the mirror and the registry it rebuilds from.
    elsewhere = tmp_path_factory.mktemp("rebuilt")
    shutil.copytree(tmp_path, elsewhere, ignore=shutil.ignore_patterns("clerk.db*"), dirs_exist_ok=True)
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id=ACCOUNT_ID, artifacts_root=elsewhere, clock=clock)
    try:
        assert rebuilt.custody_transitions() == repo.custody_transitions()
        assert rebuilt.position(SID, "SPY") == 10
        assert rebuilt.attributed_positions_by_subject() == repo.attributed_positions_by_subject()
        orders = repo.orders_for_strategy(SID)
        assert rebuilt.orders_for_strategy(SID) == orders
        for order in orders:
            assert rebuilt.fills_for_order(order.order_ref) == repo.fills_for_order(order.order_ref)
        assert _fills(rebuilt, closing) == [("SELL", 10.0, 100.0)]
    finally:
        rebuilt.close()


async def test_a_fill_marked_as_written_off_with_no_discharge_behind_it_is_refused(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The mark alone withholds nothing: the ledger must hold the discharge, or the fill is not recorded."""
    repo, _clock = crashed_with_exposure
    entry_ref = await safe_flatten._held_position(repo)
    entry = repo.order(entry_ref)
    assert entry is not None
    entered = repo.effect_operation(entry.effect_operation_id)
    assert entered is not None
    recorded = repo.transitions_for_order(entry_ref)
    another_slice = ExecutionSliceFilledFacts(
        execution_id="exec-unbacked", symbol="SPY", side="BUY", slice_qty=1.0, slice_price=100.0,
        fee=None, fee_fidelity="not_reported", evidence_source="websocket", source_event_at_ms=1_700_000_000_800,
    )

    with pytest.raises(ValueError, match="residue discharge the ledger does not hold"):
        repo.append_transition(
            TransitionInput(
                strategy_instance_id=SID,
                run_id=entered.run_id,
                command_id=entered.command_id,
                effect_operation_id=entered.effect_operation_id,
                order_ref=entry_ref,
                transition_kind="EXECUTION_SLICE_FILLED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="in_progress",
                source_event_at_ms=another_slice.source_event_at_ms,
                clerk_observed_at_ms=repo.clock(),
                summary_code=FILL_CREDIT_WITHHELD_BY_DISCHARGE,
                facts_json=another_slice.to_facts_json(),
            )
        )

    assert repo.position(SID, "SPY") == 10
    assert repo.transitions_for_order(entry_ref) == recorded
    assert [fill["execution_id"] for fill in repo.fills_for_order(entry_ref)] == ["exec-1"]
