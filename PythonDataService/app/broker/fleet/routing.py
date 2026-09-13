"""The coordinator's routed-operation core (PRD §9.8, §10.2–§10.4, delivery B).

One path turns a public clerk-scoped request into a verified provider
response: resolve the lane against the registry (readiness, pinned epoch and
generation, wrong-target refusal), validate the §10.3 command envelope,
persist the pinned routing attempt *before* dispatch for every effectful
operation, deliver through the lane adapter, and settle the attempt's typed
outcome. The provider clerk remains the sole deduplication and outcome
authority (D11); this layer never resubmits, never retargets, and never
treats an omitted identity echo as verified.

Refusals surface as the §10.4 families — the typed error hierarchy already
carries their reason codes and pinned statuses; nothing here invents a new
one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.broker.fleet.delivery import (
    DeliveryIdentityMismatch,
    DeliveryRequest,
    DeliveryResult,
    HttpLaneDelivery,
    LocalLaneDelivery,
    StreamDeliveryResult,
)
from app.broker.fleet.errors import (
    ClerkBindingGenerationConflict,
    ClerkIdentityMismatch,
    ClerkRoutingOutcomeUnknown,
    ClerkUnreachable,
    FleetControlError,
)
from app.broker.fleet.provider import (
    OperationIdempotency,
    ProviderOperation,
)
from app.broker.fleet.records import (
    ClerkSessionRecord,
    RoutingReceiptState,
)
from app.broker.fleet.service import FleetControlService

logger = logging.getLogger(__name__)

#: Response body keys treated as the provider clerk's durable receipt
#: reference on a delivered command (checked in order; the first present wins).
_RECEIPT_BODY_KEYS = ("receipt_id", "receipt_ref", "receipt", "command_id")


class CommandEnvelopeInvalid(ValueError):
    """A command body's §10.3 envelope is absent or incoherent.

    A contract violation, not a fleet family: the caller's request never
    reached a routing decision. The public router maps it to 422.
    """


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    """The §10.3 command context, validated against the routed operation."""

    capability: str
    idempotency_key: str | None
    expected_effective_binding_generation: int | None
    target: Mapping[str, str | None]

    @staticmethod
    def from_body(body: Any) -> CommandEnvelope | None:
        """Extract the envelope a command body carries, if any."""
        if not isinstance(body, Mapping):
            return None
        context = body.get("command_context")
        if not isinstance(context, Mapping):
            return None
        target = context.get("target")
        return CommandEnvelope(
            capability=str(context.get("capability") or ""),
            idempotency_key=(
                str(context["idempotency_key"])
                if context.get("idempotency_key") is not None
                else None
            ),
            expected_effective_binding_generation=(
                int(context["expected_effective_binding_generation"])
                if context.get("expected_effective_binding_generation") is not None
                else None
            ),
            target=dict(target) if isinstance(target, Mapping) else {},
        )


@dataclass(frozen=True, slots=True)
class RoutedDelivery:
    """One delivered operation plus its routing receipt facts."""

    status_code: int
    headers: dict[str, str]
    body: bytes
    correlation_id: str | None = None
    routing_state: str | None = None


@dataclass(frozen=True, slots=True)
class RoutedStream:
    """One routed streaming operation: status, headers, verified events."""

    status_code: int
    headers: dict[str, str]
    events: Any


LaneDelivery = Any  # HttpLaneDelivery | LocalLaneDelivery — structural pair


class LaneRouter:
    """Route clerk-scoped operations through the registry to the serving lane."""

    def __init__(
        self,
        *,
        service: FleetControlService,
        delivery_for: Callable[[str, ClerkSessionRecord], LaneDelivery],
    ) -> None:
        """Bind the registry and the lane-delivery resolver.

        ``delivery_for`` resolves one clerk's serving destination: the
        approved agent endpoint over HTTP (coordinator posture) or the
        in-process handler (combined posture).
        """
        self._service = service
        self._delivery_for = delivery_for

    # ---- reads and streams -------------------------------------------------

    async def deliver_read(
        self,
        *,
        broker: str,
        clerk_id: str,
        operation: ProviderOperation,
        path_params: Mapping[str, str],
        query: Mapping[str, str],
        body: object | None = None,
    ) -> RoutedDelivery:
        """Deliver one read-idempotent operation; no effect, no attempt row."""
        _clerk, session, assignment = self._resolve(
            broker=broker,
            clerk_id=clerk_id,
            operation=operation,
            path_params=path_params,
            expected_binding_generation=None,
        )
        request = self._request(
            broker=broker,
            clerk_id=clerk_id,
            operation=operation,
            path_params=path_params,
            query=query,
            body=body,
            session=session,
            assignment=assignment,
        )
        delivery = self._delivery_for(broker, session)
        try:
            result = await delivery.deliver(request)
        except DeliveryIdentityMismatch as exc:
            raise ClerkIdentityMismatch(
                f"The lane's response failed identity verification: {exc}",
                next_step="The wrong lane answered; refresh and retry against "
                "the lane's current resource.",
            ) from exc
        except FleetControlError:
            raise
        except Exception as exc:
            raise ClerkUnreachable(
                f"Clerk {clerk_id} could not serve {operation.operation_id}: {exc}",
            ) from exc
        if result.status_code >= 500:
            raise ClerkUnreachable(
                f"Clerk {clerk_id} failed serving {operation.operation_id} "
                f"with {result.status_code}.",
            )
        return RoutedDelivery(
            status_code=result.status_code,
            headers=dict(result.headers),
            body=result.body,
        )

    async def stream_read(
        self,
        *,
        broker: str,
        clerk_id: str,
        operation: ProviderOperation,
        path_params: Mapping[str, str],
        query: Mapping[str, str],
    ) -> RoutedStream:
        """Open one routed stream; every event is provenance-verified."""
        _clerk, session, assignment = self._resolve(
            broker=broker,
            clerk_id=clerk_id,
            operation=operation,
            path_params=path_params,
            expected_binding_generation=None,
        )
        request = self._request(
            broker=broker,
            clerk_id=clerk_id,
            operation=operation,
            path_params=path_params,
            query=query,
            body=None,
            session=session,
            assignment=assignment,
        )
        delivery = self._delivery_for(broker, session)
        try:
            result = await delivery.stream(request)
        except DeliveryIdentityMismatch as exc:
            raise ClerkIdentityMismatch(
                f"The lane's stream failed identity verification: {exc}",
            ) from exc
        except FleetControlError:
            raise
        except Exception as exc:
            raise ClerkUnreachable(
                f"Clerk {clerk_id} could not open {operation.operation_id}: {exc}",
            ) from exc
        if result.status_code >= 500:
            raise ClerkUnreachable(
                f"Clerk {clerk_id} failed opening {operation.operation_id} "
                f"with {result.status_code}.",
            )
        return RoutedStream(
            status_code=result.status_code,
            headers=dict(result.headers),
            events=result.events,
        )

    # ---- commands ----------------------------------------------------------

    async def deliver_command(
        self,
        *,
        broker: str,
        clerk_id: str,
        operation: ProviderOperation,
        path_params: Mapping[str, str],
        query: Mapping[str, str],
        body: object,
        envelope: CommandEnvelope | None,
    ) -> RoutedDelivery:
        """Deliver one effectful operation under a persisted routing attempt."""
        if operation.idempotency == OperationIdempotency.READ:
            return await self.deliver_read(
                broker=broker,
                clerk_id=clerk_id,
                operation=operation,
                path_params=path_params,
                query=query,
                body=body,
            )
        validated = self._validated_envelope(operation, envelope, body)
        _clerk, session, assignment = self._resolve(
            broker=broker,
            clerk_id=clerk_id,
            operation=operation,
            path_params=path_params,
            expected_binding_generation=validated.expected_effective_binding_generation,
        )
        if (
            validated.expected_effective_binding_generation is not None
            and assignment is not None
            and assignment.confirmed_binding_generation is not None
            and validated.expected_effective_binding_generation
            != assignment.confirmed_binding_generation
        ):
            # resolve_route already refused for execution readiness; this
            # guards the configuration-access command path where no
            # assignment pins the generation yet.
            raise ClerkBindingGenerationConflict(
                f"Expected binding generation "
                f"{validated.expected_effective_binding_generation} is not clerk "
                f"{clerk_id}'s confirmed generation "
                f"{assignment.confirmed_binding_generation}.",
            )
        request = self._request(
            broker=broker,
            clerk_id=clerk_id,
            operation=operation,
            path_params=path_params,
            query=query,
            body=body,
            session=session,
            assignment=assignment,
        )
        nonsecret_target = "/".join(
            f"{name}={path_params[name]}" for name in sorted(path_params)
        )
        receipt = self._service.open_routing_attempt(
            broker=broker,
            clerk_id=clerk_id,
            operation_kind=operation.operation_id,
            nonsecret_target_ref=nonsecret_target or operation.path_template,
            idempotency_key=(
                validated.idempotency_key
                or f"oneshot:{operation.operation_id}:{nonsecret_target}"
            ),
            pinned_routing_epoch=session.routing_epoch,
            pinned_agent_instance_id=session.agent_instance_id,
            pinned_binding_generation=(
                assignment.confirmed_binding_generation if assignment else None
            ),
        )
        delivery = self._delivery_for(broker, session)
        # Dispatch is one-way from here: whatever happens next, the attempt
        # can never present as definitively un-sent (D11).
        self._service.mark_routing_dispatched(correlation_id=receipt.correlation_id)
        try:
            result = await delivery.deliver(request)
        except DeliveryIdentityMismatch as exc:
            self._service.settle_routing_attempt(
                correlation_id=receipt.correlation_id,
                outcome=RoutingReceiptState.OUTCOME_UNKNOWN,
            )
            raise ClerkRoutingOutcomeUnknown(
                f"The lane's response for {operation.operation_id} failed "
                f"identity verification: {exc}",
                next_step="The command may have been applied. Reconcile by the "
                "idempotency key with the provider clerk's receipt; never "
                "resubmit blindly.",
            ) from exc
        except FleetControlError:
            raise
        except Exception as exc:
            self._service.settle_routing_attempt(
                correlation_id=receipt.correlation_id,
                outcome=RoutingReceiptState.OUTCOME_UNKNOWN,
            )
            raise ClerkRoutingOutcomeUnknown(
                f"Clerk {clerk_id} accepted {operation.operation_id} dispatch "
                f"but the outcome is unknown: {exc}",
                next_step="Reconcile by the idempotency key; the provider "
                "clerk's command machinery is the outcome authority.",
            ) from exc
        if result.status_code >= 500:
            self._service.settle_routing_attempt(
                correlation_id=receipt.correlation_id,
                outcome=RoutingReceiptState.OUTCOME_UNKNOWN,
            )
            raise ClerkRoutingOutcomeUnknown(
                f"Clerk {clerk_id} failed serving {operation.operation_id} "
                f"with {result.status_code} after the dispatch.",
            )
        if result.status_code >= 400:
            self._service.settle_routing_attempt(
                correlation_id=receipt.correlation_id,
                outcome=RoutingReceiptState.PROVIDER_REFUSED,
            )
            return RoutedDelivery(
                status_code=result.status_code,
                headers=dict(result.headers),
                body=result.body,
                correlation_id=receipt.correlation_id,
                routing_state=RoutingReceiptState.PROVIDER_REFUSED.value,
            )
        self._service.settle_routing_attempt(
            correlation_id=receipt.correlation_id,
            outcome=RoutingReceiptState.DELIVERED,
            upstream_receipt_ref=_extract_receipt_ref(result.body),
        )
        return RoutedDelivery(
            status_code=result.status_code,
            headers=dict(result.headers),
            body=result.body,
            correlation_id=receipt.correlation_id,
            routing_state=RoutingReceiptState.DELIVERED.value,
        )

    # ---- internals ----------------------------------------------------------

    def _resolve(
        self,
        *,
        broker: str,
        clerk_id: str,
        operation: ProviderOperation,
        path_params: Mapping[str, str],
        expected_binding_generation: int | None,
    ):
        if broker not in self._service._provider_adapters:
            from app.broker.fleet.errors import BrokerNotSupported

            raise BrokerNotSupported(f"No production adapter serves {broker!r}.")
        expected_account = (
            path_params.get("account_id")
            if operation.requires_effective_account
            else None
        )
        return self._service.resolve_route(
            broker=broker,
            clerk_id=clerk_id,
            expected_binding_generation=expected_binding_generation,
            expected_account_id=expected_account,
            readiness=operation.readiness,
        )

    def _request(
        self,
        *,
        broker: str,
        clerk_id: str,
        operation: ProviderOperation,
        path_params: Mapping[str, str],
        query: Mapping[str, str],
        body: object | None,
        session: ClerkSessionRecord,
        assignment,
    ) -> DeliveryRequest:
        return DeliveryRequest(
            broker=broker,
            clerk_id=clerk_id,
            operation=operation,
            path_params=dict(path_params),
            query=dict(query),
            json_body=body,
            routing_epoch=session.routing_epoch,
            binding_generation=(
                assignment.confirmed_binding_generation if assignment else None
            ),
        )

    def _validated_envelope(
        self,
        operation: ProviderOperation,
        envelope: CommandEnvelope | None,
        body: object,
    ) -> CommandEnvelope:
        """Validate the §10.3 envelope against the operation being routed."""
        if envelope is None:
            raise CommandEnvelopeInvalid(
                f"A {operation.method} to {operation.path_template} requires a "
                "command_context envelope (capability, idempotency identity, "
                "expected binding generation)."
            )
        if envelope.capability != operation.capability.value:
            raise CommandEnvelopeInvalid(
                f"The envelope names capability {envelope.capability!r}; the "
                f"routed operation is {operation.capability.value!r}."
            )
        if (
            operation.idempotency == OperationIdempotency.DURABLE_KEY
            and not (envelope.idempotency_key or "").strip()
        ):
            raise CommandEnvelopeInvalid(
                f"Operation {operation.operation_id} is durable-keyed; the "
                "envelope must carry a non-empty idempotency_key."
            )
        if (
            isinstance(body, Mapping)
            and envelope.idempotency_key
            and body.get("idempotency_key") is not None
            and body["idempotency_key"] != envelope.idempotency_key
        ):
            raise CommandEnvelopeInvalid(
                "The body's idempotency_key disagrees with the envelope's; a "
                "command has exactly one durable identity."
            )
        return envelope


def _extract_receipt_ref(body: bytes) -> str | None:
    """Read the provider clerk's durable receipt reference from a delivered body."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    for key in _RECEIPT_BODY_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def build_local_delivery(app: Any) -> LocalLaneDelivery:
    """The combined posture's delivery: dispatch into this process's own app.

    A raw ASGI call — not the httpx ASGI transport, which buffers a
    streaming response to completion and must never carry a lane stream.
    The dispatch carries the pinned identity headers and the coordinator
    service token, so the same-process agent side runs its ordinary auth
    and identity-echo path: the composed policy accepts the coordinator
    token exactly as a separate agent process would.
    """
    return LocalLaneDelivery(_LocalAsgiHandler(app))


