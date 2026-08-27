"""Broker-neutral bar and bar-snapshot wire models for the IBKR feed.

Split out of ``app/broker/ibkr/models.py`` (IBKR decommission Slice 0,
issue #1813) so the live-chart/gallery/bar-aggregator path can depend
on bar types without importing account/order/session models from the
same file. See
``docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md``.

All timestamps are ``int64`` ms UTC per the project's numerical-rigor
rules.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BarProvenance = Literal["ibkr_realtime", "ibkr_historical", "polygon_historical", "mixed"]
BarSessionPhase = Literal["PRE", "RTH", "POST", "OVERNIGHT", "CLOSED", "UNKNOWN"]


class IbkrMinuteBar(BaseModel):
    """One closed 1-minute TRADES bar from IBKR real-time bars.

    IBKR delivers 5-second bars via ``reqRealTimeBars``. The broker
    boundary aggregates those into closed 1-minute bars and stores all
    boundary timestamps as ``int64`` ms UTC.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    start_ms: int = Field(..., description="UTC milliseconds since epoch, inclusive.")
    end_ms: int = Field(..., description="UTC milliseconds since epoch, exclusive.")
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    fetched_at_ms: int
    source: Literal["ibkr", "polygon", "mixed"] = "ibkr"
    provenance: BarProvenance = "ibkr_realtime"
    venue: str | None = None
    session_phase: BarSessionPhase = "UNKNOWN"
    use_rth: bool | None = None


class IbkrBarsSnapshot(BaseModel):
    """A snapshot of the live 1-min OHLCV ring buffer for one symbol.

    ``status`` reports the aggregator's subscription health so the UI can
    show "Subscribing…" / "Streaming" / "Error: …" instead of an
    inscrutable empty chart.
    """

    symbol: str
    status: Literal["idle", "subscribing", "streaming", "errored", "resubscribing"]
    last_error: str | None = None
    last_bar_ms: int | None = None
    bars: list[IbkrMinuteBar] = Field(default_factory=list)
