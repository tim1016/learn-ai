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


@pytest.mark.parametrize(
    ("catalog", "expected_bars"),
    [
        pytest.param(INDICATOR_CONFIGS, 2_500, id="covers_the_largest_catalog_recipe"),
        pytest.param(
            {
                "future-indicator": [
                    {"name": "differently_named_window", "type": "int", "max": 650},
                    {"name": "scale", "type": "float", "max": 10.0},
                ]
            },
            3_250,
            id="uses_typed_integer_metadata_not_parameter_names",
        ),
    ],
)
def test_configured_warmup_is_sized_by_the_catalogs_typed_integer_maxima(
    catalog: dict, expected_bars: int
) -> None:
    assert configured_indicator_warmup_bars(catalog) == expected_bars


def test_every_catalog_indicator_has_a_warmup_family() -> None:
    """A new catalog indicator fails here until it is classified, instead of
    silently warming up on today's 200-bar floor — and a removed one leaves no
    stale family behind (#2611 review)."""
    assert set(INDICATOR_WARMUP_FAMILIES) == set(INDICATOR_CONFIGS)


@pytest.mark.parametrize(
    ("entry", "lookback"),
    [
        # Finite window and EMA-smoothed: m = 1, the largest whole-number window.
        pytest.param({"name": "sma", "params": {"length": 20}}, 20, id="sma_length_20"),
        pytest.param({"name": "ema", "params": {"length": 20.0}}, 20, id="ema_whole_float_length_20"),
        pytest.param({"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}}, 26, id="macd_largest_window_26"),
        pytest.param({"name": "bbands", "params": {"length": 20, "std": 2.5}}, 20, id="bbands_length_20"),
        # Wilder-smoothed: m = 2. Double-Wilder (ADX): m = 3.
        pytest.param({"name": "rsi", "params": {"length": 14}}, 28, id="rsi_wilder_m2"),
        pytest.param({"name": "atr", "params": {"length": 14}}, 28, id="atr_wilder_m2"),
        pytest.param({"name": "adx", "params": {"length": 14}}, 42, id="adx_double_wilder_m3"),
        # ...even where that reaches past today's 200-bar floor.
        pytest.param({"name": "adx", "params": {"length": 100}}, 300, id="adx_length_100_past_the_floor"),
        # Path-dependent and hidden-memory: today's lookback, whatever the length.
        pytest.param({"name": "obv", "params": {}}, 200, id="obv_path_dependent"),
        pytest.param(
            {"name": "psar", "params": {"af0": 0.02, "af": 0.02, "max_af": 0.2}}, 200, id="psar_path_dependent"
        ),
        pytest.param(
            {"name": "supertrend", "params": {"length": 10, "multiplier": 3.0}}, 200, id="supertrend_path_dependent"
        ),
        pytest.param({"name": "kama", "params": {"length": 10}}, 200, id="kama_length_10_keeps_the_floor"),
        pytest.param({"name": "kama", "params": {"length": 300}}, 300, id="kama_length_300"),
        # #2611 review P1-2: ``params`` is optional on every Data Lab request, and
        # an omitted parameter must not size the lead-in at zero.
        pytest.param({"name": "rsi"}, 28, id="2611_p1_2_omitted_params_size_on_the_catalog_defaults_rsi"),
        pytest.param(
            {"name": "macd", "params": {"fast": 5}}, 26, id="2611_p1_2_omitted_params_size_on_the_catalog_defaults_macd"
        ),
        # Not in the catalog: nothing classifies it.
        pytest.param({"name": "vwma", "params": {"length": 20}}, 200, id="not_in_the_catalog_keeps_todays_lookback"),
        # A parameter the catalog does not expose can change the memory.
        pytest.param(
            {"name": "rsi", "params": {"length": 14, "mamode": "sma"}}, 200, id="unexposed_mamode_keeps_todays_lookback"
        ),
        pytest.param(
            {"name": "ema", "params": {"length": 20, "offset": 1}}, 200, id="unexposed_offset_keeps_todays_lookback"
        ),
        # No whole-number length.
        pytest.param(
            {"name": "ema", "params": {"length": 2.5}}, 200, id="no_whole_number_length_keeps_todays_lookback"
        ),
    ],
)
def test_a_request_is_sized_by_its_smoothing_family_or_keeps_todays_lookback(
    entry: IndicatorEntry, lookback: int
) -> None:
    assert requested_indicator_warmup_lookback([entry], INDICATOR_CONFIGS) == lookback


def test_the_largest_requested_lookback_sizes_the_lead_in() -> None:
    entries: list[IndicatorEntry] = [
        {"name": "ema", "params": {"length": 20}},
        {"name": "rsi", "params": {"length": 14}},
    ]

    assert requested_indicator_warmup_lookback(entries, INDICATOR_CONFIGS) == 28
    assert requested_indicator_warmup_lookback([], INDICATOR_CONFIGS) == 0
