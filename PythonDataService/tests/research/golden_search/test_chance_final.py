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
    FinalChanceScope,
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


def _detail(returns: list[float], start: date = date(2025, 1, 2), *, exits: tuple[int, ...] = (), capital: float = CAPITAL) -> dict[str, Any]:
    session_ms = _sessions(len(returns), start)
    equity = [capital]
    for value in returns:  # compounded, as the engine's daily equity curve is
        equity.append(equity[-1] * (1.0 + value))
    return {
        "initial_cash": capital,
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


#: The 25-session final interval every fixture study freezes: April 2025's own scheduled sessions.
_FINAL_WINDOW = _window(25, date(2025, 4, 1))


def _stored_rehearsal(
    returns: list[float],
    *,
    objective: str = "total_return_pct",
    point_hash: str = "cafebabe",
    scheduled: int = 25,
    seed: int | None = None,
    final_window: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """A stored 1× rehearsal basis exactly as the evidence stage records it for this development input."""
    from app.research.golden_search.chance_compare import admit_run, series_digest

    admitted = admit_run(_completed(), _detail(returns), _window(len(returns), date(2025, 1, 2)))
    assert isinstance(admitted, SessionSeries)
    digest = series_digest(admitted)
    seed_inputs = ["golden-search-chance-rehearsal/v1", _window(len(returns), date(2025, 1, 2))[0], _window(len(returns), date(2025, 1, 2))[1], objective, *sorted((point_hash, "deadbeef")), 1]
    value = seed if seed is not None else seed_for(*seed_inputs)
    frozen_final = final_window if final_window is not None else _FINAL_WINDOW
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
            "final_interval": {"window": {"start_ms": frozen_final[0], "end_ms": frozen_final[1]}, "scheduled_sessions": scheduled},
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
    final_detail: dict[str, Any] | None = None,
    final_capital: float = CAPITAL,
    development_start: date = date(2025, 1, 2),
    point_hash: str = "cafebabe",
    **kwargs: Any,
) -> dict[str, Any]:
    final = final_returns if final_returns is not None else _smooth(25, 99)
    # The fake final result summarizes the final series it stands for: its objective is that series' own compounded value.
    total_return = float(np.prod([1.0 + value for value in final]) - 1.0)
    metrics = final_metrics if final_metrics is not None else (Metrics(status="completed", total_trades=len(final), net_profit=final_capital * total_return, total_return_pct=total_return, sharpe_ratio=final_sharpe, max_drawdown_pct=0.1, win_rate=0.5))
    return carry_over_reading(
        objective=objective,
        development_window=_window(len(development), development_start),
        final_window=_window(len(final), date(2025, 4, 1)),
        development_detail=_detail(development, development_start),
        development_metrics=_completed(),
        final_metrics=metrics,
        final_detail=final_detail if final_detail is not None else _detail(final, date(2025, 4, 1), capital=final_capital),
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
    assert reading["rank"] == reading["below"] / reading["finite"]  # the same operation, exactly
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
        _series(development), seed=stored["basis"]["seeds"]["1x"]["value"], size=25, objective="total_return_pct", cancel_check=lambda: None
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
    assert "1× length is 50 sessions" in _carry_over(development, too_short, final_returns=_smooth(25, 5))["reason"]

    no_seed = _stored_rehearsal(development)
    no_seed["basis"]["seeds"] = {}
    assert "no 1× seed" in _carry_over(development, no_seed)["reason"]

    missing = _carry_over(development, None)
    assert missing["code"] == CARRY_OVER_MISSING[0]


def test_a_rehearsal_shorter_or_longer_than_the_frozen_final_horizon_never_ranks() -> None:
    # A 10-session stored rehearsal against a 25-session final interval used to rank 98.89%; the length
    # mismatch is a named missing carry-over instead (#3025).
    development = _smooth(60, 31)
    for scheduled in (10, 40):
        mismatched = _stored_rehearsal(development, scheduled=scheduled)
        reading = _carry_over(development, mismatched)
        assert reading["code"] == CARRY_OVER_MISSING[0], scheduled
        assert f"1× length is {scheduled} sessions" in reading["reason"], scheduled
        assert "schedules 25" in reading["reason"], scheduled
    # The authentic 25-session rehearsal of the same development input still measures.
    matching = _carry_over(development, _stored_rehearsal(development))
    assert matching["status"] == "measured"


def test_a_rehearsal_of_a_different_final_window_with_an_equal_session_count_never_ranks() -> None:
    development = _smooth(60, 32)
    shifted = _window(25, date(2025, 6, 2))  # 25 sessions, but not this study's frozen final interval
    rehearsal = _stored_rehearsal(development, final_window=shifted)
    reading = _carry_over(development, rehearsal)
    assert reading["code"] == CARRY_OVER_MISSING[0]
    assert "different final interval" in reading["reason"]


def test_a_final_result_from_a_different_starting_capital_never_ranks() -> None:
    development = _smooth(60, 33)
    final = _smooth(25, 34)
    reading = _carry_over(development, _stored_rehearsal(development), final_returns=final, final_capital=200_000.0)
    assert reading["code"] == CARRY_OVER_MISSING[0]
    assert "different capital" in reading["reason"]


def test_an_unknown_rehearsal_schema_or_chunk_size_is_never_reproduced_under_today_constants() -> None:
    development = _smooth(60, 35)
    future = _stored_rehearsal(development)
    future["basis"]["schema_version"] = "future/v9"
    reading = _carry_over(development, future)
    assert reading["code"] == CARRY_OVER_MISSING[0]
    assert "future/v9" in reading["reason"] and "cannot reproduce" in reading["reason"]

    rechunked = _stored_rehearsal(development)
    rechunked["basis"]["chunk_replications"] = 2_000
    reading = _carry_over(development, rechunked)
    assert reading["code"] == CARRY_OVER_MISSING[0]
    assert "chunks of 2000" in reading["reason"]

    unchunked = _stored_rehearsal(development)
    del unchunked["basis"]["chunk_replications"]
    reading = _carry_over(development, unchunked)
    assert reading["code"] == CARRY_OVER_MISSING[0]
    assert "chunks of None" in reading["reason"]


def test_an_objective_units_or_development_window_mismatch_is_a_named_missing() -> None:
    development = _smooth(60, 36)
    reunit = _stored_rehearsal(development)
    reunit["basis"]["objective"]["units"] = "US dollars"
    reading = _carry_over(development, reunit)
    assert reading["code"] == CARRY_OVER_MISSING[0]
    assert "US dollars" in reading["reason"]

    elsewhere = _stored_rehearsal(development)
    elsewhere["basis"]["source"]["window"] = {"start_ms": 0, "end_ms": 1}
    reading = _carry_over(development, elsewhere)
    assert reading["code"] == CARRY_OVER_MISSING[0]
    assert "different development window" in reading["reason"]


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
        _series(development), seed=stored["basis"]["seeds"]["1x"]["value"], size=25, objective="total_return_pct", cancel_check=lambda: None
    )
    finite = objectives[np.isfinite(objectives)]
    # The reading derives its development returns from the detail's compounded equity, the fixture passes raw
    # ones, so the redrawn objectives agree only to the last ulp: the strict float tolerance, never a default
    # approx that would also accept a wrong percentile (#3025).
    expected = [float(value) for value in np.quantile(finite, np.linspace(0.01, 0.99, 99), method="linear")]
    assert reading["curve"]["p"] == pytest.approx(expected, rel=0.0, abs=1e-9)
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
    assert percentiles == pytest.approx(ladder["objective_percentiles_1x"]["p"], rel=0.0, abs=1e-9)  # raw vs equity-derived returns: last-ulp only


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
    objectives = reproduce_1x_objectives(_series(development), seed=987654321, size=25, objective="total_return_pct", cancel_check=lambda: None)
    again = reproduce_1x_objectives(_series(development), seed=987654321, size=25, objective="total_return_pct", cancel_check=lambda: None)
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
    partial = stored_final_chance({"chance": {"carry_over": copy.deepcopy(measured)}}, _SCOPE)
    assert partial["carry_over"] == measured
    assert partial["comparison"]["code"] == "not_measured"
    assert partial["profitability"]["code"] == "not_measured"
    unsupported = stored_final_chance({"chance": {"carry_over": {"status": "from_the_future"}}}, _SCOPE)
    assert unsupported["carry_over"]["code"] == "unsupported"
    absent = stored_final_chance({}, _SCOPE)
    assert absent["carry_over"]["code"] == CARRY_OVER_MISSING[0]


# ── Stored-record validation (#3024) ─────────────────────────────────────


def _scope() -> FinalChanceScope:
    """The frozen identity of a 60-session development / 25-session final study of the cafebabe fit."""
    return FinalChanceScope(
        objective="total_return_pct",
        development_window=_window(60, date(2025, 1, 2)),
        final_window=_window(25, date(2025, 4, 1)),
        capital=CAPITAL,
        point_hash="cafebabe",
    )


_SCOPE = _scope()


def _comparison_basis() -> dict[str, Any]:
    window = _window(25, date(2025, 4, 1))
    inputs = ["golden-search-chance-comparison/v1", window[0], window[1], "total_return_pct", *sorted(("cafebabe", "deadbeef"))]
    return {
        "schema_version": "golden-search-chance-comparison/v1",
        "convention": "stationary-bootstrap/v1",
        "replications": 10_000,
        "chunk_replications": 1_000,
        "mean_block_sessions": 5.0,
        "thresholds": {"clear": 0.95},
        "objective": {"measure": "total_return_pct", "label": "total return", "units": "fraction of the starting capital"},
        "seed": {"inputs": inputs, "value": seed_for(*inputs)},
        "source": {
            "window": {"start_ms": window[0], "end_ms": window[1]},
            "session_count": 25,
            "candidate_point_hash": "cafebabe",
            "incumbent_point_hash": "deadbeef",
            "series_digest": {"candidate": "d-all", "incumbent": "d-inc"},
            "capital": CAPITAL,
        },
    }


def _measured_comparison(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "status": "measured",
        "label": "clearly_as_good",
        "sentence": "A measured comparison sentence.",
        "incomplete_support": False,
        "bias_note": "A selection caveat.",
        "counts": {"better": 9_350, "tied": 270, "worse": 380, "undefined": 0},
        "shares": {"better": 0.935, "tied": 0.027, "worse": 0.038, "undefined": 0.0},
        "at_least_as_good": {"count": 9_620, "share": 0.962},
        "candidate_range": {"p5": 0.031, "p50": 0.089, "p95": 0.152, "finite": 10_000},
        "difference_range": {"p5": 0.019, "p50": 0.037, "p95": 0.066, "finite": 10_000},
        "basis": _comparison_basis(),
    }
    record.update(overrides)
    return record


def _measured_profitability(**overrides: Any) -> dict[str, Any]:
    basis = _comparison_basis()
    basis["schema_version"] = "golden-search-chance-profitability/v1"
    basis["seed"] = {"inputs": [], "value": 0}
    record: dict[str, Any] = {
        "status": "measured",
        "label": "clearly_positive",
        "sentence": "A measured profitability sentence.",
        "counts": {"positive": 9_740, "zero": 60, "negative": 200, "undefined": 0},
        "shares": {"positive": 0.974, "zero": 0.006, "negative": 0.02, "undefined": 0.0},
        "basis": basis,
    }
    record.update(overrides)
    return record


def _measured_carry() -> dict[str, Any]:
    return copy.deepcopy(_carry_over(_smooth(60, 30), _stored_rehearsal(_smooth(60, 30))))


def _final_fit(**chance: Any) -> dict[str, Any]:
    return {"point_hash": "cafebabe", "chance": {"comparison": _measured_comparison(), "profitability": _measured_profitability(), "carry_over": _measured_carry(), **chance}}


def test_fully_valid_measured_components_read_back_as_measured() -> None:
    fit = _final_fit()
    served = stored_final_chance(fit, _SCOPE)
    assert served["comparison"] == fit["chance"]["comparison"]
    assert served["profitability"] == fit["chance"]["profitability"]
    assert served["carry_over"] == fit["chance"]["carry_over"]


def test_an_unsupported_schema_version_or_convention_reads_invalid_even_when_fully_wire_valid() -> None:
    unsupported = _final_fit()
    unsupported["chance"]["comparison"]["basis"]["schema_version"] = "golden-search-chance-comparison/v999"
    unsupported["chance"]["profitability"]["basis"]["convention"] = "moving-block/v9"
    served = stored_final_chance(unsupported, _SCOPE)
    assert served["comparison"]["code"] == "invalid_record"
    assert "v999" in served["comparison"]["reason"] and "schema" in served["comparison"]["reason"]
    assert served["profitability"]["code"] == "invalid_record"
    assert "moving-block/v9" in served["profitability"]["reason"]


def test_forged_counts_over_all_undefined_paths_are_invalid_not_clear() -> None:
    forged = _final_fit()
    forged["chance"]["comparison"].update(
        counts={"better": 0, "tied": 0, "worse": 0, "undefined": 10_000},
        at_least_as_good={"count": 10_000, "share": 1.0},  # the forged inclusive count
        label="clearly_as_good",  # the forged label
    )
    served = stored_final_chance(forged, _SCOPE)
    assert served["comparison"]["code"] == "invalid_record"
    assert "disagrees with its own counts" in served["comparison"]["reason"]
    # A forged inclusive count alone, over otherwise coherent counts, is named too.
    inclusive = _final_fit()
    inclusive["chance"]["comparison"]["at_least_as_good"] = {"count": 9_621, "share": 0.9621}
    served = stored_final_chance(inclusive, _SCOPE)
    assert served["comparison"]["code"] == "invalid_record"
    assert "at-least-as-good" in served["comparison"]["reason"]


def test_a_basisless_positive_finite_carry_over_is_invalid() -> None:
    basisless = _final_fit()
    basisless["chance"]["carry_over"] = {"status": "measured", "finite": 10_000}
    served = stored_final_chance(basisless, _SCOPE)
    assert served["carry_over"]["code"] == "invalid_record"
    assert "basis" in served["carry_over"]["reason"]


def test_invalid_counts_totals_thresholds_and_ranges_are_named() -> None:
    wrong_total = _final_fit()
    wrong_total["chance"]["comparison"]["counts"]["undefined"] = 1  # now 10,001 paths
    boolean = _final_fit()
    boolean["chance"]["comparison"]["counts"]["better"] = True
    coerced = _final_fit()
    coerced["chance"]["comparison"]["counts"]["better"] = "9620"
    threshold = _final_fit()
    threshold["chance"]["comparison"]["basis"]["thresholds"] = {"clear": 1.5}
    unordered = _final_fit()
    unordered["chance"]["comparison"]["difference_range"] = {"p5": 0.066, "p50": 0.037, "p95": 0.019, "finite": 10_000}
    impossible_support = _final_fit()
    impossible_support["chance"]["comparison"]["difference_range"] = {"p5": 0.019, "p50": 0.037, "p95": 0.066, "finite": 10_001}  # one more than the 10,000 defined pairs
    for record in (wrong_total, boolean, coerced, threshold, unordered, impossible_support):
        served = stored_final_chance(record, _SCOPE)
        assert served["comparison"]["code"] == "invalid_record", served["comparison"]["reason"]
    assert "total" in stored_final_chance(wrong_total, _SCOPE)["comparison"]["reason"]
    assert "nonnegative integer" in stored_final_chance(boolean, _SCOPE)["comparison"]["reason"]
    assert "clear threshold" in stored_final_chance(threshold, _SCOPE)["comparison"]["reason"]
    assert "ordered" in stored_final_chance(unordered, _SCOPE)["comparison"]["reason"]
    # A defined pair whose subtraction overflows keeps its comparison but not a difference value: a difference
    # range below the defined count stays valid, one above it cannot exist.
    overflow_ok = _final_fit()
    overflow_ok["chance"]["comparison"]["difference_range"] = {"p5": 0.019, "p50": 0.037, "p95": 0.066, "finite": 9_000}
    assert stored_final_chance(overflow_ok, _SCOPE)["comparison"]["status"] == "measured"


def test_wrong_fit_window_capital_or_objective_evidence_is_rejected() -> None:
    other_fit = _final_fit()
    other_fit["chance"]["comparison"]["basis"]["source"]["candidate_point_hash"] = "someone-else"
    other_window = _final_fit()
    other_window["chance"]["profitability"]["basis"]["source"]["window"] = {"start_ms": 0, "end_ms": 1}
    other_capital = _final_fit()
    other_capital["chance"]["profitability"]["basis"]["source"]["capital"] = 200_000.0
    other_objective = _final_fit()
    other_objective["chance"]["comparison"]["basis"]["objective"] = {"measure": "sharpe_ratio", "label": "Sharpe ratio", "units": "annualized ratio of session returns (× √252)"}
    wrong_seed_window = _final_fit()
    wrong_seed_window["chance"]["carry_over"]["basis"]["seed"]["inputs"][1] = 0  # a development window that is not this study's
    wrong_seed_point = _final_fit()
    wrong_seed_point["chance"]["carry_over"]["basis"]["seed"]["inputs"][4] = "not-this-fit"
    wrong_seed_value = _final_fit()
    wrong_seed_value["chance"]["carry_over"]["basis"]["seed"]["value"] = 1
    for record, component in ((other_fit, "comparison"), (other_window, "profitability"), (other_capital, "profitability"), (other_objective, "comparison"), (wrong_seed_window, "carry_over"), (wrong_seed_point, "carry_over"), (wrong_seed_value, "carry_over")):
        served = stored_final_chance(record, _SCOPE)
        assert served[component]["code"] == "invalid_record", served[component]["reason"]
    # An alias — the same point hash — is the same identity and stays valid.
    aliased = FinalChanceScope(objective=_SCOPE.objective, development_window=_SCOPE.development_window, final_window=_SCOPE.final_window, capital=_SCOPE.capital, point_hash="cafebabe")
    assert stored_final_chance(_final_fit(), aliased)["comparison"]["status"] == "measured"


def test_malformed_outer_mappings_and_records_never_raise_and_keep_valid_siblings() -> None:
    corrupt = stored_final_chance({"point_hash": "cafebabe", "chance": "corrupt"}, _SCOPE)
    assert {component: corrupt[component]["code"] for component in ("comparison", "profitability", "carry_over")} == {
        "comparison": "unreadable",
        "profitability": "unreadable",
        "carry_over": "unreadable",
    }
    one_bad_sibling = _final_fit()
    one_bad_sibling["chance"]["profitability"] = 42
    served = stored_final_chance(one_bad_sibling, _SCOPE)
    assert served["comparison"]["status"] == "measured"
    assert served["carry_over"]["status"] == "measured"
    assert served["profitability"]["code"] == "unreadable"


def test_a_carry_over_whose_rank_disagrees_with_its_counts_is_invalid() -> None:
    mismatched = _final_fit()
    mismatched["chance"]["carry_over"]["below"] = 200  # the rank still says 62.21%
    served = stored_final_chance(mismatched, _SCOPE)
    assert served["carry_over"]["code"] == "invalid_record"
    assert "rank" in served["carry_over"]["reason"]
    relabelled = _final_fit()
    relabelled["chance"]["carry_over"]["label"] = "above_most"
    assert stored_final_chance(relabelled, _SCOPE)["carry_over"]["code"] == "invalid_record"


def test_an_invalid_required_component_gates_an_enrolled_approval_but_not_a_legacy_one() -> None:
    from app.research.golden_search.chance_approval import (
        CHANCE_UNAVAILABLE,
        CHANCE_WITHIN_NOISE,
        chance_weakness_items,
        enrollment_policy,
    )

    fit = _final_fit()
    fit["chance"]["comparison"]["basis"]["schema_version"] = "golden-search-chance-comparison/v999"
    served = stored_final_chance(fit, _SCOPE)
    items = chance_weakness_items(enrollment_policy(), served, positive_net_required=True)
    assert [item["code"] for item in items] == [CHANCE_UNAVAILABLE]
    assert "invalid_record" not in items[0]["text"]  # the reason reads as evidence words, not a code dump
    assert "schema" in items[0]["text"]
    assert chance_weakness_items(None, served, positive_net_required=True) == []
    # A weak but valid measured share still follows the existing owner decision, read from the raw counts.
    weak = _final_fit()
    weak["chance"]["comparison"].update(counts={"better": 9_499, "tied": 0, "worse": 501, "undefined": 0}, at_least_as_good={"count": 9_499, "share": 0.9499}, label="mixed")
    weak_items = chance_weakness_items(enrollment_policy(), stored_final_chance(weak, _SCOPE), positive_net_required=True)
    assert [item["code"] for item in weak_items] == [CHANCE_WITHIN_NOISE]
    assert "9,499 of 10,000" in weak_items[0]["text"]
    at_threshold = _final_fit()
    at_threshold["chance"]["comparison"].update(counts={"better": 9_500, "tied": 0, "worse": 500, "undefined": 0}, at_least_as_good={"count": 9_500, "share": 0.95}, label="clearly_as_good")
    assert chance_weakness_items(enrollment_policy(), stored_final_chance(at_threshold, _SCOPE), positive_net_required=True) == []
