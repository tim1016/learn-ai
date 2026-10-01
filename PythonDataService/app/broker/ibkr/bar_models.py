"""Broker-neutral bar wire models for the IBKR feed.

Split out of ``app/broker/ibkr/models.py`` (IBKR decommission Slice 0,
issue #1813) so the live-chart/gallery/bar-aggregator path can depend
on bar types without importing account/order/session models from the
same file.

All timestamps are ``int64`` ms UTC.

``BarSessionPhase`` is not defined here: it is broker-neutral, so its
single definition lives in ``app.marketdata.feed`` and this module
imports it (#1813 PR-C, 2026-08-27).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.marketdata.feed import BarSessionPhase
from app.utils.session_anchors import MAX_TIMESTAMP_MS

BarProvenance = Literal["ibkr_realtime", "ibkr_historical", "polygon_historical", "mixed"]


class IbkrMinuteBar(BaseModel):
    """One closed 1-minute TRADES bar from IBKR real-time bars.

    IBKR delivers 5-second bars via ``reqRealTimeBars``. The broker
    boundary aggregates those into closed 1-minute bars and stores all
    boundary timestamps as ``int64`` ms UTC.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    start_ms: int = Field(..., le=MAX_TIMESTAMP_MS, description="UTC milliseconds since epoch, inclusive.")
    end_ms: int = Field(..., le=MAX_TIMESTAMP_MS, description="UTC milliseconds since epoch, exclusive.")
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
    contribution_count: int | None = Field(
        default=None, ge=0, description="5-second bars folded into this minute; None when unknown (historical)."
    )
    spans_interruption: bool = Field(
        default=False, description="Contributions arrived over more than one connection generation."
    )
