"""Unit tests for ``IbkrEquityCommissionModel``."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.research.parity.ibkr_commission import IbkrEquityCommissionModel


@pytest.fixture
def model() -> IbkrEquityCommissionModel:
    return IbkrEquityCommissionModel()


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


def test_custom_rates_are_honored() -> None:
    model = IbkrEquityCommissionModel(
        per_share=Decimal("0.01"),
        min_per_order=Decimal("2.00"),
        max_pct_of_value=Decimal("0.01"),
    )
    # 100 * 0.01 = $1.00; floored to $2.00 min; cap = 0.01 * 100 * $150 = $150.
    assert model.fee(quantity=100, fill_price=Decimal("150.00")) == Decimal("2.00")


def test_aapl_365_shares_uses_per_share_rate() -> None:
    # 365 AAPL shares @ ~$270: per-share = 365 * 0.005 = $1.825 → rounds HALF_UP to $1.83;
    # floor $1.00 and cap ~$492.75 do not bind.
    model = IbkrEquityCommissionModel()
    assert model.fee(quantity=365, fill_price=Decimal("270.00")) == Decimal("1.83")
