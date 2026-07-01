"""Shared FastAPI dependencies for broker routers."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.broker.ibkr.client import IbkrClient, NotConnectedError, get_client


def is_broker_disabled() -> bool:
    from app.broker.ibkr.config import get_settings

    return not get_settings().broker_enabled


def require_connected_client() -> IbkrClient:
    """Return the connected IBKR client or raise the canonical broker 503."""
    if is_broker_disabled():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "IBKR broker is disabled (IBKR_BROKER_ENABLED=false). Use /api/live-runs for paper-run status.",
        )
    try:
        client = get_client()
    except NotConnectedError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "IBKR client not initialised.",
        ) from exc
    if not client.is_connected():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "IBKR client not connected to Gateway.",
        )
    return client
