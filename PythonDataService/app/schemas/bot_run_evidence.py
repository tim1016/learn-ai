"""Typed immutable evidence for one supervised bot run."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.bot_lifecycle import BotDutyOutcomeKind
from app.schemas.canary_admission import CanaryRollbackDecision

CRASH_EXCEPTION_TYPE_MAX_LENGTH = 128
CRASH_MESSAGE_MAX_LENGTH = 2_048
CRASH_SOURCE_FILE_MAX_LENGTH = 1_024


class BotCrashDiagnostic(BaseModel):
    """Exact developer evidence for the exception that ended one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exception_type: str = Field(min_length=1, max_length=CRASH_EXCEPTION_TYPE_MAX_LENGTH)
    message: str = Field(min_length=1, max_length=CRASH_MESSAGE_MAX_LENGTH)
    source_file: str = Field(min_length=1, max_length=CRASH_SOURCE_FILE_MAX_LENGTH)
    source_line: int = Field(ge=1)


class BotRunTerminalOutcomeView(BaseModel):
    """Immutable terminal outcome and optional crash evidence for one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: BotDutyOutcomeKind
    reason_code: str = Field(min_length=1, max_length=128)
    recorded_at_ms: int = Field(ge=0)
    run_id: str | None = None
    crash_diagnostic: BotCrashDiagnostic | None = None
    # #1729 AC10: the Clerk-proved canary rollback verdict recorded alongside
    # this Stop, when one was computed. See
    # ``app.services.canary_admission.evaluate_canary_rollback``.
    canary_rollback: CanaryRollbackDecision | None = None

    @model_validator(mode="after")
    def require_crashed_outcome_for_diagnostic(self) -> BotRunTerminalOutcomeView:
        if self.crash_diagnostic is not None and self.kind != "CRASHED":
            raise ValueError("crash diagnostics require a CRASHED terminal outcome")
        return self


__all__ = [
    "CRASH_EXCEPTION_TYPE_MAX_LENGTH",
    "CRASH_MESSAGE_MAX_LENGTH",
    "CRASH_SOURCE_FILE_MAX_LENGTH",
    "BotCrashDiagnostic",
    "BotRunTerminalOutcomeView",
]
