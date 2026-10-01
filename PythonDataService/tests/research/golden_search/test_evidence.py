"""Candidate evidence math and the recommendation copy map (hand-computed values, atol=1e-9, rtol=0)."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime

import numpy as np
import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.research.golden_search.declarations import KnobConstraint
from app.research.golden_search.evidence import (
    CandidateEvidence,
    StressResult,
    daily_equity,
    drawdown_series,
    monthly_results,
    neighborhood,
    pair_map,
    recommendation,
    same_as,
)
from app.research.golden_search.grid_procedure import neighbor_probes, pair_grid
from app.research.golden_search.protocol import KnobPlan, SelectionPolicy
from app.research.walk_forward_study.verdict import Verdict
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_search import declaration, knob, metrics, protocol, synthetic_point

_MINUTE_MS = 60_000


def _utc_ms(*parts: int) -> int:
    return int(datetime(*parts, tzinfo=UTC).timestamp() * 1000)


def test_daily_equity_keeps_each_et_dates_last_point_at_its_session_close() -> None:
    days = [date(2024, 3, 8), date(2024, 3, 11), date(2024, 11, 29)]  # EST, EDT after the DST change, a half-day
    curve = []
    for offset, day in enumerate(days):
        open_ms = session_open_ms_utc(day)
        curve += [
            {"timestamp": open_ms + 15 * _MINUTE_MS, "equity": 100_000.0 + offset, "cash": 0.0},
            {"timestamp": open_ms + 30 * _MINUTE_MS, "equity": 100_500.0 + offset, "cash": 0.0},
        ]

    daily = daily_equity(curve)

    assert daily == [
        (_utc_ms(2024, 3, 8, 21, 0), 100_500.0),  # 16:00 EST
        (_utc_ms(2024, 3, 11, 20, 0), 100_501.0),  # 16:00 EDT
        (_utc_ms(2024, 11, 29, 18, 0), 100_502.0),  # 13:00 EST early close
    ]


def test_daily_equity_refuses_unordered_or_non_session_timestamps() -> None:
    open_ms = session_open_ms_utc(date(2024, 3, 8))

    with pytest.raises(ValueError, match="strictly increase"):
        daily_equity([{"timestamp": open_ms, "equity": 1.0}, {"timestamp": open_ms, "equity": 2.0}])
    with pytest.raises(ValueError, match="int ms UTC"):
        daily_equity([{"timestamp": float(open_ms), "equity": 1.0}])
    with pytest.raises(ValueError, match="not a NYSE session"):
        daily_equity([{"timestamp": _utc_ms(2024, 3, 9, 15, 0), "equity": 1.0}])  # a Saturday


def test_drawdown_series_measures_each_day_against_the_running_peak() -> None:
    daily = [(1, 100.0), (2, 110.0), (3, 99.0), (4, 121.0), (5, 108.9)]

    series = drawdown_series(daily)

    assert [ms for ms, _ in series] == [1, 2, 3, 4, 5]
    # 99/110 - 1 = -0.1; 108.9/121 - 1 = -0.1.
    assert np.allclose([value for _, value in series], [0.0, 0.0, -0.1, 0.0, -0.1], atol=1e-9, rtol=0)
    with pytest.raises(ValueError, match="undefined"):
        drawdown_series([(1, 0.0)])


def test_monthly_results_profit_return_and_trades_per_et_month() -> None:
    daily = [
        (session_close_ms_utc(date(2024, 1, 31)), 101_000.0),
        (session_close_ms_utc(date(2024, 2, 15)), 102_000.0),
        (session_close_ms_utc(date(2024, 2, 29)), 100_500.0),
        (session_close_ms_utc(date(2024, 3, 28)), 103_000.0),
    ]
    exits = [_utc_ms(2024, 1, 10, 15, 0), _utc_ms(2024, 3, 5, 15, 0), _utc_ms(2024, 3, 20, 15, 0)]

    months = monthly_results(daily, 100_000.0, exits)

    assert [m.month_start_ms for m in months] == [et_midnight_ms(date(2024, month, 1)) for month in (1, 2, 3)]
    # Jan 101000 - 100000; Feb 100500 - 101000; Mar 103000 - 100500.
    assert np.allclose([m.net_profit for m in months], [1_000.0, -500.0, 2_500.0], atol=1e-9, rtol=0)
    assert np.allclose(
        [m.return_fraction for m in months], [0.01, -500.0 / 101_000.0, 2_500.0 / 100_500.0], atol=1e-9, rtol=0
    )
    assert [m.trades for m in months] == [1, 0, 2]


def test_monthly_results_refuses_a_trade_outside_the_curve() -> None:
    daily = [(session_close_ms_utc(date(2024, 1, 31)), 101_000.0)]

    with pytest.raises(ValueError, match="outside every month"):
        monthly_results(daily, 100_000.0, [_utc_ms(2024, 2, 1, 15, 0)])


def _decl():  # type: ignore[no-untyped-def]
    return declaration(knob("x", high="10"), knob("y", high="10"), constraints=(KnobConstraint("x", "<", "y", "x must be below y"),))


def test_neighborhood_reports_each_side_and_a_one_sided_view() -> None:
    decl = _decl()
    center = synthetic_point({"x": 4, "y": 5})
    below, above = neighbor_probes(decl, center, "x", canonicalize=synthetic_point)
    assert below.point_hash is not None

    hood = neighborhood("x", 4.0, metrics(1.2), (below, above), {below.point_hash: metrics(0.4, net=-250.0)})

    assert [(row.value, row.status) for row in hood.rows] == [(3.0, "tested"), (4.0, "center"), (5.0, "invalid")]
    assert hood.rows[2].reason == "x must be below y"
    assert hood.one_sided
    assert [row.value for row in hood.losing_neighbors] == [3.0]


def test_neighborhood_distinguishes_failed_and_untested_neighbors() -> None:
    decl = _decl()
    below, above = neighbor_probes(decl, synthetic_point({"x": 2, "y": 9}), "x", canonicalize=synthetic_point)
    assert below.point_hash is not None

    hood = neighborhood("x", 2.0, metrics(1.0), (below, above), {below.point_hash: metrics(None, status="failed")})

    assert [row.status for row in hood.rows] == ["failed", "center", "untested"]
    assert not hood.one_sided
    assert hood.losing_neighbors == ()


def test_pair_map_carries_every_cells_status_and_metrics() -> None:
    decl = declaration(
        knob("fast", low="2", high="30"),
        knob("slow", low="3", high="40"),
        constraints=(KnobConstraint("fast", "<", "slow", "fast must be below slow"),),
    )
    plan = protocol(
        decl,
        knobs=(KnobPlan("fast", "search", 3.0, 12.0, 5.0, step=1.0), KnobPlan("slow", "search", 8.0, 30.0, 10.0, step=1.0)),
        seed=synthetic_point({"fast": 5, "slow": 10}),
    )
    grid = pair_grid(decl, plan, synthetic_point({"fast": 8, "slow": 21}), "fast", "slow", canonicalize=synthetic_point)
    center = next(cell for cell in grid.cells if cell.values == {"fast": 8.0, "slow": 21.0})
    failed = next(cell for cell in grid.cells if cell.values == {"fast": 3.0, "slow": 8.0})
    assert center.point_hash is not None and failed.point_hash is not None

    view = pair_map(grid, {center.point_hash: metrics(1.5), failed.point_hash: metrics(None, status="failed")})

    # x is the column knob (slow), y the row knob (fast), cells row-major.
    assert (view["x_knob"], view["y_knob"]) == ("slow", "fast")
    assert view["x_values"] == [8.0, 14.0, 21.0, 24.0, 30.0]
    assert view["y_values"] == [3.0, 5.0, 8.0, 10.0, 12.0]
    assert [(cell["y"], cell["x"]) for cell in view["cells"][:2]] == [(3.0, 8.0), (3.0, 14.0)]
    cells = {(cell["y"], cell["x"]): cell for cell in view["cells"]}
    assert cells[(8.0, 21.0)]["status"] == "tested"
    assert cells[(8.0, 21.0)]["metrics"] == metrics(1.5).as_dict()
    assert cells[(3.0, 8.0)]["status"] == "failed"
    assert cells[(10.0, 8.0)]["status"] == "invalid"
    assert cells[(10.0, 8.0)]["metrics"] is None
    assert cells[(5.0, 14.0)]["status"] == "untested"


# ── Recommendation ──────────────────────────────────────────────────────

_NET = SelectionPolicy(objective="net_profit", min_trades=30)
_STILL_WORKED = Verdict("still worked", "median fold retention 0.800 is at least 0.5", 3, 3, 0.8, 0.9, 120)


def _candidate(key: str, digest: str, net: float = 1_000.0, **kwargs) -> CandidateEvidence:  # type: ignore[no-untyped-def]
    observed = kwargs.pop("observed", None) or metrics(1.0, net=net)
    return CandidateEvidence(key=key, point={"symbol": "SPY", "id": digest}, point_hash=digest, metrics=observed, **kwargs)  # type: ignore[arg-type]


def _losing_hood():  # type: ignore[no-untyped-def]
    decl = _decl()
    below, above = neighbor_probes(decl, synthetic_point({"x": 4, "y": 8}), "x", canonicalize=synthetic_point)
    assert below.point_hash is not None
    return neighborhood("x", 4.0, metrics(1.0), (below, above), {below.point_hash: metrics(0.1, net=-10.0)})


def test_recommendation_names_the_stronger_fit_and_its_weightiest_caution() -> None:
    candidates = [
        _candidate("incumbent", "h-inc", net=500.0),
        _candidate("all_period", "h-all", net=1_000.0),
        _candidate("recent", "h-rec", net=1_400.0, neighborhoods=(_losing_hood(),)),
    ]

    result = recommendation(candidates, _STILL_WORKED, _NET)

    assert result.headline == (
        "The recent fit earns more in this replay, but nearby settings lose money. "
        "Inspect that sensitivity before using your final test."
    )
    assert [f.code for f in result.findings] == ["RECENT_DIFFERS_FROM_ALL_PERIOD", "NEIGHBORS_LOSE_MONEY"]


def test_recommendation_is_independent_of_candidate_order() -> None:
    candidates = [
        _candidate("incumbent", "h-inc", net=500.0),
        _candidate("all_period", "h-all", net=1_000.0, edge_hits=("x",)),
        _candidate("recent", "h-rec", net=1_400.0, neighborhoods=(_losing_hood(),)),
    ]
    shuffled = list(candidates)
    random.Random(7).shuffle(shuffled)

    assert recommendation(shuffled, _STILL_WORKED, _NET) == recommendation(candidates, _STILL_WORKED, _NET)


def test_recommendation_validation_failure_outranks_a_neighborhood_caution() -> None:
    stopped = Verdict("stopped working", "median out-of-sample Sharpe -0.200 is not positive", 3, 3, 0.1, -0.2, 90)
    candidates = [
        _candidate("incumbent", "h-inc"),
        _candidate("all_period", "h-all", net=2_000.0, neighborhoods=(_losing_hood(),)),
        _candidate("recent", "h-all", net=2_000.0),
    ]

    result = recommendation(candidates, stopped, _NET)

    assert result.headline == (
        "The all-period fit meets your rules in this replay, but the search procedure stopped working in later "
        "test periods. Inspect that test history before using your final test."
    )
    codes = [f.code for f in result.findings]
    assert "RECENT_DIFFERS_FROM_ALL_PERIOD" not in codes
    assert codes == ["NEIGHBORS_LOSE_MONEY", "VALIDATION_STOPPED_WORKING"]


def test_recommendation_flags_stress_that_erases_profit_and_an_untested_procedure() -> None:
    stressed = (StressResult("slippage_1c", "Extra 1¢/share slippage", metrics(1.0, net=-20.0)),)
    candidates = [_candidate("incumbent", "h-inc"), _candidate("all_period", "h-all", stress=stressed)]

    result = recommendation(candidates, None, _NET)

    assert result.headline == (
        "The all-period fit meets your rules in this replay, but extra costs erase its profit. "
        "Inspect that cost sensitivity before using your final test."
    )
    assert [f.code for f in result.findings] == ["STRESS_TURNS_NEGATIVE", "VALIDATION_NOT_JUDGED"]


def test_recommendation_when_no_searched_candidate_is_eligible() -> None:
    candidates = [
        _candidate("incumbent", "h-inc"),
        _candidate("all_period", "h-all", observed=metrics(1.0, trades=12)),
        _candidate("recent", "h-rec", observed=metrics(1.0, trades=0)),
    ]

    result = recommendation(candidates, _STILL_WORKED, _NET)

    assert result.headline.startswith("No searched candidate meets your rules")
    assert [f.code for f in result.findings] == [
        "RECENT_DIFFERS_FROM_ALL_PERIOD",
        "TOO_FEW_TRADES",
        "TOO_FEW_TRADES",
        "NONE_ELIGIBLE",
    ]


def test_recommendation_when_the_search_returns_the_current_settings() -> None:
    candidates = [_candidate("incumbent", "h-same"), _candidate("all_period", "h-same"), _candidate("recent", "h-same")]

    result = recommendation(candidates, _STILL_WORKED, _NET)

    assert result.headline.startswith("The search returned the current settings.")
    assert [f.code for f in result.findings] == ["SAME_AS_INCUMBENT"]


def test_recommendation_names_the_only_new_eligible_candidate() -> None:
    candidates = [
        _candidate("incumbent", "h-inc"),
        _candidate("all_period", "h-inc"),
        _candidate("recent", "h-rec", net=900.0, edge_hits=("y",)),
    ]

    result = recommendation(candidates, _STILL_WORKED, _NET)

    assert result.headline == (
        "The recent fit is the only new candidate that meets your rules, but it sits at the edge of the searched "
        "range. Inspect that range before using your final test."
    )
    assert [f.code for f in result.findings] == ["SAME_AS_INCUMBENT", "RECENT_DIFFERS_FROM_ALL_PERIOD", "EDGE_OF_RANGE"]


def test_same_as_collapses_identical_points() -> None:
    candidates = [_candidate("incumbent", "h-1"), _candidate("all_period", "h-2"), _candidate("recent", "h-2")]

    assert same_as(candidates) == {"incumbent": [], "all_period": ["recent"], "recent": ["all_period"]}
