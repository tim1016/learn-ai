"""An option bot in a Dry Run, through the real runner: one bought call, from the signal to the sale (#2913).

``deployment_validation`` decides on one-minute bars: ENTER on the second of
two green bars, EXIT three bars later. The bot listens to SPY and buys one
SPY call, at the money, expiring on Friday. SPY's decision bar closes at
585.00, so the rules come to the 585 call, quoted 4.08 bid and 4.12 offered:
one contract costs $412.00 to buy.

Everything but the IBKR connection, the bar feed and the vendors is the real
thing (``_option_floor``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes
from app.broker.alpaca.clerk.budgets import account_budget, deployment_budget
from app.broker.alpaca.clerk.held_option_quotes import KEEP_EVERY_MS, reset_held_option_quotes_for_testing
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionPlanRefused
from app.broker.alpaca.clerk.sqlite import runtime as clerk_runtime
from app.broker.alpaca.clerk.sqlite.entry_outcome import ENTRY_OUTCOME_LIMIT_TOO_FAR, EntryOutcomeWaitRefused
from app.broker.alpaca.clerk.sqlite.risk_admission import current_risk_readiness
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulatedAccountProjection, SimulationEvidenceUnavailable
from app.broker.alpaca.market_liveness import set_market_liveness_consumer
from app.broker.ibkr.config import reset_settings_for_testing
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.options.deadlines import CloseTerms
from app.options.requirement import StructureMoneyRefusal
from app.options.simulated_fill import SimulatedFillModel
from app.options.watch import LineClass
from app.schemas.market_liveness import MarketStatusSnapshot, MarketStatusSource, TopOfBookQuote
from app.services import bot_option_run, market_liveness, option_search
from app.services.bot_option_run import OptionOrderTerms
from app.services.broker_v2_panel import budget_deploy
from app.services.option_model_inputs import ModelInputsBeingRead
from app.services.option_search import OptionSearches, OptionSearchLane
from app.services.option_watch import get_option_watch_list
from tests._helpers.bot_runner.custody import _SID, _T0, _registry
from tests._helpers.bot_runner.doubles import _FakeFeed
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

# The resolver's own scenes, for a contract by its strike.
from tests.options import test_resolver as scenes

# A stand-in worker that dies with the search it was handed.
from tests.services.test_option_search import _Dies

from ._option_floor import (
    ACCOUNT,
    CALL_585,
    DELIVERY_MS,
    DIVIDENDS_UNKNOWN,
    ENTER_MS,
    EXIT_MS,
    FIRST_GREEN_MS,
    FRIDAY,
    KNOWN,
    LEG,
    MINUTE,
    SID,
    TUESDAY,
    WEDNESDAY,
    AnswersWith,
    Floor,
    Ran,
    SearchesLate,
    Step,
    green,
    one_call,
    red,
)
from ._support import _bar, _wait_for

CALL = CALL_585.occ_symbol


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
async def floor(on_the_floor: Callable[..., Floor]) -> Floor:
    """The usual floor: the bot listens to SPY and buys one SPY call at the money, with $1,000.00."""
    return await on_the_floor().open()


def _round_trip(*, after_the_entry: Callable[[Floor], Any] | None = None, sells_at: tuple[str, str] = ("4.30", "4.34")) -> list[Step]:
    """Two green bars, then three more: the ENTER at 10:31, the EXIT at 10:34 with the call quoted ``sells_at``."""

    async def minute_after_the_entry(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + MINUTE + DELIVERY_MS)
        if after_the_entry is not None:
            after_the_entry(floor)

    async def exit_bar(floor: Floor) -> None:
        floor.markets[585] = sells_at
        await floor.bar_arrives(EXIT_MS + DELIVERY_MS)

    return [
        Step(green(FIRST_GREEN_MS)),
        Step(green(ENTER_MS)),
        Step(red(ENTER_MS + MINUTE), before=minute_after_the_entry),
        Step(red(ENTER_MS + 2 * MINUTE)),
        Step(red(EXIT_MS), before=exit_bar),
    ]


# ---- The whole loop -----------------------------------------------------------------


async def test_an_option_bot_buys_the_call_its_rules_pick_at_its_ask_and_sells_it_at_its_bid(floor: Floor) -> None:
    """The contract is the resolver's own pick from the quotes held. It is bought at the ask, held as one
    contract of a hundred shares, and the EXIT sells that same contract at the bid and leaves the bot flat."""
    held_meanwhile: dict[str, Any] = {}

    def look(floor: Floor) -> None:
        held_meanwhile["position"] = floor.holds()
        held_meanwhile["the_bot_holds"] = floor.clerk.held_option_contracts(SID)
        held_meanwhile["clerk_asks_for"] = floor.held.requests()

    ran = await floor.run(_round_trip(after_the_entry=look))

    (search,) = floor.worker.asked
    # The money is the close of SPY's own bar for the decision's minute, not its live quote.
    assert (search.underlying.pricing_bar_close, search.decision_bar_close_ms) == (Decimal("585.00"), ENTER_MS)
    assert search.selection_ms == ENTER_MS + DELIVERY_MS
    assert floor.what_the_resolver_picks().picked == {LEG: CALL_585}

    assert floor.fills() == [
        (CALL, "buy", 1.0, 4.12, 100, Decimal("412.00")),
        (CALL, "sell", 1.0, 4.30, 100, Decimal("430.00")),
    ]
    bought, sold = floor.simulated_fills()
    assert (bought.fill_model, bought.fill_price, bought.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE, Decimal("4.12"), 0,
    )
    assert (sold.fill_model, sold.fill_price, sold.quote_age_ms) == (SimulatedFillModel.NATURAL_PRICE, Decimal("4.3"), 0)

    assert held_meanwhile["position"] == {CALL: 1.0}
    assert floor.holds() == {}
    assert floor.transitions("EXIT_ATTRIBUTED_FLAT") == 1
    # What the EXIT closed was the Clerk's own answer to "what does this bot hold".
    assert (held_meanwhile["the_bot_holds"], floor.clerk.held_option_contracts(SID)) == ((CALL,), ())
    assert ran.outcomes() == [
        ("no_action", FIRST_GREEN_MS),
        ("enter_intent", ENTER_MS),
        ("no_action", ENTER_MS + MINUTE),
        ("no_action", ENTER_MS + 2 * MINUTE),
        ("exit_intent", EXIT_MS),
    ]
    # While it was held the contract was the Clerk's to ask for; flat, nobody asks for it.
    (held,) = held_meanwhile["clerk_asks_for"]
    assert (held.underlying, held.held, held.line_class) == ("SPY", (CALL_585,), LineClass.CANDIDATE)
    await floor.supervisor_passes(1)
    assert floor.held.requests() == ()
    assert floor.watch.requests() == (), "the run took its own request away as it ended"
    assert (ACCOUNT, SID) == ("sim:dry-run-buys-a-call", "dry-run-buys-a-call")


async def test_the_journal_names_the_contract_its_multiplier_and_how_each_fill_was_priced(floor: Floor) -> None:
    """What the bot page lists as this run's fills: a contract, never a share of SPY at four dollars."""
    ran = await floor.run(_round_trip())

    bought, sold = ran.journal
    assert (bought.intent, bought.symbol, bought.side, bought.quantity, bought.fill_price) == (
        "ENTER", CALL, "buy", 1.0, 4.12,
    )
    assert (bought.underlying, bought.multiplier, bought.fill_model, bought.quote_age_ms) == (
        "SPY", 100, "natural_price", 0,
    )
    # A Dry Run has no broker reference to ask: the listing IBKR gave is the contract's, and the entry says so.
    assert (bought.contract_reference, sold.contract_reference) == ("ibkr_listing", None)
    assert (sold.intent, sold.symbol, sold.side, sold.quantity, sold.fill_price) == ("EXIT", CALL, "sell", 1.0, 4.30)
    assert (sold.underlying, sold.multiplier, sold.fill_model, sold.quote_age_ms) == ("SPY", 100, "natural_price", 0)
    # Each is stamped when the simulation filled it, after the quotes were read again, and names its own order.
    assert (bought.recorded_at_ms, sold.recorded_at_ms) == (ENTER_MS + DELIVERY_MS, EXIT_MS + DELIVERY_MS)
    assert bought.order_ref != sold.order_ref
    assert {bought.price_bar_end_ms, sold.price_bar_end_ms} == {None}


def _decisions(ran: Ran) -> dict[str, str]:
    """Each decision's own id, as the runner named it to the Clerk, by its outcome."""
    return {outcome: facts["evaluation_id"] for outcome, facts in ran.receipts}


async def test_the_clerk_says_what_each_decision_filled_when_asked_by_the_decisions_own_name(floor: Floor) -> None:
    """The runner asks the Clerk what a decision filled. It takes no order out of a receipt by its place:
    an EXIT's receipt names the entry it closes before the order that closed it."""
    ran = await floor.run(_round_trip())
    decided = _decisions(ran)

    (bought,) = floor.clerk.option_fills_of_decision(SID, decided["enter_intent"])
    (sold,) = floor.clerk.option_fills_of_decision(SID, decided["exit_intent"])

    assert (bought.contract, bought.side, bought.quantity, bought.price, bought.multiplier) == (CALL, "buy", 1.0, 4.12, 100)
    assert (sold.contract, sold.side, sold.quantity, sold.price, sold.multiplier) == (CALL, "sell", 1.0, 4.30, 100)
    assert (bought.fill_model, bought.quote_age_ms, bought.filled_at_ms) == ("natural_price", 0, ENTER_MS + DELIVERY_MS)
    assert (sold.fill_model, sold.quote_age_ms, sold.filled_at_ms) == ("natural_price", 0, EXIT_MS + DELIVERY_MS)
    assert bought.order_ref != sold.order_ref, "each is the fill of its own decision's order"
    assert [row.order_ref for row in ran.journal] == [bought.order_ref, sold.order_ref]
    # A decision that made no order, and one the Clerk never heard of, filled nothing.
    assert floor.clerk.option_fills_of_decision(SID, decided["no_action"]) == ()
    assert floor.clerk.option_fills_of_decision(SID, "a-decision-nobody-made") == ()


async def test_the_watched_window_is_asked_for_from_the_start_and_follows_the_underlyings_bars(floor: Floor) -> None:
    """Quotes must be held before a decision comes. The window is centred on SPY's live quote until a bar
    closes, then on each bar's close, and a minute with no trade leaves it where it was."""
    seen: dict[str, Any] = {}

    def look(named: str) -> Callable[[Floor], Any]:
        async def arrives(floor: Floor) -> None:
            (seen[f"asked before {named}"],) = floor.watch.requests()
            await floor.bar_arrives(seen["at"])
            seen[f"lines before {named}"] = floor.ibkr.open_option_lines()

        return arrives

    async def first(floor: Floor) -> None:
        seen["at"] = FIRST_GREEN_MS + DELIVERY_MS
        await look("any bar")(floor)

    async def second(floor: Floor) -> None:
        seen["at"] = ENTER_MS + DELIVERY_MS
        await look("the move")(floor)

    async def third(floor: Floor) -> None:
        seen["at"] = ENTER_MS + MINUTE + DELIVERY_MS
        await look("the quiet minute")(floor)

    async def fourth(floor: Floor) -> None:
        seen["at"] = ENTER_MS + 2 * MINUTE + DELIVERY_MS
        await look("the end")(floor)

    await floor.run(
        [
            Step(red(FIRST_GREEN_MS, "585.00"), before=first),
            Step(red(ENTER_MS, "588.00"), before=second),
            Step(red(ENTER_MS + MINUTE, "590.00", volume=0), before=third),
            Step(red(ENTER_MS + 2 * MINUTE, "588.20"), before=fourth),
        ]
    )

    def calls(*strikes: int) -> set[str]:
        return {scenes._contract("call", strike).occ_symbol for strike in strikes}

    start, after_585, after_588, after_no_trade = (
        seen[f"asked before {named}"] for named in ("any bar", "the move", "the quiet minute", "the end")
    )
    assert (start.underlying, start.line_class, start.plan, start.money) == (
        "SPY", LineClass.CANDIDATE, floor.binding.action_plan, None,
    )
    assert (after_585.money, after_588.money, after_no_trade.money) == (
        Decimal("585.00"), Decimal("588.00"), Decimal("588.00"),
    )
    # Two strikes either side of the money, calls only: the plan has one call leg.
    assert seen["lines before any bar"] == seen["lines before the move"] == calls(583, 584, 585, 586, 587)
    assert seen["lines before the quiet minute"] == seen["lines before the end"] == calls(586, 587, 588, 589, 590)
    assert floor.worker.asked == [], "no bar here was an entry"
    # No decision here read the rate and dividends. They were read as the first bar closed all the same,
    # so that a decision, when one comes, finds them kept.
    assert floor.inputs_read_as_of[0] == FIRST_GREEN_MS


