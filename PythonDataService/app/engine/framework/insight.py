"""Insight — a structured prediction emitted by an Alpha Model.

Ported from Lean/Common/Algorithm/Framework/Alphas/Insight.cs and
Lean/Common/Algorithm/Framework/Alphas/InsightScore.cs.

An Insight captures: "I predict the price of SYMBOL will go UP/DOWN/FLAT by
MAGNITUDE over PERIOD with CONFIDENCE." After the period expires, the
InsightManager scores it against what actually happened.

Key design decisions vs LEAN's C# implementation:
  - We use Python dataclasses instead of C# properties + JSON serialization.
  - We keep Decimal for reference_value / reference_value_final (matching our
    engine's Decimal-everywhere policy for price precision).
  - We drop .NET-specific serialization machinery, IPeriodSpecification
    hierarchy, and SecurityExchangeHours-aware close time computation.
    Our engine operates on fixed-period bars so timedelta is sufficient.
  - InsightScore uses a write-once pattern: scores can be updated until
    finalized, then become immutable (matching LEAN's behavior).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import uuid4


class InsightType(Enum):
    """What the insight is predicting.

    Maps to LEAN's InsightType enum.
    """

    PRICE = 0
    VOLATILITY = 1


class InsightDirection(Enum):
    """Predicted direction of movement.

    Maps to LEAN's InsightDirection enum. Integer values match LEAN
    so they can be used directly in scoring math.
    """

    DOWN = -1
    FLAT = 0
    UP = 1


class InsightScoreType(Enum):
    """Which dimension of the score to read/write.

    Maps to LEAN's InsightScoreType enum.
    """

    DIRECTION = 0
    MAGNITUDE = 1


@dataclass
class InsightScore:
    """Scoring container for an Insight's prediction accuracy.

    Ported from Lean/Common/Algorithm/Framework/Alphas/InsightScore.cs.

    Scores are clamped to [0, 1]. Once finalized (``is_final_score=True``),
    updates are silently ignored — matching LEAN's write-once pattern.
    """

    direction: float = 0.0
    magnitude: float = 0.0
    is_final_score: bool = False
    updated_time_utc: datetime | None = None

    def set_score(self, score_type: InsightScoreType, value: float) -> None:
        """Update a score dimension. No-op if already finalized."""
        if self.is_final_score:
            return
        clamped = max(0.0, min(1.0, value))
        if score_type == InsightScoreType.DIRECTION:
            self.direction = clamped
        elif score_type == InsightScoreType.MAGNITUDE:
            self.magnitude = clamped
        self.updated_time_utc = datetime.utcnow()

    def get_score(self, score_type: InsightScoreType) -> float:
        """Read a score dimension."""
        if score_type == InsightScoreType.DIRECTION:
            return self.direction
        return self.magnitude

    def finalize(self, time: datetime) -> None:
        """Lock the score — no further updates allowed."""
        self.is_final_score = True
        self.updated_time_utc = time

    def to_dict(self) -> dict:
        return {
            "direction": round(self.direction, 4),
            "magnitude": round(self.magnitude, 4),
            "is_final": self.is_final_score,
        }


@dataclass
class Insight:
    """A structured prediction — the core of LEAN's Alpha framework.

    Ported from Lean/Common/Algorithm/Framework/Alphas/Insight.cs.

    Every field maps to the LEAN class. We drop .NET serialization machinery
    and keep only the data + behavior our engine needs.

    Usage::

        insight = Insight.price(
            symbol="SPY",
            direction=InsightDirection.UP,
            period=timedelta(minutes=75),
            magnitude=0.0005,
            confidence=0.72,
            source_model="EmaCross_5_10_RSI14",
        )
    """

    # Identity
    id: str = field(default_factory=lambda: str(uuid4()))
    group_id: str | None = None
    source_model: str = ""
    tag: str = ""

    # What we're predicting
    symbol: str = ""
    type: InsightType = InsightType.PRICE
    direction: InsightDirection = InsightDirection.FLAT

    # Prediction parameters
    period: timedelta = field(default_factory=lambda: timedelta(minutes=75))
    magnitude: float | None = None
    confidence: float | None = None
    weight: float | None = None

    # Timing
    generated_time: datetime = field(default_factory=lambda: datetime(2000, 1, 1))
    close_time: datetime = field(default_factory=lambda: datetime(2000, 1, 1))

    # Reference values for scoring (Decimal for price precision)
    reference_value: Decimal = Decimal(0)
    reference_value_final: Decimal = Decimal(0)

    # Score
    score: InsightScore = field(default_factory=InsightScore)

    def __post_init__(self) -> None:
        # Auto-compute close_time if it wasn't explicitly set
        if self.close_time <= self.generated_time:
            self.close_time = self.generated_time + self.period

    def is_active(self, utc_time: datetime) -> bool:
        """True if the prediction period has not yet elapsed."""
        return utc_time < self.close_time

    def is_expired(self, utc_time: datetime) -> bool:
        """True if the prediction period has elapsed."""
        return utc_time >= self.close_time

    @staticmethod
    def price(
        symbol: str,
        direction: InsightDirection,
        period: timedelta,
        generated_time: datetime | None = None,
        magnitude: float | None = None,
        confidence: float | None = None,
        weight: float | None = None,
        source_model: str = "",
        tag: str = "",
    ) -> Insight:
        """Factory method for price-type insights.

        Matches LEAN's ``Insight.Price(...)`` factory methods.
        """
        gen_time = generated_time or datetime(2000, 1, 1)
        return Insight(
            symbol=symbol.upper(),
            type=InsightType.PRICE,
            direction=direction,
            period=period,
            generated_time=gen_time,
            close_time=gen_time + period,
            magnitude=magnitude,
            confidence=confidence,
            weight=weight,
            source_model=source_model,
            tag=tag,
        )

    @staticmethod
    def group(*insights: Insight) -> list[Insight]:
        """Assign a shared group_id to a set of related insights.

        Matches LEAN's ``Insight.Group(params Insight[])``.
        """
        group_id = str(uuid4())
        for insight in insights:
            insight.group_id = group_id
        return list(insights)

    def to_dict(self) -> dict:
        """Serialize for API responses."""
        return {
            "id": self.id,
            "group_id": self.group_id,
            "symbol": self.symbol,
            "type": self.type.name.lower(),
            "direction": self.direction.name.lower(),
            "period_minutes": self.period.total_seconds() / 60,
            "magnitude": self.magnitude,
            "confidence": self.confidence,
            "weight": self.weight,
            "source_model": self.source_model,
            "tag": self.tag,
            "generated_time": self.generated_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "reference_value": float(self.reference_value),
            "reference_value_final": float(self.reference_value_final),
            "score": self.score.to_dict(),
        }

    def __repr__(self) -> str:
        return (
            f"Insight({self.symbol} {self.direction.name} "
            f"mag={self.magnitude} conf={self.confidence} "
            f"period={self.period} score_dir={self.score.direction:.2f})"
        )
