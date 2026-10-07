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

from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes
from app.broker.alpaca.clerk.held_option_quotes import reset_held_option_quotes_for_testing
from app.broker.ibkr.config import reset_settings_for_testing
from app.options.simulated_fill import SimulatedFillModel
from app.options.watch import LineClass
from app.services import market_liveness

from ._option_floor import (
    ACCOUNT,
    CALL_585,
    DELIVERY_MS,
    ENTER_MS,
    EXIT_MS,
    FIRST_GREEN_MS,
    LEG,
    MINUTE,
    SID,
    Floor,
    Step,
    green,
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
