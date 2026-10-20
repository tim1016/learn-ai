"""Selling in the replay: a covered spread opens at the entry time and closes at the exit time (#3106).

Every scene is worked by hand from the rules of #3095 and never taken from the code's output. Minutes are counted after
the open: a setup here enters at minute 5, so its orders fill at the open of minute 6, and leaves at minute 60, so its
closes fill at the open of minute 61. There is no slippage and no fee unless a scene says so. Money is exact.

A sold leg opens at the sale price and is bought back at the buy price; cash moves by what it sold for less its fee, and
by what it was bought back for plus its fee. Collateral is the canonical requirement of what is held: the width of a
spread times its contracts times 100 for one expiry. Free cash is cash less collateral.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

import pytest

from app.options.contract import OptionContract
from app.options_backtest import replay as replay_module
from app.options_backtest.cover_book import CoverInvariantBroken, OpeningRefusal
from app.options_backtest.describe import leg_sentences
from app.options_backtest.replay import entry_refusal
from app.options_backtest.results import (
    ENTRY_REFUSAL_EVENTS,
    EntryRefusal,
    Event,
    EventKind,
    ExitReason,
    FillGroup,
    MoneyStep,
    SessionOutcome,
    SessionStatus,
)
from tests.options_backtest._market import (
    D,
    TableMarket,
    at,
    contract,
    fee_per_contract,
    flat,
    leg,
    minute_of,
    picked,
    replay,
    row,
    setup,
    steady,
)

PUT_490 = contract("put", "490")
PUT_495 = contract("put", "495")
PUT_500 = contract("put", "500")
CALL_500 = contract("call", "500")
CALL_505 = contract("call", "505")
CALL_510 = contract("call", "510")
CALL_530 = contract("call", "530")
PUT_480 = contract("put", "480")
LATER = date(2026, 10, 16)
CALL_500_LATER = OptionContract.of(root="SPY", expiry_date=LATER, right="call", strike=D("500"))
PUT_495_LATER = OptionContract.of(root="SPY", expiry_date=LATER, right="put", strike=D("495"))
PUT_500_LATER = OptionContract.of(root="SPY", expiry_date=LATER, right="put", strike=D("500"))


def _refusals(outcome: SessionOutcome) -> list[tuple[int, str, EventKind, str | None, Decimal | None]]:
    """What was refused or not taken at an entry: ``(minute, leg, event, reason, price it would have filled at)``."""
    return [
        (minute_of(event.at_ms), event.leg_id, event.kind, event.reason, event.price)
        for event in outcome.events
        if event.kind in set(ENTRY_REFUSAL_EVENTS.values())
    ]


def _credit_put_spread() -> TableMarket:
    """A put 495 (the cover) bought at 1.00 and a put 500 sold at 2.20, both traded until minute 60. At minute 61 the
    495 is 0.60 and the 500 is 1.50."""
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: {**flat("1.00", 5, 60), 61: ("0.60", "0.60")},
            PUT_500: {**flat("2.20", 5, 60), 61: ("1.50", "1.50")},
        },
    )


def _spread_of_two() -> tuple[dict[str, Any], ...]:
    return leg("long", "put", contracts=2), leg("short", "put", contracts=2, side="sell")


# ---- A put spread sold at the entry time and closed at the exit time ------------


def test_a_put_spread_sold_at_the_entry_time_is_bought_back_and_sold_at_the_exit_time() -> None:
    written = setup(*_spread_of_two())

    outcome = replay(written, _credit_put_spread(), fee_for=fee_per_contract("0.65"))

    # Minute 6: the 495 put is bought at 1.00 (2 contracts: 200.00, fee 1.30) and the 500 put sold at 2.20 (440.00, fee
    # 1.30). Minute 61: the 500 put is bought back at 1.50 (300.00, fee 1.30) and, after it, the 495 put sold at 0.60
    # (120.00, fee 1.30).
    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("1.00"), D("1.00"), "exit_time", 61, D("0.60"), D("0.60")),
        ("short", 0, 6, D("2.20"), D("2.20"), "exit_time", 61, D("1.50"), D("1.50")),
    ], "the spread is opened at minute 6 and closed at minute 61 at the exit time"
    assert [(trade.entry.side, trade.exit.side) for trade in outcome.trades] == [
        ("buy", "sell"),
        ("sell", "buy"),
    ], "the cover is bought and sold, the short leg is sold and bought back"
    # Long: (0.60 - 1.00) x 200 - 2.60 = -82.60. Short: sold 2.20, bought back 1.50: (2.20 - 1.50) x 200 - 2.60 = 137.40.
    assert [trade.pnl_usd for trade in outcome.trades] == [D("-82.60"), D("137.40")], "each leg's profit"
    assert [(trade.entry_fee_usd, trade.exit_fee_usd) for trade in outcome.trades] == [
        (D("1.30"), D("1.30"))
    ] * 2, "each fill pays 0.65 x 2 contracts"
    # Cash: -201.30 + 438.70 = +237.40, then -301.30 + 118.70 = -182.60. Net +54.80, which is the profit.
    assert outcome.cash_change_usd == D("54.80"), "the cash that moved"
    assert (outcome.pnl_usd, outcome.marked_value_usd) == (D("54.80"), D("0")), "the profit is the cash, nothing marked"
    assert outcome.status is SessionStatus.TRADED, "the session traded"
    assert outcome.money_steps == (
        MoneyStep(
            at_ms=at(6),
            group=FillGroup.OPENING,
            cash_before_usd=D("10000"),
            cash_after_usd=D("10237.40"),
            # Nothing was held. Then a put spread 5 wide, 2 contracts, 100 shares: 5 x 2 x 100.
            collateral_before_usd=D("0"),
            collateral_after_usd=D("1000.00"),
            # Two sold contracts of the 500 strike, 100 shares each. Gross, and not a loss.
            obligation_after_usd=D("100000"),
            bought_back_with_cover_held=False,
        ),
        MoneyStep(
            at_ms=at(61),
            group=FillGroup.CLOSING,
            cash_before_usd=D("10237.40"),
            cash_after_usd=D("10054.80"),
            collateral_before_usd=D("1000.00"),
            collateral_after_usd=D("0"),
            obligation_after_usd=D("0"),
            # The cover was sold in the same group, so none stayed held.
            bought_back_with_cover_held=False,
        ),
    ), "one opening group and one closing group leave one money step each with the figures worked by hand"


def test_a_closing_group_buys_back_first_and_sells_the_cover_after_it_though_the_cover_is_listed_first() -> None:
    outcome = replay(setup(*_spread_of_two()), _credit_put_spread())  # type: ignore[arg-type]

    closes = [
        (event.leg_id, minute_of(event.at_ms)) for event in outcome.events if event.kind is EventKind.CLOSE_FILLED
    ]

    assert closes == [("short", 61), ("long", 61)]


def test_a_setup_that_buys_only_records_the_money_it_moved_and_pledges_nothing() -> None:
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", CALL_500)), options={CALL_500: flat("2.00", 5, 61)}
    )

    outcome = replay(setup(leg()), market)

    assert outcome.money_steps == (
        MoneyStep(at(6), FillGroup.OPENING, D("10000"), D("9800.00"), D("0"), D("0"), D("0"), False),
        MoneyStep(at(61), FillGroup.CLOSING, D("9800.00"), D("10000.00"), D("0"), D("0"), D("0"), False),
    )


# ---- A sold leg that waits on momentum ---------------------------------------------


def test_a_sold_leg_with_momentum_is_sold_once_its_premium_has_moved_against_the_cover_it_already_holds() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: flat("1.00", 5, 61),
            # The starting price is the close of minute 5, 2.00, so 25% up is the level 2.50. Minute 8 closes at 2.60.
            PUT_500: {**flat("2.00", 5, 7), 8: ("2.00", "2.60"), **flat("2.60", 9, 61)},
        },
    )

    outcome = replay(setup(leg("long", "put"), leg("short", "put", side="sell", momentum=("percent_up", "25"))), market)

    # The sale is decided at minute 8 and fills at the open of minute 9, 2.60, against the 495 held since minute 6.
    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("1.00"), D("1.00"), "exit_time", 61, D("1.00"), D("1.00")),
        ("short", 0, 9, D("2.60"), D("2.60"), "exit_time", 61, D("2.60"), D("2.60")),
    ]
    # Minute 6: -100.00 and no collateral (a bought put has none). Minute 9: +260.00, and a put spread 5 wide: 500.00.
    assert outcome.money_steps[:2] == (
        MoneyStep(at(6), FillGroup.OPENING, D("10000"), D("9900.00"), D("0"), D("0"), D("0"), False),
        MoneyStep(at(9), FillGroup.OPENING, D("9900.00"), D("10160.00"), D("0"), D("500.00"), D("50000"), False),
    )


# ---- The cover has no bar, or is not yet held ---------------------------------------------


def test_a_sold_leg_whose_cover_had_no_bar_in_the_fill_minute_is_refused_for_cover_because_it_did_not_trade() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        # The cover traded at minute 5 and not at minute 6, where the orders fill.
        options={PUT_495: {5: ("1.00", "1.00"), **flat("1.00", 7, 61)}, PUT_500: flat("2.20", 5, 61)},
    )

    outcome = replay(setup(leg("long", "put"), leg("short", "put", side="sell")), market)

    assert outcome.trades == ()
    assert outcome.status is SessionStatus.NO_TRADE
    assert outcome.cash_change_usd == D("0")
    assert outcome.money_steps == ()
    # The cover was dropped, and the sold leg was refused with the price it would have filled at, 2.20.
    assert [(event.leg_id, event.kind) for event in outcome.events if minute_of(event.at_ms) == 6] == [
        ("long", EventKind.ENTRY_DROPPED),
        ("short", EventKind.ENTRY_REFUSED_FOR_COVER),
    ]
    assert _refusals(outcome) == [
        (6, "short", EventKind.ENTRY_REFUSED_FOR_COVER, "cover_did_not_trade", D("2.20")),
    ]


def test_a_sold_leg_whose_cover_still_waits_on_momentum_is_refused_for_cover_as_not_yet_held_and_is_done_for_the_day() -> (
    None
):
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            # The starting price 1.00 makes the level 1.25, which minute 8 reaches: the cover is bought at minute 9.
            PUT_495: {**flat("1.00", 5, 7), 8: ("1.00", "1.30"), **flat("1.30", 9, 61)},
            PUT_500: flat("2.20", 5, 61),
        },
    )
    written = setup(leg("long", "put", momentum=("percent_up", "25")), leg("short", "put", side="sell"))

    outcome = replay(written, market)

    assert _refusals(outcome) == [(6, "short", EventKind.ENTRY_REFUSED_FOR_COVER, "cover_not_yet_held", D("2.20"))]
    # The sold leg's day was over at minute 6. The cover later bought alone is bought and sold as any bought leg is.
    assert [(trade.leg_id, trade.entry.side) for trade in outcome.trades] == [("long", "buy")]
    assert [row(trade)[:3] for trade in outcome.trades] == [("long", 0, 9)]
    assert outcome.money_steps[0].collateral_after_usd == D("0")


def test_the_description_says_what_the_replay_does_with_a_sale_whose_cover_waits_on_momentum_it_does_not_have() -> None:
    # The setup of the scene above, in words. The buy waits for its momentum and the sale does not, so the sentences must
    # not leave the owner to think the sale waits for its cover: it is made as the entry is read or it is refused.
    written = setup(leg("long", "put", momentum=("percent_up", "25")), leg("short", "put", side="sell"))

    bought, sold = (leg_sentences(each) for each in written.legs)

    assert "The buy waits until a minute closes at least 25% above where its premium stood at the entry time." in bought
    assert "The sale comes as soon as the entry is read." in sold
    assert (
        "The sale needs one bought put of the same expiry for each contract sold, held already or filling in the "
        "same minute; with too few, it is refused and the leg is done for the day."
    ) in sold


def test_a_bought_leg_of_the_other_kind_that_was_dropped_is_not_the_cover_that_did_not_trade() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long_call", "500", CALL_505), ("long_put", "500", PUT_495), ("short_call", "500", CALL_500)),
        options={
            # The call cover waits on a momentum its flat premium never reaches. The bought put, which is no cover for
            # a call, traded at minute 5 and not at minute 6, so it is dropped in the group of the sold call.
            CALL_505: flat("1.00", 5, 61),
            PUT_495: {5: ("1.00", "1.00"), **flat("1.00", 7, 61)},
            CALL_500: flat("2.20", 5, 61),
        },
    )
    written = setup(
        leg("long_call", "call", momentum=("percent_up", "25")),
        leg("long_put", "put"),
        leg("short_call", "call", side="sell"),
    )

    outcome = replay(written, market)

    # The put was dropped, and the sold call has none of its own kind: it is not yet held, and did not fail to trade.
    assert [(event.leg_id, event.kind) for event in outcome.events if event.kind is EventKind.ENTRY_DROPPED] == [
        ("long_put", EventKind.ENTRY_DROPPED)
    ]
    assert _refusals(outcome) == [(6, "short_call", EventKind.ENTRY_REFUSED_FOR_COVER, "cover_not_yet_held", D("2.20"))]


def test_a_sold_leg_of_the_same_kind_that_was_dropped_is_no_cover_that_did_not_trade() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("short_a", "500", CALL_500), ("short_b", "500", CALL_505), ("long_call", "500", CALL_510)),
        options={
            # short_a traded at minute 5 and not at minute 6, so it is dropped in the group of short_b. The only bought
            # call waits on a momentum its flat premium never reaches, so short_b has no cover held or filling.
            CALL_500: {5: ("2.00", "2.00"), **flat("2.00", 7, 61)},
            CALL_505: flat("1.50", 5, 61),
            CALL_510: flat("1.00", 5, 61),
        },
    )
    written = setup(
        leg("short_a", "call", side="sell"),
        leg("short_b", "call", side="sell"),
        leg("long_call", "call", contracts=2, momentum=("percent_up", "25")),
    )

    outcome = replay(written, market)

    # A sold leg that found no bar is no bought leg of its kind: nothing was dropped that would have covered short_b.
    assert [(event.leg_id, event.kind) for event in outcome.events if event.kind is EventKind.ENTRY_DROPPED] == [
        ("short_a", EventKind.ENTRY_DROPPED)
    ]
    assert _refusals(outcome) == [(6, "short_b", EventKind.ENTRY_REFUSED_FOR_COVER, "cover_not_yet_held", D("1.50"))]


# ---- Calls and puts that come to two expiries -------------------------------------------


def test_a_put_that_comes_to_a_second_expiry_beside_a_sold_call_is_refused_for_the_second_expiry() -> None:
    # One expiry rule, and the calls came to Friday 2026-10-09 while the put came to the Friday after.
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(
            ("long_call", "500", CALL_505), ("short_call", "500", CALL_500), ("long_put", "500", PUT_495_LATER)
        ),
        options={
            CALL_505: flat("1.00", 5, 61),
            CALL_500: flat("2.20", 5, 61),
            PUT_495_LATER: flat("0.80", 5, 61),
        },
    )
    written = setup(
        leg("long_call", "call"),
        leg("short_call", "call", side="sell"),
        leg("long_put", "put"),
    )

    outcome = replay(written, market)

    # The calls are listed first, so the spread stands; the put is then a second expiry beside a sold contract.
    assert _refusals(outcome) == [
        (6, "long_put", EventKind.ENTRY_REFUSED_FOR_SECOND_EXPIRY, "second_expiry", D("0.80"))
    ]
    assert [trade.leg_id for trade in outcome.trades] == ["long_call", "short_call"]
    # A bear call spread 5 wide, one contract: 500.00, and the put is nowhere in it.
    assert outcome.money_steps[0].collateral_after_usd == D("500.00")
    assert outcome.money_steps[0].cash_after_usd == D("10120.00")  # -100.00 + 220.00


def test_nothing_is_covered_across_two_expiries_so_a_sold_put_on_the_later_one_is_refused_for_the_second_expiry() -> (
    None
):
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500_LATER)),
        options={PUT_495: flat("1.00", 5, 61), PUT_500_LATER: flat("2.20", 5, 61)},
    )

    outcome = replay(setup(leg("long", "put"), leg("short", "put", side="sell")), market)

    # The 495 is bought alone. The sold put is a second expiry, which is the first thing the book finds wrong with it.
    assert _refusals(outcome) == [(6, "short", EventKind.ENTRY_REFUSED_FOR_SECOND_EXPIRY, "second_expiry", D("2.20"))]
    assert [trade.leg_id for trade in outcome.trades] == ["long"]


# ---- Two legs on one contract ---------------------------------------------------------


def test_two_legs_on_one_contract_on_the_same_side_add_up_and_together_cover_what_one_leg_could_not() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL_505), ("b", "500", CALL_505), ("c", "500", CALL_500)),
        options={CALL_505: flat("1.00", 5, 61), CALL_500: flat("2.20", 5, 61)},
    )
    # a and b each buy one 505 call, and c sells two 500 calls: sold 2, bought 1 + 1.
    written = setup(leg("a", "call"), leg("b", "call"), leg("c", "call", contracts=2, side="sell"))

    outcome = replay(written, market)

    assert [trade.leg_id for trade in outcome.trades] == ["a", "b", "c"]
    # A bear call spread 5 wide, two contracts: 1000.00. Cash -100.00 - 100.00 + 440.00.
    assert outcome.money_steps[0].collateral_after_usd == D("1000.00")
    assert outcome.money_steps[0].cash_after_usd == D("10240.00")
    assert outcome.money_steps[0].obligation_after_usd == D("100000")  # 2 sold x 500 x 100


@pytest.mark.parametrize(
    ("listed", "refused", "taken"),
    [
        pytest.param(
            (("a", "buy"), ("b", "sell")),
            ((6, "b", EventKind.ENTRY_REFUSED_FOR_OTHER_SIDE, "other_side", D("2.00")),),
            (("a", "buy"),),
            id="a_leg_that_would_sell_the_contract_another_leg_buys_is_refused_for_the_other_side_when_it_is_later",
        ),
        pytest.param(
            (("b", "sell"), ("a", "buy")),
            # a is the later leg on the contract and is refused for the other side. That leaves b with no cover to stand on.
            (
                (6, "b", EventKind.ENTRY_REFUSED_FOR_COVER, "cover_not_yet_held", D("2.00")),
                (6, "a", EventKind.ENTRY_REFUSED_FOR_OTHER_SIDE, "other_side", D("2.00")),
            ),
            (),
            id="when_the_selling_leg_is_listed_first_the_buying_leg_is_the_later_one_refused_and_the_sale_loses_its_cover",
        ),
    ],
)
def test_the_later_leg_on_a_contract_is_refused_for_the_other_side_and_the_leg_before_it_stands_on(
    listed: tuple[tuple[str, Literal["buy", "sell"]], ...],
    refused: tuple[tuple[int, str, EventKind, str | None, Decimal | None], ...],
    taken: tuple[tuple[str, str], ...],
) -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL_500), ("b", "500", CALL_500)),
        options={CALL_500: flat("2.00", 5, 61)},
    )

    outcome = replay(setup(*(leg(leg_id, "call", side=side) for leg_id, side in listed)), market)

    assert tuple(_refusals(outcome)) == refused, "the entries refused, with the word each is refused in"
    assert tuple((trade.leg_id, trade.entry.side) for trade in outcome.trades) == taken, "the legs that were taken"


# ---- Cash short of the collateral ----------------------------------------------------


def _wide_put_spread() -> TableMarket:
    """A put 490 bought at 0.50 and a put 500 sold at 1.20: a credit of 70.00 and collateral of 10 x 100 = 1000.00."""
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_490), ("short", "500", PUT_500)),
        options={PUT_490: flat("0.50", 5, 61), PUT_500: flat("1.20", 5, 61)},
    )


@pytest.mark.parametrize(
    ("cash", "taken", "refused", "opening"),
    [
        pytest.param(
            "929.99",
            (),
            (
                (6, "long", EventKind.ENTRY_REFUSED_FOR_CASH, "cash_short", D("0.50")),
                (6, "short", EventKind.ENTRY_REFUSED_FOR_CASH, "cash_short", D("1.20")),
            ),
            (),
            # 929.99 - 50.00 + 120.00 = 999.99 is under the 1000.00 the group would pledge. The cover alone would cost 50.00.
            id="cash_short_of_the_collateral_refuses_every_entry_of_the_group_the_cheap_cover_included",
        ),
        pytest.param(
            "930",
            ("long", "short"),
            (),
            ((D("1000.00"), D("1000.00")),),
            # 930.00 - 50.00 + 120.00 = 1000.00, which is the collateral to the cent.
            id="cash_exactly_equal_to_the_collateral_after_the_group_is_enough",
        ),
    ],
)
def test_the_cash_left_after_a_group_must_carry_its_collateral_to_the_cent(
    cash: str,
    taken: tuple[str, ...],
    refused: tuple[tuple[int, str, EventKind, str | None, Decimal | None], ...],
    opening: tuple[tuple[Decimal, Decimal], ...],
) -> None:
    outcome = replay(setup(leg("long", "put"), leg("short", "put", side="sell")), _wide_put_spread(), cash=cash)

    assert tuple(trade.leg_id for trade in outcome.trades) == taken, "the legs that were taken"
    assert tuple(_refusals(outcome)) == refused, "the entries refused for cash, with the price each would have filled at"
    assert outcome.cash_change_usd == D("0"), "the flat prices leave the cash where it began"
    assert tuple(
        (step.cash_after_usd, step.collateral_after_usd) for step in outcome.money_steps[:1]
    ) == opening, "the cash and the collateral after the opening group, and no step when nothing was taken"


# ---- An iron condor ------------------------------------------------------------------


def test_an_iron_condors_collateral_is_its_wider_side_and_not_both_added() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(
            ("long_put", "500", PUT_480),
            ("short_put", "500", PUT_490),
            ("short_call", "500", CALL_510),
            ("long_call", "500", CALL_530),
        ),
        options={
            PUT_480: flat("0.20", 5, 61),
            PUT_490: flat("0.60", 5, 61),
            CALL_510: flat("0.70", 5, 61),
            CALL_530: flat("0.25", 5, 61),
        },
    )
    written = setup(
        leg("long_put", "put"),
        leg("short_put", "put", side="sell"),
        leg("short_call", "call", side="sell"),
        leg("long_call", "call"),
    )

    outcome = replay(written, market)

    # The put side is 10 wide and the call side 20 wide. Only one of them can be lost, so the collateral is the wider:
    # 20 x 100 = 2000.00, and not 3000.00. Cash: -20.00 + 60.00 + 70.00 - 25.00 = +85.00.
    assert [trade.leg_id for trade in outcome.trades] == ["long_put", "short_put", "short_call", "long_call"]
    first = outcome.money_steps[0]
    assert (first.cash_after_usd, first.collateral_after_usd) == (D("10085.00"), D("2000.00"))
    # Sold 490 put and sold 510 call, one contract each: (490 + 510) x 100.
    assert first.obligation_after_usd == D("100000")


# ---- A sale to open at nothing, and a close at nothing -------------------------------


def test_a_sale_to_open_that_would_fill_at_nothing_is_not_taken_and_the_leg_is_done_for_the_day() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={PUT_495: flat("1.00", 5, 61), PUT_500: flat("0.01", 5, 61)},
    )

    # 0.01 x (1 - 0.003) = 0.00997, rounded down to the cent is 0.00.
    outcome = replay(
        setup(leg("long", "put"), leg("short", "put", side="sell"), slippage="0.3"),
        market,
        fee_for=fee_per_contract("0.65"),
    )

    assert _refusals(outcome) == [(6, "short", EventKind.SALE_NOT_TAKEN_AT_ZERO, "sale_at_zero", D("0.00"))]
    # The cover is bought alone: 1.00 x 1.003 = 1.003, up to 1.01, so 101.00 and a fee of 0.65. Nothing was sold, so
    # no fee for it.
    assert [(trade.leg_id, trade.entry_fee_usd) for trade in outcome.trades] == [("long", D("0.65"))]
    assert outcome.money_steps[0].cash_after_usd == D("9898.35")


def test_a_closing_sale_at_nothing_is_taken_as_it_always_was() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={PUT_495: flat("0.01", 5, 61), PUT_500: flat("2.00", 5, 61)},
    )

    outcome = replay(setup(leg("long", "put"), leg("short", "put", side="sell"), slippage="0.3"), market)

    # Opening: the 495 is bought at 0.01 x 1.003 up to 0.02 (2.00), the 500 is sold at 2.00 x 0.997 = 1.994 down to 1.99
    # (199.00). Closing: the 500 is bought back at 2.00 x 1.003 = 2.006 up to 2.01 (201.00), then the 495 is sold at
    # 0.01 x 0.997 = 0.00997 down to 0.00, which is taken.
    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("0.01"), D("0.02"), "exit_time", 61, D("0.01"), D("0.00")),
        ("short", 0, 6, D("2.00"), D("1.99"), "exit_time", 61, D("2.00"), D("2.01")),
    ]
    assert [trade.pnl_usd for trade in outcome.trades] == [D("-2.00"), D("-2.00")]
    assert outcome.cash_change_usd == D("-4.00")  # -2.00 + 199.00 - 201.00 + 0.00


def test_a_buy_back_at_nothing_is_taken_as_any_other_close_is() -> None:
    # A stored bar never prints at nothing; the table does not check, so the rule can be seen: only a sale to open that
    # would fill at nothing is not taken, and a buy-back at nothing is bought back as any other.
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={PUT_495: flat("1.00", 5, 61), PUT_500: {**flat("2.20", 5, 60), 61: ("0.00", "0.00")}},
    )

    outcome = replay(
        setup(leg("long", "put"), leg("short", "put", side="sell")), market, fee_for=fee_per_contract("0.65")
    )

    short = outcome.trades[1]
    assert (short.leg_id, short.exit.side, short.exit_price, short.exit_fee_usd) == (
        "short",
        "buy",
        D("0.00"),
        D("0.65"),
    )
    # Sold for 2.20, bought back for nothing: (2.20 - 0.00) x 100 - 0.65 - 0.65 = 218.70.
    assert short.pnl_usd == D("218.70")


def test_a_loss_with_no_limit_in_what_is_held_is_a_fault_that_stops_the_replay_and_is_not_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def a_book_that_found_a_loss_with_no_limit(*_args: object, **_kwargs: object) -> object:
        raise CoverInvariantBroken("what is held has a loss with no limit")

    monkeypatch.setattr(replay_module, "admit_opening", a_book_that_found_a_loss_with_no_limit)

    with pytest.raises(CoverInvariantBroken):
        replay(setup(leg("long", "put"), leg("short", "put", side="sell")), _credit_put_spread())


# ---- A buy-back with no bar ----------------------------------------------------------


def _buy_back_that_waits() -> TableMarket:
    """The sold 500 put traded until minute 60 and again at minute 63. The cover traded in every minute."""
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: flat("1.00", 5, 70),
            PUT_500: {**flat("2.20", 5, 60), 63: ("1.50", "1.50")},
        },
    )


def test_the_covers_sale_waits_while_the_sold_legs_buy_back_has_no_bar_and_then_both_fill_in_one_group() -> None:
    outcome = replay(setup(leg("long", "put"), leg("short", "put", side="sell")), _buy_back_that_waits())

    # Minutes 61 and 62: the 500 put did not trade, so it is still held and the 495 may not leave it bare.
    # Minute 63: the 500 put is bought back at 1.50 and then the 495 sold at 1.00. One exit group decided them both.
    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("1.00"), D("1.00"), "exit_time", 63, D("1.00"), D("1.00")),
        ("short", 0, 6, D("2.20"), D("2.20"), "exit_time", 63, D("1.50"), D("1.50")),
    ]
    assert len({trade.exit_group for trade in outcome.trades}) == 1
    assert outcome.status is SessionStatus.TRADED
    # Cash: -100.00 + 220.00 - 150.00 + 100.00 = +70.00.
    assert outcome.cash_change_usd == D("70.00")
    assert outcome.money_steps[1] == MoneyStep(
        at(63), FillGroup.CLOSING, D("10120.00"), D("10070.00"), D("500.00"), D("0"), D("0"), False
    )
    assert [minute_of(step.at_ms) for step in outcome.money_steps] == [6, 63]


def test_both_legs_are_marked_and_the_session_is_incomplete_when_the_buy_back_never_finds_a_bar() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            # The cover goes on trading to the close and ends at 0.90. The sold 500 put's last bar is minute 60, 2.40.
            PUT_495: {**flat("1.00", 5, 389), 390: ("1.00", "0.90")},
            PUT_500: {**flat("2.20", 5, 59), 60: ("2.20", "2.40")},
        },
    )

    outcome = replay(setup(leg("long", "put"), leg("short", "put", side="sell")), market)

    assert outcome.status is SessionStatus.INCOMPLETE
    assert [(trade.leg_id, trade.exit_reason, trade.exit_price) for trade in outcome.trades] == [
        ("long", ExitReason.MARKED_NOT_SOLD, D("0.90")),
        ("short", ExitReason.MARKED_NOT_BOUGHT_BACK, D("2.40")),
    ]
    assert [minute_of(trade.exit.bar_close_ms) for trade in outcome.trades] == [390, 60]
    # Long: (0.90 - 1.00) x 100 = -10.00. Short, sold 2.20 and owed 2.40: (2.20 - 2.40) x 100 = -20.00.
    assert [trade.pnl_usd for trade in outcome.trades] == [D("-10.00"), D("-20.00")]
    # Cash that moved: -100.00 + 220.00 = +120.00. Marked, signed: +90.00 for the bought leg and -240.00 owed.
    assert outcome.cash_change_usd == D("120.00")
    assert outcome.marked_value_usd == D("-150.00")
    assert outcome.pnl_usd == D("-30.00") == outcome.cash_change_usd + outcome.marked_value_usd
    marks = [(event.kind, event.leg_id, event.price) for event in outcome.events if event.at_ms == at(390)]
    assert marks == [
        (EventKind.MARKED_NOT_SOLD, "long", D("0.90")),
        (EventKind.MARKED_NOT_BOUGHT_BACK, "short", D("2.40")),
    ]


def test_a_buy_back_that_fills_while_the_cover_is_still_held_is_a_step_that_says_so() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        # The cover has no bar at 61 and the sold put does, so the buy-back fills alone and the cover stays held.
        options={
            PUT_495: {**flat("1.00", 5, 60), **flat("1.00", 62, 70)},
            PUT_500: flat("2.20", 5, 70),
        },
    )

    outcome = replay(setup(leg("long", "put"), leg("short", "put", side="sell")), market)

    first, second, third = outcome.money_steps
    assert (minute_of(second.at_ms), second.group, second.bought_back_with_cover_held) == (
        61,
        FillGroup.CLOSING,
        True,
    )
    # Bought back at 2.20 (220.00): cash 10120.00 -> 9900.00, and the collateral falls from 500.00 to nothing since the
    # bought put alone has none.
    assert (second.cash_before_usd, second.cash_after_usd) == (D("10120.00"), D("9900.00"))
    assert (second.collateral_before_usd, second.collateral_after_usd) == (D("500.00"), D("0"))
    # The cover is sold the minute after, when it traded: a group that bought nothing back.
    assert (minute_of(third.at_ms), third.bought_back_with_cover_held) == (62, False)
    assert first.group is FillGroup.OPENING


# ---- Every refusal the book gives -----------------------------------------------------


@pytest.mark.parametrize("refusal", list(OpeningRefusal))
def test_every_refusal_the_book_gives_is_told_in_a_word_of_the_replays_own_that_has_an_event(
    refusal: OpeningRefusal,
) -> None:
    told = entry_refusal(refusal)

    assert told in ENTRY_REFUSAL_EVENTS
    if refusal is OpeningRefusal.NO_COVER:
        assert told is EntryRefusal.COVER_NOT_YET_HELD
        assert entry_refusal(refusal, cover_did_not_trade=True) is EntryRefusal.COVER_DID_NOT_TRADE
    else:
        assert told.value == refusal.value


def test_a_refusal_the_book_may_gain_that_the_replay_has_no_word_for_raises_and_is_never_guessed() -> None:
    class FromTheFuture(StrEnum):
        NEW = "a_refusal_the_replay_has_no_word_for"

    with pytest.raises(ValueError, match="a_refusal_the_replay_has_no_word_for"):
        entry_refusal(FromTheFuture.NEW)  # type: ignore[arg-type]


def test_every_way_an_entry_is_refused_has_an_event_and_every_event_has_a_way() -> None:
    assert set(ENTRY_REFUSAL_EVENTS) == set(EntryRefusal)


def _refused_by(market: TableMarket, *legs: dict[str, Any], cash: str = "10000") -> list[Event]:
    outcome = replay(setup(*legs), market, cash=cash)
    return [event for event in outcome.events if event.kind in set(ENTRY_REFUSAL_EVENTS.values())]


def _the_scene_of(refusal: OpeningRefusal) -> tuple[list[Event], str]:
    """A replay in which the book gives ``refusal``, and the word the replay is to tell it in."""
    two_legs_on_one_contract = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL_500), ("b", "500", CALL_500)),
        options={CALL_500: flat("2.00", 5, 61)},
    )
    match refusal:
        case OpeningRefusal.OTHER_SIDE:
            return _refused_by(two_legs_on_one_contract, leg("a", "call"), leg("b", "call", side="sell")), "other_side"
        case OpeningRefusal.SECOND_EXPIRY:
            market = TableMarket(
                underlying=steady("500"),
                picks=picked(("a", "500", CALL_505), ("b", "500", CALL_500_LATER)),
                options={CALL_505: flat("1.00", 5, 61), CALL_500_LATER: flat("2.00", 5, 61)},
            )
            return _refused_by(market, leg("a", "call"), leg("b", "call", side="sell")), "second_expiry"
        case OpeningRefusal.NO_COVER:
            market = TableMarket(
                underlying=steady("500"),
                picks=picked(("a", "500", CALL_505), ("b", "500", CALL_500)),
                options={CALL_505: flat("1.00", 5, 61), CALL_500: flat("2.00", 5, 61)},
            )
            written = (leg("a", "call", momentum=("percent_up", "25")), leg("b", "call", side="sell"))
            return _refused_by(market, *written), "cover_not_yet_held"
        case OpeningRefusal.CASH_SHORT:
            written = (leg("long", "put"), leg("short", "put", side="sell"))
            return _refused_by(_wide_put_spread(), *written, cash="900"), "cash_short"
    raise AssertionError(f"{refusal} has no scene: a refusal the book gains needs one here")


@pytest.mark.parametrize("refusal", list(OpeningRefusal))
def test_every_refusal_the_book_gives_is_met_in_a_replay_as_an_event_with_the_word_it_is_told_in(
    refusal: OpeningRefusal,
) -> None:
    events, word = _the_scene_of(refusal)

    assert word in {event.reason for event in events}
    assert {ENTRY_REFUSAL_EVENTS[EntryRefusal(event.reason)] for event in events if event.reason} == {
        event.kind for event in events
    }


# ---- A setup that buys only takes one path, and a setup that sells takes the other ---------------------------


@dataclass(frozen=True)
class _BoughtOnly:
    """A scene of bought legs: each leg's contract, each contract's bars, the cash, the fee and the slippage."""

    legs: tuple[dict[str, Any], ...]
    contracts: dict[str, OptionContract]
    bars: dict[OptionContract, dict[int, tuple[str, ...]]]
    reaches: EventKind
    cash: str = "10000"
    fee: str = "0"
    slippage: str = "0"


_NEVER_TRADED = contract("call", "520")

_BOUGHT_ONLY_SCENES: dict[str, _BoughtOnly] = {
    "momentum with fees and slippage": _BoughtOnly(
        legs=(leg("a", "call", momentum=("percent_up", "25")),),
        contracts={"a": CALL_500},
        bars={CALL_500: {**flat("2.00", 5, 7), 8: ("2.00", "2.60"), **flat("2.60", 9, 70)}},
        reaches=EventKind.MOMENTUM_HIT,
        fee="0.65",
        slippage="0.3",
    ),
    "a bar missing where an order fills, for a leg that buys at once and one that waits on momentum": _BoughtOnly(
        legs=(leg("a", "call"), leg("b", "put", momentum=("percent_up", "10"))),
        contracts={"a": CALL_500, "b": PUT_500},
        bars={
            CALL_500: {5: ("2.00", "2.00"), **flat("2.00", 7, 70)},
            # The level is 3.00 x 1.1 = 3.30: decided at 8, dropped at 9 for want of a bar, decided again at 10.
            PUT_500: {**flat("3.00", 5, 7), 8: ("3.00", "3.40"), 10: ("3.40", "3.50"), **flat("3.50", 11, 70)},
        },
        reaches=EventKind.ENTRY_DROPPED,
    ),
    "two entries that together are more than the cash": _BoughtOnly(
        legs=(leg("a", "call"), leg("b", "put")),
        contracts={"a": CALL_500, "b": PUT_500},
        bars={CALL_500: flat("2.00", 5, 70), PUT_500: flat("3.00", 5, 70)},
        reaches=EventKind.ENTRY_REFUSED_FOR_CASH,
        cash="450",
    ),
    "a sale that waits for a bar and a leg marked, not sold": _BoughtOnly(
        legs=(leg("a", "call", contracts=2), leg("b", "put")),
        contracts={"a": CALL_500, "b": PUT_500},
        bars={
            CALL_500: {**flat("2.00", 5, 60), **flat("2.40", 63, 70)},
            PUT_500: {**flat("3.00", 5, 57), 58: ("3.00", "2.75")},
        },
        reaches=EventKind.MARKED_NOT_SOLD,
        fee="0.65",
    ),
    "three legs that cost exactly the cash with their fees": _BoughtOnly(
        legs=(leg("a", "call", contracts=2), leg("b", "put"), leg("c", "call", contracts=3)),
        contracts={"a": CALL_500, "b": PUT_500, "c": CALL_505},
        bars={CALL_500: flat("2.00", 5, 70), PUT_500: flat("3.00", 5, 70), CALL_505: flat("1.00", 5, 70)},
        # 2 x 2.00 x 100 + 3.00 x 100 + 3 x 1.00 x 100 = 1000.00, and six contracts' fees of 0.65 are 3.90.
        reaches=EventKind.ENTRY_FILLED,
        cash="1003.90",
        fee="0.65",
    ),
    "three legs that cost a cent more than the cash with their fees": _BoughtOnly(
        legs=(leg("a", "call", contracts=2), leg("b", "put"), leg("c", "call", contracts=3)),
        contracts={"a": CALL_500, "b": PUT_500, "c": CALL_505},
        bars={CALL_500: flat("2.00", 5, 70), PUT_500: flat("3.00", 5, 70), CALL_505: flat("1.00", 5, 70)},
        reaches=EventKind.ENTRY_REFUSED_FOR_CASH,
        cash="1003.89",
        fee="0.65",
    ),
}


def _market_of(scene: _BoughtOnly, *, with_a_sale_that_never_trades: bool) -> TableMarket:
    picks = [(leg_id, "500", each) for leg_id, each in scene.contracts.items()]
    if with_a_sale_that_never_trades:
        picks.append(("ghost", "500", _NEVER_TRADED))
    return TableMarket(underlying=steady("500"), picks=picked(*picks), options=scene.bars)


@pytest.mark.parametrize("scene", _BOUGHT_ONLY_SCENES.values(), ids=list(_BOUGHT_ONLY_SCENES))
def test_a_setup_that_buys_only_replays_alike_through_the_cover_book_and_without_it(
    scene: _BoughtOnly, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A setup that sells nothing never asks the cover book, so that a bought option of any multiplier replays as it
    # always did. That is a second path through the replay. Adding a sold leg whose contract never trades sends the same
    # scene through the book without changing anything it does: the two must agree on everything but the sold leg's own
    # events (it is picked, and then dropped for want of a bar).
    asked = 0
    real_admission = replay_module.admit_opening

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal asked
        asked += 1
        return real_admission(*args, **kwargs)

    monkeypatch.setattr(replay_module, "admit_opening", counting)
    fee = fee_per_contract(scene.fee)

    plain = replay(
        setup(*scene.legs, slippage=scene.slippage),
        _market_of(scene, with_a_sale_that_never_trades=False),
        cash=scene.cash,
        fee_for=fee,
    )
    assert asked == 0
    through_the_book = replay(
        setup(*scene.legs, leg("ghost", "call", side="sell"), slippage=scene.slippage),
        _market_of(scene, with_a_sale_that_never_trades=True),
        cash=scene.cash,
        fee_for=fee,
    )
    assert asked > 0

    assert scene.reaches in {event.kind for event in plain.events}, "the scene must reach what it is there for"
    assert [(event.kind, event.at_ms) for event in through_the_book.events if event.leg_id == "ghost"] == [
        (EventKind.PICKED, at(5)),
        (EventKind.ENTRY_DROPPED, at(6)),
    ]
    assert through_the_book.trades == plain.trades
    assert [event for event in through_the_book.events if event.leg_id != "ghost"] == list(plain.events)
    assert through_the_book.status is plain.status
    assert through_the_book.skip_reason == plain.skip_reason
    assert through_the_book.cash_change_usd == plain.cash_change_usd
    assert (through_the_book.marked_value_usd, through_the_book.pnl_usd) == (plain.marked_value_usd, plain.pnl_usd)
    assert through_the_book.money_steps == plain.money_steps
