"""The session-close drawdown reading: hand-computed depths, boundaries and availability (#3013)."""

from __future__ import annotations

import copy
from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.research.golden_search.chance import MEAN_BLOCK_SESSIONS, REPLICATIONS
from app.research.golden_search.chance_drawdown import NOT_MEASURED, count_falls, drawdown_reading, stored_drawdown
from app.research.golden_search.selection import Metrics
from app.research.monte_carlo.stationary import stationary_index_chunks
from app.utils.session_anchors import et_midnight_ms

CAPITAL = 100_000.0


def _completed(*, drawdown: float | None = 0.1) -> Metrics:
    return Metrics(status="completed", total_trades=40, net_profit=1_000.0, total_return_pct=0.01, sharpe_ratio=1.0, max_drawdown_pct=drawdown, win_rate=0.5)


def _sessions(n: int) -> tuple[int, ...]:
    return tuple(session_close_ms_utc(day) for day in expected_sessions(date(2025, 1, 2), date(2027, 12, 31))[:n])


def _detail(returns: list[float], *, capital: float = CAPITAL) -> dict[str, Any]:
    session_ms = _sessions(len(returns))
    equity = [capital]
    for value in returns:  # compounded, as the engine's daily equity curve is
        equity.append(equity[-1] * (1.0 + value))
    return {
        "initial_cash": capital,
        "daily_equity": [[ms, value] for ms, value in zip(session_ms, equity[1:], strict=True)],
        "trades": [],
    }


def _window_for(n: int) -> tuple[int, int]:
    days = expected_sessions(date(2025, 1, 2), date(2027, 12, 31))[:n]
    return et_midnight_ms(days[0]), et_midnight_ms(days[-1] + timedelta(days=1))


def _smooth(n: int, seed: int) -> list[float]:
    return list(np.random.default_rng(seed).normal(0.0005, 0.01, n))


def _reading(returns: list[float], *, ceiling: float = 0.20, metrics: Metrics | None = None, point_hash: str = "cafebabe", **kwargs: Any) -> dict[str, Any]:
    return drawdown_reading(
        window=_window_for(len(returns)),
        ceiling=ceiling,
        metrics=_completed() if metrics is None else metrics,
        detail=_detail(returns),
        point_hash=point_hash,
        **kwargs,
    )


def test_a_hand_computed_path_finds_its_own_close_only_depth() -> None:
    path = [0.10, -0.20, 0.05, *([0.0] * 57)]  # equity 1 -> 1.1 -> 0.88 -> 0.924, then flat: the deepest fall is 0.2 off the 1.1 peak
    reading = _reading(path)
    assert reading["recorded_close_only"] == pytest.approx(0.2, abs=1e-12)


def test_the_initial_capital_sits_in_the_initial_peak() -> None:
    path = [-0.10, 0.05, *([0.0] * 58)]
    reading = _reading(path)
    # The engine's own peak starts at the first bar (0.9), so it misses this fall; this reading counts it from capital 1.0.
    assert reading["recorded_close_only"] == pytest.approx(0.10, abs=1e-12)


def test_a_positive_only_series_falls_not_at_all() -> None:
    reading = _reading([0.01] * 60)
    assert reading["recorded_close_only"] == 0.0
    assert reading["counts"]["within_ceiling"] == 10_000
    assert reading["counts"]["beyond_ceiling"] == 0
    assert reading["percentiles"]["p95"] == 0.0


def test_the_percentiles_interpolate_by_hand_computed_positions() -> None:
    from app.research.golden_search.chance_drawdown import _percentiles

    values = np.array([0.10, 0.20, 0.30, np.nan, 0.40, 0.50])
    result = _percentiles(values)
    finite = np.sort(np.array([0.10, 0.20, 0.30, 0.40, 0.50]))
    # (n - 1) q by hand: p50 at position 2.0 exactly; p95 at position 3.8, between the 4th and 5th values.
    expected_p50 = float(finite[2])
    expected_p95 = float(finite[3] + (finite[4] - finite[3]) * 0.8)
    assert result["finite"] == 5
    assert result["p50"] == pytest.approx(expected_p50, abs=1e-15)
    assert result["p95"] == pytest.approx(expected_p95, abs=1e-15)


def test_ceiling_equality_is_within_a_strict_breach_is_a_concern() -> None:
    # At the counting seam the boundary is exact: a fall exactly at the ceiling counts within, never beyond.
    assert count_falls(np.array([0.20, 0.20, 0.19999]), 0.20) == {"beyond_ceiling": 0, "within_ceiling": 3, "undefined": 0}
    assert count_falls(np.array([0.2000001]), 0.20) == {"beyond_ceiling": 1, "within_ceiling": 0, "undefined": 0}
    assert count_falls(np.array([np.nan, 0.1]), 0.20) == {"beyond_ceiling": 0, "within_ceiling": 1, "undefined": 1}
    # On a full reading, one -25% session among flat ones puts the 95th percentile beyond a 20% ceiling (paths draw it
    # once or more; a -20% session at a 20% ceiling keeps single draws within it).
    deeper = [0.0] * 59
    deeper[30] = -0.25
    breaching = _reading(deeper, ceiling=0.20)
    assert breaching["concern"] is True
    assert "research concern" in breaching["sentence"]


