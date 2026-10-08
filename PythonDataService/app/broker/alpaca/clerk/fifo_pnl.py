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
    Option contracts (ADR 0076 decision 13): a fill that carries a contract ``multiplier``
        m counts contracts, at a premium per share.  Every product above is
        then taken over ``qty × m`` shares:
        realized_pnl_lot = signed_delta × closed_qty × m
        open_pnl         = Σ signed_delta × lot.qty × m
        cost basis       = lot.qty × lot.cost × m
        A fill without one is shares and the arithmetic is unchanged.  A
        symbol holds one multiplier: a fill that disagrees with its symbol's
        open lots is refused.
    Written-off lots (#2975): a residue discharge takes a bot's attributed
        position off the books and closes no lot.  Supplied to the walk as one
        :class:`LotWriteOff` each, the oldest lots of that owner and symbol
        are set apart: no later fill of the bot's closes them and they realise
        nothing.  The one fill that may close a set-apart lot is one whose
        credit a discharge withheld (#2854) -- the discharge stood in for
        exactly that execution.  A write-off itself books no P&L: the loss or
        gain of a close is the cost of the lot that ended.  For valuation a
        set-apart lot stays an open lot, marked ``written_off`` (ADR 0076
        decision 22): the budget carries its premium as a position, and
        equity marks it as any open lot.
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
        ``Fraction`` oracle for the ``exact_*`` fields);
        PythonDataService/tests/broker/alpaca/clerk/test_option_custody_money.py
        (the multiplier, exact).
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
from app.broker.alpaca.clerk.money import (
    ZERO,
    MoneyInputError,
    contract_multiplier,
    money_context,
    normalize_money,
)
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
    # The contract's multiplier when the lot is option contracts; None for shares.
    multiplier: int | None = None
    # Set apart by a write-off (#2975): never closed by the bot's own later
    # fills, never realised, never open exposure. Closable only by a fill
    # whose credit a discharge withheld (#2854).
    written_off: bool = False


@dataclass(frozen=True)
class LotWriteOff:
    """One operator write-off of a bot's attributed position (#2975).

    Carries a residue discharge into the FIFO walk, at the ledger place the
    discharge recorded. The discharge takes the whole attributed position of
    one owner in one symbol, so its quantity is the whole then-open position
    and the walk sets the oldest lots of that owner and symbol apart, up to
    that quantity, whatever filled after.

    All timestamps are ``int64 ms UTC``; ``exact_qty`` is signed exactly as
    the discharge recorded it (positive: a long position was written off).
    ``sid`` is the owner key exactly as the walked fills' ``sid`` fields state
    it, so an account-wide walk and a one-bot walk match their own records.
    """

    symbol: str
    exact_qty: Decimal          # signed, as the discharge recorded it (exact)
    written_off_at_ms: int      # int64 ms UTC
    ledger_sequence: int
    sid: str = ""


def _shares(qty: Decimal, multiplier: int | None) -> Decimal:
    """The shares a quantity stands for: itself, or contracts by their multiplier."""
    return qty if multiplier is None else qty * multiplier


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
    # Set when ``qty`` is option contracts and the prices are premiums per share.
    multiplier: int | None = None

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
    # Set when ``exact_qty`` is option contracts and ``exact_cost`` a premium per share.
    multiplier: int | None = None
    # True when a write-off set this lot apart (#2975): it closes for no fill
    # of the bot's and realises nothing, but it stays an open lot for
    # valuation, as ADR 0076 decision 22 records.
    written_off: bool = False

    @property
    def exact_cost_basis(self) -> Decimal:
        """What the lot's remaining quantity cost to open, exact: qty × cost, by the multiplier for contracts."""
        with money_context():
            return _shares(self.exact_qty, self.multiplier) * self.exact_cost

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
        ``closed_at_ms``.  Feeds realized P&L for a session window.

    ``open_lots``:
        Remaining open lots (FIFO remainder).  Feeds the open-P&L valuation.

    ``written_off_lots``:
        Lots a write-off set apart (#2975).  They realise nothing and close
        for no later fill of the bot's, but stay open lots for valuation;
        this is the same population as ``open_lots`` flagged ``written_off``,
        kept for the readers that ask for them by name.
    """

    exact_realized_pnl: Decimal = ZERO
    exact_open_pnl: Decimal | None = None
    marks_complete: bool = False
    fee_total: float | None = None
    closed_lots: list[ClosedLot] = field(default_factory=list)
    open_lots: list[OpenLot] = field(default_factory=list)
    written_off_lots: list[OpenLot] = field(default_factory=list)

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
    multiplier = None if fill.multiplier is None else contract_multiplier(fill.multiplier)

    queue = lots.setdefault(sym, deque())
    remaining = qty

    def closable(lot: _Lot) -> bool:
        # A written-off lot closes for no fill of the bot's own, except the
        # one fill whose credit a discharge withheld: the execution the
        # write-off stood in for (#2854, #2975).
        return fill.credit_withheld or not lot.written_off

    if queue and queue[0].multiplier != multiplier:
        # One symbol is one instrument. Lots of contracts and of shares, or of
        # two multipliers, under one symbol cannot be closed against each other.
        raise MoneyInputError(
            f"A fill of {sym} counts {_counted_as(multiplier)}, "
            f"but its open lots count {_counted_as(queue[0].multiplier)}."
        )

    head = next((lot for lot in queue if closable(lot)), None)
    if head is None or head.side == fill.side:
        queue.append(
            _Lot(
                qty=remaining,
                cost=price,
                opened_at_ms=ts,
                side=fill.side,
                strategy_instance_id=fill.sid,
                multiplier=multiplier,
            )
        )
        remaining = ZERO
    else:
        index = 0
        while remaining > _ZERO_ABS_TOL and index < len(queue):
            lot = queue[index]
            if not closable(lot):
                index += 1
                continue
            if lot.side == fill.side:
                break
            close_qty = min(remaining, lot.qty)
            if lot.side is OrderSide.BUY:
                r_pnl = (price - lot.cost) * _shares(close_qty, multiplier)
            else:
                r_pnl = (lot.cost - price) * _shares(close_qty, multiplier)
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
                    multiplier=multiplier,
                )
            )
            remaining -= close_qty
            lot.qty -= close_qty
            if abs(lot.qty) <= _ZERO_ABS_TOL:
                del queue[index]
            else:
                index += 1

        if remaining > _ZERO_ABS_TOL:
            queue.append(
                _Lot(
                    qty=remaining,
                    cost=price,
                    opened_at_ms=ts,
                    side=fill.side,
                    strategy_instance_id=fill.sid,
                    multiplier=multiplier,
                )
            )


@money_context()
def apply_write_off_to_lots(
    lots: dict[str, deque[_Lot]],
    write_off: LotWriteOff,
) -> None:
    """Set a write-off's lots apart in the FIFO queues in-place (#2975).

    A residue discharge takes one owner's whole attributed position in one
    symbol off the books and closes no lot, so the oldest lots of that owner
    and symbol -- up to the discharged quantity -- are the contracts it wrote
    off: a write-off takes what was held then, and whatever the bot bought
    since is newer. They are marked ``written_off`` in place, splitting a lot
    the discharge only partly covers.

    In an account-wide walk one owner's sale may already have consumed
    another owner's older lot (the cross-owner close the mixed queue allows),
    so the owner's own tagged lots can fall short of the discharged quantity.
    What remains is then taken from the symbol's oldest same-side lots
    whoever holds them: the quantity is what the account's fold consumes, and
    it must leave the closable books however the closes were attributed. A
    one-owner walk never reaches that step -- its queue is the owner's own,
    and the discharge proved it covered the then-open position.

    A discharged quantity beyond the lots on hand marks what is there and no
    more: the reader's fills-vs-positions proof fails closed on books that
    disagree.

    Formula: lot.written_off' = true for the oldest min(|discharged_qty|,
      open same-side lots) lots of the symbol, the owner's own taken first.
    Reference: none external; ADR 0076 decision 22 and #2975 for the
      written-off rule, #2854 for the withheld credit that may still close
      these lots.
    Canonical implementation: this file.
    Validated against:
        PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py
        (the write-off cases).
    """
    queue = lots.get(write_off.symbol)
    if not queue:
        return
    side = OrderSide.BUY if write_off.exact_qty > 0 else OrderSide.SELL
    remaining = abs(write_off.exact_qty)
    index = 0
    while remaining > _ZERO_ABS_TOL and index < len(queue):
        lot = queue[index]
        if lot.written_off or lot.side is not side or lot.strategy_instance_id != write_off.sid:
            index += 1
            continue
        remaining = _take_as_written_off(queue, index, remaining)
    index = 0
    while remaining > _ZERO_ABS_TOL and index < len(queue):
        lot = queue[index]
        if lot.written_off or lot.side is not side:
            index += 1
            continue
        remaining = _take_as_written_off(queue, index, remaining)


def _take_as_written_off(queue: deque[_Lot], index: int, quantity: Decimal) -> Decimal:
    """Mark up to ``quantity`` of ``queue[index]`` written off; return what is left.

    A lot the quantity only partly covers is split: its oldest part is
    written off, the rest stays closable behind it.
    """
    lot = queue[index]
    if lot.qty <= quantity + _ZERO_ABS_TOL:
        lot.written_off = True
        return quantity - lot.qty
    queue.insert(
        index + 1,
        _Lot(
            qty=lot.qty - quantity,
            cost=lot.cost,
            opened_at_ms=lot.opened_at_ms,
            side=lot.side,
            strategy_instance_id=lot.strategy_instance_id,
            multiplier=lot.multiplier,
        ),
    )
    lot.qty = quantity
    lot.written_off = True
    return ZERO


def _counted_as(multiplier: int | None) -> str:
    return "shares" if multiplier is None else f"contracts of {multiplier} shares"


@money_context()
def compute_open_pnl(
    lots: dict[str, deque[_Lot]],
    mark_prices: Mapping[str, float | Decimal],
) -> OpenPnLResult:
    """Value current FIFO lots without ever returning a partial portfolio sum.

    Formula: open_pnl = Σ (mark - cost) × signed remaining lot quantity, the
      quantity taken by its multiplier for a lot of option contracts.
      Written-off lots stay open lots and are valued with the rest (#2975,
      ADR 0076 decision 22): the budget carries their premium and equity
      marks them, though they close for no fill of the bot's.
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
                    multiplier=lot.multiplier,
                    written_off=lot.written_off,
                )
            )
            if exact_mark is not None:
                signed_delta = (
                    exact_mark - lot.cost
                    if lot.side is OrderSide.BUY
                    else lot.cost - exact_mark
                )
                total_open_pnl += signed_delta * _shares(lot.qty, lot.multiplier)

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


