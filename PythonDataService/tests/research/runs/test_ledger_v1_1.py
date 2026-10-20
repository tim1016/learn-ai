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


def test_ledger_schema_versions_write_1_3_by_default_and_load_legacy_1_0_and_1_1() -> None:
    default = RunLedger(**_base_ledger_kwargs())
    assert default.schema_version == "1.3", "writes schema 1.3 by default"
    assert default.warmup_start_ms is None, "default ledger has no warmup start"

    legacy_raw = _base_ledger_kwargs() | {"schema_version": "1.0"}
    legacy = RunLedger.model_validate(legacy_raw)
    assert legacy.schema_version == "1.0", "legacy 1.0 dict keeps its schema version"
    assert legacy.prediction_set_hash is None, "legacy 1.0 has no prediction set hash"
    assert legacy.window_summary is None, "legacy 1.0 has no window summary"
    assert "warmup_start_ms" not in legacy.model_dump(mode="json"), "legacy 1.0 dump omits warmup_start_ms"

    v1_1_raw = _base_ledger_kwargs() | {
        "schema_version": "1.1",
        "prediction_set_hash": "f" * 64,
    }
    v1_1 = RunLedger.model_validate(v1_1_raw)
    assert v1_1.prediction_set_hash == "f" * 64, "1.1 ledger carries its prediction set hash"
    # v1.1 ledgers predate window_summary — must still load as None.
    assert v1_1.window_summary is None, "1.1 ledger predates window_summary and loads it as None"


def test_ledger_rejects_unknown_schema_version() -> None:
    raw = _base_ledger_kwargs() | {"schema_version": "9.9"}
    with pytest.raises(Exception):
        RunLedger.model_validate(raw)
