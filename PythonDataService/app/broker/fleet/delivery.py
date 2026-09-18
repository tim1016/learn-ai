"""Lane delivery: one provider-operation path, local or over HTTP.

The coordinator forwards a routed operation to the serving agent through
exactly this seam (ADR 0062 addendum, item on delivery adapters; audit
2026-09-13, finding 8): ``HttpLaneDelivery`` talks to a real agent process
over the pinned internal client, and ``LocalLaneDelivery`` dispatches to an
in-process handler — the combined posture — preserving async event streams
without buffering them to completion, which is why the httpx ASGI transport
is never used here.

Both adapters deliver the same ``DeliveryRequest`` and both enforce the same
response-identity contract (FR-076): the serving runtime echoes the broker,
clerk, routing epoch and binding generation it actually served, and a mismatch
discovered after a possible dispatch is an *uncertain outcome* — the client is
isolated and the caller must reconcile by command identity, never resubmit
blindly (audit 2026-09-13, finding 7).
"""

from __future__ import annotations

import hmac
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field

import httpx
from starlette.datastructures import Headers

from app.broker.fleet.internal_http import (
    DEFAULT_INTERNAL_TIMEOUT_S,
    FleetStreamError,
    SseEvent,
    build_internal_client,
    enforce_private_http_target,
    iter_sse_from_response,
)
from app.broker.fleet.provider import ProviderOperation
from app.config import fleet_settings

logger = logging.getLogger(__name__)

IDENTITY_HEADER_PREFIX = "X-Fleet-"
COORDINATOR_TOKEN_HEADER = "X-Fleet-Coordinator-Token"


def lane_forward_is_authorized(headers: Headers) -> bool:
    """Whether this request proves itself as the coordinator's own forward.

    Two conditions, both required: the request presents the coordinator
    token (compared against the env-only value this agent was provisioned
    with, in constant time, never logged) **and** it carries the pinned
    fleet identity a coordinator dispatch always attaches — a token alone
    is forgeable; the pin pair is the proof. An absent or wrong token, or a
    token unaccompanied by both pins, returns ``False``.
    """
    supplied = headers.get(COORDINATOR_TOKEN_HEADER, "")
    expected = (fleet_settings.COORDINATOR_SERVICE_TOKEN or "").strip()
    if not expected or not supplied:
        return False
    if "x-fleet-clerk-id" not in headers or "x-fleet-broker" not in headers:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


class DeliveryIdentityMismatch(Exception):
    """The response's served identity contradicts the pinned attempt.

    Raised only after a possible dispatch: refusing the response does not
    undo the action, so the caller must treat the attempt as
    outcome-unknown and reconcile through the provider clerk's receipt.
    """


class DeliveryContractViolation(Exception):
    """A local handler returned a value outside the adapter's own contract.

    Distinct from :class:`DeliveryIdentityMismatch` (#2119): this fires when
    an in-process handler hands back the wrong Python type entirely -- a
    programming defect in the handler wiring, not the serving runtime naming
    the wrong broker, clerk, epoch or generation. It is raised before the
    identity echo is even inspected, so it carries no information about
    which lane answered and a caller must not describe it as a lane-identity
    problem.
    """


#: Path parameters may carry Starlette-style converters (``{order_ref:path}``);
#: binding substitutes the value and drops the converter, which is routing
#: syntax the receiving agent's own router applies to its side of the path.
_PATH_BINDING_PATTERN = re.compile(r"\{([a-z_][a-z0-9_]*)(?::[a-z]+)?\}")


