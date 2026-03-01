from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.research.options.bs_solver import RISK_FREE_RATE, implied_volatility
from app.research.options.contract_finder import find_bracket_contracts
from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

MIN_OPTION_PRICE = 0.05
MIN_IV = 0.05
MAX_IV = 3.0
MIN_DTE_DAYS = 7
MIN_VOLUME = 50
MIN_OI = 100
TARGET_DTE = 30
MAX_FFILL_DAYS = 2


def _fetch_option_daily_bar(
    polygon_client: PolygonClientService,
    option_ticker: str,
    trade_date: str,
) -> dict[str, Any] | None:
    """Fetch daily OHLCV bar for an options contract on a specific date."""
    try:
        bars = polygon_client.fetch_aggregates(
            ticker=option_ticker,
            multiplier=1,
            timespan="day",
            from_date=trade_date,
            to_date=trade_date,
            limit=1,
        )
        if bars:
            return bars[0]
    except Exception as e:
        logger.debug(f"Failed to fetch bar for {option_ticker} on {trade_date}: {e}")
    return None


def _get_option_price(bar: dict[str, Any]) -> tuple[float | None, str]:
    """Extract best price from option bar. Returns (price, source)."""
    # Prefer mid price if bid/ask available
    bid = bar.get("bid")
    ask = bar.get("ask")
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2
        if mid >= MIN_OPTION_PRICE:
            return mid, "mid"

    # Fallback to close price with volume/OI filter
    close = bar.get("close") or bar.get("c")
    volume = bar.get("volume") or bar.get("v") or 0
    if close is not None and close >= MIN_OPTION_PRICE and volume >= MIN_VOLUME:
        return float(close), "close_filtered"

    return None, "rejected"


def _derive_iv_for_contract(
    polygon_client: PolygonClientService,
    option_ticker: str,
    trade_date: str,
    stock_close: float,
    dte: int,
    option_type: str,
) -> tuple[float | None, str]:
    """Derive IV for a single contract on a single date.

    Returns (iv, price_source) or (None, reason).
    """
    if dte < MIN_DTE_DAYS:
        return None, "dte_too_low"

    bar = _fetch_option_daily_bar(polygon_client, option_ticker, trade_date)
    if bar is None:
        return None, "no_bar"

    price, source = _get_option_price(bar)
    if price is None:
        return None, "price_rejected"

    T = dte / 365.0

    # Extract strike from option ticker: O:AAPL250221C00185000
    # The strike is the last 8 digits / 1000
    try:
        strike_str = option_ticker[-8:]
        strike = int(strike_str) / 1000.0
    except (ValueError, IndexError):
        return None, "bad_ticker_format"

    iv = implied_volatility(price, stock_close, strike, T, RISK_FREE_RATE, option_type)
    if iv is None:
        return None, "solver_failed"

    return iv, source


def _interpolate_iv(
    iv_low: float, dte_low: int, iv_high: float, dte_high: int
) -> float:
    """30-day constant-maturity linear interpolation."""
    if dte_high == dte_low:
        return (iv_low + iv_high) / 2

    weight_low = (dte_high - TARGET_DTE) / (dte_high - dte_low)
    weight_high = (TARGET_DTE - dte_low) / (dte_high - dte_low)

    return weight_low * iv_low + weight_high * iv_high


def _normalize_iv_fallback(iv: float, dte: int) -> float:
    """DTE normalization fallback when only one bracket available."""
    if dte <= 0:
        return iv
    return iv * math.sqrt(TARGET_DTE / dte)


