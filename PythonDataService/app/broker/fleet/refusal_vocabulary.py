"""The fleet control plane's closed refusal vocabulary (#2067).

Forty-two distinct reason codes exist on the fleet surface: the 37-class
``FleetControlError`` subclass closure (``FleetControlError`` itself plus
every subclass, wherever in ``app/`` it is declared) and 5 codes minted
without a ``FleetControlError`` at all -- a Pydantic-typed literal
(``qualification_market_status_unavailable``), a plain ``Exception`` used on
a raw-ASGI capacity-limiting path (``fleet_lane_capacity_exhausted``), a
routing-envelope validation failure raised before any typed refusal decision
exists (``command_envelope_invalid``), and two codes written directly as
JSON on raw-ASGI compatibility-retirement paths (``compatibility_read_retired``,
``compatibility_retirement_state_invalid``).

Per decision 9 (docs/superpowers/plans/2026-09-14-fleet-lane-e-frontend-fence-and-refusals.md
Task 7a) these reasons do **not** enter the exported OpenAPI contract -- the
frontend's copy map locks against a committed snapshot instead, exactly like
the broker-v2 panel vocabulary
(``scripts/regenerate_broker_v2_vocabulary_snapshot.py``,
``.github/workflows/ci.yml``'s ``broker-v2-vocabulary-contract`` job). This
module is the Python authority ``build_snapshot()``
(``scripts/regenerate_fleet_refusal_vocabulary_snapshot.py``) reads.

This module is a leaf: it imports nothing but ``errors.py``, which itself
imports nothing but the standard library (``tests/broker/fleet/test_import_isolation.py``
fences every module under ``app/broker/fleet`` against importing a provider
package). ``_subclass_closure`` walks whatever subclasses are registered in
the running process at call time -- it does not import the three external
subclass-declaring modules (``presence.py``, ``confirmation.py``, and the
Alpaca-specific ``app/broker/alpaca/clerk/fleet_boot.py``) itself. A caller
that wants the closure to include codes from an as-yet-unimported module
must import that module first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.broker.fleet.errors import FleetControlError


@dataclass(frozen=True, slots=True)
class RefusalFamily:
    """One reason code's pinned wire status and one-line meaning."""

    status_code: int
    meaning: str


def _subclass_closure(cls: type[FleetControlError]) -> frozenset[type[FleetControlError]]:
    """Every class in ``cls``'s inheritance tree, including ``cls`` itself.

    Reflects the live class registry (``__subclasses__()``), not static
    imports: a subclass declared in a module nothing has imported yet is
    invisible to this walk until something imports that module. See the
    module docstring's note on leaf-ness -- this function itself imports
    nothing beyond what the caller already has in scope.
    """
    closure = {cls}
    for subclass in cls.__subclasses__():
        closure |= _subclass_closure(subclass)
    return frozenset(closure)


#: The five reason codes minted without ever raising a ``FleetControlError``
#: instance -- so the subclass-closure walk cannot see them. Each is declared
#: here with its mint site so the module remains a complete census.
_MINTED_OUTSIDE_THE_CLOSURE: Final[frozenset[str]] = frozenset(
    {
        # app/routers/broker_clerks.py `_envelope_invalid` -- a §10.3 command
        # envelope failed validation before any routing decision, hence
        # before any FleetControlError could apply.
        "command_envelope_invalid",
        # app/broker/fleet/lane_runtime.py -- a retired compatibility-read
        # alias route, written directly as JSON on a raw-ASGI middleware path
        # with no Response object to raise through. Also minted (as a flat
        # JSONResponse, not raised) by app/routers/fleet_compatibility_reads.py's
        # `get_legacy_live_verdict` (#2140): unlike the evidence-gated ASGI
        # path, this one refuses unconditionally -- a standalone coordinator
        # never has a legitimate answer to measure.
        "compatibility_read_retired",
        # app/broker/fleet/lane_runtime.py -- the compatibility-retirement
        # evidence file is unreadable; same raw-ASGI constraint as above.
        "compatibility_retirement_state_invalid",
        # app/broker/fleet/lane_runtime.py `FleetLaneCapacityExhausted` -- a
        # plain `Exception` (not a FleetControlError) on the bounded-capacity
        # raw-ASGI admission path.
        "fleet_lane_capacity_exhausted",
        # app/routers/fleet_qualification.py -- a Pydantic-typed response
        # literal, never an exception at all.
        "qualification_market_status_unavailable",
    }
)

