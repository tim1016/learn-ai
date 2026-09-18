"""The Clerk's history-batch provider: coordinator-owned Polygon retrieval.

Issue #2204: a Fleet Clerk boots with a present-but-empty ``POLYGON_API_KEY``
(ADR 0062) and must never call Polygon directly. This module supplies
``chart_projection_service.build_history_chart`` its ``batch_provider`` --
one call, symbol/timeframe/required-count/as_of_ms in, one already-complete
batch out -- selected by this process's fleet role:

- ``FLEET_ROLE=clerk_agent`` (the real fleet-enrolled posture): the provider
  is :class:`RemoteHistoryBatchClient`, one authenticated internal HTTP
  request to the fleet-coordinator role's
  ``/internal/fleet/history/batch`` operation (``app.routers.internal_fleet``),
  reusing the same transport identity (``X-Fleet-Clerk-Id`` +
  ``X-Fleet-Agent-Token``) and hardened client
  (``build_internal_client`` / ``enforce_private_http_target``) as
  ``app.broker.fleet.presence.RemotePresence``.
- ``FLEET_ROLE=combined`` (the legacy/development posture with no
  coordinator/clerk process split, ``FleetSettings``'s docstring): the
  provider calls the coordinator-side batch builder in-process, since this
  process *is* its own coordinator and already holds the real
  ``POLYGON_API_KEY``.

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
from pydantic import BaseModel, ConfigDict, Field

from app.broker.fleet.history_batch import (
    HISTORY_BATCH_INNER_TIMEOUT_S,
    INTERNAL_HISTORY_BATCH_PATH,
)
from app.broker.fleet.internal_http import build_internal_client, enforce_private_http_target
from app.config import fleet_settings, settings
from app.schemas.broker_v2_panel import ChartBar, ChartHistoryTimeframe, ChartOverlayNoticeView
from app.services.broker_v2_panel.chart_projection_service import (
    CompleteHistoryBatch,
    HistoryBatchProvider,
    build_coordinator_history_batch,
)
from app.services.polygon_notice_classifier import coordinator_unavailable_notice

logger = logging.getLogger(__name__)

_HISTORY_SUBJECT = "Polygon history"


class HistoryClientMisconfigured(RuntimeError):
    """A ``clerk_agent`` role has no coordinator history transport configured.

    Raised at construction, not per-request: a fleet-enrolled clerk missing
    ``FLEET_COORDINATOR_URL`` / ``FLEET_AGENT_SERVICE_TOKEN`` / ``FLEET_CLERK_ID``
    is a deployment defect (mirrors the equivalent boot-time checks in
    ``app.broker.alpaca.clerk.fleet_boot``), not a transient condition a
    per-request ``coordinator_unavailable`` notice should mask.
    """


class _HistoryBatchWireResponse(BaseModel):
    """The internal route's response body, validated strictly on receipt."""

    model_config = ConfigDict(frozen=True)

    bars: list[ChartBar] = Field(default_factory=list)
    overlay_notices: list[ChartOverlayNoticeView] = Field(default_factory=list)
    effective_as_of_ms: int


def _unavailable_batch(as_of_ms: int) -> CompleteHistoryBatch:
    """The one degraded batch every transport failure mode converges on."""
    notice = coordinator_unavailable_notice(_HISTORY_SUBJECT)
    return CompleteHistoryBatch(
        bars=[],
        overlay_notices=[
            ChartOverlayNoticeView(code=notice.code, message=notice.message, source="polygon")
        ],
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

    async def fetch_batch(
        self,
        symbol: str,
        timeframe: ChartHistoryTimeframe,
        required_bar_count: int,
        as_of_ms: int,
    ) -> CompleteHistoryBatch:
        """One internal request for one complete history batch.

        The read timeout is widened to ``HISTORY_BATCH_INNER_TIMEOUT_S`` --
        the fleet-coordinator role may run the whole backward Polygon walk
        before answering -- while connect/write/pool stay at the fleet
        default (this is a private-network destination; only the wait for a
        completed walk needs headroom, not the connection itself).
        """
        client = build_internal_client(read_timeout_s=HISTORY_BATCH_INNER_TIMEOUT_S)
        try:
            response = await client.post(
                f"{self._base_url}{INTERNAL_HISTORY_BATCH_PATH}",
                json={
                    "clerk_id": self._clerk_id,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "required_bar_count": required_bar_count,
                    "as_of_ms": as_of_ms,
                },
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
            return _unavailable_batch(as_of_ms)
        finally:
            await client.aclose()
        if response.status_code != 200:
            logger.warning(
                "History-batch request refused by the fleet coordinator",
                extra={
                    "action": "history_batch_refused",
                    "status_code": response.status_code,
                },
            )
            return _unavailable_batch(as_of_ms)
        try:
            parsed = _HistoryBatchWireResponse.model_validate(response.json())
        except ValueError as exc:
            logger.warning(
                "History-batch response from the fleet coordinator was malformed",
                extra={"action": "history_batch_malformed", "error_type": type(exc).__name__},
            )
            return _unavailable_batch(as_of_ms)
        return CompleteHistoryBatch(
            bars=list(parsed.bars),
            overlay_notices=list(parsed.overlay_notices),
            effective_as_of_ms=parsed.effective_as_of_ms,
        )


async def _local_batch_provider(
    symbol: str,
    timeframe: ChartHistoryTimeframe,
    required_bar_count: int,
    as_of_ms: int,
) -> CompleteHistoryBatch:
    """The ``combined``-posture provider: an in-process call, no HTTP hop.

    Used only when this process *is* its own coordinator
    (``FLEET_ROLE=combined``) -- the legacy/development posture that predates
    the fleet role split (ADR 0062) and holds the real ``POLYGON_API_KEY``
    itself rather than the present-but-empty value every fleet-enrolled clerk
    boots with.
    """
    return await build_coordinator_history_batch(
        symbol=symbol,
        timeframe=timeframe,
        required_bar_count=required_bar_count,
        as_of_ms=as_of_ms,
        polygon_api_key=settings.POLYGON_API_KEY,
    )


def build_history_batch_provider() -> HistoryBatchProvider:
    """Select this process's history-batch provider by its fleet role.

    A ``clerk_agent`` role reaches Polygon only through the authenticated
    internal hop (FR-015: it never calls Polygon directly). Every other role
    that can reach this call at all (``combined`` -- ``fleet_coordinator``
    never runs the Clerk-side bot-panel routes this feeds) is its own
    coordinator and answers in-process.
    """
    if fleet_settings.ROLE != "clerk_agent":
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


__all__ = [
    "HistoryClientMisconfigured",
    "RemoteHistoryBatchClient",
    "build_history_batch_provider",
]
