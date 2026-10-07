"""What an option entry came to, read from the books, and how the strategy is settled on it (#2913).

Each test brings a Paper account's books to one standing the way they get
there: the order rests at the venue, fills or does not, and the Clerk learns
of it by a lookup or from the stream. What is asserted is the standing, what
the bot holds, and whether the strategy counts itself in.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.entry_outcome import (
    EntryOutcome,
    EntrySettlement,
    EntryStanding,
    entry_outcome,
    entry_settlement,
    recorded_settlement,
    settlement_for,
)
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
from tests.broker.alpaca.clerk.sqlite.conftest import _broker_leg, _FakeTradePort
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    SYMBOL,
    WAIT_MS,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
    patiently,
)


async def test_an_entry_sent_and_not_yet_filled_is_working_and_the_bot_holds_nothing(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()

    outcome = paper.outcome(order_ref)

    assert (outcome.standing, outcome.proven) == (EntryStanding.WORKING, False)
    assert (outcome.units_asked, outcome.units_filled, outcome.contracts_filled) == (1, 0, {SYMBOL: 0})
    assert outcome.order_refs == (order_ref,) and not outcome.legs_out_of_step
    assert outcome.cancel_at_ms == paper.clock() + WAIT_MS
    assert (outcome.cancel_requested_at_ms, outcome.proven_at_ms) == (None, None)
    assert paper.held() == {}


async def test_an_entry_the_stream_reports_filled_is_filled(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(units=2)

    await paper.stream_fill(order_ref, contracts=2, filled_in_all=2, price="1.94", execution_id="x-1")

    outcome = paper.outcome(order_ref)
    assert (outcome.standing, outcome.proven, outcome.units_filled) == (EntryStanding.FILLED, True, 2)
    assert outcome.contracts_filled == {SYMBOL: 2}
    assert outcome.proven_at_ms == paper.clock()
    assert paper.held() == {SYMBOL: 2.0}
    assert settlement_for(outcome) is EntrySettlement.ENTERED


async def test_an_entry_cancelled_after_part_of_it_filled_is_part_filled_and_the_bot_holds_that_part(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await paper.resting(units=2)
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    paper.wait(WAIT_MS)

    await paper.round()

    outcome = paper.outcome(order_ref)
    assert (outcome.standing, outcome.proven) == (EntryStanding.PART_FILLED, True)
    assert (outcome.units_asked, outcome.units_filled) == (2, 1)
    assert paper.held() == {SYMBOL: 1.0}
    assert settlement_for(outcome) is EntrySettlement.ENTERED


async def test_an_entry_cancelled_with_nothing_filled_is_unfilled_and_the_decision_is_dropped(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    paper.wait(WAIT_MS)

    await paper.round()

    outcome = paper.outcome(order_ref)
    assert (outcome.standing, outcome.proven, outcome.units_filled) == (EntryStanding.UNFILLED, True, 0)
    assert outcome.cancel_requested_at_ms == outcome.cancel_at_ms == paper.clock()
    assert settlement_for(outcome) is EntrySettlement.DISCARDED


async def test_a_cancel_asked_for_and_not_yet_shown_as_ended_is_cancellation_requested(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.venue.cancel_is_slow = True
    paper.wait(WAIT_MS)

    await paper.round()

    outcome = paper.outcome(order_ref)
    assert (outcome.standing, outcome.proven) == (EntryStanding.CANCELLATION_REQUESTED, False)
    assert outcome.cancel_requested_at_ms == paper.clock()
    # Not proven, so the strategy is in: an exit will close whatever turns out to be held.
    assert settlement_for(outcome) is EntrySettlement.ENTERED


async def test_an_entry_alpaca_refuses_was_never_sent_and_the_decision_is_dropped(paper: PaperOptions) -> None:  # noqa: F811
    paper.venue.option_submit_error = BrokerOrderRejected("insufficient options buying power", broker="alpaca")

    refused = await paper.enter()

    assert refused.state is EffectOperationState.REJECTED
    [order_ref] = refused.child_order_refs
    outcome = paper.outcome(order_ref)
    assert (outcome.standing, outcome.proven, outcome.units_filled) == (EntryStanding.NOT_SENT, True, 0)
    assert settlement_for(outcome) is EntrySettlement.DISCARDED
    assert paper.venue.option_orders == {}


async def test_an_entry_whose_answer_was_lost_and_cannot_be_looked_up_is_unknown(paper: PaperOptions) -> None:  # noqa: F811
    paper.venue.option_answer_lost = True
    paper.venue.option_lookup_error = BrokerUnavailable("lookup timed out", broker="alpaca")

    sent = await paper.enter()

    [order_ref] = sent.child_order_refs
    outcome = paper.outcome(order_ref)
    assert (outcome.standing, outcome.proven) == (EntryStanding.OUTCOME_UNKNOWN, False)
    assert settlement_for(outcome) is EntrySettlement.ENTERED


async def test_a_filled_order_whose_fill_is_not_on_the_books_is_unknown_never_filled(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(units=2)
    paper.venue.option_order_fills(order_ref, contracts=2, average_price="1.94")

    # The stream's frame says filled, two contracts; the execution it stands for never arrived.
    paper.frame_without_its_execution(order_ref)

    outcome = paper.outcome(order_ref)
    assert paper.order_state(order_ref) == "filled"
    assert (outcome.standing, outcome.proven, outcome.units_filled) == (EntryStanding.OUTCOME_UNKNOWN, False, 0)
    assert paper.held() == {}

    # The pass looks the order up and counts what Alpaca says filled.
    await paper.pass_()

    assert paper.outcome(order_ref).standing is EntryStanding.FILLED
    assert paper.held() == {SYMBOL: 2.0}


async def test_a_part_filled_order_cancelled_before_all_its_fills_are_on_the_books_is_unknown(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await paper.resting(units=2)
    paper.venue.option_order_fills(order_ref, contracts=1, average_price="1.94")
    paper.venue.option_order_ends(order_ref, "canceled")

    paper.frame_without_its_execution(order_ref)

    outcome = paper.outcome(order_ref)
    assert paper.order_state(order_ref) == "canceled"
    # Nothing is on the books and Alpaca counted one contract: not proven, though the order has ended.
    assert (outcome.standing, outcome.units_filled) == (EntryStanding.OUTCOME_UNKNOWN, 0)
    # Never the decision dropped: the bot may hold what Alpaca counted.
    assert settlement_for(outcome) is EntrySettlement.ENTERED


async def test_an_order_alpaca_calls_filled_with_fewer_contracts_on_the_books_than_asked_is_unknown(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await paper.resting(units=2)
    # The frame says filled and reports no count; nothing of it is on the books.
    paper.venue.option_orders[order_ref].update(status="filled")

    paper.frame_without_its_execution(order_ref)

    outcome = paper.outcome(order_ref)
    assert paper.order_state(order_ref) == "filled"
    assert (outcome.standing, outcome.proven, outcome.units_filled) == (EntryStanding.OUTCOME_UNKNOWN, False, 0)


async def test_books_holding_more_contracts_than_the_entry_asked_for_are_unknown_never_filled(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    assert paper.outcome(order_ref).standing is EntryStanding.FILLED

    # A second execution of one contract on an order that asked for one.
    await paper.stream_execution(order_ref, contracts=1, price="1.94", execution_id="x-2")

    outcome = paper.outcome(order_ref)
    assert (outcome.units_asked, outcome.units_filled) == (1, 2)
    assert outcome.standing is EntryStanding.OUTCOME_UNKNOWN and not outcome.proven
    # The bot holds something, so the strategy is in: an exit will come for it.
    assert settlement_for(outcome) is EntrySettlement.ENTERED


@pytest.mark.parametrize("ended_as", ["canceled", "expired"])
async def test_books_holding_more_than_was_asked_on_an_order_that_did_not_end_filled_are_unknown_never_part_filled(
    paper: PaperOptions, ended_as: str  # noqa: F811
) -> None:
    """An ended order with something filled reads part filled, which is proven. Two of one is no such outcome."""
    order_ref = await paper.resting()
    paper.venue.option_order_ends(order_ref, ended_as)

    # Two executions of one contract each arrive on an order that asked for one and did not end filled.
    await paper.stream_execution(order_ref, contracts=1, price="1.94", execution_id="x-1")
    await paper.stream_execution(order_ref, contracts=1, price="1.94", execution_id="x-2")

    outcome = paper.outcome(order_ref)
    assert paper.order_state(order_ref) == ended_as
    assert (outcome.units_asked, outcome.units_filled) == (1, 2)
    assert outcome.standing is EntryStanding.OUTCOME_UNKNOWN and not outcome.proven


@pytest.mark.parametrize("ended_as", ["expired", "rejected", "canceled"])
async def test_an_order_alpaca_ended_by_itself_is_unfilled_or_part_filled_by_what_it_had_filled(
    paper: PaperOptions, ended_as: str  # noqa: F811
) -> None:
    nothing_filled, one_of_two = await paper.resting("a"), await paper.resting("b", units=2)
    await paper.stream_fill(one_of_two, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    for order_ref in (nothing_filled, one_of_two):
        paper.venue.option_order_ends(order_ref, ended_as)
    paper.wait(WAIT_MS)

    done = await paper.round()

    # Nothing was left to cancel: the lookup showed each order ended, and that is proof.
    assert set(done.entries.ended) == {nothing_filled, one_of_two}
    assert (paper.order_state(nothing_filled), paper.outcome(nothing_filled).standing) == (ended_as, EntryStanding.UNFILLED)
    assert (paper.order_state(one_of_two), paper.outcome(one_of_two).standing) == (ended_as, EntryStanding.PART_FILLED)
    assert paper.enter_state(nothing_filled) == "failed"
    assert paper.set_aside_cents("a") == 0 and paper.set_aside_cents("b") == 0
    assert paper.held("b") == {SYMBOL: 1.0}


async def test_asked_after_the_fact_the_recorded_answer_wins_and_with_none_the_books_decide(paper: PaperOptions) -> None:  # noqa: F811
    settled_first, never_asked = await paper.resting("a", decision="d-a"), await paper.resting("b", decision="d-b")
    assert recorded_settlement(paper.repo, never_asked) is None
    # A's wait runs to its limit with the entry still working: the Clerk answers "entered" and records it.
    paper.clerk._outcome_pause = patiently(paper)
    answered = await paper.clerk.await_entry_outcome(
        strategy_instance_id="a", decision_id="d-a", until_ms=paper.clock() + 5_000
    )
    assert answered.settlement is EntrySettlement.ENTERED and answered.recorded

    # Both are then cancelled with nothing filled.
    paper.wait(WAIT_MS)
    await paper.round()
    assert paper.outcome(settled_first).standing is paper.outcome(never_asked).standing is EntryStanding.UNFILLED

    # What A's strategy was told stands; B was told nothing, so the books speak.
    assert entry_settlement(paper.repo, settled_first) is EntrySettlement.ENTERED
    assert recorded_settlement(paper.repo, settled_first) == answered
    assert entry_settlement(paper.repo, never_asked) is EntrySettlement.DISCARDED


def _outcome(standing: EntryStanding, *, units_filled: int) -> EntryOutcome:
    return EntryOutcome(
        standing=standing, order_refs=("ref",), units_asked=3, units_filled=units_filled,
        contracts_filled={SYMBOL: units_filled}, legs_out_of_step=False, cancel_at_ms=None,
        cancel_requested_at_ms=None, proven_at_ms=None,
    )


@pytest.mark.parametrize(
    ("standing", "units_filled", "settled"),
    [
        (EntryStanding.FILLED, 3, EntrySettlement.ENTERED),
        (EntryStanding.PART_FILLED, 1, EntrySettlement.ENTERED),
        (EntryStanding.UNFILLED, 0, EntrySettlement.DISCARDED),
        (EntryStanding.NOT_SENT, 0, EntrySettlement.DISCARDED),
        # Not proven: the strategy is in, whatever has filled so far.
        (EntryStanding.WORKING, 0, EntrySettlement.ENTERED),
        (EntryStanding.WORKING, 1, EntrySettlement.ENTERED),
        (EntryStanding.CANCELLATION_REQUESTED, 0, EntrySettlement.ENTERED),
        (EntryStanding.OUTCOME_UNKNOWN, 0, EntrySettlement.ENTERED),
        # A fill counted on an order once proven unfilled: held means filled.
        (EntryStanding.UNFILLED, 1, EntrySettlement.ENTERED),
    ],
)
def test_the_strategy_is_in_unless_the_entry_is_proven_to_have_bought_nothing(
    standing: EntryStanding, units_filled: int, settled: EntrySettlement
) -> None:
    assert settlement_for(_outcome(standing, units_filled=units_filled)) is settled


async def test_an_order_in_shares_has_no_option_outcome(paper: PaperOptions) -> None:  # noqa: F811
    stock = await submit_enter(
        paper.repo, account_id=paper.repo.account_id, strategy_instance_id="b", decision_id="d-1",
        lifecycle_run_id="run-b", leg=_broker_leg(quantity=1), trade=_FakeTradePort(),
        envelope=paper.clerk.live_envelope, reference_price=100,
    )

    with pytest.raises(ValueError, match="not an entry in an option contract"):
        entry_outcome(paper.repo, stock.order_ref)
