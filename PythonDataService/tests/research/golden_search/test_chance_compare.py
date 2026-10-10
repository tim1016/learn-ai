"""The paired comparison readings: exact counts, quantiles, classifications and admission (#3011)."""

from __future__ import annotations

import copy
import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.research.golden_search.chance import SessionSeries, path_objective
from app.research.golden_search.chance_compare import (
    INCONSISTENT_CAPITAL,
    INCUMBENT_UNAVAILABLE,
    MISSING_SCHEDULED_SESSIONS,
    NON_CANONICAL_CLOSE,
    NOT_MEASURED,
    UNSCHEDULED_SESSION,
    _classify,
    _pair_chunk,
    _range,
    admit_run,
    development_readings,
    incumbent_readings,
    series_digest,
    stored_chance,
)
from app.research.golden_search.selection import Metrics
from app.utils.session_anchors import et_midnight_ms

CAPITAL = 100_000.0


def _completed() -> Metrics:
    return Metrics(status="completed", total_trades=40, net_profit=1_000.0, total_return_pct=0.01, sharpe_ratio=1.0, max_drawdown_pct=0.1, win_rate=0.5)


def _series(returns: list[float], *, capital: float = CAPITAL, session_ms: tuple[int, ...] | None = None) -> SessionSeries:
    if session_ms is None:
        session_ms = tuple(session_close_ms_utc(day) for day in expected_sessions(date(2025, 1, 2), date(2027, 12, 31))[: len(returns)])
    return SessionSeries(session_ms=session_ms, returns=np.asarray(returns, dtype=np.float64), exits=np.zeros(len(returns), dtype=np.int64), capital=capital)


def _window_for(n: int) -> tuple[int, int]:
    """A stage window whose scheduled sessions are exactly the first ``n`` NYSE sessions from 2025-01-02."""
    days = expected_sessions(date(2025, 1, 2), date(2027, 12, 31))[:n]
    return et_midnight_ms(days[0]), et_midnight_ms(days[-1] + timedelta(days=1))


