"""Paired comparison readings: how a fit's resampled development result stands against the current settings' (#3011, ADR 0074 decision 12).

Formula: over the development window's n scheduled sessions, a candidate run
  and the incumbent run give session series (r^c_1..r^c_n) and
  (r^i_1..r^i_n). B paired stationary-bootstrap draws (``chance.py``'s
  convention) take the same index path for both sides, so each replication b
  holds one candidate path and one incumbent path of the same sessions. On
  each pair the plan's objective O (Sharpe, total return, or net profit;
  ``chance.path_objective``) is counted once:
  better when O_c(b) > O_i(b), tied when O_c(b) == O_i(b), worse when
  O_c(b) < O_i(b), undefined when either side is NaN; at-least-as-good =
  better + tied over all B. Candidate and difference 5th/50th/95th
  percentiles are linear interpolation over the finite values. Separately,
  the candidate's own paths count compounded net return > 0, == 0, < 0 and
  undefined over B (the profitability component), whose own 5th/50th/95th
  percentiles of the compounded net return are recorded beside the counts
  (#3026) — a quantity in net-return units, independent of the objective:
  a positive Sharpe can compound to a loss, so the objective's zero and
  the profit's break-even are never the same axis.
Reference: issue #3011's delivery contract (the 2026-10-09 adversarial
  review of #2932); the draw is Politis & Romano (1994) via
  ``monte_carlo/stationary.py``; classification thresholds are the owner's
  interpretation heuristics recorded in ADR 0074 decision 12, not calibrated
  error rates.
Canonical implementation: this file for the paired counting, classification
  and reading shape; ``chance.py`` for one path's statistics and the series
  admission; ``monte_carlo/stationary.py`` for the index draw.
Validated against: tests/research/golden_search/test_chance_compare.py
  (injected index paths with exactly counted events, independently
  calculated quantiles, swapped-direction symmetry, boundary thresholds,
  overflow, and the admission checks).

The readings inform. Selection, eligibility, the recommendation and each
final-test outcome do not read them, and no reading here is a probability
of a true edge, a p-value, or a forecast: the shares describe this
resampling model only, and on the development window the fit they describe
was selected on these same sessions, so search-selection bias is not
corrected (the exam's final-window readings pass the two-selection note
instead, since their sessions were never selected on).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from fractions import Fraction
from typing import Any

import numpy as np
from pydantic import TypeAdapter, ValidationError

from app.jobs.progress import JobCancelled
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.research.golden_search import chance
from app.research.golden_search.chance import (
    CLEAR,
    MEAN_BLOCK_SESSIONS,
    REPLICATIONS,
    SessionSeries,
    path_net_return,
    path_objective,
    seed_for,
    session_series,
)
from app.research.golden_search.protocol import canonical_json
from app.research.golden_search.selection import Metrics
from app.research.monte_carlo.stationary import CHUNK_REPLICATIONS, stationary_index_chunks
from app.research.sweep.ranking import RankingMeasure
from app.schemas.golden_search import (
    GoldenSearchChanceComparison,
    GoldenSearchChanceDrawdown,
    GoldenSearchChanceProfitability,
    GoldenSearchChanceRehearsal,
)
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)

#: What a stored reading calls itself; a change to the reading's shape or interpretation needs a new one.
SCHEMA_COMPARISON = "golden-search-chance-comparison/v1"
SCHEMA_PROFITABILITY = "golden-search-chance-profitability/v1"

#: The owner's objective vocabulary: how each measure reads in a sentence, and what its numbers count.
OBJECTIVES: dict[RankingMeasure, tuple[str, str]] = {
    "sharpe_ratio": ("Sharpe ratio", "annualized ratio of session returns (× √252)"),
    "total_return_pct": ("total return", "fraction of the starting capital"),
    "net_profit": ("net profit", "US dollars"),
}

# ── Missing reasons ───────────────────────────────────────────────────────
# Codes are stable identifiers the operator copy explains; every known missing input names one.

NO_TRADE_LIST = ("no_trade_list", chance.NO_TRADE_LIST)
TOO_FEW_SESSIONS = ("too_few_sessions", "The run covers too few sessions to resample; at least 20 are needed.")
NOT_POSITIVE_CAPITAL = ("not_positive_capital", chance.NOT_POSITIVE_CAPITAL)
NOT_FINITE_EQUITY = ("not_finite_equity", chance.NOT_FINITE)
REACHED_ZERO = ("reached_zero", chance.REACHED_ZERO)
OUT_OF_ORDER_SESSIONS = ("out_of_order_sessions", chance.OUT_OF_ORDER)
UNPLACED_EXIT = ("unplaced_exit", chance.UNPLACED_EXIT)
DERIVED_RETURN_OVERFLOW = (
    "derived_return_overflow",
    "A session return derived from the run's equity is not a finite number, so the series cannot be resampled.",
)

MISSING_SCHEDULED_SESSIONS = (
    "missing_scheduled_sessions",
    "The run does not cover every session the calendar schedules for its window; the gap is not filled, sorted or interpolated.",
)
UNSCHEDULED_SESSION = (
    "unscheduled_session",
    "The run holds a session the calendar does not schedule for its window, so the window's coverage cannot be verified.",
)
NON_CANONICAL_CLOSE = (
    "non_canonical_close",
    "A session's equity is stamped at a time that is not the calendar's canonical close for that session, so two runs cannot be paired by session identity.",
)
UNPAIRED_SESSIONS = (
    "unpaired_sessions",
    "This fit's sessions and the current settings' do not cover the same scheduled sessions, so the two runs cannot be paired path by path.",
)
INCONSISTENT_CAPITAL = (
    "inconsistent_capital",
    "This fit's run and the current settings' start from different capitals, so a paired comparison has no single scope.",
)
INCUMBENT_UNAVAILABLE = (
    "incumbent_unavailable",
    "The current settings' run cannot be read, so no comparison is available; the fit's own readings are unaffected.",
)
NOT_MEASURED = (
    "not_measured",
    "Not measured for this study: comparison readings were first recorded with #3011, and stored evidence is never backfilled.",
)

#: Each ``chance.session_series`` refusal mapped to a stable code, owned beside the sentences it names (#3011).
_SERIES_CODES: dict[str, str] = {
    chance.NO_TRADE_LIST: NO_TRADE_LIST[0],
    chance.NOT_POSITIVE_CAPITAL: NOT_POSITIVE_CAPITAL[0],
    chance.NOT_FINITE: NOT_FINITE_EQUITY[0],
    chance.REACHED_ZERO: REACHED_ZERO[0],
    chance.OUT_OF_ORDER: OUT_OF_ORDER_SESSIONS[0],
    chance.UNPLACED_EXIT: UNPLACED_EXIT[0],
}
#: The too-few-sessions refusal is a composed message; this is its stable opening, defined beside the codes.
TOO_FEW_SESSIONS_PREFIX = "The run covers"

#: The caveat that stays beside every measured comparison: selection on the same data is not corrected away.
BIAS_NOTE = (
    "This fit was selected on these same development sessions. The search that selected it is not repeated inside "
    "the resamples, and search-selection bias is not corrected: even unanimous support is not a claim of a true edge."
)

_Cancel = Callable[[], object]


def _missing(code_and_reason: tuple[str, str], *, detail: str | None = None) -> dict[str, Any]:
    code, reason = code_and_reason
    return {"status": "missing", "code": code, "reason": reason if detail is None else f"{reason} {detail}"}


def _at_least(count: int, total: int, threshold: float) -> bool:
    """``count / total >= threshold`` exactly, so a share that rounds up to the threshold still fails it.

    Counts are integers and the threshold's decimal spelling is exact, so 9,499 of 10,000 at 0.95 is
    below while 9,500 is at it; no float division decides a boundary.
    """
    return Fraction(count, total) >= Fraction(str(threshold))


def series_digest(series: SessionSeries) -> str:
    """One digest over the actual detail inputs a reading resampled: the sessions, returns, exits and capital the run recorded."""
    return hashlib.sha256(
        canonical_json(
            {
                "session_ms": list(series.session_ms),
                "returns": [float(value) for value in series.returns],
                "exits": [int(value) for value in series.exits],
                "capital": series.capital,
            }
        ).encode("utf-8")
    ).hexdigest()


def _window_sessions(window: tuple[int, int]) -> list[date]:
    """The sessions the calendar schedules for a stage window: the end bound is the midnight after the last session."""
    return expected_sessions(et_date_at_ms(window[0]), et_date_at_ms(window[1]) - timedelta(days=1))


def admit_run(
    metrics: Metrics | None,
    detail: Mapping[str, Any] | None,
    window: tuple[int, int],
    *,
    sessions_label: str = "development",
) -> SessionSeries | dict[str, Any]:
    """Admit one run's sessions, or the named reason no reading can be drawn from it.

    The run's own outcome is checked before its detail is read; the series checks are ``chance.session_series``'s,
    with the boundary checks this slice adds: calendar coverage of the scheduled window, and finite derived returns.
    ``sessions_label`` names the window in the two outcome missings ("development" or "final"), so a final-interval
    refusal never calls itself a development one.
    """
    if metrics is None:
        return _missing(("not_evaluated", f"The {sessions_label} run was never evaluated for this fit, so there are no sessions to resample."))
    if metrics.status != "completed":
        return _missing(("failed_run", f"The {sessions_label} run failed, so there are no sessions to resample."), detail=f"({metrics.error or 'no reason recorded'})")
    series = session_series(detail)
    if isinstance(series, str):
        if series.startswith(TOO_FEW_SESSIONS_PREFIX):
            return _missing(TOO_FEW_SESSIONS, detail=series)
        return _missing((_SERIES_CODES.get(series, "unreadable_series"), series))
    scheduled = _window_sessions(window)
    covered = [et_date_at_ms(ms) for ms in series.session_ms]
    if set(scheduled) - set(covered):
        missing = sorted(set(scheduled) - set(covered))
        span = f"{len(missing)} scheduled session{'s' if len(missing) != 1 else ''} missing ({missing[0]}…{missing[-1]})."
        return _missing(MISSING_SCHEDULED_SESSIONS, detail=span)
    if set(covered) - set(scheduled):
        return _missing(UNSCHEDULED_SESSION)
    if any(ms != session_close_ms_utc(day) for ms, day in zip(series.session_ms, covered, strict=True)):
        return _missing(NON_CANONICAL_CLOSE)
    if not np.isfinite(series.returns).all():
        return _missing(DERIVED_RETURN_OVERFLOW)
    return series


# ── The paired draw ───────────────────────────────────────────────────────


def _pair_chunk(own: np.ndarray, other: np.ndarray) -> tuple[dict[str, int], np.ndarray]:
    """One chunk's comparison counts, and its per-replication differences (NaN where the pair or the subtraction is undefined).

    NaN loses every comparison, so an undefined path counts as undefined and never as beating, tying with or
    losing to anything. A finite pair whose subtraction overflows keeps its comparison but not a difference value.
    """
    defined = np.isfinite(own) & np.isfinite(other)
    counts = {
        "better": int(np.count_nonzero(own[defined] > other[defined])),
        "tied": int(np.count_nonzero(own[defined] == other[defined])),
        "worse": int(np.count_nonzero(own[defined] < other[defined])),
        "undefined": int(len(own) - np.count_nonzero(defined)),
    }
    with np.errstate(over="ignore", invalid="ignore"):
        difference = own - other
    return counts, np.where(defined & np.isfinite(difference), difference, np.nan)


def _profit_chunk(net: np.ndarray) -> dict[str, int]:
    """One chunk's profitability counts over compounded net returns; undefined is NaN, never money made or lost."""
    finite = np.isfinite(net)
    return {
        "positive": int(np.count_nonzero(net[finite] > 0.0)),
        "zero": int(np.count_nonzero(net[finite] == 0.0)),
        "negative": int(np.count_nonzero(net[finite] < 0.0)),
        "undefined": int(len(net) - np.count_nonzero(finite)),
    }


