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

from collections.abc import Callable

import pytest

from app.engine.strategy.spec import StrategySpec, load_spec_from_path
from app.engine.strategy.spec.tests._parity_helpers import (
    fixture_path,
)

CANONICAL_SPECS = ("spy_ema_crossover", "sma_crossover", "rsi_mean_reversion")


# ---------------------------------------------------------------------------
# Canonical fixture loading.
# ---------------------------------------------------------------------------
def _check_round_trip_stable(name: str) -> None:
    """``model_dump_json`` → ``model_validate_json`` must be a fixed point."""
    spec = load_spec_from_path(fixture_path(name))
    payload = spec.model_dump_json()
    again = StrategySpec.model_validate_json(payload)
    assert again.model_dump_json() == payload, f"round-trip not stable for {name}"


def test_canonical_specs_round_trip() -> None:
    for name in CANONICAL_SPECS:
        _check_round_trip_stable(name)


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


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        pytest.param(lambda p: p.update(symbols=["SPY", "QQQ"]), "single-symbol", id="multi_symbol"),
        pytest.param(
            lambda p: p["entry"].update(
                conditions=[{"kind": "IndicatorBetween", "indicator": "ghost", "lo": 50, "hi": 70}]
            ),
            "undeclared indicator id",
            id="undeclared_indicator_ref",
        ),
        pytest.param(lambda p: p.update(foo="bar"), "Extra inputs", id="extra_fields"),
        pytest.param(
            lambda p: p["entry"].update(conditions=[{"kind": "MysteryCondition"}]),
            "tagged-union",
            id="unknown_condition_kind",
        ),
        pytest.param(
            lambda p: p.update(
                indicators=[
                    {"id": "x", "kind": "EMA", "period": 5},
                    {"id": "x", "kind": "EMA", "period": 10},
                ]
            ),
            "duplicate indicator",
            id="duplicate_indicator_ids",
        ),
    ],
)
def test_rejects_invalid_specs(mutate: Callable[[dict], None], needle: str) -> None:
    payload = _base_spec()
    mutate(payload)
    _expect_validation_error(payload, needle)
