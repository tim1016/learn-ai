"""The Clerk's history-batch provider: coordinator-owned Polygon retrieval.

Issue #2204: a Fleet Clerk boots with a present-but-empty ``POLYGON_API_KEY``
(ADR 0062) and must never call Polygon directly. This module supplies
``chart_projection_service.build_history_chart`` its ``batch_provider`` --
one call, a single ``HistoryBatchQuery`` in, one already-complete
``HistoryBatchResponse`` out (issue #2204 gate F2) -- selected by this
process's fleet role:

- ``FLEET_ROLE=clerk_agent`` (the real fleet-enrolled posture): the provider
  is :class:`RemoteHistoryBatchClient`, one authenticated internal HTTP
  request to the fleet-coordinator role's
  ``/internal/fleet/history/batch`` operation (``app.routers.internal_fleet``),
  reusing the same transport identity (``X-Fleet-Clerk-Id`` +
  ``X-Fleet-Agent-Token``) and hardened client
  (``build_internal_client`` / ``enforce_private_http_target``) as
  ``app.broker.fleet.presence.RemotePresence``.
- ``FLEET_ROLE in ("combined", "fleet_coordinator")`` (a positive match on
  the roles that hold a real Polygon key, issue #2204 gate F5 -- not merely
  "is not a clerk agent"): the provider calls the coordinator-side batch
  builder in-process, since this process *is* its own coordinator.

This is deliberately **not** folded into the ``FleetPresence`` protocol
(``app.broker.fleet.presence``): presence's lifecycle (register, reserve,
confirm, observe) has nothing to do with a bounded market-data read, and
widening that narrow interface for one unrelated operation would blur it for
every future presence caller (PRD #2201 §10.3). It also does not live under
``app.broker.fleet`` itself: that package's generic spine may import no
provider implementation (PRD FR-005,
``tests/broker/fleet/test_import_isolation.py``), and this module reaches
Alpaca-clerk-shaped types transitively through ``chart_projection_service``.
"""

from __future__ import annotations

import logging

import httpx

from app.broker.fleet.history_batch import (
    HISTORY_BATCH_INNER_TIMEOUT_S,
    INTERNAL_HISTORY_BATCH_PATH,
)
from app.broker.fleet.internal_http import (
    FleetTransportRefused,
    build_internal_client,
    enforce_private_http_target,
)
from app.config import fleet_settings, settings
from app.schemas.fleet_history_batch import (
    HistoryBatchQuery,
    HistoryBatchRequest,
    HistoryBatchResponse,
)
from app.services.broker_v2_panel.chart_projection_service import HistoryBatchProvider, notice_view
from app.services.broker_v2_panel.history_batch_walk import build_coordinator_history_batch
from app.services.polygon_notice_classifier import coordinator_unavailable_notice

logger = logging.getLogger(__name__)

_HISTORY_SUBJECT = "Polygon history"

#: The fleet roles that hold this process's own real ``POLYGON_API_KEY`` and
#: therefore answer a history-batch query in-process (issue #2204 gate F5).
#: A positive enumeration, not ``!= "clerk_agent"``: a future role added to
#: ``FleetSettings.ROLE`` would otherwise silently fall into the in-process
#: path merely by not being named "clerk_agent".
_DATA_PLANE_ROLES_WITH_LOCAL_POLYGON_ACCESS = frozenset({"combined", "fleet_coordinator"})


class HistoryClientMisconfigured(RuntimeError):
    """A ``clerk_agent`` role has no coordinator history transport configured.

    A deployment defect (mirrors the equivalent boot-time checks in
    ``app.broker.alpaca.clerk.fleet_boot``): a fleet-enrolled clerk missing
    ``FLEET_COORDINATOR_URL`` / ``FLEET_AGENT_SERVICE_TOKEN`` / ``FLEET_CLERK_ID``.
    Raised only once, from :func:`_construct_provider` -- ``build_history_batch_provider``
    (issue #2204 gate F7) catches it there so a misconfigured live Clerk
    degrades every history request into ``coordinator_unavailable`` instead
    of crashing boot or a request.
    """


