"""The refusal vocabulary of the fleet control plane.

PRD §10.4 pins these as stable families; every refusal carries a code-like
``reason`` plus backend-authored prose in the shape the rest of the data plane
uses — the Frontend renders ``reason`` through the shared ``receiptLabel``
pipe and never composes ``message`` or ``next_step`` itself.

Retry semantics, pinned with the status codes:

- ``404`` — terminal for this address; re-submitting the same request cannot
  succeed (unknown provider, unknown clerk).
- ``409`` — a state conflict; the caller must re-read the current state and
  re-prepare the action. Never retried blindly.
- ``503`` — retry-safe: the outcome is either unknown (retry with the *same*
  broker, clerk and idempotency identity) or the lane is currently
  unreachable (retry after the lane recovers).
"""

from __future__ import annotations

from typing import ClassVar


def flat_refusal_body(reason: str, message: str, *, next_step: str | None = None) -> dict[str, str]:
    """The one wire shape every fleet refusal carries (#2067, #2107).

    Backs :meth:`FleetControlError.detail` and is also the shape a handful of
    raw-ASGI middleware writers build directly, before any ``FleetControlError``
    instance exists (``lane_runtime.py``'s capacity and compatibility-retirement
    refusals, ``agent_identity.py``'s identity-mismatch refusal) -- #2107 found
    one of those hand-rolling the same dict as a string concatenation of JSON,
    invisible to the vocabulary snapshot's reason/status pin because that pin
    never inspected body shape. Routing every writer through this one function
    means a body can no longer drift from ``detail()``'s shape at any site.
    """
    body = {"reason": reason, "message": message}
    if next_step is not None:
        body["next_step"] = next_step
    return body


class FleetControlError(Exception):
    """A refusal the operator surface can render without inventing copy."""

    reason: ClassVar[str] = "fleet_control_error"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, next_step: str | None = None) -> None:
        """Capture the operator-facing message and optional next step."""
        super().__init__(message)
        self.message = message
        self.next_step = next_step

    def detail(self) -> dict[str, str]:
        """The wire body: reason, message, and next_step when present."""
        return flat_refusal_body(self.reason, self.message, next_step=self.next_step)


class BrokerAndClerkRequired(FleetControlError):
    """A clerk-scoped request without both path identities (PRD FR-070)."""

    reason: ClassVar[str] = "broker_and_clerk_required"
    status_code: ClassVar[int] = 400


class BrokerNotSupported(FleetControlError):
    """No production adapter is registered for the named provider."""

    reason: ClassVar[str] = "broker_not_supported"
    status_code: ClassVar[int] = 404


class ClerkNotFound(FleetControlError):
    """No clerk carries this identity — including malformed or retired ones."""

    reason: ClassVar[str] = "clerk_not_found"
    status_code: ClassVar[int] = 404


class ClerkBrokerMismatch(FleetControlError):
    """The path broker differs from the clerk's immutable broker (FR-071)."""

    reason: ClassVar[str] = "clerk_broker_mismatch"
    status_code: ClassVar[int] = 409


class ClerkUnreachable(FleetControlError):
    """The clerk's agent cannot currently be reached; retry when it recovers."""

    reason: ClassVar[str] = "clerk_unreachable"
    status_code: ClassVar[int] = 503


class ClerkIdentityMismatch(FleetControlError):
    """A session presented facts that contradict the clerk's registry identity."""

    reason: ClassVar[str] = "clerk_identity_mismatch"
    status_code: ClassVar[int] = 409


class ClerkVolumeIdentityMissing(FleetControlError):
    """The volume root carries no identity marker (PRD FR-026)."""

    reason: ClassVar[str] = "clerk_volume_identity_missing"
    status_code: ClassVar[int] = 409


class ClerkVolumeIdentityMismatch(FleetControlError):
    """The marker on the volume contradicts the registry's expectation."""

    reason: ClassVar[str] = "clerk_volume_identity_mismatch"
    status_code: ClassVar[int] = 409


class ClerkVolumeAlreadyRegistered(FleetControlError):
    """A different active clerk already owns this volume identity (FR-032)."""

    reason: ClassVar[str] = "clerk_volume_already_registered"
    status_code: ClassVar[int] = 409


class ClerkVolumeMountUnproven(FleetControlError):
    """The root is not the canonical mounted volume (symlinked or noncanonical)."""

    reason: ClassVar[str] = "clerk_volume_mount_unproven"
    status_code: ClassVar[int] = 409


