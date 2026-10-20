"""The final test against development (#2821): hand-worked rates, ``atol=1e-9``."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pytest

from app.research.golden_search.final_measures import annualized_return, final_comparison
from tests._helpers.golden_search import metrics


@pytest.mark.parametrize(
    ("observed", "years", "rate"),
    [
        # 1.21 over two years is 10% a year; 10% over half a year is 21% a year.
        pytest.param(metrics(1.0, total_return=0.21), Fraction(2), pytest.approx(0.1, abs=1e-9, rel=0), id="a-return-over-two-trading-years-is-its-square-root-rate"),
        pytest.param(metrics(1.0, total_return=0.1), Fraction(1, 2), pytest.approx(0.21, abs=1e-9, rel=0), id="a-return-over-half-a-year-is-its-square"),
        # 0^(1/2) − 1: the whole account lost is −100% a year over any span.
        pytest.param(metrics(1.0, total_return=-1.0), Fraction(2), pytest.approx(-1.0, abs=1e-9, rel=0), id="a-wiped-out-account-loses-all-of-it-a-year"),
        pytest.param(metrics(1.0, total_return=-1.2), Fraction(1), None, id="an-impossible-run-has-no-rate"),
        pytest.param(metrics(1.0, total_return=None), Fraction(1), None, id="a-missing-return-has-no-rate"),
        pytest.param(metrics(None, total_return=None, status="failed"), Fraction(1), None, id="a-failed-run-has-no-rate"),
        pytest.param(None, Fraction(1), None, id="a-missing-run-has-no-rate"),
    ],
)
def test_annualized_return_is_the_yearly_rate_of_a_return_over_its_span_and_none_for_a_failed_missing_or_impossible_run(observed: Any, years: Fraction, rate: Any) -> None:
    assert annualized_return(None if observed is None else observed.as_dict(), years) == rate, "the yearly rate of the run over its span"


def test_each_measure_sits_beside_its_development_value_with_the_change() -> None:
    development = metrics(1.5, trades=100, total_return=0.21, drawdown=0.08).as_dict()
    final = metrics(0.9, trades=20, total_return=0.05, drawdown=0.03).as_dict()
    rows = {row["key"]: row for row in final_comparison(development, final, development_years=Fraction(2), final_years=Fraction(1, 2))}

    assert (rows["annualized_return"]["development"], rows["annualized_return"]["final"]) == pytest.approx((0.1, 1.05**2 - 1), abs=1e-9, rel=0)
    assert (rows["sharpe_ratio"]["change"], rows["max_drawdown_pct"]["change"]) == pytest.approx((-0.6, -0.05), abs=1e-9, rel=0)
    # 100 trades over two years is 50 a year; 20 over half a year is 40.
    assert (rows["trades_per_year"]["development"], rows["trades_per_year"]["final"], rows["trades_per_year"]["change"]) == pytest.approx((50.0, 40.0, -10.0), abs=1e-9, rel=0)
    assert list(rows) == ["annualized_return", "sharpe_ratio", "max_drawdown_pct", "trades_per_year"]


def test_a_failed_development_run_leaves_every_change_empty() -> None:
    rows = final_comparison(metrics(None, status="failed").as_dict(), metrics(0.9).as_dict(), development_years=Fraction(1), final_years=Fraction(1))
    assert all(row["development"] is None and row["change"] is None for row in rows)


def test_a_final_window_without_a_session_has_no_yearly_rate_whatever_the_run() -> None:
    # A legacy fixed-floor plan can freeze a final window with no session: its rates read none, completed or failed.
    for final in (metrics(0.9).as_dict(), metrics(None, status="failed").as_dict()):
        rows = {row["key"]: row for row in final_comparison(metrics(1.5).as_dict(), final, development_years=Fraction(1), final_years=Fraction(0))}
        assert rows["annualized_return"]["final"] is None and rows["trades_per_year"]["final"] is None
        assert rows["annualized_return"]["change"] is None
