"""Which lots an assignment, an exercise or an expiry takes, and what each owner's books then show.

Every figure is a whole number of contracts, shares or cents, so every
comparison is exact. The shares and cash are held to the pairs of activities
Alpaca publishes (``tests/fixtures/alpaca_documented/option_activities``):
documentation, not recordings.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime
from decimal import Decimal
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
    contracts: int,
    lots: list[HeldOptionLot],
    *,
    contract: OptionContract = _CALL,
    on: date = _EXPIRY_DAY,
    multiplier: int = 100,
) -> Any:
    return allocate_option_event(
        kind=kind, contract=contract, contracts=contracts, event_date_ms=_at(on, 0), multiplier=multiplier, lots=lots
    )


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
    allocation = _allocate(OptionEventKind.EXERCISED, 2, [_lot("bot-a", 2)])

    (lot,) = allocation.lots
    assert (lot.contracts, lot.remaining_quantity, lot.option_quantity_change) == (2, 0, -2)
    assert (lot.share_quantity_change, lot.cash_change_cents) == (200, -3_000_000)
    assert allocation.share_price == Decimal("150")


def test_an_exercised_put_sells_the_shares_and_receives_the_strike() -> None:
    allocation = _allocate(OptionEventKind.EXERCISED, 1, [_lot("bot-a", 3, contract=_PUT)], contract=_PUT)

    (lot,) = allocation.lots
    assert (lot.contracts, lot.remaining_quantity, lot.option_quantity_change) == (1, 2, -1)
    assert (lot.share_quantity_change, lot.cash_change_cents) == (-100, 1_500_000)


def test_an_exercise_passes_over_a_sold_lot_however_old() -> None:
    sold_first = _lot("bot-a", -4, opened=_at(date(2023, 7, 3)), sequence=1)
    bought_later = _lot("bot-b", 1, opened=_at(date(2023, 7, 12)), sequence=2)

    allocation = _allocate(OptionEventKind.EXERCISED, 1, [sold_first, bought_later])

    assert _taken(allocation) == [("bot-b", 2, 1)]


# ── An expiry ───────────────────────────────────────────────────────────────


def test_an_expired_bought_lot_is_removed_and_moves_no_shares_and_no_cash() -> None:
    allocation = _allocate(OptionEventKind.EXPIRED, 2, [_lot("bot-a", 2)])

    (lot,) = allocation.lots
    assert (lot.contracts, lot.remaining_quantity, lot.option_quantity_change) == (2, 0, -2)
    assert (lot.share_quantity_change, lot.cash_change_cents) == (0, 0)
    assert allocation.share_price is None


def test_an_expired_sold_lot_is_removed_and_moves_no_shares_and_no_cash() -> None:
    allocation = _allocate(OptionEventKind.EXPIRED, 3, [_lot("bot-a", -3)])

    (lot,) = allocation.lots
    assert (lot.contracts, lot.remaining_quantity, lot.option_quantity_change) == (3, 0, 3)
    assert (lot.share_quantity_change, lot.cash_change_cents) == (0, 0)


def test_an_expiry_takes_bought_and_sold_lots_alike_oldest_first() -> None:
    sold = _lot("bot-a", -1, opened=_at(date(2023, 7, 10)), sequence=1)
    bought = _lot("bot-b", 2, opened=_at(date(2023, 7, 11)), sequence=2)

    allocation = _allocate(OptionEventKind.EXPIRED, 3, [bought, sold])

    assert _taken(allocation) == [("bot-a", 1, 1), ("bot-b", 2, 2)]
    assert [lot.option_quantity_change for lot in allocation.lots] == [1, -2]


def test_a_lot_opened_on_the_day_it_expires_is_taken() -> None:
    same_day = _lot("bot-a", 1, opened=_at(_EXPIRY_DAY, 15, 30))

    assert _taken(_allocate(OptionEventKind.EXPIRED, 1, [same_day])) == [("bot-a", 0, 1)]


# ── The multiplier ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("multiplier", "shares", "cash_cents"),
    [(100, 300, -4_500_000), (10, 30, -450_000), (1, 3, -45_000), (150, 450, -6_750_000)],
)
def test_shares_and_cash_follow_the_contracts_multiplier(multiplier: int, shares: int, cash_cents: int) -> None:
    allocation = _allocate(OptionEventKind.EXERCISED, 3, [_lot("bot-a", 3)], multiplier=multiplier)

    assert (allocation.share_quantity_change, allocation.cash_change_cents) == (shares, cash_cents)
    assert allocation.multiplier == multiplier


def test_a_strike_in_half_dollars_comes_to_exact_cents() -> None:
    contract = OptionContract.from_occ_symbol("QS240301C00006500")

    allocation = _allocate(
        OptionEventKind.EXERCISED, 1, [_lot("bot-a", 1, contract=contract)], contract=contract, on=date(2024, 2, 27)
    )

    assert (allocation.share_quantity_change, allocation.cash_change_cents) == (100, -65_000)
    assert allocation.share_price == Decimal("6.5")


def test_cash_that_is_not_whole_cents_is_refused_and_never_rounded() -> None:
    # $12.345 a share for one share is 1234.5 cents.
    contract = OptionContract.from_occ_symbol("AAPL230721C00012345")

    with pytest.raises(ValueError, match="whole cents"):
        _allocate(OptionEventKind.EXERCISED, 1, [_lot("bot-a", 1, contract=contract)], contract=contract, multiplier=1)
    with pytest.raises(ValueError, match="whole cents"):
        expected_share_trade(OptionEventKind.EXERCISED, contract, contracts=1, multiplier=1)


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
        _allocate(OptionEventKind.EXERCISED, 2, [_lot("bot-a", -4)])

    assert refused.value.reason is OptionEventRefusal.WRONG_SIDE
    assert refused.value.unmatched_contracts == 2


@pytest.mark.parametrize("kind", list(OptionEventKind))
def test_an_event_in_a_contract_nobody_holds_is_refused(kind: OptionEventKind) -> None:
    with pytest.raises(OptionEventRefused, match="no bot holds") as refused:
        _allocate(kind, 2, [])

    assert refused.value.reason is OptionEventRefusal.CONTRACT_NOT_HELD
    assert refused.value.unmatched_contracts == 2


def test_the_right_side_must_hold_enough_on_its_own() -> None:
    """Three bought and one sold are four lots' worth, and an exercise of four is still one too many."""
    with pytest.raises(OptionEventRefused) as refused:
        _allocate(OptionEventKind.EXERCISED, 4, [_lot("bot-a", 3, sequence=1), _lot("bot-b", -1, sequence=2)])

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

    assert _taken(_allocate(OptionEventKind.EXERCISED, 2, [second, first])) == [("bot-a", 1, 1), ("bot-a", 2, 1)]


