"""Phase vocabulary for long-running jobs.

A job's runner emits ``phase`` events with a stable machine-readable id.
The same id is used in tests, CSS hooks, and progress weights. The
``friendly_label`` is what the user reads on the run panel — sentence
case, present continuous for in-progress, no jargon. Keep this file the
single source of truth so backend logs, frontend status pills, and
documentation never drift.

Adding a new job type:
    1. Define an ordered list of (phase_id, friendly_label, weight).
    2. Register it in ``JOB_PHASES`` keyed by the public job-type slug.
    3. Use ``friendly(job_type, phase_id)`` from the runner when
       emitting ``on_log`` so the message reads cleanly even if the
       phase id changes later.

Weights (rough): heavy phases get a larger share of the progress bar.
The frontend doesn't have to use them; they're a hint.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Phase:
    """One phase in a job's lifecycle."""

    id: str
    label: str
    weight: int = 1


# ── Cross-sectional batch runner ─────────────────────────────────────────
# The cross-sectional runner emits one phase per ticker (``ticker_{n}_{SYM}``)
# plus the framework boundaries. We list the framework phases here; per-ticker
# phases generate their friendly label dynamically from the ticker symbol.
CROSS_SECTIONAL_PHASES: tuple[Phase, ...] = (
    Phase("starting", "Starting cross-sectional study", 1),
    Phase("aggregating", "Aggregating per-ticker results", 1),
    Phase("completed", "Run complete", 1),
)


# ── Feature research runner ──────────────────────────────────────────────
FEATURE_RESEARCH_PHASES: tuple[Phase, ...] = (
    Phase("loading_bars", "Loading price history", 1),
    Phase("compute_target", "Computing forward returns", 1),
    Phase("compute_feature", "Computing feature values", 2),
    Phase("compute_ic", "Measuring information coefficient", 2),
    Phase("stationarity", "Running stationarity tests", 1),
    Phase("quantile", "Running quantile analysis", 1),
    Phase("robustness", "Running robustness checks", 2),
    Phase("validate", "Scoring against validation gates", 1),
    Phase("completed", "Run complete", 1),
)


# ── Signal engine runner ─────────────────────────────────────────────────
SIGNAL_ENGINE_PHASES: tuple[Phase, ...] = (
    Phase("loading_bars", "Loading price history", 1),
    Phase("compute_feature", "Computing the signal feature", 1),
    Phase("diagnostics", "Running diagnostic checks", 1),
    Phase("regime_coverage", "Checking regime coverage", 1),
    Phase("backtest_grid", "Sweeping backtest configurations", 4),
    Phase("walk_forward", "Walking forward through history", 4),
    Phase("effective_sample", "Estimating effective sample size", 1),
    Phase("graduation", "Computing graduation verdict", 1),
    Phase("completed", "Run complete", 1),
)


# ── LEAN sidecar run (external reference runner) ────────────────────────
# Coarse phases emitted by ``app.services.lean_sidecar_service.run_trusted_sample``
# when invoked through the ``lean_engine_run`` job worker. ``sidecar_running``
# is opaque elapsed-time (no sub-bar progress in v1 — the LEAN container
# is a black box from our side). Pairs with the canonical engine's
# taxonomy (see ENGINE_BACKTEST_PHASES once #471 merges) so the Engine
# Lab run dock shows consistent terminology across engines.
LEAN_ENGINE_RUN_PHASES: tuple[Phase, ...] = (
    Phase("staging_data", "Staging LEAN data fixtures", 2),
    Phase("launching_sidecar", "Submitting launch request", 1),
    Phase("sidecar_running", "LEAN container running", 5),
    Phase("parsing_results", "Parsing LEAN output", 1),
    Phase("persisting", "Persisting run to history", 1),
    Phase("done", "Run complete", 1),
)


JOB_PHASES: dict[str, tuple[Phase, ...]] = {
    "cross_sectional": CROSS_SECTIONAL_PHASES,
    "feature_research": FEATURE_RESEARCH_PHASES,
    "signal_engine": SIGNAL_ENGINE_PHASES,
    "lean_engine_run": LEAN_ENGINE_RUN_PHASES,
}


def friendly(job_type: str, phase_id: str) -> str:
    """Return the user-facing label for a phase id.

    Falls back to a humanized form of the id if the job type or phase id
    isn't registered (e.g. per-ticker dynamic phases like
    ``ticker_3_AAPL`` → "Ticker 3 AAPL"). This keeps the runner
    forward-compatible with phases the vocabulary table hasn't caught
    up with yet.
    """
    table = JOB_PHASES.get(job_type, ())
    for phase in table:
        if phase.id == phase_id:
            return phase.label
    return _humanize(phase_id)


def total_weight(job_type: str) -> int:
    """Sum of all phase weights for the job type. Useful for the UI to
    convert phase index → fractional progress when the runner doesn't
    emit explicit ``on_progress``."""
    return sum(p.weight for p in JOB_PHASES.get(job_type, ()))


def _humanize(token: str) -> str:
    """Turn ``ticker_3_AAPL`` into ``Ticker 3 AAPL``."""
    parts = [p for p in token.replace("-", "_").split("_") if p]
    if not parts:
        return token
    return " ".join(p.capitalize() if p.islower() else p for p in parts)
