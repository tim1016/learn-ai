"""Versioned run verdict schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Grade = Literal["A+", "A", "B", "C", "D", "F"]
Signal = Literal["Deploy", "Paper-trade", "Iterate", "Rework", "Reject"]
EngineKind = Literal["python", "lean"]


class RunVerdictSubScore(BaseModel):
    key: str
    label: str
    score: int | None
    raw_value: float | None = None
    display: str
    note: str


class RunVerdictDimension(BaseModel):
    key: str
    label: str
    weight: float
    score: int | None
    summary: str
    sub_scores: list[RunVerdictSubScore] = Field(default_factory=list)


class RunVerdictCleanliness(BaseModel):
    is_clean: bool
    is_reconciliation_grade: bool
    error_counts: dict[str, int] = Field(default_factory=dict)


class RunVerdict(BaseModel):
    verdict_version: int
    engine: EngineKind
    generated_at_ms: int
    composite: int | None
    grade: Grade | None
    signal: Signal | None
    headline: str
    red_flags: list[str] = Field(default_factory=list)
    dimensions: list[RunVerdictDimension] = Field(default_factory=list)
    missing_metrics: list[str] = Field(default_factory=list)
    normalized_weights: bool
    cleanliness: RunVerdictCleanliness | None = None


class RunVerdictInput(BaseModel):
    statistics: dict[str, Any] | None = None
    win_rate: float | None = None
    total_trades: int | None = None
    net_profit: float | None = None
    total_fees: float | None = None
    lean_statistics: dict[str, Any] | None = None

