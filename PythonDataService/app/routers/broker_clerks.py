"""The public clerk-scoped routing surface (PRD §10.1–§10.4, delivery B).

Everything here derives from the typed operation catalog: the coordinator's
forwarding allowlist, the route surface and the exported contract are one
declaration per provider (ADR 0062 addendum, item 4). Directory routes read
the registry projection; operation routes resolve the lane, validate the
§10.3 command envelope, persist the routing attempt for effectful
operations, forward through the lane delivery and verify the identity echo.

§10.4 refusal families surface with their pinned statuses — the typed error
hierarchy is the contract; this router invents no refusal of its own.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.broker.fleet.delivery import SseEvent
from app.broker.fleet.errors import (
    BrokerClerkCapabilityUnavailable,
    FleetControlError,
    FleetControlPlaneNotInstalled,
)
from app.broker.fleet.provider import (
    OperationIdempotency,
    OperationStream,
    ProviderOperation,
)
from app.broker.fleet.routing import CommandEnvelopeInvalid, LaneRouter
from app.security.data_plane_control import (
    require_data_plane_control_secret,
    require_data_plane_control_secret_always,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS

router = APIRouter(prefix="/api", tags=["broker-clerks"])

_CLERK_SCOPE = "/brokers/{broker}/clerks/{clerk_id}"

_ENVELOPE_KEY = "command_context"

#: Route converters (``{order_ref:path}``) are routing syntax; the handler's
#: parameter is the bare name.
_PATH_PARAM_NAME_PATTERN = re.compile(r"\{([a-z_][a-z0-9_]*)(?::[a-z]+)?\}")


def _fleet_service(request: Request) -> Any:
    """The registry-backed fleet service this coordinator surface routes on."""
    service = getattr(request.app.state, "fleet_service", None)
    if service is None:
        raise FleetControlPlaneNotInstalled(
            "no fleet registry is installed on this process",
            next_step="Confirm this process's lifespan installed "
            "app.state.fleet_service before routing fleet traffic to it.",
        )
    return service


def _lane_router(request: Request) -> LaneRouter:
    """The routing core bound to this process's delivery posture."""
    lane_router = getattr(request.app.state, "fleet_lane_router", None)
    if lane_router is None:
        raise FleetControlPlaneNotInstalled(
            "no fleet lane router is installed on this process",
            next_step="Confirm this process's lifespan installed "
            "app.state.fleet_lane_router before routing fleet traffic to it.",
        )
    return lane_router


def _refuse(error: FleetControlError) -> Response:
    """One §10.4 family, at its pinned status, with operator copy."""
    return JSONResponse(status_code=error.status_code, content=error.detail())


def _envelope_invalid(error: CommandEnvelopeInvalid) -> Response:
    """A §10.3 contract violation never reached a routing decision."""
    return JSONResponse(
        status_code=422,
        content={"reason": "command_envelope_invalid", "message": str(error)},
    )


# ---- Directory (§10.1/§10.2) -----------------------------------------------


@router.get(
    "/broker-clerks",
    dependencies=[Depends(require_data_plane_control_secret_always)],
    summary="Broker-neutral clerk fleet directory (§10.1)",
)
async def list_broker_clerks(request: Request) -> Response:
    """Every lane across production providers, from the registry projection."""
    return JSONResponse(_fleet_service(request).directory())


@router.get(
    "/brokers/{broker}/clerks",
    dependencies=[Depends(require_data_plane_control_secret_always)],
    summary="One broker's clerk lanes (§10.2)",
)
async def list_broker_clerks_for_broker(broker: str, request: Request) -> Response:
    """The same projection filtered to one provider."""
    service = _fleet_service(request)
    if broker not in service.adapters():
        from app.broker.fleet.errors import BrokerNotSupported

        return _refuse(
            BrokerNotSupported(
                f"No production adapter serves {broker!r}.",
                next_step="Use a provider this deployment supports; adding one "
                "is a reviewed code change, not a request parameter.",
            )
        )
    directory = service.directory()
    clerks = [
        clerk
        for clerk in directory["clerks"]
        if clerk.get("broker") == broker  # type: ignore[union-attr]
    ]
    return JSONResponse(
        {"observed_at_ms": directory["observed_at_ms"], "clerks": clerks}
    )


@router.get(
    "/brokers/{broker}/clerks/{clerk_id}",
    dependencies=[Depends(require_data_plane_control_secret_always)],
    summary="One clerk lane's directory entry",
)
async def describe_broker_clerk(
    broker: str, clerk_id: str, request: Request
) -> Response:
    """Lifecycle, session, capabilities and provider summary for one lane."""
    try:
        descriptor = _fleet_service(request).describe_clerk(clerk_id)
    except FleetControlError as error:
        return _refuse(error)
    fields = descriptor.public_fields()
    if fields.get("broker") != broker:
        from app.broker.fleet.errors import ClerkBrokerMismatch

        return _refuse(
            ClerkBrokerMismatch(
                f"Route broker {broker!r} does not match clerk {clerk_id}'s "
                f"immutable broker {fields.get('broker')!r}.",
            )
        )
    return JSONResponse(fields)


