"""An option bot's entry rests, and its strategy is settled on what the entry came to (#2913).

``deployment_validation`` decides on one-minute bars: ENTER on the second of
two green bars, EXIT three bars later. A discarded ENTER keeps its streak, so
the next green bar is an ENTER again. The bot listens to SPY and buys SPY
calls at the money, expiring on Friday: the 585 call, quoted 4.08 bid and
4.12 offered. So its entry is a limit order to buy at 4.12.

On a Paper account that order rests at Alpaca. The runner waits for what it
came to, no longer than fifty seconds after the bar it decided on closed. The
bot's order terms are the ones a Deploy starts from: the Clerk cancels what
has not filled twenty seconds after it took the entry. An entry is taken only
while that cancel could still be proven inside the runner's wait.

Everything but the IBKR connection, the bar feed, the vendors and Alpaca is
the real thing (``_paper_option_floor``). Alpaca is the double the Clerk's
own Paper tests use. No order was sent to Alpaca and no key was used.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes
from app.broker.alpaca.clerk.held_option_quotes import reset_held_option_quotes_for_testing
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionPlanRefused
from app.broker.alpaca.clerk.simulated_option_orders import SimulatedOptionOrders
from app.broker.alpaca.clerk.sqlite import decision_receipts as decision_receipts_module
from app.broker.alpaca.clerk.sqlite import runtime as clerk_runtime
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.entry_outcome import (
    ENTRY_OUTCOME_LIMIT_PAST,
    EntryOutcomeWaitRefused,
    EntrySettlement,
    EntryStanding,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import CONTRACT_REFERENCE_READ_BOUND_MS, SqliteAlpacaClerkFacade
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
from app.broker.ibkr.bars import REALTIME_BAR_STALL_TIMEOUT_S
from app.broker.ibkr.config import reset_settings_for_testing
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.marketdata.feed import DELIVERY_ALLOWANCE_MS, FeedHealth, MarketDataBar
from app.options.contract import OptionContract
from app.options.deadlines import CloseTerms
from app.options.entry_came_to import EntryCameTo
from app.options.quotes import QuoteFault
from app.services import bot_option_run, market_liveness
from app.services.bot_dry_run import DryRunActivity, DryRunActivityJournal
from app.services.bot_option_run import (
    CANCEL_PROOF_ROOM_MS,
    ENTRY_OUTCOME_SHORTEST_WAIT_MS,
    ENTRY_OUTCOME_WAIT_MS,
    OptionEntryRefusal,
    OptionOrderTerms,
    OptionRun,
    entry_came_to,
)
from app.services.bot_trade_strategy_warmup import ENTRY_DISCARDED
from app.services.feed_continuity_policy import DECISION_LATE_REASON_CODE
from app.services.market_liveness import QUOTE_MAX_AGE_MS
from app.services.source_bar_ledger import SourceBarLedger
from app.utils import timestamps
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES

from ._option_floor import (
    CALL_585,
    DELIVERY_MS,
    ENTER_MS,
    EXIT_MS,
    FIRST_GREEN_MS,
    FRIDAY,
    MINUTE,
    SID,
    Floor,
    Ran,
    Step,
    green,
    one_call,
    red,
)
from ._paper_option_floor import PAPER_SID, PaperFloor, paper_binding_for

CALL = CALL_585.occ_symbol
# The runner decides as its bar is delivered, and the Clerk takes the entry at that clock.
TAKEN_MS = ENTER_MS + DELIVERY_MS
# How long the Clerk lets an entry rest under the terms a Deploy starts from.
WAIT_MS = CloseTerms().entry_wait_ms
# When the runner stops waiting for what the 10:31 entry came to.
WAITED_UNTIL_MS = ENTER_MS + ENTRY_OUTCOME_WAIT_MS


@pytest.fixture
async def on_paper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Callable[..., PaperFloor]]:
    """Build a Clerk process with an option bot on a Paper account; everything it started is closed after."""
    reset_settings_for_testing()
    reset_held_option_quotes_for_testing()
    market_liveness.reset_market_liveness_store_for_testing()
    built: list[PaperFloor] = []

    def build(**floor: Any) -> PaperFloor:
        built.append(PaperFloor(monkeypatch, tmp_path, **floor))
        return built[-1]

    try:
        yield build
    finally:
        for floor in built:
            floor.lane.close()
            await floor.held.stop()
            if floor.supervisor is not None:
                await floor.supervisor.close()
            await floor.close()
        await close_synthetic_clerk_runtimes()
        reset_held_option_quotes_for_testing()
        reset_settings_for_testing()
        market_liveness.reset_market_liveness_store_for_testing()


@pytest.fixture
async def paper(on_paper: Callable[..., PaperFloor]) -> PaperFloor:
    """The usual Paper floor: the bot listens to SPY and buys one SPY call at the money, with $1,000.00."""
    return await on_paper().open()


@pytest.fixture
async def on_the_floor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Callable[..., Floor]]:
    """Build a Clerk process with an option bot's Dry Run on it; everything it started is closed after."""
    reset_settings_for_testing()
    reset_held_option_quotes_for_testing()
    market_liveness.reset_market_liveness_store_for_testing()
    built: list[Floor] = []

    def build(**floor: Any) -> Floor:
        built.append(Floor(monkeypatch, tmp_path, **floor))
        return built[-1]

    try:
        yield build
    finally:
        for floor in built:
            floor.lane.close()
            await floor.held.stop()
            if floor.supervisor is not None:
                await floor.supervisor.close()
        await close_synthetic_clerk_runtimes()
        reset_held_option_quotes_for_testing()
        reset_settings_for_testing()
        market_liveness.reset_market_liveness_store_for_testing()


@pytest.fixture
async def dry_run(on_the_floor: Callable[..., Floor]) -> Floor:
    """The Dry Run floor: the same bot on its own simulated account."""
    return await on_the_floor().open()


def _two_greens() -> list[Step]:
    """The ENTER is decided on the second green bar, which closes at 10:31."""
    return [Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))]


def _round_trip() -> list[Step]:
    """Two green bars, then three red: the ENTER at 10:31 and the EXIT at 10:34."""
    return [*_two_greens(), Step(red(ENTER_MS + MINUTE)), Step(red(ENTER_MS + 2 * MINUTE)), Step(red(EXIT_MS))]


def _decision(ran: Ran, close_ms: int) -> str:
    """The id of the decision taken on the bar that closed at ``close_ms``, as the runner named it to the Clerk."""
    (found,) = [facts["evaluation_id"] for _, facts in ran.receipts if facts["decision_bar_close_ms"] == close_ms]
    return found


def _said(caplog: pytest.LogCaptureFixture, action: str) -> list[logging.LogRecord]:
    """The runner's own log lines of one kind, oldest first."""
    return [record for record in caplog.records if getattr(record, "action", None) == action]


@pytest.fixture
def the_runner_is_heard(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="app.services.bot_trade_strategy")
    return caplog


def _a_send_that_takes_54_seconds(
    paper: PaperFloor, *, the_keeper_makes_its_rounds: bool = False, then_the_clock_is_set_back_ms: int = 0
) -> Callable[[], None]:
    """Alpaca takes fifty-four seconds to answer the send of an option order. Returns what ends that.

    ``the_keeper_makes_its_rounds``: the Clerk's deadline keeper makes a round each second of that time, as
    it does in production. Without it the keeper makes no round until the send has returned.
    ``then_the_clock_is_set_back_ms``: the machine's clock is set back by that much as the answer arrives."""
    send = paper.venue.submit_option_order

    async def slow(order: Any, *, client_order_id: str) -> Any:
        for _ in range(54):
            paper.clock.value += 1_000
            paper.repo.renew_execution_lease()
            if the_keeper_makes_its_rounds:
                await paper.keeper.tick()
        if then_the_clock_is_set_back_ms:
            paper.clock.value -= then_the_clock_is_set_back_ms
            paper.repo.renew_execution_lease()
        return await send(order, client_order_id=client_order_id)

    def answers_at_once_again() -> None:
        paper.venue.submit_option_order = send  # type: ignore[method-assign]

    paper.venue.submit_option_order = slow  # type: ignore[method-assign]
    return answers_at_once_again


def _alpaca_answers_nothing(paper: PaperFloor) -> None:
    paper.venue.cancel_error = BrokerUnavailable("cancel timed out", broker="alpaca")
    paper.venue.option_lookup_error = BrokerUnavailable("lookup timed out", broker="alpaca")


def _alpaca_answers_again(paper: PaperFloor) -> None:
    paper.venue.cancel_error = paper.venue.option_lookup_error = None


# ---- On Paper: the entry rests, and the strategy follows what it came to -----------------------------


async def test_on_paper_an_entry_that_fills_is_one_limit_order_at_the_ask_and_the_exit_sells_what_is_held(
    paper: PaperFloor,
) -> None:
    """The whole loop on a Paper account. One order goes to Alpaca for the entry, a limit to buy at the ask.
    It fills, the strategy is in, and three bars later the exit is one market order that sells the contract."""
    paper.while_it_rests(0, lambda: paper.alpaca_fills(1))
    held_meanwhile: dict[str, Any] = {}

    async def a_minute_on(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + MINUTE + DELIVERY_MS)
        held_meanwhile["position"] = floor.holds()

    script = _round_trip()
    script[2] = Step(script[2].bar, before=a_minute_on)

    ran = await paper.run(script)

    assert ran.outcomes() == [
        ("no_action", FIRST_GREEN_MS),
        ("enter_intent", ENTER_MS),
        ("no_action", ENTER_MS + MINUTE),
        ("no_action", ENTER_MS + 2 * MINUTE),
        ("exit_intent", EXIT_MS),
    ]
    assert paper.sent() == [(CALL, "buy", 1, "limit", "4.12"), (CALL, "sell", 1, "market", None)]
    assert held_meanwhile["position"] == {CALL: 1.0}
    assert paper.venue.cancel_calls == [], "a filled entry is never cancelled"
    settled = paper.settled()
    assert settled is not None and settled.recorded
    assert (settled.settlement, settled.standing) == (EntrySettlement.ENTERED, EntryStanding.FILLED)
    assert (settled.units_asked, settled.units_filled) == (1, 1)
    # The wait was asked to run no longer than fifty seconds after the decision bar closed.
    assert paper.waits_asked_until == [WAITED_UNTIL_MS]
    # Alpaca fills the sale, and the bot is flat.
    await paper.alpaca_fills(1, price="4.30")
    assert paper.holds() == {}
    assert [(fill[0], fill[1], fill[2], fill[3]) for fill in paper.fills()] == [
        (CALL, "buy", 1.0, 4.12), (CALL, "sell", 1.0, 4.3),
    ]


