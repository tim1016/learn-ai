"""The final chance readings: exact ranks, matches and the fit-to-fit pairing (#3014)."""

from __future__ import annotations

import copy
from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.research.golden_search.chance import SessionSeries, seed_for
from app.research.golden_search.chance_final import (
    CARRY_OVER_MISSING,
    SELECTION_NOTE,
    carry_over_reading,
    fit_to_fit_reading,
    reproduce_1x_objectives,
    stored_final_chance,
)
from app.research.golden_search.selection import Metrics
from app.utils.session_anchors import et_midnight_ms

CAPITAL = 100_000.0


def _completed(*, sharpe: float = 1.0) -> Metrics:
    return Metrics(status="completed", total_trades=40, net_profit=1_000.0, total_return_pct=0.01, sharpe_ratio=sharpe, max_drawdown_pct=0.1, win_rate=0.5)


def _sessions(n: int, start: date) -> tuple[int, ...]:
    return tuple(session_close_ms_utc(day) for day in expected_sessions(start, date(2027, 12, 31))[:n])


def _detail(returns: list[float], start: date = date(2025, 1, 2), *, exits: tuple[int, ...] = ()) -> dict[str, Any]:
    session_ms = _sessions(len(returns), start)
    equity = [CAPITAL]
    for value in returns:  # compounded, as the engine's daily equity curve is
        equity.append(equity[-1] * (1.0 + value))
    return {
        "initial_cash": CAPITAL,
        "daily_equity": [[ms, value] for ms, value in zip(session_ms, equity[1:], strict=True)],
        "trades": [{"exit_ms": session_ms[position] - 60_000} for position in exits],
    }


def _window(n: int, start: date) -> tuple[int, int]:
    days = expected_sessions(start, date(2027, 12, 31))[:n]
    return et_midnight_ms(days[0]), et_midnight_ms(days[-1] + timedelta(days=1))


def _smooth(n: int, seed: int) -> list[float]:
    return list(np.random.default_rng(seed).normal(0.0005, 0.01, n))


def _series(returns: list[float], start: date = date(2025, 1, 2)) -> SessionSeries:
    session_ms = _sessions(len(returns), start)
    return SessionSeries(session_ms=session_ms, returns=np.asarray(returns, dtype=np.float64), exits=np.zeros(len(returns), dtype=np.int64), capital=CAPITAL)


def _stored_rehearsal(returns: list[float], *, objective: str = "total_return_pct", point_hash: str = "cafebabe", scheduled: int = 10, seed: int | None = None) -> dict[str, Any]:
    """A stored 1× rehearsal basis exactly as the evidence stage records it for this development input."""
    from app.research.golden_search.chance_compare import admit_run, series_digest

    admitted = admit_run(_completed(), _detail(returns), _window(len(returns), date(2025, 1, 2)))
    assert isinstance(admitted, SessionSeries)
    digest = series_digest(admitted)
    seed_inputs = ["golden-search-chance-rehearsal/v1", _window(len(returns), date(2025, 1, 2))[0], _window(len(returns), date(2025, 1, 2))[1], objective, *sorted((point_hash, "deadbeef")), 1]
    value = seed if seed is not None else seed_for(*seed_inputs)
    return {
        "status": "measured",
        "rungs": [{"k": 1, "sessions": scheduled, "trade_floor": 5, "denominator": 10_000, "passes": {}, "undefined": {}, "shares": {}}],
        "active_rules": [],
        "guidance": "stored",
        "objective_percentiles_1x": {"p": [0.0] * 99, "finite": 10_000},
        "basis": {
            "schema_version": "golden-search-chance-rehearsal/v1",
            "convention": "stationary-bootstrap/v1",
            "replications": 10_000,
            "chunk_replications": 1_000,
            "mean_block_sessions": 5,
            "thresholds": {"rehearsal_target": 0.8},
            "objective": {"measure": objective, "label": "total return", "units": "fraction of the starting capital"},
            "rules": {"max_drawdown_ceiling": 0.2, "require_positive_net": True, "drawdown_basis": "session closes"},
            "activity": {"frequency_based": True, "expected_trades_per_year": 50, "final_trading_years": 0.084},
            "final_interval": {"window": {"start_ms": 0, "end_ms": 0}, "scheduled_sessions": scheduled},
            "source": {
                "window": {"start_ms": _window(len(returns), date(2025, 1, 2))[0], "end_ms": _window(len(returns), date(2025, 1, 2))[1]},
                "session_count": len(returns),
                "candidate_point_hash": point_hash,
                "incumbent_point_hash": "deadbeef",
                "series_digest": {"candidate": digest, "incumbent": "d-inc"},
                "capital": CAPITAL,
            },
            "seeds": {"1x": {"inputs": seed_inputs, "value": value}},
        },
    }


