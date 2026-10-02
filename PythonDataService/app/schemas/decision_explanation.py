"""The durable, JSON-safe form of one decision's explanation (#2639).

``app.engine.strategy.decision_explanation`` is what a strategy emits, in
``Decimal``. This is what a decision receipt and a before-start evaluation
store, and what the strategy view reads back: the decision bar itself, the
bot's own indicator values and each rule's pass/fail, as plain numbers.

Values are converted to ``float`` once, here. They are display and gate
inputs, never compared for a decision: every pass/fail was settled by the
strategy in ``Decimal`` before this record existed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.decision_explanation import DecisionExplanation, ExplainedCheck
from app.engine.strategy.signal_program import SignalDecision
from app.utils.session_anchors import MAX_TIMESTAMP_MS

DecisionSignal = Literal["ENTER", "EXIT", "HOLD"]


class DecisionBarRecord(BaseModel):
    """The consolidated decision bar, labelled by its close."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    open: float
    high: float
    low: float
    close: float
    volume: float = Field(ge=0)


class ExplainedCheckRecord(BaseModel):
    """One rule as the bot applied it: threshold, observed value, pass/fail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str = Field(min_length=1)
    role: Literal["entry", "exit"]
    passed: bool
    # A number the rule compared, or a short state token ("crossed_up").
    observed: int | float | str | None
    # A bound, an inclusive [low, high] band, or None for a state rule.
    threshold: int | float | list[float] | None = None


class DecisionExplanationRecord(BaseModel):
    """What one decision bar saw, as the bot computed it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bar: DecisionBarRecord
    ready: bool
    holding: bool
    signal: DecisionSignal
    values: dict[str, float | None]
    checks: list[ExplainedCheckRecord] = Field(default_factory=list)


def _number(value: Decimal | int | None) -> int | float | None:
    if value is None or isinstance(value, int):
        return value
    return float(value)


def _check_record(check: ExplainedCheck) -> ExplainedCheckRecord:
    threshold = check.threshold
    return ExplainedCheckRecord(
        check_id=check.check_id,
        role=check.role.value,
        passed=check.passed,
        observed=check.observed if isinstance(check.observed, str) else _number(check.observed),
        threshold=([float(threshold[0]), float(threshold[1])] if isinstance(threshold, tuple) else _number(threshold)),
    )


def explanation_record(bar: TradeBar, decision: SignalDecision) -> DecisionExplanationRecord | None:
    """Freeze one staged decision's explanation, or ``None`` when it reported none."""
    explanation: DecisionExplanation | None = decision.explanation
    if explanation is None:
        return None
    return DecisionExplanationRecord(
        bar=DecisionBarRecord(
            start_ms=bar.start_ms,
            end_ms=bar.end_ms,
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume),
        ),
        ready=decision.ready,
        holding=explanation.holding,
        signal=decision.signal_facts["decision"],  # type: ignore[arg-type]
        values={key: _number(value) for key, value in explanation.values.items()},  # type: ignore[misc]
        checks=[_check_record(check) for check in explanation.checks],
    )


__all__ = [
    "DecisionBarRecord",
    "DecisionExplanationRecord",
    "DecisionSignal",
    "ExplainedCheckRecord",
    "explanation_record",
]
