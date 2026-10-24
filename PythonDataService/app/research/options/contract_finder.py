"""Options contract finder for research pipeline.

Formula: Select the available strike nearest target delta (BSM delta = N(d1) for calls, N(d1)-1 for puts, d1 = (ln(S/K) + (r + σ²/2)·T) / (σ·√T)); choose, for each trading date d, the latest listed expiry 7 to 30 calendar days out and the earliest 31 to 45 days out, so 30 days is always inside the pair. The expiries are listed once per month; the pair is picked per date from that list. No strike interpolation occurs.
Reference: BSM delta per Hull §19.4; nearest-strike and expiry selection are internal rules, not an external port.
Canonical implementation: app/research/options/contract_finder.py
Validated against: NONE — pending (no golden fixture); the expiry pair is pinned by tests/research/options/test_contract_finder_bracket.py and tests/research/options/test_iv_builder_bracket_window.py
"""
from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

import pandas as pd

from app.services.bs_greeks import black_scholes_greeks
from app.services.polygon_client import PolygonClientService
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE

logger = logging.getLogger(__name__)


OTM_OFFSET_PCT = 0.05  # Fallback: 5% OTM for skew contracts
TARGET_DELTA_PUT = -0.25  # Target delta for OTM put (25Δ)
TARGET_DELTA_CALL = 0.25  # Target delta for OTM call (25Δ)
DEFAULT_IV = 0.25  # Default IV for delta estimation when not available

TARGET_DTE = 30  # The constant maturity the expiry pair brackets
MIN_DTE_DAYS = 7  # An expiry nearer than this is never used: no IV is derived for it
EXPIRY_MAX_DTE = 45  # The far expiry of a date's pair is at most this many days out
# The contracts one expiry lookup may return per calendar day of its window. A chain with a daily expiry lists about
# 36 calls a calendar day within 5% of the money; the cap only binds on a chain denser than this one.
EXPIRY_LOOKUP_CONTRACTS_PER_DAY = 50


class BracketGap(StrEnum):
    """Why a date has no expiry pair bracketing 30 days; the value is what the bracket frame carries."""

    LOOKUP_FAILED = "expiry_lookup_failed"  # the month's expiry lookup raised
    NO_EXPIRIES = "no_expiries_listed"  # the month's lookup answered with nothing
    NO_NEAR_EXPIRY = "no_near_expiry"  # nothing listed 7 to 30 days out
    NO_FAR_EXPIRY = "no_far_expiry"  # nothing listed 31 to 45 days out


