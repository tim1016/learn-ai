"""Typed, operator-required recovery contracts for a corrupt Clerk journal."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

JournalRecoveryPhase = Literal[
    "QUARANTINE_REQUIRED",
    "QUARANTINE_PENDING",
    "REBASELINE_REQUIRED",
    "REBASELINE_PENDING",
    "COMPLETE",
]
JournalRecoveryMissingArtifact = Literal["journal", "inbox"]


class JournalRecoveryPosition(BaseModel):
    """One broker-observed holding retained without guessed bot ownership."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1, max_length=64)
    signed_quantity: float

    @model_validator(mode="after")
    def validate_signed_quantity(self) -> JournalRecoveryPosition:
        if self.signed_quantity == 0 or not math.isfinite(self.signed_quantity):
            raise ValueError("signed_quantity must be finite and non-zero")
        return self


class JournalRecoveryState(BaseModel):
    """Durable progress of the only permitted corruption ceremony."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    recovery_epoch: int = Field(default=1, ge=1)
    phase: JournalRecoveryPhase
    quarantined_journal_name: str | None = None
    quarantined_inbox_name: str | None = None
    missing_artifacts: tuple[JournalRecoveryMissingArtifact, ...] = ()
    quarantined_at_ms: int | None = Field(default=None, ge=0)
    baseline_receipt_id: str | None = None
    quarantine_receipt_id: str | None = None
    quarantine_idempotency_key: str | None = None
    rebaseline_idempotency_key: str | None = None
    broker_evidence_positions: tuple[JournalRecoveryPosition, ...] = ()
    observed_at_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_ceremony_receipts(self) -> JournalRecoveryState:
        """Reject syntactically valid state that cannot prove crash recovery."""

        quarantine_claimed = self.phase != "QUARANTINE_REQUIRED"
        if quarantine_claimed and (
            self.quarantined_at_ms is None
            or self.quarantine_receipt_id is None
            or self.quarantine_idempotency_key is None
            or (
                self.quarantined_journal_name is None
                and self.quarantined_inbox_name is None
                and not self.missing_artifacts
            )
        ):
            raise ValueError("claimed quarantine requires its durable audit receipt and target")
        rebaseline_claimed = self.phase in {"REBASELINE_PENDING", "COMPLETE"}
        if rebaseline_claimed and (
            self.baseline_receipt_id is None
            or self.rebaseline_idempotency_key is None
            or self.observed_at_ms is None
        ):
            raise ValueError("claimed re-baseline requires its durable snapshot receipt")
        return self


class JournalRecoveryRequest(BaseModel):
    """A typed confirmation for exactly one irreversible ceremony step."""

    model_config = ConfigDict(frozen=True)

    confirmation_token: Literal["QUARANTINE", "REBASELINE"]
    idempotency_key: str = Field(min_length=1, max_length=256)


class JournalRecoveryReceipt(BaseModel):
    """Durable evidence returned after one recovery step completes."""

    model_config = ConfigDict(frozen=True)

    receipt_id: str = Field(min_length=1, max_length=320)
    account_id: str = Field(min_length=1, max_length=64)
    phase: Literal["REBASELINE_REQUIRED", "COMPLETE"]
    recorded_at_ms: int = Field(ge=0)
    quarantined_journal_name: str | None = None
    broker_evidence_positions: tuple[JournalRecoveryPosition, ...] = ()