def _draw(
    candidate: SessionSeries,
    incumbent: SessionSeries,
    *,
    objective: RankingMeasure,
    seed: int,
    cancel_check: _Cancel,
) -> dict[str, Any]:
    """B paired draws of the source window's own session count, one chunk held at a time.

    The per-replication scalars kept for quantiles are the only O(B) storage; every index chunk is released
    before the next is drawn.
    """
    n = len(candidate.session_ms)
    objectives = np.full(REPLICATIONS, np.nan)
    differences = np.full(REPLICATIONS, np.nan)
    counts = {"better": 0, "tied": 0, "worse": 0, "undefined": 0}
    rng = np.random.default_rng(seed)
    done = 0
    for chunk in stationary_index_chunks(n, size=n, mean_block=MEAN_BLOCK_SESSIONS, replications=REPLICATIONS, rng=rng):
        cancel_check()
        own = path_objective(candidate.returns[chunk], objective, candidate.capital)
        other = path_objective(incumbent.returns[chunk], objective, incumbent.capital)
        chunk_counts, chunk_differences = _pair_chunk(own, other)
        for name, value in chunk_counts.items():
            counts[name] += value
        objectives[done : done + len(own)] = own
        differences[done : done + len(own)] = chunk_differences
        done += len(own)
    return {"counts": counts, "objectives": objectives, "differences": differences}


