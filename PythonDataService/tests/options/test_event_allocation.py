"""Which lots an assignment, an exercise or an expiry takes, and what each owner's books then show.

Every figure is a whole number of contracts, shares or cents, so every
comparison is exact. The shares and cash are held to the pairs of activities
Alpaca publishes (``tests/fixtures/alpaca_documented/option_activities``):
documentation, not recordings.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import permutations
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.options.contract import OptionContract
from app.options.event_allocation import (
    EVENT_CLOSE_PRICE,
    HeldOptionLot,
    LotAllocation,
    OptionEventKind,
    OptionEventRefusal,
    OptionEventRefused,
    ShareTrade,
    allocate_option_event,
    expected_share_trade,
)
from tests.broker.alpaca.documented_examples import documented

_NY = ZoneInfo("America/New_York")
_CALL = OptionContract.from_occ_symbol("AAPL230721C00150000")
_PUT = OptionContract.from_occ_symbol("AAPL230721P00150000")
_EXPIRY_DAY = date(2023, 7, 21)


def _at(day: date, hour: int = 10, minute: int = 0, *, ms: int = 0) -> int:
    """An instant on a New York date, as ``int64`` ms UTC."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=_NY)
    return int(local.astimezone(UTC).timestamp() * 1000) + ms


def _lot(
    owner: str,
    quantity: int,
    *,
    opened: int | None = None,
    sequence: int = 0,
    contract: OptionContract = _CALL,
) -> HeldOptionLot:
    return HeldOptionLot(
        owner=owner,
        contract=contract,
        quantity=quantity,
        opened_at_ms=_at(date(2023, 7, 10)) if opened is None else opened,
        sequence=sequence,
    )


def _allocate(
    kind: OptionEventKind,
    change: int,
    lots: list[HeldOptionLot],
    *,
    contract: OptionContract = _CALL,
    on: date = _EXPIRY_DAY,
    multiplier: int = 100,
) -> Any:
    """Match an event whose row changed the account's position by ``change``: negative ends a bought position."""
    return allocate_option_event(
        kind=kind,
        contract=contract,
        position_change=change,
        event_date_ms=_at(on, 0),
        multiplier=multiplier,
        lots=lots,
    )


def _change(kind: OptionEventKind, contracts: int, *, sold: bool = False) -> int:
    """``contracts`` of an event as a broker signs the change; ``sold`` says which side an expiry ended."""
    if kind is OptionEventKind.ASSIGNED or (kind is OptionEventKind.EXPIRED and sold):
        return contracts
    return -contracts


def _taken(allocation: Any) -> list[tuple[str, int, int]]:
    """(owner, sequence, contracts) for each lot taken, in the order taken."""
    return [(lot.owner, lot.sequence, lot.contracts) for lot in allocation.lots]


# ── An assignment ───────────────────────────────────────────────────────────


def test_an_assigned_call_takes_the_whole_sold_lot_and_sells_the_shares_at_the_strike() -> None:
    allocation = _allocate(OptionEventKind.ASSIGNED, 2, [_lot("bot-a", -2)])

    (lot,) = allocation.lots
    assert lot == LotAllocation(
        owner="bot-a",
        sequence=0,
        opened_at_ms=_at(date(2023, 7, 10)),
        contracts=2,
        remaining_quantity=0,
        # The sold position is bought back by the assignment, at a price of zero.
        option_quantity_change=2,
        # 200 shares delivered at $150: the bot is short them if it held none.
        share_quantity_change=-200,
        cash_change_cents=3_000_000,
    )
    assert allocation.share_price == Decimal("150")
    assert Decimal(0) == EVENT_CLOSE_PRICE


def test_an_assigned_put_buys_the_shares_and_pays_the_strike() -> None:
    allocation = _allocate(OptionEventKind.ASSIGNED, 1, [_lot("bot-a", -1, contract=_PUT)], contract=_PUT)

    (lot,) = allocation.lots
    assert (lot.option_quantity_change, lot.share_quantity_change, lot.cash_change_cents) == (1, 100, -1_500_000)


def test_one_contract_held_by_two_bots_and_assigned_in_part_takes_the_oldest_lot_first() -> None:
    older = _lot("bot-a", -2, opened=_at(date(2023, 7, 10)), sequence=11)
    newer = _lot("bot-b", -3, opened=_at(date(2023, 7, 12)), sequence=14)

    # Offered newest first: the order offered must not matter.
    allocation = _allocate(OptionEventKind.ASSIGNED, 3, [newer, older])

    assert _taken(allocation) == [("bot-a", 11, 2), ("bot-b", 14, 1)]
    whole, part = allocation.lots
    assert whole.remaining_quantity == 0
    # Bot B is still short two of the three it sold.
    assert part.remaining_quantity == -2
    assert (whole.share_quantity_change, whole.cash_change_cents) == (-200, 3_000_000)
    assert (part.share_quantity_change, part.cash_change_cents) == (-100, 1_500_000)
    assert (allocation.share_quantity_change, allocation.cash_change_cents) == (-300, 4_500_000)


