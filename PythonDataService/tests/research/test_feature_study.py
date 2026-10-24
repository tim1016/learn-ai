"""The Feature study computes from admitted lake inputs and gives the numbers it gave on the same bars (#3521; R14, T08).

Two claims, kept apart as PRD #3490 R14 requires.

1. *Same canonical input, same outputs.* The scientific functions are not
   touched by the source change. Given the same bars they give the same
   report, at the repo's strict-float default (``atol=1e-9, rtol=0``). The
   bars here travel the real lake path (vendor HTTP, ``ensure_data``, the lake
   writer, committed-byte admission, the reader) and are compared with the
   vendor's own, on a grid the lake holds exactly.
2. *What the source change itself moves* (precision, adjustment, session,
   warmup, the forward-label tail) is pinned case by case, with the cause named
   in the reconcile taxonomy. The whole-payload provider-versus-lake
   comparison lives in ``test_feature_study_lake_vs_provider.py``.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import respx

from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.config import ResearchConfig
from app.research.features.ta_features import TechnicalFeatures
from app.research.runner import run_feature_research
from app.research.study_inputs import StudyInputRefusal, admit_minute_inputs, bars_as_rows, fold_minute_bars
from app.research.target import compute_forward_log_return
from app.services.feature_study_service import (
    FeatureStudyParams,
    cache_params,
    feature_study_identity,
    serialize_feature_report,
)
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.pinned_vendor import mock_vendor, vendor_history

#: The repo's strict-float default for indicators and closed-form calculations.
ATOL = 1e-9
RTOL = 0.0

ET = ZoneInfo("America/New_York")
NOW_MS = 1_735_689_600_000  # 2025-01-01
WINDOW = expected_sessions(date(2024, 3, 4), date(2024, 3, 15))  # ten sessions, across the DST change
ALL_FEATURES = ["momentum_5m", "rsi_14", "realized_vol_30", "volume_zscore", "macd_signal"]


@pytest.fixture
def lake_vendor(monkeypatch: pytest.MonkeyPatch, tmp_path):
    install_fake_catalog(monkeypatch)
    point_lake_writer_at_tmp(tmp_path, monkeypatch)
    with respx.mock:
        mock_launcher()
        # Dense enough that the 15-minute target clears the study's 70% validity gate: the gate is not under test here.
        tape = vendor_history(WINDOW, seed=11, start_price=150.0, regular_keep=0.995, extended_keep=0.92)
        mock_vendor("SPY", tape)
        yield tape


def study(*, multiplier: int = 1, session: str = "extended") -> FeatureStudyParams:
    return FeatureStudyParams("SPY", "rsi_14", WINDOW[0], WINDOW[-1], "minute", multiplier, session)  # type: ignore[arg-type]


def lake_rows(params: FeatureStudyParams) -> tuple[list[dict], Any]:
    identity = feature_study_identity(params)
    admitted = admit_minute_inputs(identity.inputs, requester="test", now_ms=NOW_MS)
    return bars_as_rows(fold_minute_bars(admitted.window_bars, identity.bar_minutes)), admitted


def vendor_rows(tape: dict, *, bar_minutes: int = 1) -> list[dict]:
    """What the provider path would hand the runner for the same tape: the vendor's own floats."""
    minutes = [
        {"timestamp": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": float(b["v"])}
        for day in sorted(tape)
        for b in tape[day]
    ]
    if bar_minutes == 1:
        return minutes
    bucket_ms = bar_minutes * 60_000
    folded: dict[int, dict] = {}
    for m in minutes:
        key = m["timestamp"] // bucket_ms * bucket_ms
        if key not in folded:
            folded[key] = {**m, "timestamp": key}
        else:
            bar = folded[key]
            bar["high"], bar["low"] = max(bar["high"], m["high"]), min(bar["low"], m["low"])
            bar["close"] = m["close"]
            bar["volume"] += m["volume"]
    return [folded[k] for k in sorted(folded)]


def assert_same(a: Any, b: Any, where: str = "report") -> None:
    """Deep equality, floats at the strict-float tolerance (NaN equals NaN: a warmup region is part of the answer)."""
    if isinstance(a, dict):
        assert isinstance(b, dict) and a.keys() == b.keys(), where
        for key in a:
            assert_same(a[key], b[key], f"{where}.{key}")
    elif isinstance(a, list | tuple):
        assert isinstance(b, list | tuple) and len(a) == len(b), f"{where}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            assert_same(x, y, f"{where}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert np.isclose(a, b, atol=ATOL, rtol=RTOL, equal_nan=True), f"{where}: {a!r} != {b!r}"
    else:
        assert a == b, f"{where}: {a!r} != {b!r}"


# ---------------------------------------------------------------------------
# The bars: the lake hands back the vendor's own on-grid bars, at every supported size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("multiplier", [1, 3, 5, 15])
def test_lake_bars_equal_the_vendors_own_bars_for_a_price_grid_the_lake_holds(lake_vendor, multiplier):
    rows, _ = lake_rows(study(multiplier=multiplier))

    expected = vendor_rows(lake_vendor, bar_minutes=multiplier)

    assert [r["timestamp"] for r in rows] == [r["timestamp"] for r in expected]
    for column in ("open", "high", "low", "close"):
        np.testing.assert_allclose([r[column] for r in rows], [r[column] for r in expected], atol=ATOL, rtol=RTOL)
    np.testing.assert_allclose([r["volume"] for r in rows], [r["volume"] for r in expected], atol=ATOL, rtol=RTOL)


# ---------------------------------------------------------------------------
# Same canonical input -> same outputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feature", ALL_FEATURES)
def test_every_feature_has_the_same_values_and_the_same_warmup_region_on_lake_bars(lake_vendor, feature):
    rows, _ = lake_rows(study())
    expected = vendor_rows(lake_vendor)

    ours = TechnicalFeatures.compute_feature(feature, rows)
    reference = TechnicalFeatures.compute_feature(feature, expected)

    assert ours.isna().tolist() == reference.isna().tolist(), "the NaN warmup region is part of the answer"
    np.testing.assert_allclose(ours.to_numpy(), reference.to_numpy(), atol=ATOL, rtol=RTOL, equal_nan=True)


@pytest.mark.parametrize(
    ("feature", "first_valid"),
    [
        ("momentum_5m", 5),
        # pandas-ta seeds Wilder's RSI at the first difference, so it emits from the second bar. The study has
        # always run on that; the region is pinned so a change to it is seen, not absorbed.
        ("rsi_14", 1),
        ("realized_vol_30", 30),
        ("volume_zscore", 29),
        ("macd_signal", 33),
    ],
)
def test_the_first_valid_value_sits_where_the_indicator_windows_say(lake_vendor, feature, first_valid):
    rows, _ = lake_rows(study())

    values = TechnicalFeatures.compute_feature(feature, rows)

    # No warmup is fetched before the window, so each indicator's first valid value is its own window into the
    # window: the series runs continuous across the overnight gap, as it has always done.
    assert values.first_valid_index() == first_valid
    assert values.iloc[:first_valid].isna().all() and values.iloc[first_valid:].notna().all()


@pytest.mark.parametrize("multiplier", [1, 5])
def test_the_whole_report_is_the_same_on_lake_bars_as_on_the_vendors_bars(lake_vendor, multiplier):
    rows, _ = lake_rows(study(multiplier=multiplier))
    expected = vendor_rows(lake_vendor, bar_minutes=multiplier)

    ours = run_feature_research("SPY", "rsi_14", rows, "2024-03-04", "2024-03-15", ResearchConfig())
    reference = run_feature_research("SPY", "rsi_14", expected, "2024-03-04", "2024-03-15", ResearchConfig())

    assert ours.error is None and reference.error is None, (ours.error, reference.error)
    assert ours.bars_used == len(expected)
    assert_same(serialize_feature_report(ours), serialize_feature_report(reference))


@pytest.mark.parametrize(("multiplier", "horizon_bars"), [(1, 15), (3, 5), (5, 3), (15, 1)])
def test_no_bar_is_read_past_the_requested_end_so_the_forward_label_tail_is_empty(lake_vendor, multiplier, horizon_bars):
    rows, admitted = lake_rows(study(multiplier=multiplier))

    target = compute_forward_log_return(rows, horizon_minutes=15)

    assert target.horizon_bars == horizon_bars
    assert target.invalid_reason_counts["window_runs_off_end"] == horizon_bars, "the window's last 15 minutes have no target"
    assert target.values.iloc[-horizon_bars:].isna().all()
    assert max(r["timestamp"] for r in rows) <= max(b.start_ms for b in admitted.window_bars)
    last_day = max(WINDOW)
    assert all(datetime.fromtimestamp(r["timestamp"] / 1000, ET).date() <= last_day for r in rows)
    assert target.invalid_reason_counts.get("cross_session", 0) >= (len(WINDOW) - 1) * (horizon_bars - 1), (
        "each session's last bars look into the next session and are masked, not filled"
    )


# ---------------------------------------------------------------------------
# What the source change moves, and what it must not
# ---------------------------------------------------------------------------


def test_regular_hours_is_a_different_sample_and_a_different_cache_identity(lake_vendor):
    extended_rows, extended = lake_rows(study(session="extended"))
    regular_rows, regular = lake_rows(study(session="regular"))

    extended_identity = feature_study_identity(study(session="extended"))
    regular_identity = feature_study_identity(study(session="regular"))

    assert len(regular_rows) < len(extended_rows), "extended hours add pre-market and after-hours bars"
    assert extended_identity.digest() != regular_identity.digest()
    assert cache_params(extended_identity, extended, "rsi_14") != cache_params(regular_identity, regular, "rsi_14")
    assert extended_identity.inputs.session_policy == "extended", "the default is the session the study has always computed on"


@pytest.mark.parametrize(
    ("timespan", "multiplier"),
    [("hour", 1), ("day", 1), ("minute", 60), ("minute", 240), ("minute", 2), ("minute", 4), ("minute", 10)],
)
def test_resolutions_the_target_can_never_be_formed_on_refuse_with_a_reason(timespan, multiplier):
    with pytest.raises(StudyInputRefusal) as raised:
        feature_study_identity(FeatureStudyParams("SPY", "rsi_14", WINDOW[0], WINDOW[-1], timespan, multiplier))

    assert raised.value.code == "resolution_unsupported"
    assert "15-minute forward target" in raised.value.reason
    assert "1, 3, 5, 15" in raised.value.reason
    assert raised.value.coverage is None


@pytest.mark.parametrize("multiplier", [1, 3, 5, 15])
def test_every_bar_length_that_divides_the_target_is_accepted(multiplier):
    identity = feature_study_identity(FeatureStudyParams("SPY", "rsi_14", WINDOW[0], WINDOW[-1], "minute", multiplier))

    assert identity.bar_minutes == multiplier
    assert identity.look_forward_minutes == 15
    assert identity.as_dict()["warmup_sessions"] == 0, "the study fetches no warmup"
    assert identity.as_dict()["look_forward_extends_window"] is False
    assert math.isfinite(identity.inputs.as_dict()["window_start_ms"])