def _unavailable_batch(as_of_ms: int) -> HistoryBatchResponse:
    """The one degraded batch every transport failure mode converges on.

    Reuses ``chart_projection_service.notice_view`` (issue #2204 gate F8)
    rather than constructing a ``ChartOverlayNoticeView`` by hand a second
    time -- the LIVE pane's own Polygon-failure conversion and this one are
    now the same call.
    """
    return HistoryBatchResponse(
        bars=[],
        source="polygon",
        overlay_notices=[notice_view(coordinator_unavailable_notice(_HISTORY_SUBJECT))],
        effective_as_of_ms=as_of_ms,
    )


class RemoteHistoryBatchClient:
    """One authenticated Clerk -> coordinator history-batch call (issue #2204).

    Every failure mode this call can hit -- timeout, connection refusal, a
    non-200 status, or a malformed/unexpected body -- is absorbed here into
    the stable ``coordinator_unavailable`` notice (FR-010): the caller always
    gets back a completed (possibly notice-only) batch, never an exception, so
    ``build_history_chart`` has exactly one code path regardless of transport
    health. The notice text is generic; the diagnostic detail (exception
    type, status code) goes to the coordinator/clerk log only -- never a
    hostname, port, token or response body reaches the caller.
    """

    def __init__(self, *, base_url: str, clerk_id: str, agent_service_token: str) -> None:
        """Bind the internal destination and this clerk's transport identity."""
        enforce_private_http_target(base_url)
        self._base_url = base_url.rstrip("/")
        self._clerk_id = clerk_id
        self._token = agent_service_token

    async def fetch_batch(self, query: HistoryBatchQuery) -> HistoryBatchResponse:
        """One internal request for one complete history batch.

        Sends the one canonical wire request model (issue #2204 gate F2) --
        never a hand-built dict -- built from ``query`` plus this clerk's own
        identity. The read timeout is widened to
        ``HISTORY_BATCH_INNER_TIMEOUT_S`` -- the fleet-coordinator role may
        run the whole backward Polygon walk before answering -- while
        connect/write/pool stay at the fleet default (this is a
        private-network destination; only the wait for a completed walk
        needs headroom, not the connection itself).
        """
        request = HistoryBatchRequest(clerk_id=self._clerk_id, **query.model_dump())
        client = build_internal_client(read_timeout_s=HISTORY_BATCH_INNER_TIMEOUT_S)
        try:
            response = await client.post(
                f"{self._base_url}{INTERNAL_HISTORY_BATCH_PATH}",
                json=request.model_dump(mode="json"),
                headers={
                    "X-Fleet-Clerk-Id": self._clerk_id,
                    "X-Fleet-Agent-Token": self._token,
                },
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "History-batch request to the fleet coordinator failed",
                extra={"action": "history_batch_transport_error", "error_type": type(exc).__name__},
            )
            return _unavailable_batch(query.as_of_ms)
        finally:
            await client.aclose()
        if response.status_code != 200:
            # The status class distinguishes a coordinator that refused the
            # call (4xx -- an auth or validation defect worth escalating) from
            # one that failed serving it (5xx -- likely transient) in the
            # log only; the public notice stays the same generic
            # ``coordinator_unavailable`` either way (issue #2204 gate F8).
            log = logger.error if response.status_code < 500 else logger.warning
            log(
                "History-batch request refused by the fleet coordinator",
                extra={
                    "action": (
                        "history_batch_refused_4xx"
                        if response.status_code < 500
                        else "history_batch_refused_5xx"
                    ),
                    "status_code": response.status_code,
                },
            )
            return _unavailable_batch(query.as_of_ms)
        try:
            parsed = HistoryBatchResponse.model_validate(response.json())
        except ValueError as exc:
            logger.warning(
                "History-batch response from the fleet coordinator was malformed",
                extra={"action": "history_batch_malformed", "error_type": type(exc).__name__},
            )
            return _unavailable_batch(query.as_of_ms)
        if parsed.effective_as_of_ms != query.as_of_ms:
            logger.warning(
                "History-batch response answered a different as_of_ms than requested",
                extra={"action": "history_batch_as_of_ms_mismatch"},
            )
            return _unavailable_batch(query.as_of_ms)
        return parsed


