"""Tests for app.research.features.registry."""

from __future__ import annotations

import pytest

from app.research.features.registry import (
    FEATURE_REGISTRY,
    OPTIONS_FEATURES,
    FeatureMetadata,
    FeatureName,
    get_feature_metadata,
    list_available_features,
)


def test_every_enum_member_has_metadata_entry():
    missing = [name for name in FeatureName if name not in FEATURE_REGISTRY]

    assert not missing, f"registry missing entries for {missing}"


def test_feature_metadata_is_frozen_dataclass():
    md = FEATURE_REGISTRY[FeatureName.MOMENTUM_5M]

    with pytest.raises(Exception):  # frozen dataclass → FrozenInstanceError
        md.name = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(
    "feature,expected_category",
    [
        (FeatureName.MOMENTUM_5M, "momentum"),
        (FeatureName.RSI_14, "momentum"),
        (FeatureName.REALIZED_VOL_30, "volatility"),
        (FeatureName.VOLUME_ZSCORE, "volume"),
        (FeatureName.MACD_SIGNAL, "momentum"),
        (FeatureName.IV_30D, "options"),
        (FeatureName.IV_RANK_60, "options"),
        (FeatureName.LOG_SKEW, "options"),
        (FeatureName.IV_RANK_252, "options"),
        (FeatureName.VRP_5, "options"),
    ],
)
def test_feature_category_matches_expectation(feature: FeatureName, expected_category: str):
    md = FEATURE_REGISTRY[feature]

    assert md.category == expected_category


def test_options_features_set_matches_options_category():
    options_category_values = {f.value for f in FeatureName if FEATURE_REGISTRY[f].category == "options"}

    assert options_category_values == OPTIONS_FEATURES


def test_get_feature_metadata_valid_name_returns_entry():
    md = get_feature_metadata("rsi_14")

    assert isinstance(md, FeatureMetadata)
    assert md.window == 14


def test_get_feature_metadata_unknown_name_returns_none():
    assert get_feature_metadata("not_a_real_feature") is None


def test_list_available_features_returns_all_enum_values():
    names = list_available_features()

    assert set(names) == {f.value for f in FeatureName}
    assert len(names) == len(FeatureName)


def test_options_features_have_options_data_source():
    for name in OPTIONS_FEATURES:
        md = get_feature_metadata(name)
        assert md is not None
        assert md.data_source == "options"


def test_stock_features_have_stock_data_source():
    for feature in FeatureName:
        if feature.value in OPTIONS_FEATURES:
            continue
        md = FEATURE_REGISTRY[feature]
        assert md.data_source == "stock", f"{feature} is not an options feature but has data_source={md.data_source}"
