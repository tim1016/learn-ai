"""Broker capability descriptor (Broker System v2, Layer 3).

Callers gate on **capabilities, never on broker identity** (design decision
D2). Honest differences between brokers are declared here as data — e.g.
Alpaca's IEX feed gaps on illiquid symbols (``bars_may_gap=True``) and caps
free streams at 30 symbols — so no code needs an ``if broker == "alpaca"``
branch. Each vendor layer publishes one instance describing itself.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BrokerCapabilities(BaseModel):
    """What a broker can and cannot do, as data.

    Phase-1 callers read the descriptor for display and honest-empty logic;
    later phases gate streaming and order construction on the same fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    broker: str
    # Trading surface (phase-1 informational; enforced from phase 2).
    paper_only: bool
    supports_fractional: bool
    supports_extended_hours: bool
    supported_order_types: tuple[str, ...]
    # Market-data / streaming shape (designed now, enforced from phase 3).
    data_feed: str
    bars_may_gap: bool
    max_stream_symbols: int
    max_concurrent_streams: int
    # REST budget the caller must stay within.
    rest_rate_limit_per_min: int
