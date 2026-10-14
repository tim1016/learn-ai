"""A real Clerk sends one parent, recovers its legs and cancels that parent alone (#2913)."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.broker.alpaca import adapter
from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.legs_out_of_step import bot_is_under_legs_out_of_step
from app.broker.alpaca.clerk.sqlite.manual_order_replacement import resolve_captured_order
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_failed
from app.broker.alpaca.clerk.sqlite.resting_entries import resting_entries
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
from app.broker.contract.models import BrokerOrderEvent
from tests.broker.alpaca.clerk.sqlite.conftest import documented_option_listing
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import STRADDLE, STRADDLE_PICKS, worked_entry
from tests.broker.alpaca.clerk.sqlite.several_legs_paper_harness import SeveralLegsPaper
from tests.broker.alpaca.clerk.sqlite.several_legs_paper_harness import multi_paper as _multi_paper_fixture
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _gate
from tests.broker.alpaca.clerk.sqlite.test_several_legs_admission import IRON_CONDOR, IRON_CONDOR_PICKS
from tests.broker.alpaca.documented_examples import documented

multi_paper = _multi_paper_fixture


async def test_another_bots_unfilled_order_fences_an_opposite_leg_until_it_is_proven_not_sent(
    multi_paper: SeveralLegsPaper, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import PAID_PICKS, PAID_SPREAD, call, leg, plan, put

    repo = multi_paper.repo
    pending = accept_option_enter(
        repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="resting-buy", lifecycle_run_id="run-b",
        entry=worked_entry(
            plan(leg("long_call"), leg("long_put", right="put")),
            {"long_call": (call(590), "1.20", "1.24"), "long_put": (put(590), "1.16", "1.20")},
            units=1, now_ms=multi_paper.clock(),
        ),
        envelope=_gate(cash=3000),
    )
    assert repo.attributed_positions_for_strategy("b") == {}
    multi_paper.account.listed.update({
        contract.occ_symbol: documented_option_listing(contract) for contract, *_ in PAID_PICKS.values()
    })
    refused = await multi_paper.enter(units=1, decision="opposite-refused", plan=PAID_SPREAD, picks=PAID_PICKS)
    assert refused.state is EffectOperationState.REJECTED
    assert "OPTION_CONTRACT_HELD_ON_THE_OTHER_SIDE" in refused.explanation
    assert "outstanding order" in refused.explanation and call(590).occ_symbol in refused.explanation
    assert multi_paper.venue.option_submit_calls == [] and repo.attributed_positions_for_strategy("a") == {}

    fold_failed(
        repo, effect_operation_id=pending.effect_operation_id, order_ref=pending.order_ref,
        summary_code="REFUSED_BEFORE_SEND", reason="not sent", why="the other order never reached the broker",
    )
    taken = await multi_paper.enter(units=1, decision="opposite-now-clear", plan=PAID_SPREAD, picks=PAID_PICKS)
    assert taken.state is EffectOperationState.SUBMITTED, taken.explanation


async def test_the_entry_sends_one_parent_and_reads_every_listing_with_one_account_read(multi_paper: SeveralLegsPaper) -> None:
    receipt = await multi_paper.enter()
    assert receipt.state is EffectOperationState.SUBMITTED, receipt.explanation
    (parent,) = receipt.child_order_refs
    assert multi_paper.venue.option_submit_calls == [parent]
    assert sorted(multi_paper.account.listing_reads) == sorted(contract.occ_symbol for contract, *_ in STRADDLE_PICKS.values())
    assert multi_paper.account.option_account_reads == 1
    assert multi_paper.outcome(parent).standing is EntryStanding.WORKING
    assert [each.order.order_ref for each in resting_entries(multi_paper.repo)] == [parent]
    assert multi_paper.repo.all_order_refs() == frozenset({parent})
    for child in multi_paper.venue.option_orders[parent]["legs"]:
        captured = resolve_captured_order(multi_paper.repo, adapter.from_alpaca_order(child))
        assert captured is not None and multi_paper.repo.leg_parent(captured.order_ref) == parent


async def test_both_listings_share_one_bound_and_are_started_together(multi_paper: SeveralLegsPaper) -> None:
    original = multi_paper.account.get_option_contract
    both_started = asyncio.Event()
    started = []

    async def listing(contract):
        started.append(contract.occ_symbol)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        return await original(contract)

    multi_paper.account.get_option_contract = listing
    receipt = await multi_paper.enter()
    assert receipt.state is EffectOperationState.SUBMITTED, receipt.explanation
    assert len(started) == 2 and multi_paper.account.option_account_reads == 1


async def test_a_missing_second_listing_refuses_the_whole_order(multi_paper: SeveralLegsPaper) -> None:
    missing = list(multi_paper.account.listed)[1]
    del multi_paper.account.listed[missing]
    receipt = await multi_paper.enter()
    assert receipt.state is EffectOperationState.REJECTED and "OPTION_CONTRACT_NOT_LISTED" in receipt.explanation
    assert missing in receipt.explanation and multi_paper.venue.option_submit_calls == []


async def test_a_lost_submit_answer_recovers_before_a_leg_is_judged_foreign(multi_paper: SeveralLegsPaper) -> None:
    multi_paper.venue.option_answer_lost = True
    multi_paper.venue.option_lookup_error = BrokerUnavailable("the answer has not arrived yet", broker="alpaca")
    receipt = await multi_paper.enter()
    assert receipt.state is EffectOperationState.UNCERTAIN
    (parent,) = receipt.child_order_refs
    multi_paper.venue.option_lookup_error = None
    multi_paper.venue.option_lookup_calls.clear()
    child = multi_paper.venue.option_orders[parent]["legs"][0]
    sink = SqliteTradeUpdateEvidenceSink(repo=multi_paper.repo, intake=multi_paper.clerk.intake, reconciler=multi_paper.clerk)
    result = await sink.record_lifecycle_event(
        client_order_id=child["client_order_id"], event=BrokerOrderEvent(
            event_type="new", occurred_at_ms=multi_paper.clock(), price=None, quantity=None,
        ),
        event_key="early-leg", order=adapter.from_alpaca_order(child), recovery_source=None, recovery_window_limit=None,
    )
    assert result == "order_event"
    assert multi_paper.repo.external_orders() == []
    assert multi_paper.venue.option_lookup_calls == [parent]
    assert multi_paper.outcome(parent).standing is EntryStanding.WORKING


async def test_a_parent_answer_that_stays_unreadable_does_not_make_its_leg_foreign(multi_paper: SeveralLegsPaper) -> None:
    multi_paper.venue.option_answer_lost = True
    multi_paper.venue.option_lookup_error = BrokerUnavailable("the parent is unreadable", broker="alpaca")
    receipt = await multi_paper.enter()
    (parent,) = receipt.child_order_refs
    child = multi_paper.venue.option_orders[parent]["legs"][0]
    multi_paper.account.outside_orders = [adapter.from_alpaca_order(child)]
    result = await multi_paper.pass_()
    assert result.verdict == "stale" and multi_paper.repo.external_orders() == []
    assert multi_paper.outcome(parent).standing is EntryStanding.OUTCOME_UNKNOWN


@pytest.mark.parametrize("refusal", ["pre-contact", "broker-rejected"])
async def test_a_refused_historical_parent_does_not_swallow_an_unrelated_outside_order(
    multi_paper: SeveralLegsPaper, refusal: str,
) -> None:
    if refusal == "broker-rejected":
        multi_paper.venue.option_submit_error = BrokerOrderRejected("venue refused the structure", broker="alpaca")
        receipt = await multi_paper.enter()
        assert receipt.state is EffectOperationState.REJECTED
    else:
        accepted = accept_option_enter(
            multi_paper.repo, account_id=multi_paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a",
            decision_id="refused-before-contact", entry=worked_entry(STRADDLE, STRADDLE_PICKS, units=2, now_ms=multi_paper.clock()),
            envelope=_gate(cash=3000),
        )
        fold_failed(
            multi_paper.repo, effect_operation_id=accepted.effect_operation_id, order_ref=accepted.order_ref,
            summary_code="ENTRY_QUOTE_STALE", reason="refused", why="the quote became stale before contact",
        )
    foreign = deepcopy(documented("option_orders", "documented_responses.json")["MultilegOptionsOrderResponse"]["legs"][0])
    foreign.update(
        id="outside-order", client_order_id="outside-client", symbol=next(iter(STRADDLE_PICKS.values()))[0].occ_symbol,
        status="new", filled_qty="0", filled_avg_price=None, filled_at=None, type="market", order_type="market",
    )
    multi_paper.account.outside_orders = [adapter.from_alpaca_order(foreign)]
    result = await multi_paper.pass_()
    assert result.verdict != "stale"
    assert [row["broker_order_id"] for row in multi_paper.repo.external_orders()] == ["outside-order"]


async def test_four_legs_share_one_send_and_the_deadline_cancels_and_recovers_only_the_parent(
    multi_paper: SeveralLegsPaper, monkeypatch: pytest.MonkeyPatch,
) -> None:
    multi_paper.account.listed = {
        contract.occ_symbol: documented_option_listing(contract) for contract, *_quotes in IRON_CONDOR_PICKS.values()
    }
    receipt = await multi_paper.enter(units=2, plan=IRON_CONDOR, picks=IRON_CONDOR_PICKS)
    assert receipt.state is EffectOperationState.SUBMITTED, receipt.explanation
    (parent,) = receipt.child_order_refs
    assert multi_paper.venue.option_submit_calls == [parent]
    assert len(multi_paper.account.listing_reads) == 4 and multi_paper.account.option_account_reads == 1
    multi_paper.venue.legs_fill(parent, (1, 1, 1, 1))
    await multi_paper.restart()
    multi_paper.wait(21_000)
    await multi_paper.round()
    outcome = multi_paper.outcome(parent)
    assert outcome.proven and outcome.standing is EntryStanding.PART_FILLED and outcome.units_filled == 1
    assert not outcome.legs_out_of_step and len(outcome.contracts_filled) == 4
    assert multi_paper.venue.cancel_calls == [multi_paper.venue.option_orders[parent]["id"]]
    assert set(multi_paper.venue.option_lookup_calls) == {parent}


@pytest.mark.parametrize("counts", [(1, 1), (1, 0)])
async def test_the_deadline_cancels_only_the_parent_and_proves_each_partial_fill(
    multi_paper: SeveralLegsPaper, counts: tuple[int, int],
) -> None:
    parent = await multi_paper.resting(units=2)
    multi_paper.venue.legs_fill(parent, counts)
    await multi_paper.restart()
    multi_paper.wait(21_000)
    await multi_paper.round()
    outcome = multi_paper.outcome(parent)
    assert outcome.proven and outcome.standing is EntryStanding.PART_FILLED
    assert (outcome.units_filled, outcome.legs_out_of_step) == (min(counts), counts[0] != counts[1])
    assert multi_paper.venue.cancel_calls == [multi_paper.venue.option_orders[parent]["id"]]
    assert set(multi_paper.venue.option_lookup_calls) == {parent}
    assert bot_is_under_legs_out_of_step(multi_paper.repo, "a") == (counts[0] != counts[1])
    await multi_paper.round()
    assert len(multi_paper.venue.cancel_calls) == 1


async def test_a_leg_stream_execution_is_counted_once_without_a_parent_fill(multi_paper: SeveralLegsPaper) -> None:
    parent = await multi_paper.resting(units=2)
    multi_paper.venue.legs_fill(parent, (1, 0))
    child = multi_paper.venue.option_orders[parent]["legs"][0]
    sink = SqliteTradeUpdateEvidenceSink(repo=multi_paper.repo, intake=multi_paper.clerk.intake, reconciler=multi_paper.clerk)
    for _ in range(2):
        disposition = await sink.record_lifecycle_event(
            client_order_id=child["client_order_id"], event=BrokerOrderEvent(
                event_type="partial_fill", occurred_at_ms=multi_paper.clock(), price=3.14, quantity=1,
                execution_id="one-leg-execution",
            ), event_key="one-leg-frame", order=adapter.from_alpaca_order(child), recovery_source=None, recovery_window_limit=None,
        )
        assert disposition == "order_event"
    leg_ref = multi_paper.repo.order_legs(parent)[0].order_ref
    assert len(multi_paper.repo.fills_for_order(leg_ref)) == 1
    assert multi_paper.repo.fills_for_order(parent) == []
    assert multi_paper.held() == {child["symbol"]: 1.0}


async def test_a_cancel_answer_with_no_exact_read_stays_unknown_until_a_later_pass(multi_paper: SeveralLegsPaper) -> None:
    parent = await multi_paper.resting(units=2)
    multi_paper.venue.legs_fill(parent, (1, 1))
    multi_paper.venue.option_lookup_error = BrokerUnavailable("the cancel landed but no lookup answered", broker="alpaca")
    multi_paper.wait(21_000)
    await multi_paper.round()
    assert multi_paper.outcome(parent).standing is EntryStanding.OUTCOME_UNKNOWN
    multi_paper.venue.option_lookup_error = None
    await multi_paper.pass_()
    assert multi_paper.outcome(parent).standing is EntryStanding.PART_FILLED
    assert len(multi_paper.venue.cancel_calls) == 1


async def test_a_parent_fill_frame_with_no_execution_books_nothing_and_the_next_pass_proves_the_legs(
    multi_paper: SeveralLegsPaper,
) -> None:
    """#3103: Alpaca's frame for a several-leg parent's fill has its legs' executions, none of its own."""
    parent = await multi_paper.resting(units=1)
    multi_paper.venue.legs_fill(parent, (1, 1))
    order = multi_paper.venue.option_orders[parent]
    sink = SqliteTradeUpdateEvidenceSink(repo=multi_paper.repo, intake=multi_paper.clerk.intake, reconciler=multi_paper.clerk)

    frame = {
        "event": "fill",
        "timestamp_ms": multi_paper.clock(),
        "price": "2.13",
        "qty": "1",
        "legs": [{"execution_id": f"leg-{number}-execution", "qty": "1", "price": "1"} for number in range(2)],
        "order": order,
    }

    disposition = await sink.record_lifecycle_event(
        client_order_id=order["client_order_id"],
        event=adapter.from_alpaca_trade_update(frame),
        event_key="parent-fill-frame",
        order=adapter.from_alpaca_order(order),
        recovery_source=None,
        recovery_window_limit=None,
    )

    assert disposition == "withheld_order"
    assert multi_paper.repo.fills_for_order(parent) == []
    await multi_paper.pass_()
    assert multi_paper.outcome(parent).standing is EntryStanding.FILLED
    assert sorted(multi_paper.held().values()) == [1.0, 1.0]
    assert multi_paper.repo.active_uncertainties() == []
