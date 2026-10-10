"""Final-test chance readings: per-fit uncertainty beside the frozen outcome, and the recorded result's rank among its own development rehearsals (#3014, ADR 0074 decision 12).

Formula: after each fit's frozen judgement, the same paired comparison and
  profitability machinery #3011 built runs over the final interval's own
  sessions — shared indices, B-denominator counts, better/tied/worse/
  undefined and the 5/50/95 ranges — beside a carry-over rank: the stored
  1× development rehearsal is reproduced exactly (its stored seed, its
  unchanged development detail input, its own objective and capital), and
  the recorded final objective is ranked as count(finite rehearsal
  objectives strictly below it) over count(finite), with the undefined
  count beside it. Two distinct completed fits additionally get one shared
  paired draw with both directional summaries derived from those same
  indices.
Reference: issue #3014's delivery contract (the 2026-10-09 adversarial
  review of #2932); the draw is Politis & Romano (1994) via
  ``monte_carlo/stationary.py``; White's Reality Check motivates the
  selection caveats, which this module states and never claims to correct.
Canonical implementation: this file for the carry-over rank and the
  fit-to-fit pairing; ``chance_compare`` for the paired counting both
  reuse; ``chance_rehearsal`` for the stored basis it reproduces.
Validated against: tests/research/golden_search/test_chance_final.py
  (ranks 0 and 1, ties at the recorded value, a discrete distribution
  where counting stored percentiles disagrees with the empirical rank,
  tails, fewer than all finite, mismatched objective/length/digest,
  stored constants different from today's, swapped fit-to-fit symmetry).

The readings inform. The frozen judgement, checks, exposure claim and
budget records never read them. The carry-over is a display diagnostic,
never the 95/80 approval-support test, and its labels are conditional on
the model: a high rank is not "as predicted" and a low rank is not proof
that bad luck is ruled out. Support is unadjusted for choosing between
fits after viewing them and for the development search; none of it is a
calibrated error rate, a probability of a true edge, or a multiplicity
correction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from fractions import Fraction
from typing import Any

import numpy as np

from app.jobs.progress import JobCancelled
from app.research.golden_search.chance import (
    CONVENTION,
    MEAN_BLOCK_SESSIONS,
    REPLICATIONS,
    SessionSeries,
    path_objective,
    seed_for,
)
from app.research.golden_search.chance_compare import (
    KNOWN_STATUSES,
    OBJECTIVES,
    _missing,
    _pair_chunk,
    admit_run,
    failed_component,
    series_digest,
)
from app.research.golden_search.selection import Metrics
from app.research.monte_carlo.stationary import stationary_index_chunks

logger = logging.getLogger(__name__)

#: What the stored per-fit final readings call themselves; a change to their interpretation needs a new one.
SCHEMA_CARRY_OVER = "golden-search-chance-carry-over/v1"
#: What the stored fit-to-fit reading calls itself.
SCHEMA_FIT_TO_FIT = "golden-search-chance-fit-to-fit/v1"

CARRY_OVER_MISSING = ("carry_over_unavailable", "No matching stored rehearsal could carry over, so no rank exists; the comparison readings beside it are unaffected.")
SELECTION_NOTE = (
    "This fit was chosen after the final test, from the fits that ran, and was selected by a search over the development data. "
    "Its support is unadjusted for both selections and is not a calibrated error rate or a probability of a true edge."
)

_Cancel = Callable[[], object]


def _rank_label(below: int, finite: int) -> str:
    """The recorded result's bucket: exact-Fraction tails, so no float division decides a boundary.

    ≤ 1/20 of defined paths strictly below reads ``below_most``; ≥ 19/20 reads ``above_most``; a finite path
    count of zero is its own state.
    """
    if finite == 0:
        return "no_defined_paths"
    rank = Fraction(below, finite)
    if rank <= Fraction(1, 20):
        return "below_most"
    if rank >= Fraction(19, 20):
        return "above_most"
    return "middle"


#: Each label's phrase in the rank sentence, keyed as the stored reading keys it.
_RANK_PHRASES = {
    "no_defined_paths": None,
    "below_most": "below most development rehearsals",
    "above_most": "above most development rehearsals",
    "middle": "inside the middle of the development rehearsals",
}


def _rank_sentence(below: int, finite: int, undefined: int, objective_label: str) -> str:
    label = _rank_label(below, finite)
    phrase = _RANK_PHRASES[label]
    if phrase is None:
        return "The reproduced rehearsal produced no defined objective on any path, so the recorded final result has no rank among it."
    return (
        f"The recorded final {objective_label} sits {phrase}: strictly above {below:,} of the {finite:,} defined rehearsal paths "
        f"({below / finite:.1%}), {undefined:,} undefined. This is conditional on the resampling model: a high rank is not the model "
        "proving itself right, and a low rank is not proof that bad luck is ruled out."
    )


def reproduce_1x_objectives(
    series: SessionSeries,
    *,
    seed: int,
    size: int,
    objective: str,
    cancel_check: _Cancel,
) -> np.ndarray:
    """The 1× rehearsal's per-path candidate objectives, redrawn exactly as the stored seed first drew them."""
    objectives = np.full(REPLICATIONS, np.nan)
    done = 0
    rng = np.random.default_rng(seed)
    for chunk in stationary_index_chunks(len(series.session_ms), size=size, mean_block=MEAN_BLOCK_SESSIONS, replications=REPLICATIONS, rng=rng):
        cancel_check()
        objectives[done : done + len(chunk)] = path_objective(series.returns[chunk], objective, series.capital)
        done += len(chunk)
    return objectives