def _carry_over(
    development: list[float],
    stored: dict[str, Any] | None,
    *,
    final_sharpe: float = 1.0,
    final_returns: list[float] | None = None,
    objective: str = "total_return_pct",
    final_metrics: Metrics | None = None,
    development_start: date = date(2025, 1, 2),
    point_hash: str = "cafebabe",
    **kwargs: Any,
) -> dict[str, Any]:
    final = final_returns if final_returns is not None else _smooth(25, 99)
    # The fake final result summarizes the final series it stands for: its objective is that series' own compounded value.
    total_return = float(np.prod([1.0 + value for value in final]) - 1.0)
    metrics = final_metrics if final_metrics is not None else (Metrics(status="completed", total_trades=len(final), net_profit=CAPITAL * total_return, total_return_pct=total_return, sharpe_ratio=final_sharpe, max_drawdown_pct=0.1, win_rate=0.5))
    return carry_over_reading(
        objective=objective,
        development_window=_window(len(development), development_start),
        final_window=_window(len(final), date(2025, 4, 1)),
        development_detail=_detail(development, development_start),
        development_metrics=_completed(),
        final_metrics=metrics,
        final_detail=_detail(final, date(2025, 4, 1)),
        point_hash=point_hash,
        stored_rehearsal=stored,
        **kwargs,
    )


def test_the_rank_counts_strictly_below_over_the_finite_only() -> None:
    development = _smooth(60, 1)
    stored = _stored_rehearsal(development)
    reading = _carry_over(development, stored, final_sharpe=1.0)
    assert reading["status"] == "measured"
    assert reading["finite"] + reading["undefined"] == 10_000
    assert 0 <= reading["below"] <= reading["finite"]  # strictly-below counts a subset of the finite paths only
    assert 0.0 <= reading["rank"] <= 1.0
    assert reading["rank"] == pytest.approx(reading["below"] / reading["finite"])
    assert reading["selection_note"] == SELECTION_NOTE


def test_a_result_above_every_finite_rehearsal_ranks_1_not_99_percent() -> None:
    development = [0.001] * 60  # every 10-session rehearsal objective compounds near +1%
    stored = _stored_rehearsal(development)
    reading = _carry_over(development, stored, final_returns=[0.05] * 25)  # a +239% final result
    assert reading["finite"] == 10_000
    assert reading["below"] == 10_000
    assert reading["rank"] == 1.0
    assert reading["label"] == "above_most"


def test_a_result_below_every_finite_rehearsal_ranks_0() -> None:
    development = [0.01] * 60
    stored = _stored_rehearsal(development)
    reading = _carry_over(development, stored, final_returns=[-0.30] * 25)
    assert reading["below"] == 0
    assert reading["rank"] == 0.0
    assert reading["label"] == "below_most"


def test_a_tie_at_the_recorded_value_is_not_strictly_below() -> None:
    # An exactly-flat development input and an exactly-flat final run of the rehearsal's own length: every objective is
    # bit-identically 0.0, so the recorded value ties every path and nothing is strictly below it.
    development = [0.0] * 60
    stored = _stored_rehearsal(development, scheduled=25)
    reading = _carry_over(development, stored, final_returns=[0.0] * 25)
    assert reading["finite"] == 10_000
    assert reading["below"] == 0
    assert reading["rank"] == 0.0  # ties sit at 0: strictly below is the only thing counted


def test_a_discrete_distribution_where_the_99_percentile_shortcut_disagrees() -> None:
    # 90 flat sessions and 10 deeply negative ones: the empirical rank counts actual paths, not which stored
    # percentile bin the result falls in.
    development = [0.0] * 54 + [-0.50] * 6
    stored = _stored_rehearsal(development)
    reading = _carry_over(development, stored, final_returns=[0.0] * 25)
    objectives = reproduce_1x_objectives(
        _series(development), seed=stored["basis"]["seeds"]["1x"]["value"], size=10, objective="total_return_pct", cancel_check=lambda: None
    )
    finite = np.isfinite(objectives)
    expected_below = int(np.count_nonzero(objectives[finite] < 0.0))
    assert reading["below"] == expected_below
    assert reading["finite"] == int(np.count_nonzero(finite))