def coordinator_delivery_for(
    service: FleetControlService,
    coordinator_tokens: Mapping[str, str],
    *,
    local_app: Any = None,
) -> Callable[[str, ClerkSessionRecord], Any]:
    """Resolve one lane's serving destination at dispatch time.

    The combined posture (``local_app`` set) dispatches in-process. The
    coordinator posture resolves the session's cited endpoint against the
    deployment-owned approved-endpoint row and presents that clerk's
    coordinator service token — a session citing an unapproved reference, or
    a clerk with no provisioned token, is a typed refusal, never an
    unauthenticated send.
    """
    if local_app is not None:
        return lambda broker, session: build_local_delivery(local_app)

    def resolve(broker: str, session: ClerkSessionRecord) -> Any:
        endpoint = service._store.read_approved_endpoint(session.clerk_id)
        if endpoint is None or endpoint.endpoint_ref != session.endpoint_ref:
            raise ClerkUnreachable(
                f"Clerk {session.clerk_id} cites endpoint "
                f"{session.endpoint_ref!r}, which no approved endpoint row "
                "backs; the host ceremony must approve it before routing.",
            )
        token = coordinator_tokens.get(session.clerk_id)
        if not token:
            raise ClerkUnreachable(
                f"No coordinator service token is provisioned for clerk "
                f"{session.clerk_id}; routing refuses rather than sending "
                "unauthenticated.",
            )
        return HttpLaneDelivery(
            base_url=endpoint.base_url, coordinator_service_token=token
        )

    return resolve