async def _local_batch_provider(query: HistoryBatchQuery) -> HistoryBatchResponse:
    """The ``combined``/``fleet_coordinator``-posture provider: an in-process
    call, no HTTP hop.

    Used only when this process *is* its own coordinator -- the
    legacy/development posture that predates the fleet role split (ADR 0062)
    and holds the real ``POLYGON_API_KEY`` itself rather than the
    present-but-empty value every fleet-enrolled clerk boots with.
    """
    return await build_coordinator_history_batch(
        symbol=query.symbol,
        timeframe=query.timeframe,
        required_bar_count=query.required_bar_count,
        as_of_ms=query.as_of_ms,
        polygon_api_key=settings.POLYGON_API_KEY,
    )


def _construct_provider() -> HistoryBatchProvider:
    """Select and build this process's history-batch provider by its fleet role.

    A ``clerk_agent`` role reaches Polygon only through the authenticated
    internal hop (FR-015: it never calls Polygon directly). Every other role
    that can reach this call at all is its own coordinator and answers
    in-process (issue #2204 gate F5's positive role match).

    May raise ``HistoryClientMisconfigured`` (an unconfigured ``clerk_agent``)
    or ``FleetTransportRefused`` (a ``COORDINATOR_URL`` outside the private
    network boundary) -- both are construction-time deployment defects, and
    ``build_history_batch_provider`` (below) is the sole caller, catching
    both so this function's own callers never see them.
    """
    if fleet_settings.ROLE in _DATA_PLANE_ROLES_WITH_LOCAL_POLYGON_ACCESS:
        return _local_batch_provider
    if not (
        fleet_settings.COORDINATOR_URL
        and fleet_settings.AGENT_SERVICE_TOKEN
        and fleet_settings.CLERK_ID
    ):
        raise HistoryClientMisconfigured(
            "FLEET_ROLE=clerk_agent requires FLEET_COORDINATOR_URL, "
            "FLEET_AGENT_SERVICE_TOKEN and FLEET_CLERK_ID for coordinator-owned "
            "history retrieval."
        )
    client = RemoteHistoryBatchClient(
        base_url=fleet_settings.COORDINATOR_URL,
        clerk_id=fleet_settings.CLERK_ID,
        agent_service_token=fleet_settings.AGENT_SERVICE_TOKEN,
    )
    return client.fetch_batch


def _degraded_provider_after(exc: Exception) -> HistoryBatchProvider:
    """The provider a construction failure yields: every request degrades.

    Logged once here (ERROR: this is a deployment defect a live Clerk is now
    running with, not routine per-request noise) rather than once per
    request -- ``build_history_batch_provider`` caches the returned callable
    (issue #2204 gate F7), so this log line fires exactly once per process
    regardless of how many history requests follow.
    """
    logger.error(
        "History-batch provider is misconfigured; every history request will "
        "degrade to coordinator_unavailable until this deployment is repaired.",
        extra={"action": "history_batch_provider_misconfigured", "error_type": type(exc).__name__},
    )

    async def _always_unavailable(query: HistoryBatchQuery) -> HistoryBatchResponse:
        return _unavailable_batch(query.as_of_ms)

    return _always_unavailable


#: Built once per process, on first use, and cached (issue #2204 gate F7):
#: ``RemoteHistoryBatchClient.__init__`` calls ``enforce_private_http_target``,
#: which can do a blocking DNS lookup -- this must not repeat on every
#: history request. Mirrors how ``RemotePresence`` is a one-time
#: construction, not a per-call one; unlike ``RemotePresence``, a
#: construction failure here never becomes a boot refusal (a live Clerk's
#: chart history degrading is acceptable; a live Clerk refusing to start
#: over it is not) -- ``_degraded_provider_after`` converts it into a
#: provider that answers ``coordinator_unavailable`` per request instead.
_CACHED_PROVIDER: HistoryBatchProvider | None = None


def build_history_batch_provider() -> HistoryBatchProvider:
    """Return this process's history-batch provider, building it on first call."""
    global _CACHED_PROVIDER
    if _CACHED_PROVIDER is None:
        try:
            _CACHED_PROVIDER = _construct_provider()
        except (HistoryClientMisconfigured, FleetTransportRefused) as exc:
            _CACHED_PROVIDER = _degraded_provider_after(exc)
    return _CACHED_PROVIDER


__all__ = [
    "HistoryClientMisconfigured",
    "RemoteHistoryBatchClient",
    "build_history_batch_provider",
]