def bind_path_template(template: str, params: Mapping[str, str]) -> str:
    """Bind one path template's parameters, tolerating route converters."""
    return _PATH_BINDING_PATTERN.sub(
        lambda match: params[match.group(1)], template
    )


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    """One routed operation, fully pinned before dispatch."""

    broker: str
    clerk_id: str
    operation: ProviderOperation
    path_params: Mapping[str, str]
    query: Mapping[str, str] = field(default_factory=dict)
    json_body: object | None = None
    routing_epoch: int | None = None
    binding_generation: int | None = None

    def agent_path(self) -> str:
        """The agent-side path for this operation with parameters bound."""
        return bind_path_template(self.operation.agent_path_template, self.path_params)

    def pinned_headers(self) -> dict[str, str]:
        """The identity headers the dispatch is fenced by."""
        headers = {
            f"{IDENTITY_HEADER_PREFIX}Broker": self.broker,
            f"{IDENTITY_HEADER_PREFIX}Clerk-Id": self.clerk_id,
        }
        if self.routing_epoch is not None:
            headers[f"{IDENTITY_HEADER_PREFIX}Routing-Epoch"] = str(self.routing_epoch)
        if self.binding_generation is not None:
            headers[f"{IDENTITY_HEADER_PREFIX}Binding-Generation"] = str(self.binding_generation)
        return headers


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """One complete (non-streaming) delivery outcome."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class StreamDeliveryResult:
    """One streaming delivery: headers plus parsed events.

    ``error_body`` carries the provider's refusal body when the agent
    answered a non-2xx — a refused stream is that refusal, not an empty
    successful SSE response.
    """

    status_code: int
    headers: Mapping[str, str]
    events: AsyncIterator[SseEvent]
    error_body: bytes | None = None


def verify_identity_echo(
    headers: Mapping[str, str], request: DeliveryRequest, *, status_code: int
) -> None:
    """Verify the serving runtime's identity echo against the pinned attempt.

    The broker and clerk echo are required on every delivery, and a pinned
    epoch or binding generation must be echoed too — a response that omits
    the echo of a pinned dimension is not verifiable and refuses (a missing
    echo can never distinguish the serving runtime from a reflection or a
    refusal in flight). Only unpinned dimensions go unchecked.

    One distinction (#2164): the echo headers exist only on a response the
    identity middleware actually served. A handler-raised 5xx — and an
    unrouted 404 — returns without them, and on a 4xx/5xx that absence is
    the *lane failing*, not the wrong lane answering. A wholly absent echo
    alongside a 4xx/5xx is therefore passed through so the caller sees the
    lane's own status and refusal body. Everything else refuses: a partial
    echo, a 3xx (the internal client never follows redirects, so an
    echoless redirect must never classify as a served response), a
    present-but-wrong echo at any status, and an absent echo on a 2xx —
    each is exactly the unverifiable-provenance refusal this check was
    written for.
    """
    # Header sources differ in case (httpx normalizes; a raw-ASGI dispatch
    # carries ASGI's lowercase names in a plain mapping), and the identity
    # contract is about the values, never the casing.
    lowered = {name.lower(): value for name, value in headers.items()}
    served_broker = lowered.get("x-fleet-broker")
    served_clerk = lowered.get("x-fleet-clerk-id")
    if (
        served_broker is None and served_clerk is None
    ) and status_code >= 400:
        # A wholly absent echo on a 4xx/5xx: the status and body are the
        # lane's own answer, and must not be masked as a 409 identity
        # mismatch with a retry instruction that can never succeed (#2164).
        # A partial echo is not "no echo" — it falls through and refuses.
        return
    if served_broker != request.broker:
        raise DeliveryIdentityMismatch(
            f"The response names broker {served_broker!r}; the attempt pinned "
            f"{request.broker!r}."
        )
    if served_clerk != request.clerk_id:
        raise DeliveryIdentityMismatch(
            f"The response names clerk {served_clerk!r}; the attempt pinned "
            f"{request.clerk_id!r}."
        )
    if request.routing_epoch is not None:
        served_epoch = lowered.get("x-fleet-routing-epoch")
        if served_epoch != str(request.routing_epoch):
            raise DeliveryIdentityMismatch(
                f"The response was served at epoch {served_epoch!r}; the attempt "
                f"pinned {request.routing_epoch}."
            )
    if request.binding_generation is not None:
        served_generation = lowered.get("x-fleet-binding-generation")
        if served_generation != str(request.binding_generation):
            raise DeliveryIdentityMismatch(
                f"The response was served at binding generation "
                f"{served_generation!r}; the attempt pinned "
                f"{request.binding_generation}."
            )


def validate_event_identity(event: SseEvent, request: DeliveryRequest) -> None:
    """Verify one streamed event's provenance fields (FR-076).

    Every event of a routed stream carries the serving runtime's identity as
    ``x-fleet-*`` fields — validated per event, not just on the response
    headers, so a lane that changes session or binding mid-stream cannot keep
    feeding the old stream's consumer. A missing field is as fatal as a wrong
    one: an event that cannot prove its origin is not delivered.
    """
    expected_fields: dict[str, str] = {
        "x-fleet-broker": request.broker,
        "x-fleet-clerk-id": request.clerk_id,
    }
    if request.routing_epoch is not None:
        expected_fields["x-fleet-routing-epoch"] = str(request.routing_epoch)
    if request.binding_generation is not None:
        expected_fields["x-fleet-binding-generation"] = str(request.binding_generation)
    for field_name, expected in expected_fields.items():
        served = event.identity.get(field_name)
        if served is None:
            raise DeliveryIdentityMismatch(
                f"A streamed event carries no {field_name} provenance field; "
                "the stream is closed rather than delivered unverified."
            )
        if served != expected:
            raise DeliveryIdentityMismatch(
                f"A streamed event names {field_name} {served!r}; the attempt "
                f"pinned {expected!r}. The stream is closed."
            )


async def _identity_validated_events(
    events: AsyncIterator[SseEvent], request: DeliveryRequest
) -> AsyncIterator[SseEvent]:
    """Yield only provenance-verified events; close the stream on violation.

    The mismatch propagates through the underlying iterator's ``finally``
    (response and client release), which is the stale-identity closure.
    """
    async for event in events:
        validate_event_identity(event, request)
        yield event


async def _empty_events() -> AsyncIterator[SseEvent]:
    """No events: the refused-stream carrier."""
    return
    yield  # pragma: no cover - makes this an async generator


class HttpLaneDelivery:
    """Delivery to a real agent process over the internal HTTP surface."""

    def __init__(self, *, base_url: str, coordinator_service_token: str) -> None:
        """Bind the approved agent destination and the coordinator token."""
        enforce_private_http_target(base_url)
        self._base_url = base_url.rstrip("/")
        self._token = coordinator_service_token

    async def deliver(
        self, request: DeliveryRequest, *, read_timeout_s: float | None = None
    ) -> DeliveryResult:
        """Forward one complete operation and verify the identity echo.

        ``read_timeout_s`` is the routed operation's own declared bound
        (``ProviderOperation.read_timeout_s``, issue #2204) -- ``None`` keeps
        the fleet default. This is the OUTER hop of a routed operation like
        ``bot_chart_history``: it must stay open at least as long as the
        agent's own request/response cycle can take, or the outer request
        expires first and discards work the agent already completed (see
        ``app.broker.fleet.history_batch`` for the paired inner bound).
        """
        client = build_internal_client(
            read_timeout_s=read_timeout_s
            if read_timeout_s is not None
            else DEFAULT_INTERNAL_TIMEOUT_S
        )
        try:
            response = await client.request(
                request.operation.method,
                f"{self._base_url}{request.agent_path()}",
                params=dict(request.query),
                json=request.json_body,
                headers={
                    **request.pinned_headers(),
                    COORDINATOR_TOKEN_HEADER: self._token,
                },
            )
            verify_identity_echo(
                response.headers, request, status_code=response.status_code
            )
            return DeliveryResult(
                status_code=response.status_code,
                headers=dict(response.headers),
                body=response.content,
            )
        finally:
            await client.aclose()

    async def stream(self, request: DeliveryRequest) -> StreamDeliveryResult:
        """Forward one streaming operation; events parse as they arrive."""
        client = build_internal_client(read_timeout_s=None)
        response = await client.send(
            client.build_request(
                request.operation.method,
                f"{self._base_url}{request.agent_path()}",
                params=dict(request.query),
                headers={
                    **request.pinned_headers(),
                    COORDINATOR_TOKEN_HEADER: self._token,
                },
            ),
            stream=True,
        )
        try:
            verify_identity_echo(
                response.headers, request, status_code=response.status_code
            )
        except DeliveryIdentityMismatch:
            # A refused echo still holds an open stream: release both ends
            # before surfacing the uncertain outcome.
            await response.aclose()
            await client.aclose()
            raise
        if response.status_code >= 400:
            error_body = await response.aread()
            await response.aclose()
            await client.aclose()
            return StreamDeliveryResult(
                status_code=response.status_code,
                headers=dict(response.headers),
                events=_empty_events(),
                error_body=error_body,
            )
        return StreamDeliveryResult(
            status_code=response.status_code,
            headers=dict(response.headers),
            events=_identity_validated_events(
                _closing_event_iterator(response, client), request
            ),
        )


async def _closing_event_iterator(
    response: httpx.Response, client: httpx.AsyncClient
) -> AsyncIterator[SseEvent]:
    """Yield parsed events, closing the response and client at stream end."""
    try:
        async for event in iter_sse_from_response(response):
            yield event
    except FleetStreamError:
        await response.aclose()
        await client.aclose()
        raise
    finally:
        await response.aclose()
        await client.aclose()


LocalHandler = Callable[[DeliveryRequest], "object"]


class LocalLaneDelivery:
    """Delivery to an in-process handler (the combined posture).

    The handler owns its response shape; the only contract is the identity
    echo — the handler answers from the runtime it actually serves, and this
    adapter verifies it exactly as the HTTP adapter does. Streaming handlers
    return an async iterator of events, delivered unbuffered.
    """

    def __init__(self, handler: LocalHandler) -> None:
        """Bind the in-process operation handler."""
        self._handler = handler

    async def deliver(
        self, request: DeliveryRequest, *, read_timeout_s: float | None = None
    ) -> DeliveryResult:
        """Dispatch in-process and verify the echo.

        ``read_timeout_s`` is accepted for signature parity with
        :meth:`HttpLaneDelivery.deliver` (``LaneRouter`` calls both the same
        way) and ignored: an in-process ASGI dispatch has no socket read
        timeout to widen.
        """
        result = self._handler(request)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, DeliveryResult):
            raise DeliveryContractViolation(
                "the in-process handler returned "
                f"{type(result).__name__}, not a DeliveryResult"
            )
        verify_identity_echo(result.headers, request, status_code=result.status_code)
        return result

    async def stream(self, request: DeliveryRequest) -> StreamDeliveryResult:
        """Dispatch in-process; events are forwarded provenance-verified."""
        result = self._handler(request)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, StreamDeliveryResult):
            raise DeliveryContractViolation(
                "the in-process handler returned "
                f"{type(result).__name__}, not a StreamDeliveryResult"
            )
        verify_identity_echo(result.headers, request, status_code=result.status_code)
        return StreamDeliveryResult(
            status_code=result.status_code,
            headers=result.headers,
            events=_identity_validated_events(result.events, request),
            error_body=result.error_body,
        )


__all__ = [
    "COORDINATOR_TOKEN_HEADER",
    "DeliveryContractViolation",
    "DeliveryIdentityMismatch",
    "DeliveryRequest",
    "DeliveryResult",
    "HttpLaneDelivery",
    "LocalLaneDelivery",
    "StreamDeliveryResult",
    "bind_path_template",
    "lane_forward_is_authorized",
    "validate_event_identity",
    "verify_identity_echo",
]