def test_an_assignment_smaller_than_the_oldest_lot_leaves_the_newer_lot_alone() -> None:
    allocation = _allocate(
        OptionEventKind.ASSIGNED,
        1,
        [_lot("bot-a", -2, sequence=1), _lot("bot-b", -3, opened=_at(date(2023, 7, 12)), sequence=2)],
    )

    assert _taken(allocation) == [("bot-a", 1, 1)]
    assert allocation.lots[0].remaining_quantity == -1


def test_an_assignment_passes_over_a_bought_lot_however_old() -> None:
    bought_first = _lot("bot-a", 5, opened=_at(date(2023, 7, 3)), sequence=1)
    sold_later = _lot("bot-b", -1, opened=_at(date(2023, 7, 12)), sequence=2)

    allocation = _allocate(OptionEventKind.ASSIGNED, 1, [bought_first, sold_later])

    assert _taken(allocation) == [("bot-b", 2, 1)]


# ── An exercise ─────────────────────────────────────────────────────────────


def test_an_exercised_call_buys_the_shares_and_pays_the_strike() -> None:
    allocation = _allocate(OptionEventKind.EXERCISED, -2, [_lot("bot-a", 2)])

    (lot,) = allocation.lots
    assert (lot.contracts, lot.remaining_quantity, lot.option_quantity_change) == (2, 0, -2)
    assert (lot.share_quantity_change, lot.cash_change_cents) == (200, -3_000_000)
    assert allocation.share_price == Decimal("150")


def test_an_exercised_put_sells_the_shares_and_receives_the_strike() -> None:
    allocation = _allocate(OptionEventKind.EXERCISED, -1, [_lot("bot-a", 3, contract=_PUT)], contract=_PUT)

    (lot,) = allocation.lots
    assert (lot.contracts, lot.remaining_quantity, lot.option_quantity_change) == (1, 2, -1)
    assert (lot.share_quantity_change, lot.cash_change_cents) == (-100, 1_500_000)


def test_an_exercise_passes_over_a_sold_lot_however_old() -> None:
    sold_first = _lot("bot-a", -4, opened=_at(date(2023, 7, 3)), sequence=1)
    bought_later = _lot("bot-b", 1, opened=_at(date(2023, 7, 12)), sequence=2)

    allocation = _allocate(OptionEventKind.EXERCISED, -1, [sold_first, bought_later])

    assert _taken(allocation) == [("bot-b", 2, 1)]


# ── An expiry ───────────────────────────────────────────────────────────────


def test_an_expired_bought_lot_is_removed_and_moves_no_shares_and_no_cash() -> None:
    allocation = _allocate(OptionEventKind.EXPIRED, -2, [_lot("bot-a", 2)])

    (lot,) = allocation.lots
    assert (lot.contracts, lot.remaining_quantity, lot.option_quantity_change) == (2, 0, -2)
    assert (lot.share_quantity_change, lot.cash_change_cents) == (0, 0)
    assert allocation.share_price is None


def test_an_expired_sold_lot_is_removed_and_moves_no_shares_and_no_cash() -> None:
    allocation = _allocate(OptionEventKind.EXPIRED, 3, [_lot("bot-a", -3)])

    (lot,) = allocation.lots
    assert (lot.contracts, lot.remaining_quantity, lot.option_quantity_change) == (3, 0, 3)
    assert (lot.share_quantity_change, lot.cash_change_cents) == (0, 0)


@pytest.mark.parametrize("change", [-1, 1, -3, 3], ids=["the net", "the net, signed as sold", "bought gross", "the gross"])
def test_an_expiry_of_a_contract_held_on_both_sides_is_refused_and_takes_nothing(change: int) -> None:
    """Bot A sold one and bot B bought two. The account is net long one, and Alpaca's row is that net.

    Taking the row's one contract from either side leaves the books holding
    an expired contract the broker no longer does. No count can be matched.
    """
    sold = _lot("bot-a", -1, opened=_at(date(2023, 7, 10)), sequence=1)
    bought = _lot("bot-b", 2, opened=_at(date(2023, 7, 11)), sequence=2)

    with pytest.raises(OptionEventRefused) as refused:
        _allocate(OptionEventKind.EXPIRED, change, [bought, sold])

    assert refused.value.reason is OptionEventRefusal.HELD_ON_BOTH_SIDES
    assert refused.value.unmatched_contracts == abs(change)


