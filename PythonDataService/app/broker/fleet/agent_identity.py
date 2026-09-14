"""The serving runtime's identity echo (FR-076, delivery B).

The agent answers fleet-addressed requests — the ones the coordinator pins
with ``X-Fleet-*`` headers — with the identity it actually serves, derived
from its own live state at response time:

- every response echoes broker, clerk, routing epoch and binding generation
  as headers (the delivery layer's ``verify_identity_echo`` checks them
  against the pinned attempt);
- every event of a streamed response additionally carries the same facts as
  ``x-fleet-*`` fields inside each SSE frame, so a lane whose session or
  binding changes mid-stream cannot keep feeding the old stream's consumer
  (``validate_event_identity`` closes it on the first violation).

Reflection can never satisfy this contract: the values come from the
runtime's own identity provider, never from the request.
"""

from __future__ import annotations

import codecs
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from starlette.datastructures import Headers

from app.broker.fleet.delivery import lane_forward_is_authorized
from app.broker.fleet.errors import BrokerAndClerkRequired, FleetControlError
from app.broker.fleet.internal_http import DEFAULT_MAX_EVENT_BYTES

logger = logging.getLogger(__name__)

#: app.state key holding the callable returning this runtime's served
#: identity (broker, clerk_id, routing_epoch, binding_generation), or None
#: while no fleet lane is open.
SERVED_IDENTITY_STATE_KEY = "fleet_served_identity"

#: Methods a lane agent's unpinned-mutation fence applies to (#2075). Reads
#: stay servable unpinned — health checks, the qualification router, and
#: diagnostic reads keep working.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _served_header_values(identity: Mapping[str, Any]) -> list[tuple[bytes, bytes]]:
    """The identity echo headers for one response, from the runtime's facts."""
    headers: list[tuple[bytes, bytes]] = [
        (b"x-fleet-broker", str(identity["broker"]).encode()),
        (b"x-fleet-clerk-id", str(identity["clerk_id"]).encode()),
    ]
    if identity.get("routing_epoch") is not None:
        headers.append(
            (b"x-fleet-routing-epoch", str(identity["routing_epoch"]).encode())
        )
    if identity.get("binding_generation") is not None:
        headers.append(
            (
                b"x-fleet-binding-generation",
                str(identity["binding_generation"]).encode(),
            )
        )
    return headers


def _identity_field_lines(identity: Mapping[str, Any]) -> bytes:
    """The per-event provenance field lines injected into every SSE frame."""
    parts = [
        f"x-fleet-broker: {identity['broker']}\n".encode(),
        f"x-fleet-clerk-id: {identity['clerk_id']}\n".encode(),
    ]
    if identity.get("routing_epoch") is not None:
        parts.append(f"x-fleet-routing-epoch: {identity['routing_epoch']}\n".encode())
    if identity.get("binding_generation") is not None:
        parts.append(
            f"x-fleet-binding-generation: {identity['binding_generation']}\n".encode()
        )
    return b"".join(parts)


def _pin_mismatch(
    pinned: Mapping[str, str],
    identity: Mapping[str, Any],
    method: str,
) -> str | None:
    """Compare the request's pinned identity with what this runtime serves.

    Broker and clerk are always compared: a dispatch addressed to another
    lane never reaches a handler. Epoch and binding generation are compared
    for mutations — the dimensions whose staleness would make an effect
    wrong — while reads stay servable and prove their origin through the
    echo and per-event provenance instead.
    """
    served_broker = str(identity.get("broker"))
    served_clerk = str(identity.get("clerk_id"))
    if pinned.get("x-fleet-broker") not in (None, served_broker):
        return (
            f"This runtime serves broker {served_broker!r}; the dispatch pinned "
            f"{pinned.get('x-fleet-broker')!r}."
        )
    if pinned.get("x-fleet-clerk-id") not in (None, served_clerk):
        return (
            f"This runtime serves clerk {served_clerk!r}; the dispatch pinned "
            f"{pinned.get('x-fleet-clerk-id')!r}."
        )
    if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        served_epoch = identity.get("routing_epoch")
        pinned_epoch = pinned.get("x-fleet-routing-epoch")
        if pinned_epoch is not None and served_epoch is not None and str(
            served_epoch
        ) != pinned_epoch:
            return (
                f"This runtime serves routing epoch {served_epoch}; the "
                f"dispatch pinned {pinned_epoch}."
            )
        served_generation = identity.get("binding_generation")
        pinned_generation = pinned.get("x-fleet-binding-generation")
        if (
            pinned_generation is not None
            and served_generation is not None
            and str(served_generation) != pinned_generation
        ):
            return (
                f"This runtime serves binding generation {served_generation}; "
                f"the dispatch pinned {pinned_generation}."
            )
    return None


