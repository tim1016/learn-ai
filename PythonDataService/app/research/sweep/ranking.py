"""The ranking contract every sweep leader and fold winner is chosen by.

Formula: among ELIGIBLE cells — status ``completed``, ``total_trades >=
min_trades``, and a finite, non-null value of the selected measure — order
by (1) the selected measure descending, (2) ``total_return_pct`` descending,
(3) ``params_hash`` ascending. Allowed measures, exhaustively:
``sharpe_ratio``, ``total_return_pct``, ``net_profit``; all are maximized,
so no direction flag is carried. ``params_hash`` is a total order over
distinct candidates, so the key is total and the leader is unique;
declaration order is used at no level because it is not stable across
input orderings. A zero-trade, failed, or non-finite cell is ineligible to
lead — never sorted to the bottom.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1926 "Ranking
  contract"; PRD #1925 reuses it unchanged for fold-winner selection.
Canonical implementation: this file.
Validated against: tests/research/sweep/test_ranking.py.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Literal, Protocol

RankingMeasure = Literal["sharpe_ratio", "total_return_pct", "net_profit"]
RANKING_MEASURES: tuple[RankingMeasure, ...] = ("sharpe_ratio", "total_return_pct", "net_profit")


class RankableCell(Protocol):
    """Read-only view of a cell; frozen dataclasses with narrower field types satisfy it."""

    @property
    def params_hash(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def total_trades(self) -> int: ...
    @property
    def sharpe_ratio(self) -> float | None: ...
    @property
    def total_return_pct(self) -> float | None: ...
    @property
    def net_profit(self) -> float | None: ...



def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value


def measure_value(cell: RankableCell, measure: RankingMeasure) -> float | None:
    return _finite(getattr(cell, measure))


def is_eligible(cell: RankableCell, measure: RankingMeasure, *, min_trades: int) -> bool:
    return cell.status == "completed" and cell.total_trades > 0 and cell.total_trades >= min_trades and measure_value(cell, measure) is not None


def ranking_key(cell: RankableCell, measure: RankingMeasure) -> tuple[float, float, str]:
    """Sort key for an eligible cell; smaller sorts first."""
    primary = measure_value(cell, measure)
    assert primary is not None, "ranking_key requires an eligible cell"
    secondary = _finite(cell.total_return_pct)
    return (-primary, -(secondary if secondary is not None else -math.inf), cell.params_hash)


def rank[C: RankableCell](cells: Iterable[C], measure: RankingMeasure, *, min_trades: int) -> list[C]:
    """Eligible cells, best first, under the full ordering key."""
    if measure not in RANKING_MEASURES:
        raise ValueError(f"unknown ranking measure {measure!r}; allowed: {RANKING_MEASURES}")
    eligible = [cell for cell in cells if is_eligible(cell, measure, min_trades=min_trades)]
    return sorted(eligible, key=lambda cell: ranking_key(cell, measure))


def leader[C: RankableCell](cells: Iterable[C], measure: RankingMeasure, *, min_trades: int) -> C | None:
    """The unique leader, or ``None`` when no cell is eligible."""
    ranked = rank(cells, measure, min_trades=min_trades)
    return ranked[0] if ranked else None
