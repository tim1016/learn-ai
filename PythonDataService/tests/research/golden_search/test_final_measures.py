"""The final test against development (#2821): hand-worked rates, ``atol=1e-9``."""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.research.golden_search.final_measures import annualized_return, final_comparison
from tests._helpers.golden_search import metrics


def test_a_return_over_two_trading_years_is_its_square_root_rate_and_over_half_a_year_its_square() -> None:
    # 1.21 over two years is 10% a year; 10% over half a year is 21% a year.
    assert annualized_return(metrics(1.0, total_return=0.21).as_dict(), Fraction(2)) == pytest.approx(0.1, abs=1e-9, rel=0)
    assert annualized_return(metrics(1.0, total_return=0.1).as_dict(), Fraction(1, 2)) == pytest.approx(0.21, abs=1e-9, rel=0)


def test_a_wiped_out_account_loses_all_of_it_a_year_and_a_failed_missing_or_impossible_run_has_no_rate() -> None:
    # 0^(1/2) − 1: the whole account lost is −100% a year over any span.
    assert annualized_return(metrics(1.0, total_return=-1.0).as_dict(), Fraction(2)) == pytest.approx(-1.0, abs=1e-9, rel=0)
    assert annualized_return(metrics(1.0, total_return=-1.2).as_dict(), Fraction(1)) is None
    assert annualized_return(metrics(1.0, total_return=None).as_dict(), Fraction(1)) is None
    assert annualized_return(metrics(None, total_return=None, status="failed").as_dict(), Fraction(1)) is None
    assert annualized_return(None, Fraction(1)) is None


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
