"""The cancel of a resting option entry when its owner's wait runs out (#2913).

Each test rests a bought call at a Paper account's venue and lets the wait
pass. What is asserted is what the owner would see: whether the order is
still in the market, what the bot holds, and what is still set aside for it.

One contract costs $194.00 to buy and six cents of fees are set aside with
it, so a whole entry of one contract claims $194.06 of its bot's budget.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite import reads, reconcile
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper
from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter, submit_accepted_enter
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_uncertain, submit_absence_grace_ms
from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryCapability
from app.broker.alpaca.clerk.sqlite.projections import read_recovery_context
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_execution import RecoveryExecutionRequest, execute_recovery_action
from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryPolicyContext, build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, ExecutionLeaseLost
from app.broker.alpaca.clerk.sqlite.resting_entries import (
    RestingEntry,
    WaitEndedRound,
    cancel_entries_past_their_wait,
    entries_past_their_wait,
    resting_entries,
)
from app.broker.alpaca.clerk.sqlite.stopped_run_entries import cancel_entries_of_inactive_runs, entries_owed_a_cancel
from app.broker.alpaca.clerk.sqlite.uncertainty import ENTRY_ORDER_WORKING_REASON_CODE, admit_new_exposure
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import FAILED_ENTER_FILLED_REASON_CODE
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import OptionPositionIntent
from tests.broker.alpaca.clerk.sqlite.budgeted_run_fixture import submit_budgeted_test_enter, submit_budgeted_test_run
from tests.broker.alpaca.clerk.sqlite.conftest import (
    BUDGETED_RTH_MS,
    _broker_leg,
    _FakeReadPort,
    _FakeTradePort,
    _TestClock,
    _walk_clock_to,
    documented_option_order_payload,
)
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    AAPL_CALL,
    accept_option_closing_order,
    accept_option_order,
    fill,
    open_option_books,
)
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    SYMBOL,
    WAIT_MS,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_option_contracts_to_watch import _the_broker_says
from tests.broker.alpaca.clerk.sqlite.test_option_custody import _accept_stock_order
from tests.broker.alpaca.clerk.sqlite.test_resting_entry_facts import _PINNED_OPTION

ONE_CONTRACT_SET_ASIDE = 19_406


def _due(paper: PaperOptions) -> list[str]:  # noqa: F811
    return [entry.order.order_ref for entry in entries_past_their_wait(paper.repo, paper.clock())]


# ── before the deadline, and what the step leaves alone ──────────────────


async def test_an_entry_still_inside_its_wait_is_left_in_the_market(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.wait(WAIT_MS - 1)

    done = await paper.round()

    assert done.entries == WaitEndedRound()
    assert paper.venue.cancel_calls == [] and paper.cancel_requests(order_ref) == []
    assert paper.outcome(order_ref).standing is EntryStanding.WORKING
    assert [entry.order.order_ref for entry in resting_entries(paper.repo)] == [order_ref]


async def test_an_entry_that_filled_in_full_before_its_deadline_is_never_cancelled(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    paper.wait(WAIT_MS + 5_000)

    await paper.round()

    assert paper.venue.cancel_calls == [] and paper.cancel_requests(order_ref) == []
    assert paper.outcome(order_ref).standing is EntryStanding.FILLED
    assert paper.held() == {SYMBOL: 1.0}


async def test_an_entry_from_before_entries_could_rest_has_no_deadline_and_is_left_alone(paper: PaperOptions) -> None:  # noqa: F811
    before = OptionEntryFacts.from_facts(json.loads(_PINNED_OPTION))
    accepted = accept_option_enter(
        paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", decision_id="from-before",
        lifecycle_run_id="run-a", entry=before, envelope=paper.clerk.live_envelope,
    )
    await submit_accepted_enter(paper.repo, accepted=accepted, leg=before.order, trade=paper.venue)
    paper.wait(10 * WAIT_MS)

    await paper.round()

    assert paper.order_state(accepted.order_ref) == "new"
    assert resting_entries(paper.repo) == [] and paper.venue.cancel_calls == []


async def test_an_entry_an_exit_owns_is_left_to_that_exit(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    accept_exit(
        paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", decision_id="exit-owns-it",
        lifecycle_run_id="run-a", entry_order_ref=order_ref,
    )
    paper.wait(WAIT_MS)

    await paper.round()

    # The EXIT's own machine cancels the entry and closes what filled; a second canceller would race it.
    assert _due(paper) == [] and paper.venue.cancel_calls == [] and paper.cancel_requests(order_ref) == []


# ── the cancel, and each way it can come out ─────────────────────────────


async def test_an_unfilled_entry_is_cancelled_at_its_deadline_and_its_cash_is_free_again(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    assert paper.set_aside_cents() == ONE_CONTRACT_SET_ASIDE
    paper.wait(WAIT_MS)

    done = await paper.round()

    assert done.entries == WaitEndedRound(ended=(order_ref,))
    # Recorded before the venue was asked, with why; proven only by the lookup that followed.
    assert paper.cancel_requests(order_ref) == [("ENTRY_WAIT_ENDED", paper.clock())]
    assert paper.venue.cancel_calls == [paper.venue.option_orders[order_ref]["id"]]
    assert paper.venue.option_lookup_calls[-1] == order_ref
    assert (paper.order_state(order_ref), paper.enter_state(order_ref)) == ("canceled", "failed")
    assert len(paper.recorded(order_ref, "ENTRY_TERMINAL_CONFIRMED")) == 1
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED
    assert paper.held() == {} and paper.set_aside_cents() == 0


async def test_a_part_filled_entry_keeps_what_filled_and_the_rest_of_its_cash_is_free_again(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(units=2)
    assert paper.set_aside_cents() == 38_809
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    # One contract is bought; the other is still claimed at its limit, with the entry's fees.
    assert paper.set_aside_cents() == 19_409
    paper.wait(WAIT_MS)

    await paper.round()

    assert (paper.order_state(order_ref), paper.enter_state(order_ref)) == ("canceled", "in_progress")
    assert paper.outcome(order_ref).standing is EntryStanding.PART_FILLED
    assert paper.held() == {SYMBOL: 1.0}
    assert paper.set_aside_cents() == 0


async def test_an_entry_that_filled_while_the_books_did_not_know_is_counted_when_the_cancel_looks_it_up(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    paper.venue.option_order_fills(order_ref, contracts=1, average_price="1.94")
    # The fill's frame was lost. Alpaca refuses to cancel an order that has filled.
    paper.venue.cancel_error = BrokerOrderRejected("order is already filled", broker="alpaca")
    paper.wait(WAIT_MS)

    done = await paper.round()

    assert done.entries == WaitEndedRound(ended=(order_ref,))
    assert paper.order_state(order_ref) == "filled"
    assert paper.outcome(order_ref).standing is EntryStanding.FILLED
    assert paper.held() == {SYMBOL: 1.0}


async def test_a_cancel_taken_while_the_lookup_still_shows_the_order_working_is_only_requested(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    paper.venue.cancel_is_slow = True
    paper.wait(WAIT_MS)

    first = await paper.round()

    assert first.entries == WaitEndedRound(still_due=(order_ref,))
    assert paper.outcome(order_ref).standing is EntryStanding.CANCELLATION_REQUESTED
    assert paper.order_state(order_ref) == "new" and paper.enter_state(order_ref) == "in_progress"
    assert paper.set_aside_cents() == ONE_CONTRACT_SET_ASIDE

    # The next round asks the venue again, which is a retry and not a second intent.
    paper.wait(1_000)
    await paper.round()
    assert len(paper.venue.cancel_calls) == 2 and len(paper.cancel_requests(order_ref)) == 1

    paper.venue.cancel_is_slow = False
    paper.wait(1_000)
    last = await paper.round()

    assert last.entries == WaitEndedRound(ended=(order_ref,))
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED


async def test_a_cancel_alpaca_says_is_pending_is_not_asked_for_again(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.venue.cancel_is_slow = True
    paper.wait(WAIT_MS)
    await paper.round()
    # Alpaca now shows the order as pending cancel: it has the request, and asking again adds nothing.
    paper.venue.option_orders[order_ref]["status"] = "pending_cancel"
    paper.wait(1_000)
    await paper.round()
    assert paper.order_state(order_ref) == "pending_cancel" and len(paper.venue.cancel_calls) == 2

    paper.wait(1_000)
    still = await paper.round()

    assert len(paper.venue.cancel_calls) == 2
    assert still.entries == WaitEndedRound(still_due=(order_ref,))
    assert paper.outcome(order_ref).standing is EntryStanding.CANCELLATION_REQUESTED


async def test_a_fill_reported_after_the_cancel_was_asked_is_counted(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(units=2)
    paper.venue.cancel_is_slow = True
    paper.wait(WAIT_MS)
    await paper.round()
    assert paper.outcome(order_ref).standing is EntryStanding.CANCELLATION_REQUESTED

    # One contract trades before the venue acts on the cancel; then the order ends.
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-late")
    paper.venue.option_order_ends(order_ref, "canceled")
    paper.wait(1_000)
    await paper.round()

    assert paper.outcome(order_ref).standing is EntryStanding.PART_FILLED
    assert paper.held() == {SYMBOL: 1.0}


@pytest.mark.parametrize("what_fails", ["the_cancel", "the_lookup"])
async def test_an_uncertain_cancel_leaves_the_outcome_unknown_and_the_whole_commitment_set_aside(
    paper: PaperOptions, what_fails: str  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    unreachable = BrokerUnavailable("Alpaca did not answer", broker="alpaca")
    if what_fails == "the_cancel":
        paper.venue.cancel_error = unreachable
    else:
        paper.venue.option_lookup_error = unreachable
    paper.wait(WAIT_MS)

    done = await paper.round()

    assert done.entries == WaitEndedRound(still_due=(order_ref,))
    assert len(paper.recorded(order_ref, "ORDER_CANCEL_UNCERTAIN")) == 1
    assert paper.enter_state(order_ref) == "unknown"
    assert paper.outcome(order_ref).standing is EntryStanding.OUTCOME_UNKNOWN
    assert paper.set_aside_cents() == ONE_CONTRACT_SET_ASIDE

    # Alpaca answers again: the next round proves the order ended.
    paper.venue.cancel_error = paper.venue.option_lookup_error = None
    paper.wait(1_000)
    await paper.round()

    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED
    assert paper.set_aside_cents() == 0


async def test_an_entry_whose_answer_was_lost_is_found_by_the_lookup_and_cancelled_the_round_after(
    paper: PaperOptions,  # noqa: F811
) -> None:
    paper.venue.option_answer_lost = True
    paper.venue.option_lookup_error = BrokerUnavailable("lookup timed out", broker="alpaca")
    sent = await paper.enter()
    [order_ref] = sent.child_order_refs
    assert paper.repo.order(order_ref).broker_order_id is None
    paper.venue.option_lookup_error = None
    paper.wait(WAIT_MS)

    # With no broker id there is nothing to cancel by: the lookup finds the order first.
    found = await paper.round()

    assert found.entries == WaitEndedRound(still_due=(order_ref,))
    assert paper.venue.cancel_calls == [] and paper.order_state(order_ref) == "new"

    paper.wait(1_000)
    await paper.round()

    assert paper.venue.cancel_calls == [paper.venue.option_orders[order_ref]["id"]]
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED


async def test_an_entry_that_never_reached_alpaca_is_voided_only_after_the_grace(paper: PaperOptions) -> None:  # noqa: F811
    paper.venue.option_submit_error = BrokerUnavailable("connection reset before any answer", broker="alpaca")
    sent = await paper.enter()
    [order_ref] = sent.child_order_refs
    assert paper.outcome(order_ref).standing is EntryStanding.OUTCOME_UNKNOWN
    paper.wait(WAIT_MS)

    # Alpaca has no such order, and it is too soon for that to prove anything.
    await paper.round()

    assert paper.outcome(order_ref).standing is EntryStanding.OUTCOME_UNKNOWN
    assert paper.set_aside_cents() == ONE_CONTRACT_SET_ASIDE

    paper.wait(submit_absence_grace_ms() - WAIT_MS)
    done = await paper.round()

    assert done.entries == WaitEndedRound(ended=(order_ref,))
    assert paper.outcome(order_ref).standing is EntryStanding.NOT_SENT
    assert paper.venue.cancel_calls == [] and paper.set_aside_cents() == 0


async def test_a_fill_that_arrives_after_an_entry_was_proven_unfilled_is_still_counted_and_the_owner_is_told(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    paper.wait(WAIT_MS)
    await paper.round()
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED

    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-after")

    assert paper.held() == {SYMBOL: 1.0}
    assert paper.outcome(order_ref).units_filled == 1
    assert FAILED_ENTER_FILLED_REASON_CODE in {raised["reason_code"] for raised in paper.repo.active_uncertainties()}


async def test_an_entry_whose_operation_is_claimed_is_skipped_and_cancelled_once_it_is_free(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    order = paper.repo.order(order_ref)
    held = paper.repo.claim_before_broker_contact(order.effect_operation_id)
    paper.wait(WAIT_MS)

    skipped = await paper.round()

    assert skipped.entries == WaitEndedRound(still_due=(order_ref,))
    assert paper.venue.cancel_calls == [] and paper.cancel_requests(order_ref) == []

    paper.repo.release_operation_claim(effect_operation_id=order.effect_operation_id, token=held.token)
    paper.wait(1_000)
    await paper.round()

    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED


# ── an entry voided as never sent, that Alpaca shows after all ────────────


async def _an_entry_voided_as_never_sent(paper: PaperOptions) -> str:  # noqa: F811
    """The send gets no answer and Alpaca shows no such order through the grace: the ENTER is voided."""
    paper.venue.option_submit_error = BrokerUnavailable("connection reset before any answer", broker="alpaca")
    sent = await paper.enter()
    paper.venue.option_submit_error = None
    [order_ref] = sent.child_order_refs
    paper.wait(WAIT_MS)
    await paper.round()
    assert paper.outcome(order_ref).standing is EntryStanding.OUTCOME_UNKNOWN
    paper.wait(submit_absence_grace_ms() - WAIT_MS)
    await paper.round()
    assert paper.enter_state(order_ref) == "failed"
    assert paper.outcome(order_ref).standing is EntryStanding.NOT_SENT and paper.set_aside_cents() == 0
    return order_ref


def _the_abandoned_send_lands(paper: PaperOptions, order_ref: str) -> None:  # noqa: F811
    """Alpaca has the order after all (#2342), working, and its stream says so."""
    asked = next(order for order in paper.venue.submitted_option_orders if order is not None)
    paper.venue.option_orders[order_ref] = documented_option_order_payload(
        asked, client_order_id=order_ref, at=paper.venue._venue_stamp()
    )
    paper.frame_without_its_execution(order_ref)
    assert paper.order_state(order_ref) == "new" and paper.enter_state(order_ref) == "failed"


async def test_a_voided_entry_alpaca_later_shows_working_is_cancelled_by_the_first_round_that_sees_it(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await _an_entry_voided_as_never_sent(paper)
    _the_abandoned_send_lands(paper, order_ref)
    assert _due(paper) == [order_ref]

    # In between, the bot's next entry would be a second working buy for one signal.
    refused = await paper.enter()
    assert refused.state is EffectOperationState.REJECTED, refused.explanation
    assert admit_new_exposure(paper.repo, strategy_instance_id="a").reason_code == ENTRY_ORDER_WORKING_REASON_CODE
    assert paper.venue.option_submit_calls == [order_ref]
    # Another bot on the account is not held by it.
    assert admit_new_exposure(paper.repo, strategy_instance_id="b").allowed

    done = await paper.round()

    assert done.entries == WaitEndedRound(ended=(order_ref,))
    assert paper.venue.cancel_calls == [paper.venue.option_orders[order_ref]["id"]]
    assert paper.cancel_requests(order_ref) == [("ENTRY_WAIT_ENDED", paper.clock())]
    assert (paper.order_state(order_ref), paper.enter_state(order_ref)) == ("canceled", "failed")
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED and paper.held() == {}
    assert admit_new_exposure(paper.repo, strategy_instance_id="a").allowed
    again = await paper.enter()
    assert again.state is EffectOperationState.SUBMITTED, again.explanation


async def test_a_voided_entry_alpaca_later_shows_working_is_cancelled_by_the_pass_when_no_keeper_runs(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await _an_entry_voided_as_never_sent(paper)
    _the_abandoned_send_lands(paper, order_ref)

    await paper.pass_()

    assert paper.venue.cancel_calls == [paper.venue.option_orders[order_ref]["id"]]
    assert (paper.order_state(order_ref), paper.outcome(order_ref).standing) == ("canceled", EntryStanding.UNFILLED)


async def test_a_voided_entry_that_fills_before_its_cancel_lands_is_held_and_the_owner_can_flatten_it(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await _an_entry_voided_as_never_sent(paper)
    _the_abandoned_send_lands(paper, order_ref)

    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-late")

    assert paper.held() == {SYMBOL: 1.0}
    [notice] = [raised for raised in paper.repo.active_uncertainties() if raised["reason_code"] == FAILED_ENTER_FILLED_REASON_CODE]
    # The notice is about the contract the bot now holds, and says what the owner does about it.
    assert SYMBOL in notice["explanation"] and order_ref in notice["explanation"]
    assert "flatten" in notice["next_step"].lower()
    # No second entry is taken beside a position the strategy does not know it holds.
    assert admit_new_exposure(paper.repo, strategy_instance_id="a").reason_code == FAILED_ENTER_FILLED_REASON_CODE
    # Nothing closes it by itself: the strategy sends no exit. The owner stops the bot, as the
    # notice says, and the Flatten of exactly what is held is then offered.
    assert not (await _the_flatten_offered(paper)).available
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")
    flatten = await _the_flatten_offered(paper)
    assert flatten.available, (flatten.unavailable_reason_code, flatten.unavailable_reason)
    assert flatten.reduction_plan is not None
    assert [(leg.symbol, leg.quantity) for leg in flatten.reduction_plan.legs] == [(SYMBOL, 1.0)]

    async def current_context() -> RecoveryPolicyContext:
        context = await read_recovery_context(paper.clerk, strategy_instance_id="a")
        assert context is not None
        return context

    pressed = await execute_recovery_action(
        paper.clerk,
        request=RecoveryExecutionRequest(
            action_id="execute_safe_flatten", concurrency_token=flatten.concurrency_token,
            execution_ref=flatten.execution_ref, reason="owner-flatten", confirmed_limit=None,
        ),
        current_context=current_context,
    )

    # It goes out: one market order that sells to close the one contract held.
    assert pressed.applied
    closing = paper.venue.submitted_option_orders[-1]
    assert (closing.contract.occ_symbol, closing.side, closing.intent, closing.quantity, closing.order_type) == (
        SYMBOL, OrderSide.SELL, OptionPositionIntent.CLOSE, 1, OrderType.MARKET,
    )


async def _the_flatten_offered(paper: PaperOptions) -> RecoveryCapability:  # noqa: F811
    """The owner presses Reconcile now and reads the bot's Flatten, as the bot's page does."""
    await paper.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    context = await read_recovery_context(paper.clerk, strategy_instance_id="a")
    assert context is not None
    return next(item for item in build_recovery_catalog(context) if item.action_id == "execute_safe_flatten")


async def test_a_voided_entry_that_filled_where_no_stream_showed_it_is_found_by_the_pass_as_an_option_order(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """The abandoned send landed and filled while the stream was down: Alpaca's open orders no longer list it.

    The pass sees a contract the books do not hold, looks the voided
    entry's order up by its own id, and reads it as the option order it is.
    """
    order_ref = await _an_entry_voided_as_never_sent(paper)
    asked = next(order for order in paper.venue.submitted_option_orders if order is not None)
    paper.venue.option_orders[order_ref] = documented_option_order_payload(
        asked, client_order_id=order_ref, at=paper.venue._venue_stamp()
    )
    paper.venue.option_order_fills(order_ref, contracts=1, average_price="1.94")

    await paper.pass_()

    assert paper.venue.lookup_calls == [] and paper.venue.option_lookup_calls[-1] == order_ref
    assert paper.held() == {SYMBOL: 1.0} and paper.order_state(order_ref) == "filled"
    assert FAILED_ENTER_FILLED_REASON_CODE in {raised["reason_code"] for raised in paper.repo.active_uncertainties()}


# ── a closing order is never an entry ────────────────────────────────────


async def _a_working_closing_order(paper: PaperOptions) -> str:  # noqa: F811
    """The bot holds one contract and its EXIT's closing order works at Alpaca; that order's reference."""
    order_ref = await paper.resting()
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    sold = await paper.exit()
    closing_ref = next(ref for ref in sold.child_order_refs if ref != order_ref)
    assert paper.order_state(closing_ref) == "new" and paper.repo.order(closing_ref).role == "REDUCING"
    return closing_ref


async def test_the_entry_gates_question_counts_only_an_entry_order_never_a_working_closing_order(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _a_working_closing_order(paper)

    with paper.repo.write_fence() as conn:
        working_entry = reads.working_entry_order_for_strategy(conn, "a")

    assert working_entry is None
    # The bot's next entry waits for its EXIT, and is told so by that name.
    assert admit_new_exposure(paper.repo, strategy_instance_id="a").reason_code == "EXIT_IN_PROGRESS"


async def test_a_stopped_runs_working_closing_order_under_an_ended_exit_is_never_on_the_stopped_run_worklist(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Cancelling it would take a closing order out of the market. The worklist is of entries only."""
    closing_ref = await _a_working_closing_order(paper)
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")
    closing = paper.repo.order(closing_ref)
    # While its EXIT has not ended the order is that EXIT's, whatever its role. The books are set
    # here to the one state where only the role keeps it off the list: the EXIT has ended and
    # Alpaca still shows its order working. No path of this harness ends an EXIT that way.
    with paper.repo.write_fence() as conn:
        conn.execute(
            "UPDATE effect_operations SET state = 'failed' WHERE effect_operation_id = ?", (closing.effect_operation_id,)
        )
        conn.commit()
    assert paper.repo.active_exit_for_strategy("a") is None

    assert entries_owed_a_cancel(paper.repo) == []
    await cancel_entries_of_inactive_runs(paper.repo, trade=paper.venue)

    assert paper.venue.cancel_calls == [] and paper.order_state(closing_ref) == "new"


# ── a clock that is set back ─────────────────────────────────────────────


async def test_a_clock_set_back_before_the_entrys_own_admission_makes_the_entry_due_at_once(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.wait(5_000)
    paper.clock.advance(-40_000)

    done = await paper.round()

    # The wait can no longer be measured on this clock: an early cancel, never a late one.
    assert done.entries == WaitEndedRound(ended=(order_ref,))
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED


def test_a_clock_set_back_by_less_than_the_time_waited_delays_the_cancel_by_no_more_than_that() -> None:
    order = reads.OrderResource(
        order_ref="o", effect_operation_id="e", client_order_id="o", broker_order_id="b", role="ENTRY",
        broker_state="new", submitted_at_ms=1_000, updated_at_ms=1_000,
    )
    entry = RestingEntry(order=order, cancel_at_ms=21_000, admitted_at_ms=1_000)

    assert [entry.due(at) for at in (999, 1_000, 20_999, 21_000)] == [True, False, False, True]


# ── each entry is handled by itself ──────────────────────────────────────


async def test_a_slow_cancel_of_one_entry_does_not_hold_up_another_bots(paper: PaperOptions) -> None:  # noqa: F811
    slow, quick = await paper.resting("a"), await paper.resting("b")
    paper.wait(WAIT_MS)
    answered = asyncio.Event()
    real_cancel = paper.venue.cancel

    async def cancel_that_is_slow_for_the_first(order_id: str) -> None:
        if order_id == paper.venue.option_orders[slow]["id"]:
            await answered.wait()
        await real_cancel(order_id)

    paper.venue.cancel = cancel_that_is_slow_for_the_first  # type: ignore[method-assign]
    round_ = asyncio.create_task(paper.round())
    for _ in range(20):
        await asyncio.sleep(0)

    # The second bot's entry is cancelled and proven while the first is still waiting on Alpaca.
    assert paper.outcome(quick).standing is EntryStanding.UNFILLED
    assert paper.outcome(slow).standing is EntryStanding.CANCELLATION_REQUESTED

    answered.set()
    done = await asyncio.wait_for(round_, timeout=5)
    assert set(done.entries.ended) == {slow, quick}


async def test_an_entry_whose_cancel_raises_is_left_for_the_next_round_and_the_others_are_still_cancelled(
    paper: PaperOptions,  # noqa: F811
) -> None:
    broken, sound = await paper.resting("a"), await paper.resting("b")
    paper.wait(WAIT_MS)
    real_cancel = paper.venue.cancel

    async def cancel_that_breaks_for_the_first(order_id: str) -> None:
        if order_id == paper.venue.option_orders[broken]["id"]:
            raise RuntimeError("an answer nothing here expected")
        await real_cancel(order_id)

    paper.venue.cancel = cancel_that_breaks_for_the_first  # type: ignore[method-assign]

    done = await paper.round()

    assert done.entries == WaitEndedRound(ended=(sound,), still_due=(broken,))
    assert paper.outcome(sound).standing is EntryStanding.UNFILLED

    paper.venue.cancel = real_cancel  # type: ignore[method-assign]
    paper.wait(1_000)
    assert (await paper.round()).entries == WaitEndedRound(ended=(broken,))

    # The pass's step goes on the same way.
    third = await paper.resting("a")
    paper.venue.cancel = cancel_that_breaks_for_the_first  # type: ignore[method-assign]
    broken = third
    paper.wait(WAIT_MS)
    assert await cancel_entries_past_their_wait(paper.repo, trade=paper.venue) == WaitEndedRound(still_due=(third,))


@pytest.mark.parametrize("run_by", ["the keeper's round", "the pass"])
async def test_a_lease_found_lost_during_one_entrys_cancel_comes_out_of_the_round_and_out_of_the_pass(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch, run_by: str  # noqa: F811
) -> None:
    """A lost lease is the Clerk's own loss of the account, never one entry's failure to log and try again."""
    await paper.resting()
    paper.wait(WAIT_MS)
    real_cancel, real_renewal = paper.venue.cancel, paper.repo.renew_operation_claim
    cancel_answered = False

    async def cancel(order_id: str) -> None:
        nonlocal cancel_answered
        await real_cancel(order_id)
        cancel_answered = True

    def renewal_that_finds_the_lease_gone_after_the_cancel(**claim: object) -> bool:
        nonlocal cancel_answered
        if cancel_answered:
            cancel_answered = False
            raise ExecutionLeaseLost("another Clerk now holds this account", account_id=paper.repo.account_id)
        return real_renewal(**claim)

    paper.venue.cancel = cancel  # type: ignore[method-assign]
    monkeypatch.setattr(paper.repo, "renew_operation_claim", renewal_that_finds_the_lease_gone_after_the_cancel)

    with pytest.raises(ExecutionLeaseLost):
        await (paper.round() if run_by == "the keeper's round" else paper.pass_())

    assert len(paper.venue.cancel_calls) == 1


# ── a cancel that keeps failing ──────────────────────────────────────────


@pytest.mark.parametrize("alpaca", ["refuses_the_cancel", "cannot_be_reached"])
async def test_a_cancel_that_keeps_failing_the_same_way_is_recorded_once_not_once_a_round(
    paper: PaperOptions, alpaca: str  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    if alpaca == "refuses_the_cancel":
        paper.venue.cancel_error = BrokerOrderRejected("the order cannot be cancelled now", broker="alpaca")
    else:
        paper.venue.cancel_error = paper.venue.option_lookup_error = BrokerUnavailable("no answer", broker="alpaca")
    paper.wait(WAIT_MS)
    await paper.round()
    paper.wait(1_000)
    await paper.round()
    settled = paper.log()
    assert len(paper.recorded(order_ref, "ORDER_CANCEL_REQUESTED")) == 1
    assert len(paper.recorded(order_ref, "ORDER_CANCEL_UNCERTAIN")) == 1

    for _ in range(30):
        paper.wait(5_000)
        await paper.round()

    # Asked again each time, and nothing new to say: nothing new written.
    assert len(paper.venue.cancel_calls) == 32
    assert paper.log() == settled
    assert not paper.outcome(order_ref).proven and paper.set_aside_cents() == ONE_CONTRACT_SET_ASIDE

    # The moment Alpaca answers, the order is proven ended.
    paper.venue.cancel_error = paper.venue.option_lookup_error = None
    paper.wait(5_000)
    await paper.round()
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED and paper.set_aside_cents() == 0


async def test_a_cancel_refused_again_after_the_order_moved_is_recorded_again(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(units=2)
    paper.venue.cancel_error = BrokerOrderRejected("the order cannot be cancelled now", broker="alpaca")
    paper.wait(WAIT_MS)
    await paper.round()
    paper.wait(1_000)
    await paper.round()
    assert len(paper.recorded(order_ref, "ORDER_CANCEL_UNCERTAIN")) == 1

    # The order fills in part: the books learn something, and the cancel is still refused.
    paper.venue.option_order_fills(order_ref, contracts=1, average_price="1.94")
    paper.wait(1_000)
    await paper.round()

    assert paper.order_state(order_ref) == "partially_filled"
    assert len(paper.recorded(order_ref, "ORDER_CANCEL_UNCERTAIN")) == 2


# ── the reconciliation pass runs the same step ───────────────────────────


async def test_a_keeper_that_never_ran_is_covered_by_the_next_pass(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.wait(WAIT_MS + 15_000)

    await paper.pass_()

    assert paper.cancel_requests(order_ref) == [("ENTRY_WAIT_ENDED", paper.clock())]
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED
    assert paper.set_aside_cents() == 0


async def test_the_pass_leaves_an_entry_inside_its_wait_in_the_market(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.wait(WAIT_MS - 1_000)

    await paper.pass_()

    assert paper.venue.cancel_calls == []
    assert paper.outcome(order_ref).standing is EntryStanding.WORKING


async def _a_stopped_stock_run_with_a_working_entry(root: Path) -> list[tuple[str, str, str]]:
    """One pass over a stock-only account whose stopped run left an entry working; the log it wrote."""
    clock = _TestClock(BUDGETED_RTH_MS)
    repo = ClerkSqliteRepository.initialize(account_id="PA-STOCK", artifacts_root=root, clock=clock)
    try:
        repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
        submit_budgeted_test_run(repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", lifecycle_run_id="run-1")
        venue = _FakeTradePort()
        await submit_budgeted_test_enter(
            repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", decision_id="d-1", lifecycle_run_id="run-1",
            leg=_broker_leg(), trade=venue,
        )
        submit_stop_run(repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", lifecycle_run_id="run-1")
        _walk_clock_to(repo, clock() + 60_000)
        await reconcile_account(repo, read=_FakeReadPort(), trade=venue, pricing=UNPRICEABLE_RECOVERY)
        with repo.write_fence() as conn:
            rows = conn.execute(
                "SELECT transition_kind, summary_code, facts_json FROM custody_transitions ORDER BY sequence"
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]
    finally:
        repo.close()


async def test_a_stock_only_account_is_recorded_the_same_with_the_step_and_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_the_step = await _a_stopped_stock_run_with_a_working_entry(tmp_path / "with")

    async def no_step(*_args: object, **_kwargs: object) -> WaitEndedRound:
        return WaitEndedRound()

    monkeypatch.setattr(reconcile, "cancel_entries_past_their_wait", no_step)
    without_it = await _a_stopped_stock_run_with_a_working_entry(tmp_path / "without")

    assert with_the_step == without_it
    # The scenario is not an empty one: the pass cancelled the stopped run's working entry.
    assert "ORDER_CANCEL_REQUESTED" in [kind for kind, _summary, _facts in with_the_step]


async def test_on_books_with_no_option_entry_the_step_reads_nothing_and_writes_nothing(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-STOCK", artifacts_root=tmp_path, clock=_TestClock(BUDGETED_RTH_MS))
    try:
        before = repo.last_custody_sequence()

        done = await cancel_entries_past_their_wait(repo, trade=_FakeTradePort())

        assert done == WaitEndedRound() and repo.last_custody_sequence() == before
    finally:
        repo.close()


# ── what the worklist costs to read ──────────────────────────────────────


def _plan(repo: ClerkSqliteRepository, sql: str, params: tuple[object, ...]) -> list[str]:
    with repo.write_fence() as conn:
        return [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()]


def test_the_worklist_is_read_from_the_bound_orders_and_never_scans_the_custody_log(paper: PaperOptions) -> None:  # noqa: F811
    plan = _plan(paper.repo, reads.OPTION_ENTRIES_WITH_A_DEADLINE_SQL, reads.OPTION_ENTRIES_WITH_A_DEADLINE_PARAMS)

    # It starts at the orders bound to a contract, and reaches every other table by an index.
    assert plan[0].startswith("SCAN bound")
    assert [step for step in plan[1:] if step.startswith("SCAN")] == []
    assert any(step.startswith("SEARCH accepted USING INDEX") for step in plan)
    working = _plan(
        paper.repo, reads._WORKING_OPTION_ENTRY_CANDIDATE_SQL,
        tuple(sorted(reads.CANCELLABLE_ENTRY_BROKER_STATES)),
    )
    unanswered = _plan(
        paper.repo, reads._UNANSWERED_OPTION_ENTRY_CANDIDATE_SQL,
        tuple(sorted(reads.NONTERMINAL_EFFECT_STATES)),
    )
    assert any("ix_orders_entry_broker_state (role=? AND <expr>=?)" in step for step in working)
    assert any("ix_orders_entry_broker_state (role=? AND <expr>=? AND effect_state=?)" in step
               for step in unanswered)


class _StepsCounted:
    """How many steps SQLite's machine took on a store's connection: it grows with every row a query reads."""

    def __init__(self, repo: ClerkSqliteRepository) -> None:
        self._repo = repo
        self.steps = 0

    def __enter__(self) -> _StepsCounted:
        self._repo._conn.set_progress_handler(self._count, 1)
        return self

    def __exit__(self, *_raised: object) -> None:
        self._repo._conn.set_progress_handler(None, 1)

    def _count(self) -> int:
        self.steps += 1
        return 0


async def _a_stock_account_with_a_working_entry(root: Path, *, transitions: int) -> ClerkSqliteRepository:
    """A stock-only account whose one bot has an entry working, and ``transitions`` more records in its log."""
    clock = _TestClock(BUDGETED_RTH_MS)
    repo = ClerkSqliteRepository.initialize(account_id="PA-STOCK", artifacts_root=root, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    submit_budgeted_test_run(repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", lifecycle_run_id="run-1")
    sent = await submit_budgeted_test_enter(
        repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", decision_id="d-1", lifecycle_run_id="run-1",
        leg=_broker_leg(), trade=_FakeTradePort(),
    )
    assert sent.order_ref is not None
    order = repo.order(sent.order_ref)
    assert order is not None
    for number in range(transitions):
        fold_uncertain(repo, effect_operation_id=order.effect_operation_id, order_ref=order.order_ref, why=f"n{number}")
    return repo


async def test_a_stock_only_accounts_books_are_never_read_however_long_their_log(tmp_path: Path) -> None:
    """The keeper's idle round and the pass's step read no row of the custody log on books with no option order.

    Counted in the steps SQLite's machine takes, which grow with every row
    read: a log three thousand records longer costs not one step more.
    """
    costs: dict[int, tuple[int, int, int]] = {}
    for transitions in (0, 3_000):
        repo = await _a_stock_account_with_a_working_entry(tmp_path / str(transitions), transitions=transitions)
        try:
            written = repo.last_custody_sequence()
            keeper = DeadlineKeeper(
                repo=repo, trade=_FakeTradePort(), intake=ReentrantAsyncLock(), pricing=UNPRICEABLE_RECOVERY,
                off_loop=run_inline,
            )
            with _StepsCounted(repo) as idle_round:
                done = await keeper.tick()
            with _StepsCounted(repo) as the_step:
                await cancel_entries_past_their_wait(repo, trade=_FakeTradePort())
            working = [entry for entry in repo.entry_orders_for_strategy("spy-bot") if entry.broker_state == "accepted"]
            with _StepsCounted(repo) as the_gates_question, repo.write_fence() as conn:
                # The question is asked of a stock book's own orders now (#2961): it answers
                # this entry, working under its still-open ENTER, from the index and not the log.
                found = reads.working_entry_order_for_strategy(conn, "spy-bot")
            assert found is not None and found.order_ref == working[0].order_ref
            assert done.entries == WaitEndedRound() and repo.last_custody_sequence() == written
            costs[transitions] = (idle_round.steps, the_step.steps, the_gates_question.steps)
        finally:
            repo.close()

    assert costs[0] == costs[3_000]
    # The round also runs the keeper's other step, the scheduled closes, which reads the positions once.
    # The gate's question costs more than it did when it read only an empty option table on
    # stock books (#2961): it names the oldest working entry now, a sorted pick among few rows.
    the_round, the_step, the_gates_question = costs[0]
    assert 0 < the_round < 200 and 0 < the_step < 100 and 0 < the_gates_question < 200, costs[0]


async def test_one_round_of_the_keeper_reads_the_worklist_once(paper: PaperOptions, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    await paper.resting("a")
    await paper.resting("b")
    paper.wait(WAIT_MS)
    reads_made = 0
    real_read = reads.option_entries_with_a_deadline

    def counted(conn: object) -> object:
        nonlocal reads_made
        reads_made += 1
        return real_read(conn)

    monkeypatch.setattr(reads, "option_entries_with_a_deadline", counted)

    done = await paper.round()

    # Two entries due and cancelled; what was due and when to wake both came from one read.
    assert len(done.entries.ended) == 2 and reads_made == 1


def test_an_empty_option_worklist_has_bounded_cost_after_past_round_trips(tmp_path: Path) -> None:
    repo = open_option_books(tmp_path)
    costs: dict[int, int] = {}
    try:
        for past_trips in range(65):
            if past_trips in (0, 4, 12, 64):
                with _StepsCounted(repo) as counted:
                    assert resting_entries(repo) == []
                costs[past_trips] = counted.steps
            if past_trips == 64:
                break
            entry = accept_option_order(
                repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id=f"entry-{past_trips}",
            )
            fill(repo, entry, execution_id=f"entry-fill-{past_trips}", symbol=AAPL_CALL.symbol,
                 side="BUY", quantity=1, price=1.94)
            _the_broker_says(repo, entry, symbol=AAPL_CALL.symbol, status="filled", contracts=1)
            closing = accept_option_closing_order(
                repo, terms=AAPL_CALL, opened=entry, decision_id=f"exit-{past_trips}",
            )
            fill(repo, closing, execution_id=f"exit-fill-{past_trips}", symbol=AAPL_CALL.symbol,
                 side="SELL", quantity=1, price=1.90)
            _the_broker_says(repo, closing, symbol=AAPL_CALL.symbol, status="filled", contracts=1)
    finally:
        repo.close()

    assert max(costs.values()) < 200, costs


def test_terminal_unanswered_entries_and_stock_history_do_not_reopen_the_option_worklist(tmp_path: Path) -> None:
    repo = open_option_books(tmp_path, bots={"bot-a": "AAPL", **{f"bot-s-{n}": "SPY" for n in range(64)}})
    costs: dict[int | str, int] = {}
    try:
        for past_entries in range(65):
            if past_entries in (0, 4, 12, 64):
                with _StepsCounted(repo) as counted:
                    assert resting_entries(repo) == []
                costs[past_entries] = counted.steps
            if past_entries == 64:
                break
            never_sent = accept_option_order(
                repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id=f"never-sent-{past_entries}",
            )
            with repo.write_fence() as conn:
                conn.execute(
                    "UPDATE effect_operations SET state = 'failed' WHERE effect_operation_id = ?",
                    (never_sent.effect_operation_id,),
                )
                conn.commit()

        for number in range(64):
            stock = _accept_stock_order(
                repo, symbol="SPY", side="buy", shares=1, decision_id=f"stock-{number}", bot=f"bot-s-{number}",
            )
            fill(repo, stock, execution_id=f"stock-fill-{number}", symbol="SPY",
                 side="BUY", quantity=1, price=100)
            _the_broker_says(repo, stock, symbol="SPY", status="filled", contracts=1)
        with _StepsCounted(repo) as counted:
            assert resting_entries(repo) == []
        costs["mixed"] = counted.steps
    finally:
        repo.close()

    assert max(costs.values()) < 200, costs
