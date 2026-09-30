"""Catalog-derived indicator warmup policy tests."""

from __future__ import annotations

import pytest

from app.services.dataset_service import INDICATOR_CONFIGS
from app.services.indicator_warmup_policy import (
    INDICATOR_WARMUP_FAMILIES,
    IndicatorEntry,
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


def test_every_catalog_indicator_has_a_warmup_family() -> None:
    """A new catalog indicator fails here until it is classified, instead of
    silently warming up on today's 200-bar floor — and a removed one leaves no
    stale family behind (#2611 review)."""
    assert set(INDICATOR_WARMUP_FAMILIES) == set(INDICATOR_CONFIGS)


@pytest.mark.parametrize(
    ("entry", "lookback"),
    [
        # Finite window and EMA-smoothed: m = 1, the largest whole-number window.
        ({"name": "sma", "params": {"length": 20}}, 20),
        ({"name": "ema", "params": {"length": 20.0}}, 20),
        ({"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}}, 26),
        ({"name": "bbands", "params": {"length": 20, "std": 2.5}}, 20),
        # Wilder-smoothed: m = 2. Double-Wilder (ADX): m = 3.
        ({"name": "rsi", "params": {"length": 14}}, 28),
        ({"name": "atr", "params": {"length": 14}}, 28),
        ({"name": "adx", "params": {"length": 14}}, 42),
        # ...even where that reaches past today's 200-bar floor.
        ({"name": "adx", "params": {"length": 100}}, 300),
        # Path-dependent and hidden-memory: today's lookback, whatever the length.
        ({"name": "obv", "params": {}}, 200),
        ({"name": "psar", "params": {"af0": 0.02, "af": 0.02, "max_af": 0.2}}, 200),
        ({"name": "supertrend", "params": {"length": 10, "multiplier": 3.0}}, 200),
        ({"name": "kama", "params": {"length": 10}}, 200),
        ({"name": "kama", "params": {"length": 300}}, 300),
    ],
)
def test_a_request_is_sized_by_its_smoothing_family(entry: IndicatorEntry, lookback: int) -> None:
    assert requested_indicator_warmup_lookback([entry], INDICATOR_CONFIGS) == lookback


def test_a_request_without_params_is_sized_on_the_catalog_defaults() -> None:
    """#2611 review P1-2: ``params`` is optional on every Data Lab request, and
    an omitted parameter must not size the lead-in at zero."""
    assert requested_indicator_warmup_lookback([{"name": "rsi"}], INDICATOR_CONFIGS) == 28
    assert requested_indicator_warmup_lookback([{"name": "macd", "params": {"fast": 5}}], INDICATOR_CONFIGS) == 26


@pytest.mark.parametrize(
    "entry",
    [
        # Not in the catalog: nothing classifies it.
        {"name": "vwma", "params": {"length": 20}},
        # A parameter the catalog does not expose can change the memory.
        {"name": "rsi", "params": {"length": 14, "mamode": "sma"}},
        {"name": "ema", "params": {"length": 20, "offset": 1}},
        # No whole-number length.
        {"name": "ema", "params": {"length": 2.5}},
    ],
)
def test_anything_the_families_cannot_size_keeps_todays_lookback(entry: IndicatorEntry) -> None:
    assert requested_indicator_warmup_lookback([entry], INDICATOR_CONFIGS) == 200


def test_the_largest_requested_lookback_sizes_the_lead_in() -> None:
    entries: list[IndicatorEntry] = [
        {"name": "ema", "params": {"length": 20}},
        {"name": "rsi", "params": {"length": 14}},
    ]

    assert requested_indicator_warmup_lookback(entries, INDICATOR_CONFIGS) == 28
    assert requested_indicator_warmup_lookback([], INDICATOR_CONFIGS) == 0
