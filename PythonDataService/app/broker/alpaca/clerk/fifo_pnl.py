"""Canonical FIFO lot-level P&L for the broker-v2 bot control panel (S0).

Formula:
    Lot accounting uses the First-In-First-Out (FIFO) inventory method.
    Each opening fill creates a lot: ``Lot(qty, cost_per_share, opened_at_ms)``.
    Each closing fill consumes the oldest open lots first (FIFO order).
    Realized P&L per closed lot:
        realized_pnl_lot = (exit_price - entry_price) × closed_qty
    For short positions the sign convention is inverted:
        realized_pnl_lot = (entry_price - exit_price) × closed_qty
    These two cases unify as:
        realized_pnl_lot = signed_delta × closed_qty
        signed_delta = (exit_price - entry_price) for long-opens,
                       (entry_price - exit_price) for short-opens.
    Open P&L:
        open_pnl = Σ_remaining_lots (mark_price - lot.cost) × lot.qty   [long]
                   Σ_remaining_lots (lot.cost - mark_price) × lot.qty   [short]
    Fees: rendered only when the broker reports them; ``None`` = "not reported",
        never $0.00.  The engine propagates ``None`` through — callers display
        the string "Fees not reported" when the field is None.

Reference:
    Standard FIFO inventory method (GAAP / IFRS).  No external software port —
    well-known accounting arithmetic.  See Kieso, Weygandt & Warfield,
    *Intermediate Accounting* (17e), Chapter 8 (Inventories: Measurement).
    Prior registry entry (canonical-in-dotnet-justified, closed 2026-05-06,
    per finding F-0010): ``Backend/Services/Implementation/PositionEngine.cs``.
    That instance accounts over EF/Postgres lots; **this implementation** is a
    NEW Python canonical for broker-v2 bots whose fills live in the Alpaca
    order journal, NOT in Postgres.  The two instances are parallel, not
    duplicates: they operate on different data stores with different consumers
    (portfolio-engine vs. bot-panel) and are NOT expected to net to the same
    numbers (different scope, different fills).  This file is the canonical
    implementation for the broker-v2 bot-panel P&L path.
    Validated against:
        PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py
Canonical implementation: this file.

Usage::

    from app.broker.alpaca.clerk.fills import project_instance_fills
    from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl, PnLResult

    fills = project_instance_fills(sid, journal.read_all())
    result = compute_fifo_pnl(fills)
    # result.realized_pnl  — closed lots only; may be 0.0 on no closed trades
    # result.open_pnl      — None until mark_prices are supplied for ALL open symbols
    # result.marks_complete — True only when all open-lot symbols have mark coverage
    # result.fee_total     — None when any fill has fee=None ("Fees not reported")
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide

# ── Tolerance (per numerical-rigor.md accumulated-PnL default) ───────────────
_ZERO_ABS_TOL = 1e-9


# ── Internal lot record ───────────────────────────────────────────────────────


@dataclass
class _Lot:
    """An open position lot (FIFO queue entry)."""

    qty: float          # positive remaining share count
    cost: float         # cost per share (entry fill price)
    opened_at_ms: int   # int64 ms UTC
    side: OrderSide     # BUY (long lot) or SELL (short lot)


# ── Output models ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClosedLot:
    """One realized P&L record — one FIFO lot closure."""

    symbol: str
    qty: float
    entry_price: float
    exit_price: float
    opened_at_ms: int     # int64 ms UTC
    closed_at_ms: int     # int64 ms UTC
    realized_pnl: float
    fee: float | None     # None = not reported


@dataclass(frozen=True)
class OpenLot:
    """One remaining open lot for open-P&L computation."""

    symbol: str
    qty: float
    cost: float           # cost per share
    opened_at_ms: int     # int64 ms UTC
    side: OrderSide


@dataclass
class PnLResult:
    """The full FIFO P&L result for one bot.

    ``realized_pnl``:
        Sum of P&L from all closed lots across the bot's lifetime.
        ``0.0`` when no lots have been closed.

    ``open_pnl``:
        ``None`` when no current mark price is available OR when any open-lot
        symbol is missing a mark (marks_complete=False).  ``0.0`` when there
        is no open exposure (fully flat, marks_complete=True).  Non-None only
        when ALL open-lot symbols have mark coverage.

        IMPORTANT: ``open_pnl`` is never a partial sum.  If SPY and AAPL
        both have open lots but only SPY has a mark, ``open_pnl`` is ``None``
        (not just SPY's unrealized).  Callers must check ``marks_complete``
        before using ``open_pnl``.

    ``marks_complete``:
        ``True`` when either (a) there are no open lots (bot is flat) or
        (b) every open-lot symbol has a mark in ``mark_prices``.
        ``False`` otherwise.  Only when ``marks_complete`` is ``True`` is
        ``open_pnl`` meaningful.

    ``fee_total``:
        ``None`` when ANY fill has fee=None — the broker did not report all
        fees so the total is not representable.  Callers must render ``None``
        as "Fees not reported", never "$0.00".

    ``closed_lots``:
        Chronological list of realized lot closures, smallest-first by
        ``closed_at_ms``.  Feeds the trades-today list.

    ``open_lots``:
        Remaining open lots (FIFO remainder).  Feeds the open-P&L valuation.
    """

    realized_pnl: float = 0.0
    open_pnl: float | None = None
    marks_complete: bool = False
    fee_total: float | None = None
    closed_lots: list[ClosedLot] = field(default_factory=list)
    open_lots: list[OpenLot] = field(default_factory=list)


@dataclass(frozen=True)
class OpenPnLResult:
    """Open-lot valuation shared by batch FIFO and the incremental cache."""

    value: float | None
    marks_complete: bool
    open_lots: tuple[OpenLot, ...]


# ── FIFO engine ───────────────────────────────────────────────────────────────


def apply_fill_to_lots(
    lots: dict[str, deque[_Lot]],
    fill: FillRecord,
    closed_out: list[ClosedLot],
    realized_accumulator: list[float],
) -> None:
    """Apply one fill to the FIFO lot queues in-place.

    Extracted from ``compute_fifo_pnl``'s inner loop so that the rollup cache
    can maintain FIFO state incrementally (O(1) per fill on append) without
    calling ``compute_fifo_pnl`` over the full history on every read.

    Parameters
    ----------
    lots:
        Per-symbol FIFO lot queues (oldest-first).  Mutated in-place.
    fill:
        The new fill to apply.
    closed_out:
        List to which newly closed ``ClosedLot`` records are appended.
    realized_accumulator:
        Single-element list holding the running realized P&L total.  Updated
        in-place.  A list (not a plain float) so callers can hold a reference
        to a mutable accumulator without boxing/unboxing.

    Formula:
        Same FIFO rules as ``compute_fifo_pnl``.  See module docstring.
    Canonical implementation: this file (delegate of compute_fifo_pnl).
    Validated against:
        PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py and
        PythonDataService/tests/broker/alpaca/clerk/test_rollup_cache.py.
    """
    sym = fill.symbol
    price = fill.fill_price
    qty = fill.quantity
    ts = fill.filled_at_ms

    queue = lots.setdefault(sym, deque())
    remaining = qty

    if not queue:
        queue.append(_Lot(qty=remaining, cost=price, opened_at_ms=ts, side=fill.side))
        remaining = 0.0
    else:
        top_lot = queue[0]
        if fill.side == top_lot.side:
            queue.append(_Lot(qty=remaining, cost=price, opened_at_ms=ts, side=fill.side))
            remaining = 0.0
        else:
            while remaining > _ZERO_ABS_TOL and queue and queue[0].side != fill.side:
                lot = queue[0]
                close_qty = min(remaining, lot.qty)
                if lot.side is OrderSide.BUY:
                    r_pnl = (price - lot.cost) * close_qty
                else:
                    r_pnl = (lot.cost - price) * close_qty
                closed_out.append(
                    ClosedLot(
                        symbol=sym,
                        qty=close_qty,
                        entry_price=lot.cost,
                        exit_price=price,
                        opened_at_ms=lot.opened_at_ms,
                        closed_at_ms=ts,
                        realized_pnl=r_pnl,
                        fee=fill.fee,
                    )
                )
                realized_accumulator[0] += r_pnl
                remaining -= close_qty
                lot.qty -= close_qty
                if math.isclose(lot.qty, 0.0, rel_tol=0.0, abs_tol=_ZERO_ABS_TOL):
                    queue.popleft()

            if remaining > _ZERO_ABS_TOL:
                queue.append(
                    _Lot(qty=remaining, cost=price, opened_at_ms=ts, side=fill.side)
                )


def compute_open_pnl(
    lots: dict[str, deque[_Lot]],
    mark_prices: dict[str, float],
) -> OpenPnLResult:
    """Value current FIFO lots without ever returning a partial portfolio sum.

    Formula: open_pnl = Σ (mark - cost) × signed remaining lot quantity.
    Reference: Kieso, Weygandt & Warfield, *Intermediate Accounting* (17e),
      Chapter 8; same FIFO inventory model as this module.
    Canonical implementation: this file.
    Validated against:
      PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py;
      PythonDataService/tests/broker/alpaca/clerk/test_rollup_cache.py.

    ``value`` is ``None`` until every symbol with an open lot has a mark.
    Flat state is complete and has a value of ``0.0``.
    """
    open_lots: list[OpenLot] = []
    symbols_with_open_lots: set[str] = set()
    total_open_pnl = 0.0

    for symbol, queue in lots.items():
        mark = mark_prices.get(symbol)
        for lot in queue:
            if lot.qty <= _ZERO_ABS_TOL:
                continue
            symbols_with_open_lots.add(symbol)
            open_lots.append(
                OpenLot(
                    symbol=symbol,
                    qty=lot.qty,
                    cost=lot.cost,
                    opened_at_ms=lot.opened_at_ms,
                    side=lot.side,
                )
            )
            if mark is not None:
                signed_delta = (
                    mark - lot.cost
                    if lot.side is OrderSide.BUY
                    else lot.cost - mark
                )
                total_open_pnl += signed_delta * lot.qty

    if not open_lots:
        return OpenPnLResult(value=0.0, marks_complete=True, open_lots=())
    if mark_prices.keys() >= symbols_with_open_lots:
        return OpenPnLResult(
            value=total_open_pnl,
            marks_complete=True,
            open_lots=tuple(open_lots),
        )
    return OpenPnLResult(
        value=None,
        marks_complete=False,
        open_lots=tuple(open_lots),
    )


def realized_pnl_for_window(
    closed_lots: Iterable[ClosedLot],
    *,
    session_open_ms: int,
    session_close_ms: int,
) -> float:
    """Sum canonical closed-lot P&L inside a half-open session window.

    Formula: realized_window = Σ lot.realized_pnl where
      session_open_ms <= lot.closed_at_ms < session_close_ms.
    Reference: same FIFO inventory method as this module; session boundaries
      are supplied by the canonical NYSE calendar.
    Canonical implementation: this file.
    Validated against:
      PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py;
      PythonDataService/tests/broker/alpaca/clerk/test_rollup_cache.py.
    """
    return sum(
        lot.realized_pnl
        for lot in closed_lots
        if session_open_ms <= lot.closed_at_ms < session_close_ms
    )


def compute_fifo_pnl(
    fills: Iterable[FillRecord],
    *,
    mark_prices: dict[str, float] | None = None,
) -> PnLResult:
    """Compute FIFO realized and open P&L over attributed bot fills.

    Formula:
        See module docstring.

    Reference:
        GAAP/IFRS FIFO; Kieso et al. *Intermediate Accounting* (17e) Ch. 8.
    Canonical implementation: this file.
    Validated against: PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py

    Parameters
    ----------
    fills:
        Ordered chronologically (ascending ``filled_at_ms``).  Produced by
        ``project_instance_fills``.
    mark_prices:
        Optional ``{symbol: current_price}`` for open-P&L calculation.
        ``open_pnl`` is ``None`` unless ALL open-lot symbols appear in this
        dict.  A partial mark set never produces a partial sum — ``open_pnl``
        remains ``None`` when any symbol is uncovered.

    Notes
    -----
    - Reversals are handled naturally: a BUY after a short lot closes the
      short (FIFO); any excess opens a new long lot.
    - Partial fills accumulate into lots by fill event — each ``FillRecord``
      is one broker execution event.
    - Fee tracking: ``fee_total`` is ``None`` when any fill reports no fee
      (``fee=None``). This propagates "Fees not reported" honestly rather than
      masking unknown fees as $0.
    - ``marks_complete``: ``True`` iff bot is flat OR all open-lot symbols
      have a mark in ``mark_prices``.  Check this before using ``open_pnl``.
    """
    lots: dict[str, deque[_Lot]] = {}
    result = PnLResult()
    any_fee_missing = False
    realized_acc = [0.0]

    for fill in fills:
        # Fee tracking
        if fill.fee is None:
            any_fee_missing = True
        elif not any_fee_missing:
            result.fee_total = (result.fee_total or 0.0) + fill.fee

        apply_fill_to_lots(lots, fill, result.closed_lots, realized_acc)

    result.realized_pnl = realized_acc[0]

    # Propagate fee_total honesty
    if any_fee_missing:
        result.fee_total = None

    open_result = compute_open_pnl(lots, mark_prices or {})
    result.open_lots = list(open_result.open_lots)
    result.open_pnl = open_result.value
    result.marks_complete = open_result.marks_complete

    return result


def realized_pnl_today(
    fills: Iterable[FillRecord],
    *,
    session_open_ms: int,
    session_close_ms: int,
) -> float:
    """Realized P&L for lots whose close fell within today's session window.

    Runs FIFO over the COMPLETE fill history, then filters the resulting
    closed lots to those with ``closed_at_ms in [session_open_ms,
    session_close_ms)``.

    IMPORTANT: Do NOT pre-filter fills to today before calling FIFO.  A buy
    from a prior session and a sell today would be incorrectly treated as a
    new short if the buy were excluded.  The correct path is:
        all fills → FIFO → filter closed lots by closed_at_ms.

    Formula:
        realized_today = Σ lot.realized_pnl
                         for lot in compute_fifo_pnl(all_fills).closed_lots
                         where session_open_ms <= lot.closed_at_ms < session_close_ms
    Reference: Same FIFO method as above; session window from the canonical
        NYSE calendar module (``app/lean_sidecar/trading_calendar.py``).
    Canonical implementation: this file (derived from ``compute_fifo_pnl``).
    Validated against: PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py

    The session window is supplied by the caller to keep this function pure
    (no calendar I/O).  The caller derives it from
    ``trading_calendar.session_window_for_date``.
    """
    result = compute_fifo_pnl(fills)  # run over complete history
    return realized_pnl_for_window(
        result.closed_lots,
        session_open_ms=session_open_ms,
        session_close_ms=session_close_ms,
    )
