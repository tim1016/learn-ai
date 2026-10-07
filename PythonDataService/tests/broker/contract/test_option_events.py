"""The typed option-event activities: what they refuse to be, and how one is matched to lots."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.broker.contract.option_events import (
    BrokerOptionActivityUnreadable,
    BrokerOptionEventActivity,
    BrokerOptionEventEvidence,
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
from app.utils.session_anchors import et_midnight_ms

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


def _sold(owner: str, contracts: int, *, opened: date, sequence: int) -> HeldOptionLot:
    return HeldOptionLot(
        owner=owner, contract=_CALL, quantity=-contracts, opened_at_ms=et_midnight_ms(opened) + 36_000_000, sequence=sequence
    )


# ── What an activity cannot be ──────────────────────────────────────────────


def test_an_activity_carries_its_date_and_its_whole_contracts() -> None:
    event = _event()

    assert event.activity_date == _ASSIGNED_ON
    assert event.contracts == 3


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


def test_an_activity_has_no_field_the_contract_does_not_name() -> None:
    with pytest.raises(ValidationError):
        _event(description="Option Assignment")


# ── Whether a read explains the window ──────────────────────────────────────


def _unreadable() -> BrokerOptionActivityUnreadable:
    return BrokerOptionActivityUnreadable(
        broker="alpaca",
        activity_id="20240301000000000::4bd8c683-0b2c-489f-9df9-1243ff621648",
        activity_type="OPEXP",
        activity_date_ms=et_midnight_ms(_ASSIGNED_ON),
        symbol="QS240301P00000500",
        reason="Alpaca field 'qty' must be a whole number other than zero; got '-0.5'",
    )


def test_a_complete_walk_with_every_row_read_is_a_whole_read() -> None:
    evidence = BrokerOptionEventEvidence(events=(_event(),), share_trades=(_share_trade(),), history_complete=True)

    assert evidence.read_whole is True


def test_a_walk_that_did_not_reach_the_windows_start_is_not_a_whole_read() -> None:
    evidence = BrokerOptionEventEvidence(
        events=(_event(),), share_trades=(), history_complete=False, next_page_token="20240301000000000::fee-0299"
    )

    assert evidence.read_whole is False


def test_one_unreadable_row_means_the_read_is_not_whole_however_complete_the_walk() -> None:
    evidence = BrokerOptionEventEvidence(
        events=(_event(),), share_trades=(), unreadable=(_unreadable(),), history_complete=True
    )

    assert evidence.read_whole is False


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
        contracts=3,
        event_date_ms=et_midnight_ms(_ASSIGNED_ON),
        multiplier=100,
        lots=lots,
    )


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


def test_an_assignment_and_an_expiry_on_one_date_take_the_lots_the_order_gives_them() -> None:
    """Bot A sold first. One of three is assigned and two expire: A is the one assigned."""
    lots = [
        _sold("bot-a", 1, opened=date(2024, 2, 26), sequence=5),
        _sold("bot-b", 2, opened=date(2024, 2, 28), sequence=8),
    ]
    expiry = _event(
        kind=OptionEventKind.EXPIRED, activity_type="OPEXP", activity_id="a-first-by-id", position_change=2
    )
    assignment = _event(activity_id="z-last-by-id", position_change=1)

    first, second = in_allocation_order([expiry, assignment])
    assigned = allocation_of(first, multiplier=100, lots=lots)
    # The books then hold only what the assignment left.
    expired = allocation_of(second, multiplier=100, lots=lots[1:])

    assert first is assignment
    assert [(lot.owner, lot.contracts, lot.share_quantity_change) for lot in assigned.lots] == [("bot-a", 1, -100)]
    assert [(lot.owner, lot.contracts, lot.share_quantity_change) for lot in expired.lots] == [("bot-b", 2, 0)]
