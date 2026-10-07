"""Chance measures: the engine's own series and statistics, on built runs."""

from __future__ import annotations

import hashlib
import math
from datetime import date
from typing import Any

import numpy as np
import pytest

from app.engine.results import statistics
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.research.golden_search.chance import (
    MIN_SESSIONS,
    NO_TRADE_LIST,
    NOT_FINITE,
    NOT_POSITIVE_CAPITAL,
    OUT_OF_ORDER,
    REACHED_ZERO,
    UNPLACED_EXIT,
    path_net_return,
    path_objective,
    path_sharpe,
    path_worst_fall,
    reading_basis,
    seed_for,
    session_series,
)

CAPITAL = 100_000.0


def _closes(count: int) -> list[int]:
    """The session closes of the first ``count`` NYSE sessions from 2025."""
    return [session_close_ms_utc(day) for day in expected_sessions(date(2025, 1, 2), date(2027, 12, 31))[:count]]


def _detail(equity: list[float], *, exits: tuple[int, ...] = (), capital: float = CAPITAL) -> dict[str, Any]:
    """A detail payload over the first ``len(equity)`` NYSE sessions from 2025; ``exits`` are session positions."""
    closes = _closes(len(equity))
    return {
        "initial_cash": capital,
        "daily_equity": [[ms, value] for ms, value in zip(closes, equity, strict=True)],
        "trades": [{"exit_ms": closes[position] - 60_000} for position in exits],
    }


