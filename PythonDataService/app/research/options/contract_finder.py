from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

# Concurrency limit for Polygon API calls
_SEMAPHORE = asyncio.Semaphore(5)

MIN_VOLUME = 50
MIN_OPEN_INTEREST = 100
MAX_SPREAD_RATIO = 0.10  # 10% bid-ask spread / mid
OTM_OFFSET_PCT = 0.05    # 5% OTM for skew contracts


def _find_atm_strike(contracts: list[dict], stock_close: float) -> dict | None:
    """Find the contract with strike closest to stock close price."""
    if not contracts:
        return None
    return min(contracts, key=lambda c: abs(c["strike_price"] - stock_close))


def _find_otm_put(contracts: list[dict], stock_close: float) -> dict | None:
    """Find OTM put ~5% below ATM."""
    target = stock_close * (1 - OTM_OFFSET_PCT)
    puts = [c for c in contracts if c.get("contract_type") == "put"]
    if not puts:
        return None
    return min(puts, key=lambda c: abs(c["strike_price"] - target))


def _find_otm_call(contracts: list[dict], stock_close: float) -> dict | None:
    """Find OTM call ~5% above ATM."""
    target = stock_close * (1 + OTM_OFFSET_PCT)
    calls = [c for c in contracts if c.get("contract_type") == "call"]
    if not calls:
        return None
    return min(calls, key=lambda c: abs(c["strike_price"] - target))


def _passes_liquidity_filter(contract: dict) -> bool:
    """Apply liquidity filters to a contract."""
    volume = contract.get("volume") or 0
    oi = contract.get("open_interest") or 0

    if volume < MIN_VOLUME:
        return False
    if oi < MIN_OPEN_INTEREST:
        return False

    bid = contract.get("bid")
    ask = contract.get("ask")
    if bid is not None and ask is not None and bid > 0:
        mid = (bid + ask) / 2
        if mid > 0 and (ask - bid) / mid > MAX_SPREAD_RATIO:
            return False

    return True


def _get_trading_days(start_date: str, end_date: str, stock_bars: pd.DataFrame) -> list[datetime]:
    """Extract trading days from stock bar data."""
    if stock_bars.empty:
        return []

    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)

    dates = pd.to_datetime(stock_bars["date"]).sort_values().unique()
    mask = (dates >= start) & (dates <= end)
    return [pd.Timestamp(d).to_pydatetime() for d in dates[mask]]


def _fetch_contracts_for_expiry(
    polygon_client: PolygonClientService,
    underlying: str,
    expiration_date: str,
    stock_close: float,
) -> dict[str, Any]:
    """Fetch and filter contracts for a specific expiry, returning ATM + OTM contracts."""
    strike_range = stock_close * 0.15  # Search within 15% of ATM
    contracts = polygon_client.list_options_contracts(
        underlying_ticker=underlying,
        expiration_date=expiration_date,
        expired=True,
        strike_price_gte=stock_close - strike_range,
        strike_price_lte=stock_close + strike_range,
        limit=250,
    )

    calls = [c for c in contracts if c.get("contract_type") == "call"]
    puts = [c for c in contracts if c.get("contract_type") == "put"]

    atm_call = _find_atm_strike(calls, stock_close)
    otm_put = _find_otm_put(puts, stock_close)
    otm_call = _find_otm_call(calls, stock_close)

    return {
        "atm_call": atm_call,
        "otm_put": otm_put,
        "otm_call": otm_call,
    }


def _find_bracket_expiries(
    polygon_client: PolygonClientService,
    underlying: str,
    trade_date: datetime,
) -> tuple[str | None, str | None]:
    """Find two expiry dates bracketing 30 DTE from trade_date.

    Returns (low_expiry, high_expiry) where:
    - low_expiry has DTE < 30 (closest below 30)
    - high_expiry has DTE > 30 (closest above 30)
    """
    target_date = trade_date + timedelta(days=30)
    search_start = trade_date + timedelta(days=14)  # At least 14 DTE
    search_end = trade_date + timedelta(days=60)    # At most 60 DTE

    trade_date_str = trade_date.strftime("%Y-%m-%d")

    contracts = polygon_client.list_options_contracts(
        underlying_ticker=underlying,
        as_of_date=trade_date_str,
        expiration_date_gte=search_start.strftime("%Y-%m-%d"),
        expiration_date_lte=search_end.strftime("%Y-%m-%d"),
        expired=True,
        contract_type="call",
        limit=500,
    )

    # Extract unique expiry dates
    expiries: set[str] = set()
    for c in contracts:
        exp = c.get("expiration_date")
        if exp:
            expiries.add(exp)

    if not expiries:
        return None, None

    target_str = target_date.strftime("%Y-%m-%d")

    low_expiry: str | None = None
    high_expiry: str | None = None

    for exp in sorted(expiries):
        if exp <= target_str:
            low_expiry = exp
        elif high_expiry is None:
            high_expiry = exp

    return low_expiry, high_expiry


