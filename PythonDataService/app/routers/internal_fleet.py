"""The coordinator's internal fleet surface (agent-to-coordinator only).

Mounted under ``/internal/fleet`` — deliberately outside every public
``/api`` prefix — these routes are the machine surface an agent's presence
calls: volume expectations, session registration, heartbeats, reservation
and confirmation. Authentication is two-factor and environment-only: the
``X-Fleet-Agent-Token`` header must match the clerk's agent service token in
the coordinator's environment (``FLEET_AGENT_SERVICE_TOKENS_JSON``), and the
registration body presents the durable ``worker_key`` the registry itself
compares. The browser installation secret never appears here and is never
forwarded to an agent (PRD FR-046).

No unauthenticated fallback exists: a deployment without the token mapping
refuses every internal call. ``/internal`` stays outside the exported
OpenAPI contract's public surface and outside every public mount prefix.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.broker.fleet.errors import FleetControlError
from app.broker.fleet.presence import matches_service_token
from app.broker.fleet.records import AccountAssignmentRecord
from app.broker.fleet.service import FleetControlService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/fleet", tags=["internal-fleet"], include_in_schema=False)


class RegistrationRequest(BaseModel):
    """One agent registration: durable identity plus this process's facts."""

    clerk_id: str = Field(min_length=1)
    worker_key: str = Field(min_length=1)
    agent_instance_id: str | None = None
    endpoint_ref: str | None = None
    adapter_version: str | None = None
    fleet_protocol_version: int


class ObservationRequest(BaseModel):
    """One heartbeat: observations only, never a confirmation."""

    clerk_id: str = Field(min_length=1)
    agent_instance_id: str = Field(min_length=1)
    reported_binding_generation: int | None = None
    reported_account_id: str | None = None
    reported_state: str | None = None
    reported_summary: dict[str, Any] | None = None


class ReserveRequest(BaseModel):
    """One reservation of a broker-qualified account."""

    broker: str = Field(min_length=1)
    clerk_id: str = Field(min_length=1)
    external_account_id: str = Field(min_length=1)


class ConfirmRequest(BaseModel):
    """One confirmed binding observation, fenced by its session."""

    broker: str = Field(min_length=1)
    clerk_id: str = Field(min_length=1)
    external_account_id: str = Field(min_length=1)
    binding_generation: int = Field(ge=1)
    agent_instance_id: str = Field(min_length=1)
    routing_epoch: int = Field(ge=1)
    effective_profile_id: str | None = None
    effective_revision: int | None = None


