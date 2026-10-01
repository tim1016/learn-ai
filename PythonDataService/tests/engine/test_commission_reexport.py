"""The engine.execution.commission re-export is the canonical engine-side
seam onto the IBKR fee model — keep it byte-equivalent to the research-side
canonical so a single fixture proves both paths."""

from __future__ import annotations

from app.engine.execution.commission import IbkrEquityCommissionModel as EngineModel
from app.research.parity.ibkr_commission import IbkrEquityCommissionModel as CanonicalModel


def test_engine_reexport_is_the_canonical_class() -> None:
    assert EngineModel is CanonicalModel
