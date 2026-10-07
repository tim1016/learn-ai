"""The cancel of a resting option entry when its owner's wait runs out (#2913).

Each test rests a bought call at a Paper account's venue and lets the wait
pass. What is asserted is what the owner would see: whether the order is
still in the market, what the bot holds, and what is still set aside for it.

One contract costs $194.00 to buy and six cents of fees are set aside with
it, so a whole entry of one contract claims $194.06 of its bot's budget.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite import reconcile
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter, submit_accepted_enter, submit_enter
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts
from app.broker.alpaca.clerk.sqlite.order_evidence import submit_absence_grace_ms
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.resting_entries import (
    WaitEndedRound,
    cancel_entries_past_their_wait,
    entries_past_their_wait,
    resting_entries,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import FAILED_ENTER_FILLED_REASON_CODE
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
from tests.broker.alpaca.clerk.sqlite.conftest import (
    FIXTURE_RTH_MS,
    _broker_leg,
    _FakeReadPort,
    _FakeTradePort,
    _TestClock,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    SYMBOL,
    WAIT_MS,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)
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
    clock = _TestClock(FIXTURE_RTH_MS)
    repo = ClerkSqliteRepository.initialize(account_id="PA-STOCK", artifacts_root=root, clock=clock)
    try:
        repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
        submit_start_run(repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", lifecycle_run_id="run-1")
        venue = _FakeTradePort()
        await submit_enter(
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
    repo = ClerkSqliteRepository.initialize(account_id="PA-STOCK", artifacts_root=tmp_path, clock=_TestClock(FIXTURE_RTH_MS))
    try:
        before = repo.last_custody_sequence()

        done = await cancel_entries_past_their_wait(repo, trade=_FakeTradePort())

        assert done == WaitEndedRound() and repo.last_custody_sequence() == before
    finally:
        repo.close()