def carry_over_reading(
    *,
    stored_rehearsal: Mapping[str, Any] | None,
    objective: str,
    development_window: tuple[int, int],
    final_window: tuple[int, int],
    development_detail: Mapping[str, Any] | None,
    development_metrics: Metrics | None,
    final_metrics: Metrics | None,
    final_detail: Mapping[str, Any] | None,
    point_hash: str,
    cancel_check: _Cancel = lambda: None,
) -> dict[str, Any]:
    """The recorded final result's rank among its own reproduced 1× development rehearsal.

    The match is exact — point hash, series digest, objective, starting capital and the 1× scheduled length — and a
    mismatch, an unknown convention, a nonfinite recorded objective or a scope mismatch is a named missing carry-over,
    never a fabricated rank. Today's constants are never borrowed for a stored basis that differs.
    """
    if stored_rehearsal is None or stored_rehearsal.get("status") != "measured":
        return _missing(CARRY_OVER_MISSING, detail=None if stored_rehearsal is None else f"Recorded state: {stored_rehearsal.get('status', 'unreadable')}.")
    basis = stored_rehearsal.get("basis") or {}
    source = basis.get("source") or {}
    stored_objective = (basis.get("objective") or {}).get("measure")
    if stored_objective != objective:
        return _missing(CARRY_OVER_MISSING, detail=f"The stored rehearsal was drawn for the {stored_objective} objective, not this plan's {objective}.")
    if source.get("candidate_point_hash") != point_hash:
        return _missing(CARRY_OVER_MISSING, detail="The stored rehearsal belongs to different settings.")
    scheduled = (basis.get("final_interval") or {}).get("scheduled_sessions")
    if not isinstance(scheduled, int) or scheduled < 1:
        return _missing(CARRY_OVER_MISSING, detail="The stored rehearsal names no valid 1× scheduled length.")
    seed_entry = (basis.get("seeds") or {}).get("1x")
    if not isinstance(seed_entry, Mapping) or "value" not in seed_entry:
        return _missing(CARRY_OVER_MISSING, detail="The stored rehearsal kept no 1× seed to reproduce.")
    # The reproduction must repeat the stored draw, so the stored basis's own constants are checked, never
    # assumed: a rehearsal drawn under different resampling constants would reproduce different paths from
    # the same seed, and that fabricated rank is exactly what this reading refuses to produce.
    stored_constants = (basis.get("convention"), basis.get("mean_block_sessions"), basis.get("replications"))
    if stored_constants != (CONVENTION, MEAN_BLOCK_SESSIONS, REPLICATIONS):
        return _missing(
            CARRY_OVER_MISSING,
            detail=f"The stored rehearsal was drawn under different resampling constants (convention {stored_constants[0]!r}, "
            f"mean block {stored_constants[1]!r}, {stored_constants[2]!r} replications), so today's draw would not repeat its paths.",
        )
    if final_metrics is None or final_metrics.status != "completed":
        return _missing(CARRY_OVER_MISSING, detail="The final run has no completed result to rank.")
    recorded = _recorded_objective(final_metrics, objective)
    if recorded is None or not np.isfinite(recorded):
        return _missing(CARRY_OVER_MISSING, detail="The final result's objective is not a finite number, so it has no rank.")
    try:
        development = admit_run(development_metrics, development_detail, development_window)
    except JobCancelled:
        raise
    except Exception as exc:
        return failed_component(exc, "carry-over")
    if not isinstance(development, SessionSeries):
        return _missing(CARRY_OVER_MISSING, detail="The unchanged development input behind the stored rehearsal could not be read.")
    if source.get("session_count") != len(development.session_ms) or source.get("series_digest", {}).get("candidate") != series_digest(development):
        return _missing(CARRY_OVER_MISSING, detail="The unchanged development input no longer matches the rehearsal's stored digest.")
    if source.get("capital") != development.capital:
        return _missing(CARRY_OVER_MISSING, detail="The development input's starting capital no longer matches the rehearsal's stored scope.")
    try:
        final_series = admit_run(final_metrics, final_detail, final_window, sessions_label="final")
    except JobCancelled:
        raise
    except Exception as exc:
        return failed_component(exc, "carry-over")
    if not isinstance(final_series, SessionSeries):
        return _missing(CARRY_OVER_MISSING, detail=f"The final run could not be read as a session series: {final_series.get('reason', 'unreadable')}")
    if len(final_series.session_ms) < scheduled:
        return _missing(CARRY_OVER_MISSING, detail=f"The final run covers fewer sessions than the rehearsal's {scheduled}-session 1× length.")
    try:
        objectives = reproduce_1x_objectives(development, seed=int(seed_entry["value"]), size=scheduled, objective=objective, cancel_check=cancel_check)
    except JobCancelled:
        raise
    except Exception as exc:
        return failed_component(exc, "carry-over")
    finite = np.isfinite(objectives)
    below = int(np.count_nonzero(objectives[finite] < recorded))
    finite_count = int(np.count_nonzero(finite))
    rank = below / finite_count if finite_count else 0.0
    # The curve the panel plots: the reproduction's own 1st-99th percentiles, plot summaries and never individual rehearsals.
    curve = [float(value) for value in np.quantile(objectives[finite], np.linspace(0.01, 0.99, 99), method="linear")] if finite_count else []
    return {
        "status": "measured",
        "label": _rank_label(below, finite_count),
        "rank": rank,
        "below": below,
        "finite": finite_count,
        "undefined": int(REPLICATIONS - finite_count),
        "recorded_objective": float(recorded),
        "curve": {"p": curve, "finite": finite_count},
        "sentence": _rank_sentence(below, finite_count, REPLICATIONS - finite_count, OBJECTIVES[objective][0]),
        "selection_note": SELECTION_NOTE,
        "basis": {
            "schema_version": SCHEMA_CARRY_OVER,
            "rehearsal_schema": basis.get("schema_version"),
            "seed": {"inputs": list(seed_entry.get("inputs", [])), "value": int(seed_entry["value"])},
            "scheduled_sessions": scheduled,
            "objective": {"measure": objective, "label": OBJECTIVES[objective][0], "units": OBJECTIVES[objective][1]},
            "source_digest": source.get("series_digest", {}).get("candidate"),
        },
    }