async def test_options_on_another_symbol_take_the_money_from_that_symbols_own_bar(
    on_the_floor: Callable[..., Floor],
) -> None:
    """The bot listens to QQQ and buys SPY calls. QQQ's bar decides; SPY's bar for the same minute is the money."""
    floor = await on_the_floor(plan=one_call(underlying="SPY"), listens_to="QQQ").open()

    ran = await floor.run(
        [
            Step(green(FIRST_GREEN_MS, "480.00", symbol="QQQ")),
            Step(green(ENTER_MS, "585.00", symbol="SPY")),
            Step(green(ENTER_MS, "480.40", symbol="QQQ")),
        ]
    )

    (search,) = floor.worker.asked
    assert sorted(ran.feed.symbols_asked_for) == ["QQQ", "SPY"]
    assert search.underlying.pricing_bar_close == Decimal("585.00")
    assert floor.fills() == [(CALL, "buy", 1.0, 4.12, 100, Decimal("412.00"))]
    assert [(row.symbol, row.underlying) for row in ran.journal] == [(CALL, "SPY")]


async def test_an_entry_with_no_bar_of_the_underlying_for_its_minute_is_refused_as_it_is_for_shares(
    on_the_floor: Callable[..., Floor],
) -> None:
    floor = await on_the_floor(plan=one_call(underlying="SPY"), listens_to="QQQ").open()

    ran = await floor.run(
        [Step(green(FIRST_GREEN_MS, "480.00", symbol="QQQ")), Step(green(ENTER_MS, "480.40", symbol="QQQ"))]
    )

    [(reason_code, _why)] = ran.blocked()
    assert reason_code == "PRICING_BAR_LATE"
    assert (floor.worker.asked, floor.orders_on_the_books()) == ([], 0)


# ---- Each way an entry is not sent ----------------------------------------------------


def _nothing_was_sent(floor: Floor, ran: Ran) -> None:
    assert floor.orders_on_the_books() == 0
    assert floor.simulated_fills() == [] and floor.holds() == {} and ran.journal == []


def _entry_then_three_bars(at_the_entry: Callable[[Floor], Any] | None = None, **entry_bar: Any) -> list[Step]:
    """The ENTER bar, with whatever is true as it arrives, then three more bars with everything in order."""

    async def entry(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + DELIVERY_MS)
        if at_the_entry is not None:
            at_the_entry(floor)

    return [
        Step(green(FIRST_GREEN_MS)),
        Step(green(ENTER_MS, **entry_bar), before=entry),
        Step(red(ENTER_MS + MINUTE)),
        Step(red(ENTER_MS + 2 * MINUTE)),
        Step(red(EXIT_MS)),
    ]


def _the_feed_drops(floor: Floor) -> None:
    floor.ibkr.client.connection_state = "soft_lost"


def _the_call_is_crossed(floor: Floor) -> None:
    floor.quote(585, "4.20", "4.12")


def _the_call_is_quoted_a_dollar_wide(floor: Floor) -> None:
    floor.quote(585, "4.00", "5.00")


def _no_quote_source(floor: Floor) -> None:
    floor.quote_source = lambda: None


def _the_worker_dies(floor: Floor) -> None:
    floor.lane = OptionSearchLane("entry", executor=_Dies)


def _the_ask_goes_while_the_search_runs(floor: Floor) -> None:
    floor.worker.during = lambda: floor.ibkr.tick(CALL, ask=-1.0)


@pytest.mark.parametrize(
    ("trouble", "reason_code", "searched"),
    [
        pytest.param(_no_quote_source, "OPTION_ENTRY_NO_QUOTE_SOURCE", False, id="no IBKR supervisor in this process"),
        pytest.param(_the_feed_drops, "OPTION_CONTRACT_FEED_UNHEALTHY", False, id="the quote feed is not up"),
        pytest.param(_the_call_is_crossed, "OPTION_CONTRACT_NO_USABLE_QUOTE", True, id="the resolver refuses"),
        pytest.param(_the_ask_goes_while_the_search_runs, "OPTION_CONTRACT_NO_USABLE_QUOTE", True, id="the recheck refuses"),
        pytest.param(_the_worker_dies, "OPTION_SEARCH_WORKER_FAILED", False, id="the search could not be run"),
        pytest.param(
            _the_call_is_quoted_a_dollar_wide, StructureMoneyRefusal.LEG_GAP_OVER_LIMIT.value, True, id="the Clerk refuses"
        ),
    ],
)
async def test_an_entry_refused_on_the_way_is_one_blocked_decision_and_nothing_is_sent_or_kept_for_later(
    floor: Floor, trouble: Callable[[Floor], None], reason_code: str, searched: bool
) -> None:
    """Whatever stops it, the owner sees one refused decision with its own reason, in words. The three bars
    that follow are ordinary ones: the entry is not tried again on any of them."""
    ran = await floor.run(_entry_then_three_bars(trouble))

    [(recorded, why)] = ran.blocked()
    assert recorded == reason_code
    assert why.strip(), "the refusal says nothing in words"
    assert ran.outcomes() == [
        ("no_action", FIRST_GREEN_MS),
        ("blocked", ENTER_MS),
        ("no_action", ENTER_MS + MINUTE),
        ("no_action", ENTER_MS + 2 * MINUTE),
        ("no_action", EXIT_MS),
    ]
    assert len(floor.worker.asked) == (1 if searched else 0)
    _nothing_was_sent(floor, ran)


async def test_the_recheck_refuses_a_pick_the_search_itself_made(floor: Floor) -> None:
    """The search came to the 585 call. By the time it answered nothing was offered in it."""
    ran = await floor.run(_entry_then_three_bars(_the_ask_goes_while_the_search_runs))

    assert floor.what_the_resolver_picks().picked == {LEG: CALL_585}
    assert [code for code, _ in ran.blocked()] == ["OPTION_CONTRACT_NO_USABLE_QUOTE"]
    _nothing_was_sent(floor, ran)


async def test_an_entry_decided_before_the_chain_was_listed_is_refused_as_that(floor: Floor) -> None:
    """IBKR answers the listing request with nothing. The connection is up, and there is no chain to pick from."""
    floor.ibkr.client.ib.reqSecDefOptParamsAsync.side_effect = None
    floor.ibkr.client.ib.reqSecDefOptParamsAsync.return_value = []

    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])

    assert [code for code, _ in ran.blocked()] == ["OPTION_ENTRY_LISTING_NOT_READ"]
    _nothing_was_sent(floor, ran)


async def test_an_entry_whose_quotes_have_not_arrived_is_a_search_that_could_not_be_finished(floor: Floor) -> None:
    floor.new_lines_are_answered = False

    async def lines_open_and_silent(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + DELIVERY_MS, quotes=False)

    async def first(floor: Floor) -> None:
        await floor.bar_arrives(FIRST_GREEN_MS + DELIVERY_MS, quotes=False)

    ran = await floor.run(
        [Step(green(FIRST_GREEN_MS), before=first), Step(green(ENTER_MS), before=lines_open_and_silent)]
    )

    assert [code for code, _ in ran.blocked()] == ["OPTION_CONTRACT_SEARCH_INCOMPLETE"]
    _nothing_was_sent(floor, ran)


async def test_an_entry_in_a_minute_the_underlying_did_not_trade_has_no_money_to_count_from(floor: Floor) -> None:
    ran = await floor.run(_entry_then_three_bars(volume=0))

    assert [code for code, _ in ran.blocked()] == ["OPTION_CONTRACT_UNDERLYING_DID_NOT_TRADE"]
    (search,) = floor.worker.asked
    assert search.underlying.pricing_bar_close is None
    _nothing_was_sent(floor, ran)


async def test_a_leg_picked_by_delta_is_not_picked_while_the_dividends_are_unknown(
    on_the_floor: Callable[..., Floor],
) -> None:
    """A delta needs the dividends still to be paid. Unknown is not none, and nothing is picked on a guess."""
    floor = await on_the_floor(plan=one_call({"selector": "delta", "target": 0.5})).open()
    floor.inputs = DIVIDENDS_UNKNOWN

    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])

    assert [code for code, _ in ran.blocked()] == ["OPTION_ENTRY_DIVIDENDS_UNKNOWN"]
    assert floor.worker.asked == []
    _nothing_was_sent(floor, ran)


async def test_a_leg_picked_by_its_strike_is_bought_though_the_dividends_are_unknown(floor: Floor) -> None:
    """No rule of this plan reads the model, so nothing it picks depends on what is not known."""
    floor.inputs = DIVIDENDS_UNKNOWN

    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])

    assert ran.blocked() == []
    assert floor.fills() == [(CALL, "buy", 1.0, 4.12, 100, Decimal("412.00"))]


async def test_a_leg_picked_by_delta_is_bought_when_the_dividends_are_known(on_the_floor: Callable[..., Floor]) -> None:
    """The rule by delta comes to the contract the resolver comes to from the same quotes, through the worker."""
    floor = await on_the_floor(plan=one_call({"selector": "delta", "target": 0.5})).open()
    del floor.markets[585]

    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])

    picked = floor.what_the_resolver_picks().picked[LEG]
    assert ran.blocked() == []
    assert [fill[0] for fill in floor.fills()] == [picked.occ_symbol]
    assert floor.holds() == {picked.occ_symbol: 1.0}


# ---- A fault on the way is that decision's refusal, never the end of the run --------------


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(LookupError("the series is not there"), id="the read breaks"),
        # A waiting entry can join a lookup a page began, and a page's lookup does not wait for the coordinator.
        pytest.param(
            ModelInputsBeingRead("the coordinator is looking up the rate and the dividends for SPY"),
            id="the lookup it joined says it is still running",
        ),
    ],
)
async def test_a_fault_in_the_read_of_the_rate_and_dividends_refuses_that_entry_by_name_and_the_run_goes_on(
    floor: Floor, fault: Exception
) -> None:
    """The read itself breaks: not "the dividends are unknown", which it reports, but an error. The entry is one
    refused decision with a code of its own, and the three bars after it are decided on as usual."""

    def a_reader_that_breaks(asked_as_of_ms: int) -> None:
        # The read the decision itself waits for, not the one made in the background as the bar closed.
        if asked_as_of_ms > ENTER_MS:
            raise fault

    floor.while_inputs_are_read = a_reader_that_breaks

    ran = await floor.run(_entry_then_three_bars())

    [(reason_code, why)] = ran.blocked()
    assert reason_code == "OPTION_ENTRY_PRICING_INPUTS_NOT_READ"
    assert why.strip(), "the refusal says nothing in words"
    assert ran.outcomes() == [
        ("no_action", FIRST_GREEN_MS),
        ("blocked", ENTER_MS),
        ("no_action", ENTER_MS + MINUTE),
        ("no_action", ENTER_MS + 2 * MINUTE),
        ("no_action", EXIT_MS),
    ]
    assert (floor.worker.asked, floor.asked_of_the_clerk) == ([], [])
    _nothing_was_sent(floor, ran)