# ── What the caller must not hand in ────────────────────────────────────────


def test_a_lot_of_another_contract_is_never_taken() -> None:
    with pytest.raises(ValueError, match="AAPL230721P00150000"):
        _allocate(OptionEventKind.EXPIRED, 1, [_lot("bot-a", 1), _lot("bot-b", 1, contract=_PUT)])


def test_one_lot_offered_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="offered twice"):
        _allocate(OptionEventKind.EXPIRED, 1, [_lot("bot-a", 1, sequence=4), _lot("bot-a", 2, sequence=4)])


@pytest.mark.parametrize("contracts", [0, -1, True, 1.0, "1"])
def test_an_event_is_for_a_whole_number_of_contracts_one_or_more(contracts: Any) -> None:
    with pytest.raises(ValueError, match="an event's contracts"):
        _allocate(OptionEventKind.EXPIRED, contracts, [_lot("bot-a", 1)])


@pytest.mark.parametrize("multiplier", [0, -100, True, 100.0])
def test_a_multiplier_is_a_whole_number_one_or_more(multiplier: Any) -> None:
    with pytest.raises(ValueError, match="multiplier"):
        _allocate(OptionEventKind.EXPIRED, 1, [_lot("bot-a", 1)], multiplier=multiplier)


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
    for _ in range(600):
        kind = rng.choice(list(OptionEventKind))
        contract = rng.choice([_CALL, _PUT])
        multiplier = rng.choice([1, 10, 100])
        lots = [
            HeldOptionLot(
                owner=f"bot-{rng.randrange(4)}",
                contract=contract,
                quantity=rng.choice([-1, 1]) * rng.randrange(1, 6),
                opened_at_ms=_at(date(2023, 7, rng.randrange(3, 22)), ms=rng.randrange(3)),
                sequence=sequence,
            )
            for sequence in range(rng.randrange(1, 7))
        ]
        contracts = rng.randrange(1, 12)
        on = date(2023, 7, rng.randrange(8, 22))
        try:
            allocation = _allocate(kind, contracts, lots, contract=contract, on=on, multiplier=multiplier)
        except OptionEventRefused as refused:
            assert 1 <= refused.unmatched_contracts <= contracts
            continue
        allocated += 1

        by_name = {(lot.owner, lot.sequence): lot for lot in lots}
        assert sum(taken.contracts for taken in allocation.lots) == contracts
        order = [(taken.opened_at_ms, taken.sequence, taken.owner) for taken in allocation.lots]
        assert order == sorted(order)
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
        # Only the last lot taken may be taken in part.
        assert all(taken.remaining_quantity == 0 for taken in allocation.lots[:-1])
        trade = expected_share_trade(kind, contract, contracts=contracts, multiplier=multiplier)
        if trade is None:
            assert (allocation.share_quantity_change, allocation.cash_change_cents) == (0, 0)
        else:
            assert (allocation.share_quantity_change, allocation.cash_change_cents) == (trade.shares, trade.cash_cents)
    # The sweep is not all refusals: a few hundred events were matched.
    assert allocated > 150


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
