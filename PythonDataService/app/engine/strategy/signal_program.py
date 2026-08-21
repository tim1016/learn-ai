"""Staged, broker-neutral signal programs for Engine Lab strategies.

Formula: Evaluation identity = SHA-256(canonical JSON of program version,
settings, and bar-close clock); trace root = SHA-256(canonical JSON trace list).
Reference: Issue #1725 and the EMA LEAN reconciliation receipt in
  docs/references/reconciliations/ema-crossover-signal-lean-2026-07-18.md.
Canonical implementation: this file; it stages existing strategy decisions and
  deliberately does not reimplement EMA, RSI, fills, or custody.
Validated against: tests/engine/strategy/test_ema_signal_program.py.

The session is deliberately below an Action Plan or broker boundary.  It owns
only the decision-cycle transaction: a consolidated bar is advanced once,
produces one auditable stage, and is explicitly committed or discarded before
the next decision clock can advance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.signal_intent_executor import (
    SignalIntentExecutionContext,
)
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind


class Settlement(StrEnum):
    """The only legal outcomes for a staged signal cycle."""

    COMMIT = "COMMIT"
    DISCARD = "DISCARD"


class StageStatus(StrEnum):
    """Whether advancing a session made a stage or was quarantined."""

    STAGED = "STAGED"
    UNSETTLED_STAGE = "UNSETTLED_STAGE"
    REJECTED_BAR = "REJECTED_BAR"


@dataclass(frozen=True)
class EvaluationTrace:
    """The public, semantic record of one signal-program evaluation."""

    program_key: str
    program_version: str
    evaluation_id: str
    bar_close_ms: int
    bar_qualified: bool
    bucket_closed: bool
    ready: bool
    relation_facts: dict[str, bool]
    signal_facts: dict[str, str]
    staged_candidate: str | None
    reason_evidence: dict[str, str | int | bool]
    action_plan_request: dict[str, str] | None

    def semantic_payload(self) -> dict[str, Any]:
        """Return only fields that define the observable decision meaning."""
        return asdict(self)


@dataclass
class EvaluationStage:
    """A decision cycle awaiting its single settlement action."""

    trace: EvaluationTrace
    intents: tuple[SignalIntent, ...]
    status: StageStatus = StageStatus.STAGED
    settlement: Settlement | None = None


@dataclass(frozen=True)
class StageQuarantine:
    """A fail-closed result which never invokes strategy or execution code."""

    status: StageStatus
    reason: str


class _EmaSignalStrategy(Protocol):
    """Minimal legacy-strategy surface adapted into the staged program."""

    ctx: Any
    _in_position: bool
    _bars_until_exit: int
    _ema5: Any
    _ema10: Any
    _rsi14: Any

    def _on_fifteen_minute_bar(self, bar: TradeBar) -> None: ...

    def rollback_blocked_entry(self) -> None: ...

    def rollback_blocked_exit(self) -> None: ...

    def signal_program_settings(self) -> dict[str, str]: ...


class _CaptureExecutor:
    """Capture an intent until the session's settlement commits it."""

    def __init__(self) -> None:
        self.intents: list[SignalIntent] = []

    def execute(self, _context: SignalIntentExecutionContext, intent: SignalIntent) -> None:
        self.intents.append(intent)


