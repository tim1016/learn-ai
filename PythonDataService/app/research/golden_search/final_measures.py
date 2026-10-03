"""The final decision's comparison: each measure of a run on the development period beside the same on the final test (#2821).

Formula, for one candidate's development run D and final-test run F, with
y_D and y_F their windows' trading years (``activity.trading_years``, as the
receipt froze them on a frequency plan, else the calendar's):
  * annualized return = (1 + r)^(1 / y) − 1 for a run's net return r over its
    window's y trading years; none when r is missing or r ≤ −1 (a loss of the
    whole account has no annual rate);
  * trades per trading year = trades / y (``compare_measures.trades_per_year``);
  * Sharpe and worst fall as the engine reported them;
  * change = final − development, none unless both exist.
A failed or missing run has no measures, never zero.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2821 "Server work by
  chart" (V33); ADR 0074 decision 9 (trading years).
Canonical implementation: this file; trades per year in compare_measures.py.
Validated against: tests/research/golden_search/test_final_measures.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from typing import Any

from app.research.golden_search.compare_measures import trades_per_year


def _completed(metrics: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    return metrics if metrics is not None and metrics.get("status") == "completed" else None


def annualized_return(metrics: Mapping[str, Any] | None, years: Fraction) -> float | None:
    """A completed run's net return as a yearly rate over its window's trading years."""
    run = _completed(metrics)
    if run is None or run.get("total_return_pct") is None:
        return None
    total = float(run["total_return_pct"])
    if total <= -1.0:
        return None
    if years <= 0:
        raise ValueError("A run's window must hold at least one trading session.")
    return (1.0 + total) ** (1.0 / float(years)) - 1.0


def _value(metrics: Mapping[str, Any] | None, key: str) -> float | None:
    run = _completed(metrics)
    value = None if run is None else run.get(key)
    return None if value is None else float(value)


def _measure(key: str, label: str, development: float | None, final: float | None) -> dict[str, Any]:
    return {"key": key, "label": label, "development": development, "final": final, "change": None if development is None or final is None else final - development}


def final_comparison(
    development: Mapping[str, Any] | None, final: Mapping[str, Any] | None, *, development_years: Fraction, final_years: Fraction
) -> list[dict[str, Any]]:
    """Each measure on the development period and on the final test, with the change between them."""
    return [
        _measure("annualized_return", "Annualized return", annualized_return(development, development_years), annualized_return(final, final_years)),
        _measure("sharpe_ratio", "Sharpe", _value(development, "sharpe_ratio"), _value(final, "sharpe_ratio")),
        _measure("max_drawdown_pct", "Worst fall", _value(development, "max_drawdown_pct"), _value(final, "max_drawdown_pct")),
        _measure("trades_per_year", "Trades a trading year", trades_per_year(development, development_years), trades_per_year(final, final_years)),
    ]
