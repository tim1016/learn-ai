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

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field

from app.broker.fleet.internal_http import (
    FleetStreamError,
    SseEvent,
    build_internal_client,
    iter_sse_from_response,
)
from app.broker.fleet.provider import ProviderOperation

logger = logging.getLogger(__name__)

IDENTITY_HEADER_PREFIX = "X-Fleet-"
COORDINATOR_TOKEN_HEADER = "X-Fleet-Coordinator-Token"


class DeliveryIdentityMismatch(Exception):
    """The response's served identity contradicts the pinned attempt.

    Raised only after a possible dispatch: refusing the response does not
    undo the action, so the caller must treat the attempt as
    outcome-unknown and reconcile through the provider clerk's receipt.
    """


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
        return self.operation.agent_path_template.format(**self.path_params)

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
    """One streaming delivery: headers plus parsed events."""

    status_code: int
    headers: Mapping[str, str]
    events: AsyncIterator[SseEvent]


def verify_identity_echo(
    headers: Mapping[str, str], request: DeliveryRequest
) -> None:
    """Verify the serving runtime's identity echo against the pinned attempt.

    The broker and clerk echo are required on every delivery, and a pinned
    epoch or binding generation must be echoed too — a response that omits
    the echo of a pinned dimension is not verifiable and refuses (a missing
    echo can never distinguish the serving runtime from a reflection or a
    refusal in flight). Only unpinned dimensions go unchecked.
    """
    served_broker = headers.get(f"{IDENTITY_HEADER_PREFIX}Broker")
    served_clerk = headers.get(f"{IDENTITY_HEADER_PREFIX}Clerk-Id")
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
        served_epoch = headers.get(f"{IDENTITY_HEADER_PREFIX}Routing-Epoch")
        if served_epoch != str(request.routing_epoch):
            raise DeliveryIdentityMismatch(
                f"The response was served at epoch {served_epoch!r}; the attempt "
                f"pinned {request.routing_epoch}."
            )
    if request.binding_generation is not None:
        served_generation = headers.get(f"{IDENTITY_HEADER_PREFIX}Binding-Generation")
        if served_generation != str(request.binding_generation):
            raise DeliveryIdentityMismatch(
                f"The response was served at binding generation "
                f"{served_generation!r}; the attempt pinned "
                f"{request.binding_generation}."
            )


class HttpLaneDelivery:
    """Delivery to a real agent process over the internal HTTP surface."""

    def __init__(self, *, base_url: str, coordinator_service_token: str) -> None:
        """Bind the approved agent destination and the coordinator token."""
        self._base_url = base_url.rstrip("/")
        self._token = coordinator_service_token

    async def deliver(self, request: DeliveryRequest) -> DeliveryResult:
        """Forward one complete operation and verify the identity echo."""
        client = build_internal_client()
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
            verify_identity_echo(response.headers, request)
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
            verify_identity_echo(response.headers, request)
        except DeliveryIdentityMismatch:
            # A refused echo still holds an open stream: release both ends
            # before surfacing the uncertain outcome.
            await response.aclose()
            await client.aclose()
            raise
        return StreamDeliveryResult(
            status_code=response.status_code,
            headers=dict(response.headers),
            events=_closing_event_iterator(response, client),
        )


async def _closing_event_iterator(
    response, client
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

    async def deliver(self, request: DeliveryRequest) -> DeliveryResult:
        """Dispatch in-process and verify the echo."""
        result = self._handler(request)
        if hasattr(result, "__await__"):
            result = await result
        assert isinstance(result, DeliveryResult)
        verify_identity_echo(result.headers, request)
        return result

    async def stream(self, request: DeliveryRequest) -> StreamDeliveryResult:
        """Dispatch in-process; the events iterator is forwarded unbuffered."""
        result = self._handler(request)
        if hasattr(result, "__await__"):
            result = await result
        assert isinstance(result, StreamDeliveryResult)
        verify_identity_echo(result.headers, request)
        return result


__all__ = [
    "COORDINATOR_TOKEN_HEADER",
    "DeliveryIdentityMismatch",
    "DeliveryRequest",
    "DeliveryResult",
    "HttpLaneDelivery",
    "LocalLaneDelivery",
    "StreamDeliveryResult",
    "verify_identity_echo",
]