async def test_a_fault_while_the_window_follows_a_bar_never_breaks_the_stream_the_bot_decides_on(
    floor: Floor, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The watch list breaks every time a closed bar would move the window. The bars still reach the strategy:
    the bot buys its call and sells it, from the window it asked for at the start."""
    caplog.set_level(logging.ERROR, logger="app.services.bot_option_run")
    watch = floor.watch.watch

    def breaks_on_a_bar(key: str, request: Any) -> None:
        if request.money is not None:
            raise RuntimeError("the watch list is broken")
        watch(key, request)

    monkeypatch.setattr(floor.watch, "watch", breaks_on_a_bar)

    ran = await floor.run(_round_trip())

    assert [outcome for outcome, _ in ran.outcomes()] == [
        "no_action", "enter_intent", "no_action", "no_action", "exit_intent",
    ]
    assert [fill[:3] for fill in floor.fills()] == [(CALL, "buy", 1.0), (CALL, "sell", 1.0)]
    said = [record for record in caplog.records if getattr(record, "action", None) == "option_bar_listener_failed"]
    assert len(said) == 5 and all(record.exc_info is not None for record in said), "one for each bar, with its stack"
    assert {(record.strategy_instance_id, record.underlying) for record in said} == {(SID, "SPY")}


# ---- A wait is given no longer than the decision has left -------------------------------


async def _with_a_tenth_of_a_second_left(floor: Floor) -> None:
    """The bar reaches the bot 19.9 seconds after it closed. A decision is on time for twenty."""
    await floor.bar_arrives(ENTER_MS + 19_900)


async def test_an_entry_whose_rate_is_still_being_read_when_its_time_is_up_is_refused_as_that_and_searches_nothing(
    floor: Floor,
) -> None:
    """The vendors do not answer the decision's own read in the tenth of a second it has left. The wait ends
    with the decision's time, not with the vendors: one refused decision, by that name."""
    vendors = threading.Event()

    def vendors_that_do_not_answer(asked_as_of_ms: int) -> None:
        if asked_as_of_ms > ENTER_MS:
            vendors.wait(3)

    floor.while_inputs_are_read = vendors_that_do_not_answer
    try:
        ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS), before=_with_a_tenth_of_a_second_left)])
    finally:
        vendors.set()

    [(reason_code, why)] = ran.blocked()
    assert reason_code == "OPTION_ENTRY_PRICING_INPUTS_LATE"
    assert why.strip(), "the refusal says nothing in words"
    assert (floor.worker.asked, floor.asked_of_the_clerk) == ([], [])
    _nothing_was_sent(floor, ran)


async def test_an_entry_whose_search_has_not_answered_when_its_time_is_up_is_refused_as_that_and_sends_nothing(
    floor: Floor,
) -> None:
    """The search's worker is busy, and the decision has a tenth of a second left. Nothing waits past it."""
    floor.worker = busy = SearchesLate()
    try:
        ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS), before=_with_a_tenth_of_a_second_left)])
    finally:
        busy.let_go()

    [(reason_code, why)] = ran.blocked()
    assert reason_code == "OPTION_ENTRY_SEARCH_LATE"
    assert why.strip(), "the refusal says nothing in words"
    assert (len(busy.asked), floor.asked_of_the_clerk) == (1, [])
    _nothing_was_sent(floor, ran)


# ---- The bot's two limits, each in its own place ----------------------------------------


def _the_call_is_quoted_nine_percent_wide(floor: Floor) -> None:
    """Bid $4.00, offered at $4.38: the gap is 9.07% of the mid, and buying and selling at once would cost
    8.7% of what the entry sets aside."""
    floor.quote(585, "4.00", "4.38")


async def test_a_market_wider_than_the_gap_limit_alone_is_refused_for_its_gap(floor: Floor) -> None:
    """Under the terms a Deploy starts from, a leg's gap may be 8% and the round trip may cost 10%. This
    market is over the first and inside the second, and the refusal names the gap."""
    ran = await floor.run(_entry_then_three_bars(_the_call_is_quoted_nine_percent_wide))

    assert [code for code, _ in ran.blocked()] == [StructureMoneyRefusal.LEG_GAP_OVER_LIMIT.value]
    assert floor.asked_of_the_clerk == ["ENTER"]
    _nothing_was_sent(floor, ran)


async def test_a_round_trip_dearer_than_the_cost_limit_alone_is_refused_for_its_cost(
    floor: Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bot deployed with a gap limit of 50% and a cost limit of 5%. The same market is inside the first and
    over the second, and the refusal names the cost: each limit is handed to the Clerk as itself."""
    monkeypatch.setattr(
        bot_option_run, "option_order_terms",
        lambda _binding: OptionOrderTerms(leg_gap_limit_percent=Decimal(50), cost_limit_percent=Decimal(5)),
    )

    ran = await floor.run(_entry_then_three_bars(_the_call_is_quoted_nine_percent_wide))

    assert [code for code, _ in ran.blocked()] == [StructureMoneyRefusal.COST_OVER_LIMIT.value]
    assert floor.asked_of_the_clerk == ["ENTER"]
    _nothing_was_sent(floor, ran)


async def test_a_pick_listed_with_another_multiplier_is_refused_by_name_before_the_clerk_is_asked(
    floor: Floor,
) -> None:
    """The resolver picks standard contracts only, so this answer is made by hand: the search comes back
    saying the call multiplies by 10. The runner refuses it from the listing, and asks the Clerk nothing."""

    def listed_with_ten(found: Any) -> Any:
        # A worker's answer to a search that came to contracts carries each pick's listed multiplier.
        return replace(found, multipliers={LEG: 10}) if hasattr(found, "multipliers") else found

    floor.worker = AnswersWith(listed_with_ten)

    ran = await floor.run(_entry_then_three_bars())

    assert [code for code, _ in ran.blocked()] == [OptionEffectRefusal.CONTRACT_NOT_STANDARD.value]
    assert (len(floor.worker.asked), floor.asked_of_the_clerk) == (1, [])
    _nothing_was_sent(floor, ran)


async def test_an_entry_whose_quote_grew_too_old_before_its_order_was_sent_buys_nothing_and_the_run_goes_on(
    floor: Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Clerk takes the entry on a quote a decision could use, and the process then stands still for five
    seconds before the order is reached for sending. The Clerk tests the quote again at that clock and sends
    nothing. The runner discards the decision as it does after any refusal at the send: nothing is bought,
    the journal lists no purchase, and the bars after it are decided on as usual."""
    take = clerk_runtime.accept_option_enter

    def take_and_stand_still(*args: Any, **kwargs: Any) -> Any:
        taken = take(*args, **kwargs)
        floor.clock.value += 5_001
        return taken

    monkeypatch.setattr(clerk_runtime, "accept_option_enter", take_and_stand_still)

    ran = await floor.run(_entry_then_three_bars())

    assert floor.asked_of_the_clerk == ["ENTER"]
    assert floor.transitions("ENTER_SUBMISSION_REFUSED") == 1
    assert floor.simulated_fills() == [] and floor.holds() == {} and ran.journal == []
    assert floor.clerk.option_fills_of_decision(SID, _decisions(ran)["enter_intent"]) == ()
    assert [outcome for outcome, _ in ran.outcomes()][2:] == ["no_action", "no_action", "no_action"]


# ---- On its contract's expiry day, an entry has a last moment ---------------------------

# The call expires on Friday, whose session closes at 16:00. An entry is opened only when its whole life
# ends 45 minutes before that: from 15:13:40 none is.
AT_1513 = scenes._at(FRIDAY, 15, 13)


def _entered_on_the_bar_closing_at(enter_ms: int) -> list[Step]:
    return [Step(green(enter_ms - MINUTE)), Step(green(enter_ms))]


async def _on_friday_afternoon(on_the_floor: Callable[..., Floor]) -> Floor:
    return await on_the_floor(starts_at_ms=AT_1513 - 3 * MINUTE).open()


async def test_on_its_expiry_day_a_call_is_still_bought_on_the_last_bar_before_the_entry_cutoff(
    on_the_floor: Callable[..., Floor],
) -> None:
    """Decided on the bar that closed at 15:13:00, forty seconds before the cutoff."""
    floor = await _on_friday_afternoon(on_the_floor)

    ran = await floor.run(_entered_on_the_bar_closing_at(AT_1513))

    assert ran.blocked() == []
    assert floor.fills() == [(CALL, "buy", 1.0, 4.12, 100, Decimal("412.00"))]


async def test_on_its_expiry_day_an_entry_after_the_cutoff_is_refused_by_the_runner_and_never_reaches_the_clerk(
    on_the_floor: Callable[..., Floor],
) -> None:
    """Decided on the bar that closed at 15:14:00. It could not be closed again in time, so nothing is opened."""
    floor = await _on_friday_afternoon(on_the_floor)

    ran = await floor.run(_entered_on_the_bar_closing_at(AT_1513 + MINUTE))

    [(reason_code, why)] = ran.blocked()
    assert reason_code == OptionEffectRefusal.PAST_ENTRY_CUTOFF.value
    assert why.strip(), "the refusal says nothing in words"
    assert (len(floor.worker.asked), floor.asked_of_the_clerk) == (1, [])
    _nothing_was_sent(floor, ran)


async def test_the_clerk_refuses_an_entry_past_the_cutoff_at_its_own_clock_though_the_runners_recheck_passed(
    on_the_floor: Callable[..., Floor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bot whose own terms put the cutoff at 15:13:01, a second after its decision bar closes. Its runner's
    recheck passes, three tenths of a second after that close. The entry reaches the Clerk a second later.
    The Clerk asks the cutoff itself, at its own clock and under the terms the runner handed it, and
    refuses: one refused decision, written once, and nothing sent."""
    terms = CloseTerms(entry_margin_ms=69_000)
    monkeypatch.setattr(bot_option_run, "option_order_terms", lambda _binding: OptionOrderTerms(close=terms))
    floor = await _on_friday_afternoon(on_the_floor)
    ask = floor.clerk.execute_for_instance

    async def a_second_later(**effect: Any) -> Any:
        floor.clock.value += 1_000
        return await ask(**effect)

    monkeypatch.setattr(floor.clerk, "execute_for_instance", a_second_later)

    ran = await floor.run([*_entered_on_the_bar_closing_at(AT_1513), Step(red(AT_1513 + MINUTE))])

    assert [code for code, _ in ran.blocked()] == [OptionEffectRefusal.PAST_ENTRY_CUTOFF.value]
    assert [outcome for outcome, _ in ran.outcomes()] == ["no_action", "blocked", "no_action"]
    assert floor.asked_of_the_clerk == ["ENTER"]
    _nothing_was_sent(floor, ran)


# ---- Lateness ---------------------------------------------------------------------------


async def test_an_entry_whose_time_runs_out_while_the_rate_is_read_is_refused_as_late_and_searches_nothing(
    floor: Floor,
) -> None:
    """The vendors took twenty-five seconds over the decision's own read. A decision is on time for twenty."""

    def a_slow_vendor(asked_as_of_ms: int) -> None:
        # The read the decision itself waits for, not the one made in the background as the bar closed.
        if asked_as_of_ms > ENTER_MS:
            floor.clock.value += 25_000

    floor.while_inputs_are_read = a_slow_vendor

    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])

    [(reason_code, why)] = ran.blocked()
    assert reason_code == "DECISION_LATE"
    assert why.strip(), "a decision that ran out of time on the way says so in words"
    assert floor.worker.asked == []
    _nothing_was_sent(floor, ran)


async def test_lateness_is_judged_again_after_the_search_and_the_recheck(floor: Floor) -> None:
    """The bar arrived 15.2 seconds after its close, on time. The search took 4.9 more. The picks' quotes are
    still young enough to use, and the Clerk would take them; the decision is 20.1 seconds old, and is not sent."""

    async def late_but_on_time(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + 15_200)

    def the_search_takes_a_while() -> None:
        floor.clock.value += 4_900

    floor.worker.during = the_search_takes_a_while

    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS), before=late_but_on_time)])

    assert [code for code, _ in ran.blocked()] == ["DECISION_LATE"]
    assert floor.what_the_resolver_picks().picked == {LEG: CALL_585}
    _nothing_was_sent(floor, ran)