def _range(values: np.ndarray) -> dict[str, Any]:
    """The 5th/50th/95th percentiles by linear interpolation over the finite values, with how many there are."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {"p5": None, "p50": None, "p95": None, "finite": 0}
    p5, p50, p95 = np.quantile(finite, (0.05, 0.5, 0.95), method="linear")
    return {"p5": float(p5), "p50": float(p50), "p95": float(p95), "finite": int(finite.size)}


def _shares(counts: Mapping[str, int]) -> dict[str, float]:
    return {name: count / REPLICATIONS for name, count in counts.items()}


def _classify(counts: Mapping[str, int], clear: float) -> tuple[str, bool]:
    """The comparison's label and whether undefined paths leave the support incomplete.

    Classification reads raw counts against the stored threshold, never a rounded share; an adverse label
    needs actual finite worse outcomes, and a tie is never called an improvement.
    """
    if counts["better"] + counts["tied"] + counts["worse"] == 0:
        return "no_defined_paths", counts["undefined"] > 0
    if counts["tied"] == REPLICATIONS:
        return "identical_outcomes", False
    if _at_least(counts["worse"], REPLICATIONS, clear):
        return "clearly_worse", counts["undefined"] > 0
    if _at_least(counts["better"] + counts["tied"], REPLICATIONS, clear):
        return "clearly_as_good", counts["undefined"] > 0
    return "mixed", counts["undefined"] > 0


def _comparison_sentence(label: str, counts: Mapping[str, int], objective_label: str, sessions_label: str) -> str:
    shares = _shares(counts)
    as_good = counts["better"] + counts["tied"]
    prefix = f"In {REPLICATIONS:,} paired resamples of these {sessions_label} sessions, this fit's {objective_label} was"
    incomplete = (
        f" {counts['undefined']:,} path{'s' if counts['undefined'] != 1 else ''} left the comparison undefined, so this support is incomplete."
    )
    if label == "no_defined_paths":
        return f"No paired resample produced a defined {objective_label} for both settings, so no comparison is available from this model."
    if label == "identical_outcomes":
        return f"Every one of the {REPLICATIONS:,} paired resamples tied: these settings and the current settings produced the same {objective_label} on every path. A tie is not an improvement."
    if label == "clearly_worse":
        return (
            f"{prefix} strictly worse than the current settings' in {shares['worse']:.1%} of paths "
            f"({counts['better']:,} better, {counts['tied']:,} tied, {counts['worse']:,} worse). "
            f"This reads as clearly behind under the resampling model, not as proof of one."
            + (incomplete if counts["undefined"] else "")
        )
    if label == "clearly_as_good":
        return (
            f"{prefix} at least as good as the current settings' in {as_good / REPLICATIONS:.1%} of paths "
            f"({counts['better']:,} strictly better, {counts['tied']:,} tied, {counts['worse']:,} worse). "
            f"That is support under this resampling model only, not a probability of a true edge."
            + (incomplete if counts["undefined"] else "")
        )
    return (
        f"{prefix} at least as good as the current settings' in {as_good / REPLICATIONS:.1%} of paths and strictly worse in "
        f"{shares['worse']:.1%} ({counts['better']:,} better, {counts['tied']:,} tied, {counts['worse']:,} worse). "
        f"The resampling model does not clearly separate the two settings."
        + (incomplete if counts["undefined"] else "")
    )


def _basis(
    schema: str,
    *,
    objective: RankingMeasure,
    window: tuple[int, int],
    session_count: int,
    seed_inputs: list[Any],
    seed_value: int,
    source: dict[str, Any],
) -> dict[str, Any]:
    label, units = OBJECTIVES[objective]
    return {
        "schema_version": schema,
        "convention": chance.CONVENTION,
        "replications": REPLICATIONS,
        "chunk_replications": CHUNK_REPLICATIONS,
        "mean_block_sessions": MEAN_BLOCK_SESSIONS,
        "thresholds": {"clear": CLEAR},
        "objective": {"measure": objective, "label": label, "units": units},
        "seed": {"inputs": seed_inputs, "value": seed_value},
        "source": {"window": {"start_ms": window[0], "end_ms": window[1]}, "session_count": session_count, **source},
    }


def _comparison_reading(
    candidate: SessionSeries,
    incumbent: SessionSeries,
    *,
    objective: RankingMeasure,
    window: tuple[int, int],
    candidate_hash: str,
    incumbent_hash: str,
    sessions_label: str,
    bias_note: str,
    cancel_check: _Cancel,
) -> dict[str, Any]:
    label = OBJECTIVES[objective][0]
    # The pair's hashes in one canonical order: either direction of the same comparison draws the same indices.
    seed_inputs = [SCHEMA_COMPARISON, window[0], window[1], objective, *sorted((candidate_hash, incumbent_hash))]
    seed = seed_for(*seed_inputs)
    drawn = _draw(candidate, objective=objective, seed=seed, cancel_check=cancel_check, incumbent=incumbent)
    counts = drawn["counts"]
    classification, incomplete = _classify(counts, CLEAR)
    return {
        "status": "measured",
        "label": classification,
        "sentence": _comparison_sentence(classification, counts, label, sessions_label),
        "incomplete_support": incomplete,
        "bias_note": bias_note,
        "counts": dict(counts),
        "shares": _shares(counts),
        "at_least_as_good": {"count": counts["better"] + counts["tied"], "share": (counts["better"] + counts["tied"]) / REPLICATIONS},
        "candidate_range": _range(drawn["objectives"]),
        "difference_range": _range(drawn["differences"]),
        "basis": _basis(
            SCHEMA_COMPARISON,
            objective=objective,
            window=window,
            session_count=len(candidate.session_ms),
            seed_inputs=seed_inputs,
            seed_value=seed,
            source={
                "candidate_point_hash": candidate_hash,
                "incumbent_point_hash": incumbent_hash,
                "series_digest": {"candidate": series_digest(candidate), "incumbent": series_digest(incumbent)},
                "capital": candidate.capital,
            },
        ),
    }


def _profitability_sentence(counts: Mapping[str, int], sessions_label: str) -> str:
    shares = _shares(counts)
    if counts["positive"] + counts["zero"] + counts["negative"] == 0:
        return "No resample produced a defined compounded return, so no profitability reading is available from this model."
    direction = (
        f"positive in {shares['positive']:.1%} of {REPLICATIONS:,} resampled {sessions_label} paths "
        f"({counts['zero']:,} exactly flat, {counts['negative']:,} negative)"
        if counts["positive"] >= counts["negative"]
        else f"negative in {shares['negative']:.1%} of {REPLICATIONS:,} resampled {sessions_label} paths "
        f"({counts['positive']:,} positive, {counts['zero']:,} exactly flat)"
    )
    incomplete = f" {counts['undefined']:,} path{'s' if counts['undefined'] != 1 else ''} left the return undefined." if counts["undefined"] else ""
    return f"The recorded {sessions_label} outcome resamples to a compounded net return {direction}.{incomplete} This is the model's spread of what was recorded, not a forecast."


def _profitability_reading(
    candidate: SessionSeries,
    *,
    objective: RankingMeasure,
    window: tuple[int, int],
    candidate_hash: str,
    sessions_label: str,
    cancel_check: _Cancel,
) -> dict[str, Any]:
    seed_inputs = [SCHEMA_PROFITABILITY, window[0], window[1], objective, candidate_hash]
    seed = seed_for(*seed_inputs)
    counts = {"positive": 0, "zero": 0, "negative": 0, "undefined": 0}
    n = len(candidate.session_ms)
    # The per-replication compounded net returns are the O(B) scalars the range's percentiles need (#3026);
    # nothing else about a path is kept, and every index chunk is released before the next is drawn.
    net_returns = np.full(REPLICATIONS, np.nan)
    rng = np.random.default_rng(seed)
    done = 0
    for chunk in stationary_index_chunks(n, size=n, mean_block=MEAN_BLOCK_SESSIONS, replications=REPLICATIONS, rng=rng):
        cancel_check()
        net = path_net_return(candidate.returns[chunk])
        net_returns[done : done + len(net)] = net
        for name, value in _profit_chunk(net).items():
            counts[name] += value
        done += len(net)
    if counts["positive"] + counts["zero"] + counts["negative"] == 0:
        label = "no_defined_paths"
    elif _at_least(counts["positive"], REPLICATIONS, CLEAR):
        label = "clearly_positive"
    elif _at_least(counts["negative"], REPLICATIONS, CLEAR):
        label = "clearly_negative"
    else:
        label = "mixed"
    return {
        "status": "measured",
        "label": label,
        "sentence": _profitability_sentence(counts, sessions_label),
        "counts": dict(counts),
        "shares": _shares(counts),
        # The independent compounded-net range, in net-return units — never the objective's units: a positive
        # Sharpe can compound to a loss, so the objective's zero is a tie there and break-even only here (#3026).
        "net_return_range": {
            "quantity": {"measure": "compounded_net_return", "label": "compounded net return", "units": "fraction of the starting capital"},
            **_range(net_returns),
        },
        "basis": _basis(
            SCHEMA_PROFITABILITY,
            objective=objective,
            window=window,
            session_count=n,
            seed_inputs=seed_inputs,
            seed_value=seed,
            source={
                "candidate_point_hash": candidate_hash,
                "series_digest": {"candidate": series_digest(candidate)},
                "capital": candidate.capital,
            },
        ),
    }


def _failed(exc: Exception, component: str) -> dict[str, Any]:
    """A diagnostic failure record: the operator sentence is Python's, so the browser only displays it (#3011, #3012)."""
    logger.warning(
        "golden search chance reading failed",
        extra={"action": "golden_search_chance_reading_failed", "component": component},
        exc_info=True,
    )
    reason = f"{type(exc).__name__}: {exc}"
    return {
        "status": "failed",
        "reason": reason,
        "sentence": f"The {component.replace('_', ' ')} reading could not be computed and nothing was stored: {reason}.",
    }