def _recorded_objective(metrics: Metrics, objective: str) -> float | None:
    if objective == "sharpe_ratio":
        return metrics.sharpe_ratio
    if objective == "total_return_pct":
        return metrics.total_return_pct
    if objective == "net_profit":
        return metrics.net_profit
    return None


def fit_to_fit_reading(
    *,
    objective: str,
    window: tuple[int, int],
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    cancel_check: _Cancel = lambda: None,
) -> dict[str, Any]:
    """Two distinct completed fits' final comparison: one shared paired draw, both directions summarized from it.

    ``first`` and ``second`` are (metrics, detail, point_hash) mappings. A failed or missing side leaves a named
    missing optional comparison; each fit's own incumbent comparison beside it is unaffected.
    """
    sides: list[SessionSeries] = []
    for side in (first, second):
        try:
            admitted = admit_run(side["metrics"], side["detail"], window)
        except JobCancelled:
            raise
        except Exception as exc:
            return failed_component(exc, "fit-to-fit")
        if not isinstance(admitted, SessionSeries):
            return _missing(("fit_unavailable", f"One fit's final run could not be read, so the two cannot be paired. Recorded reason: {admitted.get('reason', 'unreadable')}"))
        sides.append(admitted)
    if sides[0].session_ms != sides[1].session_ms:
        return _missing(("fit_unavailable", "The two fits' final runs do not cover the same scheduled sessions, so they cannot be paired path by path."))
    if sides[0].capital != sides[1].capital:
        return _missing(("fit_unavailable", "The two fits' final runs start from different capitals, so a paired comparison has no single scope."))
    hashes = sorted((str(first["point_hash"]), str(second["point_hash"])))
    seed_inputs = [SCHEMA_FIT_TO_FIT, window[0], window[1], objective, *hashes]
    seed = seed_for(*seed_inputs)
    counts = {"better": 0, "tied": 0, "worse": 0, "undefined": 0}
    rng = np.random.default_rng(seed)
    for chunk in stationary_index_chunks(len(sides[0].session_ms), size=len(sides[0].session_ms), mean_block=MEAN_BLOCK_SESSIONS, replications=REPLICATIONS, rng=rng):
        cancel_check()
        own = path_objective(sides[0].returns[chunk], objective, sides[0].capital)
        other = path_objective(sides[1].returns[chunk], objective, sides[1].capital)
        chunk_counts, _ = _pair_chunk(own, other)
        for name, value in chunk_counts.items():
            counts[name] += value
    label = OBJECTIVES[objective][0]
    sentence = (
        f"In {REPLICATIONS:,} paired resamples of the final sessions, {first['label']} was at least as good as {second['label']} in "
        f"{(counts['better'] + counts['tied']) / REPLICATIONS:.1%} of paths ({counts['better']:,} better, {counts['tied']:,} tied, {counts['worse']:,} worse). "
        "Both selections were made after these results were viewed; this is not adjusted for that choice."
    )
    return {
        "status": "measured",
        "pair": hashes,
        "counts": dict(counts),
        "shares": {name: value / REPLICATIONS for name, value in counts.items()},
        "at_least_as_good": {"count": counts["better"] + counts["tied"], "share": (counts["better"] + counts["tied"]) / REPLICATIONS},
        "sentence": sentence,
        "selection_note": SELECTION_NOTE,
        "basis": {
            "schema_version": SCHEMA_FIT_TO_FIT,
            "convention": "stationary-bootstrap/v1",
            "replications": REPLICATIONS,
            "mean_block_sessions": MEAN_BLOCK_SESSIONS,
            "thresholds": {"clear": 0.95},
            "objective": {"measure": objective, "label": label, "units": OBJECTIVES[objective][1]},
            "seed": {"inputs": seed_inputs, "value": seed},
            "source": {
                "window": {"start_ms": window[0], "end_ms": window[1]},
                "session_count": len(sides[0].session_ms),
                "point_hashes": hashes,
                "series_digests": [series_digest(side) for side in sides],
                "capital": sides[0].capital,
            },
        },
    }


