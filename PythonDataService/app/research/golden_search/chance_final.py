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
Canonical implementation: this file for the carry-over rank, the
  fit-to-fit pairing and the validation of stored final components
  (#3024); ``chance_compare`` for the paired counting both reuse;
  ``chance_rehearsal`` for the stored basis it reproduces.
Validated against: tests/research/golden_search/test_chance_final.py
  (ranks 0 and 1, ties at the recorded value, a discrete distribution
  where counting stored percentiles disagrees with the empirical rank,
  tails, fewer than all finite, mismatched objective/length/digest,
  stored constants different from today's, swapped fit-to-fit symmetry)
  and — for the stored-record validation #3024 adds — the invalid-record
  fixtures (unsupported versions, forged labels and inclusive counts,
  basisless carry-overs, wrong-fit evidence, malformed outer mappings,
  mixed valid and invalid siblings, aliases and the enrolled-versus-
  legacy split), and for the exact reproduction boundary #3025 adds —
  stored lengths shorter and longer than the frozen horizon, a different
  final window of equal length, a different final capital, unknown
  rehearsal schemas, wrong and missing chunk sizes, unit and development-
  window mismatches, with every numerical comparison pinned to exact
  equality or a strict absolute tolerance.

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
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
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
    SCHEMA_COMPARISON,
    SCHEMA_PROFITABILITY,
    _missing,
    _pair_chunk,
    admit_run,
    failed_component,
    series_digest,
)
from app.research.golden_search.chance_rehearsal import SCHEMA_REHEARSAL, final_session_count
from app.research.golden_search.selection import Metrics
from app.research.monte_carlo.stationary import CHUNK_REPLICATIONS, stationary_index_chunks

logger = logging.getLogger(__name__)

#: What the stored per-fit final readings call themselves; a change to their interpretation needs a new one.
SCHEMA_CARRY_OVER = "golden-search-chance-carry-over/v1"
#: What the stored fit-to-fit reading calls itself; v2 stores both directions of the one shared draw (#3029).
SCHEMA_FIT_TO_FIT = "golden-search-chance-fit-to-fit/v2"
#: The #3014 shape: one direction only — the first recorded fit's — with no identity inside the record.
SCHEMA_FIT_TO_FIT_V1 = "golden-search-chance-fit-to-fit/v1"

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

    The match is exact — point hash, series digest, development source window, objective and units, starting
    capital on both sides, the stored final interval and its 1× scheduled length against the frozen study's own
    calendar — and a mismatch, an unknown schema or convention, a nonfinite recorded objective or a scope
    mismatch is a named missing carry-over, never a fabricated rank. Today's constants — including the chunk
    size the draw order depends on — are never borrowed for a stored basis that differs (#3025).
    """
    if stored_rehearsal is None or stored_rehearsal.get("status") != "measured":
        return _missing(CARRY_OVER_MISSING, detail=None if stored_rehearsal is None else f"Recorded state: {stored_rehearsal.get('status', 'unreadable')}.")
    basis = stored_rehearsal.get("basis") or {}
    source = basis.get("source") or {}
    if basis.get("schema_version") != SCHEMA_REHEARSAL:
        return _missing(
            CARRY_OVER_MISSING,
            detail=f"The stored rehearsal holds schema {basis.get('schema_version')!r}, which this server cannot reproduce; a rank is never redrawn under a basis it cannot name.",
        )
    stored_objective = (basis.get("objective") or {}).get("measure")
    if stored_objective != objective:
        return _missing(CARRY_OVER_MISSING, detail=f"The stored rehearsal was drawn for the {stored_objective} objective, not this plan's {objective}.")
    if (basis.get("objective") or {}).get("units") != OBJECTIVES[objective][1]:
        return _missing(CARRY_OVER_MISSING, detail=f"The stored rehearsal counts its objective in {(basis.get('objective') or {}).get('units')!r}, not this plan's {OBJECTIVES[objective][1]}.")
    if source.get("candidate_point_hash") != point_hash:
        return _missing(CARRY_OVER_MISSING, detail="The stored rehearsal belongs to different settings.")
    source_window = source.get("window") or {}
    if source_window.get("start_ms") != development_window[0] or source_window.get("end_ms") != development_window[1]:
        return _missing(CARRY_OVER_MISSING, detail="The stored rehearsal was drawn over a different development window than this study's frozen one.")
    stored_final_window = (basis.get("final_interval") or {}).get("window") or {}
    if stored_final_window.get("start_ms") != final_window[0] or stored_final_window.get("end_ms") != final_window[1]:
        return _missing(CARRY_OVER_MISSING, detail="The stored rehearsal rehearsed a different final interval than this study's frozen one.")
    scheduled = (basis.get("final_interval") or {}).get("scheduled_sessions")
    if not isinstance(scheduled, int) or scheduled < 1:
        return _missing(CARRY_OVER_MISSING, detail="The stored rehearsal names no valid 1× scheduled length.")
    # The stored 1× length is the frozen final interval's own calendar count, checked — never re-derived from
    # today's plan — so a rehearsal of another horizon cannot rank this study's result (#3025).
    horizon = final_session_count(final_window)
    if scheduled != horizon:
        return _missing(
            CARRY_OVER_MISSING,
            detail=f"The stored rehearsal's 1× length is {scheduled} sessions, but this study's frozen final interval schedules {horizon}, so its paths are rehearsals of another horizon.",
        )
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
    # The chunk size is a seeded draw-order parameter: it decides how the one rng is dealt into draws, so a
    # rehearsal chunked differently would reorder the same randomness into different paths (#3025).
    if basis.get("chunk_replications") != CHUNK_REPLICATIONS:
        return _missing(
            CARRY_OVER_MISSING,
            detail=f"The stored rehearsal was drawn in chunks of {basis.get('chunk_replications')!r} replications, not the {CHUNK_REPLICATIONS:,} the draw order depends on, so today's draw would not repeat its paths.",
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
    # The recorded result must cover exactly the horizon the rehearsal rehearsed — not merely at least the
    # stored count — and start from the same capital its paths compound from: a net-profit result from a
    # different account never ranks against them (#3025).
    if len(final_series.session_ms) != scheduled:
        return _missing(CARRY_OVER_MISSING, detail=f"The final run covers {len(final_series.session_ms)} sessions, not exactly the rehearsal's {scheduled}-session 1× length, so the recorded result cannot rank against its paths.")
    if final_series.capital != source.get("capital"):
        return _missing(CARRY_OVER_MISSING, detail="The final run starts from a different capital than the rehearsal's stored scope, so its result cannot rank against the rehearsal's paths.")
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
    basis = {
        "schema_version": SCHEMA_FIT_TO_FIT,
        "convention": "stationary-bootstrap/v1",
        "replications": REPLICATIONS,
        "mean_block_sessions": MEAN_BLOCK_SESSIONS,
        "thresholds": {"clear": 0.95},
        "objective": {"measure": objective, "label": OBJECTIVES[objective][0], "units": OBJECTIVES[objective][1]},
        "seed": {"inputs": seed_inputs, "value": seed},
        "source": {
            "window": {"start_ms": window[0], "end_ms": window[1]},
            "session_count": len(sides[0].session_ms),
            "point_hashes": hashes,
            # In the pair's canonical (sorted) order, so each digest names its point through it (#3029).
            "series_digests": [series_digest(side) for _, side in sorted(zip(hashes, sides, strict=True), key=lambda item: item[0])],
            "capital": sides[0].capital,
        },
    }
    return {
        "status": "measured",
        "pair": hashes,
        "directions": both_directions(
            counts,
            first={"point_hash": str(first["point_hash"]), "label": str(first["label"])},
            second={"point_hash": str(second["point_hash"]), "label": str(second["label"])},
            basis=basis,
        ),
        "selection_note": SELECTION_NOTE,
        "basis": basis,
    }


def _direction_sentence(fit_label: str, opponent_label: str, counts: Mapping[str, int]) -> str:
    return (
        f"In {REPLICATIONS:,} paired resamples of the final sessions, {fit_label} was at least as good as {opponent_label} in "
        f"{(counts['better'] + counts['tied']) / REPLICATIONS:.1%} of paths ({counts['better']:,} better, {counts['tied']:,} tied, {counts['worse']:,} worse). "
        "Both selections were made after these results were viewed; this is not adjusted for that choice."
    )


def _direction(identity: Mapping[str, str], opponent: Mapping[str, str], counts: Mapping[str, int], basis: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "measured",
        "fit": dict(identity),
        "opponent": dict(opponent),
        "counts": dict(counts),
        "shares": {name: value / REPLICATIONS for name, value in counts.items()},
        "at_least_as_good": {"count": counts["better"] + counts["tied"], "share": (counts["better"] + counts["tied"]) / REPLICATIONS},
        "sentence": _direction_sentence(identity["label"], opponent["label"], counts),
        "selection_note": SELECTION_NOTE,
        "basis": basis,
    }


def both_directions(counts: Mapping[str, int], *, first: Mapping[str, str], second: Mapping[str, str], basis: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Both directional summaries of one paired draw's event counts, each naming its own fit and opponent (#3029).

    The second direction is derived from the same counts — better and worse reverse, tied and undefined stay
    equal — never from another draw, and an engine never runs again. Tied paths support both directions and
    undefined paths support neither, so the opposite inclusive support is (worse + tied) over B, never
    one minus the other direction's.
    """
    reversed_counts = {"better": counts["worse"], "tied": counts["tied"], "worse": counts["better"], "undefined": counts["undefined"]}
    return [
        _direction(first, second, counts, basis),
        _direction(second, first, reversed_counts, basis),
    ]


def resolved_fit_to_fit(exam: Mapping[str, Any], fit: Mapping[str, Any]) -> dict[str, Any]:
    """The fit-to-fit reading as one fit sees it: the direction of the shared draw that names this fit (#3029).

    A v2 record serves the direction whose fit identity is this fit's point hash — aliases share the hash and
    so share the direction — and a fit the pair never compared gets a named missing, never a complement
    computed in the browser or here. A legacy v1 record holds one direction only, the first recorded fit's
    (the exam wiring always passed it first); that fit reads its own record honestly, the other reads a named
    unavailable directional view, and nothing is redrawn, relabelled or backfilled on read.
    """
    chance = exam.get("chance")
    record = chance.get("fit_to_fit") if isinstance(chance, Mapping) else None
    if not isinstance(record, Mapping):
        return _missing(("not_measured", "Not measured for this study: fit-to-fit readings were first recorded with #3014."))
    if record.get("status") != "measured":
        return dict(record)
    schema = (record.get("basis") or {}).get("schema_version")
    if schema == SCHEMA_FIT_TO_FIT:
        for direction in record.get("directions") or []:
            if isinstance(direction, Mapping) and (direction.get("fit") or {}).get("point_hash") == fit.get("point_hash"):
                return dict(direction)
        return _missing(("not_compared", "This fit was not one of the two the stored fit-to-fit record compared, so it has no direction to read."))
    if schema == SCHEMA_FIT_TO_FIT_V1:
        fits = [stored for stored in exam.get("fits") or [] if isinstance(stored, Mapping)]
        if fits and fit.get("point_hash") == fits[0].get("point_hash"):
            return dict(record)
        first_label = fits[0].get("label", "first recorded fit") if fits else "the first recorded fit"
        return _missing(
            ("direction_unavailable", f"The stored fit-to-fit record predates both-direction summaries (#3029): it recorded only the {first_label}'s direction, and this fit's own direction was never recorded. It is not recomputed on read.")
        )
    return _missing(
        ("unsupported", f"The stored fit-to-fit reading holds schema {schema!r}, which this server cannot interpret; it is shown as unavailable rather than guessed at.")
    )


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

#: The schema versions this server can interpret, per component; anything else reads unavailable (#3024).
_SUPPORTED_SCHEMAS: dict[str, str] = {
    "comparison": SCHEMA_COMPARISON,
    "profitability": SCHEMA_PROFITABILITY,
    "carry_over": SCHEMA_CARRY_OVER,
}
_SUPPORTED_LABELS: dict[str, frozenset[str]] = {
    "comparison": frozenset({"clearly_as_good", "clearly_worse", "identical_outcomes", "mixed", "no_defined_paths"}),
    "profitability": frozenset({"clearly_positive", "clearly_negative", "mixed", "no_defined_paths"}),
    "carry_over": frozenset({"below_most", "above_most", "middle", "no_defined_paths"}),
}


@dataclass(frozen=True)
class FinalChanceScope:
    """The frozen study identity a stored final reading must belong to before it is served or interpreted (#3024).

    Built from the locked protocol and the fit a page, approval intent or qualification names, so a valid-looking
    record that belongs to another fit, window, capital or objective never reads as this fit's evidence. Aliases
    need no special case: they share one point hash, and the hash is the identity.
    """

    objective: str
    development_window: tuple[int, int]
    final_window: tuple[int, int]
    capital: float
    point_hash: str

    def for_fit(self, point_hash: str) -> FinalChanceScope:
        """The same scope bound to one fit's point identity."""
        return replace(self, point_hash=point_hash)


# ── Stored-record validation (#3024) ─────────────────────────────────────
#
# One authority, served through ``stored_final_chance``, checks every measured record before the page renders
# it, the approval intent reads it or the qualification payload freezes it. A record that fails reads as a
# named ``invalid_record`` missing reason — never repaired with today's constants, never reclassified, and
# never persisted back.


def _count(value: Any) -> int | None:
    """A stored count: a true integer at least zero — booleans and coerced strings are never counts."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _number(value: Any) -> float | None:
    """A stored number: int or float (never a boolean), finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _checked_thresholds(basis: Mapping[str, Any]) -> str | None:
    thresholds = basis.get("thresholds")
    if not isinstance(thresholds, Mapping):
        return "its basis holds no threshold record"
    for name, value in thresholds.items():
        number = _number(value)
        if number is None:
            return f"its basis threshold {name!r} is not a finite number"
        if name == "clear" and not 0.0 <= number <= 1.0:
            return f"its basis clear threshold {number:g} is not a share between 0 and 1"
    return None


def _checked_seed(basis: Mapping[str, Any]) -> str | None:
    seed = basis.get("seed")
    if not isinstance(seed, Mapping) or _count(seed.get("value")) is None or not isinstance(seed.get("inputs"), Sequence) or isinstance(seed.get("inputs"), (str, bytes)):
        return "its basis kept no readable seed"
    return None


def _checked_source(basis: Mapping[str, Any], scope: FinalChanceScope) -> str | None:
    """The identity binding of a stored comparison or profitability record: the fit, the window, the capital."""
    source = basis.get("source")
    if not isinstance(source, Mapping):
        return "its basis names no source run"
    window = source.get("window")
    if not isinstance(window, Mapping) or window.get("start_ms") != scope.final_window[0] or window.get("end_ms") != scope.final_window[1]:
        return "its basis source window is not this study's frozen final window"
    if source.get("candidate_point_hash") != scope.point_hash:
        return "its basis names another fit's point, not the fit this reading is being read for"
    capital = _number(source.get("capital"))
    if capital is None or capital != scope.capital:
        return "its basis names a starting capital other than this study's frozen scope"
    return None


def _checked_range(value: Any, *, name: str, finite_maximum: int) -> str | None:
    """A stored 5/50/95 range: finite support consistent with the counts behind it, endpoints finite and ordered."""
    if not isinstance(value, Mapping):
        return f"its {name} is not a recorded range"
    finite = _count(value.get("finite"))
    if finite is None or finite > finite_maximum:
        return f"its {name} claims {value.get('finite')!r} finite paths, which is not a count the reading could have produced"
    percentiles = []
    for key in ("p5", "p50", "p95"):
        raw = value.get(key)
        if finite == 0:
            if raw is not None:
                return f"its {name} records percentiles with no finite path behind them"
            continue
        number = _number(raw)
        if number is None:
            return f"its {name} holds a {key} that is not a finite number"
        percentiles.append(number)
    if percentiles and not percentiles[0] <= percentiles[1] <= percentiles[2]:
        return f"its {name} percentiles are not ordered from 5th to 95th"
    return None


def _invalid_share_record(component: str, record: Mapping[str, Any], scope: FinalChanceScope) -> str | None:
    """Why a measured comparison or profitability record cannot be interpreted, or None when it can.

    Wire-required presentation fields are checked too, so a record that reaches the page never breaks the
    response contract. The interpretation-bearing fields — counts, totals, the inclusive count, the label —
    are checked against each other, never against today's constants: a historical threshold keeps its own
    spelling.
    """
    basis = record.get("basis")
    if not isinstance(basis, Mapping):
        return "it records no interpretation basis"
    if basis.get("schema_version") != _SUPPORTED_SCHEMAS[component]:
        return f"its schema version {basis.get('schema_version')!r} is not a supported {component} schema"
    if basis.get("convention") != CONVENTION:
        return f"it was drawn under the {basis.get('convention')!r} convention, which this server cannot interpret"
    replications = _count(basis.get("replications"))
    if replications is None or replications == 0:
        return f"its basis names {basis.get('replications')!r} replications, which is no draw to read"
    if _count(basis.get("chunk_replications")) is None or _number(basis.get("mean_block_sessions")) is None:
        return "its basis omits a draw-order constant it was drawn under"
    if reason := _checked_thresholds(basis):
        return reason
    if reason := _checked_seed(basis):
        return reason
    if reason := _checked_source(basis, scope):
        return reason
    objective = basis.get("objective")
    if not isinstance(objective, Mapping) or objective.get("measure") != scope.objective or objective.get("units") != OBJECTIVES[scope.objective][1]:
        return "its basis objective is not this plan's frozen objective"
    keys = ("better", "tied", "worse") if component == "comparison" else ("positive", "zero", "negative")
    counts = record.get("counts")
    if not isinstance(counts, Mapping):
        return "it records no counts"
    numbers = {key: _count(counts.get(key)) for key in (*keys, "undefined")}
    if any(value is None for value in numbers.values()):
        return f"its counts hold a value that is not a nonnegative integer count ({', '.join(f'{key}: {counts.get(key)!r}' for key in (*keys, 'undefined'))})"
    if sum(numbers.values()) != replications:
        return f"its counts total {sum(numbers.values()):,} paths, not the {replications:,} replications the reading says it drew"
    defined = sum(numbers[key] for key in keys)
    label = record.get("label")
    if label not in _SUPPORTED_LABELS[component]:
        return f"its label {label!r} is not a reading this server can interpret"
    if (defined == 0) != (label == "no_defined_paths"):
        return f"its label {label!r} disagrees with its own counts ({defined:,} defined paths)"
    if _text(record.get("sentence")) is None:
        return "it records no operator sentence"
    shares = record.get("shares")
    if not isinstance(shares, Mapping) or any(_number(value) is None for value in shares.values()):
        return "its shares are not a recorded mapping of numbers"
    if component == "comparison":
        at_least = record.get("at_least_as_good")
        if not isinstance(at_least, Mapping) or _count(at_least.get("count")) != numbers["better"] + numbers["tied"] or _number(at_least.get("share")) is None:
            return f"its at-least-as-good count does not equal its better and tied counts ({numbers['better']:,} + {numbers['tied']:,})"
        if not isinstance(record.get("incomplete_support"), bool) or _text(record.get("bias_note")) is None:
            return "it omits the support-completeness flag or the selection caveat it must carry"
        if reason := _checked_range(record.get("candidate_range"), name="own-result range", finite_maximum=replications):
            return reason
        # A defined pair whose difference subtraction overflows keeps its comparison but not a difference
        # value, so the difference range may cover fewer paths than the counts defined — never more.
        if reason := _checked_range(record.get("difference_range"), name="difference range", finite_maximum=defined):
            return reason
    else:
        # The compounded-net range is optional — a stored reading from before #3026 carries none, and it is never
        # backfilled — but a range that is present must name its own quantity, never the objective's units.
        net_range = record.get("net_return_range")
        if net_range is not None:
            quantity = net_range.get("quantity") if isinstance(net_range, Mapping) else None
            if not isinstance(net_range, Mapping) or not isinstance(quantity, Mapping) or quantity.get("measure") != "compounded_net_return" or quantity.get("units") != "fraction of the starting capital":
                return "its net-return range does not name the compounded net return it claims to hold"
            if reason := _checked_range(net_range, name="net-return range", finite_maximum=replications):
                return reason
    return None


def _invalid_carry_record(record: Mapping[str, Any], scope: FinalChanceScope) -> str | None:
    """Why a measured carry-over record cannot be interpreted, or None when it can.

    The carry-over basis carries no window or point hash of its own; its identity is the stored seed's inputs —
    the rehearsal schema, the development window, the objective, the two points and the 1× rung — and the seed
    those inputs derive. A record whose inputs name another fit, window or objective, or whose seed does not
    follow from its inputs, is not this fit's carry-over.
    """
    basis = record.get("basis")
    if not isinstance(basis, Mapping):
        return "it records no interpretation basis"
    if basis.get("schema_version") != SCHEMA_CARRY_OVER:
        return f"its schema version {basis.get('schema_version')!r} is not a supported carry-over schema"
    if basis.get("rehearsal_schema") != SCHEMA_REHEARSAL:
        return f"it reproduces a rehearsal of schema {basis.get('rehearsal_schema')!r}, which this server cannot interpret"
    if _count(basis.get("scheduled_sessions")) is None or basis.get("scheduled_sessions") == 0:
        return f"its 1× length {basis.get('scheduled_sessions')!r} is not a session count"
    objective = basis.get("objective")
    if not isinstance(objective, Mapping) or objective.get("measure") != scope.objective or objective.get("units") != OBJECTIVES[scope.objective][1]:
        return "its basis objective is not this plan's frozen objective"
    seed = basis.get("seed")
    if not isinstance(seed, Mapping) or not isinstance(seed.get("inputs"), Sequence) or isinstance(seed.get("inputs"), (str, bytes)):
        return "its basis kept no readable seed"
    inputs = list(seed["inputs"])
    if len(inputs) < 7 or inputs[0] != SCHEMA_REHEARSAL or inputs[1] != scope.development_window[0] or inputs[2] != scope.development_window[1] or inputs[3] != scope.objective or scope.point_hash not in (inputs[4], inputs[5]) or inputs[6] != 1:
        return "its seed inputs do not name this fit's own 1× rehearsal on this study's development window"
    if seed.get("value") != seed_for(*inputs):
        return "its stored seed does not follow from its own seed inputs"
    below, finite, undefined = (_count(record.get(key)) for key in ("below", "finite", "undefined"))
    if below is None or finite is None or undefined is None or below > finite or (finite == 0 and (below != 0 or undefined == 0)):
        return f"its rank counts ({record.get('below')!r} below of {record.get('finite')!r} finite, {record.get('undefined')!r} undefined) are not coherent"
    rank = _number(record.get("rank"))
    if rank is None or not 0.0 <= rank <= 1.0:
        return f"its rank {record.get('rank')!r} is not a share between 0 and 1"
    if finite > 0 and abs(rank - below / finite) > 1e-12:
        return "its rank does not equal its strictly-below count over its finite count"
    recorded = _number(record.get("recorded_objective"))
    if recorded is None:
        return f"its recorded objective {record.get('recorded_objective')!r} is not a finite number"
    label = record.get("label")
    if label not in _SUPPORTED_LABELS["carry_over"]:
        return f"its label {label!r} is not a reading this server can interpret"
    if (finite == 0) != (label == "no_defined_paths") or (finite > 0 and label != _rank_label(below, finite)):
        return f"its label {label!r} disagrees with its own counts ({below:,} of {finite:,} strictly below)"
    curve = record.get("curve")
    if not isinstance(curve, Mapping) or _count(curve.get("finite")) != finite:
        return "its percentile curve names a different finite count than its rank does"
    points = curve.get("p")
    expected_length = 99 if finite > 0 else 0
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)) or len(points) != expected_length or any(_number(value) is None for value in points):
        return "its percentile curve is not the 99 plot summaries a reproduction of its length would hold"
    if _text(record.get("sentence")) is None or _text(record.get("selection_note")) is None:
        return "it records no operator sentence or selection caveat"
    return None


