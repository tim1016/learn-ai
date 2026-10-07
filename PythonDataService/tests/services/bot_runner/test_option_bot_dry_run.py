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

import json
import os
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes
from app.broker.alpaca.clerk.held_option_quotes import KEEP_EVERY_MS, reset_held_option_quotes_for_testing
from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionPlanRefused
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulatedAccountProjection
from app.broker.ibkr.config import reset_settings_for_testing
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.options.requirement import StructureMoneyRefusal
from app.options.simulated_fill import SimulatedFillModel
from app.options.watch import LineClass
from app.services import market_liveness
from app.services.option_search import OptionSearchLane

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
    LEG,
    MINUTE,
    SID,
    TUESDAY,
    WEDNESDAY,
    Floor,
    Ran,
    Step,
    green,
    one_call,
    red,
)

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


# ---- Lateness ---------------------------------------------------------------------------


async def test_an_entry_whose_time_runs_out_while_the_rate_is_read_is_refused_as_late_and_searches_nothing(
    floor: Floor,
) -> None:
    """The vendors took twenty-five seconds. A decision is on time for twenty."""
    def a_slow_vendor() -> None:
        if floor.clock.value >= ENTER_MS:
            floor.clock.value += 25_000

    floor.while_inputs_are_read = a_slow_vendor

    ran = await floor.run([Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))])

    assert [code for code, _ in ran.blocked()] == ["DECISION_LATE"]
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
