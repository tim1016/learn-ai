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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.broker.fleet.errors import FleetControlError
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
    """The coordinator could not be reached or refused the presence call.

    The agent's boot treats this exactly like the coordinator being absent
    (PRD FR-066): an already-confirmed lane may recover its last-effective
    binding; a first assignment, a changed binding and new enrolment refuse.
    """

    reason = "fleet_presence_unavailable"
    status_code = 503


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
    ) -> None:
        """Heartbeat: refresh observations; confirm nothing."""
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
        """Confirm through the service."""
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
    ) -> None:
        """Observe through the service."""
        self._service.observe_session(
            clerk_id=clerk_id,
            agent_instance_id=agent_instance_id,
            reported_binding_generation=reported_binding_generation,
            reported_account_id=reported_account_id,
            reported_state=reported_state,
            reported_summary=reported_summary,
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
            detail = _error_detail(response)
            raise FleetPresenceError(
                f"The fleet coordinator refused {path}: {detail or response.status_code}",
                next_step="Retry once the coordinator is reachable; an "
                "already-confirmed lane may recover its last-effective "
                "binding meanwhile.",
            )
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
            raise FleetPresenceError(
                "The fleet coordinator refused to serve the volume expectation "
                f"for clerk {clerk_id}: {_error_detail(response) or response.status_code}",
                next_step="Retry the volume-expectation call once the "
                "coordinator is reachable.",
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
    ) -> None:
        """Heartbeat over the internal surface."""
        await self._post(
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

    async def close(self) -> None:
        """Each call builds a client; nothing persists."""


def matches_service_token(presented: str, expected: str) -> bool:
    """Constant-time comparison for internal transport tokens."""
    return hmac.compare_digest(presented, expected)


def _error_detail(response: object) -> str | None:
    """Best-effort reason extraction from a refusal body."""
    import json

    try:
        body = json.loads(getattr(response, "text", ""))
    except (ValueError, TypeError):
        return None
    if isinstance(body, dict):
        message = body.get("message") or body.get("detail")
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            return str(message)
    return None


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
    "FleetPresence",
    "FleetPresenceError",
    "LocalPresence",
    "RemotePresence",
    "SessionInfo",
    "matches_service_token",
]