async def test_an_entry_that_rests_eight_seconds_and_then_fills_is_settled_as_it_fills(paper: PaperFloor) -> None:
    """Nothing is settled while the order only rests. The strategy is in from the fill, not from the send."""
    seen: dict[str, Any] = {}

    def look() -> None:
        seen["after five seconds"] = (paper.settled(), paper.entry().standing, paper.what_the_runner_has_done())

    paper.while_it_rests(5_000, look)
    paper.while_it_rests(8_000, lambda: paper.alpaca_fills(1))

    ran = await paper.run(_round_trip())

    recorded, standing, run = seen["after five seconds"]
    assert (recorded, standing) == (None, EntryStanding.WORKING)
    # The decision is still open: the runner has finished with the first bar only.
    assert run["finished"] == 1 and run["asked_of_the_clerk"] == ["ENTER"]
    settled = paper.settled()
    assert settled is not None
    assert (settled.settlement, settled.standing) == (EntrySettlement.ENTERED, EntryStanding.FILLED)
    assert TAKEN_MS + 8_000 <= settled.settled_at_ms <= TAKEN_MS + 8_400
    assert paper.venue.cancel_calls == []
    assert [outcome for outcome, _ in ran.outcomes()][-1] == "exit_intent", "the strategy was in, so it exits"


async def test_an_entry_nobody_fills_is_cancelled_at_its_deadline_and_the_next_signal_can_enter(
    paper: PaperFloor,
) -> None:
    """Twenty seconds after the Clerk took the entry it asks Alpaca to cancel it, once. A lookup shows it ended
    with nothing filled, and the decision is dropped as a refused one is: the strategy is not in. Its next
    green bar is an ENTER again, and that one fills."""
    paper.while_it_rests(0, lambda: paper.alpaca_fills(1), entry=1)

    ran = await paper.run([*_two_greens(), Step(green(ENTER_MS + MINUTE)), Step(red(ENTER_MS + 2 * MINUTE))])

    first = paper.settled(0)
    assert first is not None and first.recorded
    assert (first.settlement, first.standing) == (EntrySettlement.DISCARDED, EntryStanding.UNFILLED)
    assert (first.units_asked, first.units_filled) == (1, 0)
    assert TAKEN_MS + WAIT_MS <= first.settled_at_ms <= TAKEN_MS + WAIT_MS + 2_000
    # One cancel, of the first order, by Alpaca's own id for it.
    assert paper.venue.cancel_calls == [paper.venue.option_orders[paper.order_ref(0)]["id"]]
    # The strategy was never in: the bar after is a new ENTER, a second limit order, and that one is held.
    assert [outcome for outcome, _ in ran.outcomes()] == ["no_action", "enter_intent", "enter_intent", "no_action"]
    assert paper.sent() == [(CALL, "buy", 1, "limit", "4.12"), (CALL, "buy", 1, "limit", "4.12")]
    second = paper.settled(1)
    assert second is not None and second.settlement is EntrySettlement.ENTERED
    assert paper.holds() == {CALL: 1.0}
    # A later Start replays the first as discarded and the second as an entry.
    replayed = paper.replayed_dispositions().outcomes
    assert (replayed[_decision(ran, ENTER_MS)], replayed[_decision(ran, ENTER_MS + MINUTE)]) == (
        ENTRY_DISCARDED, "enter_intent",
    )
    # A bot in shares is not asked: its receipts are read as they are.
    assert paper.replayed_dispositions(option_entries=False).outcomes[_decision(ran, ENTER_MS)] == "enter_intent"


async def test_an_entry_that_fills_in_part_is_settled_as_entered_and_the_exit_sells_only_what_filled(
    on_paper: Callable[..., PaperFloor],
) -> None:
    """Two contracts asked for. One fills, and the rest is cancelled at the deadline. The bot holds one, the
    strategy is in, the held contract is asked for as a held quote line, and the exit sells one. The run
    reads what to ask for from the account's books, in one read."""
    paper = await on_paper(contracts=2).open()
    seen: dict[str, Any] = {}

    def look_while_it_rests() -> None:
        (asked,) = paper.watch.requests()
        seen["resting"] = (asked.plan is not None, asked.held, CALL in paper.ibkr.open_option_lines())

    async def a_minute_on(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + MINUTE + DELIVERY_MS)
        (asked,) = paper.watch.requests()
        seen["held"] = (paper.watched_as_held(), floor.holds())
        seen["window centred on"] = asked.money

    paper.while_it_rests(3_000, look_while_it_rests)
    paper.while_it_rests(5_000, lambda: paper.alpaca_fills(1))
    script = _round_trip()
    script[2] = Step(script[2].bar, before=a_minute_on)

    ran = await paper.run(script)

    settled = paper.settled()
    assert settled is not None and settled.recorded
    assert (settled.settlement, settled.standing) == (EntrySettlement.ENTERED, EntryStanding.PART_FILLED)
    assert (settled.units_asked, settled.units_filled) == (2, 1)
    assert len(paper.venue.cancel_calls) == 1, "what had not filled was cancelled, once"
    # The sale is of what is held, never of what was asked for.
    assert paper.sent() == [(CALL, "buy", 2, "limit", "4.12"), (CALL, "sell", 1, "market", None)]
    assert [outcome for outcome, _ in ran.outcomes()][-1] == "exit_intent"
    # While the entry rested the run's window was still asked for, and nothing was held yet.
    assert seen["resting"] == (True, (), True)
    # From the part fill on, the run asks for the contract it holds as a held line, before any further bar,
    # and its window stays where the last bar put it.
    assert seen["held"] == ((CALL_585,), {CALL: 1.0})
    assert seen["window centred on"] == Decimal("585.00")
    assert paper.replayed_dispositions().outcomes[_decision(ran, ENTER_MS)] == "enter_intent"


async def test_an_order_alpaca_rejects_is_a_refused_entry_and_the_next_signal_enters(
    paper: PaperFloor, the_runner_is_heard: pytest.LogCaptureFixture
) -> None:
    """Alpaca answers the send with a refusal. Nothing rests, so nothing is waited for: the decision is
    dropped at once, and the next green bar is an ENTER again. The Clerk had taken the entry first, so its
    receipt reads accepted: the runner says, once, that it came to a rejection and the code on the books."""
    paper.venue.option_submit_error = BrokerOrderRejected("insufficient options buying power", broker="alpaca")

    async def alpaca_takes_orders_again(floor: Floor) -> None:
        paper.venue.option_submit_error = None
        await floor.bar_arrives(ENTER_MS + MINUTE + DELIVERY_MS)

    paper.while_it_rests(0, lambda: paper.alpaca_fills(1))

    ran = await paper.run([*_two_greens(), Step(green(ENTER_MS + MINUTE), before=alpaca_takes_orders_again)])

    assert [outcome for outcome, _ in ran.outcomes()] == ["no_action", "enter_intent", "enter_intent"]
    # Only the second entry was waited for: a refusal is settled without asking.
    assert paper.waits_asked_until == [WAITED_UNTIL_MS + MINUTE]
    assert paper.entry(0).standing is EntryStanding.NOT_SENT and paper.settled(0) is None
    second = paper.settled(1)
    assert second is not None and second.settlement is EntrySettlement.ENTERED
    assert paper.holds() == {CALL: 1.0}
    assert paper.replayed_dispositions().outcomes[_decision(ran, ENTER_MS)] == ENTRY_DISCARDED
    (refused,) = _said(the_runner_is_heard, "bot_option_entry_refused_at_the_send")
    assert (refused.evaluation_id, refused.came_to) == (_decision(ran, ENTER_MS), EntryCameTo.REJECTED.value)
    assert refused.reason_code == "ORDER_SUBMIT_FAILED"
    # The entry that filled was settled, and is said in the same words.
    assert [said.came_to for said in _said(the_runner_is_heard, "bot_option_entry_settled")] == ["filled"]


async def test_an_order_alpaca_rejects_after_taking_it_reads_as_cancelled_unfilled_to_the_runner(
    paper: PaperFloor, the_runner_is_heard: pytest.LogCaptureFixture
) -> None:
    """Alpaca takes the order, and three seconds on ends it as rejected, with nothing filled. The Clerk's
    order row keeps Alpaca's own word. The entry's standing does not: it ended with nothing filled, like
    one cancelled at its deadline, and the standing is all the runner is told. So the runner's word for it
    is "cancelled unfilled", and the decision is dropped."""
    paper.while_it_rests(3_000, lambda: paper.venue.option_order_ends(paper.order_ref(0), "rejected"))

    ran = await paper.run([*_two_greens(), Step(red(ENTER_MS + MINUTE))])

    settled = paper.settled()
    assert settled is not None
    assert (settled.settlement, settled.standing) == (EntrySettlement.DISCARDED, EntryStanding.UNFILLED)
    order = paper.repo.order(paper.venue_entry_refs()[0])
    assert order is not None and order.broker_state == "rejected"
    (said,) = _said(the_runner_is_heard, "bot_option_entry_settled")
    assert said.came_to == EntryCameTo.CANCELLED_UNFILLED.value
    assert paper.replayed_dispositions().outcomes[_decision(ran, ENTER_MS)] == ENTRY_DISCARDED


async def test_the_read_of_the_entry_a_decision_made_says_where_it_stands_and_writes_nothing(
    paper: PaperFloor,
) -> None:
    """The read that waits for nothing, asked four ways. While the order rests: working, and the last thing
    on record is Alpaca's acknowledgement of the send. Once Alpaca has ended as rejected the order it took:
    unfilled. For an entry the Clerk took and Alpaca refused in its answer to the send: never sent, and the
    last thing on record is the failed send. For a decision that made no entry, and for the first decision
    asked under another bot's name: nothing. No read writes a row."""
    seen: dict[str, Any] = {}

    def rows_written() -> int:
        return paper.repo._conn.total_changes

    def look_while_it_rests() -> None:
        (enter,) = [facts["evaluation_id"] for outcome, facts in paper.receipts() if outcome == "enter_intent"]
        before = rows_written()
        seen["resting"] = paper.clerk.option_entry_of_decision(PAPER_SID, enter)
        seen["written while it rested"] = rows_written() - before

    async def alpaca_refuses_the_next_send(floor: Floor) -> None:
        paper.venue.option_submit_error = BrokerOrderRejected("insufficient options buying power", broker="alpaca")
        await floor.bar_arrives(ENTER_MS + MINUTE + DELIVERY_MS)

    paper.while_it_rests(1_000, look_while_it_rests)
    paper.while_it_rests(3_000, lambda: paper.venue.option_order_ends(paper.order_ref(0), "rejected"))

    ran = await paper.run([*_two_greens(), Step(green(ENTER_MS + MINUTE), before=alpaca_refuses_the_next_send)])

    first, second = _decision(ran, ENTER_MS), _decision(ran, ENTER_MS + MINUTE)
    before = rows_written()
    rejected_after_it_was_taken = paper.clerk.option_entry_of_decision(PAPER_SID, first)
    refused_at_the_send = paper.clerk.option_entry_of_decision(PAPER_SID, second)
    no_entry = paper.clerk.option_entry_of_decision(PAPER_SID, _decision(ran, FIRST_GREEN_MS))
    another_bots = paper.clerk.option_entry_of_decision("another-bot", first)
    assert (seen["written while it rested"], rows_written() - before) == (0, 0)
    resting = seen["resting"]
    assert (resting.standing, resting.last_recorded_code) == (EntryStanding.WORKING, "ORDER_SUBMIT_ACKED")
    assert rejected_after_it_was_taken.standing is EntryStanding.UNFILLED
    assert rejected_after_it_was_taken.order_ref == resting.order_ref
    assert (refused_at_the_send.standing, refused_at_the_send.last_recorded_code) == (
        EntryStanding.NOT_SENT, "ORDER_SUBMIT_FAILED",
    )
    assert refused_at_the_send.order_ref != resting.order_ref
    assert no_entry is None and another_bots is None


