"""The engine.execution.commission re-export is the canonical engine-side
seam onto the IBKR fee model — keep it byte-equivalent to the research-side
canonical so a single fixture proves both paths."""

from __future__ import annotations

from decimal import Decimal

from app.engine.execution.commission import IbkrEquityCommissionModel as EngineModel
from app.research.parity.ibkr_commission import IbkrEquityCommissionModel as CanonicalModel


def test_engine_reexport_is_the_canonical_class() -> None:
    assert EngineModel is CanonicalModel


def test_engine_reexport_produces_canonical_fees() -> None:
    em, cm = EngineModel(), CanonicalModel()
    for qty, price in [(100, Decimal("150.00")), (365, Decimal("270.00")), (221, Decimal("450.00"))]:
        assert em.fee(quantity=qty, fill_price=price) == cm.fee(quantity=qty, fill_price=price)