# ---- Audit read surface (#2104) --------------------------------------------


@router.get(
    "/broker-clerks/audit/routing-receipts",
    dependencies=[Depends(require_data_plane_control_secret_always)],
    summary="Routing-receipt audit trail, since a lower bound (#2104)",
)
async def list_routing_receipts_audit(
    request: Request,
    since_ms: int = Query(ge=0, le=MAX_TIMESTAMP_MS),
    clerk_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    before_ms: int | None = Query(None, ge=0, le=MAX_TIMESTAMP_MS),
    before_correlation_id: str | None = Query(None, min_length=1, max_length=64),
) -> Response:
    """Routing receipts at or after ``since_ms``, newest first.

    Read-only: no idempotency key, no command envelope, no ceremony. The
    durable audit trail (routing receipts, assignment history, session
    history) was otherwise reachable only by opening the coordinator's
    SQLite file by hand.

    The receipt ledger covers **commands only** (ADR 0063, #2153): routed
    streams open no receipt and a lane's own bot runner never enters the
    coordinator, so an empty or quiet window is not evidence of lane
    inactivity.

    ``before_ms``/``before_correlation_id`` continue a previous page's keyset
    (#2133) -- pass back a truncated page's ``next_before_ms``/
    ``next_before_correlation_id`` verbatim to walk the full window past
    ``limit`` instead of only ever reaching the newest page.
    """
    try:
        result = _fleet_service(request).list_routing_receipts(
            since_ms=since_ms,
            clerk_id=clerk_id,
            limit=limit,
            before_ms=before_ms,
            before_correlation_id=before_correlation_id,
        )
    except FleetControlError as error:
        return _refuse(error)
    except ValueError as exc:
        return JSONResponse(
            status_code=422,
            content={"reason": "audit_query_invalid", "message": str(exc)},
        )
    return JSONResponse(result)


# ---- Catalog-generated operation routes (§10.2/§10.3) ----------------------


def _lookup_operation(broker: str, operation: ProviderOperation, service: Any):
    """Resolve the operation against the named broker's own catalog."""
    adapter = service.adapters().get(broker)
    if adapter is None:
        from app.broker.fleet.errors import BrokerNotSupported

        raise BrokerNotSupported(f"No production adapter serves {broker!r}.")
    for candidate in adapter.operations():
        if candidate.route_key() == operation.route_key():
            return candidate
    raise BrokerClerkCapabilityUnavailable(
        f"Broker {broker!r} declares no operation at {operation.route_key()}.",
    )


#: Idle gap after which the public stream emits a keepalive comment, so
#: proxies and browsers do not time out an otherwise healthy lane whose
#: provider heartbeats as SSE comments the framing layer drops.
_KEEPALIVE_INTERVAL_S = 15.0


async def _sse_frames(events: AsyncIterator[SseEvent]) -> AsyncIterator[bytes]:
    """Re-emit verified events as public SSE frames, provenance included.

    Multiline data is re-emitted one ``data:`` field per line per the SSE
    framing — a joined blob would make the later lines unreadable fields.
    """
    iterator = events.__aiter__()
    while True:
        try:
            event = await asyncio.wait_for(
                iterator.__anext__(), timeout=_KEEPALIVE_INTERVAL_S
            )
        except TimeoutError:
            yield b": keepalive\n\n"
            continue
        except StopAsyncIteration:
            return
        lines = [f"event: {event.event}"]
        for name, value in sorted(event.identity.items()):
            lines.append(f"{name}: {value}")
        if event.id is not None:
            lines.append(f"id: {event.id}")
        lines.extend(f"data: {line}" for line in event.data.split("\n"))
        yield ("\n".join(lines) + "\n\n").encode()


