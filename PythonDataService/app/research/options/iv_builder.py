from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.research.options.bs_solver import RISK_FREE_RATE, implied_volatility
from app.research.options.contract_finder import find_bracket_contracts
from app.services.fred_service import get_risk_free_rate
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


_CONTRACT_COLUMNS = [
    "low_dte_atm_contract", "low_dte_atm_put_contract",
    "low_dte_put_contract", "low_dte_call_contract",
    "high_dte_atm_contract", "high_dte_atm_put_contract",
    "high_dte_put_contract", "high_dte_call_contract",
]

_PREFETCH_WORKERS = 5


def _collect_unique_tickers(bracket_df: pd.DataFrame) -> set[str]:
    """Scan bracket DataFrame and return all unique option tickers."""
    tickers: set[str] = set()
    for col in _CONTRACT_COLUMNS:
        if col in bracket_df.columns:
            tickers.update(bracket_df[col].dropna().unique())
    return tickers


def _prefetch_all_bars(
    polygon_client: PolygonClientService,
    tickers: set[str],
    start_date: str,
    end_date: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Fetch full date-range daily bars for each option ticker in parallel.

    Returns nested dict: bars_by_contract[ticker][date_str] = bar
    """
    bars_by_contract: dict[str, dict[str, dict[str, Any]]] = {}

    def _fetch_one(ticker: str) -> tuple[str, dict[str, dict[str, Any]]]:
        date_map: dict[str, dict[str, Any]] = {}
        try:
            bars = polygon_client.fetch_aggregates(
                ticker=ticker,
                multiplier=1,
                timespan="day",
                from_date=start_date,
                to_date=end_date,
            )
            for bar in bars:
                ts = bar.get("timestamp") or bar.get("t")
                if ts is not None:
                    if isinstance(ts, (int, float)):
                        dt = datetime.utcfromtimestamp(ts / 1000)
                    else:
                        dt = pd.Timestamp(ts).to_pydatetime()
                    date_map[dt.strftime("%Y-%m-%d")] = bar
        except Exception as e:
            logger.debug(f"[IV BUILDER] Failed to prefetch bars for {ticker}: {e}")
        return ticker, date_map

    with ThreadPoolExecutor(max_workers=_PREFETCH_WORKERS) as executor:
        futures = {executor.submit(_fetch_one, t): t for t in tickers}
        for future in as_completed(futures):
            ticker, date_map = future.result()
            bars_by_contract[ticker] = date_map

    return bars_by_contract


def _lookup_bar(
    bars_by_contract: dict[str, dict[str, dict[str, Any]]],
    option_ticker: str,
    trade_date: str,
) -> dict[str, Any] | None:
    """Look up a pre-fetched bar by ticker and date."""
    return bars_by_contract.get(option_ticker, {}).get(trade_date)


MAX_SPREAD_RATIO = 0.15  # Max bid-ask spread / mid for tight-spread filter


def _get_option_price(bar: dict[str, Any]) -> tuple[float | None, str]:
    """Extract best price from option bar using strict hierarchy.

    Priority: midpoint (tight spread) → VWAP → close (in range) → reject.
    """
    bid = bar.get("bid")
    ask = bar.get("ask")

    # Tier 1: Midpoint with tight spread validation
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2
        spread_ratio = (ask - bid) / mid if mid > 0 else 1.0
        if mid >= MIN_OPTION_PRICE and spread_ratio <= MAX_SPREAD_RATIO:
            return mid, "mid"

    # Tier 2: VWAP if available
    vwap = bar.get("vw") or bar.get("vwap")
    if vwap is not None and vwap >= MIN_OPTION_PRICE:
        return float(vwap), "vwap"

    # Tier 3: Close price — must have volume AND be within bid-ask range if available
    close = bar.get("close") or bar.get("c")
    volume = bar.get("volume") or bar.get("v") or 0
    if close is not None and close >= MIN_OPTION_PRICE and volume >= MIN_VOLUME:
        # If bid/ask exist, validate close is within range
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            if bid <= close <= ask:
                return float(close), "close_filtered"
        else:
            # No bid/ask to validate against — accept with volume filter only
            return float(close), "close_filtered"

    return None, "rejected"


def _derive_iv_for_contract(
    bar: dict[str, Any] | None,
    option_ticker: str,
    stock_close: float,
    dte: int,
    option_type: str,
    risk_free_rate: float = RISK_FREE_RATE,
) -> tuple[float | None, str]:
    """Derive IV for a single contract from a pre-fetched bar.

    Returns (iv, price_source) or (None, reason).
    """
    if dte < MIN_DTE_DAYS:
        return None, "dte_too_low"

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

    iv = implied_volatility(price, stock_close, strike, T, risk_free_rate, option_type)
    if iv is None:
        return None, "solver_failed"

    return iv, source


def _interpolate_iv(
    iv_low: float, dte_low: int, iv_high: float, dte_high: int
) -> float:
    """30-day constant-maturity variance-time interpolation."""
    if dte_high == dte_low:
        return (iv_low + iv_high) / 2

    t_low = dte_low / 365
    t_high = dte_high / 365
    t_target = TARGET_DTE / 365

    weight_low = (dte_high - TARGET_DTE) / (dte_high - dte_low)
    weight_high = (TARGET_DTE - dte_low) / (dte_high - dte_low)

    total_var = weight_low * iv_low**2 * t_low + weight_high * iv_high**2 * t_high
    return math.sqrt(total_var / t_target)


def _normalize_iv_fallback(iv: float, dte: int) -> float | None:
    """Fallback removed — returns None to avoid unreliable sqrt(T) scaling."""
    return None


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

    # Step 3: Batch prefetch all option bars
    unique_tickers = _collect_unique_tickers(bracket_df)
    logger.info(f"[IV BUILDER] Prefetching bars for {len(unique_tickers)} unique contracts...")
    bars_by_contract = _prefetch_all_bars(polygon_client, unique_tickers, start_date, end_date)
    total_bars = sum(len(dates) for dates in bars_by_contract.values())
    logger.info(f"[IV BUILDER] Prefetched {total_bars} bars across {len(bars_by_contract)} contracts")

    # Step 4: Per-day loop — local lookups instead of API calls
    rows: list[dict[str, Any]] = []

    for idx, row in bracket_df.iterrows():
        date_str = row["date"]
        stock_close = row["stock_close"]
        dte_low = row.get("low_dte")
        dte_high = row.get("high_dte")

        if idx % 50 == 0:
            logger.info(f"[IV BUILDER] Deriving IV for date {idx + 1}/{len(bracket_df)}: {date_str}")

        # Dynamic risk-free rate for this trading day (cached per date)
        rfr = get_risk_free_rate(dte_days=TARGET_DTE, observation_date=date_str)

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

        # Derive ATM IV (synthetic forward: average of call and put IV at ATM strike)
        iv_atm_low, iv_atm_high = None, None
        source = None

        low_atm = row.get("low_dte_atm_contract")
        low_atm_put = row.get("low_dte_atm_put_contract")
        high_atm = row.get("high_dte_atm_contract")
        high_atm_put = row.get("high_dte_atm_put_contract")

        if low_atm and dte_low and dte_low >= MIN_DTE_DAYS:
            bar = _lookup_bar(bars_by_contract, low_atm, date_str)
            iv_call_low_atm, src = _derive_iv_for_contract(
                bar, low_atm, stock_close, dte_low, "call", rfr
            )
            iv_put_low_atm = None
            if low_atm_put:
                bar_p = _lookup_bar(bars_by_contract, low_atm_put, date_str)
                iv_put_low_atm, _ = _derive_iv_for_contract(
                    bar_p, low_atm_put, stock_close, dte_low, "put", rfr
                )
            # Synthetic forward: average call/put IV; fall back to call-only
            if iv_call_low_atm is not None and iv_put_low_atm is not None:
                iv_atm_low = (iv_call_low_atm + iv_put_low_atm) / 2
            else:
                iv_atm_low = iv_call_low_atm
            if iv_atm_low:
                source = src

        if high_atm and dte_high and dte_high >= MIN_DTE_DAYS:
            bar = _lookup_bar(bars_by_contract, high_atm, date_str)
            iv_call_high_atm, src = _derive_iv_for_contract(
                bar, high_atm, stock_close, dte_high, "call", rfr
            )
            iv_put_high_atm = None
            if high_atm_put:
                bar_p = _lookup_bar(bars_by_contract, high_atm_put, date_str)
                iv_put_high_atm, _ = _derive_iv_for_contract(
                    bar_p, high_atm_put, stock_close, dte_high, "put", rfr
                )
            if iv_call_high_atm is not None and iv_put_high_atm is not None:
                iv_atm_high = (iv_call_high_atm + iv_put_high_atm) / 2
            else:
                iv_atm_high = iv_call_high_atm
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
            bar = _lookup_bar(bars_by_contract, low_put, date_str)
            iv_put_low, _ = _derive_iv_for_contract(
                bar, low_put, stock_close, dte_low, "put", rfr
            )

        if high_put and dte_high and dte_high >= MIN_DTE_DAYS:
            bar = _lookup_bar(bars_by_contract, high_put, date_str)
            iv_put_high, _ = _derive_iv_for_contract(
                bar, high_put, stock_close, dte_high, "put", rfr
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
            bar = _lookup_bar(bars_by_contract, low_call, date_str)
            iv_call_low, _ = _derive_iv_for_contract(
                bar, low_call, stock_close, dte_low, "call", rfr
            )

        if high_call and dte_high and dte_high >= MIN_DTE_DAYS:
            bar = _lookup_bar(bars_by_contract, high_call, date_str)
            iv_call_high, _ = _derive_iv_for_contract(
                bar, high_call, stock_close, dte_high, "call", rfr
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

    # Quality flags (soft — keep all data, let downstream decide)
    def _quality_flag(row: pd.Series) -> str:
        iv = row.get("iv_30d_atm")
        src = row.get("price_source")
        if iv is None or pd.isna(iv):
            return "missing"
        if iv < MIN_IV or iv > MAX_IV:
            return "low"
        if src in ("close_filtered", "vwap"):
            return "medium"
        return "high"

    df["iv_quality"] = df.apply(_quality_flag, axis=1)

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
