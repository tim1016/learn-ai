"""Compare chart measures (#2821): the numbers the Compare charts plot beside each candidate's stored evidence.

Formula:
  trades per trading year = development trades / the development window's
    trading years (each year's selected NYSE sessions over that year's
    scheduled sessions, summed, as ``activity.trading_years``);
  return change = a run's net return - its reference run's net return, both
    fractions of starting capital (a neighbor against its candidate, a stress
    run against the unstressed development run);
  stress runs in profit = the stress runs that completed with net profit > 0.
Reference: ADR 0074 decision 9 (trading years, as the activity floors count
  them); PRD https://github.com/tim1016/learn-ai/issues/2821 (charts V21-V23).
Canonical implementation: this file; trading years in
  app/research/golden_search/activity.py.
Validated against: tests/research/golden_search/test_compare_measures.py.

A value the study did not record stays ``None``: a failed or missing run has
no return change and no trades per year, never zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any, Literal

Step = Literal[-1, 0, 1]


def completed_net(metrics: Mapping[str, Any] | None) -> float | None:
    """A completed run's net profit; None for a failed or missing run, or one the engine left undefined."""
    if metrics is None or metrics.get("status") != "completed":
        return None
    net = metrics.get("net_profit")
    return None if net is None else float(net)


def _completed_return(metrics: Mapping[str, Any] | None) -> float | None:
    if metrics is None or metrics.get("status") != "completed":
        return None
    value = metrics.get("total_return_pct")
    return None if value is None else float(value)


def trades_per_year(metrics: Mapping[str, Any] | None, years: Fraction) -> float | None:
    """A completed run's trades per trading year of its window; None for a failed or missing run."""
    if metrics is None or metrics.get("status") != "completed":
        return None
    if years <= 0:
        raise ValueError("A run's window must hold at least one trading session.")
    return float(Fraction(int(metrics["total_trades"])) / years)


def return_change(run: Mapping[str, Any] | None, reference: Mapping[str, Any] | None) -> float | None:
    """``run``'s net return less ``reference``'s; None unless both runs completed with a return."""
    value, base = _completed_return(run), _completed_return(reference)
    return None if value is None or base is None else value - base


def neighborhood_view(hood: Mapping[str, Any]) -> dict[str, Any]:
    """A stored neighborhood with each row's step from the center (-1, 0, +1) and its change in net return."""
    center = next((row for row in hood["rows"] if row["status"] == "center"), None)
    if center is None:
        raise ValueError(f"The neighborhood on {hood['knob']!r} has no center row.")
    rows = []
    for row in hood["rows"]:
        step: Step = 0 if row is center else _step(float(row["value"]), float(center["value"]), hood["knob"])
        change = None if row is center else return_change(row["metrics"], center["metrics"])
        rows.append({**row, "step": step, "return_change": change})
    return {**hood, "rows": rows}


def _step(value: float, center: float, knob: str) -> Step:
    if value == center:
        raise ValueError(f"A neighbor on {knob!r} sits at the center value {center}.")
    return -1 if value < center else 1


def stress_view(result: Mapping[str, Any], development: Mapping[str, Any] | None) -> dict[str, Any]:
    """A stored stress run with its change in net return from the unstressed development run."""
    return {**result, "return_change": return_change(result["metrics"], development)}


def stress_tally(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many stress runs made money, how many recorded a result, and how many the plan scheduled."""
    nets = [completed_net(result["metrics"]) for result in results]
    return {
        "in_profit": sum(1 for net in nets if net is not None and net > 0),
        "recorded": sum(1 for net in nets if net is not None),
        "scenarios": len(results),
    }
