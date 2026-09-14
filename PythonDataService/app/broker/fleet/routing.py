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
    BrokerClerkCapabilityUnavailable,
    ClerkBindingGenerationConflict,
    ClerkIdentityMismatch,
    ClerkRoutingAttemptConflict,
    ClerkRoutingOutcomeUnknown,
    ClerkUnreachable,
    FleetControlError,
)
from app.broker.fleet.identity import new_correlation_id
from app.broker.fleet.provider import (
    OperationIdempotency,
    OperationReadiness,
    ProviderOperation,
)
from app.broker.fleet.records import (
    ClerkSessionRecord,
    RoutingReceiptState,
)
from app.broker.fleet.service import FleetControlService

logger = logging.getLogger(__name__)

#: Response body keys treated as the provider clerk's durable receipt
#: reference on a delivered command (checked in order; the first present
#: wins), plus a nested ``command`` mapping some families return.
_RECEIPT_BODY_KEYS = (
    "receipt_id",
    "receipt_ref",
    "receipt",
    "command_id",
    "cancel_request_id",
    "ticket_id",
)


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
        """Extract the envelope a command body carries, if any.

        Raises `CommandEnvelopeInvalid` for a present-but-malformed envelope:
        a boundary validation error belongs to the 422 family, not the
        catch-all 500.
        """
        if not isinstance(body, Mapping):
            return None
        context = body.get("command_context")
        if not isinstance(context, Mapping):
            return None
        target = context.get("target")
        generation = context.get("expected_effective_binding_generation")
        if generation is not None and (
            isinstance(generation, bool) or not isinstance(generation, int)
        ):
            raise CommandEnvelopeInvalid(
                "expected_effective_binding_generation must be an integer "
                f"when present, got {generation!r}."
            )
        return CommandEnvelope(
            capability=str(context.get("capability") or ""),
            idempotency_key=(
                str(context["idempotency_key"])
                if context.get("idempotency_key") is not None
                else None
            ),
            expected_effective_binding_generation=generation,
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
                "The lane's response failed identity verification; the "
                "transport-level detail is in the coordinator log.",
                next_step="The wrong lane answered; refresh and retry against "
                "the lane's current resource.",
            ) from exc
        except FleetControlError:
            raise
        except Exception as exc:
            # The exception text can carry internal topology (hostnames,
            # ports, paths); it goes to the log, never the public refusal.
            logger.warning(
                "Lane delivery failed for %s on %s: %r",
                operation.operation_id,
                clerk_id,
                exc,
            )
            raise ClerkUnreachable(
                f"Clerk {clerk_id} could not serve {operation.operation_id}; "
                "the transport detail is in the coordinator log.",
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
            logger.warning(
                "Lane stream failed identity verification for %s on %s: %r",
                operation.operation_id,
                clerk_id,
                exc,
            )
            raise ClerkIdentityMismatch(
                "The lane's stream failed identity verification; the "
                "transport-level detail is in the coordinator log.",
            ) from exc
        except FleetControlError:
            raise
        except Exception as exc:
            logger.warning(
                "Lane stream open failed for %s on %s: %r",
                operation.operation_id,
                clerk_id,
                exc,
            )
            raise ClerkUnreachable(
                f"Clerk {clerk_id} could not open {operation.operation_id}; "
                "the transport detail is in the coordinator log.",
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
        validated = self._validated_envelope(
            operation, envelope, body, path_params
        )
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
                if validated.idempotency_key
                else f"oneshot-{new_correlation_id()}"
            ),
            pinned_routing_epoch=session.routing_epoch,
            pinned_agent_instance_id=session.agent_instance_id,
            pinned_binding_generation=(
                assignment.confirmed_binding_generation if assignment else None
            ),
        )
        if receipt.state != RoutingReceiptState.NOT_DISPATCHED:
            # A settled attempt never redispatches: the provider clerk's
            # receipt is the outcome authority, and a retry of the same key
            # must reconcile against it, not resubmit (D11).
            raise ClerkRoutingAttemptConflict(
                f"Idempotency key {receipt.idempotency_key} already settled as "
                f"{receipt.state.value}"
                + (
                    f" with provider receipt {receipt.upstream_receipt_ref!r}"
                    if receipt.upstream_receipt_ref
                    else ""
                )
                + "; reconcile with the provider clerk's receipt.",
                next_step="Read the command's outcome by its durable identity; "
                "never resubmit the same key.",
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
            logger.warning(
                "Command %s on %s failed identity verification after dispatch: %r",
                operation.operation_id,
                clerk_id,
                exc,
            )
            raise ClerkRoutingOutcomeUnknown(
                f"The lane's response for {operation.operation_id} failed "
                "identity verification; the transport-level detail is in the "
                "coordinator log.",
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
            logger.warning(
                "Command %s on %s has an unknown outcome after dispatch: %r",
                operation.operation_id,
                clerk_id,
                exc,
            )
            raise ClerkRoutingOutcomeUnknown(
                f"Clerk {clerk_id} accepted {operation.operation_id} dispatch "
                "but the outcome is unknown; the transport detail is in the "
                "coordinator log.",
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
        if broker not in self._service.adapters():
            from app.broker.fleet.errors import BrokerNotSupported

            raise BrokerNotSupported(f"No production adapter serves {broker!r}.")
        expected_account = (
            path_params.get("account_id")
            if operation.requires_effective_account
            else None
        )
        resolved = self._service.resolve_route(
            broker=broker,
            clerk_id=clerk_id,
            expected_binding_generation=expected_binding_generation,
            expected_account_id=expected_account,
            readiness=operation.readiness,
        )
        _clerk, session, assignment = resolved
        if assignment is not None:
            # ADR 0062 Decision 5 / addendum 3: the provider's own safety gate
            # answers for execution operations only. A configuration-access
            # operation must stay routable for a lane whose binding is broken,
            # which is precisely the lane a provider gate would refuse.
            from app.broker.fleet.provider import ServedContext

            context = ServedContext(
                broker=broker,
                clerk_id=clerk_id,
                agent_instance_id=session.agent_instance_id,
                routing_epoch=session.routing_epoch,
                account_id=assignment.canonical_external_account_id,
                capability=operation.capability,
                effective_binding_generation=assignment.confirmed_binding_generation,
            )
            try:
                self._service.adapters()[broker].validate_served_context(context)
            except FleetControlError:
                raise
            except Exception as exc:
                raise BrokerClerkCapabilityUnavailable(
                    f"Provider {broker!r} refuses to serve "
                    f"{operation.operation_id} on clerk {clerk_id}: {exc}",
                    next_step="The provider's own safety gates must pass before "
                    "this operation routes; repair the lane's configuration and "
                    "retry against its current resource.",
                ) from exc
        return resolved

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
        path_params: Mapping[str, str],
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
        if operation.readiness == OperationReadiness.EXECUTION and (
            envelope.expected_effective_binding_generation is None
        ):
            # A command without its generation fence executes whatever the
            # lane currently serves — a stale intent must conflict instead.
            raise CommandEnvelopeInvalid(
                f"An execution command to {operation.path_template} must pin "
                "expected_effective_binding_generation; re-prepare the "
                "command against the lane's current resource."
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
        if operation.requires_effective_account:
            target_account = envelope.target.get("account_id")
            if (
                target_account is not None
                and target_account.strip().lower()
                != str(path_params.get("account_id", "")).strip().lower()
            ):
                raise CommandEnvelopeInvalid(
                    f"The envelope's target names account {target_account!r}; "
                    f"the command path targets "
                    f"{path_params.get('account_id')!r}. Frozen intent and "
                    "routed target must agree."
                )
        target_entity = envelope.target.get("entity_id")
        if (
            target_entity is not None
            and "sid" in path_params
            and target_entity != path_params["sid"]
        ):
            raise CommandEnvelopeInvalid(
                f"The envelope's target names entity {target_entity!r}; the "
                f"command path targets {path_params['sid']!r}."
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
    nested = parsed.get("command")
    if isinstance(nested, Mapping):
        for key in _RECEIPT_BODY_KEYS:
            value = nested.get(key)
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
            # ASGI scope header names must be lowercase; a mixed-case name
            # is invisible to Starlette's Headers and the lane-side guard
            # would refuse the dispatch as an unauthenticated forward.
            headers.append(
                (COORDINATOR_TOKEN_HEADER.lower().encode(), token.encode())
            )
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