class ClerkVolumeCloneDetected(FleetControlError):
    """A copied volume presented another clerk's marker (PRD FR-026)."""

    reason: ClassVar[str] = "clerk_volume_clone_detected"
    status_code: ClassVar[int] = 409


class ClerkBindingGenerationConflict(FleetControlError):
    """The command's expected binding generation is not the clerk's current one."""

    reason: ClassVar[str] = "clerk_binding_generation_conflict"
    status_code: ClassVar[int] = 409


class ClerkAccountMismatch(FleetControlError):
    """The account named by the command is not the clerk's effective account."""

    reason: ClassVar[str] = "clerk_account_mismatch"
    status_code: ClassVar[int] = 409


class ClerkAssignmentConflict(FleetControlError):
    """Another clerk owns the broker-qualified account assignment (FR-052)."""

    reason: ClassVar[str] = "clerk_assignment_conflict"
    status_code: ClassVar[int] = 409


class BrokerClerkCapabilityUnavailable(FleetControlError):
    """The provider adapter does not declare the requested capability, or
    cannot honor the served context for this lane (FR-006).
    """

    reason: ClassVar[str] = "broker_clerk_capability_unavailable"
    status_code: ClassVar[int] = 409


class ClerkRoutingOutcomeUnknown(FleetControlError):
    """The routing attempt's outcome is unknown; retry the same identity (FR-078)."""

    reason: ClassVar[str] = "clerk_routing_outcome_unknown"
    status_code: ClassVar[int] = 503


class ClerkRoutingAttemptConflict(FleetControlError):
    """An illegal transition or key reuse on a routing attempt.

    Not one of PRD §10.4's public route families; it is the internal refusal
    for settling a delivered attempt to anything weaker, or reusing an
    idempotency key under a different pinned attempt context (audit
    2026-09-13, finding 7).
    """

    reason: ClassVar[str] = "clerk_routing_attempt_conflict"
    status_code: ClassVar[int] = 409


class ClerkEndpointNotApproved(FleetControlError):
    """A registration cites an endpoint the deployment has not approved.

    Internal family (audit 2026-09-13, finding 4): destinations are
    deployment-owned rows; an agent may cite an approved reference but never
    choose or change where it points.
    """

    reason: ClassVar[str] = "clerk_endpoint_not_approved"
    status_code: ClassVar[int] = 409


class FleetProtocolIncompatible(FleetControlError):
    """An agent and coordinator speak different fleet protocol versions.

    Internal family (audit 2026-09-13, finding 6): incompatible builds refuse
    explicitly instead of a newer coordinator advertising operations an older
    agent cannot serve.
    """

    reason: ClassVar[str] = "fleet_protocol_incompatible"
    status_code: ClassVar[int] = 409


class ClerkDrainRequired(FleetControlError):
    """The ceremony requires a draining clerk (ADR 0063 Decisions 1/4).

    Internal family for the drain ceremony: a clerk that has served retires
    only through draining, and release/reassignment require a draining
    predecessor — the closed door is what makes the rest of the ceremony's
    evidence meaningful.
    """

    reason: ClassVar[str] = "clerk_drain_required"
    status_code: ClassVar[int] = 409


class ClerkCommandQuietRequired(FleetControlError):
    """The clerk holds a dispatch whose outcome the coordinator lost.

    ADR 0063 Decision 3: a lane cannot leave service past a routed command
    whose outcome is unknown — the attempt means "may have executed,
    reconcile by identity, never resubmit", and retiring the lane would
    strand that obligation with no owner.
    """

    reason: ClassVar[str] = "clerk_command_quiet_required"
    status_code: ClassVar[int] = 409


class ClerkLaneQuietUnproven(FleetControlError):
    """No lane-quiet confirmation answers the retirement gate (ADR 0063 Decision 2).

    The lane's own assertion that it holds no working order and runs no bot
    decision loop, fenced by the current session's instance and epoch. No
    provider can answer it yet (#2154), so the normal ``draining -> retired``
    path refuses rather than degrading to an attestation; ``force-retire`` is
    the separately named exit.
    """

    reason: ClassVar[str] = "clerk_lane_quiet_unproven"
    status_code: ClassVar[int] = 409


class ClerkDrainDeadlinePending(FleetControlError):
    """The drain's deadline instant has not elapsed yet (ADR 0063 Decision 5).

    The two-legged, calendar-derived bound that gates ``force-retire`` and
    the handover ceremonies; before it elapses the ceremony refuses and
    names the instant, never silently waits.
    """

    reason: ClassVar[str] = "clerk_drain_deadline_pending"
    status_code: ClassVar[int] = 409


