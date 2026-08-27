"""Pydantic v2 schemas surviving the retired live-runs API.

The ``/api/live-runs`` HTTP surface and the host-runner control plane retired
with PR-A/PR-B of #1813; the 44 request/response/daemon-envelope models that
served them were deleted in PR-C. What remains are the durable-artifact and
receipt schemas still imported by live modules: the run-status sidecar, the
lifecycle gate result, the mutation-rung receipt, the cold-start
reconciliation receipt, and the terminal duty-outcome view.

All timestamps are int64 milliseconds UTC.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.operator.notices.schema import (
    OperatorNoticeAction,
    OperatorNoticeActionability,
    OperatorNoticeRemedyStatus,
    OperatorNoticeTier,
    validate_actionability_action_pairing,
)
from app.schemas.bot_lifecycle import BotDutyOutcomeKind


class ExitReason(StrEnum):
    """Reason why a live run exited."""

    normal = "normal"
    force_flat_complete = "force_flat_complete"
    keyboard_interrupt = "keyboard_interrupt"
    signal = "signal"
    max_orders_exceeded = "max_orders_exceeded"
    fatal_halt = "fatal_halt"
    recovery_flatten = "recovery_flatten"
    exception = "exception"
    # A start was refused because the run is poisoned (poisoned.flag present, or
    # corrupted). Distinct from fatal_halt (the live engine's intra-day trip):
    # this is the cold-start refusal, recorded so the console explains "fresh
    # run_id required" instead of a blank "ended unexpectedly".
    poisoned = "poisoned"


class RunStatusSidecar(BaseModel):
    """Sidecar process metadata for a live run.

    Created and maintained by the observer sidecar process, containing
    lifecycle timestamps and process metadata.

    PRD #619-A adds ``submit_mode_at_start`` and ``readonly_at_start``
    as durable child/run evidence the Resume gate consults for the
    submission_capability check (ADR-0011 amendment: identity and
    capability are independent facts). Both are captured at child
    boot and never mutated after. A legacy 1.x sidecar without either
    field reads as ``None`` and Resume treats capability as UNKNOWN.
    """

    schema_version: int = 2
    run_id: str
    started_at_ms: int
    last_update_ms: int
    ended_at_ms: int | None = None
    exit_code: int | None = None
    exit_reason: ExitReason | None = None
    host_pid: int
    # PRD #619-A — capability evidence.
    submit_mode_at_start: Literal["live_paper", "shadow"] | None = None
    readonly_at_start: bool | None = None
    # Startup/runtime failure evidence. ``exit_reason=exception`` is too coarse
    # for clients to present a useful remedy; typed fields let the operator
    # surface say e.g. "IBKR client ID is already in use" instead of collapsing
    # into a generic reconcile prompt.
    exit_error_code: str | None = None
    exit_error_message: str | None = None
    exit_error_detail: dict[str, Any] = Field(default_factory=dict)


MutationBlockageStageId = Literal[
    "control_plane",
    "host_process",
    "broker",
    "account_safety",
    "account_clerk",
    "reconciliation",
    "preflight",
    "trading_session",
    "runtime_freshness",
]

MutationRungReceiptCode = Literal[
    "mutation.next_blocking_rung",
    "mutation.scoped_all_clear",
    "mutation.observational_warning",
]


class MutationRungReceipt(BaseModel):
    """Notice-shaped post-mutation receipt authored from the fresh ladder.

    These receipts are not persisted operator incidents, so their ``code`` values
    intentionally live outside the closed ``OperatorNoticeCode`` union. They
    still obey the notice actionability vocabulary and action-pairing contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: MutationRungReceiptCode
    tier: OperatorNoticeTier
    title: str
    message: str
    rung_id: MutationBlockageStageId | None = None
    source_codes: list[str] = Field(default_factory=list)
    forensic_facts: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    actionability: OperatorNoticeActionability
    resolution: str = Field(min_length=1)
    remedy_status: OperatorNoticeRemedyStatus | None = None
    action: OperatorNoticeAction
    occurred_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _actionability_contract(self) -> MutationRungReceipt:
        validate_actionability_action_pairing(
            actionability=self.actionability,
            action=self.action,
            remedy_status=self.remedy_status,
            noun="receipts",
        )
        return self


GateResultStatus = Literal[
    "pass",
    "block",
    "poison",
    "freeze",
    "unknown",
    "not_applicable",
]


class GateResult(BaseModel):
    """Canonical lifecycle gate result row.

    A gate result is the enforcement-backed predicate clients can
    render and diagnose. Older readiness rows still expose their
    ``name`` / ``status`` / ``severity`` / ``detail`` fields for
    compatibility; ``GateResult`` is the normalized contract newer
    account-level gates consume.
    """

    model_config = ConfigDict(extra="forbid")

    gate_id: str
    status: GateResultStatus
    source: str
    operator_reason: str
    operator_next_step: str | None = None
    evidence_at_ms: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Reconciliation receipt (ADR-0008 §5 / PR 1 cold-start orchestrator).
# ---------------------------------------------------------------------------

ReceiptStatus = Literal["in_progress", "passed", "failed"]
"""Lifecycle status of a reconciliation receipt.

``in_progress`` is written first (so a crash mid-reconcile leaves an honest
sentinel rather than a stale ``passed`` receipt from the previous boot);
``passed`` / ``failed`` overwrite it with the verdict via atomic replace.
"""

ReceiptOutcome = Literal["clean", "adopted"]
"""Meaningful only when ``status == passed``.

``clean`` = the broker snapshot matched the projection (Continue).
``adopted`` = one or more owned orphans were folded in via
``ADOPTED_BROKER_ORDER`` (Adopt).
"""


class ReconciliationReceipt(BaseModel):
    """Durable historical evidence of a cold-start reconciliation attempt.

    The retired IBKR runtime wrote this to
    ``<run_dir>/reconciliation_receipt.json``. Read projections retain the
    schema for existing artifacts; no current execution guard consumes it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    status: ReceiptStatus
    outcome: ReceiptOutcome | None = None
    run_id: str
    strategy_instance_id: str
    namespace: str
    started_at_ms: int = Field(gt=0)
    completed_at_ms: int | None = Field(default=None, ge=0)
    last_reconcile_ms: int | None = Field(default=None, ge=0)
    sidecar_wal_seq: int = Field(default=0, ge=0)
    broker_observed_at_ms: int | None = Field(default=None, ge=0)
    adopted_intent_ids: tuple[str, ...] = ()
    failure_reason: str | None = None


class BotDutyOutcomeView(BaseModel):
    """Durable terminal duty evidence rendered by the operator surface."""

    model_config = ConfigDict(extra="forbid")

    kind: BotDutyOutcomeKind
    reason_code: str
    recorded_at_ms: int
    run_id: str | None = None
