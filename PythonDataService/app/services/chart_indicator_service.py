"""Transport adapter for canonical chart indicator computation.

This service performs no indicator arithmetic. It validates caller-owned bars,
constructs the canonical DataFrame shape, and delegates all numerical work to
``chart_service.compute_indicator_results``.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.lean_sidecar.workspace import validate_symbol
from app.services.chart_service import compute_indicator_results
from app.services.dataset_service import assert_canonical_bar_stream


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
        return safe_symbol, compute_indicator_results(frame, indicators)


_CHART_INDICATOR_SERVICE = ChartIndicatorService()


def get_chart_indicator_service() -> ChartIndicatorService:
    return _CHART_INDICATOR_SERVICE
