"""Selection eligibility and the single-measure ranking contract (PRD #2696 "Weighted places")."""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from app.research.golden_search.protocol import SelectionPolicy
from app.research.golden_search.selection import Candidate, Metrics, best, ineligibility, objective_value
from tests._helpers.golden_search import metrics

_POLICY = SelectionPolicy(objective="sharpe_ratio", min_trades=30, max_drawdown_ceiling=0.2, require_positive_net=True)


@pytest.mark.parametrize(
    ("observed", "code"),
    [
        (metrics(1.0, status="failed"), "FAILED"),
        (metrics(1.0, trades=0), "NO_TRADES"),
        (metrics(1.0, trades=29), "TOO_FEW_TRADES"),
        (metrics(None), "OBJECTIVE_UNDEFINED"),
        (metrics(math.nan), "OBJECTIVE_UNDEFINED"),
        (metrics(math.inf), "OBJECTIVE_UNDEFINED"),
        (metrics(1.0, drawdown=None), "DRAWDOWN_UNDEFINED"),
        (metrics(1.0, drawdown=0.2000001), "DRAWDOWN_ABOVE_CEILING"),
        (metrics(1.0, net=0.0), "NOT_PROFITABLE"),
        (metrics(1.0, net=None), "NOT_PROFITABLE"),
        (metrics(1.0, trades=30, drawdown=0.2, net=0.01), None),
    ],
)
def test_ineligibility_reports_the_first_failed_rule(observed: Metrics, code: str | None) -> None:
    assert ineligibility(observed, _POLICY) == code


def test_ineligibility_allows_a_loss_when_the_policy_does_not_require_profit() -> None:
    policy = SelectionPolicy(require_positive_net=False)

    assert ineligibility(metrics(1.0, net=-10.0), policy) is None


def test_objective_value_reads_the_policy_measure() -> None:
    observed = metrics(1.5, net=250.0, total_return=0.0025)

    assert objective_value(observed, SelectionPolicy(objective="sharpe_ratio")) == 1.5
    assert objective_value(observed, SelectionPolicy(objective="net_profit")) == 250.0
    assert objective_value(observed, SelectionPolicy(objective="total_return_pct")) == 0.0025


def test_best_orders_by_objective_then_return_then_hash_whatever_the_input_order() -> None:
    cells = [
        Candidate("c", {"k": 3}, metrics(2.0, total_return=0.01)),
        Candidate("b", {"k": 2}, metrics(2.0, total_return=0.03)),
        Candidate("a", {"k": 1}, metrics(2.0, total_return=0.03)),
        Candidate("d", {"k": 4}, metrics(1.0, total_return=0.50)),
        Candidate("z", {"k": 9}, metrics(9.0, trades=5)),  # best objective, but ineligible
    ]

    assert best(cells, _POLICY).point_hash == "a"  # type: ignore[union-attr]
    assert best(list(reversed(cells)), _POLICY).point_hash == "a"  # type: ignore[union-attr]


def test_best_returns_none_when_nothing_is_eligible() -> None:
    assert best([Candidate("a", {}, metrics(1.0, trades=0))], _POLICY) is None
    assert best([], _POLICY) is None


def _competition_ranks(values: Sequence[float]) -> list[float]:
    """Place of each value, higher is better; ties share the average place."""
    ranks = []
    for value in values:
        better = sum(1 for other in values if other > value)
        tied = sum(1 for other in values if other == value)
        ranks.append(better + (tied + 1) / 2)
    return ranks


def _blended_places_choice(options: dict[str, tuple[float, float, float]], weights: tuple[float, float, float]) -> str:
    names = list(options)
    per_metric = [_competition_ranks([options[name][i] for name in names]) for i in range(3)]
    score = {name: sum(weights[i] * per_metric[i][j] for i in range(3)) for j, name in enumerate(names)}
    return min(names, key=lambda name: (score[name], name))


def test_best_resists_the_rank_reversal_that_breaks_blended_places() -> None:
    # (net profit, Sharpe, profit/drawdown) from the methodology note; weights 50/30/20.
    weights = (0.5, 0.3, 0.2)
    a, b = (10.0, 1.0, 1.0), (9.0, 2.0, 2.0)
    dominated_by_a, dominated_by_b = (9.5, 0.5, 0.5), (8.0, 1.5, 1.5)

    with_c = {"A": a, "B": b, "C": dominated_by_a}
    with_d = {"A": a, "B": b, "D": dominated_by_b}

    # Adding an option dominated by A makes A win; adding one dominated by B makes B win.
    assert _blended_places_choice(with_c, weights) == "A"
    assert _blended_places_choice(with_d, weights) == "B"

    def single_measure(options: dict[str, tuple[float, float, float]]) -> str:
        cells = [Candidate(name, {}, metrics(sharpe, net=net)) for name, (net, sharpe, _) in options.items()]
        winner = best(cells, SelectionPolicy(objective="sharpe_ratio", min_trades=1))
        assert winner is not None
        return winner.point_hash

    # One declared measure: A's and B's own numbers decide, whatever else is in the set.
    assert single_measure(with_c) == single_measure(with_d) == "B"


def test_metrics_round_trip_through_their_dict() -> None:
    observed = Metrics.failed("engine refused the window")

    assert Metrics.from_dict(observed.as_dict()) == observed
