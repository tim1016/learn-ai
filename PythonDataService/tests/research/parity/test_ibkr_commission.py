"""Unit tests for ``IbkrEquityCommissionModel``."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.research.parity.ibkr_commission import IbkrEquityCommissionModel


@pytest.fixture
def model() -> IbkrEquityCommissionModel:
    return IbkrEquityCommissionModel()


@pytest.mark.parametrize(
    ("quantity", "fill_price", "expected"),
    [
        # 500 shares * $0.005 = $2.50; cap = 0.5% * 500 * $0.10 = $0.25 < $1.00 floor < $2.50 raw.
        # The cap dominates → $0.25.
        pytest.param(500, "0.10", "0.25", id="low-price-high-quantity-caps-at-half-percent"),
        # Sell side: |quantity| drives the fee.
        pytest.param(-1_000, "150.00", "5.00", id="negative-quantity-treated-as-absolute"),
        pytest.param(0, "150.00", "0.00", id="zero-quantity-yields-zero-fee"),
        pytest.param(100, "0", "0.00", id="zero-price-yields-zero-fee"),
        # 365 AAPL shares @ ~$270: per-share = 365 * 0.005 = $1.825 → rounds HALF_UP to $1.83;
        # floor $1.00 and cap ~$492.75 do not bind.
        pytest.param(365, "270.00", "1.83", id="aapl-365-shares-uses-per-share-rate"),
    ],
)
def test_default_tier_fee(model: IbkrEquityCommissionModel, quantity: int, fill_price: str, expected: str) -> None:
    assert model.fee(quantity=quantity, fill_price=Decimal(fill_price)) == Decimal(expected)


def test_custom_rates_are_honored() -> None:
    model = IbkrEquityCommissionModel(
        per_share=Decimal("0.01"),
        min_per_order=Decimal("2.00"),
        max_pct_of_value=Decimal("0.01"),
    )
    # 100 * 0.01 = $1.00; floored to $2.00 min; cap = 0.01 * 100 * $150 = $150.
    assert model.fee(quantity=100, fill_price=Decimal("150.00")) == Decimal("2.00")
