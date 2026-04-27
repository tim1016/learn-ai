"""Continuous confidence formula shared by VRP gating and regime weighting.

Steps E and F of the IV-ownership plan
(``docs/architecture/iv-ownership-plan.md``).

Single source of truth for "how trustworthy is this IV30, on a 0..1 scale,
given (a) chain stability and (b) how much of the chain is synthesized."
Both the VRP signal generator (Step E) and the regime classifier
(Step F) call this module so the two production gates can never drift
against each other.

The confidence is multiplicative:

    confidence = health_score * (1 - variance_contribution_synthetic)

Multiplicative because stability and trust-in-inputs are roughly
independent failure modes — a healthy chain with all synthetic data is
still untrustworthy; a real OPRA chain that's wildly unstable across
refits is also untrustworthy. Both must be high for confidence to be
high. Additive doesn't capture this.

Step F's regime feature weight uses an extra ramp:

    feature_weight = max(0, 2 * health_score - 1) * (1 - vcs)

The ramp-from-0.5 means a chain at health=0.5 carries no IV-feature
weight in the regime classifier, while VRP gating still admits some
signal. This matches the spirit of the existing IV30 stability test
threshold (``health < 0.5`` flagged as "degrade gracefully").
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CONFIDENCE_FLOOR = 0.1
"""Hard-gate floor: confidence below this forces signal action to 0
regardless of z-magnitude (Step E acceptance §5.E). Configurable per route
via Pydantic settings — see ``docs/architecture/iv-ownership-decisions.md``
Q4."""


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


def regime_feature_weight(
    *,
    health_score: float,
    variance_contribution_synthetic: float,
) -> float:
    """Step F — weight applied to IV-derived features in the regime classifier.

    ``max(0, 2 * health - 1)`` ramps from 0 at ``health = 0.5`` to 1 at
    ``health = 1.0``: chains rated "uncertain" (around the existing 0.5
    flag threshold) drop out of regime feature contribution entirely,
    while clean chains contribute at full weight. The synthetic-share
    penalty applies linearly on top.
    """
    h = max(0.0, min(1.0, float(health_score)))
    s = max(0.0, min(1.0, float(variance_contribution_synthetic)))
    return max(0.0, 2.0 * h - 1.0) * (1.0 - s)