async def test_an_entry_is_sent_when_the_search_leaves_it_just_on_time(floor: Floor) -> None:
    """The same search from a bar that arrived a tenth of a second sooner: 20.0 seconds, and it is sent."""

    async def a_little_sooner(floor: Floor) -> None:
        await floor.bar_arrives(ENTER_MS + 15_100)

    def the_search_takes_a_while() -> None:
        floor.clock.value += 4_900

    floor.worker.during = the_search_takes_a_while

    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS), before=a_little_sooner)])

    assert ran.blocked() == []
    (bought,) = floor.simulated_fills()
    assert (bought.fill_price, bought.quote_age_ms) == (Decimal("4.12"), 4_900)
    # The quotes were tested again when the search answered, not as of when it began.
    assert floor.entry_as_the_clerk_kept_it().rechecked_at_ms == ENTER_MS + 15_100 + 4_900


async def test_quotes_that_went_stale_while_the_search_ran_are_refused_by_the_recheck(floor: Floor) -> None:
    """The search took 5.1 seconds and nothing ticked meanwhile. The picks were made on quotes a decision
    could use; by the time the order would go they are too old, and the recheck says so before the Clerk is
    asked."""

    def the_search_takes_a_while() -> None:
        floor.clock.value += 5_100

    floor.worker.during = the_search_takes_a_while

    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])

    assert floor.what_the_resolver_picks().picked == {LEG: CALL_585}
    assert [code for code, _ in ran.blocked()] == ["OPTION_CONTRACT_UNDERLYING_QUOTE_UNUSABLE"]
    _nothing_was_sent(floor, ran)


async def test_the_entry_is_priced_from_the_quote_as_it_stood_when_the_search_answered(floor: Floor) -> None:
    """The call was offered at 4.12 when the search began and at 4.14 when it answered. The Clerk is handed
    the later quote, and the simulation fills at it."""
    floor.worker.during = lambda: floor.quote(585, "4.10", "4.14")

    await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])

    assert floor.what_the_resolver_picks().quotes[LEG].ask.price == Decimal("4.12")
    assert floor.fills() == [(CALL, "buy", 1.0, 4.14, 100, Decimal("414.00"))]


# ---- The start of a run -----------------------------------------------------------------


async def test_a_bot_whose_window_cannot_fit_the_clerks_line_budget_is_refused_as_its_run_starts(
    on_the_floor: Callable[..., Floor],
) -> None:
    """One call leg watched two strikes either side is five lines. With four, its contracts could never all be
    quoted, and every entry would be refused. The run is refused once, by name, before it asks for a bar."""
    floor = await on_the_floor(line_budget=4).open()

    with pytest.raises(OptionPlanRefused) as refused:
        await floor.run([Step(green(FIRST_GREEN_MS))])

    assert refused.value.reason is OptionEffectRefusal.WINDOW_OVER_LINE_BUDGET
    assert floor.feed is not None and floor.feed.symbols_asked_for == []
    assert floor.watch.requests() == () and floor.receipts() == []


SHARED_STATUS = "http://status-owner:8000/api/brokers/alpaca/market-status-snapshot"


async def test_on_a_clerk_that_reads_its_market_status_from_a_shared_source_an_option_bot_is_refused_as_its_run_starts(
    on_the_floor: Callable[..., Floor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Such a Clerk makes no IBKR pass of its own, and that pass is what quotes option contracts. Nothing could
    ever be picked or priced on it. The run is refused once, by a name of its own and in words, before it
    asks for a bar, a quote line or a search worker: not at every entry, as a feed that is down. That is
    the reason given though this Clerk's line budget is too small for the bot's window as well: no budget
    would help."""
    floor = await on_the_floor(status_from=SHARED_STATUS, watch=get_option_watch_list(), line_budget=4).open()
    searches = OptionSearches(executor=lambda: floor.worker)
    monkeypatch.setattr(option_search, "_searches", searches)
    set_market_liveness_consumer(floor.consumer)
    try:
        with pytest.raises(OptionPlanRefused) as refused:
            await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))], the_process_own_market=True)
        worker_started = searches.entry.worker_pid
    finally:
        set_market_liveness_consumer(None)
        searches.close()

    assert floor.consumer.option_quotes is None
    assert refused.value.reason is OptionEffectRefusal.NO_OPTION_QUOTES_HERE
    assert refused.value.detail.strip(), "the refusal says nothing in words"
    assert floor.feed is not None and floor.feed.symbols_asked_for == []
    assert (get_option_watch_list().requests(), worker_started) == ((), None)
    assert floor.receipts() == [] and floor.orders_on_the_books() == 0


async def test_on_a_clerk_that_reads_its_market_status_from_a_shared_source_the_keeping_does_nothing_and_nothing_raises(
    on_the_floor: Callable[..., Floor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An account on such a Clerk still holds a call, bought when the Clerk had a connection of its own. No pass
    of a supervisor comes to begin a round, so the books are not read, no line is asked for, and no mark is
    written. The stock status goes on being read from the shared source, and closing the Clerk raises nothing."""
    floor = await on_the_floor(status_from=SHARED_STATUS).open()
    reads: list[int] = []

    def books() -> tuple[str, ...]:
        reads.append(floor.clock.value)
        return (CALL,)

    kept = KeptOptionQuotes.of_account(artifacts_root=floor.tmp_path, account_id="sim:holds-a-call")
    floor.held.follow("sim:holds-a-call", books=books, kept=kept)
    status_reads: list[str] = []

    async def shared(url: str, **_: Any) -> MarketStatusSnapshot:
        status_reads.append(url)
        now = floor.clock.value
        return MarketStatusSnapshot(
            source=MarketStatusSource.IBKR, connected=True, observed_at_ms=now, connection_changed_at_ms=now,
            symbol_statuses=(), quotes=(),
        )

    monkeypatch.setattr("app.broker.alpaca.market_liveness.read_shared_market_status", shared)
    tasks_before = asyncio.all_tasks()

    for _ in range(20):
        floor.clock.value += 1_000
        await floor.consumer.refresh_shared_status()
    rounds_begun = asyncio.all_tasks() - tasks_before
    await floor.held.settled()

    assert len(status_reads) == 20 and floor.store.status_snapshot(now_ms=floor.clock.value).connected
    assert (reads, rounds_begun, floor.held.requests()) == ([], set(), ())
    assert floor.ibkr.open_option_lines() == set() and kept.newest(CALL) is None
    await floor.consumer.stop()
    assert reads == [], "closing the Clerk's status source read no books either"


async def test_a_bot_whose_window_just_fits_the_budget_runs(on_the_floor: Callable[..., Floor]) -> None:
    floor = await on_the_floor(line_budget=5).open()

    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])

    assert ran.blocked() == []
    assert floor.holds() == {CALL: 1.0}


# ---- An EXIT is sent whatever the quote ------------------------------------------------


def _what_the_exit_was_handed(floor: Floor) -> dict[str, Any] | None:
    """The quotes the runner handed the Clerk with the bot's EXIT, as the EXIT's own acceptance kept them."""
    with floor.repo._write_lock:
        (facts,) = floor.repo._conn.execute(
            "SELECT facts_json FROM custody_transitions WHERE transition_kind = 'EXIT_ACCEPTED'"
        ).fetchall()
    return json.loads(facts[0]).get("option_exit")


def _round_trip_whose_exit_arrives(as_the_exit_arrives: Callable[[Floor], Any]) -> list[Step]:
    """The ENTER at 10:31. The call is quoted afresh each minute until the EXIT's bar, which finds what the
    test says it finds."""

    async def exit_bar(floor: Floor) -> None:
        await floor.bar_arrives(EXIT_MS + DELIVERY_MS, quotes=False)
        as_the_exit_arrives(floor)

    return [
        Step(green(FIRST_GREEN_MS)),
        Step(green(ENTER_MS)),
        Step(red(ENTER_MS + MINUTE)),
        Step(red(ENTER_MS + 2 * MINUTE)),
        Step(red(EXIT_MS), before=exit_bar),
    ]


async def test_an_exit_whose_contract_was_last_quoted_a_minute_ago_is_sold_at_that_quote_and_says_its_age(
    floor: Floor,
) -> None:
    """The call has not ticked since the bar before. The runner hands over what it has; the Clerk sells at its
    bid and says the quote was a minute old."""
    ran = await floor.run(_round_trip_whose_exit_arrives(lambda _floor: None))

    handed = _what_the_exit_was_handed(floor)
    _bought, sold = floor.simulated_fills()
    assert handed is not None and list(handed["quotes"]) == [CALL]
    assert (sold.fill_model, sold.fill_price, sold.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("4.08"), MINUTE,
    )
    assert floor.holds() == {}
    assert [(row.intent, row.fill_model, row.quote_age_ms) for row in ran.journal][-1] == (
        "EXIT", "natural_price_older_quote", MINUTE,
    )


def _only_delayed_data(floor: Floor) -> None:
    floor.ibkr.lines[CALL].marketDataType = 3
    floor.ibkr.tick(CALL, bid=9.00, ask=9.04)


def _no_quote_source_any_more(floor: Floor) -> None:
    floor.quote_source = lambda: None


@pytest.mark.parametrize(
    "as_the_exit_arrives",
    [
        pytest.param(_only_delayed_data, id="the line has only delayed data"),
        pytest.param(_no_quote_source_any_more, id="the process has lost its quote source"),
    ],
)
async def test_an_exit_with_no_quote_to_hand_over_is_still_sent_and_priced_from_the_last_one_kept(
    floor: Floor, as_the_exit_arrives: Callable[[Floor], None]
) -> None:
    """An EXIT is never held back for want of a quote. The Clerk kept the call's quote a minute ago, with no
    runner handing it over, and sells at that bid."""
    await floor.run(_round_trip_whose_exit_arrives(as_the_exit_arrives))

    _bought, sold = floor.simulated_fills()
    assert _what_the_exit_was_handed(floor) is None
    assert (sold.fill_model, sold.fill_price, sold.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("4.08"), MINUTE,
    )
    assert floor.holds() == {}


