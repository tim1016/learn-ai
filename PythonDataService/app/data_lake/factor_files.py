"""LEAN factor-file CSV builder.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.1

LEAN factor-file format (factor_files/<sym>.csv under the equity/usa subtree):
  date,price_factor,split_factor,reference_price
  - date: YYYYMMDD of the last completed trading session *before* the
    corporate action's ex-date. LEAN applies the event on the next
    trading day, so a row dated D produces an event on D's successor.
  - price_factor: cumulative dividend back-adjustment multiplier.
  - split_factor: cumulative split-adjustment multiplier.
  - reference_price: the raw close on the row's date.

The reference price is NOT optional and must be positive. LEAN's
``DividendEventProvider`` divides the cash dividend by it; a zero or
missing reference price raises ``InvalidOperationException: Zero
reference price``, which kills the subscription worker and silently
truncates the backtest at the first in-window dividend. An earlier
revision of this module emitted ``reference_price=0`` on every row
under the (false) belief that LEAN ignores the column — see the
cross-engine parity-matrix incident where a 6-month SPY backtest ran
only ~35 days.

Math mirrors LEAN's own ``FactorFileGenerator``: walking corporate
actions newest-to-oldest,

  reference_price = close of the trading session before the ex-date
  price_factor    = next_factor * (1 - cash_amount * split_factor / reference_price)

and each split contributes ``split_from / split_to`` to the cumulative
split factor.

Only corporate actions whose ex-date falls inside [history_start,
history_end] are emitted: a windowed capture cannot price actions
outside its own data, and a backtest in that window never encounters
them. If an in-window action has no positive reference close in the
capture, the build fails loudly rather than emitting a poison row.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent

_FACTOR_QUANTUM = Decimal("0.0000000001")


class FactorFileReferenceError(ValueError):
    """An in-window corporate action has no positive reference close."""


def build_factor_file_bytes(
    symbol: str,
    splits: list[SplitEvent],
    dividends: list[DividendEvent],
    history_start: date,
    history_end: date,
    daily_closes: Mapping[date, Decimal],
) -> bytes:
    """Build the deterministic factor-file CSV body for one symbol.

    ``daily_closes`` maps each regular-trading-hours session date to its
    RTH close. It must cover the trading session before every in-window
    corporate action; the caller derives it from the captured minute
    bars (see ``derived_daily.rth_daily_closes``).

    Splits/dividends must be sorted ascending by date (polygon_corp_actions
    returns them that way). The returned bytes are ASCII CSV without a
    header row, which is what LEAN expects.

    Raises ``FactorFileReferenceError`` when an in-window corporate
    action has no positive reference close in ``daily_closes``.
    """
    closes: dict[date, Decimal] = {d: Decimal(v) for d, v in daily_closes.items()}
    session_dates: list[date] = sorted(closes)

    events = _merge_events(splits, dividends)
    in_window = [ev for ev in events if history_start <= _event_date(ev) <= history_end]

    # Walk corporate actions newest-to-oldest, accumulating the cumulative
    # price/split factors. Each event row carries the factors that apply
    # to raw data up to and including that row's date.
    price_factor = Decimal(1)
    split_factor = Decimal(1)
    event_rows: list[tuple[date, Decimal, Decimal, Decimal]] = []
    for ev in reversed(in_window):
        ex_date = _event_date(ev)
        row_date = _trading_day_before(ex_date, session_dates)
        if row_date is None:
            raise FactorFileReferenceError(
                f"{symbol}: corporate action ex-date {ex_date.isoformat()} has no "
                f"prior trading session in the capture (window starts "
                f"{history_start.isoformat()}); cannot resolve a reference price"
            )
        reference_price = closes[row_date]
        if reference_price <= 0:
            raise FactorFileReferenceError(
                f"{symbol}: reference close on {row_date.isoformat()} is "
                f"{reference_price}; a non-positive reference price makes LEAN's "
                f"DividendEventProvider throw and truncates the backtest"
            )
        if isinstance(ev, DividendEvent):
            cash = Decimal(str(ev.cash_amount))
            price_factor = price_factor * (Decimal(1) - cash * split_factor / reference_price)
        else:
            split_factor = split_factor * (Decimal(str(ev.split_from)) / Decimal(str(ev.split_to)))
        event_rows.append((row_date, price_factor, split_factor, reference_price))
    event_rows.reverse()

    # history_start carries the fully-cumulated (oldest) factors; the row
    # is not dividend-processed by LEAN but its reference price still
    # must be positive, so anchor it to the nearest available close.
    rows: list[tuple[date, Decimal, Decimal, Decimal]] = [
        (
            history_start,
            price_factor,
            split_factor,
            _anchor_reference(history_start, closes, session_dates),
        ),
        *event_rows,
        (
            history_end,
            Decimal(1),
            Decimal(1),
            _anchor_reference(history_end, closes, session_dates),
        ),
    ]

    body = "\n".join(f"{_yyyymmdd(d)},{_fmt_factor(pf)},{_fmt_factor(sf)},{_fmt_price(rp)}" for d, pf, sf, rp in rows)
    return (body + "\n").encode("ascii")


def _event_date(ev: SplitEvent | DividendEvent) -> date:
    raw = ev.execution_date if isinstance(ev, SplitEvent) else ev.ex_dividend_date
    return date.fromisoformat(raw)


def _merge_events(splits: list[SplitEvent], dividends: list[DividendEvent]) -> list[SplitEvent | DividendEvent]:
    """Merge splits + dividends into one chronologically-sorted list."""
    return sorted([*splits, *dividends], key=_event_date)


def _trading_day_before(d: date, session_dates: list[date]) -> date | None:
    """Largest session date strictly earlier than ``d``; None if none exists."""
    idx = bisect_left(session_dates, d)
    return session_dates[idx - 1] if idx > 0 else None


def _anchor_reference(d: date, closes: Mapping[date, Decimal], session_dates: list[date]) -> Decimal:
    """Reference close for an anchor row: the close on ``d``, else the
    nearest earlier session, else the earliest session available."""
    if d in closes:
        return closes[d]
    if not session_dates:
        raise FactorFileReferenceError("daily_closes is empty; cannot anchor the factor file with a reference price")
    idx = bisect_left(session_dates, d)
    return closes[session_dates[idx - 1]] if idx > 0 else closes[session_dates[0]]


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _fmt_factor(x: Decimal) -> str:
    """Factor formatted at 10 dp, trailing zeros stripped, fixed notation."""
    return format(x.quantize(_FACTOR_QUANTUM).normalize(), "f")


def _fmt_price(x: Decimal) -> str:
    """Reference price in fixed (non-scientific) notation."""
    return format(x.normalize(), "f")
