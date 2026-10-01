"""Continuous confidence formula for VRP gating.

Formula: confidence = health_score * (1 - variance_contribution_synthetic), clamped to hard floor
Reference: Internal — no external reference.
Canonical implementation: app/engine/edge/confidence.py
Validated against: NONE — pending

See ADR 0071 decisions 10–11 for the hard floor and the policy for a missing
``health_score``.

Single source of truth for "how trustworthy is this IV30, on a 0..1 scale,
given (a) chain stability and (b) how much of the chain is synthesized."

The confidence is multiplicative:

    confidence = health_score * (1 - variance_contribution_synthetic)

Multiplicative because stability and trust-in-inputs are roughly independent
failure modes — both must be high for confidence to be high. Additive
doesn't capture this.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CONFIDENCE_FLOOR = 0.1
"""Hard-gate floor: confidence below this forces signal action to 0
regardless of z-magnitude. Configurable per route via Pydantic settings.
See ADR 0071 decision 10 for the rationale
and the planned reliability-curve calibration."""


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """Decision-explanation payload for the gating logic.

    The UI banner reads ``reason`` and shows the dominant cause. If
    ``confidence >= floor``, ``reason`` is ``None``.
    """

    confidence: float
    health_score: float
    variance_contribution_synthetic: float
    reason: str | None


def compute_confidence(
    *,
    health_score: float,
    variance_contribution_synthetic: float,
) -> float:
    """Continuous confidence ∈ [0, 1].

    Both inputs are clamped to [0, 1] for safety against floating-point
    drift; out-of-range inputs would otherwise produce confidence values
    outside [0, 1] and break downstream rescaling.
    """
    h = max(0.0, min(1.0, float(health_score)))
    s = max(0.0, min(1.0, float(variance_contribution_synthetic)))
    return h * (1.0 - s)


def confidence_with_explanation(
    *,
    health_score: float,
    variance_contribution_synthetic: float,
    floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> ConfidenceBreakdown:
    """Confidence + an explanation string when below floor.

    The explanation names the dominant component so a UI banner doesn't
    have to second-guess. When both components are bad, the synthetic
    share wins because it's the harder-to-fix problem (chain quality
    fluctuates day-to-day; synthesis is structural).
    """
    confidence = compute_confidence(
        health_score=health_score,
        variance_contribution_synthetic=variance_contribution_synthetic,
    )
    reason = None
    if confidence < floor:
        if variance_contribution_synthetic >= 0.5:
            reason = "synthetic-heavy chain (variance_contribution_synthetic ≥ 0.5)"
        elif health_score < 0.5:
            reason = "unstable IV30 (health_score < 0.5)"
        else:
            reason = (
                f"confidence below floor ({confidence:.3f} < {floor:.3f})"
            )
    return ConfidenceBreakdown(
        confidence=confidence,
        health_score=health_score,
        variance_contribution_synthetic=variance_contribution_synthetic,
        reason=reason,
    )