#: The complete fleet refusal vocabulary: the 37-class FleetControlError
#: subclass closure plus the 5 codes minted outside it. Sorted by code.
FLEET_REFUSAL_REASONS: Final[dict[str, RefusalFamily]] = {
    "broker_and_clerk_required": RefusalFamily(
        400, "A clerk-scoped request arrived without both a broker and a clerk identity."
    ),
    "broker_clerk_capability_unavailable": RefusalFamily(
        409, "The provider adapter does not declare the requested capability for this lane."
    ),
    "broker_not_supported": RefusalFamily(
        404, "No production adapter is registered for the named provider."
    ),
    "clerk_account_mismatch": RefusalFamily(
        409, "The account named by the command is not the clerk's effective account."
    ),
    "clerk_assignment_conflict": RefusalFamily(
        409, "Another clerk already owns the broker-qualified account assignment."
    ),
    "clerk_binding_generation_conflict": RefusalFamily(
        409, "The command's expected binding generation is not the clerk's current one."
    ),
    "clerk_broker_mismatch": RefusalFamily(
        409, "The path broker differs from the clerk's immutable broker."
    ),
    "clerk_command_quiet_required": RefusalFamily(
        409, "The clerk holds a dispatched routing attempt whose outcome the coordinator lost."
    ),
    "clerk_drain_deadline_pending": RefusalFamily(
        409, "The drain's calendar-derived deadline instant has not elapsed yet."
    ),
    "clerk_drain_required": RefusalFamily(
        409, "The ceremony requires a clerk whose door is closed: drain it first."
    ),
    "clerk_endpoint_not_approved": RefusalFamily(
        409, "A registration cites an endpoint the deployment has not approved."
    ),
    "clerk_identity_mismatch": RefusalFamily(
        409, "A session presented facts that contradict the clerk's registry identity."
    ),
    "clerk_lane_draining": RefusalFamily(
        409, "The clerk is draining; registration and confirmation refuse so the lane can learn its drain."
    ),
    "clerk_lane_quiet_unproven": RefusalFamily(
        409, "No lane-quiet confirmation answers the retirement gate; force-retire is the named exit."
    ),
    "clerk_lane_retired": RefusalFamily(
        404, "The lane's own clerk is retired; the lane stops its bots and never re-enrols."
    ),
    "clerk_not_found": RefusalFamily(
        404, "No clerk carries this identity, including malformed or retired ones."
    ),
    "clerk_reassignment_blocked": RefusalFamily(
        409, "Lane-to-lane reassignment is blocked until the drain ceremony can prove the drained lane's quiet."
    ),
    "clerk_routing_attempt_conflict": RefusalFamily(
        409, "An illegal transition or idempotency-key reuse on a routing attempt."
    ),
    "clerk_routing_outcome_unknown": RefusalFamily(
        503, "The routing attempt's outcome is unknown; retry the same identity."
    ),
    "clerk_unreachable": RefusalFamily(
        503, "The clerk's agent cannot currently be reached; retry when it recovers."
    ),
    "clerk_volume_already_registered": RefusalFamily(
        409, "A different active clerk already owns this volume identity."
    ),
    "clerk_volume_clone_detected": RefusalFamily(
        409, "A copied volume presented another clerk's identity marker."
    ),
    "clerk_volume_identity_mismatch": RefusalFamily(
        409, "The marker on the volume contradicts the registry's expectation."
    ),
    "clerk_volume_identity_missing": RefusalFamily(
        409, "The volume root carries no identity marker."
    ),
    "clerk_volume_mount_unproven": RefusalFamily(
        409, "The root is not the canonical mounted volume (symlinked or noncanonical)."
    ),
    "command_envelope_invalid": RefusalFamily(
        422, "A §10.3 command envelope failed validation before any routing decision."
    ),
    "compatibility_read_retired": RefusalFamily(
        410, "This compatibility read has retired; use its canonical broker and clerk route."
    ),
    "compatibility_retirement_state_invalid": RefusalFamily(
        503, "The compatibility retirement evidence is unreadable; host operator action is required."
    ),
    "confirmation_evidence_invalid": RefusalFamily(
        409, "The clerk's durable confirmation evidence is unreadable or contradicts its volume."
    ),
    "data_plane_control_secret_refused": RefusalFamily(
        403, "The presented data-plane control secret does not match the configured one."
    ),
    "fleet_agent_token_refused": RefusalFamily(
        403, "The presented X-Fleet-Agent-Token does not match the mapped token for this clerk."
    ),
    "fleet_boot_refused": RefusalFamily(
        409, "The lane may not open authority under the fleet's admission rules."
    ),
    "fleet_control_error": RefusalFamily(
        409, "An unclassified fleet refusal; no code in current use should raise the base directly."
    ),
    "fleet_control_plane_not_installed": RefusalFamily(
        503, "A required fleet control-plane component is not configured or installed on this process."
    ),
    "fleet_lane_draining": RefusalFamily(
        409, "The coordinator refused this presence call because the lane itself is drained."
    ),
    "fleet_lane_retired": RefusalFamily(
        404, "The coordinator refused this presence call because the lane's own clerk is retired."
    ),
    "fleet_lane_capacity_exhausted": RefusalFamily(
        503, "A lane's bounded request or stream budget could not admit the caller."
    ),
    "fleet_presence_unavailable": RefusalFamily(
        503, "The coordinator could not be reached or refused the agent's presence call."
    ),
    "fleet_protocol_incompatible": RefusalFamily(
        409, "An agent and coordinator speak different fleet protocol versions."
    ),
    "fleet_registry_recovery_pending": RefusalFamily(
        409, "A restored registry has not yet reconciled its original lanes."
    ),
    "fleet_registry_unavailable": RefusalFamily(
        503, "The fleet registry (the coordinator's own store) cannot be opened or read."
    ),
    "qualification_market_status_unavailable": RefusalFamily(
        503, "The market-liveness dependency this qualification probe requires is not available."
    ),
}


__all__ = [
    "FLEET_REFUSAL_REASONS",
    "FleetControlError",
    "RefusalFamily",
]