@pytest.mark.parametrize("kind", [OptionEventKind.ASSIGNED, OptionEventKind.EXERCISED])
def test_only_an_expiry_is_refused_for_a_contract_held_on_both_sides(kind: OptionEventKind) -> None:
    """An assignment names the sold side and an exercise the bought one, so each has lots to take."""
    lots = [_lot("bot-a", -1, sequence=1), _lot("bot-b", 2, sequence=2)]

    allocation = _allocate(kind, _change(kind, 1), lots)

    expected = {OptionEventKind.ASSIGNED: [("bot-a", 1, 1)], OptionEventKind.EXERCISED: [("bot-b", 2, 1)]}
    assert _taken(allocation) == expected[kind]


@pytest.mark.parametrize(
    ("held", "change", "books_move"),
    [(-2, -2, 2), (2, 2, -2)],
    ids=["sold lots, a row signed as bought", "bought lots, a row signed as sold"],
)
def test_an_expiry_signed_for_the_other_side_still_closes_lots_held_on_one_side_only(
    held: int, change: int, books_move: int
) -> None:
    """Alpaca's pages show only a bought option expiring, so a sold one's sign is not known.

    With lots on one side only, the row can mean nothing but those lots.
    The books then move the opposite way to the row's sign.
    """
    allocation = _allocate(OptionEventKind.EXPIRED, change, [_lot("bot-a", held)])

    (lot,) = allocation.lots
    assert (lot.contracts, lot.remaining_quantity, lot.option_quantity_change) == (2, 0, books_move)


def test_an_expiry_takes_the_newest_lot_first() -> None:
    """What expires is what is left once the oldest lots were assigned."""
    older = _lot("bot-a", -2, opened=_at(date(2023, 7, 10)), sequence=11)
    newer = _lot("bot-b", -3, opened=_at(date(2023, 7, 12)), sequence=14)

    whole = _allocate(OptionEventKind.EXPIRED, 3, [older, newer])
    and_part_of_the_older = _allocate(OptionEventKind.EXPIRED, 4, [older, newer])

    assert _taken(whole) == [("bot-b", 14, 3)]
    assert _taken(and_part_of_the_older) == [("bot-b", 14, 3), ("bot-a", 11, 1)]
    assert and_part_of_the_older.lots[1].remaining_quantity == -1


def test_expiring_lots_opened_in_the_same_millisecond_are_taken_last_in_the_books_order_first() -> None:
    same_instant = _at(date(2023, 7, 10))
    lots = [
        _lot("bot-b", 1, opened=same_instant, sequence=9),
        _lot("bot-a", 1, opened=same_instant, sequence=12),
        _lot("bot-c", 1, opened=same_instant, sequence=3),
    ]

    assert _taken(_allocate(OptionEventKind.EXPIRED, -2, lots)) == [("bot-a", 12, 1), ("bot-b", 9, 1)]


def test_a_lot_opened_on_the_day_it_expires_is_taken() -> None:
    same_day = _lot("bot-a", 1, opened=_at(_EXPIRY_DAY, 15, 30))

    assert _taken(_allocate(OptionEventKind.EXPIRED, -1, [same_day])) == [("bot-a", 0, 1)]


# ── The multiplier ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("multiplier", "shares", "cash_cents"),
    [(100, 300, -4_500_000), (10, 30, -450_000), (1, 3, -45_000), (150, 450, -6_750_000)],
)
def test_shares_and_cash_follow_the_contracts_multiplier(multiplier: int, shares: int, cash_cents: int) -> None:
    allocation = _allocate(OptionEventKind.EXERCISED, -3, [_lot("bot-a", 3)], multiplier=multiplier)

    assert (allocation.share_quantity_change, allocation.cash_change_cents) == (shares, cash_cents)
    assert allocation.multiplier == multiplier


def test_a_strike_in_half_dollars_comes_to_exact_cents() -> None:
    contract = OptionContract.from_occ_symbol("QS240301C00006500")

    allocation = _allocate(
        OptionEventKind.EXERCISED, -1, [_lot("bot-a", 1, contract=contract)], contract=contract, on=date(2024, 2, 27)
    )

    assert (allocation.share_quantity_change, allocation.cash_change_cents) == (100, -65_000)
    assert allocation.share_price == Decimal("6.5")


# $12.345 a share: one share is 1234.5 cents, and two are 2469.
_HALF_CENT_STRIKE = OptionContract.from_occ_symbol("AAPL230721C00012345")


