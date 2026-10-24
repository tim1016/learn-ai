"""IV term-structure builder for research options pipeline.

Formula: 30-day constant-maturity IV via variance-time interpolation between the two expiries bracketing 30 calendar days (T1 ≤ 30 ≤ T2 with T1 < T2, chosen for each date by ``contract_finder.pick_bracket_expiries``): σ²_30·30 = w·σ²_T1·T1 + (1-w)·σ²_T2·T2, with w = (T2 - 30)/(T2 - T1) and T in calendar days. Equal-maturity inputs retain the internal arithmetic-mean fallback.
Reference: Cboe VIX Methodology (2019), constant-maturity total-variance interpolation step only; this module uses selected-contract BSM IVs and does not replicate the model-free VIX calculation.
Canonical implementation: app/research/options/iv_builder.py
Validated against: the per-date pair and the end-to-end recovery are pinned by tests/research/options/test_iv_builder_bracket_window.py; tests/research/options/test_iv_builder_member_inputs.py recovers the volatility a priced option chain was generated with; and the whole build, over option inputs read back from the data lake, against the provider path it replaced (golden fixture XSIV-001) by tests/research/test_cross_sectional_options_lake_vs_provider.py. The option's price is its last regular-hours trade (``option_inputs.regular_session_close``): a lake bar carries a close and a volume and no bid, ask or vwap, so the price rule below resolves to its close tier.

Accepted precision exception (#3524, ADR 0069): comparison with XSIV-001's frozen macOS output allows atol=1e-14,
rtol=0 for final IVs only (measured Linux CI maximum 1.27675647831893e-15). Platform math.log rounding in the unchanged
Newton solver was isolated; provider/current-builder replay was exact within each tested runtime. Selection, all solver
inputs, missing-value masks and non-IV fields remain exact. The fixture attribution records the evidence and replay scope.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

import pandas as pd

from app.engine.edge.features_realtime.iv30_constructor import variance_interpolated_iv
from app.research.options.contract_finder import MIN_DTE_DAYS, TARGET_DTE
from app.services.fred_service import resolve_risk_free_rate
from app.volatility.solver import implied_volatility

if TYPE_CHECKING:
    from app.research.options.option_inputs import MemberOptionInputs, RateTally

logger = logging.getLogger(__name__)

MIN_OPTION_PRICE = 0.05
MIN_IV = 0.05
MAX_IV = 3.0
MIN_VOLUME = 50
MAX_FFILL_DAYS = 2


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
    risk_free_rate: float,
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

    result = implied_volatility(
        option_price=price,
        spot=stock_close,
        strike=strike,
        ttm=T,
        rate=risk_free_rate,
        is_call=(option_type == "call"),
    )
    iv = result.iv
    if iv is None:
        return None, "solver_failed"

    # Preserve the legacy [MIN_IV, MAX_IV] gate that bs_solver.implied_volatility
    # used to enforce internally. The richer canonical solver returns a wider
    # range; downstream quality-flagging would otherwise mark these as "low"
    # rather than dropping them, which changes the IV-history surface area.
    if iv < MIN_IV or iv > MAX_IV:
        return None, "iv_out_of_range"

    return iv, source


def _interpolate_iv(iv_low: float, dte_low: int, iv_high: float, dte_high: int) -> float:
    """30-day constant-maturity variance-time interpolation.

    Formula: σ²_30·30 = w·σ²_low·T_low + (1-w)·σ²_high·T_high.
    Reference: Cboe VIX Methodology (2019), maturity interpolation only.
    Canonical implementation: app/engine/edge/features_realtime/iv30_constructor.py::variance_interpolated_iv.
    Validated against: tests/edge/test_iv30_and_vrp.py, tests/research/options/test_iv_builder.py and tests/research/options/test_iv_builder_bracket_window.py.
    """
    if dte_high == dte_low:
        return (iv_low + iv_high) / 2
    return variance_interpolated_iv(
        sigma_t1=iv_low,
        t1_years=dte_low / 365,
        sigma_t2=iv_high,
        t2_years=dte_high / 365,
        target_t_years=TARGET_DTE / 365,
    )


def _normalize_iv_fallback(iv: float, dte: int) -> float | None:
    """Fallback removed — returns None to avoid unreliable sqrt(T) scaling."""
    return None


def build_iv_history(underlying: str, inputs: MemberOptionInputs, rates: RateTally) -> pd.DataFrame:
    """Build historical 30-day constant-maturity IV time series.

    Args:
        underlying: Stock ticker (e.g. "SPY")
        inputs: The member's admitted option inputs (``option_inputs.admit_member_option_inputs``): the contracts picked for
            each session off the raw close (``inputs.bracket``, whose ``stock_close`` is the IV spot) and each one's price
            on each session (``inputs.bar``). This builder reads nothing else of the option chain and fetches nothing.
        rates: Counts the source of each session's risk-free rate and the values used (FRED is live, not in the lake).

    Returns:
        DataFrame with columns:
        [date, iv_30d_atm, iv_30d_put, iv_30d_call, stock_close, dte_low, dte_high, price_source, missing_reason,
        iv_quality]
        ``missing_reason`` is the ``BracketGap`` value for a date that has no expiry pair bracketing 30 days
        (such a date has ``iv_quality`` "missing"), else empty (NaN, as ``price_source`` is on a date with no price).
    """
    logger.info(f"[IV BUILDER] Building IV history for {underlying}")

    bracket_df = inputs.bracket
    if bracket_df.empty:
        logger.error(f"[IV BUILDER] No bracket contracts found for {underlying}")
        return pd.DataFrame()

    logger.info(f"[IV BUILDER] Processing {len(bracket_df)} trading days for IV derivation")

    # Per-day loop — the prices are already folded, so nothing is asked of any source here
    rows: list[dict[str, Any]] = []

    for idx, row in bracket_df.iterrows():
        date_str = row["date"]
        session = date.fromisoformat(date_str)
        stock_close = row["stock_close"]
        dte_low = row.get("low_dte")
        dte_high = row.get("high_dte")

        if idx % 50 == 0:
            logger.info(f"[IV BUILDER] Deriving IV for date {idx + 1}/{len(bracket_df)}: {date_str}")

        # Dynamic risk-free rate for this trading day (cached per date)
        rate = resolve_risk_free_rate(dte_days=TARGET_DTE, observation_date=date_str)
        rfr = rate.rate
        rates.record(date_str, rate)

        result: dict[str, Any] = {
            "date": date_str,
            "iv_30d_atm": None,
            "iv_30d_put": None,
            "iv_30d_call": None,
            "stock_close": stock_close,
            "dte_low": dte_low,
            "dte_high": dte_high,
            "price_source": None,
            "missing_reason": row.get("bracket_gap"),
        }

        # Derive ATM IV (synthetic forward: average of call and put IV at ATM strike)
        iv_atm_low, iv_atm_high = None, None
        source = None

        low_atm = row.get("low_dte_atm_contract")
        low_atm_put = row.get("low_dte_atm_put_contract")
        high_atm = row.get("high_dte_atm_contract")
        high_atm_put = row.get("high_dte_atm_put_contract")

        if low_atm and dte_low and dte_low >= MIN_DTE_DAYS:
            bar = inputs.bar(low_atm, session)
            iv_call_low_atm, src = _derive_iv_for_contract(bar, low_atm, stock_close, dte_low, "call", rfr)
            iv_put_low_atm = None
            if low_atm_put:
                bar_p = inputs.bar(low_atm_put, session)
                iv_put_low_atm, _ = _derive_iv_for_contract(bar_p, low_atm_put, stock_close, dte_low, "put", rfr)
            # Synthetic forward: average call/put IV; fall back to call-only
            if iv_call_low_atm is not None and iv_put_low_atm is not None:
                iv_atm_low = (iv_call_low_atm + iv_put_low_atm) / 2
            else:
                iv_atm_low = iv_call_low_atm
            if iv_atm_low:
                source = src

        if high_atm and dte_high and dte_high >= MIN_DTE_DAYS:
            bar = inputs.bar(high_atm, session)
            iv_call_high_atm, src = _derive_iv_for_contract(bar, high_atm, stock_close, dte_high, "call", rfr)
            iv_put_high_atm = None
            if high_atm_put:
                bar_p = inputs.bar(high_atm_put, session)
                iv_put_high_atm, _ = _derive_iv_for_contract(bar_p, high_atm_put, stock_close, dte_high, "put", rfr)
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
            bar = inputs.bar(low_put, session)
            iv_put_low, _ = _derive_iv_for_contract(bar, low_put, stock_close, dte_low, "put", rfr)

        if high_put and dte_high and dte_high >= MIN_DTE_DAYS:
            bar = inputs.bar(high_put, session)
            iv_put_high, _ = _derive_iv_for_contract(bar, high_put, stock_close, dte_high, "put", rfr)

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
            bar = inputs.bar(low_call, session)
            iv_call_low, _ = _derive_iv_for_contract(bar, low_call, stock_close, dte_low, "call", rfr)

        if high_call and dte_high and dte_high >= MIN_DTE_DAYS:
            bar = inputs.bar(high_call, session)
            iv_call_high, _ = _derive_iv_for_contract(bar, high_call, stock_close, dte_high, "call", rfr)

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

    # Forward-fill gaps <= 2 days (weekends/holidays). A date with a typed missing_reason is a real gap, not a holiday:
    # it gets no number, and nothing is filled across it.
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.set_index("date").asfreq("B")  # Business day frequency
    segment = df["missing_reason"].notna().cumsum()  # each typed gap starts a segment, so no fill reaches into or past it
    for col in ["iv_30d_atm", "iv_30d_put", "iv_30d_call", "stock_close"]:
        if col in df.columns:
            df[col] = df[col].groupby(segment).ffill(limit=MAX_FFILL_DAYS)
    df = df.reset_index().rename(columns={"index": "date"})

    # Convert date back to string
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    valid_count = df["iv_30d_atm"].notna().sum()
    total_count = len(df)
    logger.info(
        f"[IV BUILDER] Complete: {valid_count}/{total_count} days with valid ATM IV "
        f"({valid_count / total_count * 100:.1f}% coverage)"
    )

    return df