def test_mismatched_objective_length_digest_or_scope_is_a_named_missing_never_a_rank() -> None:
    development = _smooth(60, 2)
    wrong_objective = _stored_rehearsal(development, objective="sharpe_ratio")
    assert _carry_over(development, wrong_objective)["code"] == CARRY_OVER_MISSING[0]
    assert "objective" in _carry_over(development, wrong_objective)["reason"]

    other_point = _stored_rehearsal(development, point_hash="someone-else")
    assert "different settings" in _carry_over(development, other_point)["reason"]

    changed_input = _smooth(60, 3)
    assert "digest" in _carry_over(changed_input, _stored_rehearsal(development))["reason"]

    too_short = _stored_rehearsal(development, scheduled=50)
    assert "fewer sessions" in _carry_over(development, too_short, final_returns=_smooth(25, 5))["reason"]

    no_seed = _stored_rehearsal(development)
    no_seed["basis"]["seeds"] = {}
    assert "no 1× seed" in _carry_over(development, no_seed)["reason"]

    missing = _carry_over(development, None)
    assert missing["code"] == CARRY_OVER_MISSING[0]


def test_a_rehearsal_stored_under_different_constants_is_never_reproduced() -> None:
    # A stored basis drawn under other resampling constants would reproduce different paths from the same
    # seed; borrowing today's constants would fabricate a rank, so the reading refuses instead.
    development = _smooth(60, 20)
    for field, value in (("mean_block_sessions", 6.0), ("replications", 20_000), ("convention", "moving-block/v9")):
        stored = _stored_rehearsal(development)
        stored["basis"][field] = value
        reading = _carry_over(development, stored)
        assert reading["code"] == CARRY_OVER_MISSING[0], field
        assert "different resampling constants" in reading["reason"], field


def test_the_curve_is_the_reproductions_own_1st_to_99th_percentiles() -> None:
    development = _smooth(60, 21)
    stored = _stored_rehearsal(development)
    reading = _carry_over(development, stored)
    objectives = reproduce_1x_objectives(
        _series(development), seed=stored["basis"]["seeds"]["1x"]["value"], size=10, objective="total_return_pct", cancel_check=lambda: None
    )
    finite = objectives[np.isfinite(objectives)]
    expected = [float(value) for value in np.quantile(finite, np.linspace(0.01, 0.99, 99), method="linear")]
    assert reading["curve"]["p"] == pytest.approx(expected)
    assert len(reading["curve"]["p"]) == 99


def test_the_reproduction_repeats_a_real_ladders_own_1x_draw() -> None:
    # The strongest statement this slice makes: a ladder the evidence stage really recorded, read back, and
    # the carry-over's reproduction redraws its 1× objectives — same seed, same length — so the ranked paths
    # are the stored rehearsal's own, not an independently-seeded lookalike.
    from app.research.golden_search.activity import TradeFloors
    from app.research.golden_search.chance_rehearsal import final_session_count, rehearsal_reading
    from app.research.golden_search.planning import protocol_from_request
    from tests._helpers.golden_search_study import plan_request

    development = _smooth(60, 22)
    incumbent = _smooth(60, 23)
    final_window = (et_midnight_ms(date(2025, 4, 1)), et_midnight_ms(date(2025, 5, 1)))
    protocol = protocol_from_request(
        plan_request(
            "SPY",
            development_start_ms=et_midnight_ms(date(2025, 1, 1)),
            development_end_ms=et_midnight_ms(date(2025, 4, 1)),
            final_start_ms=final_window[0],
            final_end_ms=final_window[1],
        )
    )
    protocol = type(protocol).from_dict({**protocol.as_dict(), "exam_min_trades": 5})
    ladder = rehearsal_reading(
        objective="total_return_pct",
        window=_window(len(development), date(2025, 1, 2)),
        final_window=final_window,
        floors=TradeFloors(protocol, {}),
        rate=None,
        final_years=None,
        ceiling=0.5,
        require_positive_net=True,
        candidate_metrics=_completed(),
        candidate_detail=_detail(development),
        candidate_hash="cafebabe",
        incumbent_metrics=_completed(),
        incumbent_detail=_detail(incumbent),
        incumbent_hash="deadbeef",
    )
    assert ladder["status"] == "measured"
    reproduced = reproduce_1x_objectives(
        _series(development),
        seed=ladder["basis"]["seeds"]["1x"]["value"],
        size=final_session_count(final_window),
        objective="total_return_pct",
        cancel_check=lambda: None,
    )
    finite = reproduced[np.isfinite(reproduced)]
    percentiles = [float(value) for value in np.quantile(finite, np.linspace(0.01, 0.99, 99), method="linear")]
    assert percentiles == pytest.approx(ladder["objective_percentiles_1x"]["p"])