def test_cash_that_is_not_whole_cents_is_refused_by_name_and_never_rounded() -> None:
    lots = [_lot("bot-a", 1, contract=_HALF_CENT_STRIKE)]

    with pytest.raises(OptionEventRefused) as from_the_rule:
        _allocate(OptionEventKind.EXERCISED, -1, lots, contract=_HALF_CENT_STRIKE, multiplier=1)
    with pytest.raises(OptionEventRefused) as from_the_share_trade:
        expected_share_trade(OptionEventKind.EXERCISED, _HALF_CENT_STRIKE, contracts=1, multiplier=1)

    assert from_the_rule.value.reason is OptionEventRefusal.CASH_NOT_WHOLE_CENTS
    assert from_the_rule.value.unmatched_contracts == 1
    assert from_the_share_trade.value.reason is OptionEventRefusal.CASH_NOT_WHOLE_CENTS


def test_an_events_cash_is_checked_on_its_total_whatever_the_books_hold() -> None:
    """Three contracts come to 3703.5 cents. That is refused for itself, before any lot is looked at."""
    with pytest.raises(OptionEventRefused) as nobody_holds_it:
        _allocate(OptionEventKind.EXERCISED, -3, [], contract=_HALF_CENT_STRIKE, multiplier=1)
    with pytest.raises(OptionEventRefused) as too_few_held:
        _allocate(
            OptionEventKind.EXERCISED, -3, [_lot("bot-a", 2, contract=_HALF_CENT_STRIKE)], contract=_HALF_CENT_STRIKE, multiplier=1
        )

    assert nobody_holds_it.value.reason is OptionEventRefusal.CASH_NOT_WHOLE_CENTS
    assert too_few_held.value.reason is OptionEventRefusal.CASH_NOT_WHOLE_CENTS
    assert (nobody_holds_it.value.unmatched_contracts, too_few_held.value.unmatched_contracts) == (3, 3)


def test_a_total_in_whole_cents_is_booked_when_one_lot_holds_it_all() -> None:
    allocation = _allocate(
        OptionEventKind.EXERCISED, -2, [_lot("bot-a", 2, contract=_HALF_CENT_STRIKE)], contract=_HALF_CENT_STRIKE, multiplier=1
    )

    assert (allocation.share_quantity_change, allocation.cash_change_cents) == (2, -2469)


def test_a_total_in_whole_cents_is_still_refused_when_a_lots_part_of_it_is_not() -> None:
    """2469 cents between two bots is 1234.5 each. Neither can be booked, so the event is refused whole."""
    lots = [
        _lot("bot-a", 1, contract=_HALF_CENT_STRIKE, sequence=1),
        _lot("bot-b", 1, contract=_HALF_CENT_STRIKE, sequence=2),
    ]

    with pytest.raises(OptionEventRefused) as refused:
        _allocate(OptionEventKind.EXERCISED, -2, lots, contract=_HALF_CENT_STRIKE, multiplier=1)

    assert refused.value.reason is OptionEventRefusal.CASH_NOT_WHOLE_CENTS
    # The count is the event's, not the lot's: nothing of it was matched.
    assert refused.value.unmatched_contracts == 2


def test_an_expiry_moves_no_cash_so_no_strike_refuses_it() -> None:
    allocation = _allocate(
        OptionEventKind.EXPIRED, -1, [_lot("bot-a", 1, contract=_HALF_CENT_STRIKE)], contract=_HALF_CENT_STRIKE, multiplier=1
    )

    assert allocation.cash_change_cents == 0


# ── Refusals, by name ───────────────────────────────────────────────────────


def test_an_event_for_more_contracts_than_are_held_is_refused_whole() -> None:
    with pytest.raises(OptionEventRefused, match="hold 3") as refused:
        _allocate(OptionEventKind.ASSIGNED, 5, [_lot("bot-a", -2, sequence=1), _lot("bot-b", -1, sequence=2)])

    assert refused.value.reason is OptionEventRefusal.MORE_CONTRACTS_THAN_HELD
    assert refused.value.unmatched_contracts == 2


def test_an_assignment_of_a_contract_held_only_as_bought_is_refused_as_the_wrong_side() -> None:
    with pytest.raises(OptionEventRefused, match="every lot of it on the books is bought") as refused:
        _allocate(OptionEventKind.ASSIGNED, 1, [_lot("bot-a", 4)])

    assert refused.value.reason is OptionEventRefusal.WRONG_SIDE
    assert refused.value.unmatched_contracts == 1


def test_an_exercise_of_a_contract_held_only_as_sold_is_refused_as_the_wrong_side() -> None:
    with pytest.raises(OptionEventRefused, match="every lot of it on the books is sold") as refused:
        _allocate(OptionEventKind.EXERCISED, -2, [_lot("bot-a", -4)])

    assert refused.value.reason is OptionEventRefusal.WRONG_SIDE
    assert refused.value.unmatched_contracts == 2


