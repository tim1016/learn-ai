"""Regression tests: existing prediction-free specs still run unchanged
under the schema 1.0 -> 1.1 ledger and the new EvalContext.predictions
field. No prediction artifacts loaded, no coverage check, no spec
validator surprises.
"""
from __future__ import annotations


def test_existing_sma_crossover_spec_round_trips() -> None:
    """The shipped fixture spec still validates after schema additions."""
    from pathlib import Path

    from app.engine.strategy.spec import load_spec_from_path

    fixtures = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "app"
        / "engine"
        / "strategy"
        / "spec"
        / "fixtures"
    )
    spec = load_spec_from_path(fixtures / "spy_ema_crossover.spec.json")
    assert spec.predictions == []


def test_legacy_1_0_ledger_loads() -> None:
    """A pre-existing artifacts/runs/<id>/ledger.json without
    prediction_set_hash must continue to load."""
    from app.research.runs.ledger import RunLedger

    legacy = {
        "schema_version": "1.0",
        "run_id": "old",
        "strategy_spec_id": "x",
        "strategy_spec_hash": "0" * 64,
        "strategy_spec_json": {},
        "engine_git_commit": "abc",
        "symbol": "SPY",
        "resolution_minutes": 15,
        "start_ms": 0,
        "end_ms": 1,
        "initial_cash": 100_000.0,
        "fill_mode": "signal_bar_close",
        "commission_per_order": 0.0,
        "slippage_per_share": 0.0,
        "random_seed": 0,
        "data_snapshot_id": "snap",
    }
    ledger = RunLedger.model_validate(legacy)
    assert ledger.prediction_set_hash is None
    assert ledger.schema_version == "1.0"
