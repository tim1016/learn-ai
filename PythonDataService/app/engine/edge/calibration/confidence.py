"""Confidence-shape calibration harness.

The production confidence path is the multiplicative product
``health * (1 - vcs)`` clamped to a hard floor (see
``app.engine.edge.confidence``). This module is the **offline**
companion that, given a labelled history of forward signals, tries
alternate shapes and ranks them by signal quality.

Trigger condition: at least ~30 forward sessions of recorder data with
realized PnL labels available (research-doc §4.5 step 2). Until then,
the harness is stubbed — calling ``evaluate_confidence_shape`` with no
data raises so misuse can't masquerade as a calibrated result.

Why a separate module:
- Production runtime should not import scikit-learn-style fitters.
- Calibration runs are notebook-style — load history, sweep shapes,
  pick winner, write the chosen parameters back into ``confidence.py``.
  The fitted shape is checked in as code, not loaded from a model file
  at runtime (no production drift between calibration runs).

Why explicit shape families:
- Identity ``f(c) = c`` is the current production path (control).
- Power ``f(c) = c**p`` for ``p ∈ [0.5, 1, 2]`` — sub-1 boosts
  borderline confidences; >1 punishes them.
- Logistic ``f(c) = 1/(1 + exp(-a*(c - b)))`` — smooth S-curve;
  threshold near ``b`` with steepness ``a``.

Reviewer's remark stands: confidence is a *multiplier on a z-score*,
not a probability, so reliability diagrams (which calibrate predicted
probabilities against empirical hit rates) are the wrong primitive.
We rank shapes by signal-quality metrics on the scaled z-score:
information coefficient (Spearman corr of ``z_scaled`` vs forward
return) and the realized Sharpe of the gated trades.

This module is **not** wired into any router or scheduled job. It is
invoked by hand from a notebook or script when the labelled history
exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

ShapeFamily = Literal["identity", "power", "logistic"]


@dataclass(frozen=True)
class SignalRecord:
    """One labelled signal observation. Populated from the recorder
    once forward returns are available (post-30-session burn-in)."""

    ts_ms: int
    ticker: str
    health_score: float
    variance_contribution_synthetic: float
    confidence_raw: float
    z_raw: float
    forward_return: float


@dataclass(frozen=True)
class ShapeFitResult:
    """Result of fitting one shape family. ``params`` holds the fitted
    coefficients (e.g. ``{"p": 0.7}`` for power, ``{"a": 8.0, "b": 0.4}``
    for logistic). ``metrics`` holds the ranking metrics."""

    family: ShapeFamily
    params: dict[str, float]
    metrics: dict[str, float]


def _identity(c: float) -> float:
    return c


def _power(c: float, p: float) -> float:
    return c**p if c > 0 else 0.0


def _logistic(c: float, a: float, b: float) -> float:
    import math

    return 1.0 / (1.0 + math.exp(-a * (c - b)))


SHAPE_FAMILIES: dict[ShapeFamily, Callable[..., float]] = {
    "identity": _identity,
    "power": _power,
    "logistic": _logistic,
}


def evaluate_confidence_shape(
    *,
    family: ShapeFamily,
    params: dict[str, float],
    log: list[SignalRecord],
) -> dict[str, float]:
    """Evaluate one shape on a labelled signal log.

    Returns ``{"ic": ..., "sharpe": ..., "n_trades": ..., "hit_rate": ...}``.

    Raises ``NotImplementedError`` until the labelled-signal pipeline
    exists. The signature is locked so the call site doesn't shift
    when the body lands; ``evaluate_confidence_shape(family=...,
    params={"p": 0.7}, log=records)`` is the same shape today and after
    the harness is implemented.
    """
    if not log:
        raise NotImplementedError(
            "Confidence-shape calibration requires labelled signal "
            "history. Trigger this after ~30 forward sessions of "
            "recorder data with realized forward returns. See "
            "docs/architecture/iv-research-chat-notes.md §4.5."
        )
    raise NotImplementedError(
        "Calibration body is intentionally unimplemented until labelled "
        "signal history is available. The shape registry and signature "
        "are pinned; fill in IC + Sharpe + hit-rate when the recorder "
        "has 30+ sessions of forward outcomes."
    )


def fit_shape_family(
    *,
    family: ShapeFamily,
    log: list[SignalRecord],
) -> ShapeFitResult:
    """Sweep parameters within a shape family and return the best fit.

    Raises ``NotImplementedError`` until the labelled-signal pipeline
    exists — see ``evaluate_confidence_shape``.
    """
    if not log:
        raise NotImplementedError(
            "fit_shape_family requires labelled signal history. "
            "See evaluate_confidence_shape for the trigger condition."
        )
    raise NotImplementedError(
        "Fit body intentionally unimplemented; the family enum and "
        "result dataclass are pinned. Implement when labelled history "
        "exists."
    )
