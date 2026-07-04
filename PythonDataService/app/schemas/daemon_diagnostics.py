"""Daemon diagnostics wire models.

The report is backend-authored trader copy plus already-redacted technical
evidence. Frontend code may group by ids/enums, but must not render those raw
ids as the primary language of the surface.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

DaemonDiagnosticStatus = Literal["pass", "warn", "fail", "skip"]
DaemonDiagnosticScope = Literal["global", "account", "instance", "run"]
DaemonReportStatus = Literal["pass", "warn", "fail"]
DaemonTransport = Literal[
    "CONNECTED",
    "RETRYING",
    "UNREACHABLE",
    "AUTH_FAILED",
    "PROTOCOL_ERROR",
    "INCOMPATIBLE_CONTRACT",
]


class DaemonDiagnosticCategory(StrEnum):
    REACHABILITY = "reachability"
    AUTH = "auth"
    CONTRACT = "contract"
    CODE_FRESHNESS = "code_freshness"
    LEASE = "lease"
    BOOT = "boot"
    PROCESS_REGISTRY = "process_registry"
    ORPHANS = "orphans"
    SOCKET_PROBE = "socket_probe"
    PROCESS = "process"
    SOCKETS = "sockets"
    RUNTIME_FRESHNESS = "runtime_freshness"
    ARTIFACTS = "artifacts"


class DaemonDominantCondition(StrEnum):
    HEALTHY = "healthy"
    INSTANCE_HEALTHY = "instance_healthy"
    UNREACHABLE = "unreachable"
    RETRYING = "retrying"
    AUTH_FAILED = "auth_failed"
    MALFORMED_RESPONSE = "malformed_response"
    BUILD_MISMATCH = "build_mismatch"
    STALE_CODE = "stale_code"
    LEASE_STALE = "lease_stale"
    LEASE_UNWRITABLE = "lease_unwritable"
    BOOT_CHANGED = "boot_changed"
    REGISTRY_SNAPSHOT_UNAVAILABLE = "registry_snapshot_unavailable"
    ORPHANS_PRESENT = "orphans_present"
    SOCKET_PROBE_UNAVAILABLE = "socket_probe_unavailable"
    NOT_STARTED = "not_started"
    PROCESS_EXITED = "process_exited"
    REGISTRY_AMNESIA = "registry_amnesia"
    NO_SOCKET = "no_socket"
    ORPHANED_SOCKET = "orphaned_socket"
    RUNTIME_STALE = "runtime_stale"
    RUN_DIR_INVISIBLE = "run_dir_invisible"
    ACCOUNT_FROZEN = "account_frozen"
    CRASH_RETIRED_BLOCKED = "crash_retired_blocked"


class DiagnosticEvidence(BaseModel):
    """Structured, already-redacted facts for technical expanders."""

    model_config = ConfigDict(frozen=True)

    facts: dict[str, JsonValue] = Field(default_factory=dict)
    redacted: bool = False


class DaemonDiagnosticAction(BaseModel):
    """Optional check action.

    ``recovery_mutation`` is allowed only for data-plane-actuatable fixes. Host
    actions such as daemon restart stay in remediation copy, not buttons.
    """

    model_config = ConfigDict(frozen=True)

    action_id: str = Field(min_length=1, max_length=80)
    kind: Literal["recovery_mutation", "navigation"]
    label: str = Field(min_length=1, max_length=120)
    endpoint: str | None = None
    confirm: bool = False
    deep_link: str | None = None


class DaemonDiagnosticCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1, max_length=120)
    category: DaemonDiagnosticCategory
    status: DaemonDiagnosticStatus
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=500)
    technical_detail: str | None = None
    remediation: str | None = None
    scope: DaemonDiagnosticScope
    scope_ref: str | None = None
    evidence: DiagnosticEvidence | None = None
    action: DaemonDiagnosticAction | None = None


class DaemonDiagnosticHeadline(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=180)
    summary: str = Field(min_length=1, max_length=600)
    remediation: str | None = None


class DaemonInstanceDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str = Field(min_length=1)
    overall_status: DaemonReportStatus
    dominant_condition: DaemonDominantCondition
    headline: DaemonDiagnosticHeadline
    checks: list[DaemonDiagnosticCheck] = Field(default_factory=list)


class DaemonDiagnosticReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    overall_status: DaemonReportStatus
    transport: DaemonTransport
    dominant_condition: DaemonDominantCondition
    headline: DaemonDiagnosticHeadline
    checks: list[DaemonDiagnosticCheck] = Field(default_factory=list)
    per_instance: list[DaemonInstanceDiagnostic] = Field(default_factory=list)
    daemon_boot_id: str | None = None
    fetched_at_ms: int = Field(ge=0)
