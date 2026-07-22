"""Durable transition rules for one Clerk account-emergency operation.

The journal is the authority for an emergency operation's progress.  This
small, pure fold is shared by the RPC-facing Clerk operations so no endpoint
can skip the intake fence or resume a terminal operation out of order.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

EmergencyPreLiquidationPhase = Literal["intake_closed", "bots_paused"]

_LIQUIDATION_RESUMABLE_PHASES = frozenset(
    {"bots_paused", "cancel_unconfirmed", "liquidation_planned", "liquidation_submitting"}
)


class AccountClerkEmergencySequenceError(RuntimeError):
    """A durable emergency operation did not reach its required predecessor."""


def emergency_operation_phases(
    entries: Iterable[object],
    *,
    operation_id: str,
) -> tuple[str, ...]:
    """Return the operation's serial phase history in journal order."""

    phases: list[str] = []
    for entry in entries:
        event = getattr(entry, "emergency_operation", None)
        if event is not None and event.operation_id == operation_id:
            phases.append(event.phase)
    return tuple(phases)


def pre_liquidation_transition_is_new(
    phases: tuple[str, ...],
    *,
    next_phase: EmergencyPreLiquidationPhase,
) -> bool:
    """Validate and classify the intake-close or bots-paused transition.

    ``False`` means a response was lost after the exact same durable transition
    and the caller may safely return success. Every other duplicate, stale, or
    reordered state is rejected instead of appending a misleading receipt.
    """

    predecessors = (
        ("authorization_issued",)
        if next_phase == "intake_closed"
        else ("authorization_issued", "intake_closed")
    )
    complete = (*predecessors, next_phase)
    if phases == predecessors:
        return True
    if phases == complete:
        return False
    raise AccountClerkEmergencySequenceError("CLERK_EMERGENCY_SEQUENCE_INVALID")


def require_liquidation_sequence(phases: tuple[str, ...]) -> None:
    """Require authorization, closed intake, and proved bot quiescence first."""

    required_prefix = ("authorization_issued", "intake_closed", "bots_paused")
    if phases[: len(required_prefix)] != required_prefix:
        raise AccountClerkEmergencySequenceError("CLERK_EMERGENCY_SEQUENCE_INVALID")
    if phases[-1] not in _LIQUIDATION_RESUMABLE_PHASES:
        raise AccountClerkEmergencySequenceError("CLERK_EMERGENCY_SEQUENCE_INVALID")


__all__ = [
    "AccountClerkEmergencySequenceError",
    "EmergencyPreLiquidationPhase",
    "emergency_operation_phases",
    "pre_liquidation_transition_is_new",
    "require_liquidation_sequence",
]