def test_the_sentence_names_the_ceiling_at_the_precision_the_exam_check_shows_it() -> None:
    # The ceiling is free in (0, 1]: a 12.5% ceiling is never rounded to "12%" (exam_rules prints it with two decimals).
    deeper = [0.0] * 59
    deeper[30] = -0.25
    beyond = _reading(deeper, ceiling=0.125)
    assert beyond["concern"] is True and "beyond the 12.50% ceiling" in beyond["sentence"]
    within = _reading([0.0] * 59, ceiling=0.10)
    assert within["concern"] is False and "within the 10.00% ceiling" in within["sentence"]


def test_a_deterministic_alternating_series_states_its_actual_outcomes_not_a_promise() -> None:
    # Exactly one losing session of -10% among flat ones: a path's fall is 0.1 iff it draws that session.
    losing_session = 30
    returns = [0.0] * 59
    returns[losing_session] = -0.10
    reading = _reading(returns, ceiling=0.05)
    # The expected count is read off the reading's own stored seed: the paths that include the losing session.
    rng = np.random.default_rng(reading["basis"]["seed"]["value"])
    chunks = stationary_index_chunks(len(returns), size=len(returns), mean_block=MEAN_BLOCK_SESSIONS, replications=REPLICATIONS, rng=rng)
    drawn = sum(int(np.count_nonzero((chunk == losing_session).any(axis=1))) for chunk in chunks)
    # Never an assumed one-in-twenty: the share is whatever the stored draw actually reaches.
    assert 0 < drawn < REPLICATIONS
    assert reading["counts"] == {"beyond_ceiling": drawn, "within_ceiling": REPLICATIONS - drawn, "undefined": 0}


def test_the_engine_figure_is_carried_verbatim_and_a_missing_one_is_named() -> None:
    reading = _reading(_smooth(60, 2), metrics=_completed(drawdown=0.123))
    assert reading["recorded_engine_per_bar"] == 0.123
    none_recorded = _reading(_smooth(60, 3), metrics=_completed(drawdown=None))
    assert none_recorded["recorded_engine_per_bar"] is None


def test_the_reading_needs_only_its_own_source() -> None:
    missing_run = drawdown_reading(
        window=_window_for(60),
        ceiling=0.2,
        metrics=None,
        detail=None,
        point_hash="cafebabe",
    )
    assert missing_run["status"] == "missing"
    assert missing_run["code"] == "not_evaluated"
    measured = _reading(_smooth(60, 4))
    assert measured["status"] == "measured"
    assert measured["basis"]["horizon"]["scheduled_sessions"] == 60
    assert measured["basis"]["thresholds"] == {"ceiling": 0.2}


def test_identical_point_hashes_share_one_reading_and_determinism_holds() -> None:
    first = _reading(_smooth(60, 5), point_hash="twin")
    second = _reading(_smooth(60, 5), point_hash="twin")
    other = _reading(_smooth(60, 6), point_hash="other")
    assert first == second
    assert first != other


def test_cancellation_propagates_and_never_becomes_a_reading() -> None:
    from app.jobs.progress import JobCancelled

    def cancelled() -> None:
        raise JobCancelled("job 11 cancelled")

    with pytest.raises(JobCancelled):
        _reading(_smooth(60, 7), cancel_check=cancelled)


def test_stored_normalization_keeps_partial_records_partial() -> None:
    measured = _reading(_smooth(60, 8))
    partial = stored_drawdown({"chance": {"drawdown": copy.deepcopy(measured)}})
    assert partial == measured
    # A measured record that fails the served contract reads unavailable instead of failing the study page.
    torn = copy.deepcopy(measured)
    del torn["percentiles"]
    assert stored_drawdown({"chance": {"drawdown": torn}})["code"] == "invalid_record"
    legacy = stored_drawdown({"chance": {}})
    assert legacy["code"] == NOT_MEASURED[0]
    unsupported = stored_drawdown({"chance": {"drawdown": {"status": "from_the_future"}}})
    assert unsupported["code"] == "unsupported"


def test_json_holds_no_nan_or_infinity() -> None:
    from app.research.golden_search.protocol import canonical_json

    canonical_json(_reading(_smooth(60, 9)))


def test_the_horizon_is_the_source_windows_own_session_count() -> None:
    reading = _reading(_smooth(84, 10))
    assert reading["basis"]["horizon"]["scheduled_sessions"] == 84
    assert reading["basis"]["horizon"]["window"] == {"start_ms": _window_for(84)[0], "end_ms": _window_for(84)[1]}