def test_a_nonfinite_or_absent_final_objective_has_no_rank() -> None:
    development = _smooth(60, 4)
    stored = _stored_rehearsal(development)
    failed_final = Metrics(status="failed", total_trades=0, net_profit=None, total_return_pct=None, sharpe_ratio=None, max_drawdown_pct=None, win_rate=None, error="refused")
    assert "no completed result" in _carry_over(development, stored, final_metrics=failed_final)["reason"]


def test_cancellation_propagates_and_never_becomes_a_reading() -> None:
    from app.jobs.progress import JobCancelled

    def cancelled() -> None:
        raise JobCancelled("job 21 cancelled")

    development = _smooth(60, 6)
    with pytest.raises(JobCancelled):
        _carry_over(development, _stored_rehearsal(development), cancel_check=cancelled)


def test_the_reproduction_is_exactly_the_stored_seed_redrawn() -> None:
    development = _smooth(60, 7)
    stored = _stored_rehearsal(development, seed=987654321)
    objectives = reproduce_1x_objectives(_series(development), seed=987654321, size=10, objective="total_return_pct", cancel_check=lambda: None)
    again = reproduce_1x_objectives(_series(development), seed=987654321, size=10, objective="total_return_pct", cancel_check=lambda: None)
    assert np.array_equal(objectives, again)
    reading = _carry_over(development, stored, final_returns=_smooth(25, 8))
    finite = np.isfinite(objectives)
    assert reading["below"] == int(np.count_nonzero(objectives[finite] < reading["recorded_objective"]))


# ── Fit to fit ────────────────────────────────────────────────────────────


def _fit_to_fit(first: list[float], second: list[float], **kwargs: Any) -> dict[str, Any]:
    assert len(first) >= 20 and len(second) >= 20
    return fit_to_fit_reading(
        objective="total_return_pct",
        window=_window(len(first), date(2025, 4, 1)),
        first={"metrics": _completed(), "detail": _detail(first, date(2025, 4, 1)), "point_hash": "aaaa", "label": "All-period fit"},
        second={"metrics": _completed(), "detail": _detail(second, date(2025, 4, 1)), "point_hash": "zzzz", "label": "Recent fit"},
        **kwargs,
    )


def test_fit_to_fit_shares_one_draw_and_both_directions_reverse() -> None:
    forward = _fit_to_fit(_smooth(25, 11), _smooth(25, 12))
    assert forward["status"] == "measured"
    assert forward["pair"] == ["aaaa", "zzzz"]
    assert sum(forward["counts"].values()) == 10_000
    backward = fit_to_fit_reading(
        objective="total_return_pct",
        window=_window(25, date(2025, 4, 1)),
        first={"metrics": _completed(), "detail": _detail(_smooth(25, 12), date(2025, 4, 1)), "point_hash": "zzzz", "label": "Recent fit"},
        second={"metrics": _completed(), "detail": _detail(_smooth(25, 11), date(2025, 4, 1)), "point_hash": "aaaa", "label": "All-period fit"},
    )
    assert forward["counts"]["better"] == backward["counts"]["worse"]
    assert forward["counts"]["worse"] == backward["counts"]["better"]
    assert forward["counts"]["tied"] == backward["counts"]["tied"]
    assert forward["basis"]["seed"]["inputs"] == backward["basis"]["seed"]["inputs"]
    assert forward["selection_note"] == SELECTION_NOTE


def test_a_failed_other_fit_leaves_a_named_missing_optional_comparison() -> None:
    failed = Metrics(status="failed", total_trades=0, net_profit=None, total_return_pct=None, sharpe_ratio=None, max_drawdown_pct=None, win_rate=None, error="engine refused")
    reading = fit_to_fit_reading(
        objective="total_return_pct",
        window=_window(25, date(2025, 4, 1)),
        first={"metrics": _completed(), "detail": _detail(_smooth(25, 13), date(2025, 4, 1)), "point_hash": "aaaa", "label": "All-period fit"},
        second={"metrics": failed, "detail": None, "point_hash": "zzzz", "label": "Recent fit"},
    )
    assert reading["status"] == "missing"
    assert reading["code"] == "fit_unavailable"


def test_stored_final_chance_normalizes_per_component() -> None:
    measured = _carry_over(_smooth(60, 14), _stored_rehearsal(_smooth(60, 14)))
    partial = stored_final_chance({"chance": {"carry_over": copy.deepcopy(measured)}})
    assert partial["carry_over"] == measured
    assert partial["comparison"]["code"] == "not_measured"
    assert partial["profitability"]["code"] == "not_measured"
    unsupported = stored_final_chance({"chance": {"carry_over": {"status": "from_the_future"}}})
    assert unsupported["carry_over"]["code"] == "unsupported"
    absent = stored_final_chance({})
    assert absent["carry_over"]["code"] == CARRY_OVER_MISSING[0]