async def test_an_exit_nobody_bids_for_sells_nothing_and_the_run_goes_on_holding(floor: Floor) -> None:
    """The simulation records no sale for nothing. The contract stays on the books, the journal lists no sale,
    and the run does not end."""
    ran = await floor.run([*_round_trip(sells_at=("0", "0.05")), Step(red(EXIT_MS + MINUTE))])

    assert floor.holds() == {CALL: 1.0}
    assert [row.intent for row in ran.journal] == ["ENTER"]
    assert [fill[1] for fill in floor.fills()] == ["buy"]
    assert ran.outcomes()[-1][1] == EXIT_MS + MINUTE, "the bar after the refused EXIT was decided on too"


async def _the_data_drops_and_comes_back(floor: Floor, *, by_ms: int | None = None) -> None:
    """IBKR's data farm drops and comes back with every request kept alive. The feed's generation moves, so
    each side of a line waits for a tick of its own, and nobody quotes anything until a test does."""
    floor.new_lines_are_answered = False
    floor.ibkr.client.connection_state = "soft_lost"
    if by_ms is None:
        await floor.time_passes_to(floor.clock.value + 15_000)
    else:
        await floor.supervisor_passes(1)
    floor.ibkr.client.connection_state = "connected"
    floor.ibkr.client.connectivity_lost_count += 1
    floor.ibkr.client.last_ibkr_code = 1102
    await floor.time_passes_to(floor.clock.value + 15_000 if by_ms is None else by_ms)


async def test_an_exit_whose_call_shows_only_an_ask_since_a_data_loss_sells_nothing_and_the_next_bar_sells_it(
    floor: Floor,
) -> None:
    """IBKR's data came back twenty seconds before the EXIT's bar, and since then only the call's ask has
    ticked. The runner hands over the line as it stands, and that quote is the newest kept. Nothing on it
    says what is bid, and no older quote stands in for it: the close is not priced, the owner is told the
    position was not closed, and the bot goes on holding. The Clerk's own keeping states no quote with one
    side as a mark, so nothing it wrote hides that. On the next bar the call is quoted whole, and the EXIT
    sells it."""
    seen: dict[str, Any] = {}

    async def exit_bar(floor: Floor) -> None:
        await floor.bar_arrives(EXIT_MS - 20_000, quotes=False)
        floor.new_lines_are_answered = False
        floor.ibkr.client.connection_state = "soft_lost"
        await floor.supervisor_passes(1)
        floor.ibkr.client.connection_state = "connected"
        floor.ibkr.client.connectivity_lost_count += 1
        floor.ibkr.client.last_ibkr_code = 1102
        await floor.time_passes_to(EXIT_MS + DELIVERY_MS)
        floor.ibkr.now = floor.clock.value
        floor.ibkr.tick(CALL, ask=4.34)
        await floor.account_is_read()

    async def the_bar_after(floor: Floor) -> None:
        kept, now = floor.kept_quotes(), floor.clock.value
        seen["held"] = floor.holds()
        seen["newest kept"] = kept.newest(CALL, at_or_before_ms=now)
        seen["last stated with a bid"] = kept.last_stated(CALL, at_or_before_ms=now, with_side="bid")
        seen["can be sold"] = kept.prices_a_close(CALL, side="sell", at_ms=now)
        seen["not closed"] = floor.transitions("EXIT_NOT_FLAT")
        await floor.bar_arrives(EXIT_MS + MINUTE + DELIVERY_MS)

    script = _round_trip()
    script[-1] = Step(red(EXIT_MS), before=exit_bar)
    ran = await floor.run([*script, Step(red(EXIT_MS + MINUTE), before=the_bar_after)])

    assert ran.outcomes()[-2:] == [("exit_intent", EXIT_MS), ("exit_intent", EXIT_MS + MINUTE)]
    assert (seen["held"], seen["can be sold"], seen["not closed"]) == ({CALL: 1.0}, False, 1)
    assert (seen["newest kept"].quote.bid, seen["newest kept"].quote.ask) == (None, Decimal("4.34"))
    # The last whole quote stated for the call is from before the data loss: the keeping stated none after.
    assert seen["last stated with a bid"].kept_at_ms < EXIT_MS - 20_000
    assert [fill[:4] for fill in floor.fills()] == [(CALL, "buy", 1.0, 4.12), (CALL, "sell", 1.0, 4.08)]
    assert floor.holds() == {} and [row.intent for row in ran.journal] == ["ENTER", "EXIT"]


async def test_an_exit_whose_call_shows_only_a_bid_since_a_data_loss_sells_at_that_bid_and_says_how_old_it_is(
    floor: Floor,
) -> None:
    """The other way round: since IBKR's data came back only the call's bid has ticked, at $2.00. That is what
    the call can be sold for now. The EXIT sells at it, and not at the $4.08 bid from before the loss, and
    the sale says it was priced from a quote no decision could have used."""

    async def exit_bar(floor: Floor) -> None:
        await floor.bar_arrives(EXIT_MS - 20_000, quotes=False)
        await _the_data_drops_and_comes_back(floor, by_ms=EXIT_MS + DELIVERY_MS)
        floor.ibkr.now = floor.clock.value
        floor.ibkr.tick(CALL, bid=2.00)
        await floor.account_is_read()

    script = _round_trip()
    script[-1] = Step(red(EXIT_MS), before=exit_bar)
    ran = await floor.run(script)

    _bought, sold = floor.simulated_fills()
    assert (sold.fill_model, sold.fill_price, sold.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("2"), 0,
    )
    assert floor.holds() == {} and [row.intent for row in ran.journal] == ["ENTER", "EXIT"]


# ---- The Clerk keeps quotes for what it holds, runner or no runner ----------------------


async def _bought_and_the_runner_gone(floor: Floor) -> None:
    """The bot buys its call at 10:31 and its Signal stream ends: no runner is alive, and the bot holds."""
    await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])
    assert floor.holds() == {CALL: 1.0}
    assert floor.watch.requests() == (), "the runner's own request went with it"
    # From here the call ticks as a busy contract does, and only the Clerk's own keeping reads it.
    floor.option_lines_tick = True


async def test_with_the_runner_gone_the_held_call_is_still_quoted_and_the_stop_sells_it_at_a_quote_seconds_old(
    floor: Floor,
) -> None:
    """Five minutes after the runner went the call is bid $4.50. The line is still open, because the Clerk's
    own books ask for it, and the close of what the run left sells at $4.50 from a quote kept 14.7 seconds
    before: not at the $4.08 the runner last saw."""
    await _bought_and_the_runner_gone(floor)
    floor.markets[585] = ("4.50", "4.54")

    await floor.time_passes_to(ENTER_MS + 5 * MINUTE)
    lines_with_no_runner = floor.ibkr.open_option_lines()
    await floor.stop_is_pressed()

    (sale,) = floor.sales_the_simulation_made()
    assert lines_with_no_runner == {CALL}
    assert (sale.fill_model, sale.fill_price, sale.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("4.5"), 14_700,
    )
    # Flat, and its Clerk closed: nobody holds the contract, so nobody asks for its line.
    assert floor.runtime is None and floor.held.held() == {}
    await floor.supervisor_passes(1)
    assert floor.ibkr.open_option_lines() == set()


async def test_a_call_the_stop_could_not_sell_is_still_quoted_with_its_clerk_closed_and_is_sold_when_bid_again(
    floor: Floor,
) -> None:
    """Nothing is bid when the owner presses Stop, so nothing is sold, and the Dry Run's Clerk is closed with
    the contract on its books. The process goes on keeping the call's quotes. When it is bid again that quote
    is in the account's file, and the pass the next look at the bot runs sells at it."""
    await _bought_and_the_runner_gone(floor)
    floor.markets[585] = ("0", "0.05")
    await floor.time_passes_to(ENTER_MS + 2 * MINUTE)

    await floor.stop_is_pressed()
    not_sold = floor.sales_the_simulation_made()
    clerk_closed = floor.runtime is None
    still_held = floor.held.held()
    floor.markets[585] = ("4.50", "4.54")
    await floor.time_passes_to(ENTER_MS + 6 * MINUTE)
    line_with_no_clerk = floor.ibkr.open_option_lines()
    can_be_sold = floor.kept_quotes().prices_a_close(CALL, side="sell", at_ms=floor.clock.value)
    async with floor.authority.runtime_for_projection():
        pass

    assert (not_sold, clerk_closed, still_held) == ([], True, {ACCOUNT: (CALL,)})
    assert (line_with_no_clerk, can_be_sold) == ({CALL}, True)
    (sale,) = floor.sales_the_simulation_made()
    assert (sale.fill_model, sale.fill_price) == (SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("4.5"))
    assert sale.quote_age_ms is not None and sale.quote_age_ms <= KEEP_EVERY_MS
    assert floor.held.held() == {}


async def _until_the_calls_line_is_open(floor: Floor) -> None:
    """A line with no whole quote is given up and asked for again, a little later each time. Wait for the
    held call's to be open, for ten minutes at most: a call nobody asks for any more never has one."""
    for _ in range(40):
        if CALL in floor.ibkr.open_option_lines():
            return
        await floor.time_passes_to(floor.clock.value + 15_000)
    pytest.fail("the held call's line was never asked for again")


async def _the_call_is_quoted_whole_again(floor: Floor, bid: str, ask: str) -> None:
    """Once the call's line is open again both sides tick, and the Clerk's keeping states the quote within
    its fifteen seconds."""
    await _until_the_calls_line_is_open(floor)
    floor.quote(585, bid, ask)
    await floor.time_passes_to(floor.clock.value + 15_000)


async def test_with_no_runner_a_call_that_shows_only_an_ask_since_a_data_loss_is_not_sold_by_the_stop_until_it_is_quoted_whole(
    floor: Floor,
) -> None:
    """Bought at $4.12 with $4.08 bid, and the run ends. IBKR's data drops and comes back, and since then only
    the call's ask has ticked, at $2.04: nothing says what is bid. Five minutes on the owner presses Stop.
    With no runner alive the Clerk's own keeping has put the line's last word in the account's file, as a
    runner's EXIT would have. So the Stop sells nothing, least of all at the $4.08 from before the loss, and
    says the close could not be priced; no Flatten is offered. The call is then quoted whole again at $2.00
    bid, and once the Clerk has let the failed close settle, the next look at the bot sells it at $2.00."""
    await _bought_and_never_requoted(floor)
    await floor.time_passes_to(ENTER_MS + MINUTE)
    await _the_data_drops_and_comes_back(floor)
    floor.ibkr.now = only_the_ask_ticked_at = floor.clock.value
    floor.ibkr.tick(CALL, ask=2.04)

    await floor.time_passes_to(floor.clock.value + 5 * MINUTE)
    kept, now = floor.kept_quotes(), floor.clock.value
    newest = kept.newest(CALL, at_or_before_ms=now)
    flatten_offered = kept.prices_a_close(CALL, side="sell", at_ms=now)
    await floor.stop_is_pressed()
    sold_by_the_stop = floor.sales_the_simulation_made()
    still_held = floor.held.held()
    told = await floor.the_owner_looks_at_the_bot()
    sold_by_the_look = floor.sales_the_simulation_made()
    await _the_call_is_quoted_whole_again(floor, "2.00", "2.04")
    quoted_whole_at = floor.clock.value - 15_000
    # The Clerk tries a close that failed again only once it has let the failure settle: two minutes.
    await floor.time_passes_to(now + 3 * MINUTE)
    await floor.the_owner_looks_at_the_bot()

    assert newest is not None and (newest.quote.bid, newest.quote.ask) == (None, Decimal("2.04"))
    assert newest.kept_at_ms == only_the_ask_ticked_at, "it is put back as of its own receipt, never as a mark of now"
    assert (flatten_offered, sold_by_the_stop, still_held) == (False, [], {ACCOUNT: (CALL,)})
    assert told is not None and "OPTION_EXIT_UNPRICED" in told and sold_by_the_look == []
    (sale,) = floor.sales_the_simulation_made()
    assert (sale.fill_model, sale.fill_price) == (SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("2"))
    assert sale.quote_age_ms == floor.clock.value - quoted_whole_at
    assert floor.held.held() == {}


