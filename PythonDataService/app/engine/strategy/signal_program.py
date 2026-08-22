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
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.signal_intent import SignalIntent


class Settlement(StrEnum):
    """The only legal outcomes for a staged signal cycle."""

    COMMIT = "COMMIT"
    DISCARD = "DISCARD"


class EvaluationMode(StrEnum):
    """The immutable authority carried by an observed source bar."""

    DECIDE = "DECIDE"
    OBSERVE_ONLY = "OBSERVE_ONLY"


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
    evaluation_mode: EvaluationMode = EvaluationMode.DECIDE

    def semantic_payload(self) -> dict[str, Any]:
        """Return only fields that define the observable decision meaning."""
        return asdict(self)


@dataclass
class EvaluationStage:
    """A decision cycle awaiting its single settlement action."""

    bar: TradeBar
    trace: EvaluationTrace
    intents: tuple[SignalIntent, ...]
    decision: SignalDecision
    status: StageStatus = StageStatus.STAGED
    settlement: Settlement | None = None


@dataclass(frozen=True)
class StageQuarantine:
    """A fail-closed result which never invokes strategy or execution code."""

    status: StageStatus
    reason: str


@dataclass(frozen=True)
class SignalDecision:
    """A strategy-owned semantic decision, before any custody effect exists.

    The program owns the stage and settlement.  The strategy owns the
    mathematical facts that make the decision meaningful.  Keeping this data
    public and immutable is what lets the session record or discard a paused
    bar without reading a strategy's private state or swapping its executor.
    """

    intent: SignalIntent | None
    ready: bool
    relation_facts: dict[str, bool]
    signal_facts: dict[str, str]
    reason_evidence: dict[str, str | int | bool]
    action_plan_request: dict[str, str] | None


class SignalProgramStrategy(Protocol):
    """Public strategy interface owned by a typed signal program."""

    def evaluate_signal_bar(self, bar: TradeBar) -> SignalDecision: ...

    def commit_signal_decision(self, bar: TradeBar, intent: SignalIntent) -> None: ...

    def discard_signal_decision(self, bar: TradeBar, intent: SignalIntent | None) -> None: ...

    def signal_program_settings(self) -> dict[str, str]: ...


class EmaCrossoverSignalSession:
    """Transactional adapter around the already canonical EMA decision math."""

    PROGRAM_KEY = "ema_crossover_signal"
    PROGRAM_VERSION = "ema-crossover-signal/v1"
    # Identifies the *shape* of this session's public contract — PRD §12's
    # staged open/advance/settle protocol (EvaluationMode DECIDE/OBSERVE_ONLY,
    # Settlement COMMIT/DISCARD, EvaluationTrace) — independent of
    # PROGRAM_VERSION, which identifies the EMA/RSI decision math. A future
    # program could reuse this exact protocol shape under a different
    # PROGRAM_VERSION, or this shape could change under the same math.
    # app.engine.strategy.registry mirrors this into the registry contract
    # rather than re-declaring it, so the two cannot drift apart silently.
    PROTOCOL_VERSION = "signal-session-protocol/v1"
    TIMEFRAME_MS = 15 * 60 * 1000

    def __init__(self, strategy: SignalProgramStrategy) -> None:
        self._strategy = strategy
        self._active_stage: EvaluationStage | None = None
        self._last_bar_close_ms: int | None = None
        self.traces: list[EvaluationTrace] = []

    @property
    def active_stage(self) -> EvaluationStage | None:
        return self._active_stage

    def advance(
        self,
        bar: TradeBar,
        *,
        mode: EvaluationMode,
    ) -> EvaluationStage | StageQuarantine:
        """Evaluate one complete bucket under its captured immutable mode."""
        if self._active_stage is not None:
            return StageQuarantine(StageStatus.UNSETTLED_STAGE, "UNSETTLED_STAGE")
        if bar.end_ms - bar.start_ms != self.TIMEFRAME_MS:
            return StageQuarantine(StageStatus.REJECTED_BAR, "TIMEFRAME_MISMATCH")
        if self._last_bar_close_ms is not None and bar.end_ms <= self._last_bar_close_ms:
            return StageQuarantine(StageStatus.REJECTED_BAR, "NON_MONOTONIC_DECISION_CLOCK")
        decision = self._strategy.evaluate_signal_bar(bar)
        intents = () if decision.intent is None else (decision.intent,)
        trace = self._build_trace(bar=bar, decision=decision, mode=mode)
        stage = EvaluationStage(bar=bar, trace=trace, intents=intents, decision=decision)
        self._active_stage = stage
        self._last_bar_close_ms = bar.end_ms
        if mode is EvaluationMode.OBSERVE_ONLY:
            # An observed candidate is deliberately terminal in the same
            # call. Continue may change the run gate immediately afterward,
            # but it cannot resurrect this already-discarded evaluation.
            self.settle(Settlement.DISCARD)
        return stage

    def settle(self, settlement: Settlement) -> EvaluationStage:
        """Commit the staged semantic action or restore the retryable EMA state."""
        stage = self._active_stage
        if stage is None:
            raise RuntimeError("SignalSession has no staged evaluation to settle")
        if settlement is Settlement.COMMIT:
            for intent in stage.intents:
                self._strategy.commit_signal_decision(stage.bar, intent)
        else:
            self._strategy.discard_signal_decision(stage.bar, stage.decision.intent)

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
        decision: SignalDecision,
        mode: EvaluationMode,
    ) -> EvaluationTrace:
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
            ready=decision.ready,
            relation_facts=decision.relation_facts,
            signal_facts=decision.signal_facts,
            staged_candidate=decision.intent.kind.value if decision.intent is not None else None,
            reason_evidence=decision.reason_evidence,
            action_plan_request=decision.action_plan_request,
            evaluation_mode=mode,
        )


