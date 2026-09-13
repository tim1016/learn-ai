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
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.broker.fleet.internal_http import DEFAULT_MAX_EVENT_BYTES

logger = logging.getLogger(__name__)

#: app.state key holding the callable returning this runtime's served
#: identity (broker, clerk_id, routing_epoch, binding_generation), or None
#: while no fleet lane is open.
SERVED_IDENTITY_STATE_KEY = "fleet_served_identity"


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


class _FrameInjector:
    """Re-frame a streamed body, injecting provenance into each SSE event.

    Line terminators are normalized to LF (a lone trailing CR is held back
    until the next chunk decides whether it opened a CRLF pair — normalizing
    early would split one terminator into two and dispatch a frame early).
    Re-framing buffers at most one event; the coordinator's parser owns the
    hard byte cap, and this buffer carries the same bound defensively.

    The provenance is read from the live identity mapping **per frame**: if
    the runtime re-registers mid-stream, subsequent frames stamp the new
    epoch and the coordinator's pinned consumer closes the stream — a
    superseded session cannot keep feeding its old subscriber.
    """

    def __init__(self, identity: Mapping[str, Any]) -> None:
        self._identity = identity
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
        fields = _identity_field_lines(self._identity)
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
    lane's contract, not the public API's.
    """

    def __init__(self, app: Any) -> None:
        """Wrap one ASGI app."""
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Echo identity headers, and per-frame provenance on event streams."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        pinned = any(
            name.lower() == b"x-fleet-clerk-id" for name, _ in scope.get("headers", [])
        )
        if not pinned:
            await self.app(scope, receive, send)
            return
        state = getattr(scope.get("app"), "state", None)
        provider = getattr(state, SERVED_IDENTITY_STATE_KEY, None) if state else None
        identity = provider() if callable(provider) else None
        if not isinstance(identity, Mapping) or identity.get("clerk_id") is None:
            logger.warning(
                "A fleet-addressed request reached a runtime serving no fleet "
                "identity; the response will fail the coordinator's echo check."
            )
            await self.app(scope, receive, send)
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
                    injector.append(_FrameInjector(identity))
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
