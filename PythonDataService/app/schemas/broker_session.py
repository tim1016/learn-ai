"""Broker session mirror wire models.

All timestamps are int64 milliseconds UTC. The data-plane mirror is read-only;
these DTOs carry observation facts and reconciliation labels only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.broker.ibkr.event_codes import (
    BrokerSessionEventCategory,
    BrokerSessionEventSeverity,
)
from app.operator.notices.schema import OperatorNotice

BrokerSessionIdentityType = Literal[
    "bot",
    "system",
    "orphaned_bot_socket",
    "ghost",
]
BrokerSessionRecency = Literal[
    "current",
    "past_closed",
    "past_last_known",
    "unknown",
]
BrokerSessionObserverStatus = Literal["online", "degraded"]
BrokerSessionGhostDetectionStatus = Literal["available", "unknown"]
BrokerSessionRecoveryState = Literal[
    "HEALTHY",
    "LINK_INTERRUPTED",
    "RESTORING",
    "SOCKET_DOWN",
    "RECONNECTING",
    "HARD_DOWN",
]
BrokerSessionAttentionCode = Literal[
    "REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE",
    "STARTED_BUT_NO_SOCKET",
    "SOCKET_WITHOUT_LIVE_PID",
    "ORPHANED_BOT_SOCKET",
    "GHOST_SOCKET",
    "GHOST_DETECTION_UNAVAILABLE",
    "CLIENT_SIGNAL_STALE",
]


class GatewaySocketRow(BaseModel):
    """One ESTABLISHED TCP connection involving the configured IBKR port."""

    model_config = ConfigDict(frozen=True)

    pid: int | None = Field(default=None, ge=0)
    command: str = ""
    argv: list[str] = Field(default_factory=list)
    run_dir: str | None = None
    local_port: int | None = Field(default=None, ge=0, le=65535)
    remote_host: str | None = None
    remote_port: int | None = Field(default=None, ge=0, le=65535)
    state: Literal["ESTABLISHED"] = "ESTABLISHED"


class GatewaySocketsSnapshot(BaseModel):
    """Host-daemon socket probe result."""

    model_config = ConfigDict(frozen=True)

    fetched_at_ms: int = Field(ge=0)
    gateway_port: int = Field(ge=1, le=65535)
    sockets: list[GatewaySocketRow] = Field(default_factory=list)


class BrokerSessionRegistryClaim(BaseModel):
    """The control plane's live-process claim for a roster row."""

    model_config = ConfigDict(frozen=True)

    state: str
    run_id: str | None = None
    pid: int | None = Field(default=None, ge=0)
    run_dir: str | None = None
    started_at_ms: int | None = Field(default=None, ge=0)
    ended_at_ms: int | None = Field(default=None, ge=0)


class BrokerSessionRosterRow(BaseModel):
    """One row in the broker session mirror roster."""

    model_config = ConfigDict(frozen=True)

    row_id: str
    identity_type: BrokerSessionIdentityType
    recency: BrokerSessionRecency
    socket_present: bool
    strategy_instance_id: str | None = None
    run_id: str | None = None
    account_id: str | None = None
    posture: str | None = None
    client_id: int | None = Field(default=None, ge=0)
    pid: int | None = Field(default=None, ge=0)
    command: str | None = None
    run_dir: str | None = None
    local_port: int | None = Field(default=None, ge=0, le=65535)
    remote_host: str | None = None
    remote_port: int | None = Field(default=None, ge=0, le=65535)
    connection_state: str | None = None
    recovery_state: BrokerSessionRecoveryState | None = None
    connection_epoch: int | None = Field(default=None, ge=0)
    last_event_ms: int | None = Field(default=None, ge=0)
    as_of_ms: int = Field(ge=0)
    event_counts: dict[BrokerSessionEventCategory, int] = Field(default_factory=dict)
    attention_codes: list[BrokerSessionAttentionCode] = Field(default_factory=list)
    registry_claim: BrokerSessionRegistryClaim | None = None
    notice: OperatorNotice | None = None


class BrokerSessionMirrorSnapshot(BaseModel):
    """Read-only broker session mirror snapshot served to Angular."""

    model_config = ConfigDict(frozen=True)

    as_of_ms: int = Field(ge=0)
    gateway_port: int = Field(ge=1, le=65535)
    observer_status: BrokerSessionObserverStatus
    ghost_detection_status: BrokerSessionGhostDetectionStatus
    rows: list[BrokerSessionRosterRow] = Field(default_factory=list)
    degradation_reasons: list[str] = Field(default_factory=list)


class BrokerSessionEvent(BaseModel):
    """Classified broker event for the session mirror."""

    model_config = ConfigDict(frozen=True)

    seq: int = Field(ge=1)
    ts_ms: int = Field(ge=0)
    category: BrokerSessionEventCategory
    severity: BrokerSessionEventSeverity
    label: str
    message: str | None = None
    raw_event_type: str
    client_id: int | None = Field(default=None, ge=0)
    account_id: str | None = None
    ibkr_code: int | None = None
    connection_state: str | None = None
    raw: dict[str, JsonValue] = Field(default_factory=dict)


class BrokerSessionEventPage(BaseModel):
    """Page of classified broker-session diagnostic events."""

    model_config = ConfigDict(frozen=True)

    rows: list[BrokerSessionEvent] = Field(default_factory=list)
    next_seq: int | None = Field(default=None, ge=1)


BrokerSessionEventPurgeConfirm = Literal["PURGE_BROKER_SESSION_DIAGNOSTICS"]


class BrokerSessionEventPurgeRequest(BaseModel):
    """Request to purge only broker-session diagnostic event history."""

    model_config = ConfigDict(frozen=True)

    client_id: int | None = Field(default=None, ge=0)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    confirm: BrokerSessionEventPurgeConfirm

    @model_validator(mode="after")
    def _validate_filter(self) -> BrokerSessionEventPurgeRequest:
        if self.client_id is None and self.start_ms is None and self.end_ms is None:
            raise ValueError("at least one purge filter is required")
        if (
            self.start_ms is not None
            and self.end_ms is not None
            and self.start_ms > self.end_ms
        ):
            raise ValueError("start_ms must be <= end_ms")
        return self


class BrokerSessionEventPurgeResult(BaseModel):
    """Diagnostic event purge result."""

    model_config = ConfigDict(frozen=True)

    purged_count: int = Field(ge=0)
    remaining_count: int = Field(ge=0)
