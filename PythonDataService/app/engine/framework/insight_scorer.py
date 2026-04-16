"""Default Insight scoring function.

Scores insights based on direction accuracy and magnitude proximity once
their prediction period expires.

This is the lightweight equivalent of LEAN's IInsightScoreFunction
implementations. LEAN's scoring is pluggable — you can replace this with
a custom scorer by setting ``InsightManager.scorer``.

Scoring methodology:
  - Direction: 1.0 if price moved in the predicted direction, 0.0 if wrong.
    FLAT predictions score 1.0 if the actual move was less than 0.1%.
  - Magnitude: If the insight included a magnitude prediction, score is
    1.0 - |predicted - actual| / max(|predicted|, |actual|). If no
    magnitude was predicted, magnitude score stays at 0.0.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.engine.framework.insight import (
    Insight,
    InsightDirection,
    InsightScoreType,
)


class InsightScoreFunction(ABC):
    """Interface for insight scoring — matches LEAN's IInsightScoreFunction."""

    @abstractmethod
    def score(self, insight: Insight) -> None:
        """Compute and set scores on the insight. Called when the insight expires."""
        ...


class DefaultInsightScoreFunction(InsightScoreFunction):
    """Scores direction accuracy and magnitude proximity.

    Direction scoring:
      UP prediction + actual return > 0  → 1.0
      DOWN prediction + actual return < 0 → 1.0
      FLAT prediction + |actual return| < flat_threshold → 1.0
      Otherwise → 0.0

    Magnitude scoring (only when magnitude was predicted):
      score = 1.0 - |predicted_mag - actual_mag| / max(|predicted_mag|, |actual_mag|)
      Clamped to [0, 1].
    """

    def __init__(self, flat_threshold: float = 0.001) -> None:
        self._flat_threshold = flat_threshold

    def score(self, insight: Insight) -> None:
        if insight.reference_value == 0:
            return

        actual_return = float((insight.reference_value_final - insight.reference_value) / insight.reference_value)

        # ── Direction scoring ──
        if insight.direction == InsightDirection.UP:
            direction_correct = actual_return > 0
        elif insight.direction == InsightDirection.DOWN:
            direction_correct = actual_return < 0
        else:  # FLAT
            direction_correct = abs(actual_return) < self._flat_threshold

        insight.score.set_score(
            InsightScoreType.DIRECTION,
            1.0 if direction_correct else 0.0,
        )

        # ── Magnitude scoring ──
        if insight.magnitude is not None and insight.magnitude != 0:
            actual_mag = abs(actual_return)
            predicted_mag = abs(insight.magnitude)
            max_mag = max(actual_mag, predicted_mag)
            if max_mag > 1e-12:
                mag_score = 1.0 - abs(predicted_mag - actual_mag) / max_mag
            else:
                mag_score = 1.0  # Both near zero = perfect match
            insight.score.set_score(InsightScoreType.MAGNITUDE, mag_score)
