"""Immutable read contracts for SQLite execution-economic projections.

The SQLite reader owns queries and FIFO orchestration.  This module owns only
the stable data shapes those reads return, so product adapters can depend on
typed economic facts without importing the reader implementation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide

ExecutionOrigin = Literal["strategy", "manual", "external", "unknown"]
ExecutionState = Literal["effective", "superseded"]
ExecutionCoverage = Literal["complete", "incomplete"]
FeeFidelity = Literal["reported", "not_reported"]


@dataclass(frozen=True)
class MarketMark:
    """One mark supplied to the canonical FIFO open-P&L valuation.

    ``observed_at_ms`` is retained with the projection so a price is never
    presented without the provenance timestamp that describes its freshness.
    """

    price: float
    observed_at_ms: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.price, bool)
            or not isinstance(self.price, (int, float))
            or not math.isfinite(self.price)
        ):
            raise ValueError("mark price must be finite")
        if (
            isinstance(self.observed_at_ms, bool)
            or not isinstance(self.observed_at_ms, int)
            or self.observed_at_ms < 0
        ):
            raise ValueError("mark observed_at_ms must be non-negative")


@dataclass(frozen=True)
class FillPage:
    """One bounded newest-first page of effective bot execution slices."""

    account_id: str
    strategy_instance_id: str
    authority_generation: int
    control_revision: int
    fills: tuple[FillRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class FillWindowProjection:
    """All effective fills in one bounded chart window at one SQLite revision.

    Unlike :class:`FillPage`, this projection never silently truncates. The
    caller either receives every effective slice in the requested half-open
    window or an explicit unavailable error from the reader.
    """

    account_id: str
    strategy_instance_id: str
    authority_generation: int
    control_revision: int
    fills: tuple[FillRecord, ...]


@dataclass(frozen=True)
class ExecutionRow:
    """One account-history execution row sourced from the SQLite fills fold."""

    fill_id: str
    execution_id: str | None
    order_ref: str
    strategy_instance_id: str
    origin: ExecutionOrigin
    state: ExecutionState
    event_kind: Literal["fill", "correction"]
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    fee: float | None
    fee_fidelity: FeeFidelity
    filled_at_ms: int
    recorded_at_ms: int
    journal_seq: int = 0


@dataclass(frozen=True)
class ExecutionPage:
    """One bounded newest-first account execution page."""

    account_id: str
    authority_generation: int
    control_revision: int
    executions: tuple[ExecutionRow, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class EconomicSnapshot:
    """One coherent bot-economic view at one SQLite control revision."""

    account_id: str
    strategy_instance_id: str
    authority_generation: int
    control_revision: int
    session_open_ms: int | None
    session_close_ms: int | None
    recent_fills: tuple[FillRecord, ...]
    fills_today: int
    exposure: dict[str, float]
    realized_pnl_today: float
    open_pnl: float | None
    marks_complete: bool
    mark_observed_at_ms: dict[str, int]
    fee_fidelity: FeeFidelity
    execution_coverage: ExecutionCoverage
    last_activity_at_ms: int | None


@dataclass(frozen=True)
class SessionEconomicProjection:
    """One revision-bound economic snapshot and its complete session markers."""

    snapshot: EconomicSnapshot
    session_fills: tuple[FillRecord, ...]


@dataclass(frozen=True)
class FifoAttributionRow:
    """One account-level FIFO lot closure, with both bot identities retained."""

    symbol: str
    quantity: float
    entry_price: float
    exit_price: float
    opened_at_ms: int
    closed_at_ms: int
    realized_pnl: float
    fee: float | None
    entry_strategy_instance_id: str
    exit_strategy_instance_id: str


@dataclass(frozen=True)
class AccountPnlAttribution:
    """Complete account FIFO attribution for one inclusive UTC-ms window."""

    account_id: str
    authority_generation: int
    control_revision: int
    from_ms: int
    to_ms: int
    attribution_rows: tuple[FifoAttributionRow, ...]
    realized_pnl_total: float
    start_open_pnl_total: float | None
    open_pnl_total: float | None
    fee_total: float | None
    fee_fidelity: FeeFidelity
    execution_coverage: ExecutionCoverage
    marks_complete: bool
    start_mark_observed_at_ms: dict[str, int]
    mark_observed_at_ms: dict[str, int]


__all__ = [
    "AccountPnlAttribution",
    "EconomicSnapshot",
    "ExecutionCoverage",
    "ExecutionOrigin",
    "ExecutionPage",
    "ExecutionRow",
    "ExecutionState",
    "FeeFidelity",
    "FifoAttributionRow",
    "FillPage",
    "FillWindowProjection",
    "MarketMark",
    "SessionEconomicProjection",
]