@dataclass
class EmaCrossoverSignalProgram:
    """Registered Signal Program which is the only session-construction seam."""

    strategy: SignalProgramStrategy
    session: EmaCrossoverSignalSession
    active: bool = False
    _default_evaluation_mode: EvaluationMode | None = None
    _captured_modes_by_close_ms: dict[int, EvaluationMode] = field(default_factory=dict)
    _completed_stages: list[EvaluationStage] = field(default_factory=list)

    @classmethod
    def create(cls, strategy: SignalProgramStrategy) -> EmaCrossoverSignalProgram:
        return cls(strategy=strategy, session=EmaCrossoverSignalSession(strategy))

    def activate_for_backtest(self) -> None:
        """Select staged callbacks only for Backtest's explicit adapter seam."""
        self.active = True
        self._default_evaluation_mode = EvaluationMode.DECIDE

    def activate_for_runner(self) -> None:
        """Require the live adapter to supply a captured mode for every bar."""
        self.active = True
        self._default_evaluation_mode = None

    def capture_source_bar(self, bar: TradeBar, *, mode: EvaluationMode) -> None:
        """Bind the source bar's mode before its consolidator can fire."""
        existing = self._captured_modes_by_close_ms.get(bar.end_ms)
        if existing is not None and existing is not mode:
            raise RuntimeError("A source bar cannot have two evaluation modes")
        self._captured_modes_by_close_ms[bar.end_ms] = mode

    def on_consolidated_bar(self, bar: TradeBar) -> None:
        """Advance using the mode captured with the bucket's closing source bar."""
        mode = self._captured_modes_by_close_ms.pop(bar.end_ms, self._default_evaluation_mode)
        if mode is None:
            raise RuntimeError("SignalProgram requires a captured evaluation mode for every live bar")
        stage = self.session.advance(bar, mode=mode)
        if isinstance(stage, EvaluationStage) and stage.settlement is not None:
            self._completed_stages.append(stage)

    def take_completed_stage(self) -> EvaluationStage | None:
        """Return an already-settled observation for runner audit projection."""
        return self._completed_stages.pop(0) if self._completed_stages else None


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
    "EvaluationMode",
    "EvaluationStage",
    "EvaluationTrace",
    "Settlement",
    "SignalDecision",
    "StageQuarantine",
    "StageStatus",
    "trace_corpus_root",
    "trace_root",
]
