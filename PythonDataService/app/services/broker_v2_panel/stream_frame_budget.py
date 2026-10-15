"""The byte budget one server-sent event may take when it leaves this lane.

The fleet coordinator's SSE parser raises ``FleetStreamError`` on any single
event over ``DEFAULT_MAX_EVENT_BYTES`` (``app.broker.fleet.internal_http``),
after the 200 has gone out, which cuts the browser's stream with no reason
(#2328 for the gallery, #3217 for the live panel). Every lane stream sizes its
frames against the one budget below instead of each re-deriving it.
"""

from __future__ import annotations

from app.broker.fleet.internal_http import DEFAULT_MAX_EVENT_BYTES

# Bytes held back from the parser's cap for framing added after a router has
# sized its frame: the lane's ``FleetIdentityMiddleware``
# (``agent_identity._FrameInjector``) appends ``x-fleet-*`` provenance lines
# to every event (~115 B for a typical clerk id, epoch and generation). 8 KB
# covers any realistic identity with a wide margin, so a frame that fits the
# budget here still fits there.
FLEET_FRAMING_RESERVE_BYTES = 8_192


def frame_budget_bytes(max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES) -> int:
    """The most bytes one SSE frame may carry when it leaves a router."""
    return max_event_bytes - FLEET_FRAMING_RESERVE_BYTES