async def test_with_no_runner_a_call_that_shows_only_a_bid_since_a_data_loss_is_no_mark_and_the_stop_sells_at_that_bid(
    floor: Floor,
) -> None:
    """The other way round: only the bid has ticked since the data came back, at $2.00. A quote with one
    side is never the account's mark, not even for its bid: the account reads as it would with nothing put
    back, at the $4.08 stated before the loss while that statement stands, and not at all once it has
    lapsed. The $2.00 is what the call can be sold for all the same: the Stop sells at it, as a runner's
    EXIT handing over the same line does, and never at the $4.08."""
    await _bought_and_never_requoted(floor)
    await floor.time_passes_to(ENTER_MS + MINUTE)
    stated_before = floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid")
    await _the_data_drops_and_comes_back(floor)
    floor.ibkr.now = only_the_bid_ticked_at = floor.clock.value
    floor.ibkr.tick(CALL, bid=2.00)
    await floor.time_passes_to(floor.clock.value + 15_000)

    put_back = floor.kept_quotes().newest(CALL, at_or_before_ms=floor.clock.value)
    while_the_old_statement_stands = _reading(floor).unrealized_pl_usd
    await floor.time_passes_to(floor.clock.value + 45_000)
    # The bid ticked 60 seconds ago: fresh enough to be a mark, if a quote with one side could be one.
    with pytest.raises(SimulationEvidenceUnavailable, match=CALL):
        _reading(floor)
    await floor.stop_is_pressed()

    assert stated_before is not None and put_back is not None
    assert (put_back.quote.bid, put_back.quote.ask, put_back.kept_at_ms) == (Decimal("2"), None, only_the_bid_ticked_at)
    assert while_the_old_statement_stands == Decimal("-4"), "the account was marked at the one-sided quote's bid"
    (sale,) = floor.sales_the_simulation_made()
    assert (sale.fill_model, sale.fill_price) == (SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("2"))
    assert sale.quote_age_ms == floor.clock.value - only_the_bid_ticked_at
    assert floor.held.held() == {}


async def test_a_session_that_ends_with_the_runner_silent_still_has_its_mark_at_the_close(floor: Floor) -> None:
    """Bought at $4.12 in the morning; the runner says nothing more all day. In the last minutes the call is
    bid $4.20, and the Clerk hands itself that quote every fifteen seconds up to the close. Wednesday starts
    from Tuesday's close: $1,000.00, $8.00 up on the call, less six cents of fees."""
    await _bought_and_the_runner_gone(floor)
    close = session_close_ms_utc(TUESDAY)

    await floor.hours_pass_to(close - 2 * MINUTE)
    floor.markets[585] = ("4.20", "4.24")
    await floor.time_passes_to(close + 30_000)
    marked_at_the_close = floor.kept_quotes().last_stated(CALL, at_or_before_ms=close, with_side="bid")
    await floor.hours_pass_to(scenes._at(WEDNESDAY, 9, 31))
    floor.markets[585] = ("4.60", "4.64")
    await floor.time_passes_to(floor.clock.value + 15_000)
    now = floor.clock.value
    reading = SimulatedAccountProjection(repo=floor.repo, artifacts_root=floor.tmp_path).observe(
        reference_cash=0, observed_at_ms=now, now_ms=now
    )

    assert marked_at_the_close is not None
    assert (marked_at_the_close.quote.bid, marked_at_the_close.kept_at_ms) == (Decimal("4.2"), close)
    assert reading.last_equity_usd == Decimal("1000") + Decimal("8") - Decimal("0.06")
    assert reading.unrealized_pl_usd == Decimal("48")


async def test_after_a_restart_the_close_made_as_the_account_opens_is_priced_from_the_last_quote_kept_and_says_its_age(
    on_the_floor: Callable[..., Floor],
) -> None:
    """The Stop could sell nothing, and the call was bid $4.50 again before the process stopped. Two hours
    later the process starts and its restoration opens the account: the call is then bid $2.00, and no quote
    has been kept since the restart, because nothing asked for the line while the account was closed. The
    close made as the account opens is priced from the newest quote there is, $4.50, and says what that is:
    an older quote, and how old. A simulated close may use the newest quote kept; it never passes it off as
    a price of now."""
    before = await on_the_floor().open()
    await _bought_and_the_runner_gone(before)
    before.markets[585] = ("0", "0.05")
    await before.time_passes_to(ENTER_MS + 2 * MINUTE)
    await before.stop_is_pressed()
    not_sold = before.sales_the_simulation_made()
    before.markets[585] = ("4.50", "4.54")
    await before.time_passes_to(ENTER_MS + 4 * MINUTE)
    last_kept = before.kept_quotes().newest(CALL)
    assert last_kept is not None and last_kept.quote.bid == Decimal("4.5")
    # The process stops. Nothing it held in memory is left; the account's files are.
    await before.held.stop()
    await before.supervisor.close()
    reset_held_option_quotes_for_testing()

    after = on_the_floor(starts_at_ms=last_kept.received_ms + 120 * MINUTE + 12_000)
    after.running = False
    after.markets[585] = ("2.00", "2.04")
    await after.authority.ensure_recoverable()

    (sale,) = after.sales_the_simulation_made()
    assert not_sold == []
    assert (sale.fill_model, sale.fill_price, sale.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("4.5"), 120 * MINUTE + 12_000,
    )
    assert sale.filled_at_ms == after.clock.value
    assert after.holds() == {}


# ---- A quiet contract's mark ------------------------------------------------------------


async def _bought_and_never_requoted(floor: Floor) -> int:
    """The bot buys its call at 10:31 and its Signal stream ends. Nobody changes the call's bid or its offer
    again: its line stays open and silent. Returns when its two sides were last received."""
    await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])
    assert floor.holds() == {CALL: 1.0}
    return ENTER_MS + DELIVERY_MS


def _reading(floor: Floor) -> Any:
    now = floor.clock.value
    return SimulatedAccountProjection(repo=floor.repo, artifacts_root=floor.tmp_path).observe(
        reference_cash=0, observed_at_ms=now, now_ms=now
    )


async def test_a_call_nobody_requotes_all_day_is_marked_at_every_hand_over_and_at_the_close_and_the_next_day_starts_from_it(
    floor: Floor,
) -> None:
    """Bought at $4.12 with $4.08 bid, and neither side ticks again all Tuesday. $4.08 is still what the call
    could be sold for, so the Clerk states it every fifteen seconds while the session is open: the account
    can be read all day, and the close has its mark. Wednesday starts from Tuesday's close: $1,000.00, $4.00
    down on the call, less six cents of fees. Nothing is restated on Wednesday until the call ticks."""
    last_ticked = await _bought_and_never_requoted(floor)
    close = session_close_ms_utc(TUESDAY)

    await floor.time_passes_to(ENTER_MS + 10 * MINUTE)
    ten_minutes_on = floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid")
    read_ten_minutes_on = _reading(floor)
    await floor.hours_pass_to(close - 2 * MINUTE)
    await floor.time_passes_to(close + 30_000)
    at_the_close = floor.kept_quotes().last_stated(CALL, at_or_before_ms=close, with_side="bid")
    after_the_close = floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid")
    await floor.hours_pass_to(scenes._at(WEDNESDAY, 9, 31))
    await floor.time_passes_to(floor.clock.value + MINUTE)
    before_it_ticks = floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid")
    floor.quote(585, "4.60", "4.64")
    await floor.time_passes_to(floor.clock.value + 15_000)
    wednesday = _reading(floor)

    assert ten_minutes_on is not None and at_the_close is not None
    assert ENTER_MS + 10 * MINUTE - ten_minutes_on.kept_at_ms < KEEP_EVERY_MS
    assert (ten_minutes_on.quote.bid, ten_minutes_on.quote.bid_received_ms) == (Decimal("4.08"), last_ticked)
    assert read_ten_minutes_on.unrealized_pl_usd == Decimal("-4")
    assert (at_the_close.quote.bid, at_the_close.kept_at_ms) == (Decimal("4.08"), close)
    assert (at_the_close.quote.bid_received_ms, at_the_close.quote.ask_received_ms) == (last_ticked, last_ticked)
    assert after_the_close == at_the_close, "nothing is restated once the session has closed"
    assert before_it_ticks == at_the_close, "Tuesday's quote is no mark of Wednesday"
    assert wednesday.last_equity_usd == Decimal("1000") - Decimal("4") - Decimal("0.06")
    assert wednesday.unrealized_pl_usd == Decimal("48")


async def test_a_call_last_requoted_ten_minutes_ago_is_sold_at_its_standing_bid_and_the_sale_says_how_old_it_is(
    floor: Floor,
) -> None:
    """The mark kept seconds ago restates a bid received ten minutes ago. The close is priced from it, and
    says what it is: an older quote, ten minutes old, not a fresh one."""
    last_ticked = await _bought_and_never_requoted(floor)

    await floor.time_passes_to(last_ticked + 10 * MINUTE)
    stated = floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid")
    await floor.stop_is_pressed()

    (sale,) = floor.sales_the_simulation_made()
    assert stated is not None and floor.clock.value - stated.kept_at_ms < KEEP_EVERY_MS
    assert (sale.fill_model, sale.fill_price, sale.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("4.08"), 10 * MINUTE,
    )


async def test_after_a_data_loss_the_quote_the_line_held_is_no_mark_until_the_call_ticks_again(floor: Floor) -> None:
    """IBKR's data farm drops and comes back with every request kept alive. The call's line is the same line,
    and what it showed before is not stated again. Nobody quotes the call for two minutes after, though the
    line is asked for again: the mark lapses, the account says it cannot be read, and the first tick of both
    sides after the recovery is the next mark."""
    await _bought_and_never_requoted(floor)
    floor.new_lines_are_answered = False
    await floor.time_passes_to(ENTER_MS + MINUTE)
    stated_before = floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid")

    floor.ibkr.client.connection_state = "soft_lost"
    await floor.time_passes_to(floor.clock.value + 15_000)
    floor.ibkr.client.connection_state = "connected"
    floor.ibkr.client.connectivity_lost_count += 1
    floor.ibkr.client.last_ibkr_code = 1102
    await floor.time_passes_to(floor.clock.value + 15_000)
    lines_after_the_recovery = (floor.ibkr.open_option_lines(), CALL in floor.ibkr.cancelled)
    await floor.time_passes_to(floor.clock.value + 2 * MINUTE)
    stated_after = floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid")
    with pytest.raises(SimulationEvidenceUnavailable, match=CALL):
        _reading(floor)
    await _until_the_calls_line_is_open(floor)
    floor.quote(585, "4.00", "4.04")
    await floor.time_passes_to(floor.clock.value + 15_000)
    ticked = floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid")

    assert stated_before is not None and stated_after == stated_before
    assert lines_after_the_recovery == ({CALL}, False), "the line was kept through the recovery, not opened afresh"
    assert ticked is not None and (ticked.quote.bid, ticked.kept_at_ms) == (Decimal("4"), floor.clock.value)
    assert _reading(floor).unrealized_pl_usd == Decimal("-12")