async def _send_refusal(
    send: Callable[[dict[str, Any]], Awaitable[None]], error: FleetControlError
) -> None:
    """Write one typed refusal as a complete ASGI JSON response."""
    await send(
        {
            "type": "http.response.start",
            "status": error.status_code,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": json.dumps(error.detail()).encode(),
            "more_body": False,
        }
    )


def _unpinned_mutation_refusal() -> BrokerAndClerkRequired:
    """The FR-070 family a lane agent answers an unpinned mutation with."""
    return BrokerAndClerkRequired(
        "This process serves one clerk lane and accepts mutations only from "
        "the fleet coordinator, which pins the broker and clerk it "
        "dispatched.",
        next_step="Send the command to the coordinator's clerk-scoped route "
        "at /api/brokers/{broker}/clerks/{clerk_id}/... instead.",
    )


class _FrameInjector:
    """Re-frame a streamed body, injecting provenance into each SSE event.

    Line terminators are normalized to LF (a lone trailing CR is held back
    until the next chunk decides whether it opened a CRLF pair — normalizing
    early would split one terminator into two and dispatch a frame early).
    Re-framing buffers at most one event; the coordinator's parser owns the
    hard byte cap, and this buffer carries the same bound defensively.

    The provenance is read from the runtime's identity **provider** per
    frame: if the runtime re-registers mid-stream, subsequent frames stamp
    the new epoch and the coordinator's pinned consumer closes the stream —
    a superseded session cannot keep feeding its old subscriber.
    """

    def __init__(self, identity_provider: Callable[[], Mapping[str, Any] | None]):
        self._provider = identity_provider
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self._pending_cr = False

    def push(self, chunk: bytes) -> list[bytes]:
        """Accept one body chunk; return complete injected frames."""
        text = self._decoder.decode(chunk)
        if self._pending_cr:
            text = "\r" + text
            self._pending_cr = False
        if text.endswith("\r"):
            self._pending_cr = True
            text = text[:-1]
        self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        if len(self._buffer.encode()) > DEFAULT_MAX_EVENT_BYTES:
            # An unterminated frame is the coordinator parser's refusal to
            # make; here the overrun only bounds this buffer. Forward the
            # bytes unchanged and reset.
            drained = self._buffer.encode()
            self._buffer = ""
            return [drained]
        identity = self._provider()
        if not isinstance(identity, Mapping):
            identity = {}
        fields = _identity_field_lines(identity)
        frames: list[bytes] = []
        while "\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split("\n\n", 1)
            frames.append(frame.encode() + b"\n" + fields + b"\n")
        return frames

    def flush(self) -> bytes:
        """Forward any unterminated tail unchanged (the parser drops it)."""
        if self._pending_cr:
            self._buffer += "\r"
            self._pending_cr = False
        tail = self._buffer + self._decoder.decode(b"", final=True)
        self._buffer = ""
        return tail.encode()


