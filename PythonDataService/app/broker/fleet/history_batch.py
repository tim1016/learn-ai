"""The coordinator-owned history-batch internal operation (issue #2204).

Bot chart history retrieval moves off the Clerk (which never holds a usable
Polygon key, ADR 0062) and onto one authenticated request to the
fleet-coordinator role's ``/internal/fleet/*`` surface
(``app.routers.internal_fleet``). The coordinator runs the entire backward
Polygon widening walk locally and returns one completed batch, so the Clerk
makes exactly one internal call per public history attempt (PRD #2201 §2.3).

**Both hops of that one browser request must widen past the fleet default**
(``DEFAULT_INTERNAL_TIMEOUT_S`` = 10s, ``app.broker.fleet.internal_http``):

- the INNER Clerk -> coordinator call
  (``app.services.broker_v2_panel.history_batch_client.RemoteHistoryBatchClient``)
  can now run the whole walk before it answers, which a cold ``1D`` request
  can push past 10 seconds;
- the OUTER coordinator -> Clerk delivery hop
  (``HttpLaneDelivery.deliver``, forwarding the ``bot_chart_history``
  ``ProviderOperation``) waits on the Clerk's own request/response cycle,
  which now includes that whole inner call.

If only the inner bound widened, the outer request would expire first,
the browser would see a spurious ``clerk_unreachable``, and the
coordinator's completed work would be discarded for nothing. The outer bound
is therefore derived from the inner one plus headroom rather than set
independently, so the two constants can never drift out of the required
order -- ``tests/broker/fleet/test_history_batch_timeouts.py`` pins it.

Connection, write and pool phases are left at the fleet default in both
directions; only each hop's *read* timeout (how long to wait for the
response body once the request is sent) widens -- an unbounded read timeout
is not permitted for this route either (PRD FR-010).
"""

from __future__ import annotations

#: The coordinator's internal path for the sixth ``/internal/fleet/*``
#: operation (issue #2204). Not a ``ProviderOperation`` -- it belongs to the
#: generic agent-to-coordinator family (sessions, assignments, heartbeats),
#: never to a broker's public clerk-scoped catalog.
INTERNAL_HISTORY_BATCH_PATH = "/internal/fleet/history/batch"

#: The INNER hop's read-timeout bound: how long the Clerk waits for the
#: coordinator to finish its entire backward Polygon walk and answer.
HISTORY_BATCH_INNER_TIMEOUT_S = 45.0

#: The OUTER hop's read-timeout bound: how long the coordinator waits for the
#: Clerk's own response, which now encloses the inner call above. Must stay
#: strictly greater than the inner bound (see module docstring) -- computed
#: from it rather than pinned separately so the two can never drift.
HISTORY_BATCH_OUTER_TIMEOUT_S = HISTORY_BATCH_INNER_TIMEOUT_S + 15.0

__all__ = [
    "HISTORY_BATCH_INNER_TIMEOUT_S",
    "HISTORY_BATCH_OUTER_TIMEOUT_S",
    "INTERNAL_HISTORY_BATCH_PATH",
]