@pytest.mark.parametrize("kind", list(OptionEventKind))
def test_an_event_in_a_contract_nobody_holds_is_refused(kind: OptionEventKind) -> None:
    with pytest.raises(OptionEventRefused, match="no bot holds") as refused:
        _allocate(kind, _change(kind, 2), [])

    assert refused.value.reason is OptionEventRefusal.CONTRACT_NOT_HELD
    assert refused.value.unmatched_contracts == 2


def test_the_right_side_must_hold_enough_on_its_own() -> None:
    """Three bought and one sold are four lots' worth, and an exercise of four is still one too many."""
    with pytest.raises(OptionEventRefused) as refused:
        _allocate(OptionEventKind.EXERCISED, -4, [_lot("bot-a", 3, sequence=1), _lot("bot-b", -1, sequence=2)])

    assert refused.value.reason is OptionEventRefusal.MORE_CONTRACTS_THAN_HELD
    assert refused.value.unmatched_contracts == 1


# ── A lot must have been open when the event happened ───────────────────────


def test_a_lot_opened_after_the_events_date_is_passed_over() -> None:
    assigned_on = date(2023, 7, 11)
    open_then = _lot("bot-b", -1, opened=_at(date(2023, 7, 11), 15, 59), sequence=7)
    opened_next_morning = _lot("bot-a", -1, opened=_at(date(2023, 7, 12), 9, 31), sequence=8)

    allocation = _allocate(OptionEventKind.ASSIGNED, 1, [opened_next_morning, open_then], on=assigned_on)

    assert _taken(allocation) == [("bot-b", 7, 1)]


def test_an_event_is_not_matched_to_lots_that_were_all_opened_after_it() -> None:
    with pytest.raises(OptionEventRefused, match="Lots opened after that date hold 1 more") as refused:
        _allocate(
            OptionEventKind.ASSIGNED,
            1,
            [_lot("bot-a", -1, opened=_at(date(2023, 7, 12), 9, 31))],
            on=date(2023, 7, 11),
        )

    assert refused.value.reason is OptionEventRefusal.MORE_CONTRACTS_THAN_HELD
    assert refused.value.unmatched_contracts == 1


def test_a_lots_opening_date_is_its_new_york_date() -> None:
    """23:59 New York on the 11th is the 11th, though UTC is already on the 12th."""
    last_minute = _lot("bot-a", -1, opened=_at(date(2023, 7, 11), 23, 59))

    assert _taken(_allocate(OptionEventKind.ASSIGNED, 1, [last_minute], on=date(2023, 7, 11))) == [("bot-a", 0, 1)]


# ── The order of lots ───────────────────────────────────────────────────────


def test_lots_opened_in_the_same_millisecond_are_taken_in_the_books_own_order() -> None:
    same_instant = _at(date(2023, 7, 10))
    lots = [
        _lot("bot-b", -1, opened=same_instant, sequence=9),
        _lot("bot-a", -1, opened=same_instant, sequence=12),
        _lot("bot-c", -1, opened=same_instant, sequence=3),
    ]

    assert _taken(_allocate(OptionEventKind.ASSIGNED, 2, lots)) == [("bot-c", 3, 1), ("bot-b", 9, 1)]


def test_the_opening_time_decides_before_the_sequence_does() -> None:
    earlier = _lot("bot-a", -1, opened=_at(date(2023, 7, 10)), sequence=50)
    later = _lot("bot-b", -1, opened=_at(date(2023, 7, 10), ms=1), sequence=2)

    assert _taken(_allocate(OptionEventKind.ASSIGNED, 1, [later, earlier])) == [("bot-a", 50, 1)]


def test_two_lots_of_one_bot_are_each_named_in_the_allocation() -> None:
    first = _lot("bot-a", 1, opened=_at(date(2023, 7, 10)), sequence=1)
    second = _lot("bot-a", 2, opened=_at(date(2023, 7, 11)), sequence=2)

    assert _taken(_allocate(OptionEventKind.EXERCISED, -2, [second, first])) == [("bot-a", 1, 1), ("bot-a", 2, 1)]


# ── What the caller must not hand in ────────────────────────────────────────


def test_a_lot_of_another_contract_is_never_taken() -> None:
    with pytest.raises(ValueError, match="AAPL230721P00150000"):
        _allocate(OptionEventKind.EXPIRED, -1, [_lot("bot-a", 1), _lot("bot-b", 1, contract=_PUT)])


def test_one_lot_offered_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="offered twice"):
        _allocate(OptionEventKind.EXPIRED, -1, [_lot("bot-a", 1, sequence=4), _lot("bot-a", 2, sequence=4)])


@pytest.mark.parametrize("change", [0, True, False, 1.0, -1.5, "1", None])
def test_an_event_changes_a_position_by_a_whole_number_of_contracts_and_never_none(change: Any) -> None:
    with pytest.raises(ValueError, match="an event's position change"):
        _allocate(OptionEventKind.EXPIRED, change, [_lot("bot-a", 1)])


