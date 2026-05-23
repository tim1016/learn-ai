"""Unit tests for ``IbkrEquityCommissionModel``."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.research.parity.ibkr_commission import IbkrEquityCommissionModel


@pytest.fixture
def model() -> IbkrEquityCommissionModel:
    return IbkrEquityCommissionModel()


def test_small_order_hits_per_order_minimum(model: IbkrEquityCommissionModel) -> None:
    # 100 shares * $0.005 = $0.50, floored to the $1.00 minimum.
    assert model.fee(quantity=100, fill_price=Decimal("150.00")) == Decimal("1.00")


def test_large_order_uses_per_share_rate(model: IbkrEquityCommissionModel) -> None:
    # 1_000 shares * $0.005 = $5.00; 0.5% of $150_000 = $750 cap (no bite).
    assert model.fee(quantity=1_000, fill_price=Decimal("150.00")) == Decimal("5.00")


def test_low_price_high_quantity_caps_at_half_percent(
    model: IbkrEquityCommissionModel,
) -> None:
    # 500 shares * $0.005 = $2.50; cap = 0.5% * 500 * $0.10 = $0.25 < $1.00 floor < $2.50 raw.
    # The cap dominates → $0.25.
    assert model.fee(quantity=500, fill_price=Decimal("0.10")) == Decimal("0.25")


def test_negative_quantity_treated_as_absolute(model: IbkrEquityCommissionModel) -> None:
    # Sell side: |quantity| drives the fee.
    assert model.fee(quantity=-1_000, fill_price=Decimal("150.00")) == Decimal("5.00")


def test_zero_quantity_yields_zero_fee(model: IbkrEquityCommissionModel) -> None:
    assert model.fee(quantity=0, fill_price=Decimal("150.00")) == Decimal("0.00")


def test_zero_price_yields_zero_fee(model: IbkrEquityCommissionModel) -> None:
    assert model.fee(quantity=100, fill_price=Decimal("0")) == Decimal("0.00")


def test_aapl_phase3_representative_fill() -> None:
    # Representative: ~526 shares @ ~$190 (≈ $100k of AAPL at start of window).
    # Raw per-share = 526 * 0.005 = $2.63 (above $1 floor, under 0.5% cap = ~$500).
    model = IbkrEquityCommissionModel()
    assert model.fee(quantity=526, fill_price=Decimal("190.00")) == Decimal("2.63")


def test_custom_rates_are_honored() -> None:
    model = IbkrEquityCommissionModel(
        per_share=Decimal("0.01"),
        min_per_order=Decimal("2.00"),
        max_pct_of_value=Decimal("0.01"),
    )
    # 100 * 0.01 = $1.00; floored to $2.00 min; cap = 0.01 * 100 * $150 = $150.
    assert model.fee(quantity=100, fill_price=Decimal("150.00")) == Decimal("2.00")


def test_spy_150_shares_hits_floor() -> None:
    # 150 shares @ $662.50: per-share = 150 * 0.005 = $0.75 → floored to $1.00 min;
    # cap = 0.5% * 150 * $662.50 = $496.88 (no bite). The minimum dominates.
    model = IbkrEquityCommissionModel()
    assert model.fee(quantity=150, fill_price=Decimal("662.50")) == Decimal("1.00")


def test_aapl_365_shares_uses_per_share_rate() -> None:
    # 365 AAPL shares @ ~$270: per-share = 365 * 0.005 = $1.825 → rounds HALF_UP to $1.83;
    # floor $1.00 and cap ~$492.75 do not bind.
    model = IbkrEquityCommissionModel()
    assert model.fee(quantity=365, fill_price=Decimal("270.00")) == Decimal("1.83")


def test_tsla_221_shares_uses_per_share_rate() -> None:
    # 221 TSLA shares @ ~$450: per-share = 221 * 0.005 = $1.105 → rounds HALF_UP to $1.11;
    # floor $1.00 and cap ~$497.25 do not bind.
    model = IbkrEquityCommissionModel()
    assert model.fee(quantity=221, fill_price=Decimal("450.00")) == Decimal("1.11")
