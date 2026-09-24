"""The agent's presence at the coordinator: one interface, two transports.

A clerk agent presents itself to the fleet through exactly this seam. The
boot order it enforces is the ADR 0062 addendum's admission protocol:

1. the volume identity gate (registry row vs mounted marker) — before any
   profile or custody database opens;
2. session registration under the agent's instance id (routing epoch);
3. account reservation — the loser refuses to open authority;
4. (caller proceeds with its own authority boot);
5. confirmation of the binding observation, fenced by the instance and epoch
   that registered;
6. heartbeats, which observe; a refused beat re-registers and re-presents the
   grant it had confirmed (step 5 again), which is repair, not protocol — an
   observation itself still confirms nothing.

Two transports implement the same asynchronous interface: ``LocalPresence``
calls the in-process control service (single-host deployments, tests), and
``RemotePresence`` calls the coordinator's internal HTTP surface through the
pinned internal client (separate processes). Nothing here authenticates with
the ``worker_key`` over the wire — registration presents it as durable
identity, while the transport itself carries the environment-only service
token (audit 2026-09-13, finding 3).
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.broker.fleet.errors import (
    ClerkEndpointNotApproved,
    ClerkIdentityMismatch,
    ClerkLaneDraining,
    ClerkLaneRetired,
    ClerkNotFound,
    FleetAgentTokenRefused,
    FleetControlError,
    FleetProtocolIncompatible,
)
from app.broker.fleet.internal_http import (
    build_internal_client,
    enforce_private_http_target,
)
from app.broker.fleet.records import AccountAssignmentRecord
from app.broker.fleet.service import FleetControlService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """What an agent needs from its registration."""

    agent_instance_id: str
    routing_epoch: int


class FleetPresenceError(FleetControlError):
    """The coordinator could not be reached, or gave no answer about this lane.

    Transport failures, 5xx, and every refusal that is not the coordinator's
    typed word about this lane's own identity or admission: a restore
    ceremony's ``fleet_registry_recovery_pending``, or a 4xx carrying no
    fleet reason code at all (a process serving no fleet router). The
    agent's boot treats this exactly like the coordinator being absent (PRD
    FR-066): an already-confirmed lane may recover its last-effective
    binding; a first assignment, a changed binding and new enrolment refuse.
    The typed lane refusals are ``FleetPresenceRefused`` instead.
    """

    reason = "fleet_presence_unavailable"
    status_code = 503


class FleetPresenceRefused(FleetControlError):
    """A reachable coordinator refused this lane's identity or admission (#2320).

    Deliberately *not* a ``FleetPresenceError``: FR-066's offline fallback
    rides out a coordinator the lane cannot reach, never one that answered.
    Raised only for the typed codes in ``LANE_ADMISSION_REFUSALS`` — an
    unknown clerk, a mixed build refused by the version fences, an identity
    or endpoint the registry does not hold, a token the coordinator does not
    accept. Each is the coordinator's word about this lane, and a lane that
    read it as absence booted its last binding offline against the very
    coordinator that had just refused it (#2320, #2340).
    ``coordinator_reason`` carries that code.
    """

    reason = "fleet_presence_refused"
    status_code = 409

    def __init__(
        self,
        message: str,
        *,
        coordinator_reason: str,
        next_step: str | None = None,
    ) -> None:
        """Capture the refusal and the coordinator's own reason code."""
        super().__init__(message, next_step=next_step)
        self.coordinator_reason = coordinator_reason


#: The coordinator's typed refusals about this lane's own identity or
#: admission (#2320, #2340): an answer, never an outage. Every other refusal
#: a coordinator can give a presence call — ``fleet_registry_recovery_pending``
#: during a restore, or a 4xx with no fleet reason code — stays in the
#: unavailability family, so a restore ceremony never stops a live lane
#: booting its custody. The drain and retirement lessons have their own types.
LANE_ADMISSION_REFUSALS: frozenset[str] = frozenset(
    {
        ClerkNotFound.reason,
        FleetProtocolIncompatible.reason,
        ClerkIdentityMismatch.reason,
        ClerkEndpointNotApproved.reason,
        FleetAgentTokenRefused.reason,
    }
)


class FleetLaneDraining(FleetControlError):
    """The coordinator refused this call because the lane itself is drained.

    Deliberately *not* a ``FleetPresenceError``: FR-066's offline fallback
    catches that family, and a drained lane that fell into it would boot its
    stale evidence right back up — the exact resurrection #2155 closes. Both
    transports raise this one type whether the coordinator refused over the
    wire (reason ``clerk_lane_draining``) or in-process, so the lane marks
    its evidence drained and stays down instead of retrying or recovering.
    """

    reason = "fleet_lane_draining"
    status_code = 409


class FleetLaneRetired(FleetControlError):
    """The coordinator refused this call because the lane's own clerk is retired.

    Issue #2351: force-retire releases the account while a lane that could
    not be made quiet may still be running bots. The lane must learn that
    from its next beat — stop its bots, stop re-registering — so the refusal
    is typed on both transports, like ``FleetLaneDraining`` and for the same
    reason kept out of ``FleetPresenceError``: FR-066's offline fallback
    catches that family, and a retired lane must not boot its binding back.
    """

    reason = "fleet_lane_retired"
    status_code = 404


@contextmanager
def _lane_lessons() -> Iterator[None]:
    """Translate the coordinator's typed lane refusals for the in-process transport.

    The same translation the wire transport performs on the refusal's reason
    (``RemotePresence._post``): one lane-learnable type per lesson across
    both transports, neither of them the unavailability family FR-066's
    offline fallback catches.
    """
    try:
        yield
    except ClerkLaneDraining as exc:
        raise FleetLaneDraining(exc.message, next_step=exc.next_step) from exc
    except ClerkLaneRetired as exc:
        raise FleetLaneRetired(exc.message, next_step=exc.next_step) from exc


def _superseded_beat(clerk_id: str, agent_instance_id: str) -> FleetPresenceError:
    """A beat the coordinator accepted but that touched no session (#2259).

    ``touch_session`` refreshes only the row naming this instance id, so a
    beat from an instance the coordinator no longer holds — a re-registration
    whose reply was lost in an outage stored an id this process never
    adopted — lands as a 200 that refreshes nothing. Treating it as a
    success left the lane beating while the coordinator projected it
    unreachable until a restart. Raising the presence refusal routes it into
    the heartbeat's existing repair, which re-registers and adopts a session.
    """
    return FleetPresenceError(
        f"The fleet coordinator holds no session for clerk {clerk_id} "
        f"instance {agent_instance_id}; the heartbeat refreshed nothing.",
        next_step="Re-register so this process adopts the session the "
        "coordinator routes to.",
    )


class FleetPresence(Protocol):
    """The one interface both transports present."""

    async def expectation(self, *, clerk_id: str) -> dict[str, object]:
        """The registry's expected volume identity for this clerk."""
        ...

    async def verify_volume(self, *, clerk_id: str, volume_root: Path) -> None:
        """Prove the mounted root against the registry before authority."""
        ...

    async def register(
        self,
        *,
        clerk_id: str,
        worker_key: str,
        agent_instance_id: str,
        endpoint_ref: str | None,
        adapter_version: str,
        fleet_protocol_version: int,
    ) -> SessionInfo:
        """Install this process's session, bumping the routing epoch."""
        ...

    async def reserve(
        self, *, broker: str, clerk_id: str, external_account_id: str
    ) -> AccountAssignmentRecord:
        """Reserve the broker-qualified account for this clerk."""
        ...

    async def confirm(
        self,
        *,
        broker: str,
        clerk_id: str,
        external_account_id: str,
        binding_generation: int,
        agent_instance_id: str,
        routing_epoch: int,
        effective_profile_id: str | None,
        effective_revision: int | None,
    ) -> AccountAssignmentRecord:
        """Record the confirmed binding observation fenced by this session."""
        ...

    async def observe(
        self,
        *,
        clerk_id: str,
        agent_instance_id: str,
        reported_binding_generation: int | None = None,
        reported_account_id: str | None = None,
        reported_state: str | None = None,
        reported_summary: dict[str, object] | None = None,
    ) -> str | None:
        """Heartbeat: refresh observations; confirm nothing.

        Returns the clerk's lifecycle value as the coordinator holds it
        (``provisioned``/``draining``), or ``None`` when this coordinator
        carries no such news — the channel a live lane learns its drain
        through (#2155).
        """
        ...

    async def confirm_lane_quiet(
        self,
        *,
        clerk_id: str,
        agent_instance_id: str,
        routing_epoch: int,
        observed_at_ms: int,
        runner_idle: bool,
        broker_work_ended: bool,
        account_flat: bool,
        intents_resolved: bool,
    ) -> None:
        """Record this draining lane's answer about its own quiescence (#2154).

        A confirmation, fenced by the session that prepared it; an answer that
        leaves a condition outstanding is recorded exactly as a quiet one is.
        """
        ...

    async def close(self) -> None:
        """Release the transport."""
        ...


class LocalPresence:
    """Presence over the in-process control service (one host)."""

    def __init__(self, service: FleetControlService, *, volume_root: Path) -> None:
        """Bind the control service and the mounted root this process serves.

        Required, not optional: the in-process transport is the one that
        provably shares a filesystem with the volume, so it re-proves the
        root on every registration and reservation rather than trusting an
        adjacent call to have done it. ``RemotePresence`` supplies none by
        design — the coordinator never inspects an agent-local path (ADR 0062
        addendum 6); its agent proves the root locally in ``verify_volume``.
        """
        self._service = service
        self._volume_root = volume_root

    async def expectation(self, *, clerk_id: str) -> dict[str, object]:
        """The registry's expected identity, including its own registry id."""
        return self._service.clerk_volume_expectation(clerk_id)

    async def verify_volume(self, *, clerk_id: str, volume_root: Path) -> None:
        """Run the registry's fail-before-authority volume gate."""
        self._service.verify_clerk_volume(clerk_id=clerk_id, volume_root=volume_root)

    async def register(
        self,
        *,
        clerk_id: str,
        worker_key: str,
        agent_instance_id: str,
        endpoint_ref: str | None,
        adapter_version: str,
        fleet_protocol_version: int,
    ) -> SessionInfo:
        """Register through the service; the epoch is theirs to assign."""
        with _lane_lessons():
            session = self._service.register_agent_session(
                clerk_id=clerk_id,
                worker_key=worker_key,
                agent_instance_id=agent_instance_id,
                volume_root=self._volume_root,
                endpoint_ref=endpoint_ref,
                adapter_version=adapter_version,
                fleet_protocol_version=fleet_protocol_version,
            )
        return SessionInfo(
            agent_instance_id=session.agent_instance_id,
            routing_epoch=session.routing_epoch,
        )

    async def reserve(
        self, *, broker: str, clerk_id: str, external_account_id: str
    ) -> AccountAssignmentRecord:
        """Reserve through the service."""
        return self._service.reserve_assignment(
            broker=broker,
            clerk_id=clerk_id,
            external_account_id=external_account_id,
            volume_root=self._volume_root,
        )

    async def confirm(
        self,
        *,
        broker: str,
        clerk_id: str,
        external_account_id: str,
        binding_generation: int,
        agent_instance_id: str,
        routing_epoch: int,
        effective_profile_id: str | None,
        effective_revision: int | None,
    ) -> AccountAssignmentRecord:
        """Confirm through the service, translating the drain lesson.

        Same translation as ``register``: a draining refusal arrives as the
        lane-learnable type on both transports, so ``confirm_binding`` can
        mark the volume's evidence wherever the drain is first heard.
        """
        with _lane_lessons():
            return self._service.confirm_assignment(
                broker=broker,
                clerk_id=clerk_id,
                external_account_id=external_account_id,
                binding_generation=binding_generation,
                agent_instance_id=agent_instance_id,
                routing_epoch=routing_epoch,
                effective_profile_id=effective_profile_id,
                effective_revision=effective_revision,
            )

    async def observe(
        self,
        *,
        clerk_id: str,
        agent_instance_id: str,
        reported_binding_generation: int | None = None,
        reported_account_id: str | None = None,
        reported_state: str | None = None,
        reported_summary: dict[str, object] | None = None,
    ) -> str | None:
        """Observe through the service, translating the retirement lesson."""
        with _lane_lessons():
            observation = self._service.observe_session(
                clerk_id=clerk_id,
                agent_instance_id=agent_instance_id,
                reported_binding_generation=reported_binding_generation,
                reported_account_id=reported_account_id,
                reported_state=reported_state,
                reported_summary=reported_summary,
            )
        if not observation.touched:
            raise _superseded_beat(clerk_id, agent_instance_id)
        return observation.lifecycle_state.value

    async def confirm_lane_quiet(
        self,
        *,
        clerk_id: str,
        agent_instance_id: str,
        routing_epoch: int,
        observed_at_ms: int,
        runner_idle: bool,
        broker_work_ended: bool,
        account_flat: bool,
        intents_resolved: bool,
    ) -> None:
        """Confirm through the service."""
        self._service.confirm_lane_quiet(
            clerk_id=clerk_id,
            agent_instance_id=agent_instance_id,
            routing_epoch=routing_epoch,
            observed_at_ms=observed_at_ms,
            runner_idle=runner_idle,
            broker_work_ended=broker_work_ended,
            account_flat=account_flat,
            intents_resolved=intents_resolved,
        )

    async def close(self) -> None:
        """The service's lifetime is the caller's, not ours."""


class RemotePresence:
    """Presence over the coordinator's internal HTTP surface."""

    def __init__(self, *, base_url: str, agent_service_token: str) -> None:
        """Bind the internal destination and this agent's transport token."""
        enforce_private_http_target(base_url)
        self._base_url = base_url.rstrip("/")
        self._token = agent_service_token

    def _headers(self, clerk_id: str) -> dict[str, str]:
        """The internal auth headers: agent identity plus its service token."""
        return {
            "X-Fleet-Clerk-Id": clerk_id,
            "X-Fleet-Agent-Token": self._token,
        }

    async def _post(self, path: str, *, clerk_id: str, payload: dict[str, object]) -> dict[str, object]:
        """One authenticated internal POST, refusing redirects and proxies."""
        import httpx

        client = build_internal_client()
        try:
            response = await client.post(
                f"{self._base_url}{path}",
                json=payload,
                headers=self._headers(clerk_id),
            )
        except httpx.HTTPError as exc:
            raise FleetPresenceError(
                f"The fleet coordinator at {self._base_url} is unreachable: {exc}",
                next_step="An already-confirmed lane recovers its last-effective "
                "binding; first assignments and changed bindings wait for the "
                "coordinator.",
            ) from exc
        finally:
            await client.aclose()
        if response.status_code >= 500:
            raise FleetPresenceError(
                f"The fleet coordinator refused {path}: {response.status_code}.",
            )
        if response.status_code != 200:
            raise _refusal(response, f"The fleet coordinator refused {path}")
        try:
            body = response.json()
        except ValueError as exc:
            # A malformed 200 is the coordinator misbehaving, not an operator
            # refusal: translate so the heartbeat family (not a bare decode
            # error) handles it.
            raise FleetPresenceError(
                f"The fleet coordinator returned a malformed body for {path}: {exc}",
            ) from exc
        if not isinstance(body, dict):
            raise FleetPresenceError(
                f"The fleet coordinator returned an unexpected body for {path}.",
            )
        return body

    async def expectation(self, *, clerk_id: str) -> dict[str, object]:
        """Fetch the registry-served expectation over the internal surface."""
        import httpx

        client = build_internal_client()
        try:
            response = await client.get(
                f"{self._base_url}/internal/fleet/clerks/{clerk_id}/volume-expectation",
                headers=self._headers(clerk_id),
            )
        except httpx.HTTPError as exc:
            raise FleetPresenceError(
                f"The fleet coordinator at {self._base_url} is unreachable: {exc}",
            ) from exc
        finally:
            await client.aclose()
        if response.status_code >= 500:
            raise FleetPresenceError(
                "The fleet coordinator failed serving the volume expectation "
                f"for clerk {clerk_id}: {response.status_code}.",
            )
        if response.status_code != 200:
            raise _refusal(
                response,
                "The fleet coordinator refused to serve the volume expectation "
                f"for clerk {clerk_id}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FleetPresenceError(
                "The fleet coordinator returned a malformed volume expectation: "
                f"{exc}",
            ) from exc
        if not isinstance(body, dict):
            raise FleetPresenceError(
                "The fleet coordinator returned an unexpected volume expectation.",
            )
        return body

    async def verify_volume(self, *, clerk_id: str, volume_root: Path) -> None:
        """Verify the mounted root against the registry's expected identity.

        The coordinator serves the clerk's expected marker facts (nonsecret);
        the agent proves its own mounted root against them locally — the
        coordinator never inspects an agent-local path, and no registration
        proceeds without this gate having passed (audit 2026-09-13, finding 4).
        """
        expectation = await self.expectation(clerk_id=clerk_id)
        from app.broker.fleet import volume as volume_module

        volume_module.verify_volume_identity(
            volume_root,
            expected_broker=str(expectation["broker"]),
            expected_clerk_id=clerk_id,
            expected_volume_id=str(expectation["volume_id"]),
            expected_attestation_kind=str(expectation["attestation_kind"]),
            expected_attestation_id=str(expectation["attestation_id"]),
        )

    async def register(
        self,
        *,
        clerk_id: str,
        worker_key: str,
        agent_instance_id: str,
        endpoint_ref: str | None,
        adapter_version: str,
        fleet_protocol_version: int,
    ) -> SessionInfo:
        """Register over the internal surface."""
        body = await self._post(
            "/internal/fleet/sessions",
            clerk_id=clerk_id,
            payload={
                "clerk_id": clerk_id,
                "worker_key": worker_key,
                "agent_instance_id": agent_instance_id,
                "endpoint_ref": endpoint_ref,
                "adapter_version": adapter_version,
                "fleet_protocol_version": fleet_protocol_version,
            },
        )
        return SessionInfo(
            agent_instance_id=str(body["agent_instance_id"]),
            routing_epoch=int(body["routing_epoch"]),
        )

    async def reserve(
        self, *, broker: str, clerk_id: str, external_account_id: str
    ) -> AccountAssignmentRecord:
        """Reserve over the internal surface."""
        body = await self._post(
            "/internal/fleet/assignments/reserve",
            clerk_id=clerk_id,
            payload={
                "broker": broker,
                "clerk_id": clerk_id,
                "external_account_id": external_account_id,
            },
        )
        return _assignment_from_body(body)

    async def confirm(
        self,
        *,
        broker: str,
        clerk_id: str,
        external_account_id: str,
        binding_generation: int,
        agent_instance_id: str,
        routing_epoch: int,
        effective_profile_id: str | None,
        effective_revision: int | None,
    ) -> AccountAssignmentRecord:
        """Confirm over the internal surface."""
        body = await self._post(
            "/internal/fleet/assignments/confirm",
            clerk_id=clerk_id,
            payload={
                "broker": broker,
                "clerk_id": clerk_id,
                "external_account_id": external_account_id,
                "binding_generation": binding_generation,
                "agent_instance_id": agent_instance_id,
                "routing_epoch": routing_epoch,
                "effective_profile_id": effective_profile_id,
                "effective_revision": effective_revision,
            },
        )
        return _assignment_from_body(body)

    async def observe(
        self,
        *,
        clerk_id: str,
        agent_instance_id: str,
        reported_binding_generation: int | None = None,
        reported_account_id: str | None = None,
        reported_state: str | None = None,
        reported_summary: dict[str, object] | None = None,
    ) -> str | None:
        """Heartbeat over the internal surface."""
        body = await self._post(
            "/internal/fleet/sessions/observe",
            clerk_id=clerk_id,
            payload={
                "clerk_id": clerk_id,
                "agent_instance_id": agent_instance_id,
                "reported_binding_generation": reported_binding_generation,
                "reported_account_id": reported_account_id,
                "reported_state": reported_state,
                "reported_summary": reported_summary,
            },
        )
        observed = body.get("observed")
        if observed is not None and not isinstance(observed, bool):
            raise FleetPresenceError(
                "The fleet coordinator returned an unexpected observed flag "
                "for /internal/fleet/sessions/observe.",
            )
        if observed is False:
            raise _superseded_beat(clerk_id, agent_instance_id)
        lifecycle = body.get("lifecycle_state")
        # An older coordinator sends no key — no news, not an error. Anything
        # that is present but not a string is the coordinator misbehaving on
        # the one field this call exists to carry, so it refuses loudly
        # rather than being flattened into "no news".
        if lifecycle is None:
            return None
        if not isinstance(lifecycle, str):
            raise FleetPresenceError(
                "The fleet coordinator returned an unexpected lifecycle_state "
                "for /internal/fleet/sessions/observe.",
            )
        return lifecycle

    async def confirm_lane_quiet(
        self,
        *,
        clerk_id: str,
        agent_instance_id: str,
        routing_epoch: int,
        observed_at_ms: int,
        runner_idle: bool,
        broker_work_ended: bool,
        account_flat: bool,
        intents_resolved: bool,
    ) -> None:
        """Confirm over the internal surface."""
        await self._post(
            "/internal/fleet/lanes/confirm-quiet",
            clerk_id=clerk_id,
            payload={
                "clerk_id": clerk_id,
                "agent_instance_id": agent_instance_id,
                "routing_epoch": routing_epoch,
                "observed_at_ms": observed_at_ms,
                "runner_idle": runner_idle,
                "broker_work_ended": broker_work_ended,
                "account_flat": account_flat,
                "intents_resolved": intents_resolved,
            },
        )

    async def close(self) -> None:
        """Each call builds a client; nothing persists."""


def matches_service_token(presented: str, expected: str) -> bool:
    """Constant-time comparison for internal transport tokens."""
    return hmac.compare_digest(presented, expected)


def _refusal(response: object, message: str) -> FleetControlError:
    """Classify a coordinator's 4xx by its reason code, never its status class.

    The drain and retirement lessons (#2155, #2351) and the typed lane
    admission refusals (``LANE_ADMISSION_REFUSALS``, #2320) are answers about
    this lane; anything else — a restore's recovery-pending, a bodyless 4xx
    from a process serving no fleet router — stays unavailability, which
    FR-066 rides out.
    """
    reason = _error_reason(response)
    detail = _error_detail(response) or str(getattr(response, "status_code", ""))
    if reason == ClerkLaneDraining.reason:
        # Kept clearly out of the unavailability family: FR-066's offline
        # fallback would otherwise boot the drained binding right back up.
        return FleetLaneDraining(
            f"{message}: {detail}",
            next_step="Finish the drain ceremony on the coordinator; "
            "this lane marks its own evidence drained and stays down.",
        )
    if reason == ClerkLaneRetired.reason:
        # The lane stops its bots and its beat rather than falling back to
        # FR-066's offline boot or re-registering forever.
        return FleetLaneRetired(
            f"{message}: {detail}",
            next_step="Nothing re-enrols a retired lane; this lane "
            "stops its bots and decommissions.",
        )
    if reason in LANE_ADMISSION_REFUSALS:
        return FleetPresenceRefused(
            f"{message}: {detail} ({reason})",
            coordinator_reason=reason,
            next_step="The coordinator answered; a lane it refuses starts no "
            "new bot. Resolve the refusal it names.",
        )
    return FleetPresenceError(
        f"{message}: {detail}" + ("" if reason is None else f" ({reason})"),
        next_step="Retry once the coordinator is reachable; an "
        "already-confirmed lane may recover its last-effective binding "
        "meanwhile.",
    )


def _error_detail(response: object) -> str | None:
    """Best-effort reason extraction from a refusal body."""
    body = _error_body(response)
    if body is not None:
        message = body.get("message") or body.get("detail")
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            return str(message)
    return None


def _error_reason(response: object) -> str | None:
    """The typed ``reason`` code a fleet refusal body carries, if any."""
    body = _error_body(response)
    if body is not None:
        reason = body.get("reason")
        if isinstance(reason, str):
            return reason
    return None


def _error_body(response: object) -> dict[str, object] | None:
    """The refusal's JSON body when it parses as a flat object."""
    import json

    try:
        body = json.loads(getattr(response, "text", ""))
    except (ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _assignment_from_body(body: dict[str, object]) -> AccountAssignmentRecord:
    """Map the internal surface's assignment JSON to its record."""
    from app.broker.fleet.records import AssignmentState

    return AccountAssignmentRecord(
        broker=str(body["broker"]),
        canonical_external_account_id=str(body["canonical_external_account_id"]),
        clerk_id=str(body["clerk_id"]),
        assignment_generation=int(body["assignment_generation"]),
        state=AssignmentState(str(body["state"])),
        effective_profile_id=_optional_str(body, "effective_profile_id"),
        effective_revision=_optional_int(body, "effective_revision"),
        confirmed_binding_generation=_optional_int(body, "confirmed_binding_generation"),
        confirmed_profile_id=_optional_str(body, "confirmed_profile_id"),
        confirmed_revision=_optional_int(body, "confirmed_revision"),
        confirmed_at_ms=_optional_int(body, "confirmed_at_ms"),
        confirmed_agent_instance_id=_optional_str(body, "confirmed_agent_instance_id"),
        confirmed_routing_epoch=_optional_int(body, "confirmed_routing_epoch"),
        recorded_at_ms=int(body.get("recorded_at_ms", 0)),
        updated_at_ms=int(body.get("updated_at_ms", 0)),
    )


def _optional_str(body: dict[str, object], key: str) -> str | None:
    """Read one optional string field."""
    value = body.get(key)
    return None if value is None else str(value)


def _optional_int(body: dict[str, object], key: str) -> int | None:
    """Read one optional integer field."""
    value = body.get(key)
    return None if value is None else int(value)


__all__ = [
    "LANE_ADMISSION_REFUSALS",
    "FleetLaneDraining",
    "FleetLaneRetired",
    "FleetPresence",
    "FleetPresenceError",
    "FleetPresenceRefused",
    "LocalPresence",
    "RemotePresence",
    "SessionInfo",
    "matches_service_token",
]
