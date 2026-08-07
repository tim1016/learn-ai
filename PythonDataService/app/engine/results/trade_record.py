"""Canonical closed-trade record shared by engine result projections."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TradeRecord:
    trade_number: int
    trade_type: str
    entry_timestamp: int
    exit_timestamp: int
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    cumulative_pnl_pct: float
    signal_reason: str
    indicator_snapshot: dict[str, float | None] = field(default_factory=dict)
