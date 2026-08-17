"""Transport adapter for canonical chart indicator computation.

This service performs no indicator arithmetic. It validates caller-owned bars,
constructs the canonical DataFrame shape, and delegates all numerical work to
``chart_service.compute_indicator_results``.
"""

from __future__ import annotations

from functools import lru_cache
from numbers import Integral, Real
from typing import Any

import pandas as pd

from app.lean_sidecar.workspace import validate_symbol
from app.services.chart_service import compute_indicator_results
from app.services.dataset_service import (
    INDICATOR_CONFIGS,
    assert_canonical_bar_stream,
    list_available_indicators,
)


@lru_cache(maxsize=1)
def _available_indicator_names() -> frozenset[str]:
    return frozenset(
        item["name"]
        for category in list_available_indicators().values()
        for item in category
    )


def _validate_indicator(entry: dict[str, Any]) -> dict[str, Any]:
    name = str(entry.get("name", "")).lower()
    if name not in _available_indicator_names():
        raise ValueError(f"unknown chart indicator: {name or '<empty>'}")

    params = entry.get("params", {})
    definitions = {definition["name"]: definition for definition in INDICATOR_CONFIGS.get(name, [])}
    unknown_params = sorted(set(params) - set(definitions))
    if unknown_params:
        raise ValueError(f"unknown parameter(s) for {name}: {', '.join(unknown_params)}")

    for param_name, value in params.items():
        definition = definitions[param_name]
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"parameter {name}.{param_name} must be numeric")
        if definition["type"] == "int" and not isinstance(value, Integral):
            raise ValueError(f"parameter {name}.{param_name} must be an integer")
        if value < definition["min"] or value > definition["max"]:
            raise ValueError(
                f"parameter {name}.{param_name} must be between "
                f"{definition['min']} and {definition['max']}"
            )
    return {"name": name, "params": dict(params)}


def _engine_indicator_id(entry: dict[str, Any]) -> str:
    values = "_".join(str(value) for value in entry["params"].values())
    return f"{entry['name']}_{values}" if values else entry["name"]


class ChartIndicatorService:
    """Validate exact chart bars and pass them to the Python math authority."""

    def compute(
        self,
        symbol: str,
        bars: list[dict[str, int | float]],
        indicators: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        safe_symbol = validate_symbol(symbol)
        canonical_bars = [
            {
                "timestamp": int(bar["t"]),
                "open": float(bar["o"]),
                "high": float(bar["h"]),
                "low": float(bar["l"]),
                "close": float(bar["c"]),
                "volume": float(bar["v"]),
            }
            for bar in bars
        ]
        assert_canonical_bar_stream(canonical_bars, safe_symbol)
        validated_indicators = [_validate_indicator(indicator) for indicator in indicators]
        frame = pd.DataFrame(
            {
                "timestamp": pd.Series([bar["timestamp"] for bar in canonical_bars], dtype="int64"),
                "open": pd.Series([bar["open"] for bar in canonical_bars], dtype="float64"),
                "high": pd.Series([bar["high"] for bar in canonical_bars], dtype="float64"),
                "low": pd.Series([bar["low"] for bar in canonical_bars], dtype="float64"),
                "close": pd.Series([bar["close"] for bar in canonical_bars], dtype="float64"),
                "volume": pd.Series([bar["volume"] for bar in canonical_bars], dtype="float64"),
            }
        )
        results = compute_indicator_results(frame, validated_indicators)
        missing = [
            entry["name"]
            for entry in validated_indicators
            if not any(
                result["id"] == _engine_indicator_id(entry)
                or result["id"].startswith(f"{_engine_indicator_id(entry)}_")
                for result in results
            )
        ]
        if missing:
            raise ValueError(
                "indicator calculation produced no series for the supplied bars: "
                + ", ".join(missing)
            )
        return safe_symbol, results


_CHART_INDICATOR_SERVICE = ChartIndicatorService()


def get_chart_indicator_service() -> ChartIndicatorService:
    return _CHART_INDICATOR_SERVICE
