"""Tests for the schema 1.0 -> 1.1 ledger bump."""
from __future__ import annotations

import pytest

from app.research.runs.ledger import RunLedger


def _base_ledger_kwargs() -> dict:
    return dict(
        run_id="r1",
        strategy_spec_id="x",
        strategy_spec_hash="0" * 64,
        strategy_spec_json={},
        engine_git_commit="abc",
        symbol="SPY",
        resolution_minutes=15,
        start_ms=0,
        end_ms=1,
        initial_cash=100_000.0,
        fill_mode="signal_bar_close",
        commission_per_order=0.0,
        slippage_per_share=0.0,
        random_seed=0,
        data_snapshot_id="snap",
    )


def test_ledger_writes_schema_1_2_by_default() -> None:
    ledger = RunLedger(**_base_ledger_kwargs())
    assert ledger.schema_version == "1.2"


def test_ledger_loads_legacy_1_0_dict() -> None:
    raw = _base_ledger_kwargs() | {"schema_version": "1.0"}
    ledger = RunLedger.model_validate(raw)
    assert ledger.schema_version == "1.0"
    assert ledger.prediction_set_hash is None
    assert ledger.window_summary is None


def test_ledger_loads_1_1_with_prediction_set_hash() -> None:
    raw = _base_ledger_kwargs() | {
        "schema_version": "1.1",
        "prediction_set_hash": "f" * 64,
    }
    ledger = RunLedger.model_validate(raw)
    assert ledger.prediction_set_hash == "f" * 64
    # v1.1 ledgers predate window_summary — must still load as None.
    assert ledger.window_summary is None


def test_ledger_rejects_unknown_schema_version() -> None:
    raw = _base_ledger_kwargs() | {"schema_version": "9.9"}
    with pytest.raises(Exception):
        RunLedger.model_validate(raw)
