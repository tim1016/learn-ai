"""The price and money arithmetic of a replay, worked by hand and compared exactly."""

from __future__ import annotations

from decimal import Context, Decimal, localcontext

import pytest

from app.options_backtest import fills
from app.options_backtest.fills import (
    entry_cost,
    exit_proceeds,
    momentum_level,
    momentum_reached,
    order_value,
    stop_hit,
    stop_level,
    trade_pnl,
)
from app.options_backtest.setup import Momentum, StopLoss

D = Decimal
buy = fills.buy_fill_price
sell = fills.sell_fill_price
_CENT = D("0.01")


def test_a_buy_at_the_default_slippage_rounds_up_to_the_cent() -> None:
    # 2.57 * 1.003 = 2.57771, and the next cent up is 2.58.
    assert buy(D("2.57"), slippage_percent=D("0.3"), price_step_usd=_CENT) == D("2.58")


def test_a_sale_at_the_default_slippage_rounds_down_to_the_cent() -> None:
    # 1.87 * 0.997 = 1.86439, and the cent below is 1.86.
    assert sell(D("1.87"), slippage_percent=D("0.3"), price_step_usd=_CENT) == D("1.86")


def test_a_price_already_on_the_step_after_slippage_is_not_moved_a_further_step() -> None:
    # 2.50 * 1.004 = 2.51 exactly; 1.99 = 2.00 * 0.995 exactly.
    assert buy(D("2.50"), slippage_percent=D("0.4"), price_step_usd=_CENT) == D("2.51")
    assert sell(D("2.00"), slippage_percent=D("0.5"), price_step_usd=_CENT) == D("1.99")


def test_no_slippage_fills_at_the_price_seen() -> None:
    assert buy(D("2.57"), slippage_percent=D("0"), price_step_usd=_CENT) == D("2.57")
    assert sell(D("2.57"), slippage_percent=D("0"), price_step_usd=_CENT) == D("2.57")


def test_the_smallest_slippage_still_costs_the_buyer_a_full_step() -> None:
    # 1.00 * 1.0001 = 1.0001, which is not a whole cent, so the buyer pays 1.01.
    assert buy(D("1.00"), slippage_percent=D("0.01"), price_step_usd=_CENT) == D("1.01")


def test_a_sale_that_rounds_to_nothing_sells_for_zero_not_less() -> None:
    # 0.01 * 0.997 = 0.00997, which is under one cent.
    assert sell(D("0.01"), slippage_percent=D("0.3"), price_step_usd=_CENT) == D("0")
    assert sell(D("0"), slippage_percent=D("0.3"), price_step_usd=_CENT) == D("0")


def test_a_wider_step_rounds_against_the_trader_to_that_step() -> None:
    nickel = D("0.05")
    # 2.00 * 1.003 = 2.006 -> 2.05. 1.87 * 0.997 = 1.86439 -> 37 nickels = 1.85.
    assert buy(D("2.00"), slippage_percent=D("0.3"), price_step_usd=nickel) == D("2.05")
    assert sell(D("1.87"), slippage_percent=D("0.3"), price_step_usd=nickel) == D("1.85")


def test_a_step_that_does_not_divide_a_dollar_is_rounded_exactly() -> None:
    three_cents = D("0.03")
    # 0.30 is ten steps exactly; 0.31 is 10.33 steps, so 11 steps up = 0.33 and 10 steps down = 0.30.
    assert buy(D("0.30"), slippage_percent=D("0"), price_step_usd=three_cents) == D("0.30")
    assert buy(D("0.31"), slippage_percent=D("0"), price_step_usd=three_cents) == D("0.33")
    assert sell(D("0.31"), slippage_percent=D("0"), price_step_usd=three_cents) == D("0.30")


def test_the_fill_does_not_depend_on_the_ambient_decimal_precision() -> None:
    with localcontext(Context(prec=4)):
        bought = buy(D("2.57"), slippage_percent=D("0.3"), price_step_usd=_CENT)
        sold = sell(D("1.87"), slippage_percent=D("0.3"), price_step_usd=_CENT)

    assert (bought, sold) == (D("2.58"), D("1.86"))