class ClerkReassignmentBlocked(FleetControlError):
    """Lane-to-lane reassignment is blocked (ADR 0063 §4.1/§7.1).

    A drained lane must not be reassigned while a restart during a
    coordinator outage can resurrect its binding (#2155), and reassignment
    requires a draining predecessor — jointly exhaustive, so the ceremony is
    unreachable until the resurrection hole closes. Whole-machine migration
    is the preferred lane move and needs no successor (#2151).
    """

    reason: ClassVar[str] = "clerk_reassignment_blocked"
    status_code: ClassVar[int] = 409


class FleetRegistryUnavailable(FleetControlError):
    """The fleet registry cannot be opened or read (the coordinator's own store).

    Not one of PRD §10.4's public route families; it is the storage refusal
    the coordinator's surfaces translate, exactly as the profiles surface has
    ``profiles_database_unavailable``. Fails closed: no fleet read or command
    is granted off an unreadable registry.
    """

    reason: ClassVar[str] = "fleet_registry_unavailable"
    status_code: ClassVar[int] = 503


class FleetRegistryRecoveryPending(FleetControlError):
    """A restored registry has not yet reconciled its original lanes.

    A registry backup can be older than the durable confirmation evidence on
    a Clerk volume.  Until the host ceremony compares those sources, routing
    and assignment changes must remain closed rather than treating an old
    registry row as a new authority grant.
    """

    reason: ClassVar[str] = "fleet_registry_recovery_pending"
    status_code: ClassVar[int] = 409


class DataPlaneControlSecretRefused(FleetControlError):
    """The presented data-plane control secret does not match the configured one.

    Distinct from :class:`FleetControlPlaneNotInstalled`: the guard itself is
    configured and reachable, but this caller's credential is wrong. Wraps
    ``app/security/data_plane_control.py``'s wrong-secret 403, which
    previously answered with a bare ``{"detail": "..."}`` at the exact guard
    every browser call to the data plane passes through (#2067).
    """

    reason: ClassVar[str] = "data_plane_control_secret_refused"
    status_code: ClassVar[int] = 403


class FleetControlPlaneNotInstalled(FleetControlError):
    """A required fleet control-plane component is not configured or installed
    on this process.

    Covers the shared control secret (unset or a retired public value), the
    fleet registry, the lane router, the internal coordinator service, and
    the agent-token mapping -- a deployment/environment state, never the
    caller's fault, so it fails closed with 503 rather than the bare
    ``{"detail": "..."}`` these sites previously answered with (#2067).
    """

    reason: ClassVar[str] = "fleet_control_plane_not_installed"
    status_code: ClassVar[int] = 503


class FleetAgentTokenRefused(FleetControlError):
    """The presented ``X-Fleet-Agent-Token`` does not match the mapped token
    for this clerk (or no mapping names this clerk at all).

    Wraps ``app/routers/internal_fleet.py``'s wrong-token 403 (#2067).
    """

    reason: ClassVar[str] = "fleet_agent_token_refused"
    status_code: ClassVar[int] = 403


__all__ = [
    "BrokerAndClerkRequired",
    "BrokerClerkCapabilityUnavailable",
    "BrokerNotSupported",
    "ClerkAccountMismatch",
    "ClerkAssignmentConflict",
    "ClerkBindingGenerationConflict",
    "ClerkBrokerMismatch",
    "ClerkCommandQuietRequired",
    "ClerkDrainDeadlinePending",
    "ClerkDrainRequired",
    "ClerkEndpointNotApproved",
    "ClerkIdentityMismatch",
    "ClerkLaneQuietUnproven",
    "ClerkNotFound",
    "ClerkReassignmentBlocked",
    "ClerkRoutingAttemptConflict",
    "ClerkRoutingOutcomeUnknown",
    "ClerkUnreachable",
    "ClerkVolumeAlreadyRegistered",
    "ClerkVolumeCloneDetected",
    "ClerkVolumeIdentityMismatch",
    "ClerkVolumeIdentityMissing",
    "ClerkVolumeMountUnproven",
    "DataPlaneControlSecretRefused",
    "FleetAgentTokenRefused",
    "FleetControlError",
    "FleetControlPlaneNotInstalled",
    "FleetProtocolIncompatible",
    "FleetRegistryRecoveryPending",
    "FleetRegistryUnavailable",
    "flat_refusal_body",
]
