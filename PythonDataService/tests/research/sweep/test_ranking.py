"""The ranking contract (PRD #1926 "Ranking contract")."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from app.research.sweep.ranking import RANKING_MEASURES, leader, rank


@dataclass(frozen=True)
class _Cell:
    params_hash: str
    status: str = "completed"
    total_trades: int = 10
    sharpe_ratio: float | None = 1.0
    total_return_pct: float | None = 5.0
    net_profit: float | None = 500.0


@pytest.mark.parametrize(
    ("measure", "expected"),
    [("sharpe_ratio", "b"), ("total_return_pct", "c"), ("net_profit", "a")],
)
def test_each_measure_produces_its_own_leader(measure: str, expected: str) -> None:
    cells = [
        _Cell("a", sharpe_ratio=0.5, total_return_pct=1.0, net_profit=900.0),
        _Cell("b", sharpe_ratio=2.0, total_return_pct=2.0, net_profit=200.0),
        _Cell("c", sharpe_ratio=1.0, total_return_pct=9.0, net_profit=100.0),
    ]

    assert leader(cells, measure, min_trades=1).params_hash == expected  # type: ignore[arg-type]


def test_reversed_candidate_order_produces_the_same_leader_and_order() -> None:
    cells = [_Cell("x", sharpe_ratio=1.0), _Cell("y", sharpe_ratio=1.0), _Cell("z", sharpe_ratio=1.0, total_return_pct=6.0)]

    forward = [cell.params_hash for cell in rank(cells, "sharpe_ratio", min_trades=1)]
    backward = [cell.params_hash for cell in rank(list(reversed(cells)), "sharpe_ratio", min_trades=1)]

    # z wins on total return; x and y tie on both measures and settle on params_hash ascending.
    assert forward == backward == ["z", "x", "y"]


def test_zero_trade_failed_and_non_finite_cells_never_lead() -> None:
    cells = [
        _Cell("zero", total_trades=0, sharpe_ratio=99.0),
        _Cell("failed", status="failed", sharpe_ratio=99.0),
        _Cell("nan", sharpe_ratio=math.nan),
        _Cell("inf", sharpe_ratio=math.inf),
        _Cell("null", sharpe_ratio=None),
        _Cell("ok", sharpe_ratio=0.1),
    ]

    assert leader(cells, "sharpe_ratio", min_trades=1).params_hash == "ok"
    assert [cell.params_hash for cell in rank(cells, "sharpe_ratio", min_trades=1)] == ["ok"]


def test_a_search_with_no_eligible_cell_reports_no_leader() -> None:
    assert leader([_Cell("a", total_trades=2)], "sharpe_ratio", min_trades=5) is None


def test_the_minimum_trade_count_gates_eligibility() -> None:
    cells = [_Cell("thin", total_trades=4, sharpe_ratio=9.0), _Cell("thick", total_trades=5, sharpe_ratio=1.0)]

    assert leader(cells, "sharpe_ratio", min_trades=5).params_hash == "thick"


def test_the_measure_vocabulary_is_closed() -> None:
    assert RANKING_MEASURES == ("sharpe_ratio", "total_return_pct", "net_profit")
    with pytest.raises(ValueError, match="unknown ranking measure"):
        rank([], "profit_factor", min_trades=1)  # type: ignore[arg-type]
