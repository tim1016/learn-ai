"""Final-test rehearsals: what share of resampled development sessions would meet every modeled final rule (#3012, ADR 0074 decision 12).

Formula: for rung k = 1..4, B paired stationary-bootstrap draws of k × M
  sessions (M the frozen final interval's scheduled session count), each
  path judged on the modeled rules: net return finite and positive when the
  plan requires it; session-close worst fall finite and within the frozen
  ceiling; resampled completed exits at or above the rung's trade floor;
  both sides' objectives finite with the candidate's at least the
  incumbent's. The rung's joint support is the share of paths meeting every
  active rule — the intersection, never a product of the marginal shares —
  over all B paths. The 1× rung also persists its objective's 1st–99th
  percentiles and the exact seed inputs that drew it, so the final-test
  slice can reproduce the full 1× rehearsal without rerunning the engine.
Reference: issue #3012's delivery contract (the 2026-10-09 adversarial
  review of #2932); the draw is Politis & Romano (1994) via
  ``monte_carlo/stationary.py``; the rules mirror ``exam_rules.judge_exam``
  at session closes and the activity policy of ADR 0074 decision 9.
Canonical implementation: this file for the rehearsal counting and its
  basis; ``chance.py`` for one path's statistics; ``chance_compare.py`` for
  the source admission this slice consumes; ``activity.py`` for trade
  floors; ``monte_carlo/stationary.py`` for the index draw.
Validated against: tests/research/golden_search/test_chance_rehearsal.py
  (exact 1× and frequency floors, joint predicates with correlated
  failures, nonmonotone ladders, per-bar breaches invisible at closes,
  positivity switched off, undefined objectives).

This is a conditional rehearsal of the recorded development outcome. It is
not an exact replay of the final test (whose gate reads the engine's
per-bar drawdown), not a statistical-power calculation, and not a
guaranteed minimum test length: a longer rung need not improve support,
and the rehearsal never claims one. It reads development evidence only;
no final-period detail, final return or final outcome is touched before
the test opens.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from fractions import Fraction
from math import ceil
from typing import Any

import numpy as np

from app.jobs.progress import JobCancelled
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.golden_search.activity import TradeFloors
from app.research.golden_search.chance import (
    CONVENTION,
    MEAN_BLOCK_SESSIONS,
    REHEARSAL_TARGET,
    REPLICATIONS,
    SessionSeries,
    path_net_return,
    path_objective,
    path_worst_fall,
    seed_for,
)
from app.research.golden_search.chance_compare import (
    INCONSISTENT_CAPITAL,
    INCUMBENT_UNAVAILABLE,
    OBJECTIVES,
    UNPAIRED_SESSIONS,
    _missing,
    admit_run,
    failed_component,
    series_digest,
)
from app.research.golden_search.selection import Metrics
from app.research.grid_search.service import window_dates
from app.research.monte_carlo.stationary import CHUNK_REPLICATIONS, stationary_index_chunks
from app.research.sweep.ranking import RankingMeasure

logger = logging.getLogger(__name__)

#: What a stored rehearsal calls itself; a change to the reading's shape or interpretation needs a new one.
SCHEMA_REHEARSAL = "golden-search-chance-rehearsal/v1"

#: The rungs, as multiples of the frozen final interval's scheduled sessions.
RUNGS: tuple[int, ...] = (1, 2, 3, 4)

NOT_MEASURED = (
    "not_measured",
    "Not measured for this study: final-test rehearsals were first recorded with #3012, and stored evidence is never backfilled.",
)

_Cancel = Callable[[], object]


def final_session_count(final_window: tuple[int, int]) -> int:
    """The frozen final interval's scheduled session count, from the calendar the plan locked its dates under."""
    return len(expected_sessions(*window_dates(*final_window)))