def _equity(n: int, seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(CAPITAL * np.cumprod(1.0 + rng.normal(0.0005, 0.01, n)))


def _with_equity_at(position: int, value: float) -> dict[str, Any]:
    equity = _equity(30, seed=6)
    equity[position] = value
    return _detail(equity)


def _reordered(first: int, second: int) -> dict[str, Any]:
    """A run whose daily equity gives session ``second``'s close where session ``first``'s belongs."""
    detail = _detail(_equity(30, seed=8))
    detail["daily_equity"][first][0] = detail["daily_equity"][second][0]
    return detail


def test_session_series_returns_are_the_engines_daily_returns() -> None:
    equity = _equity(60, seed=1)
    series = session_series(_detail(equity))
    assert not isinstance(series, str)
    assert series.session_ms == tuple(_closes(60))
    assert np.allclose(series.returns, statistics._daily_returns(equity, initial_cash=CAPITAL), atol=1e-15, rtol=0.0)


def test_path_sharpe_is_the_engines_sharpe() -> None:
    returns = np.array(statistics._daily_returns(_equity(60, seed=2), initial_cash=CAPITAL))
    engine = statistics._sharpe(list(returns), statistics.TRADING_DAYS_PER_YEAR)
    assert engine is not None
    # numpy sums pairwise and the engine sums in order; the two differ in the last bits only.
    assert np.isclose(path_sharpe(returns[None, :])[0], engine, atol=1e-12, rtol=0.0)


def test_path_sharpe_is_undefined_when_every_return_is_equal() -> None:
    assert np.isnan(path_sharpe(np.zeros((1, 30)))[0])
    assert np.isnan(path_sharpe(np.full((1, 30), 0.001))[0])
    assert np.isnan(path_sharpe(np.array([[0.01]]))[0])


def test_path_worst_fall_is_the_engines_drawdown_of_the_session_closes() -> None:
    equity = _equity(60, seed=7)
    series = session_series(_detail(equity))
    assert not isinstance(series, str)
    engine = statistics._max_drawdown([CAPITAL, *equity])
    # Ours compounds the returns back into a curve; the engine reads the curve itself.
    assert np.isclose(path_worst_fall(series.returns[None, :])[0], engine, atol=1e-12, rtol=0.0)


def test_a_runs_own_sessions_in_order_give_back_its_net_result() -> None:
    equity = _equity(60, seed=9)
    series = session_series(_detail(equity))
    assert not isinstance(series, str)
    own_path = series.returns[None, :]
    assert np.isclose(path_net_return(own_path)[0], equity[-1] / CAPITAL - 1.0, atol=1e-12, rtol=0.0)
    assert np.isclose(path_objective(own_path, "net_profit", series.capital)[0], equity[-1] - CAPITAL, atol=1e-6, rtol=0.0)


def test_path_statistics_on_a_hand_computed_path() -> None:
    path = np.array([[0.10, -0.20, 0.05]])  # equity 1 -> 1.1 -> 0.88 -> 0.924
    assert np.isclose(path_net_return(path)[0], -0.076, atol=1e-12, rtol=0.0)
    assert np.isclose(path_worst_fall(path)[0], 0.2, atol=1e-12, rtol=0.0)
    assert np.isclose(path_objective(path, "total_return_pct", CAPITAL)[0], -0.076, atol=1e-12, rtol=0.0)
    assert np.isclose(path_objective(path, "net_profit", CAPITAL)[0], -7_600.0, atol=1e-7, rtol=0.0)


def test_path_worst_fall_counts_a_fall_from_the_starting_capital() -> None:
    assert np.isclose(path_worst_fall(np.array([[-0.10, 0.05]]))[0], 0.10, atol=1e-12, rtol=0.0)


@pytest.mark.parametrize("objective", ["sharpe_ratio", "total_return_pct", "net_profit"])
def test_path_objective_reads_an_overflow_as_undefined_never_as_infinite(objective: str) -> None:
    runaway = np.array([[1e200, -0.5, 1e200]])  # compounds past the largest float
    value = path_objective(runaway, objective, CAPITAL)  # type: ignore[arg-type]
    assert np.isnan(value[0])
    # Undefined loses every comparison: it can neither be at least a rival's objective nor count as money made.
    assert not (value >= -math.inf).any() and not (value > 0).any()
    assert not (path_worst_fall(runaway) <= 0.20).any()


def test_path_objective_refuses_an_objective_it_cannot_resample() -> None:
    with pytest.raises(ValueError, match="sortino"):
        path_objective(np.array([[0.01, 0.02]]), "sortino", CAPITAL)  # type: ignore[arg-type]


def test_session_series_counts_each_trade_in_the_session_it_exited() -> None:
    series = session_series(_detail(_equity(30, seed=3), exits=(0, 4, 4, 29)))
    assert not isinstance(series, str)
    assert series.exits.sum() == 4 and series.exits[4] == 2 and series.exits[29] == 1


def test_session_series_cannot_be_altered_once_read() -> None:
    series = session_series(_detail(_equity(30, seed=11), exits=(3,)))
    assert not isinstance(series, str)
    with pytest.raises(ValueError, match="read-only"):
        series.returns[0] = 0.0
    with pytest.raises(ValueError, match="read-only"):
        series.exits[3] += 1


def test_session_series_needs_a_trade_list_and_enough_sessions() -> None:
    # Every run keeps a daily curve, but only a detail payload says when each trade exited.
    assert session_series(None) == NO_TRADE_LIST
    short = session_series(_detail(_equity(MIN_SESSIONS - 1, seed=4)))
    assert isinstance(short, str) and str(MIN_SESSIONS - 1) in short and str(MIN_SESSIONS) in short
    assert not isinstance(session_series(_detail(_equity(MIN_SESSIONS, seed=4))), str)


@pytest.mark.parametrize("capital", [0.0, -1.0, math.nan, math.inf])
def test_session_series_refuses_capital_that_is_not_a_positive_amount(capital: float) -> None:
    assert session_series(_detail(_equity(30, seed=5), capital=capital)) == NOT_POSITIVE_CAPITAL


@pytest.mark.parametrize("position", [0, 10, 29])
@pytest.mark.parametrize(("value", "reason"), [(math.nan, NOT_FINITE), (math.inf, NOT_FINITE), (0.0, REACHED_ZERO), (-5.0, REACHED_ZERO)])
def test_session_series_refuses_equity_it_cannot_take_returns_of(position: int, value: float, reason: str) -> None:
    # The last session counts too: a resample can put it anywhere on a path.
    assert session_series(_with_equity_at(position, value)) == reason


@pytest.mark.parametrize(("first", "second"), [(5, 6), (5, 9), (9, 5)])
def test_session_series_refuses_sessions_repeated_or_out_of_order(first: int, second: int) -> None:
    assert session_series(_reordered(first, second)) == OUT_OF_ORDER


def test_session_series_refuses_a_trade_that_exited_outside_its_sessions() -> None:
    detail = _detail(_equity(30, seed=10))
    detail["trades"] = [{"exit_ms": _closes(31)[30]}]
    assert session_series(detail) == UNPLACED_EXIT


def test_seed_for_is_the_first_eight_bytes_of_the_canonical_hash() -> None:
    expected = int.from_bytes(hashlib.sha256(b'["a",1,"final"]').digest()[:8], "big")
    assert seed_for("a", 1, "final") == expected
    assert seed_for("a", "final", 1) != expected
    assert math.log2(seed_for("a") + 1) <= 64


def test_reading_basis_names_the_method_and_the_thresholds_a_reading_was_judged_by() -> None:
    # These fix every stored number under the convention's name, so a change here needs a new name with it.
    assert reading_basis() == {
        "convention": "stationary-bootstrap/v1",
        "replications": 10_000,
        "mean_block_sessions": 5.0,
        "clear": 0.95,
        "rehearsal_target": 0.80,
    }