@pytest.mark.parametrize(
    ("kind", "change", "lots"),
    [
        (OptionEventKind.ASSIGNED, -1, [_lot("bot-a", -1)]),
        (OptionEventKind.ASSIGNED, -1, [_lot("bot-a", 1)]),
        (OptionEventKind.EXERCISED, 1, [_lot("bot-a", 1)]),
        (OptionEventKind.EXERCISED, 1, [_lot("bot-a", -1)]),
    ],
    ids=[
        "an assignment signed as a bought position's, sold lots held",
        "an assignment signed as a bought position's, bought lots held",
        "an exercise signed as a sold position's, bought lots held",
        "an exercise signed as a sold position's, sold lots held",
    ],
)
def test_a_change_signed_for_the_side_its_kind_cannot_end_is_matched_to_nothing(
    kind: OptionEventKind, change: int, lots: list[HeldOptionLot]
) -> None:
    """Only a sold option is assigned and only a bought one exercised. Neither is booked as its opposite."""
    with pytest.raises(ValueError, match=f"an option that is {kind.value} ends a"):
        _allocate(kind, change, lots)


@pytest.mark.parametrize("kind", list(OptionEventKind))
def test_a_kind_given_as_plain_text_is_the_kind_it_spells(kind: OptionEventKind) -> None:
    """``"expired"`` is an expiry. It was once taken for an assignment, because it is not the enum's own member."""
    held = -2 if kind is OptionEventKind.ASSIGNED else 2
    lots = [_lot("bot-a", held)]
    plain: Any = str(kind.value)
    assert type(plain) is str

    assert _allocate(plain, _change(kind, 2), lots) == _allocate(kind, _change(kind, 2), lots)
    assert expected_share_trade(plain, _CALL, contracts=2, multiplier=100) == expected_share_trade(
        kind, _CALL, contracts=2, multiplier=100
    )


@pytest.mark.parametrize("kind", ["lapsed", "EXPIRED", "", None, 3])
def test_a_kind_that_spells_none_of_the_three_is_refused(kind: Any) -> None:
    with pytest.raises(ValueError, match="OptionEventKind"):
        _allocate(kind, -1, [_lot("bot-a", 1)])
    with pytest.raises(ValueError, match="OptionEventKind"):
        expected_share_trade(kind, _CALL, contracts=1, multiplier=100)


@pytest.mark.parametrize("multiplier", [0, -100, True, 100.0])
def test_a_multiplier_is_a_whole_number_one_or_more(multiplier: Any) -> None:
    with pytest.raises(ValueError, match="multiplier"):
        _allocate(OptionEventKind.EXPIRED, -1, [_lot("bot-a", 1)], multiplier=multiplier)


@pytest.mark.parametrize("quantity", [0, True, 1.5, "2"])
def test_an_open_lot_holds_a_whole_number_of_contracts_and_never_none(quantity: Any) -> None:
    with pytest.raises(ValueError, match="never zero"):
        _lot("bot-a", quantity)


def test_a_lot_belongs_to_a_named_owner() -> None:
    with pytest.raises(ValueError, match="named owner"):
        _lot("  ", 1)


# ── Every allocation, whatever the lots ─────────────────────────────────────


