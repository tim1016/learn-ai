"""Marketable-limit anchor: the decision bar's close moved by the allowance in the trade's direction."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.alpaca.config import AlpacaSettings
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances, marketable_limit_price
from app.broker.contract.models import OrderSide


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
        # A buy that crosses the $1 band rounds on the band it lands in.
        (OrderSide.BUY, Decimal("0.9999"), Decimal("5"), Decimal("1.00")),
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
