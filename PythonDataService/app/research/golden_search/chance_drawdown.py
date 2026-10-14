"""Session-close drawdown uncertainty: how deep the recorded development outcome's resamples fall (#3013, ADR 0074 decision 12).

Formula: over the source development window's own n sessions, B stationary-
  bootstrap draws of n sessions each (the #2950 chunked convention); on every
  path the worst fall from a running peak measured at session closes with
  the starting capital as the initial peak (``chance.path_worst_fall``).
  Stored: the recorded run's own close-only worst fall; the resampled
  maximum's 50th and 95th percentiles by linear interpolation over the
  finite paths, with their finite count; integer counts of finite falls
  strictly beyond the frozen ceiling, at or within it, and undefined — all
  over B. A percentile is labelled "95th percentile"; no promise is made
  that exactly one path in twenty falls at least that far.
Reference: issue #3013's delivery contract (the 2026-10-09 adversarial
  review of #2932); the draw is Politis & Romano (1994) via
  ``monte_carlo/stationary.py``; the ceiling is the plan's frozen
  selection rule.
Canonical implementation: this file for the counting and its basis;
  ``chance.path_worst_fall`` for one path's fall; ``chance_compare`` for
  the source admission this slice consumes; ``monte_carlo/stationary.py``
  for the index draw.
Validated against: tests/research/golden_search/test_chance_drawdown.py
  (hand-computed paths, initial-capital loss, quantile interpolation and
  ties, ceiling equality versus strict breach, nonfinite draws,
  candidate-only availability, aliases, first-bar convention difference).

Peak convention, stated rather than hidden: the engine's own worst fall
starts its peak at the first bar it holds, while this reading starts at
the starting capital, so the two are not ordered — a run already below
its starting capital on the first bar counts that loss here and not
there. This is a research-risk view of the resampled development outcome
at session closes under the recorded sizing and cost assumptions. It is
not a maximum future loss, a safe capital amount, a margin figure, or a
promise about intraday losses, gaps, altered costs or regime change, and
it recommends no capital or position size.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from app.jobs.progress import JobCancelled
from app.research.golden_search.chance import (
    CONVENTION,
    MEAN_BLOCK_SESSIONS,
    REPLICATIONS,
    SessionSeries,
    path_worst_fall,
    seed_for,
)
from app.research.golden_search.chance_compare import _missing, admit_run, failed_component, series_digest
from app.research.golden_search.selection import Metrics
from app.research.monte_carlo.stationary import CHUNK_REPLICATIONS, stationary_index_chunks

#: What a stored drawdown reading calls itself; a change to the reading's shape or interpretation needs a new one.
SCHEMA_DRAWDOWN = "golden-search-chance-drawdown/v1"

NOT_MEASURED = (
    "not_measured",
    "Not measured for this study: drawdown readings were first recorded with #3013, and stored evidence is never backfilled.",
)

_Cancel = Callable[[], object]


def count_falls(falls: np.ndarray, ceiling: float) -> dict[str, int]:
    """One chunk's boundary counts: a finite fall exactly at the ceiling is within it; only a strictly deeper one is beyond."""
    finite = np.isfinite(falls)
    return {
        "beyond_ceiling": int(np.count_nonzero(falls[finite] > ceiling)),
        "within_ceiling": int(np.count_nonzero(falls[finite] <= ceiling)),
        "undefined": int(len(falls) - np.count_nonzero(finite)),
    }


def _percentiles(falls: np.ndarray) -> dict[str, Any]:
    finite = falls[np.isfinite(falls)]
    if finite.size == 0:
        return {"p50": None, "p95": None, "finite": 0}
    p50, p95 = np.quantile(finite, (0.5, 0.95), method="linear")
    return {"p50": float(p50), "p95": float(p95), "finite": int(finite.size)}


def _sentence(counts: Mapping[str, int], percentiles: Mapping[str, Any], ceiling: float) -> str:
    beyond, undefined = counts["beyond_ceiling"], counts["undefined"]
    p95 = percentiles["p95"]
    headline = (
        f"The 95th percentile of resampled session-close falls is {p95:.1%}, beyond the {ceiling:.0%} ceiling — a research concern under this model."
        if isinstance(p95, float) and p95 > ceiling
        else f"The 95th percentile of resampled session-close falls is {p95:.1%}, within the {ceiling:.0%} ceiling." if p95 is not None else "No resample produced a defined fall, so no drawdown range exists under this model."
    )
    share = f" {beyond / REPLICATIONS:.1%} of resamples fall strictly beyond the ceiling and {undefined / REPLICATIONS:.1%} leave the fall undefined." if undefined or beyond else ""
    return headline + share + " This is the model's spread of the recorded development outcome, not a maximum future loss."


