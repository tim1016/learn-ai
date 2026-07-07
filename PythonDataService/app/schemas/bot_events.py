"""Bot event stream contracts (ADR 0024 / PRD #928 Slice -1).

These models define the replacement contract for the narrated per-bot
pipeline stream. They are intentionally not wired to a router or publisher in
Slice -1; later slices emit and project these shapes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Pydantic 2.5 in the service image does not generate schemas for PEP 695
# type aliases reliably, so keep the older spelling until the runtime moves.
FactValue: TypeAlias = str | int | float | bool | None | list[object] | dict[str, object]  # noqa: UP040


class SourceAuthority(StrEnum):
    """Runtime that owns raw capture for a gate or terminal outcome."""

    ENGINE_LOOP = "engine_loop"
    DAEMON_LAUNCHER = "daemon_launcher"
    BROKER_SESSION = "broker_session"
    ACCOUNT_OWNER = "account_owner"


class GateStepResult(StrEnum):
    PASS = "pass"
    SKIP = "skip"
    BLOCK = "block"


class BotEventType(StrEnum):
    EVALUATION_IDLE = "evaluation_idle"
    SIGNAL_FIRED = "signal_fired"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_FILLED = "order_filled"
    # Preserves existing ADR-0014 cancellation rows in the stream tail.
    # Cancellations are broker outcomes, but not escalated terminal failures.
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"
    BLOCKED = "blocked"
    HALTED = "halted"
    LAUNCH_FAILED = "launch_failed"


class BotEventRawType(StrEnum):
    GATE_STEP = "gate_step"
    EVALUATION_IDLE = BotEventType.EVALUATION_IDLE.value
    SIGNAL_FIRED = BotEventType.SIGNAL_FIRED.value
    ORDER_SUBMITTED = BotEventType.ORDER_SUBMITTED.value
    ORDER_FILLED = BotEventType.ORDER_FILLED.value
    ORDER_CANCELLED = BotEventType.ORDER_CANCELLED.value
    ORDER_REJECTED = BotEventType.ORDER_REJECTED.value
    BLOCKED = BotEventType.BLOCKED.value
    HALTED = BotEventType.HALTED.value
    LAUNCH_FAILED = BotEventType.LAUNCH_FAILED.value


class BotEventSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class TerminalErrorCode(StrEnum):
    ORDER_REJECTED = "order_rejected"
    SUBMIT_UNCERTAIN = "submit_uncertain"
    HALTED = "halted"
    LAUNCH_FAILED = "launch_failed"
    UNMAPPED_DIAGNOSTIC = "unmapped_diagnostic"


class TerminalErrorSource(StrEnum):
    ENGINE = "engine"
    IBKR = "ibkr"
    DAEMON = "daemon"
    OS = "os"
    BROKER_SESSION = "broker_session"
    UNKNOWN = "unknown"


class BotEventIdentity(BaseModel):
    """The ADR-0024 identity ladder carried by raw and authored events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: str | None = Field(default=None, min_length=1)
    intent_id: str | None = Field(default=None, min_length=1)
    order_ref: str | None = Field(default=None, min_length=1)
    req_id: int | None = None
    order_id: int | None = None
    perm_id: int | None = None
    exec_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _must_have_some_identity(self) -> BotEventIdentity:
        if not any(
            (
                self.evaluation_id,
                self.intent_id,
                self.order_ref,
                self.req_id is not None,
                self.order_id is not None,
                self.perm_id is not None,
                self.exec_id,
            )
        ):
            raise ValueError("BotEventIdentity requires at least one identity field")
        return self


