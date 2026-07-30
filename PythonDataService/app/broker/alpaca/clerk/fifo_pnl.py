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
    # result.open_pnl      — None until a mark_price is supplied
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
        ``None`` when no current mark price is available (the bot has open
        exposure but the caller did not supply a mark).  ``0.0`` when there
        is no open exposure (fully flat).

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
    fee_total: float | None = None
    closed_lots: list[ClosedLot] = field(default_factory=list)
    open_lots: list[OpenLot] = field(default_factory=list)


# ── FIFO engine ───────────────────────────────────────────────────────────────


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
        Optional ``{symbol: current_price}`` for open-P&L calculation.  When
        omitted (or the symbol is missing), ``open_pnl`` stays ``None`` for
        that symbol.

    Notes
    -----
    - Reversals are handled naturally: a BUY after a short lot closes the
      short (FIFO); any excess opens a new long lot.
    - Partial fills accumulate into lots by fill event — each ``FillRecord``
      is one broker execution event.
    - Fee tracking: ``fee_total`` is ``None`` when any fill reports no fee
      (``fee=None``). This propagates "Fees not reported" honestly rather than
      masking unknown fees as $0.
    """
    # Per-symbol FIFO lot queues: deque is ordered oldest-first (FIFO).
    lots: dict[str, deque[_Lot]] = {}
    result = PnLResult()
    any_fee_missing = False

    for fill in fills:
        sym = fill.symbol
        price = fill.fill_price
        qty = fill.quantity  # always positive (sign carried by side)
        ts = fill.filled_at_ms

        # Fee tracking
        if fill.fee is None:
            any_fee_missing = True
        elif not any_fee_missing:
            result.fee_total = (result.fee_total or 0.0) + fill.fee

        queue = lots.setdefault(sym, deque())
        remaining = qty

        if not queue:
            # No open lots: open a new lot
            queue.append(_Lot(qty=remaining, cost=price, opened_at_ms=ts, side=fill.side))
            remaining = 0.0
        else:
            # Check if this fill is in the same direction or opposite
            top_lot = queue[0]
            if fill.side == top_lot.side:
                # Same direction — add to the lot stack (new lot at the back)
                queue.append(
                    _Lot(qty=remaining, cost=price, opened_at_ms=ts, side=fill.side)
                )
                remaining = 0.0
            else:
                # Opposite direction — close existing lots (FIFO)
                while remaining > _ZERO_ABS_TOL and queue and queue[0].side != fill.side:
                    lot = queue[0]
                    close_qty = min(remaining, lot.qty)
                    # Realized P&L for this partial or full lot closure
                    if lot.side is OrderSide.BUY:
                        r_pnl = (price - lot.cost) * close_qty
                    else:
                        r_pnl = (lot.cost - price) * close_qty
                    closed_lot = ClosedLot(
                        symbol=sym,
                        qty=close_qty,
                        entry_price=lot.cost,
                        exit_price=price,
                        opened_at_ms=lot.opened_at_ms,
                        closed_at_ms=ts,
                        realized_pnl=r_pnl,
                        fee=fill.fee,
                    )
                    result.closed_lots.append(closed_lot)
                    result.realized_pnl += r_pnl
                    remaining -= close_qty
                    lot.qty -= close_qty
                    if math.isclose(lot.qty, 0.0, rel_tol=0.0, abs_tol=_ZERO_ABS_TOL):
                        queue.popleft()

                # Any remaining quantity after closing opens a new lot (reversal)
                if remaining > _ZERO_ABS_TOL:
                    queue.append(
                        _Lot(qty=remaining, cost=price, opened_at_ms=ts, side=fill.side)
                    )

    # Propagate fee_total honesty
    if any_fee_missing:
        result.fee_total = None

    # Build open lots + compute open P&L
    total_open_pnl: float | None = None
    for sym, queue in lots.items():
        mark = (mark_prices or {}).get(sym)
        for lot in queue:
            if lot.qty <= _ZERO_ABS_TOL:
                continue
            result.open_lots.append(
                OpenLot(
                    symbol=sym,
                    qty=lot.qty,
                    cost=lot.cost,
                    opened_at_ms=lot.opened_at_ms,
                    side=lot.side,
                )
            )
            if mark is not None:
                if lot.side is OrderSide.BUY:
                    pnl_lot = (mark - lot.cost) * lot.qty
                else:
                    pnl_lot = (lot.cost - mark) * lot.qty
                total_open_pnl = (total_open_pnl or 0.0) + pnl_lot

    if not result.open_lots:
        result.open_pnl = 0.0
    elif total_open_pnl is not None:
        result.open_pnl = total_open_pnl
    # else: open lots exist but no mark price — open_pnl stays None

    return result


def realized_pnl_today(
    fills: Iterable[FillRecord],
    *,
    session_open_ms: int,
    session_close_ms: int,
) -> float:
    """Realized P&L for fills whose close occurred within today's session window.

    Formula:
        Filter fills to those with ``filled_at_ms in [session_open_ms, session_close_ms)``,
        then apply ``compute_fifo_pnl`` on that subset.
    Reference: Same FIFO method as above; session window from the canonical
        NYSE calendar module (``app/lean_sidecar/trading_calendar.py``).
    Canonical implementation: this file (derived from ``compute_fifo_pnl``).
    Validated against: PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py

    The session window is supplied by the caller to keep this function pure
    (no calendar I/O).  The caller derives it from
    ``trading_calendar.session_window_for_date``.
    """
    today_fills = [
        f
        for f in fills
        if session_open_ms <= f.filled_at_ms < session_close_ms
    ]
    if not today_fills:
        return 0.0
    return compute_fifo_pnl(today_fills).realized_pnl
