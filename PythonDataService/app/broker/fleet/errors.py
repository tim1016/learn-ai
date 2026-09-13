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
        body = {"reason": self.reason, "message": self.message}
        if self.next_step is not None:
            body["next_step"] = self.next_step
        return body


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
    """The provider adapter does not declare the requested capability (FR-006)."""

    reason: ClassVar[str] = "broker_clerk_capability_unavailable"
    status_code: ClassVar[int] = 409


class ClerkRoutingOutcomeUnknown(FleetControlError):
    """The routing attempt's outcome is unknown; retry the same identity (FR-078)."""

    reason: ClassVar[str] = "clerk_routing_outcome_unknown"
    status_code: ClassVar[int] = 503


class FleetRegistryUnavailable(FleetControlError):
    """The fleet registry cannot be opened or read (the coordinator's own store).

    Not one of PRD §10.4's public route families; it is the storage refusal
    the coordinator's surfaces translate, exactly as the profiles surface has
    ``profiles_database_unavailable``. Fails closed: no fleet read or command
    is granted off an unreadable registry.
    """

    reason: ClassVar[str] = "fleet_registry_unavailable"
    status_code: ClassVar[int] = 503


__all__ = [
    "BrokerAndClerkRequired",
    "BrokerClerkCapabilityUnavailable",
    "BrokerNotSupported",
    "ClerkAccountMismatch",
    "ClerkAssignmentConflict",
    "ClerkBindingGenerationConflict",
    "ClerkBrokerMismatch",
    "ClerkIdentityMismatch",
    "ClerkNotFound",
    "ClerkRoutingOutcomeUnknown",
    "ClerkUnreachable",
    "ClerkVolumeAlreadyRegistered",
    "ClerkVolumeCloneDetected",
    "ClerkVolumeIdentityMismatch",
    "ClerkVolumeIdentityMissing",
    "ClerkVolumeMountUnproven",
    "FleetControlError",
    "FleetRegistryUnavailable",
]