def rung_floor(floors: TradeFloors, final_window: tuple[int, int], k: int, *, rate: int | None, final_years: Fraction | None) -> int:
    """The trade floor rung ``k`` rehearses against.

    At 1× this is the exact final floor the receipt froze. A frequency plan's longer rung is
    ``ceil(rate × k × final trading years)`` — computed on the frozen year fractions and rounded once, never the
    already-rounded 1× floor multiplied. A fixed-floor legacy plan uses its fixed final floor on every rung.
    """
    if (rate is None and final_years is not None) or (rate is not None and final_years is None):
        raise ValueError("a frequency rehearsal needs both its rate and its frozen final trading years")
    if k == 1 or rate is None:
        return floors.at(final_window, final=True)
    return ceil(rate * k * final_years)


def _percentiles(values: np.ndarray) -> list[float]:
    """The 1st–99th percentiles by linear interpolation over the finite values, as the carry-over curve later plots them."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return []
    return [float(value) for value in np.quantile(finite, np.linspace(0.01, 0.99, 99), method="linear")]


def _rehearsal_chunk(
    candidate_returns: np.ndarray,
    incumbent_returns: np.ndarray,
    candidate_exits: np.ndarray,
    *,
    objective: RankingMeasure,
    capital: float,
    ceiling: float,
    require_positive_net: bool,
    floor: int,
) -> tuple[dict[str, int], dict[str, int]]:
    """One chunk's rule and joint pass counts, and its undefined counts; every denominator is the chunk's own path count."""
    net = path_net_return(candidate_returns)
    fall = path_worst_fall(candidate_returns)
    exits = candidate_exits.sum(axis=1)
    own = path_objective(candidate_returns, objective, capital)
    other = path_objective(incumbent_returns, objective, capital)
    rules: dict[str, np.ndarray] = {}
    undefined: dict[str, np.ndarray] = {}
    if require_positive_net:
        finite_net = np.isfinite(net)
        rules["positive_net"] = finite_net & (net > 0.0)
        undefined["positive_net"] = ~finite_net
    finite_fall = np.isfinite(fall)
    rules["drawdown"] = finite_fall & (fall <= ceiling)
    undefined["drawdown"] = ~finite_fall
    rules["exits"] = exits >= floor
    both_finite = np.isfinite(own) & np.isfinite(other)
    rules["objective"] = both_finite & (own >= other)
    undefined["objective"] = ~both_finite
    joint = np.logical_and.reduce(list(rules.values()))
    passes = {f"pass_{name}": int(np.count_nonzero(mask)) for name, mask in rules.items()}
    passes["pass_joint"] = int(np.count_nonzero(joint))
    undefined_counts = {f"undefined_{name}": int(np.count_nonzero(mask)) for name, mask in undefined.items()}
    return passes, undefined_counts, own


def _guidance(rungs: list[dict[str, Any]], target: float, active: tuple[str, ...]) -> str:
    """What the ladder says, in the owner's words: the limiting rules first, then whether any rung reaches the target.

    The words never claim the test is too short from a low joint share alone — a weak objective, a deep drawdown or low
    activity can each be the limit — and a longer rung is reported honestly even when support falls.
    """
    first = rungs[0]
    shares = {name: first["shares"][name] for name in ("joint", *active) if name in first["shares"]}
    limiting = sorted((share, name) for name, share in shares.items() if name != "joint")
    worst = limiting[:2]
    limit_words = {
        "positive_net": "the positive-net rule",
        "drawdown": "the session-close drawdown rule",
        "exits": "the trade-count rule",
        "objective": "the objective comparison with the current settings",
    }.__getitem__
    worst_names = " and ".join(f"{limit_words(name)} ({share:.0%})" for share, name in worst) if worst else "no single rule"
    reaching = [rung for rung in rungs if Fraction(rung["passes"]["pass_joint"], rung["denominator"]) >= Fraction(str(target))]
    if reaching:
        best = reaching[0]
        reach_words = f"The {best['k']}× rung reaches the {target:.0%} target ({best['shares']['joint']:.0%})."
    else:
        top = max(rungs, key=lambda rung: rung["passes"]["pass_joint"])
        reach_words = f"No rung reaches the {target:.0%} target; the highest is {top['shares']['joint']:.0%} at {top['k']}×."
    trend = (
        "Support does not rise with length on this ladder — a longer rehearsal of the same development outcome is not a better one."
        if rungs[-1]["passes"]["pass_joint"] < rungs[0]["passes"]["pass_joint"]
        else ""
    )
    return (
        f"At the final test's own length, {first['shares']['joint']:.0%} of resamples meet every modeled rule; the limit is {worst_names}. "
        f"{reach_words}{' ' + trend if trend else ''} The {target:.0%} target is the owner's reading heuristic, not a power calculation."
    )


def rehearsal_reading(
    *,
    objective: RankingMeasure,
    window: tuple[int, int],
    final_window: tuple[int, int],
    floors: TradeFloors,
    rate: int | None,
    final_years: Fraction | None,
    ceiling: float,
    require_positive_net: bool,
    candidate_metrics: Metrics | None,
    candidate_detail: Mapping[str, Any] | None,
    candidate_hash: str,
    incumbent_metrics: Metrics | None,
    incumbent_detail: Mapping[str, Any] | None,
    incumbent_hash: str,
    cancel_check: _Cancel = lambda: None,
) -> dict[str, Any]:
    """One fit's rehearsal ladder, or the typed reason none could be drawn.

    Known missing inputs come back as typed missing reasons; an unexpected error is logged and comes back as a
    diagnostic failure; cancellation between chunks propagates and is never recorded as a reading.
    """
    if candidate_hash == incumbent_hash:
        return _missing(
            ("same_settings", "These are the current settings themselves; a rehearsal compares a fit against them, and there is nothing to rehearse.")
        )
    try:
        candidate = admit_run(candidate_metrics, candidate_detail, window)
    except JobCancelled:
        raise
    except Exception as exc:
        return failed_rehearsal(exc)
    if not isinstance(candidate, SessionSeries):
        return dict(candidate)
    try:
        incumbent = admit_run(incumbent_metrics, incumbent_detail, window)
    except JobCancelled:
        raise
    except Exception as exc:
        return failed_rehearsal(exc)
    if not isinstance(incumbent, SessionSeries):
        return _missing(INCUMBENT_UNAVAILABLE, detail=f"Recorded reason: {incumbent.get('reason', 'the run could not be read')}")
    if candidate.session_ms != incumbent.session_ms:
        return _missing(UNPAIRED_SESSIONS)
    if candidate.capital != incumbent.capital:
        return _missing(INCONSISTENT_CAPITAL)
    try:
        return _draw_ladder(
            candidate,
            incumbent,
            objective=objective,
            window=window,
            final_window=final_window,
            floors=floors,
            rate=rate,
            final_years=final_years,
            ceiling=ceiling,
            require_positive_net=require_positive_net,
            candidate_hash=candidate_hash,
            incumbent_hash=incumbent_hash,
            cancel_check=cancel_check,
        )
    except JobCancelled:
        raise
    except Exception as exc:
        return failed_rehearsal(exc)


def failed_rehearsal(exc: Exception) -> dict[str, Any]:
    """The rehearsal component as a diagnostic failure: the stage records it and the comparison beside it stands."""
    return failed_component(exc, "rehearsal")


def _draw_ladder(
    candidate: SessionSeries,
    incumbent: SessionSeries,
    *,
    objective: RankingMeasure,
    window: tuple[int, int],
    final_window: tuple[int, int],
    floors: TradeFloors,
    rate: int | None,
    final_years: Fraction | None,
    ceiling: float,
    require_positive_net: bool,
    candidate_hash: str,
    incumbent_hash: str,
    cancel_check: _Cancel,
) -> dict[str, Any]:
    m = final_session_count(final_window)
    n = len(candidate.session_ms)
    rungs: list[dict[str, Any]] = []
    seeds: dict[str, Any] = {}
    percentile_curve: list[float] | None = None
    percentile_finite = 0
    for k in RUNGS:
        seed_inputs = [SCHEMA_REHEARSAL, window[0], window[1], objective, *sorted((candidate_hash, incumbent_hash)), k]
        seed = seed_for(*seed_inputs)
        floor = rung_floor(floors, final_window, k, rate=rate, final_years=final_years)
        passes: dict[str, int] = {}
        undefined_counts: dict[str, int] = {}
        denominator = 0
        objectives = np.full(REPLICATIONS, np.nan) if k == 1 else None
        done = 0
        rng = np.random.default_rng(seed)
        for chunk in stationary_index_chunks(n, size=k * m, mean_block=MEAN_BLOCK_SESSIONS, replications=REPLICATIONS, rng=rng):
            cancel_check()
            chunk_passes, chunk_undefined, chunk_objectives = _rehearsal_chunk(
                candidate.returns[chunk],
                incumbent.returns[chunk],
                candidate.exits[chunk],
                objective=objective,
                capital=candidate.capital,
                ceiling=ceiling,
                require_positive_net=require_positive_net,
                floor=floor,
            )
            for name, value in chunk_passes.items():
                passes[name] = passes.get(name, 0) + value
            for name, value in chunk_undefined.items():
                undefined_counts[name] = undefined_counts.get(name, 0) + value
            denominator += len(chunk)
            if objectives is not None:
                objectives[done : done + len(chunk)] = chunk_objectives
            done += len(chunk)
        rung = {
            "k": k,
            "sessions": k * m,
            "trade_floor": floor,
            "denominator": denominator,
            "passes": passes,
            "undefined": undefined_counts,
            "shares": {name.split("_", 1)[1]: value / denominator for name, value in passes.items() if name.startswith("pass_")},
        }
        rungs.append(rung)
        seeds[f"{k}x"] = {"inputs": seed_inputs, "value": seed}
        if objectives is not None:
            percentile_curve = _percentiles(objectives)
            percentile_finite = int(np.count_nonzero(np.isfinite(objectives)))
    active = tuple(name for name in ("positive_net", "drawdown", "exits", "objective") if f"pass_{name}" in rungs[0]["passes"])
    years = final_years
    return {
        "status": "measured",
        "rungs": rungs,
        "active_rules": list(active),
        "guidance": _guidance(rungs, REHEARSAL_TARGET, active),
        "objective_percentiles_1x": {"p": percentile_curve or [], "finite": percentile_finite},
        "basis": {
            "schema_version": SCHEMA_REHEARSAL,
            "convention": CONVENTION,
            "replications": REPLICATIONS,
            "chunk_replications": CHUNK_REPLICATIONS,
            "mean_block_sessions": MEAN_BLOCK_SESSIONS,
            "thresholds": {"rehearsal_target": REHEARSAL_TARGET},
            "objective": {"measure": objective, "label": OBJECTIVES[objective][0], "units": OBJECTIVES[objective][1]},
            "rules": {"max_drawdown_ceiling": ceiling, "require_positive_net": require_positive_net, "drawdown_basis": "session closes"},
            "activity": {"frequency_based": rate is not None, "expected_trades_per_year": rate, "final_trading_years": None if years is None else float(years)},
            "final_interval": {"window": {"start_ms": final_window[0], "end_ms": final_window[1]}, "scheduled_sessions": m},
            "source": {
                "window": {"start_ms": window[0], "end_ms": window[1]},
                "session_count": n,
                "candidate_point_hash": candidate_hash,
                "incumbent_point_hash": incumbent_hash,
                "series_digest": {"candidate": series_digest(candidate), "incumbent": series_digest(incumbent)},
                "capital": candidate.capital,
            },
            "seeds": seeds,
        },
    }




def stored_rehearsal(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The rehearsal component of one stored evidence candidate, normalized like every other chance component."""
    record = (candidate.get("chance") or {}).get("rehearsal")
    if isinstance(record, Mapping) and record.get("status") in {"measured", "missing", "failed"}:
        return dict(record)
    if isinstance(record, Mapping):
        return _missing(
            ("unsupported", f"The stored rehearsal reading holds status {record.get('status')!r}, which this server cannot interpret; it is shown as unavailable rather than guessed at.")
        )
    return _missing(NOT_MEASURED)
