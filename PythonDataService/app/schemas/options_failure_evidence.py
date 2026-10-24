"""Read-only retained Options failure facts and targeted input inspection (#3399)."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.schemas.options_candidate_evidence import OptionsHeldMarkRule
from app.utils.session_anchors import MAX_TIMESTAMP_MS

Timestamp = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]


class OptionsFailureEvent(BaseModel):
    id: str
    code: Literal["missing_held_mark", "unclosed_position"]
    session_open_ms: Timestamp
    at_ms: Timestamp
    leg_id: str
    contract: str
    retained_path: str
    expected_input_sha256: str | None
    close_filled_at_ms: Timestamp | None
    explanation: str


class OptionsFailureEvidence(OptionsHeldMarkRule):
    study_id: str
    setup_name: str
    candidate_hash: str
    scope: Literal["development", "final"]
    evidence_identity: str
    evidence_blob_sha256: str
    start_ms: Timestamp
    end_ms: Timestamp
    scheduled_sessions: int
    replayed_sessions: int
    traded_sessions: int
    required_held_marks: int
    present_held_marks: int
    missing_held_marks: int
    forward_fold_count: int
    eligible_fold_winners: int
    final_exposure: Literal["not_opened", "opened", "not_recorded"]
    risk_complete: bool
    technical_failures: list[str]
    items: list[OptionsFailureEvent]
    total: int
    offset: int
    limit: int


class OptionsFailureInspection(BaseModel):
    study_id: str
    candidate_hash: str
    evidence_identity: str
    event_id: str
    code: Literal["no_eligible_trade_bar", "retained_file_unavailable", "retained_file_corrupt",
        "retained_file_unreadable", "retained_input_changed", "retained_input_not_recorded",
        "retained_input_recorded_absent", "retained_mark_input_disagreement"]
    retained_path: str
    expected_input_sha256: str | None
    snapshot_input_sha256: str | None
    input_verified: bool
    contains_eligible_trade_bar: bool | None
    explanation: str
