"""Broker System v2 read router (transport only).

Resolves the ``{broker}`` path segment via the registry, calls the read port,
and translates broker-contract errors into HTTP responses carrying a what/why
detail. No business logic lives here — the router validates/parses, calls a
port, and shapes the response (router-freeze discipline). Phase 1 registers
only ``alpaca``; unknown brokers resolve to ``404``.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query

from app.broker.alpaca.clerk import (
    AlpacaClerk,
    OrderCancelResult,
    OrderSubmitResult,
    get_alpaca_clerk,
)
from app.broker.contract.errors import BrokerError, BrokerRateLimited
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerPosition,
)
from app.broker.contract.ports import BrokerReadPort
from app.broker.contract.registry import get_broker_registry
from app.security.data_plane_control import require_data_plane_control_secret

router = APIRouter(prefix="/api/brokers", tags=["brokers-v2"])

_DEFAULT_READ_LIMIT = 100
_MAX_READ_LIMIT = 500
_MAX_ACTIVITY_LIMIT = 100
_MAX_INT64_MS = 2**63 - 1


def _raise_http(error: BrokerError) -> NoReturn:
    """Translate a broker-contract error into an HTTPException (what/why)."""
    headers: dict[str, str] | None = None
    if isinstance(error, BrokerRateLimited) and error.retry_after_ms is not None:
        headers = {"Retry-After": str(max(1, math.ceil(error.retry_after_ms / 1000)))}
    raise HTTPException(
        status_code=error.http_status,
        detail={"broker": error.broker, "message": error.message, "why": error.detail},
        headers=headers,
    )


def _resolve_port(broker: str) -> BrokerReadPort:
    try:
        return get_broker_registry().resolve(broker)
    except BrokerError as error:
        _raise_http(error)


async def _run[T](broker: str, call: Callable[[BrokerReadPort], Awaitable[T]]) -> T:
    """Resolve the port, run one read call, and translate contract errors."""
    port = _resolve_port(broker)
    try:
        return await call(port)
    except BrokerError as error:
        _raise_http(error)


@router.get("/{broker}/account", response_model=BrokerAccountSnapshot)
async def get_account(broker: str) -> BrokerAccountSnapshot:
    return await _run(broker, lambda port: port.get_account())


@router.get("/{broker}/positions", response_model=list[BrokerPosition])
async def list_positions(broker: str) -> list[BrokerPosition]:
    return await _run(broker, lambda port: port.list_positions())


@router.get("/{broker}/orders", response_model=list[BrokerOrder])
async def list_orders(
    broker: str,
    status: Literal["open", "closed", "all"] | None = None,
    limit: int | None = Query(default=None, ge=1, le=_MAX_READ_LIMIT),
    after_ms: int | None = Query(default=None, ge=0, le=_MAX_INT64_MS),
) -> list[BrokerOrder]:
    return await _run(
        broker,
        lambda port: port.list_orders(status=status, limit=limit, after_ms=after_ms),
    )


@router.get("/{broker}/activities", response_model=list[BrokerActivity])
async def list_activities(
    broker: str,
    limit: int = Query(default=_DEFAULT_READ_LIMIT, ge=1, le=_MAX_ACTIVITY_LIMIT),
    after_ms: int | None = Query(default=None, ge=0, le=_MAX_INT64_MS),
) -> list[BrokerActivity]:
    return await _run(
        broker,
        lambda port: port.list_activities(after_ms=after_ms, limit=limit),
    )


@router.get("/{broker}/assets", response_model=list[BrokerAsset])
async def list_assets(
    broker: str,
    status: Literal["active", "inactive"] | None = None,
    limit: int = Query(default=_DEFAULT_READ_LIMIT, ge=1, le=_MAX_READ_LIMIT),
) -> list[BrokerAsset]:
    return await _run(broker, lambda port: port.list_assets(status=status, limit=limit))


@router.get("/{broker}/clock", response_model=BrokerClockEvidence)
async def get_clock_evidence(broker: str) -> BrokerClockEvidence:
    # Vendor evidence only — the canonical calendar module remains the sole
    # authority for scheduled session structure (no authority change).
    return await _run(broker, lambda port: port.get_clock_evidence())


def _require_trade_clerk(broker: str) -> AlpacaClerk:
    """Resolve the account-scoped Alpaca Clerk, or raise the right HTTP error.

    Shared by the write endpoints (submit + cancel). An unknown broker surfaces
    the read path's ``404``; an unconfigured Clerk surfaces a ``503`` with a
    what/why. Only Alpaca has a trade port in phase 2.
    """
    if broker != "alpaca":
        # Surface the same 404 shape the read path uses for an unknown broker.
        _resolve_port(broker)
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise HTTPException(
            status_code=503,
            detail={
                "broker": broker,
                "message": "Alpaca order management is not configured.",
                "why": "Set Alpaca paper credentials in .env and restart the service.",
            },
        )
    return clerk


@router.post(
    "/{broker}/orders",
    response_model=OrderSubmitResult,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def submit_orders(broker: str, request: BrokerOrderRequest) -> OrderSubmitResult:
    """Submit one or more equity market/limit legs (phase-2 write path).

    Transport only: FastAPI validates the body — an inconsistent leg (a limit
    order with no ``limit_price``, a market order carrying one) is a Pydantic
    ``422`` here, never a ``500`` — this resolves the account-scoped Clerk
    facade, and the Clerk owns identity minting, fail-closed journaling, the
    broker call, and per-leg result shaping. A per-leg broker rejection is a
    *failed* leg in a ``200`` response (the request itself succeeded), never a
    ``500``.
    """
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.submit(request)
    except BrokerError as error:
        _raise_http(error)


@router.delete(
    "/{broker}/orders/{order_id}",
    response_model=OrderCancelResult,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def cancel_order(broker: str, order_id: str) -> OrderCancelResult:
    """Cancel one working order by its broker-assigned id (phase-2 S3 write path).

    Transport only: resolve the account-scoped Clerk facade and delegate. The
    Clerk owns ownership resolution, fail-closed journaling, the broker call, and
    result shaping. A non-cancelable order is a *failed* result in a ``200``
    response with a typed what/why (never a ``500``). Cancel is intentionally a
    first-class Clerk path, independent of the submit gate, so a future exposure
    hold (S6) that blocks new submission never blocks reducing exposure.
    """
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.cancel(order_id)
    except BrokerError as error:
        _raise_http(error)
