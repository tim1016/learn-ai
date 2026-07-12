"""Equity-curve downsampling for persisted run-detail display.

Formula: keep first/last points, requested trade marks, running highs/lows,
  then stride the remaining points until the display cap is satisfied.
Reference: Internal display-only policy in
  docs/superpowers/specs/2026-07-12-engine-lab-overhaul-design.md § 4.3.
Canonical implementation: this file.
Validated against: PythonDataService/tests/engine/results/test_equity_downsample.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

MAX_EQUITY_POINTS = 10_000


@dataclass(frozen=True)
class EquityCurvePoint:
    t: int
    e: float


def build_equity_curve_envelope(
    points: list[EquityCurvePoint],
    *,
    cadence: Literal["strategy_bar_close", "lean_chart_sampling"],
    trade_timestamps: set[int] | None = None,
    max_points: int = MAX_EQUITY_POINTS,
) -> dict[str, Any]:
    if max_points < 2:
        raise ValueError("max_points must be at least 2")

    trade_marks = trade_timestamps or set()
    kept = _select_points(points, trade_marks=trade_marks, max_points=max_points)
    return {
        "cadence": cadence,
        "downsample": {
            "policy": "first_last+trade_marks+running_extrema+stride",
            "raw_points": len(points),
            "kept_points": len(kept),
        },
        "points": [{"t": point.t, "e": point.e} for point in kept],
    }


def from_engine_curve(
    points: list[dict[str, Any]],
    *,
    trade_timestamps: set[int] | None = None,
    max_points: int = MAX_EQUITY_POINTS,
) -> dict[str, Any]:
    parsed = [
        EquityCurvePoint(t=int(point["timestamp"]), e=float(point["equity"]))
        for point in points
        if point.get("timestamp") is not None and point.get("equity") is not None
    ]
    return build_equity_curve_envelope(
        parsed,
        cadence="strategy_bar_close",
        trade_timestamps=trade_timestamps,
        max_points=max_points,
    )


def from_lean_curve(
    points: list[dict[str, Any]],
    *,
    trade_timestamps: set[int] | None = None,
    max_points: int = MAX_EQUITY_POINTS,
) -> dict[str, Any]:
    parsed = [
        EquityCurvePoint(t=int(point["ms_utc"]), e=float(point["value"]))
        for point in points
        if point.get("ms_utc") is not None and point.get("value") is not None
    ]
    return build_equity_curve_envelope(
        parsed,
        cadence="lean_chart_sampling",
        trade_timestamps=trade_timestamps,
        max_points=max_points,
    )


def _select_points(
    points: list[EquityCurvePoint],
    *,
    trade_marks: set[int],
    max_points: int,
) -> list[EquityCurvePoint]:
    if len(points) <= max_points:
        return points
    required_indexes = _required_indexes(points, trade_marks)
    if len(required_indexes) >= max_points:
        return [points[i] for i in sorted(required_indexes)[: max_points - 1]] + [points[-1]]

    remaining_slots = max_points - len(required_indexes)
    optional = [i for i in range(len(points)) if i not in required_indexes]
    stride = max(1, len(optional) // remaining_slots)
    sampled = set(optional[::stride][:remaining_slots])
    selected = sorted(required_indexes | sampled)
    if len(selected) > max_points:
        selected = [*selected[: max_points - 1], len(points) - 1]
    return [points[i] for i in selected]


def _required_indexes(points: list[EquityCurvePoint], trade_marks: set[int]) -> set[int]:
    if not points:
        return set()
    required = {0, len(points) - 1}
    by_timestamp = {point.t: i for i, point in enumerate(points)}
    for timestamp in trade_marks:
        if timestamp in by_timestamp:
            required.add(by_timestamp[timestamp])

    running_high = float("-inf")
    low_since_peak = float("inf")
    for i, point in enumerate(points):
        if point.e > running_high:
            running_high = point.e
            low_since_peak = point.e
            required.add(i)
        if point.e < low_since_peak:
            low_since_peak = point.e
            required.add(i)
    return required
