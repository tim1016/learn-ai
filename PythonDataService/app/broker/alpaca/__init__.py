"""Alpaca vendor layer (Broker System v2, Layer 1 + adapter).

Speaks pure Alpaca: settings/safety, the async SDK client wrapper, the verbatim
capture hook, the error map, and the adapter that converts raw Alpaca payloads
to broker-contract models. No alpaca-py type escapes this package.
"""

from __future__ import annotations

from app.broker.alpaca.client import BROKER_ID, AlpacaTradingClient
from app.broker.alpaca.config import (
    AlpacaSettings,
    get_alpaca_settings,
    reset_alpaca_settings_for_testing,
)
from app.broker.alpaca.errors import map_api_error

__all__ = [
    "BROKER_ID",
    "AlpacaSettings",
    "AlpacaTradingClient",
    "get_alpaca_settings",
    "map_api_error",
    "reset_alpaca_settings_for_testing",
]