def drawdown_reading(
    *,
    window: tuple[int, int],
    ceiling: float,
    metrics: Metrics | None,
    detail: Mapping[str, Any] | None,
    point_hash: str,
    cancel_check: _Cancel = lambda: None,
) -> dict[str, Any]:
    """One candidate's own session-close drawdown reading — the incumbent included, no pairing required.

    Known missing inputs come back as typed missing reasons; an unexpected error is logged and comes back as a
    diagnostic failure; cancellation between chunks propagates and is never recorded as a reading.
    """
    try:
        admitted = admit_run(metrics, detail, window)
    except JobCancelled:
        raise
    except Exception as exc:
        return failed_component(exc, "drawdown")
    if not isinstance(admitted, SessionSeries):
        return dict(admitted)
    recorded_engine = None if metrics is None or metrics.max_drawdown_pct is None else float(metrics.max_drawdown_pct)
    try:
        return _draw(admitted, window=window, ceiling=ceiling, point_hash=point_hash, recorded_engine=recorded_engine, cancel_check=cancel_check)
    except JobCancelled:
        raise
    except Exception as exc:
        return failed_component(exc, "drawdown")


def _draw(
    admitted: SessionSeries,
    *,
    window: tuple[int, int],
    ceiling: float,
    point_hash: str,
    recorded_engine: float | None,
    cancel_check: _Cancel,
) -> dict[str, Any]:
    seed_inputs = [SCHEMA_DRAWDOWN, window[0], window[1], point_hash]
    seed = seed_for(*seed_inputs)
    counts = {"beyond_ceiling": 0, "within_ceiling": 0, "undefined": 0}
    falls = np.full(REPLICATIONS, np.nan)
    done = 0
    rng = np.random.default_rng(seed)
    for chunk in stationary_index_chunks(len(admitted.session_ms), size=len(admitted.session_ms), mean_block=MEAN_BLOCK_SESSIONS, replications=REPLICATIONS, rng=rng):
        cancel_check()
        chunk_falls = path_worst_fall(admitted.returns[chunk])
        for name, value in count_falls(chunk_falls, ceiling).items():
            counts[name] += value
        falls[done : done + len(chunk_falls)] = chunk_falls
        done += len(chunk_falls)
    percentiles = _percentiles(falls)
    recorded_close_only = float(path_worst_fall(admitted.returns[np.newaxis, :])[0])
    return {
        "status": "measured",
        "recorded_close_only": recorded_close_only,
        "recorded_engine_per_bar": recorded_engine,
        "percentiles": percentiles,
        "counts": dict(counts),
        "shares": {name: value / REPLICATIONS for name, value in counts.items()},
        "sentence": _sentence(counts, percentiles, ceiling),
        "concern": isinstance(percentiles["p95"], float) and percentiles["p95"] > ceiling,
        "basis": {
            "schema_version": SCHEMA_DRAWDOWN,
            "convention": CONVENTION,
            "replications": REPLICATIONS,
            "chunk_replications": CHUNK_REPLICATIONS,
            "mean_block_sessions": MEAN_BLOCK_SESSIONS,
            "thresholds": {"ceiling": ceiling},
            "horizon": {"window": {"start_ms": window[0], "end_ms": window[1]}, "scheduled_sessions": len(admitted.session_ms)},
            "seed": {"inputs": seed_inputs, "value": seed},
            "source": {
                "point_hash": point_hash,
                "series_digest": series_digest(admitted),
                "capital": admitted.capital,
                "recorded_engine_per_bar": recorded_engine,
            },
        },
    }


def stored_drawdown(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The drawdown component of one stored evidence candidate, normalized like every other chance component."""
    record = (candidate.get("chance") or {}).get("drawdown")
    if isinstance(record, Mapping) and record.get("status") in {"measured", "missing", "failed"}:
        return dict(record)
    if isinstance(record, Mapping):
        return _missing(
            ("unsupported", f"The stored drawdown reading holds status {record.get('status')!r}, which this server cannot interpret; it is shown as unavailable rather than guessed at.")
        )
    return _missing(NOT_MEASURED)