def test_lots_taken_always_hold_the_events_contracts_and_add_up_to_its_share_trade() -> None:
    rng = random.Random(2913)
    allocated = 0
    refused_for: set[OptionEventRefusal] = set()
    for _ in range(900):
        kind = rng.choice(list(OptionEventKind))
        contract = rng.choice([_CALL, _PUT])
        multiplier = rng.choice([1, 10, 100])
        # Two books in three hold the contract on one side only, as a netted account's bots mostly do.
        sides = rng.choice([[-1], [1], [-1, 1]])
        lots = [
            HeldOptionLot(
                owner=f"bot-{rng.randrange(4)}",
                contract=contract,
                quantity=rng.choice(sides) * rng.randrange(1, 6),
                opened_at_ms=_at(date(2023, 7, rng.randrange(3, 22)), ms=rng.randrange(3)),
                sequence=sequence,
            )
            for sequence in range(rng.randrange(1, 7))
        ]
        contracts = rng.randrange(1, 12)
        change = _change(kind, contracts, sold=rng.choice([True, False]))
        on = date(2023, 7, rng.randrange(8, 22))
        held_sides = {lot.quantity > 0 for lot in lots}
        try:
            allocation = _allocate(kind, change, lots, contract=contract, on=on, multiplier=multiplier)
        except OptionEventRefused as refused:
            assert 1 <= refused.unmatched_contracts <= contracts
            # An expiry of a contract held on both sides is always refused, and for that.
            assert (refused.reason is OptionEventRefusal.HELD_ON_BOTH_SIDES) == (
                kind is OptionEventKind.EXPIRED and len(held_sides) == 2
            )
            refused_for.add(refused.reason)
            continue
        allocated += 1

        by_name = {(lot.owner, lot.sequence): lot for lot in lots}
        assert sum(taken.contracts for taken in allocation.lots) == contracts
        # An assignment or an exercise takes the oldest lots first, an expiry the newest.
        order = [(taken.opened_at_ms, taken.sequence, taken.owner) for taken in allocation.lots]
        assert order == sorted(order, reverse=kind is OptionEventKind.EXPIRED)
        for taken in allocation.lots:
            held = by_name[taken.owner, taken.sequence]
            assert 1 <= taken.contracts <= abs(held.quantity)
            assert datetime.fromtimestamp(held.opened_at_ms / 1000, tz=_NY).date() <= on
            assert held.quantity + taken.option_quantity_change == taken.remaining_quantity
            # A lot is never turned round: what is left is on the side it was on, or nothing.
            assert taken.remaining_quantity * held.quantity >= 0
            if kind is OptionEventKind.ASSIGNED:
                assert held.quantity < 0
            if kind is OptionEventKind.EXERCISED:
                assert held.quantity > 0
        if kind is OptionEventKind.EXPIRED:
            # An expiry is matched only when the contract is held on one side.
            assert len(held_sides) == 1
        # Only the last lot taken may be taken in part.
        assert all(taken.remaining_quantity == 0 for taken in allocation.lots[:-1])
        trade = expected_share_trade(kind, contract, contracts=contracts, multiplier=multiplier)
        if trade is None:
            assert (allocation.share_quantity_change, allocation.cash_change_cents) == (0, 0)
        else:
            assert (allocation.share_quantity_change, allocation.cash_change_cents) == (trade.shares, trade.cash_cents)
    # The sweep is not all refusals, and it meets each refusal the books can cause.
    assert allocated > 150
    assert refused_for == {
        OptionEventRefusal.WRONG_SIDE,
        OptionEventRefusal.MORE_CONTRACTS_THAN_HELD,
        OptionEventRefusal.HELD_ON_BOTH_SIDES,
    }


# ── Which bot is assigned does not turn on which row is matched first ───────


def _books_after(lots: list[HeldOptionLot], allocation: Any) -> list[HeldOptionLot]:
    """The open lots once an allocation is booked: what the books hand to the next event."""
    left = {(taken.owner, taken.sequence): taken.remaining_quantity for taken in allocation.lots}
    after = []
    for lot in lots:
        quantity = left.get((lot.owner, lot.sequence), lot.quantity)
        if quantity:
            after.append(replace(lot, quantity=quantity))
    return after


def _ended_and_expired(
    lots: list[HeldOptionLot], events: tuple[tuple[OptionEventKind, int, date], ...]
) -> tuple[dict[tuple[str, int], int], dict[tuple[str, int], int]]:
    """Match ``events`` in the order given, booking each before the next.

    Returns the contracts each lot lost to an assignment or an exercise, and
    the contracts each lot lost to an expiry, by (owner, sequence).
    """
    ended: dict[tuple[str, int], int] = {}
    expired: dict[tuple[str, int], int] = {}
    for kind, change, on in events:
        allocation = _allocate(kind, change, lots, on=on)
        tally = expired if kind is OptionEventKind.EXPIRED else ended
        for taken in allocation.lots:
            tally[taken.owner, taken.sequence] = tally.get((taken.owner, taken.sequence), 0) + taken.contracts
        lots = _books_after(lots, allocation)
    return ended, expired


def _from_one_end(lots: list[HeldOptionLot], contracts: int, *, newest_first: bool) -> dict[tuple[str, int], int]:
    """``contracts`` counted off the lots from the oldest end, or the newest: the rule, worked by hand."""
    taken: dict[tuple[str, int], int] = {}
    for lot in sorted(lots, key=lambda lot: (lot.opened_at_ms, lot.sequence, lot.owner), reverse=newest_first):
        take = min(abs(lot.quantity), contracts)
        if take:
            taken[lot.owner, lot.sequence] = take
        contracts -= take
    return taken


def _in_parts(rng: random.Random, total: int) -> list[int]:
    """``total`` as one row, or split over two, as a broker might report it."""
    if total < 2 or rng.random() < 0.5:
        return [total] if total else []
    first = rng.randrange(1, total)
    return [first, total - first]


