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
    Arithmetic: each recorded fill quantity and price is normalized once by
        ``money.normalize_money``; lots, closures, open valuation and totals
        are then exact ``Decimal`` under ``money.money_context`` (inexact
        arithmetic raises).  The ``exact_*`` fields are the money authority
        (custody budgets and simulated account equity read them).  Float
        attributes such as ``qty``, ``realized_pnl`` and ``open_pnl`` are
        display views: the exact value rounded once, never re-normalized into
        money.

Reference:
    Standard FIFO inventory method (GAAP / IFRS).  No external software port —
    well-known accounting arithmetic.  See Kieso, Weygandt & Warfield,
    *Intermediate Accounting* (17e), Chapter 8 (Inventories: Measurement).
    The .NET FIFO engine over EF/Postgres lots (``PositionEngine.cs``) was
    removed with the Portfolio page (#2756).  This file is the canonical
    implementation for the broker-v2 bot-panel P&L path, whose fills live in
    the Alpaca order journal.
    Validated against:
        PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py
        (hand-derived fixtures at atol=1e-9 for float views; an exact
        ``Fraction`` oracle for the ``exact_*`` fields).
Canonical implementation: this file.

Usage::

    from app.broker.alpaca.clerk.fills import project_instance_fills
    from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl, PnLResult

    fills = project_instance_fills(sid, journal.read_all())
    result = compute_fifo_pnl(fills)
    # result.exact_realized_pnl — closed lots only, exact Decimal (money authority)
    # result.realized_pnl  — its float display view; 0.0 on no closed trades
    # result.exact_open_pnl — None until mark_prices are supplied for ALL open symbols
    # result.open_pnl      — its float display view
    # result.marks_complete — True only when all open-lot symbols have mark coverage
    # result.fee_total     — None when any fill has fee=None ("Fees not reported")
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.money import ZERO, money_context, normalize_money
from app.broker.contract.models import OrderSide

# ── Lot-closing share tolerance ───────────────────────────────────────────────
# The FIFO matching rule: a lot at or below this many shares is closed, and a
# fill remainder at or below it opens nothing.  Quantities are exact, so it
# only absorbs dust already present in recorded fill values.
_ZERO_ABS_TOL = Decimal("1e-9")


# ── Internal lot record ───────────────────────────────────────────────────────


@dataclass
class _Lot:
    """An open position lot (FIFO queue entry)."""

    qty: Decimal        # positive remaining share count (exact)
    cost: Decimal       # cost per share (entry fill price, exact)
    opened_at_ms: int   # int64 ms UTC
    side: OrderSide     # BUY (long lot) or SELL (short lot)
    strategy_instance_id: str


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
    exact_realized_pnl: Decimal
    fee: float | None     # None = not reported
    entry_strategy_instance_id: str = ""
    exit_strategy_instance_id: str = ""

    @property
    def realized_pnl(self) -> float:
        """Display view of ``exact_realized_pnl``, rounded once."""
        return float(self.exact_realized_pnl)


@dataclass(frozen=True)
class OpenLot:
    """One remaining open lot for open-P&L computation."""

    symbol: str
    exact_qty: Decimal
    exact_cost: Decimal   # cost per share
    opened_at_ms: int     # int64 ms UTC
    side: OrderSide

    @property
    def qty(self) -> float:
        """Display view of ``exact_qty``, rounded once."""
        return float(self.exact_qty)

    @property
    def cost(self) -> float:
        """Display view of ``exact_cost``, rounded once."""
        return float(self.exact_cost)


@dataclass
class PnLResult:
    """The full FIFO P&L result for one bot.

    ``exact_realized_pnl``:
        Exact sum of P&L from all closed lots across the bot's lifetime.
        ``0`` when no lots have been closed.  ``realized_pnl`` is its float
        display view.

    ``exact_open_pnl``:
        Exact open valuation of the remaining lots at the supplied marks.
        ``None`` when no current mark price is available OR when any open-lot
        symbol is missing a mark (marks_complete=False).  ``0`` when there
        is no open exposure (fully flat, marks_complete=True).  Non-None only
        when ALL open-lot symbols have mark coverage.  ``open_pnl`` is its
        float display view.

        IMPORTANT: it is never a partial sum.  If SPY and AAPL both have open
        lots but only SPY has a mark, ``exact_open_pnl`` is ``None`` (not just
        SPY's unrealized).  Callers must check ``marks_complete`` before using
        it.

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

    exact_realized_pnl: Decimal = ZERO
    exact_open_pnl: Decimal | None = None
    marks_complete: bool = False
    fee_total: float | None = None
    closed_lots: list[ClosedLot] = field(default_factory=list)
    open_lots: list[OpenLot] = field(default_factory=list)

    @property
    def realized_pnl(self) -> float:
        """Display view of ``exact_realized_pnl``, rounded once."""
        return float(self.exact_realized_pnl)

    @property
    def open_pnl(self) -> float | None:
        """Display view of ``exact_open_pnl``, rounded once."""
        return None if self.exact_open_pnl is None else float(self.exact_open_pnl)


@dataclass(frozen=True)
class OpenPnLResult:
    """Open-lot valuation shared by batch FIFO and the incremental cache."""

    exact_value: Decimal | None
    marks_complete: bool
    open_lots: tuple[OpenLot, ...]


# ── FIFO engine ───────────────────────────────────────────────────────────────


@money_context()
def apply_fill_to_lots(
    lots: dict[str, deque[_Lot]],
    fill: FillRecord,
    closed_out: list[ClosedLot],
) -> None:
    """Apply one fill to the FIFO lot queues in-place.

    Extracted from ``compute_fifo_pnl``'s inner loop so that the SQLite economic cache
    can maintain FIFO state incrementally (O(1) per fill on append) without
    calling ``compute_fifo_pnl`` over the full history on every read.

    Parameters
    ----------
    lots:
        Per-symbol FIFO lot queues (oldest-first).  Mutated in-place.
    fill:
        The new fill to apply.
    closed_out:
        List to which newly closed ``ClosedLot`` records are appended.  Their
        exact P&L sums to the realized total, so no accumulator is needed.

    Formula:
        Same FIFO rules as ``compute_fifo_pnl``.  See module docstring.
    Canonical implementation: this file (delegate of compute_fifo_pnl).
    Validated against:
        PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py and
        PythonDataService/tests/broker/alpaca/clerk/sqlite/test_economic_projection.py.
    """
    sym = fill.symbol
    # Normalize each recorded value once; every product below is exact.
    price = normalize_money(fill.fill_price)
    qty = normalize_money(fill.quantity)
    ts = fill.filled_at_ms

    queue = lots.setdefault(sym, deque())
    remaining = qty

    if not queue:
        queue.append(
            _Lot(
                qty=remaining,
                cost=price,
                opened_at_ms=ts,
                side=fill.side,
                strategy_instance_id=fill.sid,
            )
        )
        remaining = ZERO
    else:
        top_lot = queue[0]
        if fill.side == top_lot.side:
            queue.append(
                _Lot(
                    qty=remaining,
                    cost=price,
                    opened_at_ms=ts,
                    side=fill.side,
                    strategy_instance_id=fill.sid,
                )
            )
            remaining = ZERO
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
                        qty=float(close_qty),
                        entry_price=float(lot.cost),
                        exit_price=float(price),
                        opened_at_ms=lot.opened_at_ms,
                        closed_at_ms=ts,
                        exact_realized_pnl=r_pnl,
                        fee=fill.fee,
                        entry_strategy_instance_id=lot.strategy_instance_id,
                        exit_strategy_instance_id=fill.sid,
                    )
                )
                remaining -= close_qty
                lot.qty -= close_qty
                if abs(lot.qty) <= _ZERO_ABS_TOL:
                    queue.popleft()

            if remaining > _ZERO_ABS_TOL:
                queue.append(
                    _Lot(
                        qty=remaining,
                        cost=price,
                        opened_at_ms=ts,
                        side=fill.side,
                        strategy_instance_id=fill.sid,
                    )
                )


@money_context()
def compute_open_pnl(
    lots: dict[str, deque[_Lot]],
    mark_prices: Mapping[str, float | Decimal],
) -> OpenPnLResult:
    """Value current FIFO lots without ever returning a partial portfolio sum.

    Formula: open_pnl = Σ (mark - cost) × signed remaining lot quantity.
    Reference: Kieso, Weygandt & Warfield, *Intermediate Accounting* (17e),
      Chapter 8; same FIFO inventory model as this module.
    Canonical implementation: this file.
    Validated against:
      PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py;
      PythonDataService/tests/broker/alpaca/clerk/sqlite/test_economic_projection.py.

    ``exact_value`` is ``None`` until every symbol with an open lot has a
    mark.  Flat state is complete and has a value of exactly ``0``.  Each mark
    is normalized once; ``exact_value`` is the exact sum.  Its consumers
    carry it as their own ``exact_open_pnl`` beside a float display view.
    """
    open_lots: list[OpenLot] = []
    symbols_with_open_lots: set[str] = set()
    total_open_pnl = ZERO

    for symbol, queue in lots.items():
        mark = mark_prices.get(symbol)
        exact_mark = None if mark is None else normalize_money(mark)
        for lot in queue:
            if lot.qty <= _ZERO_ABS_TOL:
                continue
            symbols_with_open_lots.add(symbol)
            open_lots.append(
                OpenLot(
                    symbol=symbol,
                    exact_qty=lot.qty,
                    exact_cost=lot.cost,
                    opened_at_ms=lot.opened_at_ms,
                    side=lot.side,
                )
            )
            if exact_mark is not None:
                signed_delta = (
                    exact_mark - lot.cost
                    if lot.side is OrderSide.BUY
                    else lot.cost - exact_mark
                )
                total_open_pnl += signed_delta * lot.qty

    if not open_lots:
        return OpenPnLResult(exact_value=ZERO, marks_complete=True, open_lots=())
    if mark_prices.keys() >= symbols_with_open_lots:
        return OpenPnLResult(
            exact_value=total_open_pnl,
            marks_complete=True,
            open_lots=tuple(open_lots),
        )
    return OpenPnLResult(
        exact_value=None,
        marks_complete=False,
        open_lots=tuple(open_lots),
    )


@money_context()
def realized_pnl_for_window(
    closed_lots: Iterable[ClosedLot],
    *,
    session_open_ms: int,
    session_close_ms: int,
) -> float:
    """Sum canonical closed-lot P&L inside a half-open session window.

    Formula: realized_window = Σ lot.exact_realized_pnl where
      session_open_ms <= lot.closed_at_ms < session_close_ms, summed exactly
      and rounded once to the returned float display value.
    Reference: same FIFO inventory method as this module; session boundaries
      are supplied by the canonical NYSE calendar.
    Canonical implementation: this file.
    Validated against:
      PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py;
      PythonDataService/tests/broker/alpaca/clerk/sqlite/test_economic_projection.py.
    """
    return float(sum(
        (
            lot.exact_realized_pnl
            for lot in closed_lots
            if session_open_ms <= lot.closed_at_ms < session_close_ms
        ),
        ZERO,
    ))


@money_context()
def compute_fifo_pnl(
    fills: Iterable[FillRecord],
    *,
    mark_prices: Mapping[str, float | Decimal] | None = None,
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
        Optional ``{symbol: current_price}`` for open-P&L calculation; each
        recorded price is normalized once.  ``exact_open_pnl`` is ``None``
        unless ALL open-lot symbols appear in this mapping.  A partial mark
        set never produces a partial sum — it remains ``None`` when any
        symbol is uncovered.

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
      have a mark in ``mark_prices``.  Check this before using
      ``exact_open_pnl``.
    """
    lots: dict[str, deque[_Lot]] = {}
    result = PnLResult()
    any_fee_missing = False

    for fill in fills:
        # Fee tracking
        if fill.fee is None:
            any_fee_missing = True
        elif not any_fee_missing:
            result.fee_total = (result.fee_total or 0.0) + fill.fee

        apply_fill_to_lots(lots, fill, result.closed_lots)

    result.exact_realized_pnl = sum((lot.exact_realized_pnl for lot in result.closed_lots), ZERO)

    # Propagate fee_total honesty
    if any_fee_missing:
        result.fee_total = None

    open_result = compute_open_pnl(lots, mark_prices or {})
    result.open_lots = list(open_result.open_lots)
    result.exact_open_pnl = open_result.exact_value
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