def _smooth(n: int, seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(rng.normal(0.0005, 0.01, n))


def _detail(returns: list[float], *, capital: float = CAPITAL) -> dict[str, Any]:
    series = _series(returns, capital=capital)
    equity = [capital, *(capital * (1.0 + value) for value in returns)]
    return {
        "initial_cash": capital,
        "daily_equity": [[ms, value] for ms, value in zip(series.session_ms, equity[1:], strict=True)],
        "trades": [],
    }


# ── Injected index paths: exact counts and independently calculated quantiles ──


def test_injected_paths_count_events_exactly() -> None:
    candidate = _series([0.1, -0.2, 0.5])
    incumbent = _series([0.05, -0.1, 0.2])
    indices = np.array([[0, 1, 2], [2, 1, 0], [0, 0, 1], [1, 1, 1], [2, 2, 0]])
    own = path_objective(candidate.returns[indices], "sharpe_ratio", CAPITAL)
    other = path_objective(incumbent.returns[indices], "sharpe_ratio", CAPITAL)
    counts, differences = _pair_chunk(own, other)
    # Row [0,1,2] and its reversal give both sides their own multiset, so the candidate's Sharpe beats the incumbent's twice;
    # row [0,0,1] centers both sides exactly on a 0.0 Sharpe (a tie); row [1,1,1] is undefined for the candidate.
    assert counts == {"better": 2, "tied": 1, "worse": 1, "undefined": 1}
    assert sum(counts.values()) == len(indices)
    assert np.isnan(differences[3])


def test_quantiles_are_linear_interpolation_over_the_finite_values() -> None:
    values = np.array([0.32, -7.0, 1.5, np.nan, 0.0, 4.0, -0.25, 9.0, 2.5])
    result = _range(values)
    finite = np.sort(np.array([-7.0, -0.25, 0.0, 0.32, 1.5, 2.5, 4.0, 9.0]))
    # The linear-interpolation positions (n - 1) q, computed by hand rather than by numpy.
    def at(fraction: float) -> float:
        position = (len(finite) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        return float(finite[lower] + (finite[upper] - finite[lower]) * (position - lower))

    assert result["finite"] == 8
    assert result["p5"] == pytest.approx(at(0.05), abs=1e-15)
    assert result["p50"] == pytest.approx(at(0.5), abs=1e-15)
    assert result["p95"] == pytest.approx(at(0.95), abs=1e-15)


def test_a_range_without_finite_values_is_explicitly_empty_never_nan() -> None:
    assert _range(np.full(4, np.nan)) == {"p5": None, "p50": None, "p95": None, "finite": 0}


def test_finite_subtraction_overflow_keeps_the_count_and_loses_the_difference() -> None:
    counts, differences = _pair_chunk(np.array([1e308, 5.0]), np.array([-1e308, 2.0]))
    assert counts == {"better": 2, "tied": 0, "worse": 0, "undefined": 0}
    assert np.isnan(differences[0]) and differences[1] == 3.0


# ── Classification: raw counts against stored thresholds ──


def _counts(better: int, tied: int, worse: int, undefined: int = 0) -> dict[str, int]:
    return {"better": better, "tied": tied, "worse": worse, "undefined": undefined}


def test_the_clear_threshold_is_exact_at_9500_of_10000() -> None:
    assert _classify(_counts(9_500, 0, 500), 0.95) == ("clearly_as_good", False)
    assert _classify(_counts(0, 9_500, 500), 0.95) == ("clearly_as_good", False)
    assert _classify(_counts(9_499, 0, 501), 0.95) == ("mixed", False)


def test_an_adverse_label_needs_actual_finite_worse_outcomes() -> None:
    # 9,500 undefined paths with only 500 finite losses must not read as clearly worse, whatever 1 minus the success share says.
    assert _classify(_counts(0, 0, 500, undefined=9_500), 0.95) == ("mixed", True)
    assert _classify(_counts(0, 0, 9_500), 0.95) == ("clearly_worse", False)
    assert _classify(_counts(10, 0, 9_499), 0.95) == ("mixed", False)


def test_an_all_tie_draw_is_not_strong_improvement() -> None:
    assert _classify(_counts(0, 10_000, 0), 0.95) == ("identical_outcomes", False)


def test_no_defined_paths_leaves_the_comparison_unavailable() -> None:
    assert _classify(_counts(0, 0, 0, undefined=10_000), 0.95) == ("no_defined_paths", True)


def test_mixed_undefined_cases_read_as_incomplete_support() -> None:
    assert _classify(_counts(9_600, 0, 100, undefined=300), 0.95) == ("clearly_as_good", True)


# ── The reading itself ────────────────────────────────────────────────────


def _reading(candidate: list[float], incumbent: list[float], *, hashes: tuple[str, str] = ("cafebabe", "deadbeef"), **kwargs: Any) -> dict[str, Any]:
    window = _window_for(len(candidate))
    readings = development_readings(
        objective="total_return_pct",
        window=window,
        candidate_metrics=_completed(),
        candidate_detail=_detail(candidate),
        candidate_hash=hashes[0],
        incumbent_metrics=_completed(),
        incumbent_detail=_detail(incumbent),
        incumbent_hash=hashes[1],
        **kwargs,
    )
    return readings


def test_a_measured_reading_counts_every_replication_against_the_stored_basis() -> None:
    readings = _reading(_smooth(60, seed=1), _smooth(60, seed=2))
    comparison = readings["comparison"]
    assert comparison["status"] == "measured"
    counts = comparison["counts"]
    assert sum(counts.values()) == 10_000
    assert comparison["at_least_as_good"]["count"] == counts["better"] + counts["tied"]
    basis = comparison["basis"]
    assert basis["schema_version"] == "golden-search-chance-comparison/v1"
    assert basis["replications"] == 10_000 and basis["chunk_replications"] == 1_000 and basis["mean_block_sessions"] == 5.0
    assert basis["thresholds"] == {"clear": 0.95}
    assert basis["objective"]["measure"] == "total_return_pct"
    assert basis["source"]["session_count"] == 60
    assert basis["source"]["candidate_point_hash"] == "cafebabe" and basis["source"]["incumbent_point_hash"] == "deadbeef"
    assert basis["seed"]["value"] > 0
    assert comparison["candidate_range"]["finite"] == 10_000 and comparison["difference_range"]["finite"] == 10_000


def test_profitability_counts_over_the_same_window_but_its_own_seed() -> None:
    readings = _reading(_smooth(60, seed=3), _smooth(60, seed=4))
    profitability = readings["profitability"]
    assert profitability["status"] == "measured"
    assert sum(profitability["counts"].values()) == 10_000
    assert profitability["basis"]["schema_version"] == "golden-search-chance-profitability/v1"
    assert profitability["basis"]["seed"]["inputs"] != readings["comparison"]["basis"]["seed"]["inputs"]


def test_the_same_inputs_redraw_identically_for_retry_and_reopen() -> None:
    first = _reading(_smooth(60, seed=5), _smooth(60, seed=6))
    second = _reading(_smooth(60, seed=5), _smooth(60, seed=6))
    assert first == second


def test_swapping_the_comparison_direction_exchanges_better_and_worse_only() -> None:
    candidate, incumbent = _smooth(60, seed=7), _smooth(60, seed=8)
    forward = _reading(candidate, incumbent, hashes=("cafebabe", "deadbeef"))["comparison"]
    backward = _reading(incumbent, candidate, hashes=("deadbeef", "cafebabe"))["comparison"]
    assert forward["counts"]["better"] == backward["counts"]["worse"]
    assert forward["counts"]["worse"] == backward["counts"]["better"]
    assert forward["counts"]["tied"] == backward["counts"]["tied"]
    assert forward["counts"]["undefined"] == backward["counts"]["undefined"]
    # The pair's seed inputs are one canonically ordered list, so both directions draw the same indices.
    assert forward["basis"]["seed"]["inputs"] == backward["basis"]["seed"]["inputs"]


def test_identical_series_draw_an_identical_outcome_reading() -> None:
    same = _smooth(60, seed=9)
    readings = _reading(same, same)
    comparison = readings["comparison"]
    assert comparison["label"] == "identical_outcomes"
    assert comparison["counts"] == {"better": 0, "tied": 10_000, "worse": 0, "undefined": 0}
    assert "tie is not an improvement" in comparison["sentence"]


def test_identical_settings_say_same_settings_without_drawing() -> None:
    readings = _reading(_smooth(60, seed=10), _smooth(60, seed=11), hashes=("twin", "twin"))
    assert readings["comparison"]["status"] == "same_settings"
    assert readings["comparison"]["basis"]["seed"]["inputs"] == []
    assert readings["profitability"]["status"] == "measured"


def test_the_incumbent_is_a_baseline_never_compared_with_itself() -> None:
    readings = incumbent_readings(objective="sharpe_ratio", window=_window_for(60), session_count=60)
    assert readings["comparison"]["status"] == "baseline"
    assert readings["profitability"]["status"] == "missing"
    assert readings["profitability"]["code"] == "not_measured"


def test_a_missing_incumbent_erases_only_the_comparison() -> None:
    window = _window_for(60)
    readings = development_readings(
        objective="sharpe_ratio",
        window=window,
        candidate_metrics=_completed(),
        candidate_detail=_detail(_smooth(60, seed=12)),
        candidate_hash="cafebabe",
        incumbent_metrics=None,
        incumbent_detail=None,
        incumbent_hash="deadbeef",
    )
    assert readings["comparison"]["status"] == "missing"
    assert readings["comparison"]["code"] == INCUMBENT_UNAVAILABLE[0]
    assert readings["profitability"]["status"] == "measured"


def test_an_unevaluated_or_failed_run_is_a_named_missing_reading() -> None:
    window = _window_for(60)
    not_evaluated = development_readings(
        objective="sharpe_ratio",
        window=window,
        candidate_metrics=None,
        candidate_detail=None,
        candidate_hash="cafebabe",
        incumbent_metrics=_completed(),
        incumbent_detail=_detail(_smooth(60, seed=13)),
        incumbent_hash="deadbeef",
    )
    assert not_evaluated["comparison"]["code"] == "not_evaluated"
    failed = Metrics(status="failed", total_trades=0, net_profit=None, total_return_pct=None, sharpe_ratio=None, max_drawdown_pct=None, win_rate=None, error="engine refused")
    failed_reading = development_readings(
        objective="sharpe_ratio",
        window=window,
        candidate_metrics=failed,
        candidate_detail=None,
        candidate_hash="cafebabe",
        incumbent_metrics=_completed(),
        incumbent_detail=_detail(_smooth(60, seed=13)),
        incumbent_hash="deadbeef",
    )
    assert failed_reading["comparison"]["code"] == "failed_run"
    assert "engine refused" in failed_reading["comparison"]["reason"]


# ── Admission boundary checks ─────────────────────────────────────────────


def test_admission_accepts_a_run_that_covers_its_scheduled_window() -> None:
    admitted = admit_run(_completed(), _detail(_smooth(60, seed=14)), _window_for(60))
    assert isinstance(admitted, SessionSeries)


def test_a_run_missing_a_scheduled_session_is_refused_not_repaired() -> None:
    detail = _detail(_smooth(60, seed=15))
    del detail["daily_equity"][30]
    missing = admit_run(_completed(), detail, _window_for(60))
    assert missing["code"] == MISSING_SCHEDULED_SESSIONS[0]
    assert "1 scheduled session missing" in missing["reason"]


def test_a_session_stamped_off_the_canonical_close_is_refused() -> None:
    detail = _detail(_smooth(60, seed=16))
    detail["daily_equity"][30][0] += 1_000
    admitted = admit_run(_completed(), detail, _window_for(60))
    assert admitted["code"] == NON_CANONICAL_CLOSE[0]


def test_a_run_holding_an_unscheduled_session_is_refused() -> None:
    detail = _detail(_smooth(60, seed=16))
    extra = session_close_ms_utc(expected_sessions(date(2025, 1, 2), date(2027, 12, 31))[60])
    detail["daily_equity"].append([extra, CAPITAL])
    admitted = admit_run(_completed(), detail, _window_for(60))
    assert admitted["code"] == UNSCHEDULED_SESSION[0]


def test_mismatched_sessions_or_capital_refuse_the_pairing_not_the_fit() -> None:
    # An incumbent that misses a scheduled session of the shared window fails its own admission first.
    uncovered = development_readings(
        objective="sharpe_ratio",
        window=_window_for(60),
        candidate_metrics=_completed(),
        candidate_detail=_detail(_smooth(60, seed=17)),
        candidate_hash="cafebabe",
        incumbent_metrics=_completed(),
        incumbent_detail=_detail(_smooth(59, seed=18), capital=CAPITAL),
        incumbent_hash="deadbeef",
    )
    assert uncovered["comparison"]["code"] == INCUMBENT_UNAVAILABLE[0]

    inconsistent = development_readings(
        objective="sharpe_ratio",
        window=_window_for(60),
        candidate_metrics=_completed(),
        candidate_detail=_detail(_smooth(60, seed=19)),
        candidate_hash="cafebabe",
        incumbent_metrics=_completed(),
        incumbent_detail=_detail(_smooth(60, seed=20), capital=50_000.0),
        incumbent_hash="deadbeef",
    )
    assert inconsistent["comparison"]["code"] == INCONSISTENT_CAPITAL[0]
    # The fit's own profitability is independent of the pairing's scope.
    assert inconsistent["profitability"]["status"] == "measured"


# ── Stored-shape normalization ────────────────────────────────────────────


def test_a_legacy_record_reads_not_measured_per_component_without_backfill() -> None:
    measured = _reading(_smooth(60, seed=25), _smooth(60, seed=26))
    # Evidence recorded after #3011 shipped but before a later slice: the comparison stays, the rest reads missing.
    partial = stored_chance({"chance": {"comparison": copy.deepcopy(measured["comparison"])}})
    assert partial["comparison"] == measured["comparison"]
    assert partial["profitability"]["status"] == "missing"
    assert partial["profitability"]["code"] == NOT_MEASURED[0]
    # A study from before #3011 has no chance record at all.
    absent = stored_chance({})
    assert absent["comparison"]["code"] == NOT_MEASURED[0]
    assert absent["profitability"]["code"] == NOT_MEASURED[0]


def test_a_malformed_stored_component_says_why_it_is_unavailable() -> None:
    malformed = stored_chance({"chance": {"comparison": "garbage", "profitability": {"status": "measured", "label": "mixed"}}})
    assert malformed["comparison"]["status"] == "missing"
    assert malformed["comparison"]["code"] == NOT_MEASURED[0]
    assert malformed["profitability"]["status"] == "measured"


def test_series_digest_distinguishes_inputs_and_ignores_object_identity() -> None:
    first = _series(_smooth(30, seed=27))
    second = _series(list(first.returns))
    other = _series(_smooth(30, seed=28))
    assert series_digest(first) == series_digest(second)
    assert series_digest(first) != series_digest(other)


def test_json_never_holds_nan_or_infinity() -> None:
    from app.research.golden_search.protocol import canonical_json

    readings = _reading(_smooth(60, seed=29), _smooth(60, seed=30))
    canonical_json(readings)  # raises on NaN/Infinity: a measured reading must serialize cleanly
    overflow_only = _pair_chunk(np.array([1e308]), np.array([-1e308]))
    canonical_json({"counts": overflow_only[0]})
