"""An option bot's entry rests, and its strategy is settled on what the entry came to (#2913).

``deployment_validation`` decides on one-minute bars: ENTER on the second of
two green bars, EXIT three bars later. A discarded ENTER keeps its streak, so
the next green bar is an ENTER again. The bot listens to SPY and buys SPY
calls at the money, expiring on Friday: the 585 call, quoted 4.08 bid and
4.12 offered. So its entry is a limit order to buy at 4.12.

On a Paper account that order rests at Alpaca. The runner waits for what it
came to, no longer than fifty seconds after the bar it decided on closed. The
bot's order terms are the ones a Deploy starts from: the Clerk cancels what
has not filled twenty seconds after it took the entry.

Everything but the IBKR connection, the bar feed, the vendors and Alpaca is
the real thing (``_paper_option_floor``). Alpaca is the double the Clerk's
own Paper tests use. No order was sent to Alpaca and no key was used.
"""

from __future__ import annotations

import asyncio
import json
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
from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionPlanRefused
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntrySettlement, EntryStanding
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
from app.broker.ibkr.bars import REALTIME_BAR_STALL_TIMEOUT_S
from app.broker.ibkr.config import reset_settings_for_testing
from app.marketdata.feed import FeedHealth, MarketDataBar
from app.options.deadlines import CloseTerms
from app.services import bot_option_run, market_liveness
from app.services.bot_dry_run import DryRunActivity, DryRunActivityJournal
from app.services.bot_option_run import ENTRY_OUTCOME_WAIT_MS, EntryCameTo, OptionOrderTerms, entry_came_to
from app.services.bot_trade_strategy_warmup import ENTRY_DISCARDED
from app.services.source_bar_ledger import SourceBarLedger
from app.utils import timestamps
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES

from ._option_floor import (
    CALL_585,
    DELIVERY_MS,
    ENTER_MS,
    EXIT_MS,
    FIRST_GREEN_MS,
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
    strategy is in, the held contract is asked for as a held quote line, and the exit sells one."""
    paper = await on_paper(contracts=2).open()
    seen: dict[str, Any] = {}

    def look_while_it_rests() -> None:
        (asked,) = paper.watch.requests()
        seen["resting"] = (asked.plan is not None, asked.held, CALL in paper.ibkr.open_option_lines())

    async def a_minute_on(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + MINUTE + DELIVERY_MS)
        seen["held"] = (paper.watched_as_held(), floor.holds())

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
    # From the part fill on, the run asks for the contract it holds as a held line.
    assert seen["held"] == ((CALL_585,), {CALL: 1.0})
    assert paper.replayed_dispositions().outcomes[_decision(ran, ENTER_MS)] == "enter_intent"


async def test_an_order_alpaca_rejects_is_a_refused_entry_and_the_next_signal_enters(paper: PaperFloor) -> None:
    """Alpaca answers the send with a refusal. Nothing rests, so nothing is waited for: the decision is
    dropped at once, and the next green bar is an ENTER again."""
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


async def test_an_entry_still_unproven_when_its_wait_ends_is_settled_as_entered_and_no_second_entry_is_made(
    paper: PaperFloor,
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


async def test_an_entry_whose_send_outran_its_wait_is_settled_as_entered_with_nothing_recorded(
    paper: PaperFloor,
) -> None:
    """Alpaca takes fifty-four seconds to answer the send. By then the wait for the entry's outcome is over
    before it could be asked for, and the Clerk gives no answer. The entry is unproven at the end of its
    wait like any other: the strategy is in, and nothing is recorded."""
    send = paper.venue.submit_option_order

    async def a_send_that_takes_nearly_a_minute(order: Any, *, client_order_id: str) -> Any:
        for _ in range(6):
            paper.clock.value += 9_000
            paper.repo.renew_execution_lease()
        return await send(order, client_order_id=client_order_id)

    paper.venue.submit_option_order = a_send_that_takes_nearly_a_minute  # type: ignore[method-assign]

    ran = await paper.run([*_two_greens(), Step(green(ENTER_MS + MINUTE))])

    assert paper.settled() is None and paper.outcome_reads_paused == 0
    assert paper.waits_asked_until == [WAITED_UNTIL_MS]
    # The strategy is in: the green bar after is no second ENTER.
    assert [outcome for outcome, _ in ran.outcomes()] == ["no_action", "enter_intent", "no_action"]
    assert len(paper.venue.option_submit_calls) == 1


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
    ("account_id", "authority_kind", "named"),
    [
        pytest.param("LIVE-OPTIONS", "sqlite", "A Live account", id="a Live account"),
        pytest.param("shadow:LIVE-OPTIONS", "shadow", "A Live Shadow", id="a Live Shadow"),
    ],
)
async def test_the_runner_raises_the_clerks_own_refusal_of_an_option_plan_before_anything_is_asked_for(
    paper: PaperFloor, tmp_path: Path, account_id: str, authority_kind: Any, named: str
) -> None:
    """Only a Dry Run and a Paper account take an option bot. The runner asks the account's Clerk, and its
    refusal comes out by name before a stream is opened or a quote line asked for."""
    repo = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=tmp_path / "live")
    clerk = SqliteAlpacaClerkFacade(
        repo=repo, read=paper.account, trade=paper.venue, account_mode="live", authority_kind=authority_kind,
        live_envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=authority_kind == "shadow"),
    )
    binding = replace_account(paper, account_id)
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

    assert refused.value.reason is OptionEffectRefusal.ACCOUNT_NOT_OPEN
    assert named in refused.value.detail
    assert feed.symbols_asked_for == [] and paper.watch.requests() == ()
    assert paper.venue.option_submit_calls == []


def replace_account(paper: PaperFloor, account_id: str) -> Any:
    return paper_binding_for(one_call()).model_copy(update={"sealed_account_id": account_id})


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

    ran = await dry_run.run(_round_trip())

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
    price = SyntheticBroker._simulated_option_fill
    priced: list[Any] = []

    def above_the_limit_the_first_time(self: Any, order: Any, bound: Any, *, at_ms: int) -> Any:
        fill = price(self, order, bound, at_ms=at_ms)
        priced.append(order)
        return fill._replace(price=fill.price + Decimal("0.05")) if len(priced) == 1 else fill

    monkeypatch.setattr(SyntheticBroker, "_simulated_option_fill", above_the_limit_the_first_time)

    ran = await dry_run.run([*_two_greens(), Step(green(ENTER_MS + MINUTE))])

    assert [outcome for outcome, _ in ran.outcomes()] == ["no_action", "enter_intent", "enter_intent"]
    assert dry_run.transitions("ENTRY_SETTLED") == 2
    assert [(row.intent, row.fill_price, row.entry_outcome) for row in ran.journal] == [("ENTER", 4.12, "filled")]
    assert dry_run.holds() == {CALL: 1.0}


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
        (EntryStanding.OUTCOME_UNKNOWN, EntryCameTo.RESTING),
    ],
)
def test_every_standing_the_clerk_can_answer_comes_to_one_of_the_runners_five_words(
    standing: EntryStanding, came_to: EntryCameTo
) -> None:
    assert entry_came_to(standing) is came_to


def test_the_runners_five_words_cover_every_standing_the_clerk_has() -> None:
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


def test_a_journal_row_never_says_an_entry_with_a_fill_bought_nothing() -> None:
    with pytest.raises(ValueError):
        _row(entry_outcome="cancelled_unfilled")
