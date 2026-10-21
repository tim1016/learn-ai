"""Read-only archive health and explicit manual-preservation receipts."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

TimestampMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]

ArchiveSeverity = Literal["healthy", "catching_up", "waiting", "at_risk", "failed", "unavailable"]


class ArchiveGap(BaseModel):
    session_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    permanent: bool
    days_left: int | None = None
    reason: str
    recorded_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)


class UnderlyingArchiveHealth(BaseModel):
    underlying: str
    severity: ArchiveSeverity
    reason: str
    earliest_required_session_ms: TimestampMs | None = None
    complete_through_session_ms: TimestampMs | None = None
    last_archived_session_ms: TimestampMs | None = None
    pending_sessions: int
    partial_sessions: int
    lost_sessions: int
    gaps: list[ArchiveGap]
    phase: str
    last_success_ms: TimestampMs | None = None
    last_failure: str | None = None
    last_pass_started_ms: TimestampMs | None = None
    last_pass_ended_ms: TimestampMs | None = None
    next_due_ms: TimestampMs | None = None
    snapshot_session_ms: TimestampMs | None = None
    snapshot_missed_evenings: int = 0
    snapshot_incomplete: int = 0
    snapshot_field_gaps: int = 0


class ArchiveJobHealth(BaseModel):
    severity: ArchiveSeverity
    reason: str
    last_success_ms: TimestampMs | None = None
    age_ms: int | None = None
    generation_id: str | None = None
    last_failure: str | None = None
    size_bytes: int | None = None
    free_bytes: int | None = None
    manifest_sha256: str | None = None
    oldest_unbacked_capture_ms: TimestampMs | None = None


class OptionArchiveHealth(BaseModel):
    enabled: bool
    generated_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    attention: bool
    underlyings: list[UnderlyingArchiveHealth]
    backup: ArchiveJobHealth
    test_restore: ArchiveJobHealth
    retention_permission: Literal["awaiting_vendor_reply"] = "awaiting_vendor_reply"


class ArchiveTriggerReceipt(BaseModel):
    accepted: bool
    job: Literal["capture", "snapshot", "backup", "restore"]
    status_url: str = "/api/option-archive/status"


class ArchiveSessionCaptureRequest(BaseModel):
    underlying: Literal["SPY", "QQQ", "IWM"]
    session_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    retry_exhausted: bool = False
