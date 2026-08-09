from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.results.equity_downsample import (
    EquityCurvePoint,
    RealizedEquityTrade,
    build_equity_curve_envelope,
    build_realized_equity_envelope,
    build_run_equity_envelope,
)


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


def test_realized_equity_steps_once_per_exit_and_reconciles_net_pnl() -> None:
    envelope = build_realized_equity_envelope(
        initial_cash=Decimal("100000.00"),
        trades=[
            RealizedEquityTrade(1, 2_000, Decimal("250.25")),
            RealizedEquityTrade(2, 3_000, Decimal("-75.50")),
            RealizedEquityTrade(3, 4_000, Decimal("0.00")),
        ],
        start_ms_utc=1_000,
        end_ms_utc=5_000,
    )

    assert envelope["cadence"] == "trade_exit"
    assert envelope["points"] == [
        {"t": 1_000, "e": 100000.0},
        {"t": 2_000, "e": 100250.25},
        {"t": 3_000, "e": 100174.75},
        {"t": 4_000, "e": 100174.75},
        {"t": 5_000, "e": 100174.75},
    ]
    assert envelope["points"][-1]["e"] == pytest.approx(100174.75, abs=1e-6, rel=0)


def test_realized_equity_folds_equal_exit_timestamps_in_trade_number_order() -> None:
    envelope = build_realized_equity_envelope(
        initial_cash=100.0,
        trades=[
            RealizedEquityTrade(2, 2_000, Decimal("-3.00")),
            RealizedEquityTrade(1, 2_000, Decimal("5.00")),
        ],
        start_ms_utc=1_000,
        end_ms_utc=3_000,
    )

    assert envelope["points"] == [
        {"t": 1_000, "e": 100.0},
        {"t": 2_000, "e": 102.0},
        {"t": 3_000, "e": 102.0},
    ]


def test_realized_equity_zero_trade_run_stays_flat() -> None:
    envelope = build_realized_equity_envelope(
        initial_cash=100.0,
        trades=[],
        start_ms_utc=1_000,
        end_ms_utc=2_000,
    )

    assert envelope["points"] == [{"t": 1_000, "e": 100.0}, {"t": 2_000, "e": 100.0}]


def test_realized_equity_rejects_exit_at_or_before_start() -> None:
    with pytest.raises(ValueError, match="must follow"):
        build_realized_equity_envelope(
            initial_cash=100.0,
            trades=[RealizedEquityTrade(1, 1_000, Decimal("1"))],
            start_ms_utc=1_000,
            end_ms_utc=2_000,
        )


def test_run_envelope_declares_both_curve_identities() -> None:
    envelope = build_run_equity_envelope(
        mark_to_market={"cadence": "strategy_bar_close", "points": []},
        realized={"cadence": "trade_exit", "points": []},
    )

    assert envelope["schema_version"] == 2
    assert set(envelope) == {"schema_version", "mark_to_market", "realized"}
