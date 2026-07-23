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

from fastapi import APIRouter, HTTPException, Query

from app.broker.contract.errors import BrokerError, BrokerRateLimited
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerPosition,
)
from app.broker.contract.ports import BrokerReadPort
from app.broker.contract.registry import get_broker_registry

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
