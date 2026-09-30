"""Catalog-derived indicator warmup policy tests."""

from __future__ import annotations

from app.services.dataset_service import INDICATOR_CONFIGS
from app.services.indicator_warmup_policy import (
    configured_indicator_warmup_bars,
    requested_indicator_warmup_lookback,
)


def test_configured_warmup_covers_the_largest_catalog_recipe() -> None:
    assert configured_indicator_warmup_bars(INDICATOR_CONFIGS) == 2_500


def test_configured_warmup_uses_typed_integer_metadata_not_parameter_names() -> None:
    catalog = {
        "future-indicator": [
            {"name": "differently_named_window", "type": "int", "max": 650},
            {"name": "scale", "type": "float", "max": 10.0},
        ]
    }

    assert configured_indicator_warmup_bars(catalog) == 3_250


def test_requested_lookback_takes_the_largest_whole_number_window_by_value() -> None:
    entries = [
        {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
        {"name": "supertrend", "params": {"length": 10, "multiplier": 3.0}},
        {"name": "bb", "params": {"bb_length": 20.0, "bb_std": 2.5}},
    ]

    # Name-independent: "slow" and "bb_length" count like "length"; the whole
    # float 20.0 counts, the fractional 2.5 does not.
    assert requested_indicator_warmup_lookback(entries) == 26


def test_requested_lookback_has_no_floor_and_no_entries_need_none() -> None:
    """#2611: the requested recipes alone size the lead-in — there is no
    200-bar floor — and a request with no integer window warms up on nothing."""
    assert requested_indicator_warmup_lookback([{"name": "ema", "params": {"length": 3}}]) == 3
    assert requested_indicator_warmup_lookback([{"name": "bb", "params": {"bb_std": 2.0}}]) == 2
    assert requested_indicator_warmup_lookback([]) == 0
    assert requested_indicator_warmup_lookback([{"name": "x", "params": {}}]) == 0