async def test_an_entry_still_unproven_when_its_wait_ends_is_settled_as_entered_and_no_second_entry_is_made(
    paper: PaperFloor, the_runner_is_heard: pytest.LogCaptureFixture
) -> None:
    """Alpaca stops answering: the cancel at the deadline fails and so does every lookup. Nothing proves what
    the order came to. Until the wait ends the decision stays open, and the run asks its stream for no
    further bar. Then the strategy is settled as entered, the cautious side, and that answer is recorded
    with where the entry stood. The next green bar is decided on that settled state: no second entry."""
    paper.venue.cancel_error = BrokerUnavailable("cancel timed out", broker="alpaca")
    paper.venue.option_lookup_error = BrokerUnavailable("lookup timed out", broker="alpaca")
    seen: dict[str, Any] = {}

    def look() -> None:
        seen["forty seconds in"] = (paper.settled(), dict(paper.asked_for_the_bar_after), paper.what_the_runner_has_done())

    paper.while_it_rests(40_000, look)

    ran = await paper.run([*_two_greens(), Step(green(ENTER_MS + MINUTE))])

    settled = paper.settled()
    assert settled is not None and settled.recorded
    assert (settled.settlement, settled.standing) == (EntrySettlement.ENTERED, EntryStanding.OUTCOME_UNKNOWN)
    assert (settled.units_filled, settled.until_ms) == (0, WAITED_UNTIL_MS)
    assert WAITED_UNTIL_MS <= settled.settled_at_ms < WAITED_UNTIL_MS + 400
    # Nobody knows where the order is. The runner does not call that resting.
    assert [said.came_to for said in _said(the_runner_is_heard, "bot_option_entry_settled")] == ["unproven"]
    # Forty seconds in nothing was settled, the decision was still open, and the stream had been asked for
    # no bar after the one decided on.
    recorded, asked_after, run = seen["forty seconds in"]
    assert recorded is None and ENTER_MS not in asked_after
    assert (run["finished"], run["outcomes"]) == (1, ["no_action", "enter_intent"])
    # The stream was asked for the next bar only once the entry was settled.
    then = paper.asked_for_the_bar_after[ENTER_MS]
    assert (then["finished"], then["settled"]) == (2, 1)
    # That bar was decided on the settled state: the strategy is in, so a third green bar is no second ENTER.
    assert ran.outcomes() == [
        ("no_action", FIRST_GREEN_MS), ("enter_intent", ENTER_MS), ("no_action", ENTER_MS + MINUTE),
    ]
    assert paper.asked_of_the_clerk == ["ENTER"] and len(paper.venue.option_submit_calls) == 1
    # Alpaca answers again and the cancel is proven: nothing filled. The recorded answer still stands.
    paper.venue.cancel_error = paper.venue.option_lookup_error = None
    await paper.keeper.tick()
    assert paper.entry().standing is EntryStanding.UNFILLED
    assert paper.replayed_dispositions().outcomes[_decision(ran, ENTER_MS)] == "enter_intent"


def test_the_wait_for_an_entry_ends_well_inside_the_bar_streams_stall_timer_and_before_the_next_bar_closes() -> None:
    """While the runner waits nothing reads its bar stream. A reader that sees no new bar for a minute is
    told its line has stalled, and the line is replaced for every bot on the symbol."""
    assert ENTRY_OUTCOME_WAIT_MS + 10_000 <= REALTIME_BAR_STALL_TIMEOUT_S * 1000
    assert ENTRY_OUTCOME_WAIT_MS < MINUTE
    # A limit asked a moment from now is still ahead when the Clerk first reads its books again, and is
    # reached by its second read: the runner is not kept from its stream for a third.
    between_two_reads_ms = clerk_runtime._ENTRY_OUTCOME_POLL_S * 1000
    assert between_two_reads_ms < ENTRY_OUTCOME_SHORTEST_WAIT_MS <= 2 * between_two_reads_ms


@pytest.mark.parametrize(
    ("the_keeper_made_its_rounds", "alpaca_answers", "came_to", "the_next_green_bar", "replayed_as"),
    [
        pytest.param(
            False, True, (EntrySettlement.DISCARDED, EntryStanding.UNFILLED), "enter_intent", ENTRY_DISCARDED,
            id="the keeper made no round while the send was out, so its cancel is proven in the last look",
        ),
        pytest.param(
            False, False, (EntrySettlement.ENTERED, EntryStanding.OUTCOME_UNKNOWN), "no_action", "enter_intent",
            id="alpaca answers nothing",
        ),
        pytest.param(
            True, True, (EntrySettlement.ENTERED, EntryStanding.WORKING), "no_action", "enter_intent",
            id="the keeper made its rounds while the send was out, and the order still rests",
        ),
    ],
)
async def test_an_entry_whose_send_outran_its_wait_is_still_settled_by_the_clerk_and_a_replay_agrees(
    paper: PaperFloor, the_keeper_made_its_rounds: bool, alpaca_answers: bool, came_to: tuple[Any, Any],
    the_next_green_bar: str, replayed_as: str,
) -> None:
    """Alpaca takes fifty-four seconds to answer the send, so the fifty the runner waits are over before it
    can ask what the entry came to. It asks all the same, with a limit a quarter of a second from now and
    not one already past. The Clerk looks again until that limit, judges at four tenths of a second, and
    records its answer, so the settlement is the Clerk's and never the runner's own. What the live run did
    on the next bar and what a later Start replays are then one answer.

    The first case, discarded, rests on the stand-in's own pacing: its keeper makes no round while the send
    is out, so its first round after it cancels at once and the cancel is proven inside the runner's short
    look. In production the keeper asks about the entry from its deadline on, while the send still holds the
    order, and no ask can act. More than ten seconds past the deadline it asks no more often than every five
    seconds, so when the send returns its next ask falls after the look. That is the last case, and the
    usual one: entered while the order still rests, which the keeper then cancels on a later round."""
    _a_send_that_takes_54_seconds(paper, the_keeper_makes_its_rounds=the_keeper_made_its_rounds)
    if not alpaca_answers:
        _alpaca_answers_nothing(paper)
    sent_by_ms = TAKEN_MS + 54_000

    ran = await paper.run([*_two_greens(), Step(green(ENTER_MS + MINUTE))])

    assert paper.waits_asked_until[0] == sent_by_ms + 250
    settled = paper.settled()
    assert settled is not None and settled.recorded
    assert (settled.settlement, settled.standing) == came_to
    assert sent_by_ms < settled.settled_at_ms <= sent_by_ms + 400
    # Live: in the strategy, the next green bar is no second ENTER; dropped, it is an ENTER again.
    assert ran.outcomes()[2] == (the_next_green_bar, ENTER_MS + MINUTE)
    assert len(paper.venue.option_submit_calls) == (2 if the_next_green_bar == "enter_intent" else 1)
    # Whatever is proven afterwards, a later Start replays the decision as the live run settled it.
    _alpaca_answers_again(paper)
    await paper.keeper.tick()
    assert paper.entry(0).standing is EntryStanding.UNFILLED
    assert paper.replayed_dispositions().outcomes[_decision(ran, ENTER_MS)] == replayed_as


async def test_a_clock_set_back_while_the_send_is_out_does_not_lengthen_the_wait(paper: PaperFloor) -> None:
    """The send takes fifty-four seconds, and the machine's clock is set back five minutes as its answer
    arrives. Counted from the bar's close alone, the limit would then be nearly five minutes ahead, with
    nothing reading the bot's bar stream meanwhile. The limit is never further from now than the wait
    itself. Alpaca answers nothing, so the wait runs its whole fifty seconds and no longer, and the Clerk
    records its answer."""
    _a_send_that_takes_54_seconds(paper, then_the_clock_is_set_back_ms=300_000)
    _alpaca_answers_nothing(paper)
    asked_at_ms = TAKEN_MS + 54_000 - 300_000

    ran = await paper.run(_two_greens())

    assert paper.waits_asked_until == [asked_at_ms + 50_000]
    settled = paper.settled()
    assert settled is not None and settled.recorded
    assert (settled.settlement, settled.standing) == (EntrySettlement.ENTERED, EntryStanding.OUTCOME_UNKNOWN)
    assert asked_at_ms + 50_000 <= settled.settled_at_ms < asked_at_ms + 50_400
    assert paper.replayed_dispositions().outcomes[_decision(ran, ENTER_MS)] == "enter_intent"


async def test_a_later_start_after_a_send_that_outran_its_wait_names_no_decision_the_live_run_never_staged(
    paper: PaperFloor,
) -> None:
    """The send outran the wait and Alpaca answered nothing more, so the Clerk settled the entry as entered
    while unproven. The live run was in from 10:31: the green bar at 10:32 was no action, and the process
    died as the green bar at 10:33 was retained. The Clerk came back, cancelled the order and proved it
    unfilled.

    The owner starts the bot again, through the trade runner. Its warm-up replays the 10:31 entry as the
    live run settled it, as an entry. So the green bar at 10:33 stages nothing, and no decision is written
    down as one a crash left undecided. Replayed from the books, as unfilled, that bar would stage an ENTER
    the live run never staged."""
    answers_at_once_again = _a_send_that_takes_54_seconds(paper)
    _alpaca_answers_nothing(paper)
    first = await paper.run([*_two_greens(), Step(green(ENTER_MS + MINUTE))])
    assert [outcome for outcome, _ in first.outcomes()] == ["no_action", "enter_intent", "no_action"]
    answers_at_once_again()
    _alpaca_answers_again(paper)
    paper.ledger().append(green(ENTER_MS + 2 * MINUTE), run_id=paper.binding.run_id)
    await paper.the_clerk_restarts()
    assert paper.entry().standing is EntryStanding.UNFILLED

    later = await paper.run([Step(red(EXIT_MS))], as_a_later_run="run-later")

    assert later.outcomes() == [
        ("no_action", FIRST_GREEN_MS), ("enter_intent", ENTER_MS), ("no_action", ENTER_MS + MINUTE),
        ("no_action", EXIT_MS),
    ]
    assert len(paper.venue.option_submit_calls) == 1


