"""Shared Options session rules with actual observed outcomes, worked by hand."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.options_backtest.results import EventKind, ExitReason, Fill
from app.options_bots.session import (
    CancelEntry,
    CloseLeg,
    ExecutionOutcome,
    OpenLeg,
    OptionsSession,
    Phase,
    SessionState,
)
from tests.options_backtest._market import (
    D,
    TableMarket,
    after_open,
    at,
    contract,
    flat,
    leg,
    picked,
    setup,
    steady,
)

CALL = contract("call", "500")
COVER = contract("call", "505")


def _market(*, close_at_stop: bool = False) -> TableMarket:
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: {**flat("2.00", 5, 60), **({7: ("2.00", "1.40")} if close_at_stop else {})}},
    )


def _observed(
    identity: str,
    *,
    minute: int = 6,
    quantity: int = 1,
    price: str = "2.00",
    kind: str = "entry",
    terminal: bool = True,
    entry_number: int = 0,
) -> ExecutionOutcome:
    return ExecutionOutcome(
        outcome_id=identity,
        leg_id="call",
        entry_number=entry_number,
        kind=kind,
        terminal=terminal,
        fill=Fill("call", CALL, "buy" if kind == "entry" else "sell", quantity, 100, at(minute), D(price), D(price)),
    )


def test_no_position_or_stop_is_invented_from_order_acceptance() -> None:
    rules = OptionsSession(setup(leg(stop=("percent", "25"))), _market(close_at_stop=True))

    first = rules.advance(at(5))
    assert len(first.decisions) == 1 and isinstance(first.decisions[0], OpenLeg)
    unknown = ExecutionOutcome("resting", "call", 0, "entry")
    assert rules.advance(at(6), (unknown,)).decisions == ()
    assert rules.advance(at(7)).decisions == ()
    assert rules.state.legs[0].position is None
    assert rules.state.legs[0].phase is Phase.ORDERED


def test_observed_average_sets_the_stop_and_partial_entry_cancels_remainder_once() -> None:
    rules = OptionsSession(setup(leg(contracts=3, stop=("percent", "25"))), _market(close_at_stop=True))
    rules.advance(at(5))

    partial = _observed("partial", quantity=1, price="2.50", terminal=False)
    first = rules.advance(at(6), (partial,))
    assert [type(decision) for decision in first.decisions] == [CancelEntry]
    assert rules.state.legs[0].position.stop_level == D("1.875")
    assert rules.state.legs[0].position.contracts_held == 1
    assert rules.advance(at(6), (partial,)).decisions == ()

    stopped = rules.advance(at(7))
    assert len(stopped.decisions) == 1
    close = stopped.decisions[0]
    assert isinstance(close, CloseLeg)
    assert (close.contracts, close.reason) == (1, ExitReason.STOP)
    assert rules.advance(at(8)).decisions == ()


def test_partial_close_waits_for_entry_terminal_and_consumes_reentry_only_on_its_fill() -> None:
    rules = OptionsSession(
        setup(leg(contracts=3, stop=("percent", "25"), reentry=("at_once", 1)), no_reentry_after=after_open(7)),
        _market(close_at_stop=True),
    )
    rules.advance(at(5))
    rules.advance(at(6), (_observed("partial", quantity=1, terminal=False),))
    rules.advance(at(7))

    closed = _observed("closed", minute=8, price="1.30", kind="close")
    assert rules.advance(at(8), (closed,)).decisions == ()
    assert rules.state.legs[0].position.contracts_held == 0
    assert rules.state.legs[0].phase is Phase.HELD
    assert rules.state.legs[0].reentries_left == 1

    # The stop was before the cutoff: proving the cancel later does not erase its earned re-entry.
    cancellation = ExecutionOutcome("cancelled", "call", 0, "entry", terminal=True)
    reopened = rules.advance(at(9), (cancellation,))
    assert len(reopened.decisions) == 1 and isinstance(reopened.decisions[0], OpenLeg)
    assert reopened.decisions[0].entry_number == 1
    assert rules.state.legs[0].reentries_left == 1

    rules.advance(at(10), (_observed("reentry", minute=10, entry_number=1, quantity=2),))
    assert rules.state.legs[0].reentries_left == 0
    assert rules.state.legs[0].position.contracts_held == 2


def test_late_fill_after_partial_close_stays_owned_until_entire_attempt_is_flat() -> None:
    rules = OptionsSession(setup(leg(contracts=3, stop=("percent", "25"))), _market(close_at_stop=True))
    rules.advance(at(5))
    rules.advance(at(6), (_observed("first", terminal=False),))
    rules.advance(at(7))
    rules.advance(at(8), (_observed("close-first", minute=8, kind="close"),))
    assert rules.state.legs[0].position.contracts_held == 0

    rules.advance(at(9), (_observed("late-second", minute=9, quantity=2, terminal=True),))
    assert rules.state.legs[0].position.contracts_held == 1
    assert rules.state.legs[0].position.close.reason is ExitReason.STOP
    rules.advance(at(10), (_observed("close-all", minute=10, quantity=2, kind="close"),))
    assert rules.state.legs[0].phase is Phase.DONE
    assert rules.state.legs[0].position is None


def test_reentry_and_held_custody_survive_json_restore_without_duplicate_order() -> None:
    written = setup(leg(stop=("percent", "25"), reentry=("at_once", 1)))
    market = _market(close_at_stop=True)
    original = OptionsSession(written, market)
    original.advance(at(5))
    entry = _observed("actual")
    original.advance(at(6), (entry,))

    restored = OptionsSession(written, market, state=SessionState.model_validate_json(original.state.model_dump_json()))
    assert restored.advance(at(6), (entry,)).decisions == ()
    close = restored.advance(at(7)).decisions[0]
    assert isinstance(close, CloseLeg) and close.contracts == 1
    restored.advance(at(8), (_observed("close", minute=8, kind="close", price="1.30"),))
    assert restored.state.legs[0].phase is Phase.ORDERED
    assert restored.state.legs[0].reentries_left == 1


def test_reused_outcome_identity_with_changed_facts_is_refused() -> None:
    rules = OptionsSession(setup(leg()), _market())
    rules.advance(at(5))
    observed = _observed("same")
    rules.advance(at(6), (observed,))
    with pytest.raises(ValueError, match="different facts"):
        rules.advance(at(6), (replace(observed, fee_usd=D("1")),))


def test_decreasing_cumulative_fill_or_wrong_attempt_is_refused() -> None:
    rules = OptionsSession(setup(leg(contracts=3)), _market())
    rules.advance(at(5))
    rules.advance(at(6), (_observed("two", quantity=2, terminal=False),))
    with pytest.raises(ValueError, match="cannot decrease"):
        rules.advance(at(7), (_observed("one", minute=7),))
    with pytest.raises(ValueError, match="another leg attempt"):
        rules.advance(at(7), (_observed("wrong", minute=7, entry_number=1),))


def test_stale_qualification_holds_entries_and_reentries_but_still_stops_actual_positions() -> None:
    written = setup(leg(stop=("percent", "25"), reentry=("at_once", 1)))
    rules = OptionsSession(written, _market(close_at_stop=True))
    rules.advance(at(5))
    rules.advance(at(6), (_observed("entry"),))
    stopped = rules.advance(at(7), allow_entries=False)
    assert len(stopped.decisions) == 1 and isinstance(stopped.decisions[0], CloseLeg)

    rules.advance(at(8), (_observed("close", minute=8, kind="close"),), allow_entries=False)
    assert rules.state.legs[0].phase is Phase.EARNED
    assert rules.advance(at(9), allow_entries=False).decisions == ()
    assert isinstance(rules.advance(at(10)).decisions[0], OpenLeg)


def test_stale_qualification_cancels_an_existing_resting_entry() -> None:
    rules = OptionsSession(setup(leg()), _market())
    rules.advance(at(5))
    assert isinstance(rules.advance(at(6), allow_entries=False).decisions[0], CancelEntry)
    assert rules.advance(at(7), allow_entries=False).decisions == ()


def test_daily_new_deployment_after_entry_does_not_catch_up_or_mutate_into_tomorrow() -> None:
    rules = OptionsSession(setup(leg()), _market())
    assert rules.advance(at(6)).decisions == ()
    assert rules.advance(at(7)).decisions == ()
    with pytest.raises(ValueError, match="calendar session"):
        rules.advance(at(391))


def test_exit_time_runs_without_underlying_or_option_prints_and_cancels_unknown_entry() -> None:
    market = TableMarket(underlying={5: "500"}, picks=picked(("call", "500", CALL)), options={})
    rules = OptionsSession(setup(leg()), market)
    rules.advance(at(5))
    assert isinstance(rules.advance(at(60)).decisions[0], CancelEntry)
    assert rules.advance(at(61)).decisions == ()


def test_held_position_is_closed_on_missed_exit_minute_and_stale_qualification() -> None:
    rules = OptionsSession(setup(leg()), _market())
    rules.advance(at(5))
    rules.advance(at(6), (_observed("entry"),))
    close = rules.advance(at(61), allow_entries=False).decisions[0]
    assert isinstance(close, CloseLeg) and close.reason is ExitReason.EXIT_TIME
    assert rules.advance(at(62), allow_entries=False).decisions == ()


def test_stopped_cover_queues_buyback_before_its_sale_using_confirmed_partial_quantities() -> None:
    written = setup(leg("cover", contracts=3, stop=("percent", "25")), leg("sold", side="sell", contracts=3))
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("cover", "500", COVER), ("sold", "500", CALL)),
        options={COVER: {**flat("1.00", 5, 6), 7: ("1.00", "0.70")}, CALL: flat("2.00", 5, 7)},
    )
    rules = OptionsSession(written, market)
    opening = rules.advance(at(5))
    assert [decision.side for decision in opening.decisions] == ["buy", "sell"]
    outcomes = (
        ExecutionOutcome("cover", "cover", 0, "entry", Fill("cover", COVER, "buy", 2, 100, at(6), D(1), D(1)), True),
        ExecutionOutcome("sold", "sold", 0, "entry", Fill("sold", CALL, "sell", 1, 100, at(6), D(2), D(2)), True),
    )
    rules.advance(at(6), outcomes)
    leaving = rules.advance(at(7))
    assert [(decision.leg_id, decision.contracts) for decision in leaving.decisions] == [("sold", 1), ("cover", 2)]
    assert leaving.decisions[0].reason is ExitReason.CLOSED_WITH_ITS_PROTECTION
    assert leaving.decisions[1].reason is ExitReason.STOP
    assert leaving.decisions[0].group == leaving.decisions[1].group


def test_complete_squareoff_stops_held_legs_and_cancels_waiting_momentum() -> None:
    written = setup(
        leg("cover", stop=("percent", "25")),
        leg("call", momentum=("percent_up", "25")),
        square_off="complete",
    )
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("cover", "500", COVER), ("call", "500", CALL)),
        options={COVER: {**flat("1.00", 5, 6), 7: ("1.00", "0.70")}, CALL: flat("2.00", 5, 7)},
    )
    rules = OptionsSession(written, market)
    rules.advance(at(5))
    observed = ExecutionOutcome(
        "cover", "cover", 0, "entry", Fill("cover", COVER, "buy", 1, 100, at(6), D(1), D(1)), True
    )
    rules.advance(at(6), (observed,))
    stopped = rules.advance(at(7))
    assert len(stopped.decisions) == 1 and isinstance(stopped.decisions[0], CloseLeg)
    assert rules.state.legs[1].phase is Phase.DONE
    assert any(event.kind is EventKind.LEG_CANCELLED and event.leg_id == "call" for event in stopped.events)


def test_restore_rejects_changed_setup_and_corrupted_reentry_count() -> None:
    rules = OptionsSession(setup(leg(reentry=("at_once", 1), stop=("percent", "25"))), _market())
    rules.advance(at(5))
    with pytest.raises(ValueError, match="exact setup"):
        OptionsSession(setup(leg()), _market(), state=rules.state)
    corrupted = rules.state.model_dump(mode="json")
    corrupted["legs"][0]["reentries_left"] = 2
    with pytest.raises(ValueError, match="re-entry count"):
        SessionState.model_validate(corrupted)


def test_squareoff_survives_market_retrieval_failure_at_the_exit_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    market = _market()
    rules = OptionsSession(setup(leg()), market)
    rules.advance(at(5))
    rules.advance(at(6), (_observed("entry"),))

    def unavailable(*_args: object) -> None:
        raise RuntimeError("IBKR option trade history is unavailable")

    monkeypatch.setattr(market, "option_bar", unavailable)
    close = rules.advance(at(60), allow_entries=False).decisions[0]
    assert isinstance(close, CloseLeg) and close.reason is ExitReason.EXIT_TIME


def test_complete_squareoff_cancels_pending_entry_and_manages_its_late_fill() -> None:
    written = setup(leg("cover", stop=("percent", "25")), leg("call"), square_off="complete")
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("cover", "500", COVER), ("call", "500", CALL)),
        options={COVER: {**flat("1.00", 5, 6), 7: ("1.00", "0.70")}, CALL: flat("2.00", 5, 8)},
    )
    rules = OptionsSession(written, market)
    rules.advance(at(5))
    cover = ExecutionOutcome(
        "cover",
        "cover",
        0,
        "entry",
        Fill("cover", COVER, "buy", 1, 100, at(6), D(1), D(1)),
        True,
    )
    rules.advance(at(6), (cover,))
    stopped = rules.advance(at(7))
    assert [(type(decision), decision.leg_id) for decision in stopped.decisions] == [
        (CancelEntry, "call"),
        (CloseLeg, "cover"),
    ]

    late = rules.advance(at(8), (_observed("late", minute=8),))
    assert len(late.decisions) == 1
    close = late.decisions[0]
    assert isinstance(close, CloseLeg) and close.leg_id == "call"
    assert close.reason is ExitReason.SQUARE_OFF and close.contracts == 1


@pytest.mark.parametrize("exit_observed_minute", [60, 390])
@pytest.mark.parametrize("pre_intent_snapshot", [False, True])
def test_scheduled_exit_intention_survives_unknown_entry_and_late_fill_on_same_minute(
    exit_observed_minute: int,
    pre_intent_snapshot: bool,
) -> None:
    written, market = setup(leg()), _market()
    rules = OptionsSession(written, market)
    rules.advance(at(5))
    pending = rules.advance(at(exit_observed_minute))
    assert [type(decision) for decision in pending.decisions] == [CancelEntry]

    state = SessionState.model_validate_json(rules.state.model_dump_json())
    if pre_intent_snapshot:
        state.legs[0].close_on_fill = None
    restored = OptionsSession(written, market, state=state)
    late = restored.advance(at(exit_observed_minute), (_observed("late-entry", minute=exit_observed_minute),))
    assert len(late.decisions) == 1
    close = late.decisions[0]
    assert isinstance(close, CloseLeg) and close.reason is ExitReason.EXIT_TIME
    assert close.contracts == 1
    assert close.decided_at_ms == at(exit_observed_minute)
    assert restored.advance(at(exit_observed_minute)).decisions == ()
    events = [event for event in late.events if event.kind is EventKind.EXIT_TIME]
    assert len(events) == 1
    assert not any(event.kind is EventKind.SQUARED_OFF for event in late.events)


def test_full_clerk_reduction_without_rule_close_ends_the_leg_without_reentry() -> None:
    written = setup(leg(contracts=3, stop=("percent", "25"), reentry=("at_once", 1)))
    rules = OptionsSession(written, _market())
    rules.advance(at(5))
    rules.advance(at(6), (_observed("entry", quantity=3),))

    reduced = rules.advance(at(7), (_observed("clerk-expiry-close", minute=7, quantity=3, kind="close"),))
    assert reduced.decisions == ()
    assert rules.state.legs[0].phase is Phase.DONE
    assert rules.state.legs[0].position is None
    assert rules.state.legs[0].reentries_left == 1
    assert rules.advance(at(8)).decisions == ()
    ending = [event for event in reduced.events if event.kind is EventKind.CLOSE_FILLED]
    assert len(ending) == 1 and ending[0].reason == "clerk_reduction"
    assert not any(event.kind is EventKind.RE_ENTRY_EARNED for event in reduced.events)


def test_partial_clerk_reduction_preserves_remaining_stops_and_never_earns_reentry() -> None:
    written, market = (
        setup(leg(contracts=3, stop=("percent", "25"), reentry=("at_once", 1))),
        _market(close_at_stop=True),
    )
    rules = OptionsSession(written, market)
    rules.advance(at(5))
    rules.advance(at(6), (_observed("entry", quantity=3),))
    partial = _observed("clerk-partial", minute=6, quantity=1, kind="close")
    reduced = rules.advance(at(6), (partial,))
    assert reduced.decisions == ()
    assert rules.state.legs[0].position.contracts_held == 2
    assert rules.state.legs[0].position.close is None

    restored = OptionsSession(written, market, state=SessionState.model_validate_json(rules.state.model_dump_json()))
    stopped = restored.advance(at(7))
    assert len(stopped.decisions) == 1
    assert isinstance(stopped.decisions[0], CloseLeg) and stopped.decisions[0].contracts == 2
    assert stopped.decisions[0].reason is ExitReason.STOP
    assert not any(event.kind is EventKind.RE_ENTRY_EARNED for event in stopped.events)
    restored.advance(at(8), (_observed("clerk-and-rule-close", minute=8, quantity=3, kind="close"),))
    assert restored.state.legs[0].phase is Phase.DONE
    assert restored.state.legs[0].reentries_left == 1


def test_full_clerk_reduction_waits_for_pending_entry_cancellation_proof() -> None:
    rules = OptionsSession(setup(leg(contracts=3, stop=("percent", "25"), reentry=("at_once", 1))), _market())
    rules.advance(at(5))
    rules.advance(at(6), (_observed("partial-entry", quantity=1, terminal=False),))
    rules.advance(at(6), (_observed("clerk-close", minute=6, quantity=1, kind="close"),))
    assert rules.state.legs[0].phase is Phase.HELD
    assert rules.state.legs[0].position.contracts_held == 0
    terminal = ExecutionOutcome("cancel-proven", "call", 0, "entry", terminal=True)
    settled = rules.advance(at(6), (terminal,))
    assert settled.decisions == ()
    assert rules.state.legs[0].phase is Phase.DONE
