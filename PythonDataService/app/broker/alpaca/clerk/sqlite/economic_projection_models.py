"""Immutable read contracts for SQLite execution-economic projections.

The SQLite reader owns queries and FIFO orchestration.  This module owns only
the stable data shapes those reads return, so product adapters can depend on
typed economic facts without importing the reader implementation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import BrokerPosition, OrderSide

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


def current_position_marks(positions: Iterable[BrokerPosition]) -> dict[str, MarketMark]:
    """Each held symbol's broker-reported current price, keyed upper-case.

    A position without a price is left out, so FIFO valuation reports that
    symbol unmarked instead of valuing it at a guess.
    """
    return {
        position.symbol.upper(): MarketMark(price=position.current_price, observed_at_ms=position.observed_at_ms)
        for position in positions
        if position.current_price is not None
    }


def prior_close_position_marks(
    positions: Iterable[BrokerPosition], *, closed_at_ms: int,
) -> dict[str, MarketMark]:
    """Each held symbol's broker-reported price at the prior regular-session close.

    ``closed_at_ms`` is that close, the instant the price describes. Only a
    symbol held now has a broker position to carry it; any other symbol is
    left out and so reported unmarked.
    """
    return {
        position.symbol.upper(): MarketMark(price=position.prior_close_price, observed_at_ms=closed_at_ms)
        for position in positions
        if position.prior_close_price is not None
    }


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
    strategy_instance_id: str | None
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
    subject_id: str = ""
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
    """One coherent bot-economic view at one SQLite control revision.

    ``exact_open_pnl`` is canonical FIFO's exact open valuation
    (``OpenPnLResult.exact_value``) -- the one a displayed dollar figure is
    rounded from (#2556). ``open_pnl`` is its float display view.
    """

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
    exact_open_pnl: Decimal | None
    marks_complete: bool
    mark_observed_at_ms: dict[str, int]
    fee_fidelity: FeeFidelity
    execution_coverage: ExecutionCoverage
    last_activity_at_ms: int | None

    @property
    def open_pnl(self) -> float | None:
        """Display view of ``exact_open_pnl``, rounded once."""
        return None if self.exact_open_pnl is None else float(self.exact_open_pnl)


@dataclass(frozen=True)
class SessionEconomicProjection:
    """One revision-bound economic snapshot and its complete session markers."""

    snapshot: EconomicSnapshot
    session_fills: tuple[FillRecord, ...]


@dataclass(frozen=True)
class FifoAttributionRow:
    """One account-level FIFO lot closure, with its custody actors retained."""

    symbol: str
    quantity: float
    entry_price: float
    exit_price: float
    opened_at_ms: int
    closed_at_ms: int
    realized_pnl: float
    fee: float | None
    entry_strategy_instance_id: str | None
    exit_strategy_instance_id: str | None
    entry_subject_id: str
    exit_subject_id: str


@dataclass(frozen=True)
class AccountPnlAttribution:
    """Complete account FIFO attribution for one inclusive UTC-ms window.

    The ``exact_*`` totals are canonical FIFO's exact values: the window's
    closed-lot ``exact_realized_pnl`` summed exactly, and the exact open
    valuation at the start and end marks. A displayed dollar figure is
    rounded from them (#2556); the float totals are their display views,
    each rounded once.
    """

    account_id: str
    authority_generation: int
    control_revision: int
    from_ms: int
    to_ms: int
    attribution_rows: tuple[FifoAttributionRow, ...]
    exact_realized_pnl_total: Decimal
    exact_start_open_pnl_total: Decimal | None
    exact_open_pnl_total: Decimal | None
    fee_total: float | None
    fee_fidelity: FeeFidelity
    execution_coverage: ExecutionCoverage
    marks_complete: bool
    start_mark_observed_at_ms: dict[str, int]
    mark_observed_at_ms: dict[str, int]

    @property
    def realized_pnl_total(self) -> float:
        """Display view of ``exact_realized_pnl_total``, rounded once."""
        return float(self.exact_realized_pnl_total)

    @property
    def start_open_pnl_total(self) -> float | None:
        """Display view of ``exact_start_open_pnl_total``, rounded once."""
        return None if self.exact_start_open_pnl_total is None else float(self.exact_start_open_pnl_total)

    @property
    def open_pnl_total(self) -> float | None:
        """Display view of ``exact_open_pnl_total``, rounded once."""
        return None if self.exact_open_pnl_total is None else float(self.exact_open_pnl_total)


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
    "current_position_marks",
    "prior_close_position_marks",
]
