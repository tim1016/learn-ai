"""Structured event emitters for IV30 / VRP threshold-firing audit.

Formula: Schema/orchestration only — structured logging of threshold-firing events; no arithmetic
Reference: Internal — no external reference
Canonical implementation: app/engine/edge/threshold_events.py
Validated against: NONE — pending

Companion to the recorder burn-in: every threshold or gate that can
silently kill a signal emits a structured log line tagged with
``event=<name>``. After ~100 forward signals, ``grep "event="`` over
the recorder logs gives a count by event so the operator can confirm
gates fire at the expected rate (and not, for example, every bar).

Adding events here is preferred over inlining ``logger.info(...)`` at
each call site so the event vocabulary stays small and greppable.

Event vocabulary (keep stable; downstream dashboards key off these):

- ``iv_dominance_warn`` — single-strike share crossed the warn band
  (below the hard gate).
- ``iv_dominance_gate`` — single-strike share triggered the hard gate
  and the IV30 was either recomputed or set to confidence=0.
- ``confidence_floor_fired`` — confidence below the hard floor; signal
  forced to 0 regardless of z-magnitude.
- ``imputed_prior_emitted`` — bar emitted with imputed (null/missing)
  ``health_score``; downstream confidence uses the missing-health
  branch rather than the multiplicative product.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def log_iv_dominance_warn(
    *,
    ticker: str,
    snapshot_ts_ms: int,
    max_share: float,
    threshold: float,
) -> None:
    """Emit when ``max_single_strike_share`` enters the warn band but
    has not yet hit the hard gate. Operator wants to see the run-up
    before the gate fires."""
    logger.warning(
        "[iv-threshold] dominance_warn ticker=%s ts=%d share=%.3f threshold=%.3f",
        ticker,
        snapshot_ts_ms,
        max_share,
        threshold,
        extra={
            "event": "iv_dominance_warn",
            "ticker": ticker,
            "snapshot_ts_ms": snapshot_ts_ms,
            "max_share": max_share,
            "threshold": threshold,
        },
    )


def log_iv_dominance_gate(
    *,
    ticker: str,
    snapshot_ts_ms: int,
    max_share_before: float,
    max_share_after: float | None,
    iterations: int,
    hard_failed: bool,
    strikes_remaining: int,
) -> None:
    """Emit when the dominance gate fires. ``max_share_after`` is the
    final max share after iterative drop-and-recompute; ``None`` if
    the gate hard-failed before any successful recompute. ``hard_failed``
    is true when the gate set confidence=0."""
    logger.warning(
        "[iv-threshold] dominance_gate ticker=%s ts=%d before=%.3f after=%s "
        "iter=%d hard_failed=%s strikes_remaining=%d",
        ticker,
        snapshot_ts_ms,
        max_share_before,
        f"{max_share_after:.3f}" if max_share_after is not None else "none",
        iterations,
        hard_failed,
        strikes_remaining,
        extra={
            "event": "iv_dominance_gate",
            "ticker": ticker,
            "snapshot_ts_ms": snapshot_ts_ms,
            "max_share_before": max_share_before,
            "max_share_after": max_share_after,
            "iterations": iterations,
            "hard_failed": hard_failed,
            "strikes_remaining": strikes_remaining,
        },
    )


def log_confidence_floor_fired(
    *,
    ticker: str | None,
    snapshot_ts_ms: int | None,
    confidence: float,
    floor: float,
    health_score: float | None = None,
    variance_contribution_synthetic: float | None = None,
) -> None:
    """Emit when computed confidence falls below the hard floor and the
    signal is being forced to 0. ``health_score`` and ``vcs`` are
    optional — supply them when the caller has the per-bar values handy
    (the regime-feature path does), omit when the caller only has the
    summary confidence (the realtime route)."""
    logger.info(
        "[iv-threshold] confidence_floor_fired ticker=%s ts=%s "
        "conf=%.3f floor=%.3f h=%s vcs=%s",
        ticker if ticker is not None else "?",
        snapshot_ts_ms if snapshot_ts_ms is not None else "?",
        confidence,
        floor,
        f"{health_score:.3f}" if health_score is not None else "?",
        f"{variance_contribution_synthetic:.3f}" if variance_contribution_synthetic is not None else "?",
        extra={
            "event": "confidence_floor_fired",
            "ticker": ticker,
            "snapshot_ts_ms": snapshot_ts_ms,
            "confidence": confidence,
            "floor": floor,
            "health_score": health_score,
            "variance_contribution_synthetic": variance_contribution_synthetic,
        },
    )


def log_imputed_prior_emitted(
    *,
    ticker: str | None,
    snapshot_ts_ms: int | None,
    shape: str,
    extra_context: dict[str, Any] | None = None,
) -> None:
    """Emit when a bar is processed with missing ``health_score``.
    ``shape`` is ``"missing_key"`` or ``"explicit_null"`` so the operator
    can distinguish recorder-side gaps (key absent in older payloads)
    from calculator-side failures (explicit null when the health
    computation raised)."""
    payload: dict[str, Any] = {
        "event": "imputed_prior_emitted",
        "ticker": ticker,
        "snapshot_ts_ms": snapshot_ts_ms,
        "shape": shape,
    }
    if extra_context:
        payload.update(extra_context)
    logger.info(
        "[iv-threshold] imputed_prior_emitted ticker=%s ts=%s shape=%s",
        ticker if ticker is not None else "?",
        snapshot_ts_ms if snapshot_ts_ms is not None else "?",
        shape,
        extra=payload,
    )