def test_a_negative_price_is_refused_not_sold_for_less_than_nothing() -> None:
    with pytest.raises(ValueError, match="negative"):
        sell(D("-0.01"), slippage_percent=D("0.3"), price_step_usd=_CENT)
    with pytest.raises(ValueError, match="negative"):
        buy(D("-0.01"), slippage_percent=D("0.3"), price_step_usd=_CENT)


def test_an_order_is_valued_at_its_price_times_the_multiplier_times_the_contracts() -> None:
    # 2.58 * 100 * 2 = 516.00
    assert order_value(D("2.58"), multiplier=100, contracts=2) == D("516.00")


def test_an_entry_costs_its_value_plus_its_fee_and_an_exit_brings_in_its_value_less_its_fee() -> None:
    # 516.00 + 1.30 = 517.30; 1.86 * 100 * 2 = 372.00, less 1.30 = 370.70.
    assert entry_cost(D("2.58"), multiplier=100, contracts=2, fee=D("1.30")) == D("517.30")
    assert exit_proceeds(D("1.86"), multiplier=100, contracts=2, fee=D("1.30")) == D("370.70")


def test_a_trades_profit_is_the_price_move_less_both_fees_and_equals_the_cash_it_moved() -> None:
    # (1.86 - 2.58) * 100 * 2 = -144.00, less 1.30 twice = -146.60.
    pnl = trade_pnl(
        bought=D("2.58"), sold=D("1.86"), multiplier=100, contracts=2, entry_fee=D("1.30"), exit_fee=D("1.30")
    )

    assert pnl == D("-146.60")
    # -517.30 out, +370.70 back.
    assert pnl == -D("517.30") + D("370.70")


def test_a_percent_stop_sits_below_the_buy_fill_and_is_not_rounded() -> None:
    # 2.58 * (1 - 25/100) = 2.58 * 0.75 = 1.935
    level = stop_level(StopLoss(kind="percent", value=D("25")), bought=D("2.58"))

    assert level == D("1.935")


def test_a_points_stop_sits_that_many_points_below_the_buy_fill() -> None:
    # 2.58 - 0.50
    assert stop_level(StopLoss(kind="points", value=D("0.50")), bought=D("2.58")) == D("2.08")


def test_a_stop_is_hit_by_a_close_at_or_below_its_level_and_not_by_one_above() -> None:
    level = D("1.935")

    assert stop_hit(level, close=D("1.94")) is False
    assert stop_hit(level, close=D("1.935")) is True
    assert stop_hit(level, close=D("1.93")) is True


@pytest.mark.parametrize(
    ("kind", "value", "level"),
    [
        ("percent_up", "25", "2.50"),  # 2.00 * 1.25
        ("percent_down", "25", "1.50"),  # 2.00 * 0.75
        ("points_up", "0.30", "2.30"),  # 2.00 + 0.30
        ("points_down", "0.30", "1.70"),  # 2.00 - 0.30
    ],
)
def test_a_momentum_level_is_the_starting_price_moved_by_the_rule(kind: str, value: str, level: str) -> None:
    rule = Momentum(kind=kind, value=D(value))  # type: ignore[arg-type]

    assert momentum_level(rule, starting_price=D("2.00")) == D(level)


@pytest.mark.parametrize(
    ("kind", "level", "just_short", "exactly", "beyond"),
    [
        ("percent_up", "2.50", "2.49", "2.50", "2.51"),
        ("points_up", "2.30", "2.29", "2.30", "2.31"),
        ("percent_down", "1.50", "1.51", "1.50", "1.49"),
        ("points_down", "1.70", "1.71", "1.70", "1.69"),
    ],
)
def test_momentum_is_reached_at_the_level_and_beyond_it_but_not_just_short(
    kind: str, level: str, just_short: str, exactly: str, beyond: str
) -> None:
    rule = Momentum(kind=kind, value=D("1"))  # type: ignore[arg-type]

    assert momentum_reached(rule, level=D(level), close=D(just_short)) is False
    assert momentum_reached(rule, level=D(level), close=D(exactly)) is True
    assert momentum_reached(rule, level=D(level), close=D(beyond)) is True
