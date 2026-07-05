"""Broker session mirror wire models.

All timestamps are int64 milliseconds UTC. The data-plane mirror is read-only;
these DTOs carry observation facts and reconciliation labels only.
"""

from __future__ import annotations

from typing import Literal, Self, TypeGuard

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.broker.ibkr.event_codes import (
    BrokerSessionEventCategory,
    BrokerSessionEventSeverity,
)
from app.operator.notices.schema import OperatorNotice

BrokerSessionDisplaySeverity = Literal["ok", "info", "warning", "critical", "neutral"]
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
    "REGISTRY_SNAPSHOT_UNAVAILABLE",
    "SOCKET_ATTRIBUTION_UNAVAILABLE",
    "CLIENT_SIGNAL_STALE",
]


class BrokerSessionDisplayLabel(BaseModel):
    """Backend-authored label + severity for a mirror display chip."""

    model_config = ConfigDict(frozen=True)

    label: str
    severity: BrokerSessionDisplaySeverity


class BrokerSessionAttentionItem(BaseModel):
    """Backend-authored attention chip for one broker-session row."""

    model_config = ConfigDict(frozen=True)

    code: BrokerSessionAttentionCode
    label: str
    severity: BrokerSessionDisplaySeverity
    summary: str | None = None


class BrokerSessionRosterPresentation(BaseModel):
    """Display labels for a roster row.

    Raw identity/recency/recovery fields remain for audit and filtering, but
    Angular renders these server-authored labels instead of maintaining its own
    copy map.
    """

    model_config = ConfigDict(frozen=True)

    display_name: str
    identity: BrokerSessionDisplayLabel
    recency: BrokerSessionDisplayLabel
    broker: BrokerSessionDisplayLabel
    recovery: BrokerSessionDisplayLabel


class BrokerSessionGlobalEvent(BaseModel):
    """Global mirror event that should not be listed as a bot session."""

    model_config = ConfigDict(frozen=True)

    code: str
    label: str
    severity: BrokerSessionDisplaySeverity
    summary: str
    current: bool
    source: Literal["network", "data_plane"]
    observed_at_ms: int | None = Field(default=None, ge=0)
    client_id: int | None = Field(default=None, ge=0)


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
    events: list[BrokerSessionEvent] = Field(default_factory=list)
    attention_codes: list[BrokerSessionAttentionCode] = Field(default_factory=list)
    attention_items: list[BrokerSessionAttentionItem] = Field(default_factory=list)
    presentation: BrokerSessionRosterPresentation
    registry_claim: BrokerSessionRegistryClaim | None = None
    notice: OperatorNotice | None = None

    @model_validator(mode="before")
    @classmethod
    def _backfill_presentation(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "attention_items" not in out:
            codes = out.get("attention_codes")
            if isinstance(codes, list):
                out["attention_items"] = [
                    broker_session_attention_item(code).model_dump()
                    for code in codes
                    if _is_attention_code(code)
                ]
        presentation = out.get("presentation")
        if not isinstance(presentation, dict):
            out["presentation"] = broker_session_row_presentation(out).model_dump()
        elif "display_name" not in presentation:
            out["presentation"] = {
                **broker_session_row_presentation(out).model_dump(),
                **presentation,
            }
        return out


class BrokerSessionMirrorSummary(BaseModel):
    """Backend-authored aggregate counts for the mirror roster."""

    model_config = ConfigDict(frozen=True)

    current: int = Field(default=0, ge=0)
    past: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)
    attention: int = Field(default=0, ge=0)


class BrokerSessionMirrorSnapshot(BaseModel):
    """Read-only broker session mirror snapshot served to Angular."""

    model_config = ConfigDict(frozen=True)

    as_of_ms: int = Field(ge=0)
    gateway_port: int = Field(ge=1, le=65535)
    observer_status: BrokerSessionObserverStatus
    ghost_detection_status: BrokerSessionGhostDetectionStatus
    global_events: list[BrokerSessionGlobalEvent] = Field(default_factory=list)
    rows: list[BrokerSessionRosterRow] = Field(default_factory=list)
    summary: BrokerSessionMirrorSummary = Field(default_factory=BrokerSessionMirrorSummary)
    degradation_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _backfill_legacy_summary(cls, data: object) -> object:
        if not isinstance(data, dict) or "summary" in data:
            return data
        rows = data.get("rows")
        if not isinstance(rows, list):
            return data
        return {**data, "summary": _summary_from_raw_rows(rows)}


class BrokerSessionHistoryPage(BaseModel):
    """Recent broker-session roster snapshots, newest first."""

    model_config = ConfigDict(frozen=True)

    rows: list[BrokerSessionMirrorSnapshot] = Field(default_factory=list)
    retained_count: int = Field(ge=0)