def failed_component(exc: Exception, component: str) -> dict[str, Any]:
    """A diagnostic failure record for one named component, for callers outside this module (#3012)."""
    return _failed(exc, component)


def failed_readings(exc: Exception) -> dict[str, dict[str, Any]]:
    """Both components as diagnostic failures: the caller records them and the stage's frozen evidence stands."""
    return {"comparison": _failed(exc, "comparison"), "profitability": _failed(exc, "profitability")}


def development_readings(
    *,
    objective: RankingMeasure,
    window: tuple[int, int],
    candidate_metrics: Metrics | None,
    candidate_detail: Mapping[str, Any] | None,
    candidate_hash: str,
    incumbent_metrics: Metrics | None,
    incumbent_detail: Mapping[str, Any] | None,
    incumbent_hash: str,
    sessions_label: str = "development",
    bias_note: str = BIAS_NOTE,
    cancel_check: _Cancel = lambda: None,
) -> dict[str, dict[str, Any]]:
    """One window's chance readings for a fit — the comparison component and the independent profitability component.

    The window's own name rides through ``sessions_label`` so every sentence names the sessions it actually
    resampled, and ``bias_note`` is the caveat those sentences deserve: the development-stage default says the fit
    was selected on these same sessions, while the exam passes the two-selection note its final-interval readings
    require. Known missing inputs come back as typed missing reasons per component; an unexpected error is logged and
    comes back as a diagnostic failure for that component, never as a number and never silently.
    """
    readings: dict[str, dict[str, Any]] = {}
    try:
        admitted = admit_run(candidate_metrics, candidate_detail, window, sessions_label=sessions_label)
    except JobCancelled:
        raise  # cancellation stops the stage; it is never recorded as a reading
    except Exception as exc:  # a diagnostic failure, not a refusal: the stage records it and continues
        return {"comparison": _failed(exc, "comparison"), "profitability": _failed(exc, "profitability")}
    if not isinstance(admitted, SessionSeries):
        return {"comparison": dict(admitted), "profitability": dict(admitted)}
    try:
        readings["profitability"] = _profitability_reading(admitted, objective=objective, window=window, candidate_hash=candidate_hash, sessions_label=sessions_label, cancel_check=cancel_check)
    except JobCancelled:
        raise  # cancellation stops the stage; it is never recorded as a reading
    except Exception as exc:
        readings["profitability"] = _failed(exc, "profitability")
    if candidate_hash == incumbent_hash:
        readings["comparison"] = {
            "status": "same_settings",
            "sentence": "These are the current settings themselves; there is nothing to compare them with.",
            "basis": _basis(
                SCHEMA_COMPARISON,
                objective=objective,
                window=window,
                session_count=len(admitted.session_ms),
                seed_inputs=[],
                seed_value=0,
                source={"candidate_point_hash": candidate_hash, "incumbent_point_hash": incumbent_hash, "series_digest": {"candidate": series_digest(admitted)}, "capital": admitted.capital},
            ),
        }
        return readings
    try:
        incumbent = admit_run(incumbent_metrics, incumbent_detail, window, sessions_label=sessions_label)
    except JobCancelled:
        raise  # cancellation stops the stage; it is never recorded as a reading
    except Exception as exc:  # an unexpected incumbent-side error is a diagnostic failure of the comparison, not a missing reading
        readings["comparison"] = _failed(exc, "comparison")
        return readings
    if not isinstance(incumbent, SessionSeries):
        readings["comparison"] = _missing(
            INCUMBENT_UNAVAILABLE, detail=f"Recorded reason: {incumbent.get('reason', 'the run could not be read')}"
        )
        return readings
    if admitted.session_ms != incumbent.session_ms:
        readings["comparison"] = _missing(UNPAIRED_SESSIONS)
        return readings
    if admitted.capital != incumbent.capital:
        readings["comparison"] = _missing(INCONSISTENT_CAPITAL)
        return readings
    try:
        readings["comparison"] = _comparison_reading(
            admitted,
            incumbent,
            objective=objective,
            window=window,
            candidate_hash=candidate_hash,
            incumbent_hash=incumbent_hash,
            sessions_label=sessions_label,
            bias_note=bias_note,
            cancel_check=cancel_check,
        )
    except JobCancelled:
        raise  # cancellation stops the stage; it is never recorded as a reading
    except Exception as exc:
        readings["comparison"] = _failed(exc, "comparison")
    return readings