def find_bracket_contracts(
    underlying: str,
    start_date: str,
    end_date: str,
    polygon_client: PolygonClientService,
    stock_bars: list[dict[str, Any]],
) -> pd.DataFrame:
    """Find bracket contracts (two expiries around 30 DTE) for each trading day.

    Args:
        underlying: Stock ticker (e.g. "SPY")
        start_date: ISO date string
        end_date: ISO date string
        polygon_client: Polygon REST client wrapper
        stock_bars: Daily OHLCV bars with 'timestamp' and 'close' fields

    Returns:
        DataFrame with columns per date:
        [date, stock_close, low_dte, low_dte_atm_contract, low_dte_put_contract,
         low_dte_call_contract, high_dte, high_dte_atm_contract, high_dte_put_contract,
         high_dte_call_contract]
    """
    # Build date→close map from stock bars
    date_close_map: dict[str, float] = {}
    for bar in stock_bars:
        ts = bar.get("timestamp") or bar.get("t")
        close = bar.get("close") or bar.get("c")
        if ts is not None and close is not None:
            if isinstance(ts, (int, float)):
                dt = datetime.utcfromtimestamp(ts / 1000)
            else:
                dt = pd.Timestamp(ts).to_pydatetime()
            date_str = dt.strftime("%Y-%m-%d")
            date_close_map[date_str] = float(close)

    # Get trading days in range
    all_dates = sorted(date_close_map.keys())
    filtered_dates = [d for d in all_dates if start_date <= d <= end_date]

    if not filtered_dates:
        logger.warning(f"No trading days found for {underlying} in [{start_date}, {end_date}]")
        return pd.DataFrame()

    logger.info(f"[CONTRACT FINDER] Processing {len(filtered_dates)} trading days for {underlying}")

    rows: list[dict[str, Any]] = []

    # Cache expiry lookups — same expiries are valid for nearby dates
    _expiry_cache: dict[str, tuple[str | None, str | None]] = {}

    for i, date_str in enumerate(filtered_dates):
        stock_close = date_close_map[date_str]
        trade_date = datetime.strptime(date_str, "%Y-%m-%d")

        if i % 50 == 0:
            logger.info(f"[CONTRACT FINDER] Processing date {i+1}/{len(filtered_dates)}: {date_str}")

        # Find bracket expiries (cacheable by month)
        cache_key = f"{underlying}:{trade_date.strftime('%Y-%m')}"
        if cache_key not in _expiry_cache:
            try:
                low_exp, high_exp = _find_bracket_expiries(polygon_client, underlying, trade_date)
                _expiry_cache[cache_key] = (low_exp, high_exp)
            except Exception as e:
                logger.warning(f"[CONTRACT FINDER] Error finding expiries for {date_str}: {e}")
                _expiry_cache[cache_key] = (None, None)

        low_exp, high_exp = _expiry_cache[cache_key]

        row: dict[str, Any] = {
            "date": date_str,
            "stock_close": stock_close,
            "low_dte": None,
            "low_dte_atm_contract": None,
            "low_dte_put_contract": None,
            "low_dte_call_contract": None,
            "high_dte": None,
            "high_dte_atm_contract": None,
            "high_dte_put_contract": None,
            "high_dte_call_contract": None,
        }

        if low_exp:
            low_dte = (datetime.strptime(low_exp, "%Y-%m-%d") - trade_date).days
            if low_dte >= 7:
                row["low_dte"] = low_dte
                try:
                    low_contracts = _fetch_contracts_for_expiry(
                        polygon_client, underlying, low_exp, stock_close
                    )
                    if low_contracts["atm_call"]:
                        row["low_dte_atm_contract"] = low_contracts["atm_call"].get("ticker")
                    if low_contracts["otm_put"]:
                        row["low_dte_put_contract"] = low_contracts["otm_put"].get("ticker")
                    if low_contracts["otm_call"]:
                        row["low_dte_call_contract"] = low_contracts["otm_call"].get("ticker")
                except Exception as e:
                    logger.warning(f"[CONTRACT FINDER] Error fetching low-DTE contracts for {date_str}: {e}")

        if high_exp:
            high_dte = (datetime.strptime(high_exp, "%Y-%m-%d") - trade_date).days
            if high_dte >= 7:
                row["high_dte"] = high_dte
                try:
                    high_contracts = _fetch_contracts_for_expiry(
                        polygon_client, underlying, high_exp, stock_close
                    )
                    if high_contracts["atm_call"]:
                        row["high_dte_atm_contract"] = high_contracts["atm_call"].get("ticker")
                    if high_contracts["otm_put"]:
                        row["high_dte_put_contract"] = high_contracts["otm_put"].get("ticker")
                    if high_contracts["otm_call"]:
                        row["high_dte_call_contract"] = high_contracts["otm_call"].get("ticker")
                except Exception as e:
                    logger.warning(f"[CONTRACT FINDER] Error fetching high-DTE contracts for {date_str}: {e}")

        rows.append(row)

    return pd.DataFrame(rows)
