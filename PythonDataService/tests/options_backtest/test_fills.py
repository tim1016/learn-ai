"""The price and money arithmetic of a replay, worked by hand and compared exactly."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Context, Decimal, localcontext

import pytest

from app.options_backtest import fills
from app.options_backtest.fills import (
    cash_effect,
    entry_cost,
    exit_proceeds,
    momentum_level,
    momentum_reached,
    order_value,
    sold_stop_hit,
    sold_stop_level,
    stop_hit,
    stop_level,
    trade_pnl,
)
from app.options_backtest.setup import Momentum, StopLoss

D = Decimal
buy = fills.buy_fill_price
sell = fills.sell_fill_price
_CENT = D("0.01")


@pytest.mark.parametrize(
    ("fill", "price", "slippage", "step", "expected"),
    [
        # 2.57 * 1.003 = 2.57771, and the next cent up is 2.58.
        pytest.param(buy, "2.57", "0.3", "0.01", "2.58", id="a_buy_at_the_default_slippage_rounds_up_to_the_cent"),
        # 1.87 * 0.997 = 1.86439, and the cent below is 1.86.
        pytest.param(sell, "1.87", "0.3", "0.01", "1.86", id="a_sale_at_the_default_slippage_rounds_down_to_the_cent"),
        # 2.50 * 1.004 = 2.51 exactly.
        pytest.param(
            buy, "2.50", "0.4", "0.01", "2.51", id="a_price_already_on_the_step_after_slippage_is_not_moved_a_further_step-buy"
        ),
        # 1.99 = 2.00 * 0.995 exactly.
        pytest.param(
            sell, "2.00", "0.5", "0.01", "1.99", id="a_price_already_on_the_step_after_slippage_is_not_moved_a_further_step-sell"
        ),
        pytest.param(buy, "2.57", "0", "0.01", "2.57", id="no_slippage_fills_at_the_price_seen-buy"),
        pytest.param(sell, "2.57", "0", "0.01", "2.57", id="no_slippage_fills_at_the_price_seen-sell"),
        # 1.00 * 1.0001 = 1.0001, which is not a whole cent, so the buyer pays 1.01.
        pytest.param(
            buy, "1.00", "0.01", "0.01", "1.01", id="the_smallest_slippage_still_costs_the_buyer_a_full_step"
        ),
        # 0.01 * 0.997 = 0.00997, which is under one cent.
        pytest.param(
            sell, "0.01", "0.3", "0.01", "0", id="a_sale_that_rounds_to_nothing_sells_for_zero_not_less-under_one_cent"
        ),
        pytest.param(
            sell, "0", "0.3", "0.01", "0", id="a_sale_that_rounds_to_nothing_sells_for_zero_not_less-a_price_of_zero"
        ),
        # 2.00 * 1.003 = 2.006 -> 2.05.
        pytest.param(buy, "2.00", "0.3", "0.05", "2.05", id="a_wider_step_rounds_against_the_trader_to_that_step-buy"),
        # 1.87 * 0.997 = 1.86439 -> 37 nickels = 1.85.
        pytest.param(sell, "1.87", "0.3", "0.05", "1.85", id="a_wider_step_rounds_against_the_trader_to_that_step-sell"),
        # 0.30 is ten steps exactly.
        pytest.param(
            buy, "0.30", "0", "0.03", "0.30", id="a_step_that_does_not_divide_a_dollar_is_rounded_exactly-ten_steps_exactly"
        ),
        # 0.31 is 10.33 steps, so 11 steps up = 0.33 ...
        pytest.param(
            buy, "0.31", "0", "0.03", "0.33", id="a_step_that_does_not_divide_a_dollar_is_rounded_exactly-eleven_steps_up"
        ),
        # ... and 10 steps down = 0.30.
        pytest.param(
            sell, "0.31", "0", "0.03", "0.30", id="a_step_that_does_not_divide_a_dollar_is_rounded_exactly-ten_steps_down"
        ),
    ],
)
def test_a_fill_is_moved_by_the_slippage_and_rounded_against_the_trader_to_the_step(
    fill: Callable[..., Decimal], price: str, slippage: str, step: str, expected: str
) -> None:
    assert fill(D(price), slippage_percent=D(slippage), price_step_usd=D(step)) == D(expected)


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


def test_an_order_is_valued_costed_and_cashed_by_its_price_multiplier_contracts_and_fee() -> None:
    # 2.58 * 100 * 2 = 516.00
    assert order_value(D("2.58"), multiplier=100, contracts=2) == D("516.00"), (
        "an order is valued at its price times the multiplier times the contracts"
    )
    # 516.00 + 1.30 = 517.30; 1.86 * 100 * 2 = 372.00, less 1.30 = 370.70.
    assert entry_cost(D("2.58"), multiplier=100, contracts=2, fee=D("1.30")) == D("517.30"), (
        "an entry costs its value plus its fee"
    )
    assert exit_proceeds(D("1.86"), multiplier=100, contracts=2, fee=D("1.30")) == D("370.70"), (
        "an exit brings in its value less its fee"
    )
    # A bought leg buys to open (-517.30) and sells to close (+370.70). A sold leg sells to open and buys to close.
    assert cash_effect("buy", D("2.58"), multiplier=100, contracts=2, fee=D("1.30")) == D("-517.30"), (
        "a buy moves its cost out of the cash"
    )
    assert cash_effect("sell", D("1.86"), multiplier=100, contracts=2, fee=D("1.30")) == D("370.70"), (
        "a sale moves its proceeds in, whichever order of a leg it is"
    )
    # A sale whose fee is more than its value takes cash out.
    assert cash_effect("sell", D("0.00"), multiplier=100, contracts=1, fee=D("0.65")) == D("-0.65"), (
        "a sale whose fee is more than its value takes cash out"
    )


def test_a_trades_profit_is_the_price_move_less_both_fees_and_equals_the_cash_it_moved() -> None:
    # A bought leg: (1.86 - 2.58) * 100 * 2 = -144.00, less 1.30 twice = -146.60.
    bought_leg_pnl = trade_pnl(
        bought=D("2.58"), sold=D("1.86"), multiplier=100, contracts=2, entry_fee=D("1.30"), exit_fee=D("1.30")
    )
    # A sold leg, sold at 2.20 and bought back at 1.50, 2 contracts, a fee of 1.30 each way:
    # (2.20 - 1.50) * 100 * 2 = 140.00, less 1.30 twice = 137.40.
    sold_leg_pnl = trade_pnl(
        bought=D("1.50"), sold=D("2.20"), multiplier=100, contracts=2, entry_fee=D("1.30"), exit_fee=D("1.30")
    )

    assert bought_leg_pnl == D("-146.60"), "a bought leg's profit is the price move less both fees"
    # -517.30 out, +370.70 back.
    assert bought_leg_pnl == -D("517.30") + D("370.70"), "a bought leg's profit equals the cash it moved"
    assert sold_leg_pnl == D("137.40"), "a sold leg's profit is its sale less its buy-back less both fees"
    # +438.70 in at the sale (440.00 less 1.30), -301.30 out at the buy-back (300.00 and 1.30).
    cash = cash_effect("sell", D("2.20"), multiplier=100, contracts=2, fee=D("1.30")) + cash_effect(
        "buy", D("1.50"), multiplier=100, contracts=2, fee=D("1.30")
    )
    assert sold_leg_pnl == cash == D("137.40"), "a sold leg's profit equals the cash it moved"


@pytest.mark.parametrize(
    ("kind", "value", "level"),
    [
        # 2.58 * (1 - 25/100) = 2.58 * 0.75 = 1.935
        pytest.param("percent", "25", "1.935", id="a_percent_stop_sits_below_the_buy_fill_and_is_not_rounded"),
        # 2.58 - 0.50
        pytest.param("points", "0.50", "2.08", id="a_points_stop_sits_that_many_points_below_the_buy_fill"),
    ],
)
def test_a_stop_sits_below_the_buy_fill_by_the_rule(kind: str, value: str, level: str) -> None:
    rule = StopLoss(kind=kind, value=D(value))  # type: ignore[arg-type]

    assert stop_level(rule, bought=D("2.58")) == D(level)


@pytest.mark.parametrize(
    ("hit", "level", "close", "expected"),
    [
        pytest.param(stop_hit, "1.935", "1.94", False, id="a_stop_is_not_hit_by_a_close_above_its_level"),
        pytest.param(stop_hit, "1.935", "1.935", True, id="a_stop_is_hit_by_a_close_at_its_level"),
        pytest.param(stop_hit, "1.935", "1.93", True, id="a_stop_is_hit_by_a_close_below_its_level"),
        pytest.param(sold_stop_hit, "2.75", "2.74", False, id="a_sold_legs_stop_is_not_hit_by_a_close_below_its_level"),
        pytest.param(sold_stop_hit, "2.75", "2.75", True, id="a_sold_legs_stop_is_hit_by_a_close_at_its_level"),
        pytest.param(sold_stop_hit, "2.75", "2.76", True, id="a_sold_legs_stop_is_hit_by_a_close_above_its_level"),
    ],
)
def test_a_stop_is_hit_by_a_close_at_its_level_or_beyond_it_and_not_by_one_short_of_it(
    hit: Callable[..., bool], level: str, close: str, expected: bool
) -> None:
    assert hit(D(level), close=D(close)) is expected


@pytest.mark.parametrize(
    ("kind", "value", "level"),
    [
        ("percent", "25", "2.75"),  # 2.20 * (1 + 25/100) = 2.20 * 1.25
        ("percent", "150", "5.50"),  # 2.20 * (1 + 150/100) = 2.20 * 2.5: no ceiling at 100
        ("percent", "12.5", "2.475"),  # 2.20 * 1.125, and a level is not rounded to the price step
        ("points", "0.50", "2.70"),  # 2.20 + 0.50
        ("points", "3", "5.20"),  # 2.20 + 3
    ],
)
def test_a_sold_legs_stop_sits_above_the_sale_price_by_the_rule_and_is_not_rounded(
    kind: str, value: str, level: str
) -> None:
    rule = StopLoss(kind=kind, value=D(value))  # type: ignore[arg-type]

    assert sold_stop_level(rule, sold=D("2.20")) == D(level)


def test_a_sold_legs_stop_is_the_bought_legs_stop_the_other_way_round() -> None:
    # The same premium and the same rule: a bought leg's level is the same distance below it as a sold leg's is above.
    rule = StopLoss(kind="points", value=D("0.50"))

    assert stop_level(rule, bought=D("2.00")) == D("1.50")
    assert sold_stop_level(rule, sold=D("2.00")) == D("2.50")


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