class BrokerSessionEventPage(BaseModel):
    """Page of classified broker-session diagnostic events."""

    model_config = ConfigDict(frozen=True)

    rows: list[BrokerSessionEvent] = Field(default_factory=list)
    next_seq: int | None = Field(default=None, ge=1)


BrokerSessionEventPurgeConfirm = Literal["PURGE_BROKER_SESSION_DIAGNOSTICS"]


class _BrokerSessionPurgeFilterRequest(BaseModel):
    """Shared request guard for destructive broker-session diagnostic purges."""

    model_config = ConfigDict(frozen=True)

    client_id: int | None = Field(default=None, ge=0)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    confirm: BrokerSessionEventPurgeConfirm

    @model_validator(mode="after")
    def _validate_filter(self) -> Self:
        if self.client_id is None and self.start_ms is None and self.end_ms is None:
            raise ValueError("at least one purge filter is required")
        if self.start_ms is not None and self.end_ms is not None and self.start_ms > self.end_ms:
            raise ValueError("start_ms must be <= end_ms")
        return self


class BrokerSessionEventPurgeRequest(_BrokerSessionPurgeFilterRequest):
    """Request to purge only broker-session diagnostic event history."""


class BrokerSessionEventPurgeResult(BaseModel):
    """Diagnostic event purge result."""

    model_config = ConfigDict(frozen=True)

    purged_count: int = Field(ge=0)
    remaining_count: int = Field(ge=0)


class BrokerSessionHistoryPurgeRequest(_BrokerSessionPurgeFilterRequest):
    """Request to purge only broker-session roster history diagnostics."""


class BrokerSessionHistoryPurgeResult(BaseModel):
    """Diagnostic roster-history purge result."""

    model_config = ConfigDict(frozen=True)

    purged_row_count: int = Field(ge=0)
    purged_snapshot_count: int = Field(ge=0)
    remaining_snapshot_count: int = Field(ge=0)


def broker_session_attention_item(code: BrokerSessionAttentionCode) -> BrokerSessionAttentionItem:
    label, severity, summary = _ATTENTION_PRESENTATION[code]
    return BrokerSessionAttentionItem(
        code=code,
        label=label,
        severity=severity,
        summary=summary,
    )


def broker_session_row_presentation(row: object) -> BrokerSessionRosterPresentation:
    identity = _value_from(row, "identity_type")
    recency = _value_from(row, "recency")
    connection_state = _value_from(row, "connection_state")
    recovery_state = _value_from(row, "recovery_state")
    return BrokerSessionRosterPresentation(
        display_name=_row_display_name(row),
        identity=_display_label(_IDENTITY_PRESENTATION, identity, "Unattributed session", "warning"),
        recency=_display_label(_RECENCY_PRESENTATION, recency, "Recency unknown", "warning"),
        broker=_broker_display_label(connection_state),
        recovery=_display_label(_RECOVERY_PRESENTATION, recovery_state, "Recovery unknown", "neutral"),
    )


def _row_display_name(row: object) -> str:
    strategy_instance_id = _value_from(row, "strategy_instance_id")
    if isinstance(strategy_instance_id, str) and strategy_instance_id:
        return strategy_instance_id
    identity = _value_from(row, "identity_type")
    if identity == "system":
        return "Data-plane broker client"
    if identity == "orphaned_bot_socket":
        return "Orphaned bot socket"
    if identity == "ghost":
        return "Unattributed broker socket"
    return "Broker session"


def _display_label(
    table: dict[str, tuple[str, BrokerSessionDisplaySeverity]],
    value: object,
    fallback_label: str,
    fallback_severity: BrokerSessionDisplaySeverity,
) -> BrokerSessionDisplayLabel:
    if isinstance(value, str):
        match = table.get(value)
        if match is not None:
            return BrokerSessionDisplayLabel(label=match[0], severity=match[1])
    return BrokerSessionDisplayLabel(label=fallback_label, severity=fallback_severity)


def _broker_display_label(connection_state: object) -> BrokerSessionDisplayLabel:
    if not isinstance(connection_state, str) or not connection_state:
        return BrokerSessionDisplayLabel(label="Broker state not reported", severity="neutral")
    label, severity = _BROKER_CONNECTION_PRESENTATION.get(
        connection_state,
        (connection_state.replace("_", " ").capitalize(), "warning"),
    )
    return BrokerSessionDisplayLabel(label=label, severity=severity)


def _value_from(row: object, name: str) -> object:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _is_attention_code(value: object) -> TypeGuard[BrokerSessionAttentionCode]:
    return isinstance(value, str) and value in _ATTENTION_PRESENTATION


_IDENTITY_PRESENTATION: dict[str, tuple[str, BrokerSessionDisplaySeverity]] = {
    "bot": ("Bot session", "ok"),
    "system": ("System infrastructure", "info"),
    "orphaned_bot_socket": ("Orphaned bot socket", "critical"),
    "ghost": ("Unattributed broker socket", "warning"),
}