def _service(request: Request) -> FleetControlService:
    """The coordinator's fleet service, installed by the lifespan."""
    service = getattr(request.app.state, "fleet_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="fleet coordinator not installed")
    return service


def _authorized_agent(request: Request, clerk_id: str, token: str) -> None:
    """Refuse any agent whose transport token is not the mapped one."""
    mapping_text = getattr(request.app.state, "fleet_agent_tokens_text", "")
    try:
        mapping = json.loads(mapping_text) if mapping_text else {}
    except ValueError:
        # A malformed mapping is a misconfigured coordinator: fail closed.
        raise HTTPException(status_code=503, detail="agent token mapping unreadable")
    if not isinstance(mapping, dict):
        raise HTTPException(status_code=503, detail="agent token mapping unreadable")
    expected = mapping.get(clerk_id)
    if (
        not isinstance(expected, str)
        or not expected
        or not matches_service_token(token, expected)
    ):
        logger.warning(
            "internal fleet call refused for unknown or mismatched agent token",
            extra={"clerk_id": clerk_id},
        )
        raise HTTPException(status_code=403, detail="agent token refused")


def _refuse(control_error: FleetControlError) -> HTTPException:
    """Translate one typed fleet refusal to its pinned HTTP shape."""
    return HTTPException(status_code=control_error.status_code, detail=control_error.detail())


def _assignment_body(assignment: AccountAssignmentRecord) -> dict[str, Any]:
    """The assignment JSON both internal callers and agents read."""
    return {
        "broker": assignment.broker,
        "canonical_external_account_id": assignment.canonical_external_account_id,
        "clerk_id": assignment.clerk_id,
        "assignment_generation": assignment.assignment_generation,
        "state": str(assignment.state),
        "effective_profile_id": assignment.effective_profile_id,
        "effective_revision": assignment.effective_revision,
        "confirmed_binding_generation": assignment.confirmed_binding_generation,
        "confirmed_profile_id": assignment.confirmed_profile_id,
        "confirmed_revision": assignment.confirmed_revision,
        "confirmed_at_ms": assignment.confirmed_at_ms,
        "confirmed_agent_instance_id": assignment.confirmed_agent_instance_id,
        "confirmed_routing_epoch": assignment.confirmed_routing_epoch,
        "recorded_at_ms": assignment.recorded_at_ms,
        "updated_at_ms": assignment.updated_at_ms,
    }


@router.get("/clerks/{clerk_id}/volume-expectation")
async def volume_expectation(
    clerk_id: str,
    request: Request,
    x_fleet_agent_token: Annotated[str | None, Header(alias="X-Fleet-Agent-Token")] = None,
) -> dict[str, Any]:
    """Serve the clerk's expected volume identity for local agent proof."""
    _authorized_agent(request, clerk_id, x_fleet_agent_token or "")
    try:
        return _service(request).clerk_volume_expectation(clerk_id)
    except FleetControlError as exc:
        raise _refuse(exc) from exc


@router.post("/sessions")
async def register_session(
    payload: RegistrationRequest,
    request: Request,
    x_fleet_agent_token: Annotated[str | None, Header(alias="X-Fleet-Agent-Token")] = None,
) -> dict[str, Any]:
    """Install one agent session under a fresh routing epoch."""
    _authorized_agent(request, payload.clerk_id, x_fleet_agent_token or "")
    try:
        session = _service(request).register_agent_session(
            clerk_id=payload.clerk_id,
            worker_key=payload.worker_key,
            agent_instance_id=payload.agent_instance_id,
            endpoint_ref=payload.endpoint_ref,
            adapter_version=payload.adapter_version,
            fleet_protocol_version=payload.fleet_protocol_version,
        )
    except FleetControlError as exc:
        raise _refuse(exc) from exc
    return {
        "agent_instance_id": session.agent_instance_id,
        "routing_epoch": session.routing_epoch,
    }


@router.post("/sessions/observe")
async def observe_session(
    payload: ObservationRequest,
    request: Request,
    x_fleet_agent_token: Annotated[str | None, Header(alias="X-Fleet-Agent-Token")] = None,
) -> dict[str, Any]:
    """Record one heartbeat; observations never confirm anything."""
    _authorized_agent(request, payload.clerk_id, x_fleet_agent_token or "")
    try:
        touched = _service(request).observe_session(
            clerk_id=payload.clerk_id,
            agent_instance_id=payload.agent_instance_id,
            reported_binding_generation=payload.reported_binding_generation,
            reported_account_id=payload.reported_account_id,
            reported_state=payload.reported_state,
            reported_summary=payload.reported_summary,
        )
    except FleetControlError as exc:
        raise _refuse(exc) from exc
    return {"observed": touched}


@router.post("/assignments/reserve")
async def reserve_assignment(
    payload: ReserveRequest,
    request: Request,
    x_fleet_agent_token: Annotated[str | None, Header(alias="X-Fleet-Agent-Token")] = None,
) -> dict[str, Any]:
    """Reserve the broker-qualified account for the authenticated clerk."""
    _authorized_agent(request, payload.clerk_id, x_fleet_agent_token or "")
    try:
        assignment = _service(request).reserve_assignment(
            broker=payload.broker,
            clerk_id=payload.clerk_id,
            external_account_id=payload.external_account_id,
        )
    except FleetControlError as exc:
        raise _refuse(exc) from exc
    return _assignment_body(assignment)


@router.post("/assignments/confirm")
async def confirm_assignment(
    payload: ConfirmRequest,
    request: Request,
    x_fleet_agent_token: Annotated[str | None, Header(alias="X-Fleet-Agent-Token")] = None,
) -> dict[str, Any]:
    """Record the confirmed binding observation fenced by its session."""
    _authorized_agent(request, payload.clerk_id, x_fleet_agent_token or "")
    try:
        assignment = _service(request).confirm_assignment(
            broker=payload.broker,
            clerk_id=payload.clerk_id,
            external_account_id=payload.external_account_id,
            binding_generation=payload.binding_generation,
            agent_instance_id=payload.agent_instance_id,
            routing_epoch=payload.routing_epoch,
            effective_profile_id=payload.effective_profile_id,
            effective_revision=payload.effective_revision,
        )
    except FleetControlError as exc:
        raise _refuse(exc) from exc
    return _assignment_body(assignment)


__all__ = ["router"]