async def test_a_wait_the_clerk_refuses_ends_the_run_and_the_runner_settles_nothing_of_its_own(
    paper: PaperFloor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no moment kept, a send that outran the wait is asked about with a limit already past. The Clerk
    refuses that by name and records nothing. The runner does not make up an answer in its place: the run
    ends on the refusal."""
    monkeypatch.setattr(bot_option_run, "ENTRY_OUTCOME_SHORTEST_WAIT_MS", 0)
    _a_send_that_takes_54_seconds(paper)

    with pytest.raises(EntryOutcomeWaitRefused) as refused:
        await paper.run([*_two_greens(), Step(green(ENTER_MS + MINUTE))])

    assert refused.value.reason_code == ENTRY_OUTCOME_LIMIT_PAST
    assert paper.settled() is None
    assert [outcome for outcome, _ in paper.receipts()] == ["no_action", "enter_intent"]


async def test_an_accepted_entry_with_no_entry_on_the_books_ends_the_run(
    paper: PaperFloor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Clerk's receipt says it took the ENTER, and its books hold no option entry for that decision. The
    two contradict each other. The runner settles nothing on a guess: the run ends on the Clerk's own
    error, with nothing sent."""
    real = paper.clerk.execute_for_instance

    async def a_receipt_with_nothing_behind_it(**effect: Any) -> Any:
        refused = await real(**{**effect, "option_entry": None})
        return refused.model_copy(update={"state": EffectOperationState.SUBMITTED})

    monkeypatch.setattr(paper.clerk, "execute_for_instance", a_receipt_with_nothing_behind_it)

    with pytest.raises(LookupError, match="no entry in an option contract"):
        await paper.run([*_two_greens(), Step(green(ENTER_MS + MINUTE))])

    assert paper.venue.option_submit_calls == [] and paper.venue_entry_refs() == []
    assert paper.transitions("ENTRY_SETTLED") == 0


# ---- Settled as entered before it was proven: the exit clears it ---------------------------------------


@pytest.mark.parametrize(
    ("road", "stood_at_the_settle", "the_word"),
    [
        pytest.param("alpaca is silent, then answers", EntryStanding.OUTCOME_UNKNOWN, "unproven", id="silent, then proven unfilled"),
        pytest.param("it still rests at the exit", EntryStanding.CANCELLATION_REQUESTED, "resting", id="still resting at the exit"),
        pytest.param("it ends before the exit", EntryStanding.CANCELLATION_REQUESTED, "resting", id="ended unfilled before the exit"),
    ],
)
async def test_an_entry_settled_as_entered_before_it_was_proven_is_cleared_by_its_exit_and_the_next_signal_enters(
    paper: PaperFloor, the_runner_is_heard: pytest.LogCaptureFixture, road: str, stood_at_the_settle: EntryStanding,
    the_word: str,
) -> None:
    """The wait ends with the order's outcome unproven, so the strategy counts itself in. The order never
    fills. Three ways it goes from there, and one ending:

    - Alpaca answers nothing until 10:33, then shows the order cancelled;
    - Alpaca takes the cancel and goes on showing the order working, so it still rests when the exit comes;
    - the same, and the order ends by itself a minute before the exit.

    The strategy's own EXIT at 10:34 is what clears it. The Clerk cancels the entry if it still rests,
    proves that nothing was bought, and sends no sale. The strategy is flat again, and its next signal is
    a second limit order, sent only after the first had ended. A later Start replays every decision as
    the live run settled it."""
    if road == "alpaca is silent, then answers":
        _alpaca_answers_nothing(paper)
    else:
        paper.venue.cancel_is_slow = True
    seen: dict[str, Any] = {}

    async def a_minute_before_the_exit(floor: Floor) -> None:
        if road == "alpaca is silent, then answers":
            _alpaca_answers_again(paper)
        elif road == "it ends before the exit":
            paper.venue.cancel_is_slow = False
            paper.venue.option_order_ends(paper.order_ref(0))
        if road != "it still rests at the exit":
            # The keeper's next round finds out, with no runner waiting on it.
            await paper.keeper.tick()
        await floor.bar_arrives(ENTER_MS + 2 * MINUTE + DELIVERY_MS)

    async def the_exit_bar(floor: Floor) -> None:
        paper.venue.cancel_is_slow = False
        seen["at the exit"] = (paper.entry(0).standing, paper.holds(), paper.venue.option_orders[paper.order_ref(0)]["status"])
        seen["cancels asked before the exit"] = len(paper.venue.cancel_calls)
        await floor.bar_arrives(EXIT_MS + DELIVERY_MS)

    paper.while_it_rests(0, lambda: paper.alpaca_fills(1), entry=1)
    script = [
        *_two_greens(),
        Step(green(ENTER_MS + MINUTE)),
        Step(red(ENTER_MS + 2 * MINUTE), before=a_minute_before_the_exit),
        Step(red(EXIT_MS), before=the_exit_bar),
        Step(green(EXIT_MS + MINUTE)),
        Step(green(EXIT_MS + 2 * MINUTE)),
        Step(red(EXIT_MS + 3 * MINUTE)),
    ]

    ran = await paper.run(script)

    first = paper.settled(0)
    assert first is not None and first.recorded
    assert (first.settlement, first.standing, first.units_filled) == (EntrySettlement.ENTERED, stood_at_the_settle, 0)
    assert _said(the_runner_is_heard, "bot_option_entry_settled")[0].came_to == the_word
    # In the strategy from 10:31, though it holds nothing: the green bar at 10:32 is no second entry.
    lived = [outcome for outcome, _ in ran.outcomes()]
    assert lived == [
        "no_action", "enter_intent", "no_action", "no_action", "exit_intent", "no_action", "enter_intent", "no_action",
    ]
    stood, held, at_alpaca = seen["at the exit"]
    assert held == {}
    still_resting = road == "it still rests at the exit"
    assert (stood, at_alpaca) == (
        (EntryStanding.CANCELLATION_REQUESTED, "new") if still_resting else (EntryStanding.UNFILLED, "canceled")
    )
    # The exit cancels the entry when it still rests, and asks for no cancel of one that has ended.
    assert len(paper.venue.cancel_calls) - seen["cancels asked before the exit"] == (1 if still_resting else 0)
    assert paper.entry(0).standing is EntryStanding.UNFILLED
    # No sale was ever sent: there was nothing to sell. The second order is the next signal's entry.
    assert paper.asked_of_the_clerk == ["ENTER", "EXIT", "ENTER"]
    assert paper.sent() == [(CALL, "buy", 1, "limit", "4.12"), (CALL, "buy", 1, "limit", "4.12")]
    assert paper.holds() == {CALL: 1.0}
    # A later Start replays what the live run lived, decision for decision.
    replayed = paper.replayed_dispositions().outcomes
    assert [replayed[facts["evaluation_id"]] for _, facts in ran.receipts] == lived


async def test_an_entry_settled_as_entered_while_its_cancel_was_unproven_and_then_filled_is_sold_by_its_exit(
    paper: PaperFloor,
) -> None:
    """Alpaca takes the cancel at the deadline and goes on showing the order working, so the wait ends with
    the cancel unproven and the strategy counts itself in. Alpaca then fills the order after all, at
    10:32:30. The bot holds one contract, and the strategy's EXIT at 10:34 sells it: the one road after an
    unproven settle on which money moves. A later Start replays every decision as the live run settled it."""
    paper.venue.cancel_is_slow = True
    seen: dict[str, Any] = {}

    async def it_fills_after_all(floor: Floor) -> None:
        await floor.time_passes_to(ENTER_MS + MINUTE + 30_000)
        await paper.alpaca_fills(1)
        await floor.bar_arrives(ENTER_MS + 2 * MINUTE + DELIVERY_MS)

    async def the_exit_bar(floor: Floor) -> None:
        seen["held at the exit"] = paper.holds()
        await floor.bar_arrives(EXIT_MS + DELIVERY_MS)

    script = _round_trip()
    script[3], script[4] = Step(script[3].bar, before=it_fills_after_all), Step(script[4].bar, before=the_exit_bar)

    ran = await paper.run(script)

    settled = paper.settled()
    assert settled is not None and settled.recorded
    assert (settled.settlement, settled.standing, settled.units_filled) == (
        EntrySettlement.ENTERED, EntryStanding.CANCELLATION_REQUESTED, 0,
    )
    lived = [outcome for outcome, _ in ran.outcomes()]
    assert lived == ["no_action", "enter_intent", "no_action", "no_action", "exit_intent"]
    assert seen["held at the exit"] == {CALL: 1.0}
    assert paper.sent() == [(CALL, "buy", 1, "limit", "4.12"), (CALL, "sell", 1, "market", None)]
    replayed = paper.replayed_dispositions().outcomes
    assert [replayed[facts["evaluation_id"]] for _, facts in ran.receipts] == lived


async def test_no_second_entry_is_sent_while_an_exit_has_not_proven_what_the_first_came_to(
    paper: PaperFloor,
) -> None:
    """The cancel at the deadline is slow, so the entry still rests when the EXIT comes, and from that bar
    on Alpaca answers nothing. The EXIT cannot prove what the entry came to, and stays unfinished. Two green
    bars follow. The strategy's next ENTER is refused by the Clerk while that EXIT is unfinished: one order
    in all has gone to Alpaca, and no second buy stands beside an order nobody can place."""
    paper.venue.cancel_is_slow = True

    async def alpaca_goes_silent(floor: Floor) -> None:
        _alpaca_answers_nothing(paper)
        await floor.bar_arrives(EXIT_MS + DELIVERY_MS)

    ran = await paper.run(
        [
            *_two_greens(), Step(green(ENTER_MS + MINUTE)), Step(red(ENTER_MS + 2 * MINUTE)),
            Step(red(EXIT_MS), before=alpaca_goes_silent),
            Step(green(EXIT_MS + MINUTE)), Step(green(EXIT_MS + 2 * MINUTE)), Step(red(EXIT_MS + 3 * MINUTE)),
        ]
    )

    assert [outcome for outcome, _ in ran.outcomes()] == [
        "no_action", "enter_intent", "no_action", "no_action", "exit_intent", "no_action", "blocked", "no_action",
    ]
    assert [code for code, _ in ran.blocked()] == ["EXIT_IN_PROGRESS"]
    assert paper.asked_of_the_clerk == ["ENTER", "EXIT", "ENTER"]
    assert len(paper.venue.option_submit_calls) == 1 and paper.holds() == {}


# ---- The run ends while an entry rests ---------------------------------------------------------------


async def test_a_run_stopped_while_its_entry_rests_is_answered_at_once_and_nothing_is_recorded(
    paper: PaperFloor,
) -> None:
    """The owner presses Stop five seconds into the rest. The wait ends at the next read of the books, long
    before the entry's deadline, and the Clerk records no settlement. The pass that follows cancels the
    stopped run's order, and a later Start reads from the books that nothing was bought."""
    paper.while_it_rests(5_000, paper.stop_is_pressed)

    ran = await paper.run(_two_greens())

    assert paper.settled() is None
    assert paper.clock.value < TAKEN_MS + 6_000, "the wait did not run on to the entry's deadline"
    assert paper.entry().standing is EntryStanding.WORKING
    await paper.a_pass()
    assert paper.entry().standing is EntryStanding.UNFILLED and paper.holds() == {}
    assert paper.replayed_dispositions().outcomes[_decision(ran, ENTER_MS)] == ENTRY_DISCARDED


async def test_a_runner_cancelled_while_its_entry_rests_leaves_the_cancel_at_the_deadline_to_the_clerk(
    paper: PaperFloor,
) -> None:
    """The runner's task is cancelled mid-wait. The cancel goes through at once. The order still rests, and
    the Clerk's keeper cancels it at its deadline with no runner alive."""
    running = asyncio.ensure_future(paper.running_the_bot(_two_greens()))
    paper.while_it_rests(5_000, running.cancel)

    with pytest.raises(asyncio.CancelledError):
        await running

    assert paper.settled() is None and paper.entry().standing is EntryStanding.WORKING
    assert paper.venue.cancel_calls == []
    await paper.time_passes_to(TAKEN_MS + WAIT_MS)
    await paper.keeper.tick()
    assert len(paper.venue.cancel_calls) == 1 and paper.entry().standing is EntryStanding.UNFILLED


@pytest.mark.parametrize(
    ("before_the_crash", "alpaca_answers", "proven_as", "replayed_as", "holds"),
    [
        pytest.param(None, True, EntryStanding.UNFILLED, ENTRY_DISCARDED, {}, id="nothing had filled"),
        pytest.param(1, True, EntryStanding.PART_FILLED, "enter_intent", {CALL: 1.0}, id="one of two had filled"),
        pytest.param(None, False, None, "enter_intent", {}, id="alpaca does not answer"),
    ],
)
async def test_after_a_restart_while_an_entry_rested_a_later_start_settles_it_from_the_books(
    on_paper: Callable[..., PaperFloor],
    before_the_crash: int | None,
    alpaca_answers: bool,
    proven_as: EntryStanding | None,
    replayed_as: str,
    holds: dict[str, float],
) -> None:
    """The process dies five seconds into the rest. The Clerk comes back, stops the run and cancels its
    order. No settlement was recorded, so a later Start settles the decision by the one rule, on the books
    as they stand: dropped when nothing was bought, entered when something was, entered while nobody knows."""
    paper = await on_paper(contracts=2).open()
    running = asyncio.ensure_future(paper.running_the_bot(_two_greens()))
    if before_the_crash is not None:
        paper.while_it_rests(3_000, lambda: paper.alpaca_fills(before_the_crash))
    paper.while_it_rests(5_000, running.cancel)
    with pytest.raises(asyncio.CancelledError):
        await running
    if not alpaca_answers:
        paper.venue.cancel_error = BrokerUnavailable("cancel timed out", broker="alpaca")
        paper.venue.option_lookup_error = BrokerUnavailable("lookup timed out", broker="alpaca")

    await paper.the_clerk_restarts()

    assert paper.repo.active_run(PAPER_SID) is None, "no entry outlives its run"
    assert paper.settled() is None
    stands = paper.entry()
    assert (stands.standing if stands.proven else None, paper.holds()) == (proven_as, holds)
    (enter,) = [facts["evaluation_id"] for outcome, facts in paper.receipts() if outcome == "enter_intent"]
    assert paper.replayed_dispositions().outcomes[enter] == replayed_as


class _Retained:
    """A feed that has only what an earlier run retained: its bars for a warm-up, and no live bar."""

    feed_id = "fake"

    def __init__(self, bars: list[MarketDataBar]) -> None:
        self._bars = bars

    async def stream_bars(self, symbol: str, **_how: Any) -> AsyncIterator[MarketDataBar]:
        del symbol
        for bar in ():
            yield bar

    async def recent_closed_bars(self, symbol: str, **_how: Any) -> list[MarketDataBar]:
        del symbol
        return list(self._bars)

    def health(self, _symbol: str | None = None) -> FeedHealth:
        return FeedHealth(
            connected=True, stale=False, last_bar_ms=None, reason="", active_subscription_count=0,
            observed_at_ms=timestamps.now_ms_utc(),
        )


@pytest.mark.parametrize(
    ("option_entries", "named"),
    [
        pytest.param(True, [], id="settled as it ended"),
        pytest.param(False, [("EXIT", EXIT_MS)], id="the old rule, entered on acceptance"),
    ],
)
async def test_a_later_start_names_no_exit_of_a_position_an_unfilled_entry_never_opened(
    paper: PaperFloor, option_entries: bool, named: list[tuple[str, int]]
) -> None:
    """The first run's entry ended unfilled and the bot went on, flat, for two more bars. It then died as the
    bar that closes at 10:34 was delivered, before deciding on it.

    A later Start replays those bars. Replayed as entered, the unfilled ENTER would put the strategy in a
    position at 10:31 and stage its EXIT at 10:34, and that EXIT would be written down as a decision a crash
    left undecided: evidence of a position the bot never held. Replayed as it ended, nothing is named."""
    first_run = [*_two_greens(), Step(red(ENTER_MS + MINUTE)), Step(red(ENTER_MS + 2 * MINUTE))]
    await paper.run(first_run)
    assert paper.entry().standing is EntryStanding.UNFILLED
    retained = _Retained([*(step.bar for step in first_run), red(EXIT_MS)])
    later = paper.binding.model_copy(update={"run_id": "run-later"})

    replayed = [
        evaluation
        async for evaluation in bot_trade_strategy.strategy_evaluations(
            later, retained, captured_decisions=paper.replayed_dispositions(run_id="run-later", option_entries=option_entries)
        )
    ]

    assert [
        (evaluation.intents[0].kind.value, evaluation.decision_bar_close_ms)
        for evaluation in replayed
        if evaluation.crash_recovered
    ] == named


async def test_a_later_run_on_paper_writes_down_no_exit_of_a_position_its_unfilled_entry_never_opened(
    paper: PaperFloor,
) -> None:
    """The same, through the trade runner itself. The first run's entry ended unfilled, it decided two more
    bars flat, and the 10:34 bar was retained as the process died. The owner starts the bot again. The new
    run warms up on what the first retained, and names no crash evidence."""
    await paper.run([*_two_greens(), Step(red(ENTER_MS + MINUTE)), Step(red(ENTER_MS + 2 * MINUTE))])
    assert paper.entry().standing is EntryStanding.UNFILLED
    paper.ledger().append(red(EXIT_MS), run_id=paper.binding.run_id)
    await paper.the_clerk_restarts()

    ran = await paper.run([Step(red(EXIT_MS + MINUTE))], as_a_later_run="run-later")

    assert [outcome for outcome, _ in ran.outcomes()] == [
        "no_action", "enter_intent", "no_action", "no_action", "no_action",
    ]
    assert ran.outcomes()[-1] == ("no_action", EXIT_MS + MINUTE)


async def test_a_later_dry_run_writes_down_no_exit_of_a_position_an_entry_refused_at_the_send_never_opened(
    dry_run: Floor, monkeypatch: pytest.MonkeyPatch, the_runner_is_heard: pytest.LogCaptureFixture
) -> None:
    """A Dry Run's Clerk took the entry and then refused it as its order was sent: its quote had grown old.
    The receipt reads accepted, and the run went on flat. The runner says once what the entry came to, with
    the code the books recorded. A later run of the Dry Run replays that entry as discarded too."""
    take = clerk_runtime.accept_option_enter

    def take_and_stand_still(*args: Any, **kwargs: Any) -> Any:
        taken = take(*args, **kwargs)
        dry_run.clock.value += 5_001
        return taken

    monkeypatch.setattr(clerk_runtime, "accept_option_enter", take_and_stand_still)
    await dry_run.run([*_two_greens(), Step(red(ENTER_MS + MINUTE)), Step(red(ENTER_MS + 2 * MINUTE))])
    assert dry_run.transitions("ENTER_SUBMISSION_REFUSED") == 1 and dry_run.holds() == {}
    on_the_books = dry_run.repo.last_strategy_transition(
        strategy_instance_id=SID, transition_kind="ENTER_SUBMISSION_REFUSED"
    )
    (refused,) = _said(the_runner_is_heard, "bot_option_entry_refused_at_the_send")
    assert (refused.came_to, refused.reason_code) == (EntryCameTo.REJECTED.value, on_the_books["summary_code"])
    assert _said(the_runner_is_heard, "bot_option_entry_settled") == [], "nothing was asked about its outcome"
    # Said once, by the line that carries its word: the journal's writer is not asked about a refused decision.
    assert _said(the_runner_is_heard, "dry_run_option_entry_not_filled") == []
    retained = dry_run.authority.source_bars()
    try:
        retained.append(red(EXIT_MS), run_id=dry_run.binding.run_id)
    finally:
        retained.close()
    dry_run.binding = dry_run.binding.model_copy(update={"run_id": "run-later"})
    dry_run._evaluations_finished = 0

    ran = await dry_run.run([Step(red(EXIT_MS + MINUTE))])

    assert [outcome for outcome, _ in ran.outcomes()] == [
        "no_action", "enter_intent", "no_action", "no_action", "no_action",
    ]


@pytest.mark.parametrize(
    ("alpaca_answers_in_time", "held"),
    [
        pytest.param(False, True, id="settled as entered before it was proven, and unfilled in the end"),
        pytest.param(True, False, id="cancelled unfilled at its deadline"),
    ],
)
async def test_the_entry_a_later_start_replays_as_held_is_the_one_the_strategy_was_settled_as_in(
    on_paper: Callable[..., PaperFloor], monkeypatch: pytest.MonkeyPatch, alpaca_answers_in_time: bool, held: bool
) -> None:
    """The ENTER is older than the receipts a Start still reads. Whether its position is replayed as open is
    asked of its settlement. Settled as entered while Alpaca would not answer, the live run went on as in a
    position, though the order was proven unfilled in the end and its ENTER then ended failed: it is still
    replayed as held. Cancelled unfilled inside the wait, the live run went on flat: nothing is held."""
    monkeypatch.setattr(decision_receipts_module, "MAX_DECISION_RECEIPTS_PER_STRATEGY", 2)
    paper = await on_paper().open()
    if not alpaca_answers_in_time:
        paper.venue.cancel_error = BrokerUnavailable("cancel timed out", broker="alpaca")
        paper.venue.option_lookup_error = BrokerUnavailable("lookup timed out", broker="alpaca")

    ran = await paper.run([*_two_greens(), Step(red(ENTER_MS + MINUTE)), Step(red(ENTER_MS + 2 * MINUTE))])

    paper.venue.cancel_error = paper.venue.option_lookup_error = None
    await paper.keeper.tick()
    assert paper.entry().standing is EntryStanding.UNFILLED and paper.holds() == {}
    receipts = SqliteDecisionReceipts(paper.repo, strategy_instance_id=PAPER_SID)
    window = receipts.retained_window()
    assert [receipt.outcome for receipt in window] == ["no_action", "no_action"], "the ENTER is older than the read"

    entry = receipts.entry_held_into(window)

    assert (entry is not None) is held
    replayed = paper.replayed_dispositions().outcomes
    assert replayed.get(_decision(ran, ENTER_MS)) == ("enter_intent" if held else None)


# ---- The bot's own order terms -----------------------------------------------------------------------


async def test_the_bots_own_order_terms_reach_the_clerk_and_set_when_it_cancels(
    on_paper: Callable[..., PaperFloor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bot whose entries wait seven seconds. The Clerk keeps those terms with the entry, and cancels it
    seven seconds after it took it, not the twenty a Deploy starts from."""
    terms = CloseTerms(entry_wait_ms=7_000)
    monkeypatch.setattr(bot_option_run, "option_order_terms", lambda _binding: OptionOrderTerms(close=terms))
    paper = await on_paper().open()

    await paper.run(_two_greens())

    entry = paper.entry_as_the_clerk_kept_it()
    assert entry.terms is not None and entry.terms.close_terms == terms
    assert entry.cancel_at_ms == TAKEN_MS + 7_000
    settled = paper.settled()
    assert settled is not None and settled.standing is EntryStanding.UNFILLED
    assert TAKEN_MS + 7_000 <= settled.settled_at_ms <= TAKEN_MS + 9_000


# The guard's numbers are written out in these tests, never worked out from the constants: the runner waits
# fifty seconds, an entry waits twenty or thirty, seven are kept to prove a cancel, and the Clerk's read of
# Alpaca's listing may take three.
THIRTY_SECOND_WAIT_MS = 30_000


def _the_bot_waits(monkeypatch: pytest.MonkeyPatch, entry_wait_ms: int) -> None:
    terms = CloseTerms(entry_wait_ms=entry_wait_ms)
    monkeypatch.setattr(bot_option_run, "option_order_terms", lambda _binding: OptionOrderTerms(close=terms))


@pytest.mark.parametrize(
    ("entry_wait_ms", "starts"),
    [
        pytest.param(39_999, True, id="a millisecond is left to take an entry in"),
        pytest.param(40_000, False, id="no moment is left"),
        pytest.param(40_001, False, id="a millisecond over"),
    ],
)
async def test_a_bot_whose_entry_wait_can_never_be_seen_through_is_refused_as_its_run_starts(
    paper: PaperFloor, monkeypatch: pytest.MonkeyPatch, entry_wait_ms: int, starts: bool
) -> None:
    """The runner waits fifty seconds for what an entry came to. Of those it keeps seven to prove a cancel
    and three for the Clerk's read of the listing: 50 - 7 - 3 = 40. An entry wait of forty seconds leaves no
    moment after a bar's close at which an entry could still be taken, so every entry would be refused. The
    run is refused instead, once, by name, before it asks for a quote line. A millisecond shorter, it starts."""
    _the_bot_waits(monkeypatch, entry_wait_ms)
    run = OptionRun(paper.binding, paper.market(), held_contracts=tuple, contracts_to_watch=tuple)

    if starts:
        async with run:
            assert len(paper.watch.requests()) == 1
        return
    with pytest.raises(OptionPlanRefused) as refused:
        async with run:
            pytest.fail("the run started")
    assert refused.value.reason is OptionEffectRefusal.ENTRY_WAIT_OVER_OUTCOME_WAIT
    assert paper.watch.requests() == ()


@pytest.mark.parametrize(
    ("taken_after_ms", "taken"),
    [
        pytest.param(9_999, True, id="a millisecond inside"),
        pytest.param(10_000, True, id="the last moment"),
        pytest.param(10_001, False, id="a millisecond late"),
    ],
)
async def test_an_entry_taken_too_late_for_its_cancel_to_be_proven_in_the_wait_is_refused_and_nothing_is_sent(
    on_paper: Callable[..., PaperFloor], monkeypatch: pytest.MonkeyPatch, taken_after_ms: int, taken: bool
) -> None:
    """A bot whose entries wait thirty seconds: 50 - 30 - 7 - 3 = 10. Its bar reaches it ten seconds after
    it closed, which is on time. Taken then, the order is cancelled at forty seconds and proven ended inside
    the fifty the runner waits. A millisecond later it could still be resting when the runner had to settle,
    so the entry is refused by name: one blocked decision, the Clerk is not asked, and nothing is sent."""
    _the_bot_waits(monkeypatch, THIRTY_SECOND_WAIT_MS)
    paper = await on_paper().open()

    async def late_but_on_time(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + taken_after_ms)

    ran = await paper.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS), before=late_but_on_time)])

    if not taken:
        assert [code for code, _ in ran.blocked()] == [OptionEntryRefusal.TAKEN_TOO_LATE.value]
        assert [outcome for outcome, _ in ran.outcomes()] == ["no_action", "blocked"]
        assert paper.asked_of_the_clerk == [] and paper.sent() == [] and paper.venue_entry_refs() == []
        return
    assert ran.blocked() == [] and paper.sent() == [(CALL, "buy", 1, "limit", "4.12")]
    # What the guard keeps room for: nobody filled it, and its cancel was proven before the wait ended.
    settled = paper.settled()
    assert settled is not None
    assert (settled.settlement, settled.standing) == (EntrySettlement.DISCARDED, EntryStanding.UNFILLED)
    assert paper.entry_as_the_clerk_kept_it().cancel_at_ms == ENTER_MS + taken_after_ms + THIRTY_SECOND_WAIT_MS
    assert settled.settled_at_ms <= WAITED_UNTIL_MS


@pytest.mark.parametrize(
    ("taken_after_ms", "taken"),
    [
        pytest.param(0, True, id="as its bar closes"),
        pytest.param(20_000, True, id="the last moment a decision is on time"),
        pytest.param(20_001, False, id="a millisecond after it"),
    ],
)
def test_under_the_terms_a_deploy_starts_from_an_entry_may_be_taken_for_as_long_as_its_decision_is_on_time(
    paper: PaperFloor, taken_after_ms: int, taken: bool
) -> None:
    """The numbers the guard stands on, each as itself. The runner waits fifty seconds for what an entry
    came to. An entry waits twenty for its fill, seven are kept to prove a cancel, and the Clerk's read of
    Alpaca's listing may take three: 50 - 20 - 7 - 3 = 20. A decision is on time for those same twenty
    seconds, and lateness is asked first. So the guard refuses no entry the lateness gate lets through."""
    assert (ENTRY_OUTCOME_WAIT_MS, WAIT_MS, CANCEL_PROOF_ROOM_MS, CONTRACT_REFERENCE_READ_BOUND_MS) == (
        50_000, 20_000, 7_000, 3_000,
    )
    assert DELIVERY_ALLOWANCE_MS == 20_000
    run = OptionRun(
        paper.binding, paper.market(clock=lambda: ENTER_MS + taken_after_ms), held_contracts=tuple,
        contracts_to_watch=tuple,
    )

    assert (run.too_late_to_see_through(ENTER_MS) is None) is taken


async def test_a_decision_both_late_and_too_late_to_see_through_is_refused_as_late(dry_run: Floor) -> None:
    """The bar reached the bot 15.2 seconds after its close, on time, and the search took 4.9 more. At 20.1
    seconds the decision is late, and it is also past the last moment its entry could be seen through.
    Lateness is asked first, so the decision is refused as every late decision is, whatever the bot trades."""

    async def late_but_on_time(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + 15_200)

    def the_search_takes_a_while() -> None:
        dry_run.clock.value += 4_900

    dry_run.worker.during = the_search_takes_a_while

    ran = await dry_run.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS), before=late_but_on_time)])

    assert [code for code, _ in ran.blocked()] == [DECISION_LATE_REASON_CODE]
    assert dry_run.asked_of_the_clerk == []
    # Asked by itself at that moment, the other check refuses too: what is held here is which speaks first.
    at_that_moment = OptionRun(
        dry_run.binding, dry_run.market(clock=lambda: ENTER_MS + 20_100), held_contracts=tuple,
        contracts_to_watch=tuple,
    )
    too_late = at_that_moment.too_late_to_see_through(ENTER_MS)
    assert too_late is not None and too_late.reason_code == OptionEntryRefusal.TAKEN_TOO_LATE.value


@pytest.mark.parametrize(
    ("the_listing_takes_ms", "the_clerk_is_held_up_ms", "cancel_shown_ended_after_ms", "left_ms"),
    [
        pytest.param(2_900, 0, 6_000, 7_100, id="the listing takes all but a tenth of its three seconds"),
        pytest.param(0, 4_900, 4_000, 5_100, id="the Clerk takes the entry 4.9 seconds after the runner looked"),
        pytest.param(0, 5_100, None, None, id="held up 5.1 seconds, the entry's quotes are too old to take it"),
    ],
)
async def test_what_is_left_to_prove_a_cancel_for_an_entry_taken_at_the_last_moment(
    on_paper: Callable[..., PaperFloor], monkeypatch: pytest.MonkeyPatch, the_listing_takes_ms: int,
    the_clerk_is_held_up_ms: int, cancel_shown_ended_after_ms: int | None, left_ms: int | None,
) -> None:
    """A thirty-second entry wait, and a bar that reaches the bot ten seconds after its close: the last
    moment the guard allows. The Clerk sets the cancel's time when it takes the entry, which is later.

    - Alpaca's listing takes 2.9 of the three seconds counted for it. The cancel is due 7.1 seconds before
      the runner stops waiting. Alpaca shows the order ended six seconds after its cancel was due, and that
      is proven inside the wait.
    - What is not counted. The Clerk takes the entry 4.9 seconds after the runner looked at the clock, as
      it can when an entry first waits for a newer account reading. That hold-up is put in by hand here: the
      refusal that causes it is not staged. 5.1 seconds are then left, not seven. An order shown ended four
      seconds after its cancel was due is still proven inside the wait.
    - Held up longer than the five seconds its quotes are good for, the entry is not taken at all. So five
      seconds is the most that can come between the runner's look and the Clerk's taking of the entry."""
    assert QUOTE_MAX_AGE_MS == 5_000
    _the_bot_waits(monkeypatch, THIRTY_SECOND_WAIT_MS)
    paper = await on_paper().open()
    read_the_listing, take = paper.account.get_option_contract, paper.clerk.execute_for_instance

    async def a_slow_listing(contract: OptionContract) -> Any:
        paper.clock.value += the_listing_takes_ms
        paper.repo.renew_execution_lease()
        return await read_the_listing(contract)

    async def taken_later(**effect: Any) -> Any:
        paper.clock.value += the_clerk_is_held_up_ms
        paper.repo.renew_execution_lease()
        return await take(**effect)

    monkeypatch.setattr(paper.account, "get_option_contract", a_slow_listing)
    monkeypatch.setattr(paper.clerk, "execute_for_instance", taken_later)
    paper.venue.cancel_is_slow = True

    def alpaca_shows_the_order_ended() -> None:
        paper.venue.cancel_is_slow = False
        paper.venue.option_order_ends(paper.order_ref(0))

    async def at_the_last_moment(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + 10_000)

    if cancel_shown_ended_after_ms is not None:
        # Counted from the Clerk's taking of the entry: its thirty seconds, then Alpaca's delay.
        paper.while_it_rests(THIRTY_SECOND_WAIT_MS + cancel_shown_ended_after_ms, alpaca_shows_the_order_ended)

    ran = await paper.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS), before=at_the_last_moment)])

    if left_ms is None:
        assert [code for code, _ in ran.blocked()] == [QuoteFault.STALE.value]
        assert paper.sent() == [] and paper.venue_entry_refs() == []
        return
    taken_at_ms = ENTER_MS + 10_000 + the_listing_takes_ms + the_clerk_is_held_up_ms
    cancel_at_ms = paper.entry_as_the_clerk_kept_it().cancel_at_ms
    assert cancel_at_ms == taken_at_ms + THIRTY_SECOND_WAIT_MS
    assert WAITED_UNTIL_MS - cancel_at_ms == left_ms
    settled = paper.settled()
    assert settled is not None and settled.recorded
    assert (settled.settlement, settled.standing) == (EntrySettlement.DISCARDED, EntryStanding.UNFILLED)
    assert cancel_at_ms + cancel_shown_ended_after_ms <= settled.settled_at_ms <= WAITED_UNTIL_MS


async def test_a_dry_runs_entry_is_handed_the_bots_own_order_terms_too(
    on_the_floor: Callable[..., Floor], monkeypatch: pytest.MonkeyPatch
) -> None:
    terms = CloseTerms(entry_wait_ms=7_000, entry_margin_ms=45_000)
    monkeypatch.setattr(bot_option_run, "option_order_terms", lambda _binding: OptionOrderTerms(close=terms))
    floor = await on_the_floor().open()

    await floor.run(_two_greens())

    entry = floor.entry_as_the_clerk_kept_it()
    assert entry.terms is not None and entry.terms.close_terms == terms
    assert entry.cancel_at_ms == TAKEN_MS + 7_000


@pytest.mark.parametrize(
    ("asked_at", "named"),
    [
        pytest.param(session_close_ms_utc(FRIDAY) - 1, (CALL_585,), id="in its last session"),
        pytest.param(session_close_ms_utc(FRIDAY), (), id="once its last session has closed"),
    ],
)
async def test_a_paper_run_asks_for_a_held_contract_on_its_underlying_until_its_last_session_closes(
    paper: PaperFloor, asked_at: int, named: tuple[Any, ...]
) -> None:
    """The account's books name a SPY call and a QQQ call. The run of a bot on SPY asks for the SPY call as a
    held line: the other is another underlying's. Nothing trades in a contract after its last session, so
    no quote line is spent on it then."""
    another_underlyings = OptionContract.of(root="QQQ", expiry_date=FRIDAY, right="call", strike=585).occ_symbol
    run = OptionRun(
        paper.binding, paper.market(clock=lambda: asked_at), held_contracts=tuple,
        contracts_to_watch=lambda: (CALL, another_underlyings),
    )

    async with run:
        assert paper.watched_as_held() == named

    assert paper.watch.requests() == ()


# ---- A real account's close needs no quote --------------------------------------------------------------


@pytest.mark.parametrize(
    "at_the_exit",
    ["this process has no option quote source", "the held contract's quote is three minutes old", "reading the quotes fails"],
)
async def test_on_paper_the_exit_is_sent_whatever_has_become_of_the_quotes_and_the_run_goes_on(
    paper: PaperFloor, at_the_exit: str
) -> None:
    """A Paper account's close is a market order. No quote prices it, so the runner reads none and hands
    none over: a quote source that is gone, a quote that is old, and a read that fails each leave the sale
    exactly as it was, and the run decides its next bar."""
    paper.while_it_rests(0, lambda: paper.alpaca_fills(1))
    quotes_stop_after_the_entry = at_the_exit == "the held contract's quote is three minutes old"

    def a_bar(close_ms: int) -> Step:
        async def arrives(floor: Floor) -> None:
            await floor.bar_arrives(close_ms + DELIVERY_MS, quotes=not quotes_stop_after_the_entry)
            if close_ms == EXIT_MS and at_the_exit == "this process has no option quote source":
                paper.quote_source = lambda: None
            if close_ms == EXIT_MS and at_the_exit == "reading the quotes fails":
                paper.quote_source = _a_read_that_fails

        return Step(red(close_ms), before=arrives)

    ran = await paper.run([*_two_greens(), *(a_bar(ENTER_MS + minutes * MINUTE) for minutes in (1, 2, 3, 4))])

    assert ran.outcomes()[-2:] == [("exit_intent", EXIT_MS), ("no_action", EXIT_MS + MINUTE)]
    assert paper.sent() == [(CALL, "buy", 1, "limit", "4.12"), (CALL, "sell", 1, "market", None)]


def _a_read_that_fails() -> Any:
    raise RuntimeError("the supervisor's read failed")


# ---- A bot that listens to one symbol and trades options on another ------------------------------------


async def test_on_paper_options_on_another_symbol_take_the_money_and_the_window_from_that_symbols_own_bars(
    on_paper: Callable[..., PaperFloor],
) -> None:
    """The bot listens to QQQ and buys SPY calls on a Paper account. QQQ's bar decides. SPY's bar for the same
    minute, on the run's second stream, is the money the strike is counted from, and each SPY bar moves the
    window the run asks to have quoted: no bar of QQQ could."""
    paper = await on_paper(plan=one_call(underlying="SPY"), listens_to="QQQ").open()
    paper.while_it_rests(0, lambda: paper.alpaca_fills(1))
    seen: dict[str, Any] = {}

    async def a_minute_on(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + MINUTE + DELIVERY_MS)
        (seen["asked for"],) = paper.watch.requests()

    ran = await paper.run(
        [
            Step(green(FIRST_GREEN_MS, "480.00", symbol="QQQ")),
            Step(green(ENTER_MS, "585.00", symbol="SPY")),
            Step(green(ENTER_MS, "480.40", symbol="QQQ")),
            Step(red(ENTER_MS + MINUTE, "480.00", symbol="QQQ"), before=a_minute_on),
        ]
    )

    assert sorted(ran.feed.symbols_asked_for) == ["QQQ", "SPY"]
    (search,) = paper.worker.asked
    assert search.underlying.pricing_bar_close == Decimal("585.00")
    assert paper.sent() == [(CALL, "buy", 1, "limit", "4.12")] and paper.holds() == {CALL: 1.0}
    assert (seen["asked for"].underlying, seen["asked for"].money) == ("SPY", Decimal("585.00"))
    assert seen["asked for"].held == (CALL_585,)


# ---- Which accounts run an option bot ----------------------------------------------------------------


class _NothingToStream:
    feed_id = "fake"

    def __init__(self) -> None:
        self.symbols_asked_for: list[str] = []

    async def stream_bars(self, symbol: str, **_how: Any) -> AsyncIterator[MarketDataBar]:
        self.symbols_asked_for.append(symbol)
        for bar in ():
            yield bar

    async def recent_closed_bars(self, symbol: str, **_how: Any) -> list[MarketDataBar]:
        self.symbols_asked_for.append(symbol)
        return []


@pytest.mark.parametrize(
    ("account_id", "authority_kind", "plan", "reason", "named"),
    [
        pytest.param(
            "LIVE-OPTIONS", "sqlite", one_call, OptionEffectRefusal.ACCOUNT_NOT_OPEN, "A Live account",
            id="a Live account, any option plan",
        ),
    ],
)
async def test_the_runner_raises_the_clerks_own_refusal_of_an_option_plan_before_anything_is_asked_for(
    paper: PaperFloor, tmp_path: Path, account_id: str, authority_kind: Any, plan: Callable[[], Any],
    reason: OptionEffectRefusal, named: str,
) -> None:
    """A Live account with the rollout setting off takes no option bot. The runner asks the account's
    Clerk before opening a stream or asking for quote lines."""
    repo = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=tmp_path / "live")
    clerk = SqliteAlpacaClerkFacade(
        repo=repo, read=paper.account, trade=paper.venue, account_mode="live", authority_kind=authority_kind,
        live_envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=authority_kind == "shadow"),
    )
    binding = paper_binding_for(plan()).model_copy(update={"sealed_account_id": account_id})
    ledger = SourceBarLedger(artifacts_root=tmp_path / "live-ledger", account_id=account_id)
    feed = _NothingToStream()
    set_alpaca_clerk(clerk)
    try:
        with pytest.raises(OptionPlanRefused) as refused:
            await bot_trade_strategy.run_trade_bot(
                binding, feed, source_bars=ledger, pricing_feed=feed, option_market=paper.market()
            )
    finally:
        set_alpaca_clerk(paper.clerk)
        ledger.close()
        repo.close()

    assert refused.value.reason is reason
    assert named in refused.value.detail
    assert feed.symbols_asked_for == [] and paper.watch.requests() == ()
    assert paper.venue.option_submit_calls == []


async def test_an_option_bot_on_paper_without_its_source_bar_ledger_does_not_start(paper: PaperFloor) -> None:
    """The money a strike rule counts from is read from the run's own retained bars."""
    feed = _NothingToStream()

    with pytest.raises(RuntimeError, match="source-bar ledger"):
        await bot_trade_strategy.run_trade_bot(paper.binding, feed, source_bars=None, option_market=paper.market())

    assert feed.symbols_asked_for == [] and paper.watch.requests() == ()


# ---- A Dry Run goes through the same wait ------------------------------------------------------------


async def test_in_a_dry_run_an_entry_is_settled_at_once_through_the_same_wait(dry_run: Floor) -> None:
    """A simulation fills inside the send. The Clerk is asked what the entry came to all the same, answers on
    its first read without a pause, and records it. The journal's row says what the entry came to."""

    async def no_pause(_seconds: float) -> None:
        pytest.fail("a simulated entry is proven as it is sent: the Clerk never pauses for it")

    dry_run.clerk._outcome_pause = no_pause

    asked_for_while_held: list[Any] = []

    async def two_minutes_on(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + 2 * MINUTE + DELIVERY_MS)
        asked_for_while_held.extend(floor.watch.requests())
        asked_for_while_held.append(floor.holds())

    script = _round_trip()
    script[3] = Step(script[3].bar, before=two_minutes_on)

    ran = await dry_run.run(script)

    # What a Dry Run's bot holds is its Clerk's to ask for, not its run's: a bar after the fill, with the
    # window moved since, the run's own request still names no held contract.
    the_runs_own, held = asked_for_while_held
    assert held == {CALL: 1.0}
    assert the_runs_own.plan is not None and the_runs_own.held == ()
    assert dry_run.transitions("ENTRY_SETTLED") == 1
    (order_ref,) = [row.order_ref for row in ran.journal if row.intent == "ENTER"]
    recorded = dry_run.repo.last_order_transition(order_ref=order_ref, transition_kind="ENTRY_SETTLED")
    assert recorded is None, "the settlement is on the ENTER's own timeline, not the order's"
    settled = json.loads(_the_settlement(dry_run))
    assert (settled["settlement"], settled["standing"]) == ("entered", "filled")
    assert (settled["units_asked"], settled["units_filled"], settled["until_ms"]) == (1, 1, WAITED_UNTIL_MS)
    bought, sold = ran.journal
    assert (bought.entry_outcome, sold.entry_outcome) == ("filled", None)
    assert [outcome for outcome, _ in ran.outcomes()][-1] == "exit_intent"


def _the_settlement(floor: Floor) -> str:
    with floor.repo._write_lock:
        (facts,) = floor.repo._conn.execute(
            "SELECT facts_json FROM custody_transitions WHERE transition_kind = 'ENTRY_SETTLED'"
        ).fetchone()
    return facts


async def test_a_simulated_entry_that_buys_nothing_is_dropped_writes_no_journal_row_and_the_run_goes_on(
    dry_run: Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A simulation that prices its fill above the order's limit ends the order unfilled on the spot. That is
    an ordinary ending, not a broken simulation: the decision is dropped, the journal lists no fill, and the
    next green bar is an ENTER again, which fills."""
    price = SimulatedOptionOrders._simulated_option_fill
    priced: list[Any] = []

    def above_the_limit_the_first_time(self: Any, order: Any, bound: Any, *, at_ms: int) -> Any:
        fill = price(self, order, bound, at_ms=at_ms)
        priced.append(order)
        return fill._replace(price=fill.price + Decimal("0.05")) if len(priced) == 1 else fill

    monkeypatch.setattr(SimulatedOptionOrders, "_simulated_option_fill", above_the_limit_the_first_time)

    ran = await dry_run.run([*_two_greens(), Step(green(ENTER_MS + MINUTE))])

    assert [outcome for outcome, _ in ran.outcomes()] == ["no_action", "enter_intent", "enter_intent"]
    assert dry_run.transitions("ENTRY_SETTLED") == 2
    assert [(row.intent, row.fill_price, row.entry_outcome) for row in ran.journal] == [("ENTER", 4.12, "filled")]
    assert dry_run.holds() == {CALL: 1.0}


async def test_an_exit_the_clerk_refused_is_never_said_to_have_been_accepted(
    dry_run: Floor, the_runner_is_heard: pytest.LogCaptureFixture
) -> None:
    """Nobody bids for the call when the EXIT comes, so the Clerk cannot price the close and refuses it. The
    bot goes on holding, and the journal lists no sale. The journal's writer is not asked about a refused
    decision, so nothing says that this EXIT "was accepted and has not filled": that line is for an EXIT the
    Clerk took and holds for the open."""

    async def nobody_bids(floor: Floor) -> None:
        floor.markets[585] = ("0", "0.05")
        await floor.bar_arrives(EXIT_MS + DELIVERY_MS)

    script = _round_trip()
    script[-1] = Step(script[-1].bar, before=nobody_bids)

    ran = await dry_run.run(script)

    assert dry_run.transitions("EXIT_NOT_FLAT") == 1 and dry_run.holds() == {CALL: 1.0}
    assert [row.intent for row in ran.journal] == ["ENTER"]
    assert _said(the_runner_is_heard, "dry_run_option_exit_not_filled") == []


async def test_the_journal_refuses_a_fill_the_clerk_says_never_happened(dry_run: Floor, tmp_path: Path) -> None:
    """The one thing still raised: the Clerk's answer and its own fill records disagree."""
    ran = await dry_run.run(_two_greens())
    enter = _decision(ran, ENTER_MS)
    said = await dry_run.clerk.await_entry_outcome(strategy_instance_id=SID, decision_id=enter, until_ms=ENTER_MS)
    assert said.units_filled == 1
    (evaluation_bar,) = [facts for _, facts in ran.receipts if facts["decision_bar_close_ms"] == ENTER_MS]
    journal = DryRunActivityJournal(tmp_path / "another-journal")

    def write(settled: Any) -> None:
        bot_trade_strategy._journal_option_fill(
            journal, clerk=dry_run.clerk, binding=dry_run.binding, options=_Underlying(), account_id=dry_run.repo.account_id,
            evaluation=_Evaluated(enter), intent=_Entering(), receipt=_Accepted(), bar_ref=evaluation_bar["bar_ref"],
            settled=settled,
        )

    with pytest.raises(RuntimeError, match="filled 0 of 1"):
        write(replace(said, standing=EntryStanding.UNFILLED, units_filled=0))
    assert journal.tail(5) == []
    write(said)
    assert [row.entry_outcome for row in journal.tail(5)] == ["filled"]


class _Underlying:
    underlying = "SPY"


class _Evaluated:
    def __init__(self, evaluation_id: str) -> None:
        self.evaluation_id = evaluation_id


class _Entering:
    from app.engine.strategy.signal_intent import SignalIntentKind as _Kind

    kind = _Kind.ENTER


class _Accepted:
    state = "submitted"
    child_order_refs: tuple[str, ...] = ()


# ---- The closed set ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("standing", "came_to"),
    [
        (EntryStanding.FILLED, EntryCameTo.FILLED),
        (EntryStanding.PART_FILLED, EntryCameTo.PART_FILLED),
        (EntryStanding.UNFILLED, EntryCameTo.CANCELLED_UNFILLED),
        (EntryStanding.NOT_SENT, EntryCameTo.REJECTED),
        (EntryStanding.WORKING, EntryCameTo.RESTING),
        (EntryStanding.CANCELLATION_REQUESTED, EntryCameTo.RESTING),
        (EntryStanding.OUTCOME_UNKNOWN, EntryCameTo.UNPROVEN),
    ],
)
def test_every_standing_the_clerk_can_answer_comes_to_one_of_the_runners_six_words(
    standing: EntryStanding, came_to: EntryCameTo
) -> None:
    """An order the books show at the broker is resting. One whose outcome nobody knows is never called that."""
    assert entry_came_to(standing) is came_to


def test_the_runners_six_words_cover_every_standing_the_clerk_has() -> None:
    assert {entry_came_to(standing) for standing in EntryStanding} == set(EntryCameTo)


def _row(**changes: Any) -> DryRunActivity:
    row: dict[str, Any] = {
        "seq": 1, "strategy_instance_id": SID, "run_id": "run-dry",
        "authority_account_id": synthetic_account_id_for_strategy(SID), "authority_kind": "synthetic",
        "recorded_at_ms": ENTER_MS, "bar_ref": "bar", "intent": "ENTER", "order_ref": "order", "symbol": CALL,
        "side": "buy", "quantity": 1.0, "fill_price": 4.12, "underlying": "SPY", "multiplier": 100,
        "fill_model": "natural_price", "quote_age_ms": 0, "contract_reference": "ibkr_listing",
    }
    row.update(changes)
    return DryRunActivity.model_validate(row)


def test_a_journal_row_written_before_entries_said_what_they_came_to_reads_and_writes_as_it_did() -> None:
    """The field is left out when absent, so an older row's bytes are what they were."""
    older = _row()

    assert older.entry_outcome is None
    assert "entry_outcome" not in json.loads(older.model_dump_json())
    assert json.loads(_row(entry_outcome="filled").model_dump_json())["entry_outcome"] == "filled"


@pytest.mark.parametrize(
    "not_an_entrys_fill_in_a_contract",
    [
        {"intent": "EXIT", "side": "sell", "contract_reference": None},
        {
            "symbol": "SPY", "underlying": None, "multiplier": None, "fill_model": None, "quote_age_ms": None,
            "contract_reference": None,
        },
    ],
    ids=["an exit", "a fill in shares"],
)
def test_only_an_entrys_fill_in_a_contract_says_what_its_entry_came_to(
    not_an_entrys_fill_in_a_contract: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match=r"entry came to|option contract"):
        _row(entry_outcome="filled", **not_an_entrys_fill_in_a_contract)


@pytest.mark.parametrize("word", ["cancelled_unfilled", "rejected", "bought"])
def test_a_journal_row_never_says_an_entry_with_a_fill_bought_nothing_nor_a_word_outside_the_set(word: str) -> None:
    with pytest.raises(ValueError):
        _row(entry_outcome=word)


@pytest.mark.parametrize("word", ["filled", "part_filled_then_cancelled", "resting", "unproven"])
def test_a_journal_row_says_what_its_entry_came_to_in_the_runners_own_words(word: str) -> None:
    """The journal spells no words of its own: each is one of the runner's set, read back as that."""
    assert _row(entry_outcome=word).entry_outcome is EntryCameTo(word)
