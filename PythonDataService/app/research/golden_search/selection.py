"""Which evaluations may win a Golden Search procedure, and in what order.

Formula: an evaluation is ELIGIBLE under a frozen policy iff, checked in this
order (the first failure is its reason code):
``FAILED`` status is ``completed``; ``NO_TRADES`` ``total_trades > 0``;
``TOO_FEW_TRADES`` ``total_trades >= min_trades``; ``OBJECTIVE_UNDEFINED``
the objective measure is finite and non-null; ``DRAWDOWN_UNDEFINED``
``max_drawdown_pct`` is finite and non-null; ``DRAWDOWN_ABOVE_CEILING``
``max_drawdown_pct <= max_drawdown_ceiling`` (both fractions of peak
equity); ``NOT_PROFITABLE`` when the policy requires it, ``net_profit > 0``.
A null never passes a check. Eligible evaluations are ordered by the
canonical sweep ranking key — objective descending, ``total_return_pct``
descending, point hash ascending — so the winner is unique and independent
of input order. One declared measure, never a blend of places: adding a
dominated alternative can reverse a rank-blend choice, never this one
(pinned by test). ``min_trades`` is the evaluated window's floor: the plan's
fixed floor, or the one its expected trade frequency froze for that window
(``activity.py``, ADR 0074).
Reference: ``app/research/sweep/ranking.py`` (PRD #1926 "Ranking contract"),
  extended with the frozen policy constraints of PRD #2696 "Weighted places".
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_selection.py.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.research.golden_search.protocol import SelectionPolicy
from app.research.sweep.ranking import measure_value, ranking_key

IneligibilityCode = Literal[
    "FAILED",
    "NO_TRADES",
    "TOO_FEW_TRADES",
    "OBJECTIVE_UNDEFINED",
    "DRAWDOWN_UNDEFINED",
    "DRAWDOWN_ABOVE_CEILING",
    "NOT_PROFITABLE",
]


@dataclass(frozen=True)
class Metrics:
    """What one engine evaluation contributes to selection; ``*_pct`` values are fractions."""

    status: Literal["completed", "failed"]
    total_trades: int
    net_profit: float | None
    total_return_pct: float | None
    sharpe_ratio: float | None
    max_drawdown_pct: float | None
    win_rate: float | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "total_trades": self.total_trades,
            "net_profit": self.net_profit,
            "total_return_pct": self.total_return_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate": self.win_rate,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Metrics:
        return cls(
            status=data["status"],
            total_trades=int(data["total_trades"]),
            net_profit=data["net_profit"],
            total_return_pct=data["total_return_pct"],
            sharpe_ratio=data["sharpe_ratio"],
            max_drawdown_pct=data["max_drawdown_pct"],
            win_rate=data["win_rate"],
            error=data.get("error"),
        )

    @classmethod
    def failed(cls, error: str) -> Metrics:
        return cls(
            status="failed",
            total_trades=0,
            net_profit=None,
            total_return_pct=None,
            sharpe_ratio=None,
            max_drawdown_pct=None,
            win_rate=None,
            error=error,
        )


@dataclass(frozen=True)
class _RankedCell:
    """``Metrics`` seen through the sweep ranking contract's cell protocol."""

    params_hash: str
    status: str
    total_trades: int
    sharpe_ratio: float | None
    total_return_pct: float | None
    net_profit: float | None


def _cell(point_hash: str, metrics: Metrics) -> _RankedCell:
    return _RankedCell(
        params_hash=point_hash,
        status=metrics.status,
        total_trades=metrics.total_trades,
        sharpe_ratio=metrics.sharpe_ratio,
        total_return_pct=metrics.total_return_pct,
        net_profit=metrics.net_profit,
    )


def objective_value(metrics: Metrics, policy: SelectionPolicy) -> float | None:
    """The policy's measure when finite, else ``None`` (``sweep.ranking.measure_value`` semantics)."""
    return measure_value(_cell("", metrics), policy.objective)


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def ineligibility(metrics: Metrics, policy: SelectionPolicy) -> IneligibilityCode | None:
    """The first rule ``metrics`` fails under ``policy``, or ``None`` when it is eligible."""
    if metrics.status != "completed":
        return "FAILED"
    if metrics.total_trades <= 0:
        return "NO_TRADES"
    if policy.min_trades is None:
        raise ValueError("This selection policy has no trade floor; resolve the window's floor from the study receipt first.")
    if metrics.total_trades < policy.min_trades:
        return "TOO_FEW_TRADES"
    if objective_value(metrics, policy) is None:
        return "OBJECTIVE_UNDEFINED"
    drawdown = _finite(metrics.max_drawdown_pct)
    if drawdown is None:
        return "DRAWDOWN_UNDEFINED"
    if drawdown > policy.max_drawdown_ceiling:
        return "DRAWDOWN_ABOVE_CEILING"
    if policy.require_positive_net:
        net = _finite(metrics.net_profit)
        if net is None or net <= 0:
            return "NOT_PROFITABLE"
    return None


@dataclass(frozen=True)
class Candidate:
    """One evaluated point: its hash, its canonical parameters and what the engine reported."""

    point_hash: str
    point: dict[str, Any]
    metrics: Metrics


def best(cells: Sequence[Candidate], policy: SelectionPolicy) -> Candidate | None:
    """The unique eligible winner under the canonical ranking key, or ``None`` when nothing is eligible."""
    eligible = [cell for cell in cells if ineligibility(cell.metrics, policy) is None]
    if not eligible:
        return None
    return min(eligible, key=lambda cell: ranking_key(_cell(cell.point_hash, cell.metrics), policy.objective))
