"""The cross-engine matrix needs FillModel + LeanSetHoldingsSizing both
wired with IbkrEquityCommissionModel — otherwise the engine charges
flat $1 fees while LEAN charges per-share, and Gate 3 fails on
COMMISSION_DRIFT for every fill.

This test enforces the wiring at the source-code level (cheap, no LEAN
container required). End-to-end coverage lives in test_cross_engine_study.py
post-regeneration."""

from __future__ import annotations

from pathlib import Path


def test_cross_runner_constructs_backtest_engine_with_ibkr_fee_model() -> None:
    src = (Path(__file__).resolve().parents[2] / "app" / "lean_sidecar" / "cross_runner.py").read_text(encoding="utf-8")
    # FillModel must be explicitly constructed with the IBKR fee model.
    assert "IbkrEquityCommissionModel" in src
    assert "FillModel(fee_model=IbkrEquityCommissionModel())" in src
    # Sizing model must also receive the IBKR fee model.
    assert "LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())" in src