#: What each component's never-recorded state says, so the copy names the component it belongs to.
_NOT_MEASURED_BY_COMPONENT: dict[str, tuple[str, str]] = {
    "comparison": ("not_measured", "Not measured for this study: final comparison readings were first recorded with #3014, and stored evidence is never backfilled."),
    "profitability": ("not_measured", "Not measured for this study: final profitability readings were first recorded with #3014, and stored evidence is never backfilled."),
    "carry_over": CARRY_OVER_MISSING,
}

#: The statuses each stored component may read back as-recorded; the comparison components share #3011's set
#: (``same_settings``/``baseline`` never arise from the exam wiring, but a stored record that holds one is
#: served as it stands, never re-classified), while the carry-over is this slice's own vocabulary.
_KNOWN_BY_COMPONENT: dict[str, frozenset[str]] = {
    "comparison": KNOWN_STATUSES,
    "profitability": KNOWN_STATUSES,
    "carry_over": frozenset({"measured", "missing", "failed"}),
}


def stored_final_chance(fit: Mapping[str, Any]) -> dict[str, Any]:
    """The per-fit final chance components of one stored exam fit, normalized per component like every other chance reading."""
    stored = fit.get("chance") or {}
    components: dict[str, dict[str, Any]] = {}
    for component in ("comparison", "profitability", "carry_over"):
        record = stored.get(component)
        if isinstance(record, Mapping) and record.get("status") in _KNOWN_BY_COMPONENT[component]:
            components[component] = dict(record)
        elif isinstance(record, Mapping):
            components[component] = _missing(
                ("unsupported", f"The stored {component} reading holds status {record.get('status')!r}, which this server cannot interpret; it is shown as unavailable rather than guessed at.")
            )
        else:
            components[component] = _missing(_NOT_MEASURED_BY_COMPONENT[component])
    return components
