"""Fleet record shapes — storage rows and public projections.

Frozen dataclasses only. Every timestamp is ``int64 ms UTC`` (ADR 0022); no
record carries a credential value, fragment, length, environment name or
secret-derived hash. ``worker_key`` is durable registry identity and never
appears in a public projection (PRD FR-012).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class StoredLifecycleState(StrEnum):
    """The durable states the registry persists for a clerk.

    ``starting`` / ``ready`` / ``degraded`` / ``unreachable`` are *projections*
    over the current session and assignment (PRD FR-080/081): a stored flag
    would let a historical acknowledgement present itself as current liveness.
    """

    PROVISIONED = "provisioned"
    DRAINING = "draining"
    RETIRED = "retired"


class ClerkLifecycleState(StrEnum):
    """The full public lifecycle vocabulary (PRD FR-080)."""

    PROVISIONED = "provisioned"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    DRAINING = "draining"
    RETIRED = "retired"


class AssignmentState(StrEnum):
    """An assignment is reserved, effective, or terminally released.

    There is no expiry transition: ``released`` is reached only through the
    host ceremony, never through heartbeat loss or coordinator restart
    (PRD FR-054/056).
    """

    RESERVED = "reserved"
    EFFECTIVE = "effective"
    RELEASED = "released"


class RoutingReceiptState(StrEnum):
    DELIVERED = "delivered"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class ClerkRecord:
    clerk_id: str
    broker: str
    worker_key: str
    display_label: str
    volume_id: str
    volume_root: str
    volume_attestation_kind: str
    volume_attestation_id: str
    lifecycle_state: StoredLifecycleState
    created_at_ms: int
    retired_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ClerkSessionRecord:
    broker: str
    clerk_id: str
    agent_instance_id: str
    routing_epoch: int
    started_at_ms: int
    last_seen_at_ms: int
    reported_binding_generation: int | None = None
    reported_account_id: str | None = None
    reported_state: str | None = None


@dataclass(frozen=True, slots=True)
class AccountAssignmentRecord:
    broker: str
    canonical_external_account_id: str
    clerk_id: str
    assignment_generation: int
    state: AssignmentState
    effective_profile_id: str | None = None
    effective_revision: int | None = None
    recorded_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class RoutingReceiptRecord:
    correlation_id: str
    broker: str
    clerk_id: str
    operation_kind: str
    nonsecret_target_ref: str
    idempotency_key: str
    state: RoutingReceiptState
    upstream_receipt_ref: str | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class VolumeMarker:
    """The versioned identity marker written at a clerk volume's root.

    Nonsecret by construction: it names identities and the external mount
    attestation, never credentials (PRD FR-024).
    """

    marker_version: int
    broker: str
    clerk_id: str
    volume_id: str
    attestation_kind: str
    attestation_id: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ClerkDescriptor:
    """One directory entry — the broker-neutral public projection of a clerk.

    Carries identity, lifecycle, generations, declared capabilities and the
    provider-authored summary (PRD FR-033). Deliberately absent: ``worker_key``,
    internal endpoints, internal credentials, and every financial quantity —
    the directory computes no balances, positions, P&L, exposure or risk
    (PRD FR-034).
    """

    broker: str
    clerk_id: str
    display_label: str
    lifecycle_state: ClerkLifecycleState
    volume_id: str
    last_seen_at_ms: int | None
    routing_epoch: int | None
    effective_binding_generation: int | None
    capabilities: tuple[str, ...]
    provider_summary: Mapping[str, object] | None
    observed_at_ms: int

    def public_fields(self) -> dict[str, object]:
        """The wire shape; ``provider_summary`` is provider-authored and opaque."""
        return {
            "broker": self.broker,
            "clerk_id": self.clerk_id,
            "display_label": self.display_label,
            "lifecycle_state": str(self.lifecycle_state),
            "volume_id": self.volume_id,
            "last_seen_at_ms": self.last_seen_at_ms,
            "routing_epoch": self.routing_epoch,
            "effective_binding_generation": self.effective_binding_generation,
            "capabilities": list(self.capabilities),
            "provider_summary": dict(self.provider_summary) if self.provider_summary else None,
            "observed_at_ms": self.observed_at_ms,
        }


__all__ = [
    "AccountAssignmentRecord",
    "AssignmentState",
    "ClerkDescriptor",
    "ClerkLifecycleState",
    "ClerkRecord",
    "ClerkSessionRecord",
    "RoutingReceiptRecord",
    "RoutingReceiptState",
    "StoredLifecycleState",
    "VolumeMarker",
]
