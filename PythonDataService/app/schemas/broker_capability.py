from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.broker.ibkr.api_evidence import IbkrApiEvidenceEvent

SessionKind = Literal["RTH", "PRE", "POST", "OVERNIGHT"]
CapabilityDataQuality = Literal["live", "delayed", "frozen", "delayed_frozen", "none"]
CapabilityTradeability = Literal["yes", "needs_enablement", "no"]
CapabilityAccountMode = Literal["live", "paper"]


class SessionCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_today_open_ms: int | None = Field(default=None, ge=0)
    window_today_close_ms: int | None = Field(default=None, ge=0)
    data: CapabilityDataQuality
    tradeable: CapabilityTradeability
    order_eligible_outside_rth: bool
    evidence_codes: list[int] = Field(default_factory=list)


class SessionDataCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z][A-Z0-9.\-]*$")
    con_id: int = Field(ge=0)
    account_mode: CapabilityAccountMode
    account_id: str
    probed_at_ms: int = Field(gt=0)
    time_zone_id: str = Field(min_length=1)
    sessions: dict[str, SessionCapability]
    raw_evidence: list[IbkrApiEvidenceEvent] = Field(default_factory=list)

    @field_validator("sessions")
    @classmethod
    def _requires_all_sessions(
        cls,
        value: dict[str, SessionCapability],
    ) -> dict[str, SessionCapability]:
        required = {"RTH", "PRE", "POST", "OVERNIGHT"}
        missing = required - set(value)
        if missing:
            raise ValueError(f"sessions missing required keys: {sorted(missing)}")
        return value


class BrokerCapabilityProbeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshots: list[SessionDataCapability]


class BrokerCapabilityReadResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshots: list[SessionDataCapability]
