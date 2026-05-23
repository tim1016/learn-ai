"""Light contract test that the FillModel fee seam is the single source of
truth the engine consults. Heavier integration coverage (full backtest
under IBKR fees) lives in test_cross_engine_study.py after cells are
regenerated."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.fill_model import FillModel


@pytest.mark.parametrize(
    "qty,price,expected",
    [
        (150, Decimal("662.50"), Decimal("1.00")),
        (365, Decimal("270.00"), Decimal("1.83")),
        (221, Decimal("450.00"), Decimal("1.11")),
    ],
)
def test_engine_fee_seam_matches_canonical(qty: int, price: Decimal, expected: Decimal) -> None:
    fm = FillModel(fee_model=IbkrEquityCommissionModel())
    assert fm.compute_fee(quantity=qty, fill_price=price) == expected


def test_legacy_field_preserved_for_default_fillmodel() -> None:
    """When fee_model is None, compute_fee MUST return commission_per_order
    byte-identically — this is the contract that keeps the historical SPY
    parity fixtures green."""
    fm = FillModel()
    assert fm.commission_per_order == Decimal("1.00")
    assert fm.compute_fee(quantity=999, fill_price=Decimal("123.45")) == Decimal("1.00")
