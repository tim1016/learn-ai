"""The paired comparison readings: exact counts, quantiles, classifications and admission (#3011)."""

from __future__ import annotations

import copy
import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.research.golden_search.chance import (
    MEAN_BLOCK_SESSIONS,
    REPLICATIONS,
    SessionSeries,
    path_net_return,
    path_objective,
    path_sharpe,
    seed_for,
)
from app.research.golden_search.chance_compare import (
    DERIVED_RETURN_OVERFLOW,
    INCONSISTENT_CAPITAL,
    INCUMBENT_UNAVAILABLE,
    MISSING_SCHEDULED_SESSIONS,
    NON_CANONICAL_CLOSE,
    NOT_MEASURED,
    SCHEMA_PROFITABILITY,
    TOO_FEW_SESSIONS,
    UNSCHEDULED_SESSION,
    _classify,
    _pair_chunk,
    _profitability_reading,
    _range,
    admit_run,
    development_readings,
    incumbent_readings,
    series_digest,
    stored_chance,
)
from app.research.golden_search.chance_drawdown import stored_drawdown
from app.research.golden_search.chance_rehearsal import stored_rehearsal
from app.research.golden_search.selection import Metrics
from app.schemas.golden_search import GoldenSearchChance
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


def test_injected_paths_count_events_exactly_and_an_overflowing_difference_keeps_its_count() -> None:
    candidate = _series([0.1, -0.2, 0.5])
    incumbent = _series([0.05, -0.1, 0.2])
    indices = np.array([[0, 1, 2], [2, 1, 0], [0, 0, 1], [1, 1, 1], [2, 2, 0]])
    own = path_objective(candidate.returns[indices], "sharpe_ratio", CAPITAL)
    other = path_objective(incumbent.returns[indices], "sharpe_ratio", CAPITAL)
    counts, differences = _pair_chunk(own, other)
    # Row [0,1,2] and its reversal give both sides their own multiset, so the candidate's Sharpe beats the incumbent's twice;
    # row [0,0,1] centers both sides exactly on a 0.0 Sharpe (a tie); row [1,1,1] is undefined for the candidate.
    assert counts == {"better": 2, "tied": 1, "worse": 1, "undefined": 1}, "injected paths count each event exactly"
    assert sum(counts.values()) == len(indices), "every injected path is counted once"
    assert np.isnan(differences[3]), "the path undefined for the candidate has no difference"
    overflow_counts, overflow_differences = _pair_chunk(np.array([1e308, 5.0]), np.array([-1e308, 2.0]))
    assert overflow_counts == {"better": 2, "tied": 0, "worse": 0, "undefined": 0}, "a finite subtraction overflow keeps the count"
    assert np.isnan(overflow_differences[0]) and overflow_differences[1] == 3.0, "a finite subtraction overflow loses the difference"


