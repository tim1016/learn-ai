"""Explicit live-source setup for broker client doubles."""

from __future__ import annotations

from typing import Any

from app.broker.ibkr.live.local import NativeLiveSource


def native_source(client: Any) -> Any:
    client.live_data = NativeLiveSource(client)
    return client


async def unused_qualifier(symbol: str):
    raise AssertionError(f"This fixture must not qualify an underlying: {symbol}")
