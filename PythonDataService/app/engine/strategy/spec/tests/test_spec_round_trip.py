"""Round-trip tests for ``StrategySpec`` schema loading and validation.

These tests exercise the schema layer in isolation — no engine, no
indicators, no bars. Their job is to prove that:

  * each canonical fixture loads without error;
  * round-trip via ``model_dump_json`` → ``model_validate_json`` is stable;
  * the JSON-Schema export is valid draft-2020-12;
  * malformed specs are rejected with descriptive errors.

This file is the cheapest sanity check on the schema; the full parity
gate lives in the per-strategy ``test_spec_*_parity.py`` modules.
"""

from __future__ import annotations

import sys

from app.engine.strategy.spec import StrategySpec, load_spec_from_path
from app.engine.strategy.spec.tests._parity_helpers import (
    configure_script_logger,
    fixture_path,
    logger,
)

CANONICAL_SPECS = ("spy_ema_crossover", "sma_crossover", "rsi_mean_reversion")


# ---------------------------------------------------------------------------
# Canonical fixture loading.
# ---------------------------------------------------------------------------
def _check_fixture_loads(name: str) -> None:
    spec = load_spec_from_path(fixture_path(name))
    assert spec.schema_version == "1.0"
    assert len(spec.symbols) == 1
    assert len(spec.indicators) >= 1
    assert spec.entry.size.kind == "SetHoldings"


def _check_round_trip_stable(name: str) -> None:
    """``model_dump_json`` → ``model_validate_json`` must be a fixed point."""
    spec = load_spec_from_path(fixture_path(name))
    payload = spec.model_dump_json()
    again = StrategySpec.model_validate_json(payload)
    assert again.model_dump_json() == payload, f"round-trip not stable for {name}"


def test_canonical_specs_load() -> None:
    for name in CANONICAL_SPECS:
        _check_fixture_loads(name)


def test_canonical_specs_round_trip() -> None:
    for name in CANONICAL_SPECS:
        _check_round_trip_stable(name)


# ---------------------------------------------------------------------------
# Schema export.
# ---------------------------------------------------------------------------
def test_json_schema_exports() -> None:
    schema = StrategySpec.model_json_schema()
    # Must be a real schema object with required structure.
    assert "$defs" in schema
    assert "properties" in schema
    assert "required" in schema
    # Must include the discriminated unions we depend on.
    defs = schema["$defs"]
    assert "FreshCross" in defs
    assert "IndicatorComparison" in defs
    assert "Subtract" in defs


# ---------------------------------------------------------------------------
# Validator rejection cases.
# ---------------------------------------------------------------------------
def _base_spec() -> dict:
    return {
        "schema_version": "1.0",
        "name": "x",
        "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "indicators": [],
        "entry": {
            "logic": "AND",
            "conditions": [{"kind": "BarsSinceEntry", "op": ">=", "value": 0}],
            "size": {"kind": "SetHoldings", "fraction": 1.0},
        },
        "exit": {"logic": "OR", "conditions": []},
    }


def _expect_validation_error(payload: dict, needle: str) -> None:
    try:
        StrategySpec.model_validate(payload)
    except Exception as e:
        msg = str(e)
        assert needle in msg, f"expected {needle!r} in error, got: {msg[:300]}"
        return
    raise AssertionError(f"expected validation error mentioning {needle!r}")


def test_rejects_multi_symbol() -> None:
    payload = _base_spec()
    payload["symbols"] = ["SPY", "QQQ"]
    _expect_validation_error(payload, "single-symbol")


def test_rejects_undeclared_indicator_ref() -> None:
    payload = _base_spec()
    payload["entry"]["conditions"] = [{"kind": "IndicatorBetween", "indicator": "ghost", "lo": 50, "hi": 70}]
    _expect_validation_error(payload, "undeclared indicator id")


def test_rejects_extra_fields() -> None:
    payload = _base_spec()
    payload["foo"] = "bar"
    _expect_validation_error(payload, "Extra inputs")


def test_rejects_client_id_as_strategy_field() -> None:
    payload = _base_spec()
    payload["client_id"] = 12
    _expect_validation_error(payload, "Extra inputs")


def test_rejects_unknown_condition_kind() -> None:
    payload = _base_spec()
    payload["entry"]["conditions"] = [{"kind": "MysteryCondition"}]
    _expect_validation_error(payload, "tagged-union")


def test_rejects_duplicate_indicator_ids() -> None:
    payload = _base_spec()
    payload["indicators"] = [
        {"id": "x", "kind": "EMA", "period": 5},
        {"id": "x", "kind": "EMA", "period": 10},
    ]
    _expect_validation_error(payload, "duplicate indicator")


# ---------------------------------------------------------------------------
# Script entry point.
# ---------------------------------------------------------------------------
def run_all() -> None:
    configure_script_logger()
    failed = False
    tests = [
        ("canonical fixtures load", test_canonical_specs_load),
        ("canonical fixtures round-trip", test_canonical_specs_round_trip),
        ("JSON schema export", test_json_schema_exports),
        ("multi-symbol rejected", test_rejects_multi_symbol),
        ("undeclared indicator ref rejected", test_rejects_undeclared_indicator_ref),
        ("extra fields rejected", test_rejects_extra_fields),
        ("client_id strategy field rejected", test_rejects_client_id_as_strategy_field),
        ("unknown kind rejected", test_rejects_unknown_condition_kind),
        ("duplicate indicator ids rejected", test_rejects_duplicate_indicator_ids),
    ]
    for label, fn in tests:
        try:
            fn()
            logger.info("PASS: %s", label)
        except Exception as e:
            failed = True
            logger.error("FAIL: %s — %s", label, e)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