def incumbent_readings(*, objective: RankingMeasure, window: tuple[int, int]) -> dict[str, dict[str, Any]]:
    """The baseline's own record: the current settings are what every fit is compared against, never with themselves."""
    return {
        "comparison": {
            "status": "baseline",
            "sentence": "The current settings are the baseline every fit is compared against. They are not compared with themselves.",
            "basis": _basis(
                SCHEMA_COMPARISON,
                objective=objective,
                window=window,
                session_count=0,
                seed_inputs=[],
                seed_value=0,
                source={},
            ),
        },
        "profitability": _missing(
            ("not_measured", "The current settings' own profitability is not measured; every fit's readings stand against them.")
        ),
        # The baseline has no ladder of its own: a rehearsal compares a fit against it (#3012).
        "rehearsal": _missing(
            ("baseline", "The current settings are the baseline; a rehearsal compares a fit against them, and there is nothing to rehearse.")
        ),
    }


# ── The stored shape a read model serves ──────────────────────────────────

#: What each component's never-recorded state says, so the copy names the component it belongs to.
_NOT_MEASURED_BY_COMPONENT: dict[str, tuple[str, str]] = {
    "comparison": NOT_MEASURED,
    "profitability": (
        "not_measured",
        "Not measured for this study: profitability readings were first recorded with #3011, and stored evidence is never backfilled.",
    ),
}
#: The statuses a stored chance component may carry; anything else reads as unsupported rather than guessed at (#3011).
KNOWN_STATUSES = frozenset({"measured", "same_settings", "baseline", "missing", "failed"})

