"""Operator-gated raw-evidence schemas (S4).

Bounded/paged, size-capped, redaction re-verified at response time.
Every read produces a server-side audit entry — tested through the HTTP seam.

Wire temporal fields are ``int64 ms UTC``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EvidenceEntry(BaseModel):
    """One redacted, size-capped custody record entry exposed as evidence."""

    model_config = ConfigDict(frozen=True)

    seq: int
    kind: str
    kind_label: str
    recorded_at_ms: int
    order_ref: str | None
    intent_id: str | None
    # Summarised, redacted payload — never raw bytes; secrets stripped at
    # capture time and re-verified at response time.
    summary: str
    has_more_detail: bool
    operation_ref: str | None = None
    operation_state: str | None = None
    broker_state: str | None = None
    custody_owner: str | None = None
    proof_reference: str | None = None
    source_event_at_ms: int | None = None
    clerk_observed_at_ms: int | None = None


class EvidencePage(BaseModel):
    """A bounded page of operator-gated evidence entries."""

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    account_id: str
    transaction_ref: str | None
    entries: list[EvidenceEntry]
    # Cursor for the next page; ``None`` when this is the last page.
    next_cursor: str | int | None
    total_entries: int
    truncated: bool
    # The operator identity attached to the audit log for this read.
    read_by: str
    read_at_ms: int


class EvidenceAuditEntry(BaseModel):
    """One server-side audit record produced by every evidence read.

    Audit entries are append-only and are never exposed through the same
    evidence endpoint — they have a separate audit path.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    strategy_instance_id: str
    transaction_ref: str | None
    operator_identity: str
    read_at_ms: int
    page_cursor: str | int | None
    page_size: int
    entries_returned: int
    client_hint: str | None = Field(default=None, max_length=256)
