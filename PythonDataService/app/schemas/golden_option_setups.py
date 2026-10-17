"""HTTP contracts for immutable owner-approved Options setups."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.options_backtest.setup import OptionsBacktestSetup
from app.utils.session_anchors import MAX_TIMESTAMP_MS

COMMAND_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(pattern=COMMAND_KEY_PATTERN)


class ApproveGoldenOptionSetupRequest(_Request):
    run_id: int = Field(ge=1, le=2**63 - 1)
    expected_setup_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    note: str = Field(min_length=3, max_length=4000)

    @field_validator("note")
    @classmethod
    def _note_required(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 3:
            raise ValueError("Record a note about the exact tested setup being approved.")
        return text


class RevokeGoldenOptionSetupRequest(_Request):
    reason: str = Field(min_length=3, max_length=4000)

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 3:
            raise ValueError("Record why this Golden setup is being revoked.")
        return text


class ReproveGoldenOptionSetupRequest(_Request):
    pass


class GoldenOptionSetupEvidence(BaseModel):
    first_session_open_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    last_session_open_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    sessions_tested: int
    trades: int
    net_profit_usd: str
    starting_cash_usd: str
    slippage_percent: str
    limitations: list[str]


class GoldenOptionSetupEventView(BaseModel):
    id: int
    kind: Literal["reproved", "revoked"]
    rules_digest: str | None
    reason: str | None
    actor: str
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)


class GoldenOptionSetupView(BaseModel):
    id: str
    name: str
    underlying: str
    setup: OptionsBacktestSetup
    setup_sha256: str
    source_run_id: int
    rules_digest: str
    proof_sha256: str
    status: Literal["ready", "stale", "revoked"]
    status_explanation: str
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    approved_by: str
    note: str
    evidence: GoldenOptionSetupEvidence
    proof: dict[str, Any]
    events: list[GoldenOptionSetupEventView]


class GoldenOptionSetupRefusal(BaseModel):
    code: str
    message: str