#: The served contract of each development component, so a malformed stored record never reaches the study page.
_SERVED = {
    "comparison": TypeAdapter(GoldenSearchChanceComparison),
    "profitability": TypeAdapter(GoldenSearchChanceProfitability),
    "rehearsal": TypeAdapter(GoldenSearchChanceRehearsal),
    "drawdown": TypeAdapter(GoldenSearchChanceDrawdown),
}


def served_record(component: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """One stored development component as the study page is served it, or a named unavailable when it cannot be.

    A record that fails the wire contract the response is validated under would fail the whole response, so it
    reads ``missing`` with an ``invalid_record`` code instead — logged, never repaired and never written back.
    """
    try:
        _SERVED[component].validate_python(record)
    except ValidationError as exc:
        first = exc.errors()[0]["loc"] if exc.errors() else ()
        logger.warning(
            "golden search stored chance record fails the served contract; serving it as unavailable",
            extra={"action": "golden_search_stored_chance_invalid", "component": component, "location": ".".join(str(part) for part in first)},
        )
        return _missing(("invalid_record", f"The stored {component} reading fails validation and is shown as unavailable rather than guessed at."))
    return dict(record)


def stored_chance(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The chance components of one stored evidence candidate, each normalized independently.

    A component the study never recorded (evidence from before the slice that measures it) reads
    ``missing`` with its reason; a record this server cannot interpret says so. Nothing is recomputed,
    backfilled or reclassified with today's constants.
    """
    stored = candidate.get("chance")
    components: dict[str, dict[str, Any]] = {}
    for component in ("comparison", "profitability"):
        record = (stored or {}).get(component)
        if isinstance(record, Mapping) and record.get("status") in KNOWN_STATUSES:
            components[component] = served_record(component, record)
        elif isinstance(record, Mapping):
            unsupported = record.get("status")
            components[component] = _missing(
                ("unsupported", f"The stored {component} reading holds status {unsupported!r}, which this server cannot interpret; it is shown as unavailable rather than guessed at.")
            )
        else:
            components[component] = _missing(_NOT_MEASURED_BY_COMPONENT[component])
    return components