async def test_delayed_data_on_a_held_calls_line_is_never_its_mark(floor: Floor) -> None:
    """The line falls back to delayed data and shows $9.00 bid. No live quote stands on it any more, so
    nothing is stated, least of all the delayed price."""
    await _bought_and_never_requoted(floor)
    await floor.time_passes_to(ENTER_MS + MINUTE)
    stated_before = floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid")

    floor.ibkr.lines[CALL].marketDataType = 3
    floor.ibkr.now = floor.clock.value
    floor.ibkr.tick(CALL, bid=9.00, ask=9.04)
    await floor.time_passes_to(floor.clock.value + 2 * MINUTE)

    assert stated_before is not None
    assert floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid") == stated_before
    assert floor.kept_quotes().newest(CALL).quote.bid == Decimal("4.08")  # type: ignore[union-attr]


async def test_a_call_bought_in_the_second_its_run_ended_is_asked_for_and_marked_from_the_next_pass_on(
    floor: Floor,
) -> None:
    """The books are read after a pass, never under it. A run that ends before any pass has read of its
    purchase leaves the contract unasked for one pass; the next asks for it, and its mark follows."""
    floor.a_pass_before_the_stream_ends = False
    await _bought_and_never_requoted(floor)
    floor.option_lines_tick = True

    await floor.supervisor_passes(2)
    await floor.time_passes_to(floor.clock.value + 15_000)

    assert [request.held for request in floor.held.requests()] == [(CALL_585,)]
    assert floor.ibkr.open_option_lines() == {CALL}
    stated = floor.kept_quotes().last_stated(CALL, at_or_before_ms=floor.clock.value, with_side="bid")
    assert stated is not None and stated.kept_at_ms == floor.clock.value


# ---- The stock quotes never wait on the option side -------------------------------------


async def _spy_is_quoted_for_a_stock_bot(floor: Floor) -> None:
    """SPY is asked for as a bot in shares asks for it, and its quote is published."""
    floor.store.request_symbol("SPY", now_ms=floor.clock.value)
    await floor.supervisor_passes(2)
    floor.spy()
    await floor.consumer.refresh_shared_status()
    assert floor.store.top_of_book("SPY", now_ms=floor.clock.value) is not None


async def _a_second_passes(floor: Floor) -> None:
    floor.clock.value += 1_000
    floor.store.request_symbol("SPY", now_ms=floor.clock.value)
    floor.spy()
    await floor.consumer.refresh_shared_status()


async def test_books_that_do_not_answer_never_hold_up_the_stock_quotes(floor: Floor) -> None:
    """A Dry Run account's books are stuck behind a lock for as long as this test lasts. SPY's quote is
    published at every pass all the same, each within the second: the pass begins the option side's round
    and does not wait for it."""
    await _spy_is_quoted_for_a_stock_bot(floor)
    stuck = threading.Event()

    def books_that_do_not_answer() -> tuple[str, ...]:
        stuck.wait(30)
        return ()

    floor.held.follow(
        "sim:stuck-books", books=books_that_do_not_answer,
        kept=KeptOptionQuotes.of_account(artifacts_root=floor.tmp_path, account_id="sim:stuck-books"),
    )
    try:
        began, published = time.monotonic(), []
        for _ in range(4):
            await _a_second_passes(floor)
            quote = floor.store.top_of_book("SPY", now_ms=floor.clock.value)
            published.append(None if quote is None else quote.observed_at_ms)
        took_s = time.monotonic() - began
        still_stuck = not stuck.is_set()
    finally:
        stuck.set()
        await floor.held.settled()

    assert still_stuck
    assert published == [floor.clock.value - 3_000, floor.clock.value - 2_000, floor.clock.value - 1_000, floor.clock.value]
    assert took_s < 5.0, "the stock quotes waited on the option side's books"


async def test_a_keeping_that_breaks_in_every_way_never_fails_the_stock_quotes(
    floor: Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The books raise, the file cannot be written, the round itself cannot be begun. A status source that
    raises blocks every entry of every bot in the process, so none of this may reach it: SPY's quote is
    published at every pass, and the connection reads as up."""
    await _spy_is_quoted_for_a_stock_bot(floor)

    def books_that_raise() -> tuple[str, ...]:
        raise RuntimeError("the books cannot be read")

    floor.held.follow(
        "sim:broken-books", books=books_that_raise,
        kept=KeptOptionQuotes.of_account(artifacts_root=floor.tmp_path, account_id="sim:broken-books"),
    )
    published = []
    for trouble in ("books that raise", "a round that fails", "a round that cannot begin"):
        if trouble == "a round that fails":
            monkeypatch.setattr(floor.held, "keep", lambda _source: 1 / 0)
        elif trouble == "a round that cannot begin":
            monkeypatch.setattr(floor.held, "_one_round", lambda _source: 1 / 0)
        await _a_second_passes(floor)
        await floor.held.settled()
        quote = floor.store.top_of_book("SPY", now_ms=floor.clock.value)
        published.append((quote is not None, floor.store.status_snapshot(now_ms=floor.clock.value).connected))

    assert published == [(True, True)] * 3


async def test_closing_the_clerks_status_source_ends_the_round_of_the_keeping_it_began(floor: Floor) -> None:
    """The process shuts its status source while a round of the keeping waits on books that do not answer.
    Closing ends that round with the supervisor that drove it. Nothing of it is left under way on the loop
    to outlive the supervisor, though the thread reading the books is still stuck."""
    stuck, reads = threading.Event(), []

    def books_that_do_not_answer() -> tuple[str, ...]:
        reads.append(floor.clock.value)
        stuck.wait(30)
        return ()

    floor.held.follow(
        "sim:stuck-books", books=books_that_do_not_answer,
        kept=KeptOptionQuotes.of_account(artifacts_root=floor.tmp_path, account_id="sim:stuck-books"),
    )
    try:
        await floor.consumer._status_snapshot_source()  # type: ignore[misc]
        await _wait_for(lambda: bool(reads))
        await floor.consumer.stop()
        await asyncio.wait_for(floor.held.settled(), timeout=1)
        still_stuck = not stuck.is_set()
    finally:
        stuck.set()

    assert still_stuck and len(reads) == 1


# ---- The search in a real worker process ------------------------------------------------


async def test_with_a_real_search_worker_process_the_bot_buys_the_call_and_sells_it(floor: Floor) -> None:
    """The same round trip with the entry lane as production has it: the search crosses to a spawned worker
    process and its answer comes back. Nothing here searches in this process."""
    floor.lane = OptionSearchLane("entry")

    ran = await floor.run(_round_trip())

    assert floor.worker.asked == [], "the stand-in worker was asked nothing"
    assert floor.lane.worker_pid not in (None, os.getpid())
    assert ran.blocked() == []
    assert floor.fills() == [
        (CALL, "buy", 1.0, 4.12, 100, Decimal("412.00")),
        (CALL, "sell", 1.0, 4.30, 100, Decimal("430.00")),
    ]
    assert floor.holds() == {}


# ---- A run refused by name is recorded by that name -------------------------------------


@pytest.mark.parametrize(
    "refusal",
    [
        OptionEffectRefusal.WINDOW_OVER_LINE_BUDGET,
        OptionEffectRefusal.ACCOUNT_NOT_OPEN,
        OptionEffectRefusal.NO_OPTION_QUOTES_HERE,
    ],
    ids=str,
)
async def test_a_run_whose_option_plan_is_refused_is_recorded_under_the_refusals_own_reason(
    tmp_path: Path, refusal: OptionEffectRefusal
) -> None:
    """Through the registry's own supervision: the bot's page says why the run could not trade its plan, in
    the refusal's code, not the name of a Python exception, and the bot is left stopped."""
    feed = _FakeFeed([_bar(_T0)], mode="crash", error=OptionPlanRefused(refusal, "its option plan cannot be traded here"))
    registry = _registry(tmp_path, feed)
    await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert (view.duty_outcome.kind, view.duty_outcome.reason_code) == ("CRASHED", refusal.value)
    assert view.desired_state == "STOPPED"


async def test_a_run_ended_by_a_refused_wait_for_its_entrys_outcome_is_recorded_under_the_refusals_own_reason(
    tmp_path: Path,
) -> None:
    """The Clerk refuses the runner's wait for what an option entry came to, and the runner settles nothing
    by itself, so the run ends. Through the registry's own supervision: the bot's page says so in the
    refusal's code, not the name of a Python exception, and the bot is left stopped."""
    refused = EntryOutcomeWaitRefused(ENTRY_OUTCOME_LIMIT_TOO_FAR, "the wait limit was too far ahead")
    registry = _registry(tmp_path, _FakeFeed([_bar(_T0)], mode="crash", error=refused))
    await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert (view.duty_outcome.kind, view.duty_outcome.reason_code) == ("CRASHED", ENTRY_OUTCOME_LIMIT_TOO_FAR)
    assert view.desired_state == "STOPPED"


async def test_an_exit_decided_after_the_close_is_held_for_the_open_and_the_journal_lists_no_sale(floor: Floor) -> None:
    """The EXIT's bar reached the bot a second after the session closed. An EXIT is never refused for being
    late, and an option order is sent only in the regular session: the Clerk holds it for the next open. No
    sale has happened, so the journal lists none, least of all the purchase a second time."""

    async def after_the_close(floor: Floor) -> None:
        await floor.hours_pass_to(session_close_ms_utc(TUESDAY) + 1_000)

    script = _round_trip()
    script[-1] = Step(red(EXIT_MS), before=after_the_close)

    ran = await floor.run(script)

    assert ran.outcomes()[-1] == ("exit_intent", EXIT_MS)
    assert floor.holds() == {CALL: 1.0}
    assert [(row.intent, row.side, row.fill_price) for row in ran.journal] == [("ENTER", "buy", 4.12)]
    assert [fill[1] for fill in floor.fills()] == ["buy"]
    # Asked what the EXIT filled, the Clerk says nothing yet. The purchase is the ENTER's, and stays its.
    decided = _decisions(ran)
    assert floor.clerk.option_fills_of_decision(SID, decided["exit_intent"]) == ()
    assert [fill.side for fill in floor.clerk.option_fills_of_decision(SID, decided["enter_intent"])] == ["buy"]


async def test_a_close_of_options_on_another_symbol_waits_for_no_bar_of_that_symbol(
    on_the_floor: Callable[..., Floor],
) -> None:
    """The bot listens to QQQ and holds a SPY call. SPY has no bar for the EXIT's minute, and the second
    stream would wait a minute for one. An option close is priced from quotes: it goes at once."""
    floor = await on_the_floor(plan=one_call(underlying="SPY"), listens_to="QQQ").open()
    started = time.monotonic()

    await floor.run(
        [
            Step(green(FIRST_GREEN_MS, "480.00", symbol="QQQ")),
            Step(green(ENTER_MS, "585.00", symbol="SPY")),
            Step(green(ENTER_MS, "480.40", symbol="QQQ")),
            Step(red(ENTER_MS + MINUTE, "480.00", symbol="QQQ")),
            Step(red(ENTER_MS + 2 * MINUTE, "479.80", symbol="QQQ")),
            Step(red(EXIT_MS, "479.60", symbol="QQQ")),
        ],
        pricing_bar_wait_ms=60_000,
    )

    assert time.monotonic() - started < 20, "the close waited for a bar of the underlying"
    assert [fill[:3] for fill in floor.fills()] == [(CALL, "buy", 1.0), (CALL, "sell", 1.0)]
    assert floor.holds() == {}


# ---- The bot's money view ----------------------------------------------------------------


async def _money_view(floor: Floor) -> Any:
    """The bot's money as its page reads it, with SPY's own stock quote published at $585.12: the figure a
    bot in shares would be priced from."""
    await floor.account_is_read()
    now = floor.clock.value
    market_liveness.get_market_liveness_store().apply_status_snapshot(
        MarketStatusSnapshot(
            source=MarketStatusSource.IBKR, connected=True, observed_at_ms=now, connection_changed_at_ms=now,
            symbol_statuses=(),
            quotes=(
                TopOfBookQuote(symbol="SPY", bid=585.10, ask=585.12, source="ibkr.market_data.status", observed_at_ms=now),
            ),
        ),
        now_ms=now,
    )
    return budget_deploy._budget_view(floor.runtime, SID)


def _lines(view: Any) -> dict[str, str]:
    return {line.label: line.amount_usd for line in view.statement}


def _claims_no_money_is_enough(view: Any) -> None:
    """The view judged no cost, so it says neither that the bot is ready, nor that its money covers an entry,
    nor that it is short of one, and it gives no figure for one."""
    words = f"{view.headline} {view.detail}"
    assert "ready" not in words.lower() and "cover" not in words.lower() and "short" not in words.lower()
    assert "585" not in words and "USD" not in words, "a share of SPY was priced as the next entry"
    assert [label for label in _lines(view) if "next entry" in label.lower()] == []


async def test_an_option_bots_money_view_prices_no_next_entry_from_a_share_of_the_underlying(floor: Floor) -> None:
    """Running and flat with $1,000.00 free, and the 585 call offered at $4.12. One contract would cost
    $412.00; a share of SPY is no price of it. The view gives no figure for the next entry and makes no
    claim that the money is enough: it says the entry is priced when its contracts are picked."""
    view = await _money_view(floor)

    assert (view.state, view.world, view.entry_eligible) == ("ready", "synthetic", True)
    _claims_no_money_is_enough(view)
    assert "picked" in view.headline and "option" in view.detail.lower()
    assert _lines(view)["Free to trade"] == "1000.00"


async def test_an_option_bot_with_a_budget_of_one_cent_is_never_said_to_be_ready(
    on_the_floor: Callable[..., Floor],
) -> None:
    """A cent buys no contract, and the view has no price to know that by. So it says nothing about the
    money being enough. The entry is then refused for its cost, with the true figure, and the view says no
    more after that than before."""
    floor = await on_the_floor(budget_cents=1).open()

    before = await _money_view(floor)
    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])
    after = await _money_view(floor)

    for view in (before, after):
        _claims_no_money_is_enough(view)
        assert _lines(view)["Free to trade"] == "0.01"
    assert (before.headline, before.detail) == (after.headline, after.detail)
    [(reason_code, why)] = ran.blocked()
    assert reason_code == "LIVE_ENVELOPE_CASH_EXCEEDED" and "412.06" in why
    _nothing_was_sent(floor, ran)