def build_iv_history(
    underlying: str,
    start_date: str,
    end_date: str,
    polygon_client: PolygonClientService,
    stock_bars: list[dict[str, Any]] | None = None,
) -> pd.DataFrame:
    """Build historical 30-day constant-maturity IV time series.

    Args:
        underlying: Stock ticker (e.g. "SPY")
        start_date: ISO date string
        end_date: ISO date string
        polygon_client: Polygon REST client wrapper
        stock_bars: Optional pre-fetched daily stock bars. If None, fetches from Polygon.

    Returns:
        DataFrame with columns:
        [date, iv_30d_atm, iv_30d_put, iv_30d_call, stock_close, dte_low, dte_high, price_source]
    """
    logger.info(f"[IV BUILDER] Building IV history for {underlying} [{start_date} → {end_date}]")

    # Step 1: Fetch stock daily bars if not provided
    if stock_bars is None:
        stock_bars = polygon_client.fetch_aggregates(
            ticker=underlying,
            multiplier=1,
            timespan="day",
            from_date=start_date,
            to_date=end_date,
        )

    if not stock_bars:
        logger.error(f"[IV BUILDER] No stock bars found for {underlying}")
        return pd.DataFrame()

    # Step 2: Find bracket contracts for each trading day
    logger.info(f"[IV BUILDER] Finding bracket contracts...")
    bracket_df = find_bracket_contracts(
        underlying, start_date, end_date, polygon_client, stock_bars
    )

    if bracket_df.empty:
        logger.error(f"[IV BUILDER] No bracket contracts found for {underlying}")
        return pd.DataFrame()

    logger.info(f"[IV BUILDER] Processing {len(bracket_df)} trading days for IV derivation")

    # Step 3: For each day, derive IV from bracket contracts
    # Cache option bars to avoid duplicate fetches
    _option_bar_cache: dict[str, dict | None] = {}

    rows: list[dict[str, Any]] = []

    for idx, row in bracket_df.iterrows():
        date_str = row["date"]
        stock_close = row["stock_close"]
        dte_low = row.get("low_dte")
        dte_high = row.get("high_dte")

        if idx % 50 == 0:
            logger.info(f"[IV BUILDER] Deriving IV for date {idx + 1}/{len(bracket_df)}: {date_str}")

        result: dict[str, Any] = {
            "date": date_str,
            "iv_30d_atm": None,
            "iv_30d_put": None,
            "iv_30d_call": None,
            "stock_close": stock_close,
            "dte_low": dte_low,
            "dte_high": dte_high,
            "price_source": None,
        }

        # Derive ATM IV
        iv_atm_low, iv_atm_high = None, None
        source = None

        low_atm = row.get("low_dte_atm_contract")
        high_atm = row.get("high_dte_atm_contract")

        if low_atm and dte_low and dte_low >= MIN_DTE_DAYS:
            iv_atm_low, src = _derive_iv_for_contract(
                polygon_client, low_atm, date_str, stock_close, dte_low, "call"
            )
            if iv_atm_low:
                source = src

        if high_atm and dte_high and dte_high >= MIN_DTE_DAYS:
            iv_atm_high, src = _derive_iv_for_contract(
                polygon_client, high_atm, date_str, stock_close, dte_high, "call"
            )
            if iv_atm_high:
                source = src

        # Interpolate or fallback
        if iv_atm_low is not None and iv_atm_high is not None:
            result["iv_30d_atm"] = _interpolate_iv(iv_atm_low, dte_low, iv_atm_high, dte_high)
        elif iv_atm_low is not None and dte_low:
            result["iv_30d_atm"] = _normalize_iv_fallback(iv_atm_low, dte_low)
        elif iv_atm_high is not None and dte_high:
            result["iv_30d_atm"] = _normalize_iv_fallback(iv_atm_high, dte_high)

        # Derive OTM Put IV (for skew)
        iv_put_low, iv_put_high = None, None

        low_put = row.get("low_dte_put_contract")
        high_put = row.get("high_dte_put_contract")

        if low_put and dte_low and dte_low >= MIN_DTE_DAYS:
            iv_put_low, _ = _derive_iv_for_contract(
                polygon_client, low_put, date_str, stock_close, dte_low, "put"
            )

        if high_put and dte_high and dte_high >= MIN_DTE_DAYS:
            iv_put_high, _ = _derive_iv_for_contract(
                polygon_client, high_put, date_str, stock_close, dte_high, "put"
            )

        if iv_put_low is not None and iv_put_high is not None:
            result["iv_30d_put"] = _interpolate_iv(iv_put_low, dte_low, iv_put_high, dte_high)
        elif iv_put_low is not None and dte_low:
            result["iv_30d_put"] = _normalize_iv_fallback(iv_put_low, dte_low)
        elif iv_put_high is not None and dte_high:
            result["iv_30d_put"] = _normalize_iv_fallback(iv_put_high, dte_high)

        # Derive OTM Call IV (for skew)
        iv_call_low, iv_call_high = None, None

        low_call = row.get("low_dte_call_contract")
        high_call = row.get("high_dte_call_contract")

        if low_call and dte_low and dte_low >= MIN_DTE_DAYS:
            iv_call_low, _ = _derive_iv_for_contract(
                polygon_client, low_call, date_str, stock_close, dte_low, "call"
            )

        if high_call and dte_high and dte_high >= MIN_DTE_DAYS:
            iv_call_high, _ = _derive_iv_for_contract(
                polygon_client, high_call, date_str, stock_close, dte_high, "call"
            )

        if iv_call_low is not None and iv_call_high is not None:
            result["iv_30d_call"] = _interpolate_iv(iv_call_low, dte_low, iv_call_high, dte_high)
        elif iv_call_low is not None and dte_low:
            result["iv_30d_call"] = _normalize_iv_fallback(iv_call_low, dte_low)
        elif iv_call_high is not None and dte_high:
            result["iv_30d_call"] = _normalize_iv_fallback(iv_call_high, dte_high)

        result["price_source"] = source
        rows.append(result)

    df = pd.DataFrame(rows)

    if df.empty:
        return df

    # Quality filters
    for col in ["iv_30d_atm", "iv_30d_put", "iv_30d_call"]:
        if col in df.columns:
            mask = df[col].notna()
            df.loc[mask & ((df[col] < MIN_IV) | (df[col] > MAX_IV)), col] = np.nan

    # Forward-fill gaps <= 2 days (weekends/holidays)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").asfreq("B")  # Business day frequency
    for col in ["iv_30d_atm", "iv_30d_put", "iv_30d_call", "stock_close"]:
        if col in df.columns:
            df[col] = df[col].ffill(limit=MAX_FFILL_DAYS)
    df = df.reset_index().rename(columns={"index": "date"})

    # Convert date back to string
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    valid_count = df["iv_30d_atm"].notna().sum()
    total_count = len(df)
    logger.info(
        f"[IV BUILDER] Complete: {valid_count}/{total_count} days with valid ATM IV "
        f"({valid_count/total_count*100:.1f}% coverage)"
    )

    return df
