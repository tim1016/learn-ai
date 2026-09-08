"""Marketable-limit anchor: the decision bar's close moved by the allowance in the trade's direction."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.broker.alpaca.config import AlpacaSettings
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances, marketable_limit_price
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType, TimeInForce


@pytest.mark.parametrize(
    ("side", "close", "bps", "expected"),
    [
        # 10 bps on a $100 close: buy up to 100.10, sell down to 99.90 — exact.
        (OrderSide.BUY, Decimal("100.00"), Decimal("10"), Decimal("100.10")),
        (OrderSide.SELL, Decimal("100.00"), Decimal("10"), Decimal("99.90")),
        # Rounding is in the marketable direction: 7 bps on 123.45 is 123.536415 → buy 123.54, sell 123.363585 → 123.36.
        (OrderSide.BUY, Decimal("123.45"), Decimal("7"), Decimal("123.54")),
        (OrderSide.SELL, Decimal("123.45"), Decimal("7"), Decimal("123.36")),
        # Sub-dollar prices allow four decimals (Alpaca tick rule).
        (OrderSide.BUY, Decimal("0.5000"), Decimal("10"), Decimal("0.5005")),
        (OrderSide.SELL, Decimal("0.5000"), Decimal("10"), Decimal("0.4995")),
        # A zero allowance anchors exactly at the close.
        (OrderSide.BUY, Decimal("55.55"), Decimal("0"), Decimal("55.55")),
        # A buy that crosses the $1 band rounds up on the band it lands in: 1.00039995 → 1.01.
        (OrderSide.BUY, Decimal("0.9999"), Decimal("5"), Decimal("1.01")),
    ],
)
def test_marketable_limit_price(side: OrderSide, close: Decimal, bps: Decimal, expected: Decimal) -> None:
    assert marketable_limit_price(side=side, close=close, allowance_bps=bps) == expected


def test_a_negative_allowance_is_refused() -> None:
    with pytest.raises(ValueError, match="allowance_bps"):
        marketable_limit_price(side=OrderSide.BUY, close=Decimal("10"), allowance_bps=Decimal("-1"))


def test_a_non_positive_close_is_refused() -> None:
    with pytest.raises(ValueError, match="close"):
        marketable_limit_price(side=OrderSide.BUY, close=Decimal("0"), allowance_bps=Decimal("1"))


def test_allowances_come_from_settings_and_are_absent_when_unset() -> None:
    both = AlpacaSettings(api_key_id="k", api_secret_key="s", live_xh_entry_bps=12.5, live_xh_exit_bps=8.0)
    neither = AlpacaSettings(api_key_id="k", api_secret_key="s")
    one = AlpacaSettings(api_key_id="k", api_secret_key="s", live_xh_entry_bps=12.5)

    assert ExtendedHoursAllowances.from_settings(both) == ExtendedHoursAllowances(
        entry_bps=Decimal("12.5"), exit_bps=Decimal("8.0")
    )
    assert ExtendedHoursAllowances.from_settings(neither) is None
    assert ExtendedHoursAllowances.from_settings(one) is None


def test_an_anchor_that_quantises_to_zero_is_refused() -> None:
    """A sell at 10 000 bps is a 100 % giveaway; the quantised anchor is 0.0000.

    ``BrokerOrderLeg`` rejects such a price too, but as a pydantic
    ``ValidationError`` raised from inside ``LegShape.apply`` — outside every
    ``except ProgramLegRefused`` its callers hold, so it escaped and killed the
    shielded effect task instead of writing a rejected receipt.
    """
    with pytest.raises(ValueError, match="not a submittable limit price"):
        marketable_limit_price(
            side=OrderSide.SELL, close=Decimal("100.00"), allowance_bps=Decimal("10000")
        )


def test_a_sub_penny_close_floored_to_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="not a submittable limit price"):
        marketable_limit_price(
            side=OrderSide.SELL, close=Decimal("0.00005"), allowance_bps=Decimal("10")
        )


@pytest.mark.parametrize("bps", ["10000", "10001", "50000"])
def test_an_allowance_of_a_hundred_percent_or_more_will_not_load(bps: str) -> None:
    """A 100 % allowance is not a price; the configuration refuses to load at all."""
    with pytest.raises(ValidationError, match="live_xh_exit_bps"):
        AlpacaSettings(api_key_id="k", api_secret_key="s", live_xh_exit_bps=float(bps))
    with pytest.raises(ValidationError, match="live_xh_entry_bps"):
        AlpacaSettings(api_key_id="k", api_secret_key="s", live_xh_entry_bps=float(bps))


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.parametrize(
    "close",
    [
        "0.9000", "0.9500", "0.9990", "0.9999", "0.99999",
        "1.0000", "1.0001", "1.0100", "1.5000", "2.0000", "100.00",
    ],
)
@pytest.mark.parametrize("bps", ["0", "5", "10", "50", "100"])
def test_every_anchor_across_the_dollar_band_is_a_valid_leg_limit_price(
    side: OrderSide, close: str, bps: str
) -> None:
    """Parity for the duplicated $1 tick rule (CLAUDE.md guiding philosophy #5).

    ``marketable_limit.py`` picks the tick from the *pre*-quantisation ``raw``;
    ``BrokerOrderLeg._limit_price_matches_order_type`` checks the *final*
    price. The two can only disagree at the band boundary — a sub-dollar close
    whose anchor lands at or above $1 — so the sweep straddles it, including
    the 0.99999 → 1.0000 case the formula row calls out.
    """
    price = marketable_limit_price(side=side, close=Decimal(close), allowance_bps=Decimal(bps))

    leg = BrokerOrderLeg(
        symbol="SPY",
        side=side,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price=float(price),
        time_in_force=TimeInForce.DAY,
    )

    assert leg.limit_price == float(price)
