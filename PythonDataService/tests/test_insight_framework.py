"""Tests for the Insight framework: Insight, InsightScore, InsightScorer, InsightManager.

Validates the LEAN-ported prediction-tracking system end-to-end:
  - InsightScore write-once semantics
  - Insight lifecycle (active/expired/serialization)
  - DefaultInsightScoreFunction direction + magnitude scoring
  - InsightManager add/step/summary pipeline
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.framework.insight import (
    Insight,
    InsightDirection,
    InsightScore,
    InsightScoreType,
    InsightType,
)
from app.engine.framework.insight_manager import InsightManager, InsightSummary
from app.engine.framework.insight_scorer import DefaultInsightScoreFunction


# ──────────────────────────────────────────────────────────────────
# InsightScore
# ──────────────────────────────────────────────────────────────────
class TestInsightScore:
    def test_initial_values(self):
        score = InsightScore()
        assert score.direction == 0.0
        assert score.magnitude == 0.0
        assert score.is_final_score is False

    def test_set_direction(self):
        score = InsightScore()
        score.set_score(InsightScoreType.DIRECTION, 1.0)
        assert score.direction == 1.0
        assert score.magnitude == 0.0

    def test_set_magnitude(self):
        score = InsightScore()
        score.set_score(InsightScoreType.MAGNITUDE, 0.85)
        assert score.magnitude == 0.85

    def test_clamp_above_one(self):
        score = InsightScore()
        score.set_score(InsightScoreType.DIRECTION, 1.5)
        assert score.direction == 1.0

    def test_clamp_below_zero(self):
        score = InsightScore()
        score.set_score(InsightScoreType.MAGNITUDE, -0.3)
        assert score.magnitude == 0.0

    def test_finalize_locks_scores(self):
        score = InsightScore()
        score.set_score(InsightScoreType.DIRECTION, 1.0)
        score.finalize(datetime(2024, 6, 1))
        assert score.is_final_score is True
        # Subsequent writes are silently ignored.
        score.set_score(InsightScoreType.DIRECTION, 0.0)
        assert score.direction == 1.0

    def test_get_score(self):
        score = InsightScore()
        score.set_score(InsightScoreType.DIRECTION, 0.7)
        score.set_score(InsightScoreType.MAGNITUDE, 0.4)
        assert score.get_score(InsightScoreType.DIRECTION) == 0.7
        assert score.get_score(InsightScoreType.MAGNITUDE) == 0.4

    def test_to_dict(self):
        score = InsightScore()
        score.set_score(InsightScoreType.DIRECTION, 1.0)
        score.finalize(datetime(2024, 6, 1))
        d = score.to_dict()
        assert d["direction"] == 1.0
        assert d["is_final"] is True


# ──────────────────────────────────────────────────────────────────
# Insight
# ──────────────────────────────────────────────────────────────────
class TestInsight:
    def test_price_factory(self):
        i = Insight.price(
            symbol="SPY",
            direction=InsightDirection.UP,
            period=timedelta(minutes=75),
            magnitude=0.001,
            confidence=0.72,
            source_model="TestModel",
        )
        assert i.symbol == "SPY"
        assert i.direction == InsightDirection.UP
        assert i.type == InsightType.PRICE
        assert i.magnitude == 0.001
        assert i.confidence == 0.72
        assert i.period == timedelta(minutes=75)

    def test_close_time_auto_computed(self):
        gen = datetime(2024, 6, 1, 10, 0)
        i = Insight.price(
            symbol="SPY",
            direction=InsightDirection.UP,
            period=timedelta(minutes=75),
            generated_time=gen,
        )
        assert i.close_time == gen + timedelta(minutes=75)

    def test_is_active_and_expired(self):
        gen = datetime(2024, 6, 1, 10, 0)
        i = Insight.price(
            symbol="SPY",
            direction=InsightDirection.UP,
            period=timedelta(minutes=30),
            generated_time=gen,
        )
        assert i.is_active(datetime(2024, 6, 1, 10, 15))
        assert not i.is_expired(datetime(2024, 6, 1, 10, 15))
        assert i.is_expired(datetime(2024, 6, 1, 10, 30))
        assert not i.is_active(datetime(2024, 6, 1, 10, 30))

    def test_group_assigns_shared_id(self):
        a = Insight.price("SPY", InsightDirection.UP, timedelta(minutes=15))
        b = Insight.price("QQQ", InsightDirection.DOWN, timedelta(minutes=15))
        grouped = Insight.group(a, b)
        assert len(grouped) == 2
        assert a.group_id == b.group_id
        assert a.group_id is not None

    def test_to_dict_serialization(self):
        gen = datetime(2024, 6, 1, 10, 0)
        i = Insight.price(
            symbol="SPY",
            direction=InsightDirection.UP,
            period=timedelta(minutes=75),
            generated_time=gen,
            magnitude=0.001,
            confidence=0.72,
        )
        i.reference_value = Decimal("520.50")
        i.reference_value_final = Decimal("521.00")
        d = i.to_dict()
        assert d["symbol"] == "SPY"
        assert d["direction"] == "up"
        assert d["type"] == "price"
        assert d["period_minutes"] == 75.0
        assert d["magnitude"] == 0.001
        assert d["confidence"] == 0.72
        assert d["reference_value"] == 520.50
        assert d["reference_value_final"] == 521.00
        assert "score" in d

    def test_symbol_uppercased(self):
        i = Insight.price("spy", InsightDirection.UP, timedelta(minutes=15))
        assert i.symbol == "SPY"


# ──────────────────────────────────────────────────────────────────
# DefaultInsightScoreFunction
# ──────────────────────────────────────────────────────────────────
class TestDefaultInsightScorer:
    def _make_scored_insight(
        self,
        direction: InsightDirection,
        ref_start: Decimal,
        ref_end: Decimal,
        magnitude: float | None = None,
    ) -> Insight:
        i = Insight.price(
            symbol="SPY",
            direction=direction,
            period=timedelta(minutes=75),
            magnitude=magnitude,
        )
        i.reference_value = ref_start
        i.reference_value_final = ref_end
        DefaultInsightScoreFunction().score(i)
        return i

    def test_up_correct(self):
        i = self._make_scored_insight(InsightDirection.UP, Decimal("500"), Decimal("502"))
        assert i.score.direction == 1.0

    def test_up_wrong(self):
        i = self._make_scored_insight(InsightDirection.UP, Decimal("500"), Decimal("498"))
        assert i.score.direction == 0.0

    def test_down_correct(self):
        i = self._make_scored_insight(InsightDirection.DOWN, Decimal("500"), Decimal("498"))
        assert i.score.direction == 1.0

    def test_down_wrong(self):
        i = self._make_scored_insight(InsightDirection.DOWN, Decimal("500"), Decimal("502"))
        assert i.score.direction == 0.0

    def test_flat_correct(self):
        # Price barely moved — within 0.1% threshold.
        i = self._make_scored_insight(InsightDirection.FLAT, Decimal("500"), Decimal("500.04"))
        assert i.score.direction == 1.0

    def test_flat_wrong(self):
        # Price moved too much for FLAT prediction.
        i = self._make_scored_insight(InsightDirection.FLAT, Decimal("500"), Decimal("505"))
        assert i.score.direction == 0.0

    def test_magnitude_scoring_perfect(self):
        # Predicted 0.4% move, actual was exactly 0.4%.
        i = self._make_scored_insight(
            InsightDirection.UP, Decimal("500"), Decimal("502"), magnitude=0.004
        )
        assert i.score.magnitude == pytest.approx(1.0, abs=0.01)

    def test_magnitude_scoring_partial(self):
        # Predicted 1% move, actual was 0.5%.
        i = self._make_scored_insight(
            InsightDirection.UP, Decimal("500"), Decimal("502.5"), magnitude=0.01
        )
        # score = 1 - |0.01 - 0.005| / max(0.01, 0.005) = 1 - 0.005/0.01 = 0.5
        assert i.score.magnitude == pytest.approx(0.5, abs=0.01)

    def test_no_magnitude_leaves_zero(self):
        i = self._make_scored_insight(InsightDirection.UP, Decimal("500"), Decimal("502"))
        assert i.score.magnitude == 0.0

    def test_zero_reference_value_skips_scoring(self):
        i = Insight.price("SPY", InsightDirection.UP, timedelta(minutes=75))
        i.reference_value = Decimal(0)
        i.reference_value_final = Decimal("502")
        DefaultInsightScoreFunction().score(i)
        # Scores should remain at defaults — no crash.
        assert i.score.direction == 0.0


# ──────────────────────────────────────────────────────────────────
# InsightManager
# ──────────────────────────────────────────────────────────────────
class TestInsightManager:
    def _make_manager_with_insights(self) -> tuple[InsightManager, list[Insight]]:
        """Create a manager with 3 insights: 2 correct UP, 1 wrong UP."""
        mgr = InsightManager()
        t0 = datetime(2024, 6, 1, 10, 0)

        # Insight 1: UP prediction, price goes up → correct
        i1 = Insight.price("SPY", InsightDirection.UP, timedelta(minutes=30),
                           generated_time=t0, magnitude=0.002, confidence=0.7)
        mgr.add(i1, Decimal("500"))

        # Insight 2: UP prediction, price goes down → wrong
        i2 = Insight.price("SPY", InsightDirection.UP, timedelta(minutes=30),
                           generated_time=t0 + timedelta(minutes=60), magnitude=0.003, confidence=0.6)
        mgr.add(i2, Decimal("505"))

        # Insight 3: UP prediction, price goes up → correct
        i3 = Insight.price("SPY", InsightDirection.UP, timedelta(minutes=30),
                           generated_time=t0 + timedelta(minutes=120), magnitude=0.001, confidence=0.8)
        mgr.add(i3, Decimal("503"))

        return mgr, [i1, i2, i3]

    def test_add_tracks_insights(self):
        mgr = InsightManager()
        i = Insight.price("SPY", InsightDirection.UP, timedelta(minutes=30))
        mgr.add(i, Decimal("500"))
        assert len(mgr.all_insights) == 1
        assert i.reference_value == Decimal("500")

    def test_step_scores_expired_insights(self):
        mgr = InsightManager()
        t0 = datetime(2024, 6, 1, 10, 0)
        i = Insight.price("SPY", InsightDirection.UP, timedelta(minutes=30),
                          generated_time=t0, confidence=0.7)
        mgr.add(i, Decimal("500"))

        # Before expiration — nothing scored.
        scored = mgr.step(t0 + timedelta(minutes=15), {"SPY": Decimal("502")})
        assert len(scored) == 0
        assert not i.score.is_final_score

        # After expiration — scored.
        scored = mgr.step(t0 + timedelta(minutes=30), {"SPY": Decimal("502")})
        assert len(scored) == 1
        assert i.score.is_final_score
        assert i.score.direction == 1.0  # UP was correct

    def test_step_does_not_rescore_finalized(self):
        mgr = InsightManager()
        t0 = datetime(2024, 6, 1, 10, 0)
        i = Insight.price("SPY", InsightDirection.UP, timedelta(minutes=30),
                          generated_time=t0)
        mgr.add(i, Decimal("500"))

        # Score once.
        mgr.step(t0 + timedelta(minutes=30), {"SPY": Decimal("502")})
        assert i.score.direction == 1.0

        # Step again with a different price — should NOT rescore.
        mgr.step(t0 + timedelta(minutes=60), {"SPY": Decimal("490")})
        assert i.score.direction == 1.0  # Still 1.0, not rescored

    def test_get_active_insights(self):
        mgr = InsightManager()
        t0 = datetime(2024, 6, 1, 10, 0)
        i1 = Insight.price("SPY", InsightDirection.UP, timedelta(minutes=30),
                           generated_time=t0)
        i2 = Insight.price("SPY", InsightDirection.UP, timedelta(minutes=60),
                           generated_time=t0)
        mgr.add(i1, Decimal("500"))
        mgr.add(i2, Decimal("500"))

        active = mgr.get_active_insights(t0 + timedelta(minutes=45))
        assert len(active) == 1  # Only i2 is still active
        assert active[0].id == i2.id

    def test_get_summary_direction_accuracy(self):
        mgr, insights = self._make_manager_with_insights()
        t_end = datetime(2024, 6, 1, 14, 0)

        # Score all: i1 up (correct), i2 up (wrong), i3 up (correct)
        mgr.step(t_end, {"SPY": Decimal("510")})  # All expired
        # i1: ref=500, final=510 → UP correct
        # i2: ref=505, final=510 → UP correct (price went up from 505→510)
        # i3: ref=503, final=510 → UP correct

        summary = mgr.get_summary()
        assert summary.total_insights == 3
        assert summary.scored_insights == 3
        # All 3 went up since the reference was below 510
        assert summary.direction_accuracy == pytest.approx(1.0)

    def test_get_summary_confidence_calibration(self):
        mgr, _ = self._make_manager_with_insights()
        t_end = datetime(2024, 6, 1, 14, 0)
        mgr.step(t_end, {"SPY": Decimal("510")})

        summary = mgr.get_summary()
        assert len(summary.confidence_calibration) > 0
        # All insights had confidence in [0.6, 0.8] range
        buckets = {b.bucket_low: b for b in summary.confidence_calibration}
        # 0.6 confidence → bucket 0.6-0.7
        assert 0.6 in buckets
        # 0.7 confidence → bucket 0.7-0.8
        assert 0.7 in buckets

    def test_get_summary_empty_when_no_insights(self):
        mgr = InsightManager()
        summary = mgr.get_summary()
        assert summary.total_insights == 0
        assert summary.scored_insights == 0
        assert summary.direction_accuracy == 0.0

    def test_summary_to_dict(self):
        mgr, _ = self._make_manager_with_insights()
        t_end = datetime(2024, 6, 1, 14, 0)
        mgr.step(t_end, {"SPY": Decimal("510")})
        d = mgr.get_summary().to_dict()
        assert "total_insights" in d
        assert "direction_accuracy" in d
        assert "confidence_calibration" in d
        assert "accuracy_by_hour" in d
        assert "magnitude_bias" in d
