"""The legacy feature experiment's headline keeps every validation column the retired page showed."""

from __future__ import annotations

from decimal import Decimal

from app.research.history.legacy_sources import FeatureExperiments

ROW = {
    "MeanIC": Decimal("0.01234500"),
    "ICTStat": Decimal("2.40000000"),
    "AdfPValue": Decimal("0.01000000"),
    "KpssPValue": Decimal("0.10000000"),
    "IsStationary": True,
    "MonotonicityRatio": Decimal("0.80000000"),
    "IsMonotonic": True,
    "PassedValidation": True,
    "BarsUsed": 31_250,
}


def test_a_feature_headline_carries_the_stationarity_and_monotonicity_columns() -> None:
    assert FeatureExperiments()._headline(ROW) == (
        "Mean IC 0.0123, t-stat 2.40; ADF p 0.0100, KPSS p 0.1000 (stationary); "
        "monotonic (ratio 0.80); passed validation; 31,250 bars"
    )


def test_a_feature_headline_says_when_the_series_is_not_stationary_or_not_monotonic() -> None:
    row = {
        **ROW,
        "IsStationary": False,
        "IsMonotonic": False,
        "MonotonicityRatio": Decimal("0.35000000"),
        "PassedValidation": False,
    }

    assert FeatureExperiments()._headline(row) == (
        "Mean IC 0.0123, t-stat 2.40; ADF p 0.0100, KPSS p 0.1000 (not stationary); "
        "not monotonic (ratio 0.35); did not pass validation; 31,250 bars"
    )