def _invalid_measured(component: str, record: Mapping[str, Any], scope: FinalChanceScope) -> str | None:
    if component == "carry_over":
        return _invalid_carry_record(record, scope)
    return _invalid_share_record(component, record, scope)


def stored_final_chance(fit: Mapping[str, Any], scope: FinalChanceScope) -> dict[str, Any]:
    """The per-fit final chance components of one stored exam fit, validated against the study each record claims to describe (#3024).

    Every measured component is checked — supported schema and draw convention, integer nonnegative counts that
    total the stored replications, the inclusive count, finite consistent ranges, a coherent label, and the fit,
    window, capital and objective the record belongs to — before it is served or interpreted. A record that
    fails reads ``missing`` with an ``invalid_record`` code and its specific reason; a malformed sibling never
    erases a valid one, nothing is recomputed or backfilled, and nothing is written back. A record this server
    cannot interpret at all reads ``unsupported``; an absent one keeps its legacy ``not_measured`` reason.
    """
    stored = fit.get("chance")
    components: dict[str, dict[str, Any]] = {}
    if stored is not None and not isinstance(stored, Mapping):
        for component in ("comparison", "profitability", "carry_over"):
            components[component] = _missing(
                ("unreadable", f"The stored final chance record is a {type(stored).__name__}, not a record of components, so the stored {component} reading cannot be read.")
            )
        return components
    for component in ("comparison", "profitability", "carry_over"):
        record = (stored or {}).get(component)
        if record is not None and not isinstance(record, Mapping):
            components[component] = _missing(
                ("unreadable", f"The stored {component} reading is a {type(record).__name__}, not a record, so it cannot be read.")
            )
        elif isinstance(record, Mapping) and record.get("status") in _KNOWN_BY_COMPONENT[component]:
            reason = _invalid_measured(component, record, scope) if record.get("status") == "measured" else None
            components[component] = dict(record) if reason is None else _missing(
                ("invalid_record", f"The stored {component} reading fails validation and is shown as unavailable rather than guessed at: {reason}.")
            )
        elif isinstance(record, Mapping):
            components[component] = _missing(
                ("unsupported", f"The stored {component} reading holds status {record.get('status')!r}, which this server cannot interpret; it is shown as unavailable rather than guessed at.")
            )
        else:
            components[component] = _missing(_NOT_MEASURED_BY_COMPONENT[component])
    return components
