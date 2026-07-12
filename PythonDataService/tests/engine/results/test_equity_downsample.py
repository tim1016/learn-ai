from __future__ import annotations

import pytest

from app.engine.results.equity_downsample import EquityCurvePoint, build_equity_curve_envelope


def test_downsample_preserves_first_last_trade_marks_and_extrema() -> None:
    points = [
        EquityCurvePoint(t=1, e=100.0),
        EquityCurvePoint(t=2, e=101.0),
        EquityCurvePoint(t=3, e=99.0),
        EquityCurvePoint(t=4, e=102.0),
        EquityCurvePoint(t=5, e=98.0),
        EquityCurvePoint(t=6, e=100.0),
        EquityCurvePoint(t=7, e=103.0),
        EquityCurvePoint(t=8, e=101.0),
    ]

    envelope = build_equity_curve_envelope(
        points,
        cadence="strategy_bar_close",
        trade_timestamps={4},
        max_points=7,
    )

    timestamps = {point["t"] for point in envelope["points"]}
    assert {1, 4, 5, 7, 8}.issubset(timestamps)
    assert envelope["downsample"] == {
        "policy": "first_last+trade_marks+running_extrema+stride",
        "raw_points": 8,
        "kept_points": len(envelope["points"]),
    }


def test_downsample_rejects_impossible_cap() -> None:
    with pytest.raises(ValueError, match="max_points"):
        build_equity_curve_envelope([], cadence="strategy_bar_close", max_points=1)


def test_downsample_preserves_trough_above_start_after_new_peak() -> None:
    points = [
        EquityCurvePoint(t=1, e=100.0),
        EquityCurvePoint(t=2, e=120.0),
        EquityCurvePoint(t=3, e=105.0),
        EquityCurvePoint(t=4, e=121.0),
        EquityCurvePoint(t=5, e=118.0),
        EquityCurvePoint(t=6, e=122.0),
        EquityCurvePoint(t=7, e=119.0),
        EquityCurvePoint(t=8, e=123.0),
    ]

    envelope = build_equity_curve_envelope(
        points,
        cadence="strategy_bar_close",
        max_points=7,
    )

    timestamps = {point["t"] for point in envelope["points"]}
    assert 3 in timestamps


def test_downsample_samples_required_extrema_when_they_exceed_cap() -> None:
    points = [EquityCurvePoint(t=i, e=float(i)) for i in range(1, 21)]

    envelope = build_equity_curve_envelope(
        points,
        cadence="strategy_bar_close",
        max_points=5,
    )

    timestamps = [point["t"] for point in envelope["points"]]
    assert timestamps == [1, 6, 11, 15, 20]