class EmaCrossoverSignalSession:
    """Transactional adapter around the already canonical EMA decision math."""

    PROGRAM_KEY = "ema_crossover_signal"
    PROGRAM_VERSION = "ema-crossover-signal/v1"
    TIMEFRAME_MS = 15 * 60 * 1000

    def __init__(self, strategy: _EmaSignalStrategy) -> None:
        self._strategy = strategy
        self._active_stage: EvaluationStage | None = None
        self._last_bar_close_ms: int | None = None
        self.traces: list[EvaluationTrace] = []

    @property
    def active_stage(self) -> EvaluationStage | None:
        return self._active_stage

    def advance(self, bar: TradeBar) -> EvaluationStage | StageQuarantine:
        """Evaluate one complete 15-minute bucket without applying its intent."""
        if self._active_stage is not None:
            return StageQuarantine(StageStatus.UNSETTLED_STAGE, "UNSETTLED_STAGE")
        if bar.end_ms - bar.start_ms != self.TIMEFRAME_MS:
            return StageQuarantine(StageStatus.REJECTED_BAR, "TIMEFRAME_MISMATCH")
        if self._last_bar_close_ms is not None and bar.end_ms <= self._last_bar_close_ms:
            return StageQuarantine(StageStatus.REJECTED_BAR, "NON_MONOTONIC_DECISION_CLOCK")
        if self._strategy.ctx is None:
            raise RuntimeError("SignalSession requires an initialized strategy context")

        context = self._strategy.ctx
        prior_executor = context._signal_intent_executor
        if prior_executor is None:
            raise RuntimeError("SignalSession requires a bound SignalIntentExecutor")
        capture = _CaptureExecutor()
        context.set_signal_intent_executor(capture)
        prior_in_position = self._strategy._in_position
        prior_countdown = self._strategy._bars_until_exit
        try:
            self._strategy._on_fifteen_minute_bar(bar)
        finally:
            context.set_signal_intent_executor(prior_executor)

        intents = tuple(capture.intents)
        if len(intents) > 1:
            raise RuntimeError("EMA SignalSession may stage at most one intent per decision clock")
        trace = self._build_trace(
            bar=bar,
            intent=intents[0] if intents else None,
            prior_in_position=prior_in_position,
            prior_countdown=prior_countdown,
        )
        stage = EvaluationStage(trace=trace, intents=intents)
        self._active_stage = stage
        self._last_bar_close_ms = bar.end_ms
        return stage

    def settle(self, settlement: Settlement) -> EvaluationStage:
        """Commit the staged semantic action or restore the retryable EMA state."""
        stage = self._active_stage
        if stage is None:
            raise RuntimeError("SignalSession has no staged evaluation to settle")
        if settlement is Settlement.COMMIT:
            context = self._strategy.ctx
            assert context is not None
            executor = context._signal_intent_executor
            assert executor is not None
            for intent in stage.intents:
                executor.execute(context, intent)
        else:
            for intent in stage.intents:
                if intent.kind is SignalIntentKind.ENTER:
                    self._strategy.rollback_blocked_entry()
                elif intent.kind is SignalIntentKind.EXIT:
                    self._strategy.rollback_blocked_exit()
                else:  # pragma: no cover - closed enum guard
                    raise ValueError(f"unsupported signal intent kind: {intent.kind!r}")

        stage.settlement = settlement
        self.traces.append(stage.trace)
        self._active_stage = None
        return stage

    def commit_if_staged(self) -> None:
        """Backtest compatibility adapter: apply each staged result immediately."""
        if self._active_stage is not None:
            self.settle(Settlement.COMMIT)

    def _build_trace(
        self,
        *,
        bar: TradeBar,
        intent: SignalIntent | None,
        prior_in_position: bool,
        prior_countdown: int,
    ) -> EvaluationTrace:
        ema_fast = self._indicator_value(self._strategy._ema5)
        ema_slow = self._indicator_value(self._strategy._ema10)
        rsi = self._indicator_value(self._strategy._rsi14)
        ready = all(
            indicator is not None and bool(indicator.is_ready)
            for indicator in (self._strategy._ema5, self._strategy._ema10, self._strategy._rsi14)
        )
        relation_facts = {
            "ema_fast_above_slow": ready and ema_fast is not None and ema_slow is not None and ema_fast > ema_slow,
            "was_in_position": prior_in_position,
        }
        signal_facts = {
            "decision": intent.kind.value if intent is not None else "HOLD",
            "timeframe": "15m",
        }
        settings = self._strategy.signal_program_settings()
        evaluation_id = _semantic_hash(
            {
                "program_key": self.PROGRAM_KEY,
                "program_version": self.PROGRAM_VERSION,
                "settings": settings,
                "bar_close_ms": bar.end_ms,
            }
        )
        return EvaluationTrace(
            program_key=self.PROGRAM_KEY,
            program_version=self.PROGRAM_VERSION,
            evaluation_id=evaluation_id,
            bar_close_ms=bar.end_ms,
            bar_qualified=True,
            bucket_closed=True,
            ready=ready,
            relation_facts=relation_facts,
            signal_facts=signal_facts,
            staged_candidate=intent.kind.value if intent is not None else None,
            reason_evidence={
                "prior_countdown": prior_countdown,
                "ema_fast": str(ema_fast) if ema_fast is not None else "UNREADY",
                "ema_slow": str(ema_slow) if ema_slow is not None else "UNREADY",
                "rsi": str(rsi) if rsi is not None else "UNREADY",
            },
            action_plan_request=(
                {"contract": "single_long_stock", "intent": intent.kind.value}
                if intent is not None
                else None
            ),
        )

    @staticmethod
    def _indicator_value(indicator: Any) -> Decimal | None:
        if indicator is None or not indicator.is_ready:
            return None
        return indicator.current_value


@dataclass
class EmaCrossoverSignalProgram:
    """Registered Signal Program which is the only session-construction seam."""

    strategy: _EmaSignalStrategy
    session: EmaCrossoverSignalSession
    active: bool = False

    @classmethod
    def create(cls, strategy: _EmaSignalStrategy) -> EmaCrossoverSignalProgram:
        return cls(strategy=strategy, session=EmaCrossoverSignalSession(strategy))

    def activate_for_backtest(self) -> None:
        """Select staged callbacks only for Backtest's explicit adapter seam."""
        self.active = True


def trace_root(traces: list[EvaluationTrace]) -> str:
    """Return the stable semantic root for an ordered trace corpus."""
    return _semantic_hash([trace.semantic_payload() for trace in traces])


def trace_corpus_root(entries: list[dict[str, Any]]) -> str:
    """Hash named validated-settings evidence without treating a refactor as data."""
    return _semantic_hash(entries)


def _semantic_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EmaCrossoverSignalProgram",
    "EmaCrossoverSignalSession",
    "EvaluationStage",
    "EvaluationTrace",
    "Settlement",
    "StageQuarantine",
    "StageStatus",
    "trace_corpus_root",
    "trace_root",
]