async def test_an_option_bot_whose_entry_took_its_whole_budget_reads_as_holding_with_nothing_free(
    on_the_floor: Callable[..., Floor],
) -> None:
    """A budget of $412.06 is the call at $4.12 and the six cents set aside for its fees, to the cent. The bot
    buys it and holds: nothing is free, and its next entry waits for this position to be sold."""
    floor = await on_the_floor(budget_cents=41_206).open()

    await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])
    view = await _money_view(floor)

    assert floor.holds() == {CALL: 1.0}
    assert (view.state, view.entry_eligible) == ("ready", False)
    assert (view.headline, view.detail) == ("Holding its position", "Its next entry waits until this position is sold.")
    assert _lines(view)["Free to trade"] == "0.00"


async def test_an_option_bot_with_nothing_of_its_budget_free_waits_for_its_next_entry_and_says_why(
    floor: Floor,
) -> None:
    """Flat and running, with every cent of its budget gone: on a real account fees charged above the ones set
    aside can do that. No price is needed to know an entry cannot be made, since every entry costs
    something. The view says it waits, and why, and never that an entry is still to be priced."""
    await floor.account_is_read()
    repo, sync = floor.repo, floor.runtime.envelope_sync
    risk = current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock())

    def read_with(fees: str) -> Any:
        own = deployment_budget(
            strategy_instance_id=SID, committed_cents=100_000, active=True, realized_gross=Decimal("-900"),
            fees=Decimal(fees), position_cost=Decimal(0), pending_orders=Decimal(0),
        )
        money = account_budget(cash=Decimal("1000"), deployments=[own], order_claims=Decimal(0), fee_claims=Decimal(0))
        return budget_deploy._next_entry(floor.runtime, repo, money, SID, risk)

    nothing_free, over_its_budget, a_cent_free = read_with("100"), read_with("100.50"), read_with("99.99")

    for waits in (nothing_free, over_its_budget):
        assert (waits.eligible, waits.headline) == (False, "Its next entry waits")
        assert "nothing of its budget is free" in waits.detail.lower() and waits.required is None
    assert a_cent_free.eligible and "picked" in a_cent_free.headline


# ---- With nothing handed in, the process's own -------------------------------------------


async def test_with_no_market_handed_in_the_runner_reads_the_process_own_supervisor_list_lane_and_settings(
    on_the_floor: Callable[..., Floor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production hands the runner nothing. The Clerk's supervisor, the watch list, the entry lane, the reader
    of the rate and dividends and the line budget are then the process's own."""
    # Five lines: just what one call leg watched two strikes either side needs, and no wider window would fit.
    floor = await on_the_floor(watch=get_option_watch_list(), line_budget=5).open()
    searches = OptionSearches(executor=lambda: floor.worker)
    asked_for: list[tuple[str, date]] = []

    def read(underlying: str, *, at_ms: int, latest_expiry: date) -> Any:
        del at_ms
        asked_for.append((underlying, latest_expiry))
        return KNOWN

    monkeypatch.setattr(option_search, "_searches", searches)
    monkeypatch.setattr(bot_option_run, "read_option_model_inputs", read)
    set_market_liveness_consumer(floor.consumer)
    try:
        ran = await floor.run(_round_trip(), the_process_own_market=True)
        lanes_started = (searches.entry.worker_pid, searches.preview.lane.worker_pid)
    finally:
        set_market_liveness_consumer(None)
        searches.close()

    assert ran.blocked() == []
    assert lanes_started == (os.getpid(), None), "the search ran in the entry lane, and no page's lane was started"
    assert [fill[:4] for fill in floor.fills()] == [(CALL, "buy", 1.0, 4.12), (CALL, "sell", 1.0, 4.30)]
    assert set(asked_for) == {("SPY", FRIDAY)}
    assert len(floor.worker.asked) == 1, "the entry's search went to the process's entry lane"
    assert get_option_watch_list().requests() == ()


async def test_with_no_market_handed_in_the_line_budget_is_the_clerks_own_setting(
    on_the_floor: Callable[..., Floor],
) -> None:
    floor = await on_the_floor(line_budget=4).open()

    with pytest.raises(OptionPlanRefused) as refused:
        await floor.run([Step(green(FIRST_GREEN_MS))], the_process_own_market=True)

    assert refused.value.reason is OptionEffectRefusal.WINDOW_OVER_LINE_BUDGET


async def test_a_bar_handed_over_a_moment_before_this_machines_clock_says_it_closed_is_still_decided_on(
    floor: Floor,
) -> None:
    """The host's clock is two tenths of a second behind the feed's. The bar that closed at 10:31:00 arrives
    at 10:30:59.8 by this machine. The entry is searched for and bought, its selection stamped at the bar's
    close and never before it."""
    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS), delay_ms=-200)])

    (search,) = floor.worker.asked
    assert ran.blocked() == []
    assert (search.decision_bar_close_ms, search.selection_ms) == (ENTER_MS, ENTER_MS)
    assert floor.entry_as_the_clerk_kept_it().rechecked_at_ms == ENTER_MS
    assert floor.fills() == [(CALL, "buy", 1.0, 4.12, 100, Decimal("412.00"))]


@pytest.mark.parametrize(("bid", "open_pnl"), [("4.08", "-4.00"), ("0.00", "-412.00")])
async def test_option_bot_panel_reads_its_quote_fill_receipt_and_multiplier_marks(
    floor: Floor, monkeypatch: pytest.MonkeyPatch, bid: str, open_pnl: str,
) -> None:
    from app.services.bot_runner import BotTaskRegistry
    from app.services.broker_v2_panel import panel_data_source

    registry = BotTaskRegistry(floor.tmp_path, feed_resolver=lambda: None, now_ms=floor.clock, boot_recovery_required=False)
    registry._bindings.record_launch(floor.binding, launch_reason="deploy")
    observed = []

    async def look(held: Floor) -> None:
        await held.bar_arrives(ENTER_MS + MINUTE + DELIVERY_MS)
        kept = held.kept_quotes()
        stated = kept.stated_mark(CALL, at_or_before_ms=held.clock())
        assert stated is not None
        # This panel boundary reads a retained whole quote, including a bid of zero.
        quote = stated.quote.model_copy(update={"bid": Decimal(bid)})
        kept.keep(CALL, quote.held, kept_at_ms=held.clock())
        panel, _, _ = await panel_data_source._get_panel_with_entries_from_authority(
            "alpaca", ACCOUNT, SID, resolved=ACCOUNT, captured_now_ms=held.clock(),
            registry=registry, binding=held.binding, facade=held.clerk,
        )
        observed.append(panel)
        monkeypatch.setattr(held.clerk, "synthesized_option_fills", lambda: {})
        unknown, _, _ = await panel_data_source._get_panel_with_entries_from_authority(
            "alpaca", ACCOUNT, SID, resolved=ACCOUNT, captured_now_ms=held.clock(),
            registry=registry, binding=held.binding, facade=held.clerk,
        )
        observed.append(unknown)
        stale, _, _ = await panel_data_source._get_panel_with_entries_from_authority(
            "alpaca", ACCOUNT, SID, resolved=ACCOUNT, captured_now_ms=held.clock() + 80_001,
            registry=registry, binding=held.binding, facade=held.clerk,
        )
        observed.append(stale)

    await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS)), Step(red(ENTER_MS + MINUTE), before=look)])
    priced, unknown, stale = observed
    assert priced.open_pnl_usd == open_pnl  # (bid - paid ask 4.12) * 100 shares per contract.
    [fill] = priced.run_fills
    assert (fill.symbol, fill.simulated.kind, fill.simulated.caution) == (CALL, "natural_price", False)
    assert "ask" in fill.simulated.sentence
    assert [fill.simulated.kind for fill in unknown.run_fills] == ["unknown"]
    assert stale.open_pnl_usd is None
    assert len(stale.run_fills) == 1


async def test_option_fill_explanation_records_an_older_sell_quote_and_refuses_a_mismatch(floor: Floor) -> None:
    from app.broker.alpaca.clerk.fills import FillRecord
    from app.broker.contract.models import OrderSide
    from app.options.simulated_fill import SimulatedFillModel
    from app.services.broker_v2_panel.bot_page_projection import option_fill_view

    await floor.run(_round_trip())
    receipt = next(value for value in floor.clerk.synthesized_option_fills().values() if value.request.side is OrderSide.SELL)
    older = receipt.model_copy(update={"fill_model": SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, "quote_age_ms": 60_000})
    fill = FillRecord(account_id=ACCOUNT, sid=SID, intent_id="exit", order_ref="exit", event_key="exit",
        symbol=CALL, side=OrderSide.SELL, quantity=1, fill_price=float(receipt.fill_price),
        filled_at_ms=receipt.filled_at_ms, fee=None, multiplier=100)
    view = option_fill_view(older, fill)
    assert (view.kind, view.caution, view.price_age_ms) == ("natural_price_older_quote", True, 60_000)
    assert "bid" in view.sentence and "older" in view.label
    assert option_fill_view(older.model_copy(update={"fill_price": Decimal("999")}), fill).kind == "unknown"