def _bs_delta(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
) -> float:
    """Zero-dividend BSM delta for strike selection.

    Formula: call Δ=N(d1), put Δ=N(d1)-1 for q=0.
    Reference: Hull §19.4, BSM delta.
    Canonical implementation: app/services/bs_greeks.py::black_scholes_greeks.
    Validated against: tests/services/test_bs_cross_engine_parity.py and tests/research/options/test_contract_finder.py.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    return black_scholes_greeks(
        spot=S,
        strike=K,
        ttm_years=T,
        volatility=sigma,
        rate=r,
        dividend=0.0,
        is_call=option_type == "call",
    ).delta


def _find_atm_strike(contracts: list[dict], stock_close: float) -> dict | None:
    """Find the contract with strike closest to stock close price."""
    if not contracts:
        return None
    return min(contracts, key=lambda c: abs(c["strike_price"] - stock_close))


def _find_otm_put_by_delta(
    contracts: list[dict],
    stock_close: float,
    dte_days: int,
    *,
    rfr: float,
    iv_estimate: float = DEFAULT_IV,
) -> dict | None:
    """Find OTM put closest to 25Δ. Falls back to 5% OTM offset."""
    puts = [c for c in contracts if c.get("contract_type") == "put"]
    if not puts:
        return None

    T = dte_days / 365.0
    if T <= 0 or iv_estimate <= 0:
        # Fall back to fixed offset
        target = stock_close * (1 - OTM_OFFSET_PCT)
        return min(puts, key=lambda c: abs(c["strike_price"] - target))

    return min(
        puts,
        key=lambda c: abs(_bs_delta(stock_close, c["strike_price"], T, rfr, iv_estimate, "put") - TARGET_DELTA_PUT),
    )


def _find_otm_call_by_delta(
    contracts: list[dict],
    stock_close: float,
    dte_days: int,
    *,
    rfr: float,
    iv_estimate: float = DEFAULT_IV,
) -> dict | None:
    """Find OTM call closest to 25Δ. Falls back to 5% OTM offset."""
    calls = [c for c in contracts if c.get("contract_type") == "call"]
    if not calls:
        return None

    T = dte_days / 365.0
    if T <= 0 or iv_estimate <= 0:
        target = stock_close * (1 + OTM_OFFSET_PCT)
        return min(calls, key=lambda c: abs(c["strike_price"] - target))

    return min(
        calls,
        key=lambda c: abs(_bs_delta(stock_close, c["strike_price"], T, rfr, iv_estimate, "call") - TARGET_DELTA_CALL),
    )


def _fetch_contracts_for_expiry(
    polygon_client: PolygonClientService,
    underlying: str,
    expiration_date: str,
    stock_close: float,
    dte_days: int = 30,
) -> dict[str, Any]:
    """Fetch and filter contracts for a specific expiry, returning ATM + OTM contracts."""
    strike_range = stock_close * 0.15  # Search within 15% of ATM
    # expired=True is needed here (no as_of_date conflict) to find
    # contracts whose expiration date has already passed.
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
    atm_put = _find_atm_strike(puts, stock_close)
    otm_put = _find_otm_put_by_delta(contracts, stock_close, dte_days, rfr=DEFAULT_RISK_FREE_RATE)
    otm_call = _find_otm_call_by_delta(contracts, stock_close, dte_days, rfr=DEFAULT_RISK_FREE_RATE)

    return {
        "atm_call": atm_call,
        "atm_put": atm_put,
        "otm_put": otm_put,
        "otm_call": otm_call,
    }


def _list_month_expiries(
    polygon_client: PolygonClientService,
    underlying: str,
    first_date: datetime,
    last_date: datetime,
    stock_close: float | None = None,
) -> set[str]:
    """List the call expiries, as of ``first_date``, that a session from ``first_date`` to ``last_date`` can use.

    One wide lookup serves the whole month: the window runs from the first session's nearest usable expiry (7 days
    out) to the last session's farthest (45 days out). ``pick_bracket_expiries`` picks each date's pair from it.
    """
    search_start = first_date + timedelta(days=MIN_DTE_DAYS)
    search_end = last_date + timedelta(days=EXPIRY_MAX_DTE)

    trade_date_str = first_date.strftime("%Y-%m-%d")

    # Use narrow strike filter (±5% ATM) to reduce results per expiry,
    # ensuring we capture enough expiry dates within the limit.
    strike_gte = None
    strike_lte = None
    if stock_close and stock_close > 0:
        narrow_range = stock_close * 0.05
        strike_gte = stock_close - narrow_range
        strike_lte = stock_close + narrow_range

    # Do NOT pass expired=True with as_of — that combination filters to
    # contracts already expired on the as_of date, returning 0 results
    # for contracts that were still active at that time.
    limit = EXPIRY_LOOKUP_CONTRACTS_PER_DAY * (search_end - search_start).days
    contracts = polygon_client.list_options_contracts(
        underlying_ticker=underlying,
        as_of_date=trade_date_str,
        expiration_date_gte=search_start.strftime("%Y-%m-%d"),
        expiration_date_lte=search_end.strftime("%Y-%m-%d"),
        strike_price_gte=strike_gte,
        strike_price_lte=strike_lte,
        contract_type="call",
        limit=limit,
    )
    if len(contracts) >= limit:
        # Contracts come nearest expiry first, so the cut falls on the far end: later sessions may lack a far expiry
        logger.warning(f"[CONTRACT FINDER] Expiry lookup for {underlying} hit its {limit}-contract cap; the far expiries may be cut")

    return {exp for c in contracts if (exp := c.get("expiration_date"))}


def pick_bracket_expiries(expiries: Collection[str] | None, trade_date: str) -> tuple[str, str] | BracketGap:
    """Pick the expiry pair bracketing 30 calendar days from ``trade_date`` out of the month's listed expiries.

    Returns ``(low_expiry, high_expiry)``: the latest expiry 7 to 30 days out and the earliest 31 to 45 days out,
    so 30 days is always inside the pair. An expiry exactly 30 days out is the low end, or the high end when
    nothing is listed beyond it (its own IV is the answer either way). A date with no such pair gets the
    ``BracketGap`` that says what is missing. ``expiries`` is ``None`` when the month's lookup failed.
    """
    if expiries is None:
        return BracketGap.LOOKUP_FAILED
    if not expiries:
        return BracketGap.NO_EXPIRIES

    day = date.fromisoformat(trade_date)
    near_end = (day + timedelta(days=MIN_DTE_DAYS)).isoformat()
    target = (day + timedelta(days=TARGET_DTE)).isoformat()
    far_end = (day + timedelta(days=EXPIRY_MAX_DTE)).isoformat()

    low_expiry = max((exp for exp in expiries if near_end <= exp <= target), default=None)
    if low_expiry is None:
        return BracketGap.NO_NEAR_EXPIRY
    high_expiry = min((exp for exp in expiries if target < exp <= far_end), default=None)
    if high_expiry is not None:
        return low_expiry, high_expiry
    if low_expiry == target:
        below = max((exp for exp in expiries if near_end <= exp < target), default=None)
        if below is not None:
            return below, low_expiry
    return BracketGap.NO_FAR_EXPIRY


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
        [date, stock_close, low_dte, low_dte_atm_contract, low_dte_atm_put_contract,
         low_dte_put_contract, low_dte_call_contract, high_dte, high_dte_atm_contract,
         high_dte_atm_put_contract, high_dte_put_contract, high_dte_call_contract, bracket_gap]
        ``bracket_gap`` is None when the date has a pair, else the ``BracketGap`` value saying why it has none.
        The pair is picked per date (``pick_bracket_expiries``) from one expiry lookup per month, so 30 days is
        inside ``[low_dte, high_dte]`` on every date that has one.
    """
    # Build date→close map from stock bars
    date_close_map: dict[str, float] = {}
    for bar in stock_bars:
        ts = bar.get("timestamp") or bar.get("t")
        close = bar.get("close") or bar.get("c")
        if ts is not None and close is not None:
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts / 1000, tz=UTC)
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

    # The last trading day of each month in the range: a month's one expiry lookup must reach what its last day needs
    last_date_by_month = {d[:7]: d for d in filtered_dates}

    # One wide expiry lookup per month; each date picks its own pair from it. None marks a lookup that failed.
    _expiry_cache: dict[str, set[str] | None] = {}

    # Cache contract selections — same (underlying, expiry) reused across many trading days
    _contract_cache: dict[str, dict[str, Any]] = {}

    for i, date_str in enumerate(filtered_dates):
        stock_close = date_close_map[date_str]
        trade_date = datetime.strptime(date_str, "%Y-%m-%d")

        if i % 50 == 0:
            logger.info(f"[CONTRACT FINDER] Processing date {i + 1}/{len(filtered_dates)}: {date_str}")

        # List the month's expiries once, then pick this date's bracketing pair from them
        cache_key = f"{underlying}:{trade_date.strftime('%Y-%m')}"
        if cache_key not in _expiry_cache:
            try:
                _expiry_cache[cache_key] = _list_month_expiries(
                    polygon_client,
                    underlying,
                    trade_date,
                    datetime.strptime(last_date_by_month[date_str[:7]], "%Y-%m-%d"),
                    stock_close=stock_close,
                )
            except Exception as e:
                logger.warning(f"[CONTRACT FINDER] Error finding expiries for {date_str}: {e}")
                _expiry_cache[cache_key] = None

        pair = pick_bracket_expiries(_expiry_cache[cache_key], date_str)
        low_exp, high_exp = (None, None) if isinstance(pair, BracketGap) else pair

        row: dict[str, Any] = {
            "date": date_str,
            "stock_close": stock_close,
            "low_dte": None,
            "low_dte_atm_contract": None,
            "low_dte_atm_put_contract": None,
            "low_dte_put_contract": None,
            "low_dte_call_contract": None,
            "high_dte": None,
            "high_dte_atm_contract": None,
            "high_dte_atm_put_contract": None,
            "high_dte_put_contract": None,
            "high_dte_call_contract": None,
            "bracket_gap": pair.value if isinstance(pair, BracketGap) else None,
        }

        if low_exp:
            low_dte = (datetime.strptime(low_exp, "%Y-%m-%d") - trade_date).days
            row["low_dte"] = low_dte
            try:
                contract_key = f"{underlying}:{low_exp}"
                if contract_key not in _contract_cache:
                    _contract_cache[contract_key] = _fetch_contracts_for_expiry(
                        polygon_client, underlying, low_exp, stock_close, dte_days=low_dte
                    )
                low_contracts = _contract_cache[contract_key]
                if low_contracts["atm_call"]:
                    row["low_dte_atm_contract"] = low_contracts["atm_call"].get("ticker")
                if low_contracts.get("atm_put"):
                    row["low_dte_atm_put_contract"] = low_contracts["atm_put"].get("ticker")
                if low_contracts["otm_put"]:
                    row["low_dte_put_contract"] = low_contracts["otm_put"].get("ticker")
                if low_contracts["otm_call"]:
                    row["low_dte_call_contract"] = low_contracts["otm_call"].get("ticker")
            except Exception as e:
                logger.warning(f"[CONTRACT FINDER] Error fetching low-DTE contracts for {date_str}: {e}")

        if high_exp:
            high_dte = (datetime.strptime(high_exp, "%Y-%m-%d") - trade_date).days
            row["high_dte"] = high_dte
            try:
                contract_key = f"{underlying}:{high_exp}"
                if contract_key not in _contract_cache:
                    _contract_cache[contract_key] = _fetch_contracts_for_expiry(
                        polygon_client, underlying, high_exp, stock_close, dte_days=high_dte
                    )
                high_contracts = _contract_cache[contract_key]
                if high_contracts["atm_call"]:
                    row["high_dte_atm_contract"] = high_contracts["atm_call"].get("ticker")
                if high_contracts.get("atm_put"):
                    row["high_dte_atm_put_contract"] = high_contracts["atm_put"].get("ticker")
                if high_contracts["otm_put"]:
                    row["high_dte_put_contract"] = high_contracts["otm_put"].get("ticker")
                if high_contracts["otm_call"]:
                    row["high_dte_call_contract"] = high_contracts["otm_call"].get("ticker")
            except Exception as e:
                logger.warning(f"[CONTRACT FINDER] Error fetching high-DTE contracts for {date_str}: {e}")

        rows.append(row)

    return pd.DataFrame(rows)