def test_a_range_is_linear_interpolation_over_the_finite_values_and_explicitly_empty_without_any() -> None:
    values = np.array([0.32, -7.0, 1.5, np.nan, 0.0, 4.0, -0.25, 9.0, 2.5])
    result = _range(values)
    finite = np.sort(np.array([-7.0, -0.25, 0.0, 0.32, 1.5, 2.5, 4.0, 9.0]))
    # The linear-interpolation positions (n - 1) q, computed by hand rather than by numpy.
    def at(fraction: float) -> float:
        position = (len(finite) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        return float(finite[lower] + (finite[upper] - finite[lower]) * (position - lower))

    assert result["finite"] == 8, "the range counts the finite values"
    assert result["p5"] == pytest.approx(at(0.05), abs=1e-15), "p5 is the linear interpolation over the finite values"
    assert result["p50"] == pytest.approx(at(0.5), abs=1e-15), "p50 is the linear interpolation over the finite values"
    assert result["p95"] == pytest.approx(at(0.95), abs=1e-15), "p95 is the linear interpolation over the finite values"
    assert _range(np.full(4, np.nan)) == {"p5": None, "p50": None, "p95": None, "finite": 0}, "a range without finite values is explicitly empty, never NaN"


# ── Classification: raw counts against stored thresholds ──


def _counts(better: int, tied: int, worse: int, undefined: int = 0) -> dict[str, int]:
    return {"better": better, "tied": tied, "worse": worse, "undefined": undefined}


@pytest.mark.parametrize(
    ("better", "tied", "worse", "undefined", "expected"),
    [
        pytest.param(9_500, 0, 500, 0, ("clearly_as_good", False), id="the_clear_threshold_is_exact_at_9500_better_of_10000"),
        pytest.param(0, 9_500, 500, 0, ("clearly_as_good", False), id="the_clear_threshold_is_exact_at_9500_tied_of_10000"),
        pytest.param(9_499, 0, 501, 0, ("mixed", False), id="the_clear_threshold_is_not_met_at_9499_of_10000"),
        # 9,500 undefined paths with only 500 finite losses must not read as clearly worse, whatever 1 minus the success share says.
        pytest.param(0, 0, 500, 9_500, ("mixed", True), id="an_adverse_label_needs_actual_finite_worse_outcomes"),
        pytest.param(0, 0, 9_500, 0, ("clearly_worse", False), id="an_adverse_label_at_9500_finite_worse_outcomes"),
        pytest.param(10, 0, 9_499, 0, ("mixed", False), id="an_adverse_label_is_not_met_at_9499_worse_outcomes"),
        pytest.param(0, 10_000, 0, 0, ("identical_outcomes", False), id="an_all_tie_draw_is_not_strong_improvement"),
        pytest.param(0, 0, 0, 10_000, ("no_defined_paths", True), id="no_defined_paths_leaves_the_comparison_unavailable"),
        pytest.param(9_600, 0, 100, 300, ("clearly_as_good", True), id="mixed_undefined_cases_read_as_incomplete_support"),
    ],
)
def test_the_classification_reads_raw_counts_against_the_clear_threshold(better: int, tied: int, worse: int, undefined: int, expected: tuple[str, bool]) -> None:
    assert _classify(_counts(better, tied, worse, undefined), 0.95) == expected


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


# ── The independent compounded-net range (#3026) ─────────────────────────


def _profitability_of(returns: list[float], *, objective: str = "total_return_pct", hash_: str = "cafebabe") -> dict[str, Any]:
    return _profitability_reading(_series(returns), objective=objective, window=_window_for(len(returns)), candidate_hash=hash_, sessions_label="development", cancel_check=lambda: None)


def test_a_positive_sharpe_can_compound_to_a_loss_and_the_range_says_so() -> None:
    # The #3026 counterexample: 20 sessions alternating +50% and -40% have a positive session Sharpe
    # (+1.7191729277636834) but compound to -65.13%, so the objective's zero and break-even are not one axis.
    alternating = [0.5, -0.4] * 10
    series = _series(alternating)
    assert path_sharpe(series.returns.reshape(1, -1))[0] == pytest.approx(1.7191729277636834, rel=0.0, abs=1e-9)
    assert float(np.prod(1.0 + series.returns) - 1.0) == pytest.approx(-0.6513215599, rel=0.0, abs=1e-9)

    readings = _profitability_of(alternating)
    net_range = readings["net_return_range"]
    assert net_range["quantity"] == {"measure": "compounded_net_return", "label": "compounded net return", "units": "fraction of the starting capital"}
    assert net_range["p5"] is not None and net_range["p50"] is not None and net_range["p95"] is not None
    assert net_range["p5"] <= net_range["p50"] <= net_range["p95"]
    assert net_range["p50"] < 0.0  # the median resampled path loses money whatever the Sharpe says
    assert readings["counts"]["negative"] > readings["counts"]["positive"]
    assert net_range["finite"] == sum(readings["counts"][key] for key in ("positive", "zero", "negative"))


def test_the_net_return_range_of_a_flat_an_overflowing_and_a_drifting_series() -> None:
    flat = _profitability_of([0.0] * 40)
    flat_range = flat["net_return_range"]
    assert flat["counts"] == {"positive": 0, "zero": 10_000, "negative": 0, "undefined": 0}, "a flat series compounds to exactly zero on every path"
    assert (flat_range["p5"], flat_range["p50"], flat_range["p95"]) == (0.0, 0.0, 0.0), "a flat series ranges exactly zero, never a rounded one"
    assert flat_range["finite"] == 10_000, "every path of a flat series is finite"

    # Sessions alternating +1e30 and flat: each session's own (1 + r) is finite, but a resampled path that
    # draws the big return eleven times or more compounds past every float, and that path is undefined — not
    # a loss, not a gain.
    overflowing = [1e30, 0.0] * 20
    overflow = _profitability_of(overflowing)
    overflow_range = overflow["net_return_range"]
    assert overflow["counts"]["undefined"] > 0, "paths that overflow are counted undefined"
    assert overflow_range["finite"] < 10_000 == REPLICATIONS, "paths that overflow leave fewer finite results than replications"
    assert overflow_range["finite"] == sum(overflow["counts"][key] for key in ("positive", "zero", "negative")), "the overflow range counts exactly the paths that are money"
    for key in ("p5", "p50", "p95"):
        assert overflow_range[key] is None or np.isfinite(overflow_range[key]), f"the overflow range's {key} is undefined or finite, never money"

    drifting = list(np.random.default_rng(2).normal(0.01, 0.004, 60))  # a strong, steady per-session gain
    drift = _profitability_of(drifting)
    drift_range = drift["net_return_range"]
    assert drift["counts"]["positive"] == 10_000, "a genuinely positive series is positive on every path"
    assert drift_range["p5"] > 0.0, "a genuinely positive series ranges positive from p5"
    assert drift_range["p95"] > drift_range["p5"], "a genuinely positive series ranges upward"


def test_the_range_is_the_same_draw_the_counts_came_from() -> None:
    returns = _smooth(60, 77)
    window = _window_for(60)
    seed_inputs = [SCHEMA_PROFITABILITY, window[0], window[1], "total_return_pct", "cafebabe"]
    seed = seed_for(*seed_inputs)
    series = _series(returns)
    redrawn = np.full(REPLICATIONS, np.nan)
    done = 0
    from app.research.monte_carlo.stationary import stationary_index_chunks

    for chunk in stationary_index_chunks(len(returns), size=len(returns), mean_block=MEAN_BLOCK_SESSIONS, replications=REPLICATIONS, rng=np.random.default_rng(seed)):
        redrawn[done : done + len(chunk)] = path_net_return(series.returns[chunk])
        done += len(chunk)
    expected = _range(redrawn)
    readings = _profitability_of(returns)
    net_range = readings["net_return_range"]
    # Identical operations over the identical redrawn array: exact equality, never a rounded agreement (#3026).
    assert (net_range["p5"], net_range["p50"], net_range["p95"], net_range["finite"]) == (expected["p5"], expected["p50"], expected["p95"], expected["finite"])


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


def test_identical_series_read_as_ties_and_identical_settings_as_the_same_settings_without_drawing() -> None:
    same = _smooth(60, seed=9)
    readings = _reading(same, same)
    comparison = readings["comparison"]
    assert comparison["label"] == "identical_outcomes", "identical series draw an identical outcome reading"
    assert comparison["counts"] == {"better": 0, "tied": 10_000, "worse": 0, "undefined": 0}, "identical series tie on every path"
    assert "tie is not an improvement" in comparison["sentence"], "the tie sentence says a tie is not an improvement"

    twin = _reading(_smooth(60, seed=10), _smooth(60, seed=11), hashes=("twin", "twin"))
    assert twin["comparison"]["status"] == "same_settings", "identical settings say same settings"
    assert twin["comparison"]["basis"]["seed"]["inputs"] == [], "identical settings draw nothing"
    assert twin["profitability"]["status"] == "measured", "identical settings still measure the profitability"


def test_the_incumbent_is_a_baseline_never_compared_with_itself() -> None:
    readings = incumbent_readings(objective="sharpe_ratio", window=_window_for(60))
    assert readings["comparison"]["status"] == "baseline"
    assert readings["profitability"]["status"] == "missing"
    assert readings["profitability"]["code"] == "not_measured"


def test_a_comparison_that_cannot_be_made_is_a_named_reading_and_erases_only_the_comparison() -> None:
    window = _window_for(60)
    no_incumbent = development_readings(
        objective="sharpe_ratio",
        window=window,
        candidate_metrics=_completed(),
        candidate_detail=_detail(_smooth(60, seed=12)),
        candidate_hash="cafebabe",
        incumbent_metrics=None,
        incumbent_detail=None,
        incumbent_hash="deadbeef",
    )
    assert no_incumbent["comparison"]["status"] == "missing", "a missing incumbent erases the comparison"
    assert no_incumbent["comparison"]["code"] == INCUMBENT_UNAVAILABLE[0], "a missing incumbent names the incumbent as unavailable"
    assert no_incumbent["profitability"]["status"] == "measured", "a missing incumbent erases only the comparison"

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
    assert not_evaluated["comparison"]["code"] == "not_evaluated", "an unevaluated run is a named missing reading"
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
    assert failed_reading["comparison"]["code"] == "failed_run", "a failed run is a named missing reading"
    assert "engine refused" in failed_reading["comparison"]["reason"], "a failed run's reading carries the engine's reason"

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
    assert uncovered["comparison"]["code"] == INCUMBENT_UNAVAILABLE[0], "an incumbent short of a scheduled session refuses the pairing"

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
    assert inconsistent["comparison"]["code"] == INCONSISTENT_CAPITAL[0], "an incumbent with another capital refuses the pairing"
    # The fit's own profitability is independent of the pairing's scope.
    assert inconsistent["profitability"]["status"] == "measured", "mismatched capital refuses the pairing, not the fit"

    malformed = {"daily_equity": []}
    incumbent_crash = development_readings(
        objective="sharpe_ratio",
        window=_window_for(60),
        candidate_metrics=_completed(),
        candidate_detail=_detail(_smooth(60, seed=34)),
        candidate_hash="cafebabe",
        incumbent_metrics=_completed(),
        incumbent_detail=malformed,
        incumbent_hash="deadbeef",
    )
    assert incumbent_crash["comparison"]["status"] == "failed", "an incumbent admission crash is a failed comparison, not an unavailable one"
    assert "KeyError" in incumbent_crash["comparison"]["reason"], "the incumbent admission crash names its error"
    # The fit's own profitability is untouched by the incumbent's crash.
    assert incumbent_crash["profitability"]["status"] == "measured", "the incumbent's crash leaves the fit's profitability untouched"


# ── Admission boundary checks ─────────────────────────────────────────────


def test_admission_accepts_a_run_that_covers_its_scheduled_window_and_refuses_one_that_does_not() -> None:
    covered = admit_run(_completed(), _detail(_smooth(60, seed=14)), _window_for(60))
    assert isinstance(covered, SessionSeries), "a run that covers its scheduled window is admitted"

    gapped = _detail(_smooth(60, seed=15))
    del gapped["daily_equity"][30]
    missing = admit_run(_completed(), gapped, _window_for(60))
    assert missing["code"] == MISSING_SCHEDULED_SESSIONS[0], "a run missing a scheduled session is refused, not repaired"
    assert "1 scheduled session missing" in missing["reason"], "the refusal counts the missing scheduled session"

    stamped = _detail(_smooth(60, seed=16))
    stamped["daily_equity"][30][0] += 1_000
    off_close = admit_run(_completed(), stamped, _window_for(60))
    assert off_close["code"] == NON_CANONICAL_CLOSE[0], "a session stamped off the canonical close is refused"

    padded = _detail(_smooth(60, seed=16))
    extra = session_close_ms_utc(expected_sessions(date(2025, 1, 2), date(2027, 12, 31))[60])
    padded["daily_equity"].append([extra, CAPITAL])
    unscheduled = admit_run(_completed(), padded, _window_for(60))
    assert unscheduled["code"] == UNSCHEDULED_SESSION[0], "a run holding an unscheduled session is refused"

    # The code is read off the series refusal's wording, so a reworded refusal would silently become "unreadable_series".
    short = admit_run(_completed(), _detail(_smooth(10, seed=18)), _window_for(10))

    assert short["status"] == "missing" and short["code"] == TOO_FEW_SESSIONS[0], "a run too short to resample reads too few sessions"
    assert short["reason"].startswith(TOO_FEW_SESSIONS[1]) and "10 sessions" in short["reason"], "the too-short refusal carries the series' own sentence"


@pytest.mark.filterwarnings("ignore:overflow encountered in divide:RuntimeWarning")
def test_equity_whose_session_return_overflows_is_refused_not_resampled() -> None:
    # Every equity value is finite and positive, but one session's ratio to the last is beyond a float.
    detail = _detail(_smooth(60, seed=19))
    detail["daily_equity"][30][1] = 1e-300
    detail["daily_equity"][31][1] = 1e300

    admitted = admit_run(_completed(), detail, _window_for(60))

    assert isinstance(admitted, dict), "an overflowing series was admitted for resampling"
    assert admitted["status"] == "missing" and admitted["code"] == DERIVED_RETURN_OVERFLOW[0]


# ── Stored-shape normalization ────────────────────────────────────────────


def test_a_legacy_or_unsupported_record_reads_not_measured_or_unavailable_per_component_without_backfill() -> None:
    measured = _reading(_smooth(60, seed=25), _smooth(60, seed=26))
    # Evidence recorded after #3011 shipped but before a later slice: the comparison stays, the rest reads missing.
    partial = stored_chance({"chance": {"comparison": copy.deepcopy(measured["comparison"])}})
    assert partial["comparison"] == measured["comparison"], "a partial legacy record keeps its stored comparison"
    assert partial["profitability"]["status"] == "missing", "a partial legacy record reads the unrecorded profitability as missing"
    assert partial["profitability"]["code"] == NOT_MEASURED[0], "a partial legacy record reads the unrecorded profitability as not measured"
    # A study from before #3011 has no chance record at all.
    absent = stored_chance({})
    assert absent["comparison"]["code"] == NOT_MEASURED[0], "a study with no chance record reads the comparison as not measured"
    assert absent["profitability"]["code"] == NOT_MEASURED[0], "a study with no chance record reads the profitability as not measured"
    normalized = stored_chance({"chance": {"comparison": {"status": "from_the_future"}, "profitability": None}})
    assert normalized["comparison"]["status"] == "missing", "an unsupported stored status reads as missing"
    assert normalized["comparison"]["code"] == "unsupported", "an unsupported stored status says it is unsupported"
    assert "from_the_future" in normalized["comparison"]["reason"], "an unsupported stored status names the status it could not read"
    assert normalized["profitability"]["code"] == "not_measured", "a profitability stored as None reads as not measured"
    assert "profitability readings were first recorded" in normalized["profitability"]["reason"], "a not-measured profitability says when readings were first recorded"


def test_a_malformed_stored_component_reads_unavailable_and_never_breaks_the_response() -> None:
    malformed = stored_chance({"chance": {"comparison": "garbage", "profitability": {"status": "measured", "label": "mixed"}}})
    # Not a record at all reads as never measured; a record that fails the served contract says it failed it.
    assert malformed["comparison"]["status"] == "missing"
    assert malformed["comparison"]["code"] == NOT_MEASURED[0]
    assert malformed["profitability"]["status"] == "missing"
    assert malformed["profitability"]["code"] == "invalid_record"
    # What a study page is served satisfies its wire model: a malformed sibling never fails the whole response.
    GoldenSearchChance.model_validate({**malformed, "rehearsal": stored_rehearsal({}), "drawdown": stored_drawdown({})})


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


# ── Cancellation and diagnostic failures ──────────────────────────────────


def test_cancellation_between_chunks_stops_the_stage_and_is_never_a_reading() -> None:
    from app.jobs.progress import JobCancelled

    def cancelled() -> None:
        raise JobCancelled("job 42 cancelled")

    with pytest.raises(JobCancelled):
        _reading(_smooth(60, seed=31), _smooth(60, seed=32), cancel_check=cancelled)
    # The profitability draw alone also stops: cancellation never becomes a failed or missing record.
    with pytest.raises(JobCancelled):
        development_readings(
            objective="total_return_pct",
            window=_window_for(60),
            candidate_metrics=_completed(),
            candidate_detail=_detail(_smooth(60, seed=31)),
            candidate_hash="cafebabe",
            incumbent_metrics=None,
            incumbent_detail=None,
            incumbent_hash="deadbeef",
            cancel_check=cancelled,
        )


def test_an_admission_crash_is_a_diagnostic_failure_with_its_sentence_not_a_missing_reading() -> None:
    malformed = {"daily_equity": []}  # no initial_cash: session_series itself raises
    readings = development_readings(
        objective="sharpe_ratio",
        window=_window_for(60),
        candidate_metrics=_completed(),
        candidate_detail=malformed,
        candidate_hash="cafebabe",
        incumbent_metrics=_completed(),
        incumbent_detail=_detail(_smooth(60, seed=33)),
        incumbent_hash="deadbeef",
    )
    for component in ("comparison", "profitability"):
        assert readings[component]["status"] == "failed"
        assert "KeyError" in readings[component]["reason"]
        assert readings[component]["sentence"].startswith(f"The {component} reading could not be computed")
