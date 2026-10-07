"""Questions about the market are asked of an option position's underlying, never of the contract (#2913).

An option contract has no stock quote line, no market status of its own,
and is not the symbol its bot is registered under. Three reads that were
written for shares are held to that here: the symbols the Clerk asks IBKR
to watch, whether the Clerk has work in flight on a symbol, and whether the
market for a close is halted.
"""

from __future__ import annotations

from dataclasses import replace

from app.broker.alpaca.clerk.recovery_reduction import market_status_symbol
from app.broker.alpaca.clerk.sqlite.commands import submit_retire_strategy_instance, submit_stop_run
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_watchdog import clerk_work_in_flight
from app.schemas.market_liveness import MarketLivenessFact, SymbolTradingStatusEvidence
from app.services.market_liveness import unknown_market_liveness
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    SYMBOL,
    WAIT_MS,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)

OTHER_CONTRACT = "SPY260918C00590000"


async def _a_holds_one_call(paper: PaperOptions) -> str:  # noqa: F811
    order_ref = await paper.resting("a")
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    return order_ref


async def test_a_held_contract_is_watched_through_its_underlying_and_never_as_a_stock_line(paper: PaperOptions) -> None:  # noqa: F811
    await _a_holds_one_call(paper)

    assert paper.repo.market_data_symbols() == ("SPY",)

    # Still watched, through the underlying, once the bot is stopped and only the position is left.
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")
    assert SYMBOL not in paper.repo.market_data_symbols()
    assert "SPY" in paper.repo.market_data_symbols()


async def test_a_contract_held_by_a_retired_bot_with_no_work_left_still_has_its_underlying_watched(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """The one case where only the holding itself asks for the line: a fill counted on an entry already ended."""
    order_ref = await paper.resting("a")
    paper.wait(WAIT_MS)
    await paper.round()
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-late")
    assert paper.held("a") == {SYMBOL: 1.0} and paper.enter_state(order_ref) == "failed"
    for bot in ("a", "b"):
        submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id=bot, lifecycle_run_id=f"run-{bot}")
        submit_retire_strategy_instance(
            paper.repo, account_id=paper.repo.account_id, strategy_instance_id=bot, retired_at_ms=paper.clock()
        )

    assert paper.repo.market_data_symbols() == ("SPY",)


async def test_an_entry_resting_in_a_contract_is_work_in_flight_on_that_contract_and_on_no_other(
    paper: PaperOptions,  # noqa: F811
) -> None:
    assert not clerk_work_in_flight(paper.repo, SYMBOL)

    await paper.resting("a")

    assert clerk_work_in_flight(paper.repo, SYMBOL)
    assert clerk_work_in_flight(paper.repo, SYMBOL.lower())
    assert not clerk_work_in_flight(paper.repo, OTHER_CONTRACT)


def test_a_contracts_market_status_is_its_underlyings_and_a_stocks_is_its_own() -> None:
    assert market_status_symbol(SYMBOL) == "SPY"
    assert market_status_symbol(SYMBOL.lower()) == "SPY"
    assert market_status_symbol("SPY") == "SPY"
    assert market_status_symbol("BRK.B") == "BRK.B"


async def test_a_close_is_held_while_the_underlying_is_halted_and_sent_when_the_halt_lifts(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await _a_holds_one_call(paper)
    asked: list[str] = []
    halted = {"SPY"}

    def status(symbol: str, now_ms: int) -> MarketLivenessFact:
        # IBKR holds a status for the underlying. It holds none for a contract.
        asked.append(symbol)
        fact = unknown_market_liveness(symbol, observed_at_ms=now_ms)
        if symbol not in halted:
            return fact
        return fact.model_copy(update={
            "state": "HALTED",
            "symbol_status": SymbolTradingStatusEvidence(
                symbol=symbol, state="HALTED", source="ibkr.market_data.status", observed_at_ms=now_ms,
            ),
        })

    pricing = replace(paper.clerk.recovery_pricing, liveness_source=status)
    accepted = accept_exit(
        paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", decision_id="exit-in-a-halt",
        lifecycle_run_id="run-a", entry_order_ref=order_ref,
    )
    sent_before = len(paper.venue.submitted_option_orders)

    await resolve_exit(
        paper.repo, effect_operation_id=accepted.effect_operation_id, trade=paper.venue, pricing=pricing, read=None
    )

    assert asked and set(asked) == {"SPY"}
    assert len(paper.venue.submitted_option_orders) == sent_before
    assert paper.repo.active_exit_for_strategy("a") is not None

    halted.clear()
    await resolve_exit(
        paper.repo, effect_operation_id=accepted.effect_operation_id, trade=paper.venue, pricing=pricing, read=None
    )

    assert len(paper.venue.submitted_option_orders) == sent_before + 1
    assert set(asked) == {"SPY"}
