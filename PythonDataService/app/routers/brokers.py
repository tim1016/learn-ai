"""Broker System v2 read router (transport only).

Resolves the ``{broker}`` path segment via the registry, calls the read port,
and translates broker-contract errors into HTTP responses carrying a what/why
detail. No business logic lives here — the router validates/parses, calls a
port, and shapes the response (router-freeze discipline). Phase 1 registers
only ``alpaca``; unknown brokers resolve to ``404``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NoReturn

from fastapi import APIRouter, HTTPException

from app.broker.contract.errors import BrokerError, BrokerRateLimited
from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.contract.ports import BrokerReadPort
from app.broker.contract.registry import get_broker_registry

router = APIRouter(prefix="/api/brokers", tags=["brokers-v2"])


def _raise_http(error: BrokerError) -> NoReturn:
    """Translate a broker-contract error into an HTTPException (what/why)."""
    headers: dict[str, str] | None = None
    if isinstance(error, BrokerRateLimited) and error.retry_after_ms is not None:
        headers = {"Retry-After": str(max(1, round(error.retry_after_ms / 1000)))}
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