class _LocalAsgiHandler:
    """One raw-ASGI dispatch per routed operation."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, request: DeliveryRequest) -> Any:
        from urllib.parse import urlencode

        from app.broker.fleet.delivery import COORDINATOR_TOKEN_HEADER
        from app.broker.fleet.internal_http import iter_sse_events
        from app.config import fleet_settings

        body_bytes = (
            json.dumps(request.json_body).encode()
            if request.json_body is not None
            else b""
        )
        headers: list[tuple[bytes, bytes]] = [
            (b"host", b"fleet-local"),
            *[
                (name.lower().encode(), value.encode())
                for name, value in request.pinned_headers().items()
            ],
        ]
        if body_bytes:
            headers.append((b"content-type", b"application/json"))
        token = (fleet_settings.COORDINATOR_SERVICE_TOKEN or "").strip()
        if token:
            headers.append((COORDINATOR_TOKEN_HEADER.encode(), token.encode()))
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.operation.method,
            "scheme": "http",
            "path": request.agent_path(),
            "raw_path": request.agent_path().encode(),
            "query_string": urlencode(dict(request.query)).encode(),
            "root_path": "",
            "headers": headers,
            "app": self._app,
        }

        body_sent = False
        # A live connection stays open until the app finishes: Starlette's
        # streaming responses race a disconnect listener against the body
        # sender, so an instant ``http.disconnect`` would cancel the stream
        # mid-send and the bridge would never deliver its end sentinel.
        connection_open = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            await connection_open.wait()
            return {"type": "http.disconnect"}

        if request.operation.stream.value != "sse":
            messages: list[dict[str, Any]] = []

            async def collect(message: dict[str, Any]) -> None:
                messages.append(message)

            await self._app(scope, receive, collect)
            start = next(m for m in messages if m["type"] == "http.response.start")
            payload = b"".join(
                m.get("body", b"")
                for m in messages
                if m["type"] == "http.response.body"
            )
            return DeliveryResult(
                status_code=start["status"],
                headers={
                    name.decode(): value.decode()
                    for name, value in start.get("headers") or []
                },
                body=payload,
            )

        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        started: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )

        async def bridge(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                if not started.done():
                    started.set_result(message)
            elif message["type"] == "http.response.body":
                if message.get("body"):
                    await queue.put(message["body"])
                if not message.get("more_body"):
                    await queue.put(None)

        task = asyncio.create_task(
            self._app(scope, receive, bridge), name="fleet-local-stream"
        )

        async def chunks() -> AsyncIterator[bytes]:
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item
                await task
            finally:
                connection_open.set()
                if not task.done():
                    task.cancel()

        start = await started
        return StreamDeliveryResult(
            status_code=start["status"],
            headers={
                name.decode(): value.decode()
                for name, value in start.get("headers") or []
            },
            events=iter_sse_events(chunks()),
        )


__all__ = [
    "CommandEnvelope",
    "CommandEnvelopeInvalid",
    "LaneRouter",
    "RoutedDelivery",
    "RoutedStream",
    "build_local_delivery",
    "coordinator_delivery_for",
]
