"""Reverse dividend adjustment on TradingView prices.

TradingView's chart shows dividend-adjusted prices by default: a bar's
displayed price equals the actual trade price minus the sum of all
dividends paid between that bar and "today." Polygon's
``adjusted=True`` flag adjusts only for splits, NOT for dividends.

This module reverses TradingView's dividend adjustment by **adding back**
each dividend to all OHLC bars dated before that dividend's ex-date.
The result is a TV DataFrame whose prices are directly comparable with
Polygon's split-adjusted-only prices.

Usage:
    tv_unadj = reverse_dividend_adjustment(tv_df, SPY_DIVIDENDS)

Long-term we should fetch dividend events from Polygon's
``/v3/reference/dividends`` endpoint so this stays current. The ETF SPY
distributes quarterly with ex-dates around the third Friday of
March / June / September / December.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DividendEvent:
    ex_date: date  # First trading day on which buyer no longer entitled.
    cash_amount: float  # Cash dividend per share, USD.
    ticker: str = "SPY"


# Hard-coded SPY dividend events covering the current research window.
# Values were recovered empirically from the TV-vs-Polygon close-price step
# changes at each ex-date in the uploaded 15-min CSV (see exploration
# in chat log). They should be replaced with values fetched from Polygon
# (`/v3/reference/dividends?ticker=SPY`) once the live ingest path runs.
SPY_DIVIDENDS: tuple[DividendEvent, ...] = (
    # SPY's ex-date is the third Friday of the quarter-end month. The step
    # observed in the data confirms the ex-date is when bars "go unadjusted"
    # on the TV chart: Dec 19 2025 is a Friday, Mar 20 2026 is a Friday.
    DividendEvent(date(2025, 12, 19), 1.98),
    DividendEvent(date(2026, 3, 20), 1.64),
)


def reverse_dividend_adjustment(
    df: pd.DataFrame,
    dividends: tuple[DividendEvent, ...] = SPY_DIVIDENDS,
    et_col: str = "et",
    price_cols: tuple[str, ...] = ("open", "high", "low", "close"),
) -> pd.DataFrame:
    """Add dividends back into ``df`` so prices match the unadjusted feed.

    For each dividend ``D`` with ex-date ``X``, every bar with a date
    strictly less than ``X`` gets ``D.cash_amount`` added to each of its
    OHLC columns. Bars on or after ``X`` are left unchanged.

    Args:
        df: TradingView-sourced bar DataFrame. Must have a tz-aware ET
            column and the OHLC columns named in ``price_cols``.
        dividends: Iterable of :class:`DividendEvent`. Order doesn't
            matter; the math is additive.
        et_col: Name of the tz-aware America/New_York column on ``df``.
        price_cols: OHLC columns to adjust.

    Returns:
        New DataFrame with adjusted prices.
    """
    if et_col not in df.columns:
        raise ValueError(f"DataFrame missing required column {et_col!r}")
    out = df.copy()
    bar_date = out[et_col].dt.date
    for div in dividends:
        # Bars dated *before* the ex-date had their displayed price
        # depressed by `cash_amount`; restore by adding it back.
        mask = bar_date < div.ex_date
        if not mask.any():
            continue
        for col in price_cols:
            if col in out.columns:
                out.loc[mask, col] = out.loc[mask, col] + div.cash_amount
        logger.info(
            "[DIV ADJ] +%.2f to %d bars before %s (%s)",
            div.cash_amount,
            int(mask.sum()),
            div.ex_date,
            div.ticker,
        )
    return out


def apply_dividend_adjustment(
    df: pd.DataFrame,
    dividends: tuple[DividendEvent, ...],
    timestamp_col: str = "timestamp",
    price_cols: tuple[str, ...] = ("open", "high", "low", "close"),
) -> pd.DataFrame:
    """Subtract dividends from Polygon bars to produce TV-style adjusted prices.

    Polygon's ``adjusted=True`` adjusts for splits only. To match TradingView's
    default dividend-adjusted chart, every bar dated *before* a dividend's
    ex-date must have ``cash_amount`` subtracted from each OHLC column.

    This is the inverse of :func:`reverse_dividend_adjustment`: that one adds
    dividends back into already-TV-adjusted data (useful for reconciliation);
    this one applies the TV adjustment to raw Polygon data.

    Args:
        df: Bar DataFrame with a ms-since-epoch ``timestamp`` column (Polygon
            aggregate shape) and OHLC columns in ``price_cols``.
        dividends: Iterable of :class:`DividendEvent` with ``ex_date`` values.
            Pass an empty tuple to pass through unchanged.
        timestamp_col: Name of the int64-ms-UTC timestamp column.
        price_cols: OHLC columns to adjust.

    Returns:
        New DataFrame with adjusted prices.
    """
    if not dividends:
        return df.copy()
    if timestamp_col not in df.columns:
        raise ValueError(f"DataFrame missing required column {timestamp_col!r}")
    out = df.copy()
    # Convert ms-since-epoch to ET date for the strict-less-than compare.
    # Timestamp rigor: this is local arithmetic inside the function; we
    # don't return the datetime column, only use it for the mask.
    bar_date = pd.to_datetime(out[timestamp_col], unit="ms", utc=True).dt.tz_convert("America/New_York").dt.date
    for div in dividends:
        mask = bar_date < div.ex_date
        if not mask.any():
            continue
        for col in price_cols:
            if col in out.columns:
                out.loc[mask, col] = out.loc[mask, col] - div.cash_amount
        logger.info(
            "[DIV ADJ] -%.2f to %d bars before %s (%s)",
            div.cash_amount,
            int(mask.sum()),
            div.ex_date,
            div.ticker,
        )
    return out


def dividends_from_polygon_payload(
    raw: list[dict[str, Any]],
    ticker: str,
) -> tuple[DividendEvent, ...]:
    """Convert ``polygon_client.list_dividends`` output into DividendEvent tuples.

    Polygon's payload keys: ``ex_dividend_date`` (YYYY-MM-DD), ``cash_amount``
    (float). Rows missing either field are skipped with a warning.
    """
    events: list[DividendEvent] = []
    for row in raw:
        ex_str = row.get("ex_dividend_date")
        amt = row.get("cash_amount")
        if not ex_str or amt is None:
            logger.warning("[DIV ADJ] Skipping dividend row with missing field: %s", row)
            continue
        try:
            ex = date.fromisoformat(ex_str)
        except (TypeError, ValueError):
            logger.warning("[DIV ADJ] Skipping dividend with unparseable ex_date: %s", ex_str)
            continue
        events.append(DividendEvent(ex_date=ex, cash_amount=float(amt), ticker=ticker))
    return tuple(events)


def detect_dividends_from_gap(
    merged: pd.DataFrame,
    tv_close_col: str = "close_tv",
    pg_close_col: str = "close_pg",
    et_col: str = "time_utc",
    min_step: float = 0.50,
) -> list[DividendEvent]:
    """Recover dividends from a TV-vs-Polygon merged dataframe.

    Useful as a sanity check: after applying ``reverse_dividend_adjustment``
    with the hard-coded list, run this on the merged result. If it returns
    new candidate ex-dates, the hard-coded list is missing entries.

    Algorithm: compute daily gap = pg - tv, take the day-over-day diff;
    days where the absolute diff exceeds ``min_step`` are candidate ex-dates.
    """
    df = merged[[et_col, tv_close_col, pg_close_col]].copy()
    df["et_date"] = pd.to_datetime(df[et_col]).dt.tz_convert("America/New_York").dt.date
    df["gap"] = df[pg_close_col] - df[tv_close_col]
    daily = df.groupby("et_date").agg(gap=("gap", "last")).reset_index()
    daily["delta"] = daily["gap"].diff()
    candidates: list[DividendEvent] = []
    for _, row in daily.iterrows():
        if pd.notna(row["delta"]) and abs(row["delta"]) > min_step:
            candidates.append(
                DividendEvent(
                    ex_date=row["et_date"],
                    cash_amount=round(-float(row["delta"]), 2),  # gap drops -> dividend went ex
                )
            )
    return candidates
