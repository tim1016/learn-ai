"""InsightManager — tracks insights and scores them when they expire.

Ported from Lean/Common/Algorithm/Framework/Alphas/Analysis/InsightManager.cs.

The InsightManager is the central hub for insight lifecycle management:
  1. Strategies emit insights via ``add()``
  2. Each engine time step calls ``step()`` which scores expired insights
  3. After the backtest, ``get_summary()`` returns aggregate analytics

The manager is attached to ``StrategyContext`` so strategies can emit
insights through ``ctx.emit_insight()``.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.engine.framework.insight import Insight, InsightDirection
from app.engine.framework.insight_scorer import (
    DefaultInsightScoreFunction,
    InsightScoreFunction,
)


@dataclass
class ConfidenceBucket:
    """A single bucket in the confidence calibration analysis."""

    bucket_low: float
    bucket_high: float
    count: int = 0
    correct: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.count if self.count > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "bucket": f"{self.bucket_low:.1f}-{self.bucket_high:.1f}",
            "count": self.count,
            "correct": self.correct,
            "accuracy": round(self.accuracy, 4),
        }


@dataclass
class InsightSummary:
    """Aggregate analytics computed from all scored insights."""

    total_insights: int = 0
    scored_insights: int = 0
    direction_accuracy: float = 0.0
    avg_magnitude_score: float = 0.0
    avg_confidence_emitted: float = 0.0
    confidence_calibration: list[ConfidenceBucket] = field(default_factory=list)
    accuracy_by_hour: dict[int, dict] = field(default_factory=dict)
    accuracy_by_quarter: dict[str, dict] = field(default_factory=dict)
    magnitude_bias: float = 0.0  # avg(predicted - actual), positive = overestimate

    def to_dict(self) -> dict:
        return {
            "total_insights": self.total_insights,
            "scored_insights": self.scored_insights,
            "direction_accuracy": round(self.direction_accuracy, 4),
            "avg_magnitude_score": round(self.avg_magnitude_score, 4),
            "avg_confidence_emitted": round(self.avg_confidence_emitted, 4),
            "confidence_calibration": [b.to_dict() for b in self.confidence_calibration],
            "accuracy_by_hour": self.accuracy_by_hour,
            "accuracy_by_quarter": self.accuracy_by_quarter,
            "magnitude_bias": round(self.magnitude_bias, 6),
        }


class InsightManager:
    """Tracks all insights emitted during a backtest and scores them.

    Ported from LEAN's InsightManager. Simplified for our engine:
      - Single-threaded (no concurrent access concerns)
      - Dictionary keyed by symbol → list of insights
      - Pluggable scorer (defaults to DefaultInsightScoreFunction)
    """

    def __init__(
        self,
        scorer: InsightScoreFunction | None = None,
    ) -> None:
        self._insights_by_symbol: dict[str, list[Insight]] = defaultdict(list)
        self._all_insights: list[Insight] = []
        self._scorer = scorer or DefaultInsightScoreFunction()

    @property
    def all_insights(self) -> list[Insight]:
        return list(self._all_insights)

    def add(self, insight: Insight, current_price: Decimal) -> None:
        """Register a new insight and set its reference price.

        Called by ``StrategyContext.emit_insight()``.
        """
        insight.reference_value = current_price
        self._insights_by_symbol[insight.symbol].append(insight)
        self._all_insights.append(insight)

    def step(
        self,
        utc_time: datetime,
        current_prices: dict[str, Decimal],
    ) -> list[Insight]:
        """Process a time step — score any insights that have expired.

        Called by BacktestEngine on every minute bar. Returns the list
        of insights that were newly scored (finalized) in this step.
        """
        newly_scored: list[Insight] = []
        for symbol, insights in self._insights_by_symbol.items():
            price = current_prices.get(symbol, Decimal(0))
            for insight in insights:
                if insight.is_expired(utc_time) and not insight.score.is_final_score:
                    insight.reference_value_final = price
                    self._scorer.score(insight)
                    insight.score.finalize(utc_time)
                    newly_scored.append(insight)
        return newly_scored

    def get_active_insights(
        self, utc_time: datetime, symbol: str | None = None,
    ) -> list[Insight]:
        """Return insights whose prediction period has not yet elapsed."""
        if symbol:
            return [
                i for i in self._insights_by_symbol.get(symbol, [])
                if i.is_active(utc_time)
            ]
        return [
            i for ilist in self._insights_by_symbol.values()
            for i in ilist
            if i.is_active(utc_time)
        ]

    def get_scored_insights(self) -> list[Insight]:
        """Return all insights that have been scored (finalized)."""
        return [i for i in self._all_insights if i.score.is_final_score]

    def get_summary(self) -> InsightSummary:
        """Compute aggregate analytics from all scored insights.

        This is the main analytical output — it powers the Insight Analysis
        panel in the Engine Lab UI.
        """
        summary = InsightSummary(total_insights=len(self._all_insights))
        scored = self.get_scored_insights()
        summary.scored_insights = len(scored)

        if not scored:
            return summary

        # ── Direction accuracy ──
        direction_correct = sum(1 for i in scored if i.score.direction > 0.5)
        summary.direction_accuracy = direction_correct / len(scored)

        # ── Average magnitude score ──
        mag_scores = [i.score.magnitude for i in scored if i.magnitude is not None]
        summary.avg_magnitude_score = (
            sum(mag_scores) / len(mag_scores) if mag_scores else 0.0
        )

        # ── Average confidence emitted ──
        confidences = [i.confidence for i in scored if i.confidence is not None]
        summary.avg_confidence_emitted = (
            sum(confidences) / len(confidences) if confidences else 0.0
        )

        # ── Confidence calibration (buckets of 0.1) ──
        buckets: list[ConfidenceBucket] = [
            ConfidenceBucket(bucket_low=i / 10, bucket_high=(i + 1) / 10)
            for i in range(10)
        ]
        for insight in scored:
            if insight.confidence is not None:
                bucket_idx = min(int(insight.confidence * 10), 9)
                buckets[bucket_idx].count += 1
                if insight.score.direction > 0.5:
                    buckets[bucket_idx].correct += 1
        # Only include buckets that have data
        summary.confidence_calibration = [b for b in buckets if b.count > 0]

        # ── Accuracy by hour of day ──
        hour_stats: dict[int, dict] = {}
        for insight in scored:
            hour = insight.generated_time.hour
            if hour not in hour_stats:
                hour_stats[hour] = {"count": 0, "correct": 0}
            hour_stats[hour]["count"] += 1
            if insight.score.direction > 0.5:
                hour_stats[hour]["correct"] += 1
        for hour, stats in hour_stats.items():
            stats["accuracy"] = round(
                stats["correct"] / stats["count"] if stats["count"] > 0 else 0.0, 4
            )
        summary.accuracy_by_hour = dict(sorted(hour_stats.items()))

        # ── Accuracy by quarter ──
        quarter_stats: dict[str, dict] = {}
        for insight in scored:
            q = f"{insight.generated_time.year}-Q{(insight.generated_time.month - 1) // 3 + 1}"
            if q not in quarter_stats:
                quarter_stats[q] = {"count": 0, "correct": 0}
            quarter_stats[q]["count"] += 1
            if insight.score.direction > 0.5:
                quarter_stats[q]["correct"] += 1
        for q, stats in quarter_stats.items():
            stats["accuracy"] = round(
                stats["correct"] / stats["count"] if stats["count"] > 0 else 0.0, 4
            )
        summary.accuracy_by_quarter = dict(sorted(quarter_stats.items()))

        # ── Magnitude bias (avg predicted - actual) ──
        biases: list[float] = []
        for insight in scored:
            if insight.magnitude is not None and insight.reference_value != 0:
                actual_return = float(
                    (insight.reference_value_final - insight.reference_value)
                    / insight.reference_value
                )
                # Compare predicted direction-adjusted magnitude vs actual
                predicted_signed = (
                    insight.magnitude * insight.direction.value
                    if insight.direction != InsightDirection.FLAT
                    else insight.magnitude
                )
                biases.append(predicted_signed - actual_return)
        summary.magnitude_bias = sum(biases) / len(biases) if biases else 0.0

        return summary
