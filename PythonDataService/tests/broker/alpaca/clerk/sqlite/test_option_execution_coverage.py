"""An option order's executions, when the Clerk learns of one fill twice or cannot place one (#2913).

The books bind an order in an option contract to that contract, and its
bot is registered under the underlying. Every check that asks "is this
execution this order's?" must ask about the contract, or a correct
execution is refused for being in the wrong symbol.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.repository_execution_coverage_api import (
    ExecutionCoverageResolutionUnavailable,
)
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    AAPL_CALL,
    AAPL_PUT,
    accept_option_order,
    open_option_books,
)
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    SYMBOL,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_folds_execution import _append_slice, _coverage_conflict_transition
from tests.broker.alpaca.clerk.sqlite.test_option_custody import _put_in_the_recovered_fills_place, _recover_total


async def test_an_execution_the_stream_delivers_after_a_lookup_counted_the_fill_takes_its_place(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Alpaca's REST answer shows the fill before its stream does: an ordinary order of events."""
    order_ref = await paper.resting()
    paper.venue.option_order_fills(order_ref, contracts=1, average_price="1.94")
    await paper.pass_()
    (recovered,) = paper.repo.fills_for_order(order_ref)
    assert (recovered["evidence_source"], recovered["execution_id"]) == ("cumulative_recovery", None)
    assert paper.held() == {SYMBOL: 1.0}

    await paper.stream_execution(order_ref, contracts=1, price="1.94", execution_id="x-1")

    (exact,) = paper.repo.fills_for_order(order_ref)
    assert (exact["evidence_source"], exact["execution_id"]) == ("websocket", "x-1")
    # The same contract, counted once: the exact execution replaced the total, and moved no position.
    assert paper.held() == {SYMBOL: 1.0}
    assert paper.outcome(order_ref).standing is EntryStanding.FILLED
    assert paper.repo.active_uncertainties() == []


def _an_execution_held_aside(
    repo: ClerkSqliteRepository, order: EnterSubmission, *, reported: str, price: float
) -> str:
    """An exact execution of 2 held aside behind a coverage conflict on ``order``; the conflict's id."""
    exact = ExecutionSliceFilledFacts(
        execution_id="x1", symbol=reported, side="BUY", slice_qty=2.0, slice_price=price, fee=None,
        fee_fidelity="not_reported", evidence_source="websocket", source_event_at_ms=NOON,
    )
    repo.append_transition(_coverage_conflict_transition(repo, accepted=order, facts=exact))
    assert _append_slice(repo, accepted=order, facts=exact) == "coverage_conflict_quarantined"
    (conflict,) = repo.active_uncertainties_for_admission(strategy_instance_id="bot-a")
    return conflict["uncertainty_id"]


def test_the_operators_recovery_of_an_option_orders_execution_is_asked_of_the_orders_contract(tmp_path: Path) -> None:
    repo = open_option_books(tmp_path)
    try:
        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        _recover_total(repo, order, symbol=AAPL_CALL.symbol, wanted=2, filled=2, price=1.50)
        conflict = _an_execution_held_aside(repo, order, reported=AAPL_CALL.symbol, price=1.50)

        target = repo.historical_execution_recovery_target(conflict)

        # The bot is registered under AAPL; the execution the operator recovers is in the call.
        assert target.symbol == AAPL_CALL.symbol
        assert target.order_ref == order.order_ref
    finally:
        repo.close()


def test_an_execution_in_another_contract_is_refused_as_a_replacement_and_nothing_is_written(tmp_path: Path) -> None:
    repo = open_option_books(tmp_path)
    try:
        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        _recover_total(repo, order, symbol=AAPL_CALL.symbol, wanted=2, filled=2, price=1.50)
        conflict = _an_execution_held_aside(repo, order, reported=AAPL_PUT.symbol, price=1.50)
        before = repo.last_custody_sequence()

        # A refusal the operator's route answers as one, never an error out of the books.
        with pytest.raises(ExecutionCoverageResolutionUnavailable, match=f"which trades {AAPL_CALL.symbol}"):
            _put_in_the_recovered_fills_place(repo, conflict)

        assert repo.last_custody_sequence() == before
        (kept,) = repo.fills_for_order(order.order_ref or "")
        assert (kept["evidence_source"], kept["execution_id"]) == ("cumulative_recovery", None)
    finally:
        repo.close()
