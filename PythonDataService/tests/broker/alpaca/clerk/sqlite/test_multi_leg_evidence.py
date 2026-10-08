"""A parent answer counts per-leg evidence and never turns a parent into a fill (#2913)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_option_enter
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding, entry_outcome
from app.broker.alpaca.clerk.sqlite.legs_out_of_step import (
    bot_is_under_legs_out_of_step,
    end_notices_of_flat_bots,
    refresh_legs_out_of_step_notices,
)
from app.broker.alpaca.clerk.sqlite.option_order_evidence import MultiLegOrderEvidence, multi_leg_order_evidence
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    fold_failed,
    fold_order_evidence,
    fold_order_submission_acknowledgement,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import ReductionIntent, admit_new_exposure, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import Capability
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    ACCOUNT_ID,
    RUN_ID,
    accept_option_closing_order,
    fill,
)
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import (
    PAID_PICKS,
    PAID_SPREAD,
    RECEIVED_PICKS,
    RECEIVED_SPREAD,
    worked_entry,
)
from tests.broker.alpaca.clerk.test_simulated_multi_leg_orders import _bind
from tests.broker.alpaca.clerk.test_simulated_multi_leg_orders import world as _world_fixture

world = _world_fixture


async def _order(
    world: tuple[ClerkSqliteRepository, SyntheticBroker], *, received: bool = False, decision: str = "d1", sid: str = "bot-a",
) -> tuple[EnterSubmission, MultiLegOrderEvidence]:
    books, port = world
    plan, picks = (RECEIVED_SPREAD, RECEIVED_PICKS) if received else (PAID_SPREAD, PAID_PICKS)
    entry = worked_entry(plan, picks, units=2, now_ms=NOON)
    accepted = accept_option_enter(
        books, account_id=ACCOUNT_ID, strategy_instance_id=sid, decision_id=decision, lifecycle_run_id=RUN_ID, entry=entry,
    )
    assert accepted.order_ref is not None
    _bind(port, accepted.order_ref, entry)
    answer = await port.submit_option_order(entry.order, client_order_id=accepted.order_ref)
    return accepted, multi_leg_order_evidence(answer)


def _part(evidence: MultiLegOrderEvidence, counts: tuple[int, int], *, state: str) -> MultiLegOrderEvidence:
    return evidence.model_copy(update={
        "status": state, "units_filled": min(counts),
        "legs": tuple(
            leg.model_copy(update={
                "filled_quantity": float(count), "filled_avg_price": leg.filled_avg_price if count else None,
                "status": state,
            })
            for leg, count in zip(evidence.legs, counts, strict=True)
        ),
    })


async def test_a_real_submit_answer_keeps_leg_ids_but_counts_no_fill(world) -> None:
    books, _port = world
    accepted, evidence = await _order(world)
    fold_order_submission_acknowledgement(books, effect_operation_id=accepted.effect_operation_id, order=evidence)
    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.OUTCOME_UNKNOWN
    for leg, observed in zip(books.order_legs(accepted.order_ref), evidence.legs, strict=True):
        assert books.order(leg.order_ref).broker_order_id == observed.order_id
        assert books.fills_for_order(leg.order_ref) == []
    assert books.fills_for_order(accepted.order_ref) == []


async def test_partial_leg_reports_in_either_order_and_twice_count_one_whole_unit(world) -> None:
    books, _port = world
    accepted, evidence = await _order(world)
    for counts in ((1, 0), (1, 1), (1, 1)):
        observed = _part(evidence, counts, state="partially_filled")
        if counts == (1, 1):
            observed = observed.model_copy(update={"legs": tuple(reversed(observed.legs))})
        fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=observed)
    final = _part(evidence, (1, 1), state="canceled")
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=final)
    outcome = entry_outcome(books, accepted.order_ref)
    assert (outcome.standing, outcome.units_filled, outcome.legs_out_of_step) == (EntryStanding.PART_FILLED, 1, False)
    assert outcome.proven and list(outcome.contracts_filled.values()) == [1, 1]
    assert not bot_is_under_legs_out_of_step(books, "bot-a")
    assert books.effect_operation(accepted.effect_operation_id).state == "in_progress"


@pytest.mark.parametrize("received", [False, True], ids=["one bought contract", "one uncovered sold contract"])
async def test_a_final_uneven_fill_holds_only_this_bot_and_allows_a_confirmed_reduction(world, received: bool) -> None:
    books, _port = world
    accepted, evidence = await _order(world, received=received)
    final = _part(evidence, (1, 0), state="canceled")
    for _ in range(2):
        fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=final)
    outcome = entry_outcome(books, accepted.order_ref)
    assert outcome.standing is EntryStanding.PART_FILLED and outcome.proven
    assert outcome.units_filled == 0 and outcome.legs_out_of_step
    assert bot_is_under_legs_out_of_step(books, "bot-a")
    assert not admit_new_exposure(books, strategy_instance_id="bot-a").allowed
    assert admit_new_exposure(books, strategy_instance_id="bot-b").allowed
    symbol = evidence.legs[0].symbol
    assert decide_capability(
        books, capability=Capability.REDUCE, strategy_instance_id="bot-a",
        reduction_intent=ReductionIntent(symbol=symbol, side="BUY" if received else "SELL", quantity=1),
    ).allowed
    notice = books.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code="OPTION_LEGS_OUT_OF_STEP", strategy_instance_id="bot-a",
    )
    assert all(leg.symbol in notice["explanation"] for leg in evidence.legs)
    assert ("not covered" in notice["explanation"]) == received
    assert end_notices_of_flat_bots(books) == ()
    if not received:
        closing = accept_option_closing_order(
            books, terms=ContractTerms(symbol=symbol, multiplier=100), opened=accepted, decision_id="close",
        )
        fill(books, closing, execution_id="close-one", symbol=symbol, side="SELL", quantity=1, price=2.0)
        assert end_notices_of_flat_bots(books) == ("bot-a",)
        assert not bot_is_under_legs_out_of_step(books, "bot-a")


async def test_a_parent_ended_with_no_leg_filled_is_proven_unfilled(world) -> None:
    books, _port = world
    accepted, evidence = await _order(world)
    fold_order_evidence(
        books, effect_operation_id=accepted.effect_operation_id, order=_part(evidence, (0, 0), state="canceled"),
    )
    outcome = entry_outcome(books, accepted.order_ref)
    assert outcome.standing is EntryStanding.UNFILLED and outcome.proven
    assert books.effect_operation(accepted.effect_operation_id).state == "failed"


@pytest.mark.parametrize("counts", [(0, 0), (1, 0)])
async def test_a_canceled_parent_reporting_a_unit_keeps_custody_and_cash_until_each_leg_is_recorded(world, counts) -> None:
    from decimal import Decimal

    from app.broker.alpaca.clerk.sqlite.envelope_reservations import entry_cash_claims
    from tests.broker.alpaca.clerk.sqlite.test_several_legs_admission import _reserve

    books, _port = world
    accepted, evidence = await _order(world)
    assert _reserve(books, worked_entry(PAID_SPREAD, PAID_PICKS, units=2, now_ms=NOON)).order_ref == accepted.order_ref
    delayed = _part(evidence, counts, state="canceled").model_copy(update={"units_filled": 1})
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=delayed)

    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.OUTCOME_UNKNOWN
    assert books.effect_operation(accepted.effect_operation_id).state == "unknown"
    assert books.order_fills_short_of_broker_cumulative(accepted.order_ref)
    assert accepted.effect_operation_id in {item.effect_operation_id for item in books.reconcilable_effect_operations()}
    with books.write_fence() as conn:
        claim = next(item for item in entry_cash_claims(conn, seen_before_ms=NOON + 1) if item.order_ref == accepted.order_ref)
    # Two units at $194 set aside each, plus the recorded 19-cent fee provision.
    assert claim.unfilled_cost == Decimal("388.00") and claim.unfilled_fee == Decimal("0.19")
    assert books.fills_for_order(accepted.order_ref) == []

    fold_order_evidence(
        books, effect_operation_id=accepted.effect_operation_id, order=_part(evidence, (1, 1), state="canceled"),
    )
    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.PART_FILLED
    assert not books.order_fills_short_of_broker_cumulative(accepted.order_ref)
    with books.write_fence() as conn:
        claim = next(item for item in entry_cash_claims(conn, seen_before_ms=NOON + 1) if item.order_ref == accepted.order_ref)
    assert claim.unfilled_cost == claim.unfilled_fee == Decimal("0")


async def test_a_counted_leg_with_no_price_stays_unknown_then_recovers_on_a_readable_answer(world) -> None:
    books, _port = world
    accepted, evidence = await _order(world)
    final = _part(evidence, (1, 0), state="canceled")
    broken = final.model_copy(update={"legs": (final.legs[0].model_copy(update={"filled_avg_price": None}), final.legs[1])})
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=broken)
    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.OUTCOME_UNKNOWN
    assert not bot_is_under_legs_out_of_step(books, "bot-a")
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=final)
    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.PART_FILLED
    assert bot_is_under_legs_out_of_step(books, "bot-a")


async def test_a_reported_whole_unit_needs_every_leg_at_its_captured_ratio(world) -> None:
    from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import STRADDLE_PICKS, leg, plan

    books, port = world
    entry = worked_entry(plan(leg("long_call", ratio=2), leg("long_put", right="put")), STRADDLE_PICKS, units=2, now_ms=NOON)
    accepted = accept_option_enter(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="ratio", lifecycle_run_id=RUN_ID, entry=entry,
    )
    _bind(port, accepted.order_ref, entry)
    evidence = multi_leg_order_evidence(await port.submit_option_order(entry.order, client_order_id=accepted.order_ref))
    fold_order_evidence(
        books, effect_operation_id=accepted.effect_operation_id, order=_part(evidence, (1, 1), state="canceled"),
    )
    assert books.order_fills_short_of_broker_cumulative(accepted.order_ref)
    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.OUTCOME_UNKNOWN
    fold_order_evidence(
        books, effect_operation_id=accepted.effect_operation_id,
        order=_part(evidence, (2, 1), state="canceled").model_copy(update={"units_filled": 1}),
    )
    assert not books.order_fills_short_of_broker_cumulative(accepted.order_ref)
    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.PART_FILLED


async def test_a_filled_parent_with_incomplete_leg_counts_is_not_a_proven_partial_fill(world) -> None:
    books, _port = world
    accepted, evidence = await _order(world)
    fold_order_evidence(
        books, effect_operation_id=accepted.effect_operation_id, order=_part(evidence, (1, 1), state="filled"),
    )
    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.OUTCOME_UNKNOWN


async def test_a_late_fill_on_a_failed_parent_is_fenced_by_each_leg_it_actually_filled(world) -> None:
    books, _port = world
    accepted, evidence = await _order(world)
    fold_failed(
        books, effect_operation_id=accepted.effect_operation_id, order_ref=accepted.order_ref,
        summary_code="REFUSED_BEFORE_SEND", reason="refused", why="the entry was refused before the send",
    )
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=evidence)
    notice = books.active_uncertainty(
        scope="CUSTODY_SUBJECT", strategy_instance_id="bot-a", reason_code="FAILED_ENTER_FILLED",
    )
    assert notice is not None and all(leg.symbol in notice["explanation"] for leg in evidence.legs)
    assert not admit_new_exposure(books, strategy_instance_id="bot-a").allowed


@pytest.mark.parametrize("filled", [False, True])
async def test_a_known_child_prevents_every_parent_absence_proof(world, filled) -> None:
    from app.broker.alpaca.clerk.sqlite.order_evidence import (
        SUBMIT_ABSENCE_SUMMARY_CODE,
        _fold_absence_if_past_grace,
        entry_never_accepted_durably,
        order_never_reached_broker,
    )
    from app.broker.alpaca.clerk.sqlite.order_projection import ORDER_ENDED_SQL
    from tests.broker.alpaca.clerk.sqlite.conftest import _TestClock

    books, _port = world
    accepted, evidence = await _order(world)
    child = books.order_legs(accepted.order_ref)[0]
    observed = evidence.legs[0] if filled else evidence.legs[0].model_copy(update={
        "filled_quantity": 0.0, "filled_avg_price": None, "status": "new",
    })
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=observed, order_ref=child.order_ref)
    clock = books.clock
    assert isinstance(clock, _TestClock)
    for _ in range(31):
        clock.advance(1_000)
        books.renew_execution_lease()
    assert not order_never_reached_broker(books, books.order(accepted.order_ref))
    _fold_absence_if_past_grace(
        books, effect_operation_id=accepted.effect_operation_id, order_ref=accepted.order_ref,
        uncertain_since_ms=NOON,
    )
    assert books.effect_operation(accepted.effect_operation_id).state != "failed"
    # A receipt left by an older process ceases to prove absence when a child contradicts it.
    fold_failed(
        books, effect_operation_id=accepted.effect_operation_id, order_ref=accepted.order_ref,
        summary_code=SUBMIT_ABSENCE_SUMMARY_CODE, reason="absent", why="an older absence receipt",
    )
    assert not entry_never_accepted_durably(books, books.order(accepted.order_ref))
    with books.write_fence() as conn:
        ended = conn.execute(
            "SELECT " + ORDER_ENDED_SQL + " FROM orders o JOIN effect_operations e "
            "ON e.effect_operation_id=o.effect_operation_id WHERE o.order_ref=?", (accepted.order_ref,),
        ).fetchone()[0]
    assert not ended and books.position("bot-a", child.symbol) == (2 if filled else 0)
    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.OUTCOME_UNKNOWN


async def test_a_never_sent_parent_is_not_owed_leg_ids_but_a_known_child_is(world) -> None:
    books, _port = world
    accepted, evidence = await _order(world)
    fold_failed(
        books, effect_operation_id=accepted.effect_operation_id, order_ref=accepted.order_ref,
        summary_code="ENTRY_QUOTE_STALE", reason="never sent", why="pre-contact quote refusal",
    )
    assert books.option_parents_awaiting_leg_ids(evidence.legs[0].symbol) == ()
    child = books.order_legs(accepted.order_ref)[0]
    fold_order_evidence(
        books, effect_operation_id=accepted.effect_operation_id, order=evidence.legs[0], order_ref=child.order_ref,
    )
    assert books.option_parents_awaiting_leg_ids(evidence.legs[1].symbol) == (accepted.order_ref,)


async def test_buying_power_counts_its_own_child_evidence_as_one_order_but_detects_another_bot(world) -> None:
    from app.broker.alpaca.clerk.sqlite.entry_buying_power import _another_order_moved

    books, _port = world
    accepted, evidence = await _order(world)
    with books.write_fence() as conn:
        before_fill = conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM custody_transitions").fetchone()[0]
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=evidence)

    def another_moved() -> bool:
        with books.write_fence() as conn:
            return _another_order_moved(
                conn, order_ref=accepted.order_ref, effect_operation_id=accepted.effect_operation_id,
                broker_order_id=evidence.order_id, after_sequence=before_fill,
            )

    assert not another_moved()
    await _order(world, sid="bot-b", decision="other-bot-entry")
    assert another_moved()


def test_notice_recovery_on_books_without_option_parents_never_walks_stock_history(world, monkeypatch) -> None:
    books, _port = world

    def stock_history_walk(*_args, **_kwargs):
        raise AssertionError("notice recovery walked unrelated stock history")

    monkeypatch.setattr(books, "strategy_instances", stock_history_walk)
    monkeypatch.setattr(books, "entry_orders_for_strategy", stock_history_walk)
    refresh_legs_out_of_step_notices(books)
    assert not bot_is_under_legs_out_of_step(books, "bot-a")


async def test_an_overfilled_leg_in_a_canceled_parent_is_unknown(world) -> None:
    books, _port = world
    accepted, evidence = await _order(world)
    fold_order_evidence(
        books, effect_operation_id=accepted.effect_operation_id, order=_part(evidence, (3, 1), state="canceled"),
    )
    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.OUTCOME_UNKNOWN


@pytest.mark.parametrize("received", [False, True])
async def test_multi_leg_buying_power_records_completion_and_restart_even_with_judgment_off(world, received) -> None:
    from app.broker.alpaca.clerk.sqlite.entry_buying_power import COMPARED_KIND, EntryBuyingPowerWatch
    from app.broker.alpaca.clerk.sqlite.facts import EntryBuyingPowerComparedFacts
    from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock

    books, _port = world
    accepted, evidence = await _order(world, received=received)
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=evidence)
    watch = EntryBuyingPowerWatch(repo=books, read=object(), intake=ReentrantAsyncLock(), judged=lambda: False)
    watch.sent(("bot-a", "d1"), order_ref=accepted.order_ref, sent_at_ms=NOON)
    assert await watch.round(last_pass_at_ms=NOON, last_pass_clean=True) == (accepted.order_ref,)
    facts = EntryBuyingPowerComparedFacts.from_facts_json(books.first_effect_transition(
        effect_operation_id=accepted.effect_operation_id, transition_kind=COMPARED_KIND,
    )["facts_json"])
    assert (facts.result, facts.contracts_asked, facts.contracts_filled, facts.holds) == ("unknown", 4, 4, False)
    # Two units: +$388 paid, or -$614 received. A $1,000 requirement makes the latter cost $386.
    assert (facts.premium_paid_cents, facts.cost_of_filled_cents) == ((-61_400, 38_600) if received else (38_800, 38_800))
    later, later_evidence = await _order(world, decision="d2", received=received, sid="bot-b")
    fold_order_evidence(books, effect_operation_id=later.effect_operation_id, order=later_evidence)
    restarted = EntryBuyingPowerWatch(repo=books, read=object(), intake=ReentrantAsyncLock(), judged=lambda: False)
    assert await restarted.round(last_pass_at_ms=NOON, last_pass_clean=True) == (later.order_ref,)
    recorded = EntryBuyingPowerComparedFacts.from_facts_json(books.first_effect_transition(
        effect_operation_id=later.effect_operation_id, transition_kind=COMPARED_KIND,
    )["facts_json"])
    assert recorded.reason_code == "CLERK_RESTARTED" and not recorded.holds


async def test_a_canceled_parent_waits_for_every_counted_execution_on_its_legs(world) -> None:
    from dataclasses import replace

    books, _port = world
    accepted, evidence = await _order(world)
    fold_order_submission_acknowledgement(
        books, effect_operation_id=accepted.effect_operation_id, order=_part(evidence, (2, 2), state="canceled"),
    )
    for leg in books.order_legs(accepted.order_ref):
        fill(
            books, replace(accepted, order_ref=leg.order_ref), execution_id="one-" + leg.leg_id,
            symbol=leg.symbol, side=leg.side.upper(), quantity=1, price=2.0,
        )
    assert entry_outcome(books, accepted.order_ref).standing is EntryStanding.OUTCOME_UNKNOWN


async def test_a_failed_multi_leg_buying_power_comparison_still_records_one_unknown(world, monkeypatch) -> None:
    from app.broker.alpaca.clerk.sqlite import entry_buying_power
    from app.broker.alpaca.clerk.sqlite.facts import EntryBuyingPowerComparedFacts
    from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock

    books, _port = world
    accepted, evidence = await _order(world)
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=evidence)
    watch = entry_buying_power.EntryBuyingPowerWatch(
        repo=books, read=object(), intake=ReentrantAsyncLock(), judged=lambda: False,
    )
    watch.sent(("bot-a", "d1"), order_ref=accepted.order_ref, sent_at_ms=NOON)

    def fails(*_args, **_kwargs):
        raise ValueError("comparison arithmetic unavailable")

    monkeypatch.setattr(entry_buying_power, "_compare", fails)
    assert await watch.round(last_pass_at_ms=NOON, last_pass_clean=True) == (accepted.order_ref,)
    assert await watch.round(last_pass_at_ms=NOON, last_pass_clean=True) == ()
    recorded = EntryBuyingPowerComparedFacts.from_facts_json(books.first_effect_transition(
        effect_operation_id=accepted.effect_operation_id, transition_kind=entry_buying_power.COMPARED_KIND,
    )["facts_json"])
    assert recorded.reason_code == "COMPARISON_FAILED" and not recorded.holds



async def test_an_answered_uneven_entry_does_not_hold_a_later_balanced_entry_in_the_same_contracts(world) -> None:
    from app.broker.alpaca.clerk.sqlite.exit_resolution import _fold_attributed_flat

    books, _port = world
    accepted, evidence = await _order(world)
    fold_order_evidence(
        books, effect_operation_id=accepted.effect_operation_id, order=_part(evidence, (1, 0), state="canceled"),
    )
    symbol = evidence.legs[0].symbol
    closing = accept_option_closing_order(
        books, terms=ContractTerms(symbol=symbol, multiplier=100), opened=accepted, decision_id="close-first",
    )
    fill(books, closing, execution_id="close-first", symbol=symbol, side="SELL", quantity=1, price=2.0)
    _fold_attributed_flat(books, closing.effect_operation_id, accepted.order_ref)
    assert end_notices_of_flat_bots(books) == ("bot-a",)
    later, later_evidence = await _order(world, decision="d2")
    fold_order_evidence(books, effect_operation_id=later.effect_operation_id, order=later_evidence)
    refresh_legs_out_of_step_notices(books)
    assert not bot_is_under_legs_out_of_step(books, "bot-a")


async def test_the_journal_keeps_the_contract_that_filled_when_no_whole_structure_filled(world, tmp_path) -> None:
    from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
    from app.broker.alpaca.clerk.sqlite.entry_outcome import SettledEntry, settled_now
    from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
    from app.engine.strategy.signal_intent import SignalIntentKind
    from app.services.bot_dry_run import DryRunActivityJournal
    from app.services.bot_trade_strategy import _journal_option_fill

    books, port = world
    accepted, evidence = await _order(world)
    fold_order_evidence(
        books, effect_operation_id=accepted.effect_operation_id, order=_part(evidence, (1, 0), state="canceled"),
    )
    said = SettledEntry.of(
        settled_now(entry_outcome(books, accepted.order_ref), until_ms=NOON, now_ms=NOON),
        order_ref=accepted.order_ref, recorded=False,
    )
    journal = DryRunActivityJournal(tmp_path / "journal")
    _journal_option_fill(
        journal, clerk=SqliteAlpacaClerkFacade(repo=books, read=port, trade=port, account_mode="paper"),
        binding=SimpleNamespace(strategy_instance_id="bot-a", run_id=RUN_ID), options=SimpleNamespace(underlying="SPY"),
        account_id=synthetic_account_id_for_strategy("bot-a"), evaluation=SimpleNamespace(evaluation_id="d1"),
        intent=SimpleNamespace(kind=SignalIntentKind.ENTER), receipt=SimpleNamespace(child_order_refs=(accepted.order_ref,)),
        bar_ref="bar:one", settled=said,
    )
    (row,) = journal.tail(5)
    assert (row.symbol, row.quantity, row.entry_outcome) == (evidence.legs[0].symbol, 1, "part_filled_then_cancelled")
