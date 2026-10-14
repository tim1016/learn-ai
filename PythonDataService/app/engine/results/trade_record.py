"""Canonical closed-trade record shared by engine result projections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradeRecord:
    trade_number: int
    entry_timestamp: int
    exit_timestamp: int
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    signal_reason: str