def merged_fifo_events(
    fills: Iterable[FillRecord],
    write_offs: Iterable[LotWriteOff],
) -> tuple[FillRecord | LotWriteOff, ...]:
    """Fills and write-offs as one stream, each at its ledger place (#2975).

    A fill stands at ``(filled_at_ms, ledger_sequence)``, a write-off at
    ``(written_off_at_ms, ledger_sequence)``. The ledger's sequences are
    unique, so the two never tie; if one ever did, the fill comes first.
    Both the batch fold and the SQLite incremental cache order their stream
    by this one rule, so an incremental answer and a full rebuild close the
    same lots.
    """
    events: list[tuple[tuple[int, int], int, FillRecord | LotWriteOff]] = []
    for fill in fills:
        events.append(((fill.filled_at_ms, fill.ledger_sequence), 0, fill))
    for write_off in write_offs:
        events.append(((write_off.written_off_at_ms, write_off.ledger_sequence), 1, write_off))
    return tuple(event for _, _, event in sorted(events, key=lambda event: event[:2]))


@money_context()
def compute_fifo_pnl(
    fills: Iterable[FillRecord],
    *,
    mark_prices: Mapping[str, float | Decimal] | None = None,
    write_offs: Iterable[LotWriteOff] = (),
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
    write_offs:
        Residue discharges as :class:`LotWriteOff` events (#2975), each
        applied at its own ledger place: the fill stream and the write-off
        stream are walked as one, ordered by ``(event ms, ledger sequence)``.
        Default ``()`` folds fills exactly as before the write-off rule
        existed.

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

    # One walk over fills and write-offs together, each at its ledger place:
    # a write-off sets apart what was held when it was recorded, and every
    # fill after it closes only what was bought since (#2975).
    for event in merged_fifo_events(fills, write_offs):
        if not isinstance(event, FillRecord):
            apply_write_off_to_lots(lots, event)
            continue
        fill = event
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

    result.written_off_lots = [
        OpenLot(
            symbol=symbol,
            exact_qty=lot.qty,
            exact_cost=lot.cost,
            opened_at_ms=lot.opened_at_ms,
            side=lot.side,
            multiplier=lot.multiplier,
            written_off=True,
        )
        for symbol, queue in lots.items()
        for lot in queue
        if lot.written_off and lot.qty > _ZERO_ABS_TOL
    ]

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
    write_offs: Iterable[LotWriteOff] = (),
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
    result = compute_fifo_pnl(fills, write_offs=write_offs)  # run over complete history
    return realized_pnl_for_window(
        result.closed_lots,
        session_open_ms=session_open_ms,
        session_close_ms=session_close_ms,
    )
