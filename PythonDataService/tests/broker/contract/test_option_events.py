"""The typed option-event activities: what they refuse to be, and how one is matched to lots."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.broker.contract.option_events import (
    BrokerOptionActivityUnreadable,
    BrokerOptionEventActivity,
    BrokerOptionEventEvidence,
    BrokerOptionEventResume,
    BrokerOptionShareTradeActivity,
    OptionActivityStatus,
    allocation_of,
    in_allocation_order,
    share_trade_disagreements,
)
from app.options.contract import OptionContract
from app.options.event_allocation import (
    HeldOptionLot,
    OptionEventKind,
    OptionEventRefusal,
    OptionEventRefused,
    allocate_option_event,
    expected_share_trade,
)
from app.options.expiry import option_expiry_anchor_ms
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_midnight_ms

_OBSERVED = 1_700_000_000_000
_CALL = OptionContract.from_occ_symbol("QS240301C00004500")
_ASSIGNED_ON = date(2024, 3, 1)


def _event(**changes: Any) -> BrokerOptionEventActivity:
    fields: dict[str, Any] = {
        "broker": "alpaca",
        "activity_id": "20240301000000000::001140db-3947-456b-aefc-253861fb65df",
        "activity_type": "OPASN",
        "kind": OptionEventKind.ASSIGNED,
        "status": OptionActivityStatus.EXECUTED,
        "activity_date_ms": et_midnight_ms(_ASSIGNED_ON),
        "contract": _CALL,
        "position_change": 3,
        "net_amount": Decimal("0"),
        "group_id": None,
        "created_at_ms": None,
        "observed_at_ms": _OBSERVED,
    }
    return BrokerOptionEventActivity(**{**fields, **changes})


def _share_trade(**changes: Any) -> BrokerOptionShareTradeActivity:
    fields: dict[str, Any] = {
        "broker": "alpaca",
        "activity_id": "20240301000000000::a88c089f-a8c3-4672-9b68-2f6f2d05e914",
        "activity_type": "OPTRD",
        "status": OptionActivityStatus.EXECUTED,
        "activity_date_ms": et_midnight_ms(_ASSIGNED_ON),
        "underlying_symbol": "QS",
        "shares": -300,
        "price": Decimal("4.5"),
        "net_amount": Decimal("1350"),
        "group_id": None,
        "created_at_ms": None,
        "observed_at_ms": _OBSERVED,
    }
    return BrokerOptionShareTradeActivity(**{**fields, **changes})


def _sold(owner: str, contracts: int, *, opened: date, sequence: int, contract: OptionContract = _CALL) -> HeldOptionLot:
    return HeldOptionLot(
        owner=owner, contract=contract, quantity=-contracts, opened_at_ms=et_midnight_ms(opened) + 36_000_000, sequence=sequence
    )


def _bought(owner: str, contracts: int, *, opened: date, sequence: int, contract: OptionContract = _CALL) -> HeldOptionLot:
    return replace(_sold(owner, contracts, opened=opened, sequence=sequence, contract=contract), quantity=contracts)


# ── What an activity cannot be ──────────────────────────────────────────────


def _handed_out(activity: BrokerOptionEventActivity | BrokerOptionShareTradeActivity) -> dict[str, Any]:
    """Every value an activity gives a caller: its fields, and what its class works out from them."""
    worked_out = [name for name, member in vars(type(activity)).items() if isinstance(member, property)]
    return {name: getattr(activity, name) for name in (*type(activity).model_fields, *worked_out)}


def test_an_activitys_date_travels_as_milliseconds_and_in_no_second_form() -> None:
    """The date is ``activity_date_ms``. A calendar date beside it would be a second form of one time."""
    for activity in (_event(), _share_trade()):
        handed_out = _handed_out(activity)

        assert handed_out["activity_date_ms"] == et_midnight_ms(_ASSIGNED_ON)
        assert [name for name, value in handed_out.items() if isinstance(value, date)] == []


@pytest.mark.parametrize(
    "instant",
    [
        # An expiry's 16:00 anchor: a time of day an activity does not have.
        option_expiry_anchor_ms(_ASSIGNED_ON),
        # Midnight UTC, which is the evening before in New York.
        1_709_251_200_000,
        et_midnight_ms(_ASSIGNED_ON) + 1,
        0,
    ],
)
def test_an_activity_date_is_new_york_midnight_and_nothing_else(instant: int) -> None:
    with pytest.raises(ValidationError):
        _event(activity_date_ms=instant)
    with pytest.raises(ValidationError):
        _share_trade(activity_date_ms=instant)


def test_an_event_changes_a_position_by_at_least_one_contract() -> None:
    with pytest.raises(ValidationError, match="never none"):
        _event(position_change=0)


def test_an_exercise_cannot_add_to_a_position_and_an_assignment_cannot_take_from_a_bought_one() -> None:
    with pytest.raises(ValidationError, match="an exercise takes from a bought position"):
        _event(kind=OptionEventKind.EXERCISED, activity_type="OPEXC", position_change=1)
    with pytest.raises(ValidationError, match="an assignment takes from a sold position"):
        _event(position_change=-1)


@pytest.mark.parametrize("position_change", [-2, 2])
def test_an_expiry_may_end_a_bought_position_or_a_sold_one(position_change: int) -> None:
    event = _event(kind=OptionEventKind.EXPIRED, activity_type="OPEXP", position_change=position_change)

    assert event.contracts == 2


def test_an_option_cannot_expire_before_its_own_expiry_date() -> None:
    """The contract expires on Friday 2024-03-01. An expiry dated the Thursday is about something else."""
    with pytest.raises(ValidationError, match="this expiry is dated 2024-02-29"):
        _event(
            kind=OptionEventKind.EXPIRED,
            activity_type="OPEXP",
            position_change=-1,
            activity_date_ms=et_midnight_ms(date(2024, 2, 29)),
        )


@pytest.mark.parametrize("day", [date(2024, 3, 1), date(2024, 3, 2), date(2024, 3, 4)], ids=["the day", "the Saturday", "the Monday"])
def test_an_expiry_is_dated_on_its_contracts_expiry_date_or_after(day: date) -> None:
    event = _event(
        kind=OptionEventKind.EXPIRED, activity_type="OPEXP", position_change=-1, activity_date_ms=et_midnight_ms(day)
    )

    assert event.activity_date_ms == et_midnight_ms(day)


def test_an_assignment_or_an_exercise_may_come_before_the_expiry_date() -> None:
    """An American option can be exercised, and so assigned, on any day it is open."""
    early = et_midnight_ms(date(2024, 2, 20))

    assert _event(activity_date_ms=early).activity_date_ms == early
    assert _event(
        kind=OptionEventKind.EXERCISED, activity_type="OPEXC", position_change=-1, activity_date_ms=early
    ).activity_date_ms == early


@pytest.mark.parametrize("net_amount", [Decimal("-450"), Decimal("0.01")])
def test_an_event_that_states_cash_of_its_own_is_not_one_the_books_can_follow(net_amount: Decimal) -> None:
    with pytest.raises(ValidationError, match="moves no cash of its own"):
        _event(net_amount=net_amount)


@pytest.mark.parametrize("net_amount", [None, Decimal("0"), Decimal("0.00")])
def test_an_event_states_no_cash_or_none_at_all(net_amount: Decimal | None) -> None:
    assert _event(net_amount=net_amount).net_amount == net_amount


@pytest.mark.parametrize("changes", [{"shares": 0}, {"price": Decimal("0")}, {"price": Decimal("-4.5")}])
def test_a_share_trade_is_for_some_shares_at_a_price_above_zero(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _share_trade(**changes)


# ── Nothing is read as a number, a price or a kind that is not one ──────────


@pytest.mark.parametrize("position_change", [True, 1.0, "1", Decimal("1")], ids=["a boolean", "a float", "text", "a decimal"])
def test_a_count_of_contracts_is_a_whole_number_and_nothing_else_is_read_as_one(position_change: Any) -> None:
    with pytest.raises(ValidationError):
        _event(position_change=position_change)


@pytest.mark.parametrize("shares", [True, -300.0, "-300", Decimal("-300")], ids=["a boolean", "a float", "text", "a decimal"])
def test_a_count_of_shares_is_a_whole_number_and_nothing_else_is_read_as_one(shares: Any) -> None:
    with pytest.raises(ValidationError):
        _share_trade(shares=shares)


@pytest.mark.parametrize(
    "changes",
    [{"price": 4.5}, {"price": "4.5"}, {"price": 4}, {"net_amount": 1350}, {"net_amount": 1350.0}, {"net_amount": "1350"}],
    ids=["a float price", "a price in text", "a whole-number price", "whole-number cash", "float cash", "cash in text"],
)
def test_a_share_trades_money_is_an_exact_decimal_or_it_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _share_trade(**changes)


@pytest.mark.parametrize("net_amount", [0, 0.0, "0", False], ids=["a whole number", "a float", "text", "a boolean"])
def test_an_events_cash_is_an_exact_decimal_or_it_is_refused(net_amount: Any) -> None:
    with pytest.raises(ValidationError):
        _event(net_amount=net_amount)


@pytest.mark.parametrize(
    "changes",
    [{"kind": "assigned"}, {"status": "executed"}, {"contract": "QS240301C00004500"}, {"activity_id": 7}, {"group_id": 7}],
    ids=["a kind in text", "a status in text", "a contract as its symbol", "an id that is a number", "a group id that is a number"],
)
def test_a_kind_a_status_and_a_contract_are_handed_over_as_what_they_are(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _event(**changes)
    if "kind" not in changes and "contract" not in changes:
        with pytest.raises(ValidationError):
            _share_trade(**changes)


# ── Every time is inside the range the domain admits ────────────────────────

_TIMES_OUT_OF_RANGE = [-1, MAX_TIMESTAMP_MS + 1, 2**63 - 1, True, 1_700_000_000_000.0, "1700000000000"]
_TIMES_OUT_OF_RANGE_IDS = ["before 1970", "past year 9999", "the int64 limit", "a boolean", "a float", "text"]


@pytest.mark.parametrize("field", ["observed_at_ms", "created_at_ms"])
@pytest.mark.parametrize("instant", _TIMES_OUT_OF_RANGE, ids=_TIMES_OUT_OF_RANGE_IDS)
def test_a_time_on_an_activity_is_whole_milliseconds_inside_the_domains_range(field: str, instant: Any) -> None:
    with pytest.raises(ValidationError):
        _event(**{field: instant})
    with pytest.raises(ValidationError):
        _share_trade(**{field: instant})


@pytest.mark.parametrize("instant", [0, MAX_TIMESTAMP_MS], ids=["the epoch", "the last admitted millisecond"])
def test_the_ends_of_the_domains_range_are_inside_it(instant: int) -> None:
    for activity in (
        _event(observed_at_ms=instant, created_at_ms=instant),
        _share_trade(observed_at_ms=instant, created_at_ms=instant),
    ):
        assert (activity.observed_at_ms, activity.created_at_ms) == (instant, instant)


def test_an_activity_date_past_the_domains_range_is_refused() -> None:
    with pytest.raises(ValidationError):
        _event(activity_date_ms=MAX_TIMESTAMP_MS + 1)
    with pytest.raises(ValidationError):
        _unreadable(activity_date_ms=MAX_TIMESTAMP_MS + 1)


# ── Whether a read explains the window ──────────────────────────────────────


def _unreadable(**changes: Any) -> BrokerOptionActivityUnreadable:
    fields: dict[str, Any] = {
        "broker": "alpaca",
        "activity_id": "20240301000000000::4bd8c683-0b2c-489f-9df9-1243ff621648",
        "activity_type": "OPEXP",
        "activity_date_ms": et_midnight_ms(_ASSIGNED_ON),
        "symbol": "QS240301P00000500",
        "reason": "Alpaca field 'qty' must be a whole number other than zero; got '-0.5'",
    }
    return BrokerOptionActivityUnreadable(**{**fields, **changes})


def test_a_complete_walk_with_every_row_read_is_a_whole_read() -> None:
    evidence = BrokerOptionEventEvidence(events=(_event(),), share_trades=(_share_trade(),), history_complete=True)

    assert evidence.read_whole is True


def _resume(**changes: Any) -> BrokerOptionEventResume:
    fields: dict[str, Any] = {
        "page_token": "20240301000000000::fee-0299",
        "last_page_oldest_ms": et_midnight_ms(_ASSIGNED_ON),
    }
    return BrokerOptionEventResume(**{**fields, **changes})


def test_a_walk_that_did_not_reach_the_windows_start_is_not_a_whole_read() -> None:
    evidence = BrokerOptionEventEvidence(events=(_event(),), share_trades=(), history_complete=False, resume=_resume())

    assert evidence.read_whole is False


def test_a_resume_cannot_be_made_from_the_cursor_alone() -> None:
    """A cursor without the time the walk had reached would start the order check again from nothing."""
    with pytest.raises(ValidationError, match="last_page_oldest_ms"):
        BrokerOptionEventResume(page_token="20240301000000000::fee-0299")
    with pytest.raises(ValidationError, match="page_token"):
        BrokerOptionEventResume(last_page_oldest_ms=et_midnight_ms(_ASSIGNED_ON))


def test_a_resume_says_so_when_the_last_page_proved_no_order() -> None:
    assert _resume(last_page_oldest_ms=None).last_page_oldest_ms is None


@pytest.mark.parametrize(
    "changes",
    [{"page_token": ""}, {"page_token": 299}, *({"last_page_oldest_ms": instant} for instant in _TIMES_OUT_OF_RANGE)],
    ids=["a blank cursor", "a cursor that is a number", *_TIMES_OUT_OF_RANGE_IDS],
)
def test_a_resume_holds_a_cursor_and_a_time_inside_the_domains_range(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _resume(**changes)


def test_a_resume_survives_being_written_down_and_read_back() -> None:
    """A poller keeps it between calls. What it reads back is what the walk handed out."""
    for resume in (_resume(), _resume(last_page_oldest_ms=None)):
        assert BrokerOptionEventResume.model_validate_json(resume.model_dump_json()) == resume


def test_one_unreadable_row_means_the_read_is_not_whole_however_complete_the_walk() -> None:
    evidence = BrokerOptionEventEvidence(
        events=(_event(),), share_trades=(), unreadable=(_unreadable(),), history_complete=True
    )

    assert evidence.read_whole is False


@pytest.mark.parametrize("status", [OptionActivityStatus.CORRECT, OptionActivityStatus.CANCELED])
def test_an_event_the_broker_does_not_report_as_executed_means_the_read_is_not_whole(
    status: OptionActivityStatus,
) -> None:
    """What a corrected or cancelled row does to a position is not known, so the read explains less than it shows."""
    stood_by = _event(activity_id="20240301000000000::executed")
    not_stood_by = _event(status=status)
    evidence = BrokerOptionEventEvidence(events=(stood_by, not_stood_by), share_trades=(), history_complete=True)

    assert evidence.read_whole is False
    # The row is still listed, so a hold can name it.
    assert evidence.not_executed == (not_stood_by,)
    assert not_stood_by in evidence.events


@pytest.mark.parametrize("status", [OptionActivityStatus.CORRECT, OptionActivityStatus.CANCELED])
def test_a_share_trade_the_broker_does_not_report_as_executed_means_the_read_is_not_whole(
    status: OptionActivityStatus,
) -> None:
    not_stood_by = _share_trade(status=status)
    evidence = BrokerOptionEventEvidence(events=(_event(),), share_trades=(not_stood_by,), history_complete=True)

    assert evidence.read_whole is False
    assert evidence.not_executed == (not_stood_by,)


# ── A broker's activity matched to lots ─────────────────────────────────────


def test_an_executed_assignment_held_by_two_bots_is_allocated_oldest_first_with_shares_and_cash() -> None:
    lots = [
        _sold("bot-b", 2, opened=date(2024, 2, 28), sequence=8),
        _sold("bot-a", 2, opened=date(2024, 2, 26), sequence=5),
    ]

    allocation = allocation_of(_event(), multiplier=100, lots=lots)

    assert [(lot.owner, lot.contracts, lot.remaining_quantity) for lot in allocation.lots] == [
        ("bot-a", 2, 0),
        ("bot-b", 1, -1),
    ]
    # Each bot delivers its shares at $4.50 and is paid for them.
    assert [(lot.share_quantity_change, lot.cash_change_cents) for lot in allocation.lots] == [
        (-200, 90_000),
        (-100, 45_000),
    ]
    assert allocation == allocate_option_event(
        kind=OptionEventKind.ASSIGNED,
        contract=_CALL,
        position_change=3,
        event_date_ms=et_midnight_ms(_ASSIGNED_ON),
        multiplier=100,
        lots=lots,
    )


def test_an_exercise_the_broker_reports_takes_bought_lots_because_its_sign_reaches_the_rule() -> None:
    exercise = _event(kind=OptionEventKind.EXERCISED, activity_type="OPEXC", position_change=-2)
    lots = [
        _bought("bot-a", 1, opened=date(2024, 2, 26), sequence=5),
        _bought("bot-b", 4, opened=date(2024, 2, 28), sequence=8),
    ]

    allocation = allocation_of(exercise, multiplier=100, lots=lots)

    assert [(lot.owner, lot.contracts, lot.option_quantity_change) for lot in allocation.lots] == [
        ("bot-a", 1, -1),
        ("bot-b", 1, -1),
    ]
    # Each bot buys its shares at $4.50.
    assert [(lot.share_quantity_change, lot.cash_change_cents) for lot in allocation.lots] == [(100, -45_000), (100, -45_000)]


@pytest.mark.parametrize("position_change", [-1, 1], ids=["the net, as Alpaca would sign it", "signed the other way"])
def test_an_expiry_of_a_contract_two_bots_hold_on_opposite_sides_is_matched_to_neither(position_change: int) -> None:
    """Bot A sold 1 and bot B bought 2: the account is net long 1, and that net is all the row reports.

    Taking one contract from either bot would leave the books holding an
    expired contract the broker does not. The event is refused by name.
    """
    lots = [
        _sold("bot-a", 1, opened=date(2024, 2, 26), sequence=5),
        _bought("bot-b", 2, opened=date(2024, 2, 27), sequence=8),
    ]
    expiry = _event(kind=OptionEventKind.EXPIRED, activity_type="OPEXP", position_change=position_change)

    with pytest.raises(OptionEventRefused) as refused:
        allocation_of(expiry, multiplier=100, lots=lots)

    assert refused.value.reason is OptionEventRefusal.HELD_ON_BOTH_SIDES
    assert refused.value.unmatched_contracts == 1


@pytest.mark.parametrize("status", [OptionActivityStatus.CORRECT, OptionActivityStatus.CANCELED])
def test_an_activity_the_broker_does_not_report_as_executed_is_allocated_to_nothing(
    status: OptionActivityStatus,
) -> None:
    lots = [_sold("bot-a", 5, opened=date(2024, 2, 26), sequence=5)]

    with pytest.raises(OptionEventRefused, match=f"as {status.value}, not executed") as refused:
        allocation_of(_event(status=status), multiplier=100, lots=lots)

    assert refused.value.reason is OptionEventRefusal.NOT_EXECUTED
    assert refused.value.unmatched_contracts == 3


def test_the_activitys_date_decides_which_lots_were_open() -> None:
    opened_the_monday_after = _sold("bot-a", 3, opened=date(2024, 3, 4), sequence=9)

    with pytest.raises(OptionEventRefused) as refused:
        allocation_of(_event(), multiplier=100, lots=[opened_the_monday_after])

    assert refused.value.reason is OptionEventRefusal.MORE_CONTRACTS_THAN_HELD


def test_a_refusal_from_the_rule_reaches_the_caller_by_name() -> None:
    with pytest.raises(OptionEventRefused) as refused:
        allocation_of(_event(), multiplier=100, lots=[])

    assert refused.value.reason is OptionEventRefusal.CONTRACT_NOT_HELD


# ── The broker's share trade against the contract's terms ───────────────────


def _expected() -> Any:
    trade = expected_share_trade(OptionEventKind.ASSIGNED, _CALL, contracts=3, multiplier=100)
    assert trade is not None
    return trade


def test_a_share_trade_that_matches_the_contracts_terms_has_no_disagreement() -> None:
    assert share_trade_disagreements(_expected(), _share_trade()) == ()
    # The broker may write the same cash with or without its cents.
    assert share_trade_disagreements(_expected(), _share_trade(net_amount=Decimal("1350.00"))) == ()


@pytest.mark.parametrize(
    ("changes", "difference"),
    [
        ({"underlying_symbol": "QSI"}, "the shares are in QSI, and the contract's root is QS"),
        ({"shares": -200}, "-200 shares were reported, and the contract moves -300"),
        ({"shares": 300}, "300 shares were reported, and the contract moves -300"),
        ({"price": Decimal("4.55")}, "the reported price is 4.55, and the strike is 4.5"),
        ({"net_amount": Decimal("1349.99")}, "the reported cash is 1349.99, and the shares at the strike come to 1350.00"),
        ({"net_amount": Decimal("-1350")}, "the reported cash is -1350, and the shares at the strike come to 1350.00"),
    ],
)
def test_each_way_a_share_trade_can_differ_is_named(changes: dict[str, Any], difference: str) -> None:
    assert share_trade_disagreements(_expected(), _share_trade(**changes)) == (difference,)


def test_cash_that_differs_far_below_a_cent_is_still_a_difference() -> None:
    a_hair_over = Decimal("1350." + "0" * 33 + "1")

    assert len(share_trade_disagreements(_expected(), _share_trade(net_amount=a_hair_over))) == 1


@pytest.mark.parametrize("status", [OptionActivityStatus.CORRECT, OptionActivityStatus.CANCELED])
def test_a_share_trade_the_broker_does_not_report_as_executed_agrees_with_nothing(
    status: OptionActivityStatus,
) -> None:
    assert share_trade_disagreements(_expected(), _share_trade(status=status)) == (
        f"the broker reports the share trade as {status.value}, not executed",
    )


def test_several_differences_are_all_named() -> None:
    differences = share_trade_disagreements(_expected(), _share_trade(shares=-200, net_amount=Decimal("900")))

    assert len(differences) == 2


# ── The order events are matched in ─────────────────────────────────────────


def _on(day: date, kind: OptionEventKind, activity_id: str) -> BrokerOptionEventActivity:
    code = {"assigned": "OPASN", "exercised": "OPEXC", "expired": "OPEXP"}[kind.value]
    return _event(
        kind=kind,
        activity_type=code,
        activity_id=activity_id,
        activity_date_ms=et_midnight_ms(day),
        position_change=1 if kind is OptionEventKind.ASSIGNED else -1,
    )


def test_events_are_matched_oldest_date_first_though_they_are_read_newest_first() -> None:
    friday = _on(date(2024, 3, 1), OptionEventKind.EXPIRED, "c")
    tuesday = _on(date(2024, 2, 27), OptionEventKind.EXERCISED, "b")
    monday = _on(date(2024, 2, 26), OptionEventKind.ASSIGNED, "a")

    assert in_allocation_order([friday, tuesday, monday]) == (monday, tuesday, friday)


def test_on_one_date_an_expiry_is_matched_after_the_assignments_and_exercises() -> None:
    """What expires is what is left: the oldest sold lot is the one assigned, not the one that lapsed."""
    expiry = _on(date(2024, 3, 1), OptionEventKind.EXPIRED, "a-first-by-id")
    assignment = _on(date(2024, 3, 1), OptionEventKind.ASSIGNED, "z-last-by-id")
    exercise = _on(date(2024, 3, 1), OptionEventKind.EXERCISED, "m")

    assert in_allocation_order([expiry, assignment, exercise]) == (exercise, assignment, expiry)


def test_the_order_does_not_depend_on_how_the_rows_arrived() -> None:
    events = [_on(date(2024, 3, 1), OptionEventKind.ASSIGNED, activity_id) for activity_id in ("b", "c", "a")]

    assert [event.activity_id for event in in_allocation_order(events)] == ["a", "b", "c"]
    assert in_allocation_order(reversed(events)) == in_allocation_order(events)


# ── Which bot is assigned, whichever row is matched first ───────────────────

_AAPL_CALL = OptionContract.from_occ_symbol("AAPL230721C00150000")
_EXPIRY_FRIDAY = date(2023, 7, 21)
_THE_SATURDAY = date(2023, 7, 22)


def _booked(lots: list[HeldOptionLot], allocation: Any) -> list[HeldOptionLot]:
    """The open lots once an allocation is booked: what the books hand to the next event."""
    left = {(taken.owner, taken.sequence): taken.remaining_quantity for taken in allocation.lots}
    after = []
    for lot in lots:
        quantity = left.get((lot.owner, lot.sequence), lot.quantity)
        if quantity:
            after.append(replace(lot, quantity=quantity))
    return after


@pytest.mark.parametrize("expiry_first", [True, False], ids=["the expiry first", "the assignment first"])
def test_the_bot_that_sold_first_is_assigned_whether_the_expiry_or_the_assignment_is_matched_first(
    expiry_first: bool,
) -> None:
    """Bot A sold 2, then bot B sold 3. On expiry night 2 are assigned and 3 expire.

    Say the expiry is dated the Friday and the assignment the Saturday, or
    the assignment simply arrives a poll later: the expiry is matched first.
    A's lots, the oldest, are the ones assigned all the same. (No published
    example dates an assignment after its expiry; the rule must not need
    them to come in order.)
    """
    lots = [
        _sold("bot-a", 2, opened=date(2023, 7, 10), sequence=5, contract=_AAPL_CALL),
        _sold("bot-b", 3, opened=date(2023, 7, 12), sequence=8, contract=_AAPL_CALL),
    ]
    expiry = _event(
        kind=OptionEventKind.EXPIRED,
        activity_type="OPEXP",
        activity_id="20230721000000000::expiry",
        contract=_AAPL_CALL,
        position_change=3,
        activity_date_ms=et_midnight_ms(_EXPIRY_FRIDAY),
    )
    assignment = _event(
        activity_id="20230722000000000::assignment",
        contract=_AAPL_CALL,
        position_change=2,
        activity_date_ms=et_midnight_ms(_THE_SATURDAY),
    )
    # By date the expiry comes first: the order alone would hand it the oldest lots.
    assert in_allocation_order([assignment, expiry]) == (expiry, assignment)

    taken: dict[OptionEventKind, list[tuple[str, int, int]]] = {}
    for event in (expiry, assignment) if expiry_first else (assignment, expiry):
        allocation = allocation_of(event, multiplier=100, lots=lots)
        taken[event.kind] = [(lot.owner, lot.contracts, lot.share_quantity_change) for lot in allocation.lots]
        lots = _booked(lots, allocation)

    # Bot A delivers 200 shares; bot B's three lapse and move none.
    assert taken[OptionEventKind.ASSIGNED] == [("bot-a", 2, -200)]
    assert taken[OptionEventKind.EXPIRED] == [("bot-b", 3, 0)]
    assert lots == []