def _make_operation_handler(operation: ProviderOperation) -> Any:
    """Build one route handler bound to a catalog operation.

    The signature carries every path parameter explicitly so the exported
    contract documents them, and every mutating operation declares an opaque
    JSON body — the provider clerk owns its request and response shapes.
    """
    is_stream = operation.stream == OperationStream.SSE
    path_param_names = [
        match.group(1)
        for match in _PATH_PARAM_NAME_PATTERN.finditer(operation.path_template)
    ]
    declares_body = operation.method != "GET"

    async def handler(**kwargs: Any) -> Response:
        request: Request = kwargs["request"]
        service = _fleet_service(request)
        lane = _lane_router(request)
        broker = str(kwargs["broker"])
        clerk_id = str(kwargs["clerk_id"])
        path_params = {name: str(kwargs[name]) for name in path_param_names}
        try:
            routed = _lookup_operation(broker, operation, service)
        except FleetControlError as error:
            return _refuse(error)
        query = dict(request.query_params)
        body: Any = kwargs.get("body")
        envelope = None
        try:
            if declares_body and isinstance(body, dict) and _ENVELOPE_KEY in body:
                from app.broker.fleet.routing import CommandEnvelope

                envelope = CommandEnvelope.from_body(body)
                forwarded = {
                    key: value
                    for key, value in body.items()
                    if key != _ENVELOPE_KEY
                }
                body = forwarded or None
            if is_stream:
                result = await lane.stream_read(
                    broker=broker,
                    clerk_id=clerk_id,
                    operation=routed,
                    path_params=path_params,
                    query=query,
                )
                if result.status_code >= 400:
                    # A refused stream is the provider's refusal, not an
                    # empty successful SSE response.
                    return Response(
                        status_code=result.status_code,
                        content=getattr(result, "error_body", None) or b"",
                        media_type="application/json",
                    )
                return StreamingResponse(
                    _sse_frames(result.events),
                    status_code=result.status_code,
                    media_type="text/event-stream",
                    headers={
                        key: value
                        for key, value in result.headers.items()
                        if key.lower().startswith("x-fleet-")
                        or key.lower() in ("cache-control", "x-accel-buffering")
                    },
                )
            if routed.idempotency == OperationIdempotency.READ:
                delivered = await lane.deliver_read(
                    broker=broker,
                    clerk_id=clerk_id,
                    operation=routed,
                    path_params=path_params,
                    query=query,
                    body=body,
                )
            else:
                delivered = await lane.deliver_command(
                    broker=broker,
                    clerk_id=clerk_id,
                    operation=routed,
                    path_params=path_params,
                    query=query,
                    body=body,
                    envelope=envelope,
                )
        except FleetControlError as error:
            return _refuse(error)
        except CommandEnvelopeInvalid as error:
            return _envelope_invalid(error)
        # §10.3: the public response echoes the target identity the lane
        # served — the frontend's provenance — alongside the routing receipt.
        headers = {
            key: value
            for key, value in delivered.headers.items()
            if key.lower() in ("content-type", "retry-after")
            or key.lower().startswith("x-fleet-")
        }
        if delivered.correlation_id:
            headers["X-Fleet-Correlation-Id"] = delivered.correlation_id
        if delivered.routing_state:
            headers["X-Fleet-Routing-State"] = delivered.routing_state
        return Response(
            status_code=delivered.status_code,
            content=delivered.body,
            headers=headers,
        )

    parameters = [
        inspect.Parameter(
            "request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request
        ),
        inspect.Parameter(
            "broker", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
        ),
        inspect.Parameter(
            "clerk_id", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
        ),
        *(
            inspect.Parameter(
                name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
            )
            for name in path_param_names
        ),
    ]
    if declares_body:
        # An opaque JSON body declaration: the exported contract and the
        # generated consumers must be able to represent the command body,
        # even though its schema belongs to the provider clerk.
        parameters.append(
            inspect.Parameter(
                "body",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=dict[str, Any] | None,
                default=Body(default=None),
            )
        )
    handler.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    handler.__doc__ = (
        f"Fleet-routed {operation.method} {operation.path_template} "
        f"({operation.capability.value})."
    )
    return handler


def register_catalog_operations(
    operations_by_broker: Mapping[str, frozenset[ProviderOperation]],
) -> None:
    """Mount one public route per distinct catalog operation.

    Two providers may declare the same public route shape; the handler
    resolves the named broker's own declaration at request time, so the
    route registers once and refuses brokers that do not declare it. Reads
    carry the always-on protected-read guard — these routes expose exactly
    the sensitive broker state the lane-local routers protect — and
    mutations the control-secret guard.
    """
    seen: set[tuple[str, str]] = set()
    for operations in operations_by_broker.values():
        for operation in operations:
            key = operation.route_key()
            if key in seen:
                continue
            seen.add(key)
            path = f"{_CLERK_SCOPE}{operation.path_template}"
            dependencies = (
                [Depends(require_data_plane_control_secret)]
                if operation.method != "GET"
                else [Depends(require_data_plane_control_secret_always)]
            )
            router.add_api_route(
                path,
                _make_operation_handler(operation),
                methods=[operation.method],
                name=f"fleet_{operation.operation_id}",
                dependencies=dependencies,
            )


__all__ = [
    "register_catalog_operations",
    "router",
]