class GateStep(BaseModel):
    """One enforcement-time gate traversal in a bot event gate-walk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: str = Field(min_length=1)
    gate_id: str = Field(min_length=1)
    gate_result: GateStepResult
    source_authority: SourceAuthority
    facts: dict[str, FactValue] = Field(default_factory=dict)


class TerminalError(BaseModel):
    """Most granular error captured at the failing gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: TerminalErrorCode
    source: TerminalErrorSource
    gate_id: str | None = Field(default=None, min_length=1)
    message: str = Field(min_length=1)
    detail: str | None = Field(default=None, min_length=1)
    external_code: str | int | None = None
    external_message: str | None = Field(default=None, min_length=1)
    cause_chain: tuple[str, ...] = Field(default_factory=tuple)
    forensic_facts: dict[str, FactValue] = Field(default_factory=dict)


class IncidentDedupeKey(BaseModel):
    """Stable key for the one-visible-terminal-story rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str = Field(min_length=1)
    terminal_code: TerminalErrorCode
    evaluation_id: str | None = Field(default=None, min_length=1)
    order_ref: str | None = Field(default=None, min_length=1)
    req_id: int | None = None
    order_id: int | None = None
    perm_id: int | None = None

    @model_validator(mode="after")
    def _requires_evaluation_or_order(self) -> IncidentDedupeKey:
        if not any(
            (
                self.evaluation_id,
                self.order_ref,
                self.req_id is not None,
                self.order_id is not None,
                self.perm_id is not None,
            )
        ):
            raise ValueError("IncidentDedupeKey requires evaluation, order, or broker identity")
        return self


class BotEventRaw(BaseModel):
    """Enforcement-point-owned raw event stored in ``bot_events.jsonl``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    seq: int = Field(ge=0)
    ts_ms: int = Field(gt=0)
    strategy_instance_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    event_type: BotEventRawType
    source_authority: SourceAuthority
    identity: BotEventIdentity
    gate_step: GateStep | None = None
    terminal_error: TerminalError | None = None
    facts: dict[str, FactValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _required_child_matches_event_type(self) -> BotEventRaw:
        if self.event_type is BotEventRawType.GATE_STEP and self.gate_step is None:
            raise ValueError("gate_step raw events require gate_step")
        if self.event_type is not BotEventRawType.GATE_STEP and self.gate_step is not None:
            raise ValueError("gate_step child is only valid for gate_step raw events")
        if self.event_type in _TERMINAL_RAW_TYPES and self.terminal_error is None:
            raise ValueError(f"{self.event_type.value} raw events require terminal_error")
        if self.event_type not in _TERMINAL_RAW_TYPES and self.terminal_error is not None:
            raise ValueError("terminal_error child is only valid for terminal raw events")
        return self


class BotEventRow(BaseModel):
    """Authored projection row emitted to the operator-facing stream."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    seq: int = Field(ge=0)
    ts_ms: int = Field(gt=0)
    event_type: BotEventType
    source_authority: SourceAuthority
    identity: BotEventIdentity
    severity: BotEventSeverity
    headline: str = Field(min_length=1)
    narrative: str = Field(min_length=1)
    gate_steps: tuple[GateStep, ...] = Field(default_factory=tuple)
    terminal_error: TerminalError | None = None
    facts: dict[str, FactValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _terminal_rows_require_error(self) -> BotEventRow:
        if self.event_type in _TERMINAL_ROW_TYPES and self.terminal_error is None:
            raise ValueError(f"{self.event_type.value} rows require terminal_error")
        if self.event_type not in _TERMINAL_ROW_TYPES and self.terminal_error is not None:
            raise ValueError("terminal_error child is only valid for terminal rows")
        if self.event_type is BotEventType.BLOCKED and not any(
            step.gate_result is GateStepResult.BLOCK for step in self.gate_steps
        ):
            raise ValueError("blocked rows require at least one blocking gate_step")
        return self


_TERMINAL_RAW_TYPES = frozenset(
    {
        BotEventRawType.ORDER_REJECTED,
        BotEventRawType.HALTED,
        BotEventRawType.LAUNCH_FAILED,
    }
)

_TERMINAL_ROW_TYPES = frozenset(
    {
        BotEventType.ORDER_REJECTED,
        BotEventType.HALTED,
        BotEventType.LAUNCH_FAILED,
    }
)