_RECENCY_PRESENTATION: dict[str, tuple[str, BrokerSessionDisplaySeverity]] = {
    "current": ("Live now", "ok"),
    "past_closed": ("Past session", "neutral"),
    "past_last_known": ("Last known", "neutral"),
    "unknown": ("Unproven now", "warning"),
}

_BROKER_CONNECTION_PRESENTATION: dict[str, tuple[str, BrokerSessionDisplaySeverity]] = {
    "connected": ("Broker connected", "ok"),
    "soft_lost": ("Broker feed lost", "warning"),
    "subscriptions_stale": ("Subscriptions stale", "warning"),
    "degraded_data_farm": ("Data farm degraded", "critical"),
    "reconnecting": ("Broker reconnecting", "warning"),
    "recovering": ("Broker recovering streams", "warning"),
    "hard_down": ("Broker recovery exhausted", "critical"),
    "disconnected": ("Broker disconnected", "warning"),
    "disabled": ("Broker disabled", "info"),
    "unknown": ("Broker state unproven", "warning"),
}

_RECOVERY_PRESENTATION: dict[str, tuple[str, BrokerSessionDisplaySeverity]] = {
    "HEALTHY": ("Healthy", "ok"),
    "LINK_INTERRUPTED": ("Link interrupted", "warning"),
    "RESTORING": ("Restoring streams", "warning"),
    "SOCKET_DOWN": ("Socket down", "critical"),
    "RECONNECTING": ("Reconnecting", "warning"),
    "HARD_DOWN": ("Hard down", "critical"),
}

_ATTENTION_PRESENTATION: dict[BrokerSessionAttentionCode, tuple[str, BrokerSessionDisplaySeverity, str]] = {
    "REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE": (
        "Registry offline; socket live",
        "warning",
        "The daemon registry says this process is offline, but the socket is still connected.",
    ),
    "STARTED_BUT_NO_SOCKET": (
        "Started; no socket",
        "warning",
        "The daemon registry reports a live bot process, but no gateway socket was observed.",
    ),
    "SOCKET_WITHOUT_LIVE_PID": (
        "No live PID",
        "critical",
        "The gateway socket is known to a bot, but the owning process PID is unavailable.",
    ),
    "ORPHANED_BOT_SOCKET": (
        "Orphaned bot socket",
        "critical",
        "A bot-owned broker socket appears to outlive its host process.",
    ),
    "GHOST_SOCKET": (
        "Unattributed broker socket",
        "warning",
        "A broker socket is present but cannot be attributed to a known bot run.",
    ),
    "GHOST_DETECTION_UNAVAILABLE": (
        "Socket attribution unavailable",
        "warning",
        "The daemon socket probe is unavailable, so current socket attribution cannot be proven.",
    ),
    "REGISTRY_SNAPSHOT_UNAVAILABLE": (
        "Registry snapshot unavailable",
        "warning",
        "The host daemon process registry could not be read.",
    ),
    "SOCKET_ATTRIBUTION_UNAVAILABLE": (
        "Socket attribution unavailable",
        "warning",
        "The socket probe did not provide enough evidence to attribute this session.",
    ),
    "CLIENT_SIGNAL_STALE": (
        "Client signal stale",
        "warning",
        "The latest broker runtime signal is older than the mirror freshness window.",
    ),
}


def summarize_broker_session_rows(
    rows: list[BrokerSessionRosterRow],
) -> BrokerSessionMirrorSummary:
    current = 0
    past = 0
    unknown = 0
    attention = 0
    for row in rows:
        if row.recency == "current":
            current += 1
        elif row.recency == "unknown":
            unknown += 1
        else:
            past += 1
        if row.attention_codes:
            attention += 1
    return BrokerSessionMirrorSummary(
        current=current,
        past=past,
        unknown=unknown,
        attention=attention,
    )


def _summary_from_raw_rows(rows: list[object]) -> dict[str, int]:
    current = 0
    past = 0
    unknown = 0
    attention = 0
    for row in rows:
        if isinstance(row, BrokerSessionRosterRow):
            if row.recency == "current":
                current += 1
            elif row.recency == "unknown":
                unknown += 1
            else:
                past += 1
            if row.attention_codes:
                attention += 1
            continue
        if not isinstance(row, dict):
            continue
        recency = row.get("recency")
        if recency == "current":
            current += 1
        elif recency == "unknown":
            unknown += 1
        else:
            past += 1
        attention_codes = row.get("attention_codes")
        if isinstance(attention_codes, list) and attention_codes:
            attention += 1
    return {
        "current": current,
        "past": past,
        "unknown": unknown,
        "attention": attention,
    }