class FleetIdentityMiddleware:
    """ASGI middleware echoing the serving runtime's fleet identity.

    Fleet-addressed traffic only: browser-direct requests carry no pinned
    identity and their responses are untouched — the echo is the forwarded
    lane's contract, not the public API's. A separately deployed lane agent
    may also fence unpinned mutations (``refuse_unpinned_mutations``, #2075):
    on that posture, browser-direct reads still pass untouched, but a
    browser-direct mutation is refused rather than served.
    """

    def __init__(self, app: Any, *, refuse_unpinned_mutations: bool = False) -> None:
        """Wrap one ASGI app; a lane agent may also fence unpinned mutations."""
        self.app = app
        self._refuse_unpinned_mutations = refuse_unpinned_mutations

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Verify the pin, echo identity headers, stamp per-frame provenance.

        A mutation pinned to a stale clerk, epoch or generation is refused
        *before* the handler runs — discovering the mismatch in the response
        echo would be after the effect. Reads pass with a provenance echo;
        the coordinator's echo and per-event checks own their verification.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        if "x-fleet-clerk-id" not in request_headers:
            # Browser-direct traffic carries no pinned identity; the echo is
            # the forwarded lane's contract, not the public API's. On a lane
            # agent, though, an unpinned *mutation* has no legitimate caller:
            # the coordinator always pins both x-fleet-clerk-id and
            # x-fleet-broker on its own forwards (lane_forward_is_authorized
            # requires the pair), so a request reaching this branch is never
            # a proven coordinator dispatch. Refusing here makes the
            # composed-auth claim code, not a property of a gitignored
            # compose overlay (#2075).
            if (
                self._refuse_unpinned_mutations
                and str(scope.get("method", "GET")).upper() in _MUTATING_METHODS
                and not lane_forward_is_authorized(Headers(scope=scope))
            ):
                await _send_refusal(send, _unpinned_mutation_refusal())
                return
            await self.app(scope, receive, send)
            return
        state = getattr(scope.get("app"), "state", None)
        provider = getattr(state, SERVED_IDENTITY_STATE_KEY, None) if state else None
        identity = provider() if callable(provider) else None
        if not isinstance(identity, Mapping) or identity.get("clerk_id") is None:
            # Reaching here means a lane-serving process has not installed its
            # served identity yet — a real condition worth surfacing. It no
            # longer fires for agent-to-coordinator traffic, which pins a clerk
            # id on a process that serves no lane and never owed an echo.
            logger.warning(
                "A fleet-addressed request reached a lane process that is "
                "serving no fleet identity; its response cannot carry the "
                "identity echo the coordinator verifies.",
                extra={"action": "fleet_identity_echo_unavailable"},
            )
            await self.app(scope, receive, send)
            return

        mismatch = _pin_mismatch(request_headers, identity, scope.get("method", "GET"))
        if mismatch is not None:
            body = (
                '{"reason": "clerk_identity_mismatch", "message": '
                + json.dumps(mismatch)
                + "}"
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 409,
                    "headers": [
                        (b"content-type", b"application/json"),
                        *_served_header_values(identity),
                    ],
                }
            )
            await send(
                {"type": "http.response.body", "body": body, "more_body": False}
            )
            return

        echo_headers = _served_header_values(identity)
        injector: list[_FrameInjector] = []

        async def send_echoing(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [*(message.get("headers") or []), *echo_headers]
                content_type = next(
                    (
                        value
                        for name, value in headers
                        if name.lower() == b"content-type"
                    ),
                    b"",
                )
                if content_type.startswith(b"text/event-stream"):
                    # The provider (not a snapshot) rides with the injector:
                    # a re-registration mid-stream re-stamps later frames and
                    # the coordinator closes the superseded stream.
                    injector.append(_FrameInjector(provider))
                await send({**message, "headers": headers})
                return
            if message["type"] == "http.response.body" and injector:
                more = bool(message.get("more_body"))
                for chunk in injector[0].push(message.get("body", b"")):
                    await send(
                        {"type": "http.response.body", "body": chunk, "more_body": True}
                    )
                if not more:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": injector[0].flush(),
                            "more_body": False,
                        }
                    )
                return
            await send(message)

        await self.app(scope, receive, send_echoing)


__all__ = [
    "SERVED_IDENTITY_STATE_KEY",
    "FleetIdentityMiddleware",
]