def test_the_lots_assigned_are_the_same_whichever_row_is_matched_first() -> None:
    """Random one-sided books; some contracts assigned or exercised, some expired; every order of the rows.

    An assignment dated the day after the expiry it belongs with (as Alpaca's
    own example is), or read in a later poll, is matched after it. The bot
    assigned must still be the one that sold first.
    """
    rng = random.Random(291313)
    rows_in_a_book: set[int] = set()
    for _ in range(400):
        sold = rng.choice([True, False])
        ending = OptionEventKind.ASSIGNED if sold else OptionEventKind.EXERCISED
        lots = [
            HeldOptionLot(
                owner=f"bot-{rng.randrange(4)}",
                contract=_CALL,
                quantity=(-1 if sold else 1) * rng.randrange(1, 5),
                opened_at_ms=_at(date(2023, 7, rng.randrange(3, 15)), ms=rng.randrange(2)),
                sequence=sequence,
            )
            for sequence in range(rng.randrange(2, 6))
        ]
        held = sum(abs(lot.quantity) for lot in lots)
        ended_count = rng.randrange(1, held)
        expired_count = rng.randrange(1, held - ended_count + 1)
        # The assignment may be dated early, on expiry day, or the Saturday after; the expiry on the day or after.
        events = (
            *((ending, _change(ending, part), date(2023, 7, rng.choice([17, 21, 22]))) for part in _in_parts(rng, ended_count)),
            *(
                (OptionEventKind.EXPIRED, _change(OptionEventKind.EXPIRED, part, sold=sold), date(2023, 7, rng.choice([21, 22])))
                for part in _in_parts(rng, expired_count)
            ),
        )
        by_hand = (
            _from_one_end(lots, ended_count, newest_first=False),
            _from_one_end(lots, expired_count, newest_first=True),
        )

        rows_in_a_book.add(len(events))
        for order in permutations(events):
            assert _ended_and_expired(lots, order) == by_hand
    # The sweep holds books of two, three and four rows: up to twenty-four orders each.
    assert rows_in_a_book == {2, 3, 4}


# ── Held to what Alpaca publishes ───────────────────────────────────────────

_KIND_OF_ALPACA_CODE = {"OPEXC": OptionEventKind.EXERCISED, "OPASN": OptionEventKind.ASSIGNED}
_DOCUMENTED = documented("option_activities", "documented_activities.json")
# Each pair of rows Alpaca publishes: the event, and the share trade it says the event created.
_DOCUMENTED_PAIRS = {
    f"{product}/{heading}": rows
    for product, examples in _DOCUMENTED.items()
    for heading, rows in examples.items()
    if len(rows) == 2
}
# The Trading API page's exercised calls carry "price": "90", against its own
# sentence ("a per-share amount of $150 (strike price)") and its own net
# amount (-30000 for 200 shares). The shares and the cash are still held to it.
_PRICE_ALPACA_PRINTS_AGAINST_ITS_OWN_TEXT = {"trading_api/option_exercise": Decimal("90")}


def test_alpaca_publishes_six_pairs_of_an_event_and_its_share_trade() -> None:
    assert sorted(_DOCUMENTED_PAIRS) == [
        "broker_api/call_assignment",
        "broker_api/exercising_call",
        "broker_api/exercising_put",
        "broker_api/put_assignment",
        "trading_api/option_assignment",
        "trading_api/option_exercise",
    ]


@pytest.mark.parametrize("name", sorted(_DOCUMENTED_PAIRS))
def test_the_share_trade_of_each_documented_event_is_the_one_alpaca_publishes(name: str) -> None:
    event, share_trade = _DOCUMENTED_PAIRS[name]
    contract = OptionContract.from_occ_symbol(event["symbol"])

    trade = expected_share_trade(
        _KIND_OF_ALPACA_CODE[event["activity_type"]], contract, contracts=abs(int(event["qty"])), multiplier=100
    )

    assert isinstance(trade, ShareTrade)
    assert trade.root == share_trade["symbol"]
    assert trade.shares == int(share_trade["qty"])
    assert trade.cash_cents == Decimal(share_trade["net_amount"]) * 100
    printed_price = Decimal(share_trade["price"])
    assert printed_price == _PRICE_ALPACA_PRINTS_AGAINST_ITS_OWN_TEXT.get(name, trade.price)
    assert trade.price == contract.strike


@pytest.mark.parametrize("name", ["trading_api/otm_option_expiry", "broker_api/call_expiration", "broker_api/put_expiration"])
def test_a_documented_expiry_comes_with_no_share_trade(name: str) -> None:
    product, heading = name.split("/")
    (event,) = _DOCUMENTED[product][heading]

    contract = OptionContract.from_occ_symbol(event["symbol"])

    assert expected_share_trade(OptionEventKind.EXPIRED, contract, contracts=abs(int(event["qty"])), multiplier=100) is None
