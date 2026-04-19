"""LEAN-compatible backtest engine API.

POST /api/engine/backtest runs a strategy through the in-process engine at
``app.engine`` against LEAN-format minute data. Phase 1 supports a single
registered strategy (``spy_ema_crossover``) with bit-exact LEAN parity.

This endpoint is intentionally separate from the existing
``/api/backtest`` pipeline so both can coexist while the new engine is
being rolled out.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
import pandas as pd
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError

from app.engine.data.availability import (
    AvailabilityReport,
    check_availability,
    ensure_range,
)
from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.results.statistics import summarize
from app.engine.strategy.algorithms.rsi_mean_reversion import (
    RsiMeanReversionAlgorithm,
)
from app.engine.strategy.algorithms.sma_crossover import SmaCrossoverAlgorithm
from app.engine.strategy.algorithms.spy_ema_crossover import (
    SpyEmaCrossoverAlgorithm,
)
from app.engine.strategy.algorithms.spy_ema_crossover_options import (
    SpyEmaCrossoverOptionsAlgorithm,
)
from app.engine.strategy.algorithms.spy_orb import SpyOpeningRangeBreakout
from app.engine.strategy.base import Strategy
from app.routers.backtest import (
    LeanPortfolioStatsResponse,
    LeanRuntimeStatsResponse,
    LeanStatisticsResponse,
    LeanTradeStatsResponse,
)
from app.services.strategies.common import TradeRecord
from app.services.strategies.lean_statistics import compute_lean_statistics

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------
# Each registered strategy declares:
#   * ``factory``  — a zero-arg callable that returns an instance when invoked
#                    with the strategy's own default parameters
#   * ``param_schema`` — a Pydantic model the router uses to validate the
#                        request body's ``params`` field and to advertise the
#                        schema via ``GET /api/engine/strategies``
#   * ``build``    — a callable that takes the validated params model and
#                    returns a fully-constructed strategy instance
#   * ``display_name`` / ``description`` — shown by the Angular strategy picker
#
# Keeping the three callables separate (factory/build + schema) lets the
# router expose a default-argument instance for metadata listing while still
# honouring request-level overrides when the user actually runs a backtest.
# ---------------------------------------------------------------------------


class StrategyParamsBase(BaseModel):
    """Base for every strategy's parameter model.

    Subclasses declare the strategy's own fields. A strategy with no
    parameters can simply reuse this class directly.
    """

    model_config = {"extra": "forbid"}


class EmaCrossoverParams(StrategyParamsBase):
    """EMA crossover parameters — dynamic ticker.

    Shares the exact indicator / gap / RSI logic as the LEAN-parity SPY
    reference run, but lets the user pick any ticker at request time.
    Defaults to SPY so the out-of-the-box run matches the bit-exact
    reference fixture; other symbols (QQQ, IWM, etc.) can be substituted
    without touching code as long as the data has been fetched into the
    cache first (flip ``auto_fetch: true`` to pull on demand).
    """

    symbol: str = Field("SPY", min_length=1, max_length=20)


class SmaCrossoverParams(StrategyParamsBase):
    symbol: str = Field("SPY", min_length=1, max_length=20)
    short_window: int = Field(10, ge=2, le=500)
    long_window: int = Field(30, ge=3, le=1000)
    resolution_minutes: int = Field(15, ge=1, le=1440)


class DailySmaCrossoverParams(StrategyParamsBase):
    """Daily-resolution SMA crossover — no ``resolution_minutes`` field.

    The bar cadence is fixed to 1 day by the registry's build function
    (which sets ``resolution_minutes=1440`` on the underlying algorithm)
    because the strategy runs directly against LEAN daily zips. Window
    sizes are in *days* here: a 50/200 is the classic long-term golden
    cross.
    """

    symbol: str = Field("AAPL", min_length=1, max_length=20)
    short_window: int = Field(50, ge=2, le=500)
    long_window: int = Field(200, ge=3, le=1000)


class RsiMeanReversionParams(StrategyParamsBase):
    symbol: str = Field("SPY", min_length=1, max_length=20)
    window: int = Field(14, ge=2, le=500)
    oversold: float = Field(30.0, gt=0, lt=100)
    overbought: float = Field(70.0, gt=0, lt=100)
    resolution_minutes: int = Field(15, ge=1, le=1440)


class OrbParams(StrategyParamsBase):
    """Opening Range Breakout parameters — dynamic ticker.

    Zero-warmup, price-action-only strategy. Each trading day resets
    independently so the parameter set carries over cleanly between
    tickers (SPY, QQQ, IWM, etc.) without re-tuning.

    * ``orb_bars`` — number of 15-min bars forming the opening range
      (3 = 45 minutes, the default).
    * ``hold_bars`` — bars to hold after entry before flattening
      (5 = 75 minutes, the default).
    * ``min_range_pct`` / ``max_range_pct`` — accept only days whose
      opening-range size, expressed as a percentage of the range-low
      price, falls inside this band. Filters out both flat-open days
      (nothing to break out of) and gap-open days (too stretched).
    """

    symbol: str = Field("SPY", min_length=1, max_length=20)
    orb_bars: int = Field(3, ge=1, le=6)
    hold_bars: int = Field(5, ge=1, le=50)
    min_range_pct: float = Field(0.30, ge=0.0, le=10.0)
    max_range_pct: float = Field(1.50, ge=0.1, le=20.0)


class EmaCrossoverOptionsParams(StrategyParamsBase):
    """EMA crossover options spread strategy parameters.

    Same signal engine as the equity EMA crossover, but trades bull call
    or bull put spreads on the underlying's option chain instead.
    """

    # Signal parameters (same defaults as equity strategy)
    symbol: str = Field("SPY", min_length=1, max_length=20)
    ema_fast_period: int = Field(5, ge=2, le=200)
    ema_slow_period: int = Field(10, ge=3, le=500)
    rsi_period: int = Field(14, ge=2, le=200)
    ema_gap_min: float = Field(0.20, ge=0)
    rsi_min: float = Field(50.0, ge=0, lt=100)
    rsi_max: float = Field(70.0, gt=0, le=100)
    timeframe_minutes: int = Field(15, ge=1, le=1440)
    bars_to_hold: int = Field(5, ge=1, le=200)

    # Options parameters
    spread_type: str = Field(
        "BULL_CALL",
        description="BULL_CALL or BULL_PUT",
    )
    min_dte: int = Field(7, ge=0, le=365)
    max_dte: int = Field(30, ge=1, le=365)
    long_call_delta_target: float = Field(0.60, ge=0, le=1)
    short_call_delta_target: float = Field(0.30, ge=0, le=1)
    short_put_delta_target: float = Field(-0.30, ge=-1, le=0)
    long_put_delta_target: float = Field(-0.15, ge=-1, le=0)
    min_open_interest: int = Field(100, ge=0)
    min_volume: int = Field(10, ge=0)
    max_bid_ask_spread_pct: float = Field(0.20, ge=0, le=1)
    contracts_per_trade: int = Field(1, ge=1, le=100)
    max_positions: int = Field(1, ge=1, le=10)
    contract_multiplier: int = Field(100, ge=1)

    # Pricing parameters
    pricing_mode: str = Field(
        "quantlib_only",
        description="quantlib_only, market_preferred, or market_required",
    )
    pricing_engine: str = Field("analytic_bs")
    risk_free_rate: float = Field(0.05, ge=0, le=1)
    dividend_yield: float = Field(0.0, ge=0, le=1)
    default_iv: float = Field(0.20, ge=0.01, le=5.0)
    half_spread_pct: float = Field(0.01, ge=0, le=0.5)


@dataclass
class StrategyRegistration:
    display_name: str
    description: str
    param_schema: type[StrategyParamsBase]
    build: Callable[[StrategyParamsBase], Strategy]
    # Which data resolutions the strategy can run against. Defaults to
    # minute-only because every currently-ported strategy consolidates
    # minute bars via a ``TradeBarConsolidator``. Daily-native strategies
    # explicitly declare ``{"daily"}``.
    supported_resolutions: set[str] = dc_field(default_factory=lambda: {"minute"})
    # Human-readable pseudocode snippet, shown under the strategy in the
    # frontend picker. Keep it short and accurate — the point is to let
    # users see the rules at a glance without opening the source file.
    algorithm_pseudocode: str = ""
    # Parity-critical gotchas and implementation quirks. Surfaced in the
    # UI so they're not rediscovered by trial and error on the next
    # ticker / strategy combination. Each entry is one short paragraph
    # or bullet; render as a list on the frontend.
    gotchas: list[str] = dc_field(default_factory=list)


_STRATEGY_REGISTRY: dict[str, StrategyRegistration] = {
    "ema_crossover": StrategyRegistration(
        display_name="EMA Crossover",
        description=(
            "15-minute EMA(5)/EMA(10) crossover with Wilders RSI(14) filter "
            "(50 ≤ RSI ≤ 70), minimum 0.20 gap between the fast and slow EMA "
            "at the signal bar, and a 5-bar hold exit. Same rules as the "
            "LEAN reference — bit-exact against the SPY log when run with "
            "the default SPY symbol. Pick any ticker; flip auto-fetch on "
            "to pull missing bars from Polygon into the local cache."
        ),
        algorithm_pseudocode=(
            "on each 15-min bar close:\n"
            "    update EMA5, EMA10, RSI14 with bar.close\n"
            "    if in_position:\n"
            "        bars_held += 1\n"
            "        if bars_held == 5:  exit at bar close\n"
            "    else if all indicators ready:\n"
            "        fresh_cross = EMA5 > EMA10  and  EMA5[-1] <= EMA10[-1]\n"
            "        gap_ok     = (EMA5 - EMA10) >= 0.20\n"
            "        rsi_ok     = 50 <= RSI <= 70\n"
            "        if fresh_cross and gap_ok and rsi_ok:\n"
            "            enter long, size = 100% of equity"
        ),
        gotchas=[
            "EMA warmup is seeded from the SMA of the first N samples — "
            "LEAN's convention, not the Pine/most-libraries default of "
            "seeding from the first sample alone. Porting EMA to a new "
            "language without matching this will produce a different "
            "EMA trajectory for the first ~4×N bars.",
            "RSI uses Wilders smoothing with period+1 warmup. The first "
            "average gain/loss is a plain SMA at sample N+1; "
            "`is_ready` flips true at sample ≥ period+1.",
            "Indicators are updated with bar.close at bar.end_time, NOT at "
            "bar.time. Off-by-one-bar timestamps break downstream timing.",
            "The _prev_ema5_above_ema10 crossover-state flag must be updated "
            "on warmup bars too — LEAN's C# sets it before the early return. "
            "Skipping warmup updates produces a spurious cross on the first "
            "post-warmup bar.",
            "15-min consolidator must be wall-clock / epoch-anchored (bars "
            "landing on :00 :15 :30 :45). A first-bar-anchored consolidator "
            "phase-shifts every bar and ruins parity.",
            "Prices must be Python Decimal end-to-end. Float drift over two "
            "years of recursive EMA is enough to misprint at 4-decimal "
            "display precision.",
            "Use ROUND_HALF_UP for display strings to match C# "
            'decimal.ToString("F2"). Python\'s default Decimal.quantize '
            "uses banker's rounding (ROUND_HALF_EVEN) and disagrees on "
            "midpoints like 515.045 → 515.05.",
            "TradingView exports timestamps in the viewer's LOCAL timezone; "
            "Engine Lab writes proper UTC in bar.end_time. Cross-system "
            "diffs must normalize to a single reference frame (see SPY EMA "
            "validation report §5.1).",
            "TV labels trades by bar START, Engine Lab by bar END — a fixed "
            "15-minute label offset that is cosmetic, not a fill-time bug.",
        ],
        param_schema=EmaCrossoverParams,
        build=lambda p: SpyEmaCrossoverAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
        ),
    ),
    "sma_crossover": StrategyRegistration(
        display_name="SMA Crossover",
        description=(
            "Classic golden-cross / death-cross rule. Enters long when the "
            "short SMA crosses above the long SMA and exits on the opposite "
            "cross. Configurable symbol, window sizes, and bar resolution."
        ),
        algorithm_pseudocode=(
            "on each bar close:\n"
            "    update SMA_short (window=short_window)\n"
            "    update SMA_long  (window=long_window)\n"
            "    if both ready:\n"
            "        if in_position and SMA_short < SMA_long:  exit\n"
            "        elif flat  and SMA_short > SMA_long and\n"
            "                       SMA_short[-1] <= SMA_long[-1]:\n"
            "            enter long, size = 100% of equity"
        ),
        gotchas=[
            "Window sizes are in BARS, not minutes. With "
            "resolution_minutes=15 and short_window=10, the short SMA "
            "covers 150 minutes (10 × 15).",
            "Re-entry requires a FRESH crossover (short was ≤ long on "
            "the prior bar and is strictly > long on this bar). The "
            "strategy cannot re-enter on the same side of the cross it "
            "just exited from, so the multi-trade-per-day issue that hit "
            "ORB doesn't apply here.",
            "Simple SMA has no recursion — it's just a rolling mean — so "
            "warmup parity across implementations is much easier than EMA. "
            "Both LEAN and Pine produce identical SMA values once they "
            "have window-size samples.",
            "Same TV timestamp conventions as other strategies: viewer's "
            "local timezone in TV exports, proper UTC in Engine Lab, "
            "bar-start vs bar-end labeling differs by the bar length.",
            "On the daily variant (see daily_sma_crossover) a 50/200 "
            "cross produces very few signals per year — 1-year backtests "
            "will often show only 1–4 trades.",
        ],
        param_schema=SmaCrossoverParams,
        build=lambda p: SmaCrossoverAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            short_window=p.short_window,  # type: ignore[attr-defined]
            long_window=p.long_window,  # type: ignore[attr-defined]
            resolution_minutes=p.resolution_minutes,  # type: ignore[attr-defined]
        ),
    ),
    "daily_sma_crossover": StrategyRegistration(
        display_name="Daily SMA Crossover",
        description=(
            "Long-term golden-cross / death-cross run against LEAN daily "
            "bars (one zip per symbol under equity/usa/daily/). Defaults "
            "to the classic 50/200 on AAPL. The underlying algorithm is "
            "the same SmaCrossoverAlgorithm used for intraday — only the "
            "bar cadence differs, which is handled by the data reader."
        ),
        algorithm_pseudocode=(
            "on each daily bar close:\n"
            "    update SMA_short  (window = short_window days)\n"
            "    update SMA_long   (window = long_window days)\n"
            "    if both ready:\n"
            "        if in_position and SMA_short < SMA_long:  exit\n"
            "        elif flat  and SMA_short > SMA_long and\n"
            "                       SMA_short[-1] <= SMA_long[-1]:\n"
            "            enter long, size = 100% of equity"
        ),
        gotchas=[
            "Uses the LEAN daily-bar reader, not a consolidator. Bars come "
            "one-per-day from equity/usa/daily/{symbol}.zip — make sure "
            "those files exist for your chosen symbol.",
            "resolution_minutes is hardcoded to 1440 in the registry's "
            "build function; exposing it as a parameter would let users "
            "break parity with the intended daily cadence.",
            "Classic 50/200 on liquid US equities produces ~1–4 crosses "
            "per year. Choose a 5–10 year window for enough trades to "
            "evaluate performance meaningfully.",
            "No intraday timing — fills are at each signal bar's close "
            "(end of trading day). Slippage/overnight-gap risk is real "
            "but not modeled by the fill model.",
            "Because bars are daily, timezone reporting conventions don't "
            "apply the same way as intraday strategies — TradingView "
            "exports a date (no time) for daily fills.",
        ],
        param_schema=DailySmaCrossoverParams,
        build=lambda p: SmaCrossoverAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            short_window=p.short_window,  # type: ignore[attr-defined]
            long_window=p.long_window,  # type: ignore[attr-defined]
            # 1440 min = 1 day. TradeBarConsolidator is reference-rounded
            # to midnight ET and passes daily bars through 1:1 as long as
            # consecutive inputs are separated by >= 1 day, which is
            # always true for LEAN daily zip rows.
            resolution_minutes=1440,
        ),
        supported_resolutions={"daily"},
    ),
    "rsi_mean_reversion": StrategyRegistration(
        display_name="RSI Mean Reversion",
        description=(
            "Long-only RSI threshold strategy. Buys when RSI drops below the "
            "oversold level and sells when RSI rises above the overbought "
            "level. Configurable symbol, window, thresholds, and resolution."
        ),
        algorithm_pseudocode=(
            "on each bar close:\n"
            "    update RSI(window, Wilders smoothing)\n"
            "    if RSI not ready:  return\n"
            "    if in_position and RSI > overbought:  exit\n"
            "    elif flat       and RSI < oversold:    enter long, 100% equity"
        ),
        gotchas=[
            "Uses Wilders smoothing — first average gain/loss is a plain "
            "SMA at sample window+1, then the recursion thereafter. Same "
            "warmup convention as the EMA strategy's RSI.",
            "No time-based exit. Position is held for as long as RSI stays "
            "between oversold and overbought. On grinding sideways markets "
            "this can be many bars; on sharp reversals it can be one bar.",
            "No per-day trade limit — once flat, the strategy will re-enter "
            "if RSI drops back below oversold. Multiple entries per day are "
            "possible (this is intentional, unlike the ORB defect).",
            "Default thresholds (30/70) may need adjustment by ticker. "
            "Higher-volatility names rarely reach 30 or 70 on the default "
            "14-period window.",
            "Same TV timestamp conventions as other intraday strategies "
            "(viewer-local-tz exports, bar-start vs bar-end labeling).",
        ],
        param_schema=RsiMeanReversionParams,
        build=lambda p: RsiMeanReversionAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            window=p.window,  # type: ignore[attr-defined]
            oversold=p.oversold,  # type: ignore[attr-defined]
            overbought=p.overbought,  # type: ignore[attr-defined]
            resolution_minutes=p.resolution_minutes,  # type: ignore[attr-defined]
        ),
    ),
    "orb": StrategyRegistration(
        display_name="Opening Range Breakout",
        description=(
            "Pure price-action Opening Range Breakout — zero indicator "
            "warmup. The opening range (default: first 3 fifteen-minute "
            "bars of the regular session, i.e. 9:30–10:15 ET) defines a "
            "high/low channel. The first bar that closes above the ORB "
            "high triggers a long entry, provided the range size as a "
            "percent of price falls inside [min_range_pct, max_range_pct]. "
            "Position is held for exactly hold_bars bars (default 5 = 75 "
            "minutes), then flattened. One trade per day max. "
            "\n\n"
            "Designed as a cross-system validation primitive: because "
            "every day starts with no carried state, two implementations "
            "of the same rules cannot drift the way recursive-indicator "
            "strategies (EMA, RSI, MACD) do. Symbol-parameterized, so the "
            "same code runs against SPY, QQQ, IWM, etc."
        ),
        algorithm_pseudocode=(
            "at first RTH bar of each day:\n"
            "    reset ORB state, traded_today = False\n"
            "on each RTH 15-min bar:\n"
            "    bar_of_day += 1\n"
            "    if bar_of_day <= orb_bars:\n"
            "        orb_high = max(orb_high, bar.high)\n"
            "        orb_low  = min(orb_low,  bar.low)\n"
            "        if bar_of_day == orb_bars:\n"
            "            range_pct = (orb_high - orb_low) / orb_low * 100\n"
            "            orb_valid = min_range_pct <= range_pct <= max_range_pct\n"
            "    elif orb_valid and not traded_today:\n"
            "        if in_position:\n"
            "            bars_held += 1\n"
            "            if bars_held == hold_bars:  exit\n"
            "        elif bar.close > orb_high:\n"
            "            enter long, 100% equity\n"
            "            traded_today = True"
        ),
        gotchas=[
            "ONE trade per day. The _traded_today flag was added to the "
            "Python port on 2026-04-18 — prior versions re-entered after "
            "every 5-bar hold exit, producing ~3× the intended trade "
            "count. Regression test: test_spy_orb_one_trade_per_day.py.",
            "RTH-only. The strategy filters on _is_rth(bar.end_time) "
            "against 9:30–16:00 ET hardcoded. If feeding extended-hours "
            "data the pre/post-market bars are ignored by the algorithm "
            "but may show up in data availability checks.",
            "15-min consolidator alignment must be wall-clock / epoch-"
            "anchored (bars at :00 :15 :30 :45) so that the first 3 bars "
            "of each day reliably correspond to 9:30–10:15.",
            "Range filter is percentage-based ((high - low) / low × 100), "
            "so it ports cleanly across tickers — 0.30–1.50% works for "
            "both SPY and QQQ without re-tuning.",
            "On days where a late entry's 5-bar hold crosses the 16:00 ET "
            "close, the exit bar is the next day's first RTH bar. Engine "
            "Lab handles this cleanly; some brokerages would reject the "
            "overnight hold.",
            "Pine Script port: `time(tf, session, tz)` returns the CURRENT "
            "bar's timestamp when in session, not the session's start. "
            'Use `time("D")` change-detection for new-day reset, NOT '
            "change-detection on the session-time value. Earlier Pine "
            "versions had this bug and produced 0 ORBs completed.",
        ],
        param_schema=OrbParams,
        build=lambda p: SpyOpeningRangeBreakout(
            symbol=p.symbol,  # type: ignore[attr-defined]
            orb_bars=p.orb_bars,  # type: ignore[attr-defined]
            hold_bars=p.hold_bars,  # type: ignore[attr-defined]
            min_range_pct=p.min_range_pct,  # type: ignore[attr-defined]
            max_range_pct=p.max_range_pct,  # type: ignore[attr-defined]
        ),
    ),
    "ema_crossover_options": StrategyRegistration(
        display_name="EMA Crossover Options",
        description=(
            "This strategy uses the exact same EMA crossover signal as the equity version "
            "— same entry times, same exit times, same 5-bar hold — but instead of buying "
            "the stock, it opens an options spread on each signal. "
            "\n\n"
            "HOW IT WORKS: Every 15-minute bar, EMA(5) and EMA(10) are computed. When "
            "EMA(5) crosses above EMA(10) with at least a 0.20 gap and RSI(14) is between "
            "50-70, the signal fires. The strategy then builds a bull call spread (or bull "
            "put spread, configurable) by selecting two option contracts by delta targeting: "
            "a long leg near 0.60 delta and a short leg near 0.30 delta. The spread is held "
            "for exactly 5 bars (75 minutes) and then closed. "
            "\n\n"
            "BULL CALL SPREAD: Buy the lower-strike call (higher delta, more expensive), "
            "sell the higher-strike call (lower delta, cheaper). You pay a net debit. "
            "Max profit = spread width minus debit. Max loss = the debit paid. Profits "
            "when the underlying moves up. "
            "\n\n"
            "BULL PUT SPREAD: Sell the higher-strike put, buy the lower-strike put. You "
            "receive a net credit. Max profit = the credit received. Max loss = spread "
            "width minus credit. Also profits when the underlying stays flat or moves up. "
            "\n\n"
            "PRICING: In QuantLib-only mode (default), option prices and Greeks are computed "
            "synthetically using Black-Scholes via QuantLib — no market data needed, fast to "
            "backtest. Market-preferred mode uses real Polygon option snapshots when available, "
            "falling back to QuantLib. Market-required mode only trades when real data exists. "
            "\n\n"
            "TRADE PARITY: Signal timing is identical to the equity EMA Crossover strategy. "
            "Every entry and exit bar matches 1:1. The only difference is what is traded — "
            "a defined-risk options spread instead of the underlying stock."
        ),
        algorithm_pseudocode=(
            "on each 15-min bar close (signal logic identical to ema_crossover):\n"
            "    update EMA_fast, EMA_slow, RSI\n"
            "    if signal fires:\n"
            "        select option chain with min_dte <= dte <= max_dte\n"
            "        long_leg  = strike with delta closest to long_call_delta_target  (0.60)\n"
            "        short_leg = strike with delta closest to short_call_delta_target (0.30)\n"
            "        verify open_interest, volume, bid-ask spread filters\n"
            "        open spread (debit for bull-call, credit for bull-put)\n"
            "        hold for bars_to_hold bars, then close both legs"
        ),
        gotchas=[
            "Signal timing is BIT-IDENTICAL to ema_crossover (same EMAs, "
            "same RSI, same gap filter). Differences in trade outcomes vs "
            "the equity strategy come purely from option pricing and "
            "spread mechanics, not from signal differences.",
            "QuantLib-only pricing mode is the default — option prices "
            "and Greeks are computed synthetically via Black-Scholes "
            "with default_iv and risk_free_rate. Switch to "
            "market_preferred or market_required to use real Polygon "
            "option snapshots when available.",
            "Bull-call spread: pay net debit upfront. Max profit = spread width − debit. Max loss = the debit paid.",
            "Bull-put spread: receive net credit upfront. Max profit = the credit. Max loss = spread width − credit.",
            "Delta targeting picks the closest available strike — exact "
            "delta values won't match the targets exactly. The "
            "long_call_delta_target=0.60 / short_call_delta_target=0.30 "
            "defaults give a roughly 1:2 risk:reward profile.",
            "min_open_interest, min_volume, and max_bid_ask_spread_pct "
            "filters can cause some signals to skip if no liquid contract "
            "is available. The skip count is reported in the engine logs.",
            "All EMA/RSI gotchas from ema_crossover apply here unchanged.",
        ],
        param_schema=EmaCrossoverOptionsParams,
        build=lambda p: SpyEmaCrossoverOptionsAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            ema_fast_period=p.ema_fast_period,  # type: ignore[attr-defined]
            ema_slow_period=p.ema_slow_period,  # type: ignore[attr-defined]
            rsi_period=p.rsi_period,  # type: ignore[attr-defined]
            ema_gap_min=p.ema_gap_min,  # type: ignore[attr-defined]
            rsi_min=p.rsi_min,  # type: ignore[attr-defined]
            rsi_max=p.rsi_max,  # type: ignore[attr-defined]
            timeframe_minutes=p.timeframe_minutes,  # type: ignore[attr-defined]
            bars_to_hold=p.bars_to_hold,  # type: ignore[attr-defined]
            spread_type=p.spread_type,  # type: ignore[attr-defined]
            min_dte=p.min_dte,  # type: ignore[attr-defined]
            max_dte=p.max_dte,  # type: ignore[attr-defined]
            long_call_delta_target=p.long_call_delta_target,  # type: ignore[attr-defined]
            short_call_delta_target=p.short_call_delta_target,  # type: ignore[attr-defined]
            short_put_delta_target=p.short_put_delta_target,  # type: ignore[attr-defined]
            long_put_delta_target=p.long_put_delta_target,  # type: ignore[attr-defined]
            min_open_interest=p.min_open_interest,  # type: ignore[attr-defined]
            min_volume=p.min_volume,  # type: ignore[attr-defined]
            max_bid_ask_spread_pct=p.max_bid_ask_spread_pct,  # type: ignore[attr-defined]
            contracts_per_trade=p.contracts_per_trade,  # type: ignore[attr-defined]
            max_positions=p.max_positions,  # type: ignore[attr-defined]
            contract_multiplier=p.contract_multiplier,  # type: ignore[attr-defined]
            pricing_mode=p.pricing_mode,  # type: ignore[attr-defined]
            pricing_engine=p.pricing_engine,  # type: ignore[attr-defined]
            risk_free_rate=p.risk_free_rate,  # type: ignore[attr-defined]
            dividend_yield=p.dividend_yield,  # type: ignore[attr-defined]
            default_iv=p.default_iv,  # type: ignore[attr-defined]
            half_spread_pct=p.half_spread_pct,  # type: ignore[attr-defined]
        ),
    ),
}


def _resolve_lean_data_root() -> Path:
    """Return the LEAN reference Data directory.

    Reads the ``LEAN_DATA_ROOT`` environment variable if set; otherwise
    falls back to the standard local-development location. This root is
    expected to be read-only in containerized deployments — any
    Polygon-sourced data goes into the cache root instead.
    """
    configured = os.environ.get("LEAN_DATA_ROOT")
    if configured:
        return Path(configured)
    return Path("/sessions/ecstatic-hopeful-volta/mnt/Lean/Data")


def _resolve_lean_cache_root() -> Path:
    """Return the writable cache root for Polygon-sourced LEAN zips.

    Reads ``LEAN_DATA_CACHE`` if set, otherwise defaults to a sibling
    ``lean-cache`` directory next to the service. This root is writable
    and receives any data fetched on demand for symbols that aren't in
    the read-only reference mount.
    """
    configured = os.environ.get("LEAN_DATA_CACHE")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "lean-cache"


def _resolve_lean_data_roots() -> list[Path]:
    """Return the ordered list of roots the reader should search.

    Reference mount comes first so the bit-exact SPY fixture always wins
    over anything that may have been materialized into the cache with the
    same date range.
    """
    roots: list[Path] = []
    ref = _resolve_lean_data_root()
    if ref.exists():
        roots.append(ref)
    cache = _resolve_lean_cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    roots.append(cache)
    return roots


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class EngineBacktestRequest(BaseModel):
    strategy_name: str = Field(..., description="Registered strategy identifier")
    fill_mode: str = Field(
        "signal_bar_close",
        description="Fill mode: signal_bar_close or next_bar_open",
    )
    commission_per_order: float = Field(1.0, ge=0)
    # Optional overrides — when omitted, the strategy's own defaults (set in
    # its Initialize equivalent) are used.
    start_date: str | None = Field(None, description="YYYY-MM-DD override")
    end_date: str | None = Field(None, description="YYYY-MM-DD override")
    initial_cash: float | None = Field(None, ge=0)
    # Strategy-specific parameters — validated per-strategy against the
    # corresponding ``StrategyParamsBase`` subclass in the registry. Left
    # untyped here because the schema varies per strategy.
    params: dict[str, Any] = Field(default_factory=dict)
    # Data resolution the engine will read. ``"minute"`` feeds
    # ``LeanMinuteDataReader`` (the Phase 1 default, used by every
    # intraday strategy that consolidates up to 15m/1h/etc.).
    # ``"daily"`` feeds ``LeanDailyDataReader`` and is reserved for
    # strategies that declare themselves daily-native.
    resolution: Literal["minute", "daily"] = Field(
        "minute",
        description="Data resolution: 'minute' (default) or 'daily'",
    )
    # When true, the router will materialize any missing data for the
    # run's symbol + date range into the cache root before starting the
    # engine. Defaults to false so the SPY fixture path (which should
    # always hit the reference mount) is never accidentally fetched.
    auto_fetch: bool = Field(
        False,
        description="Fetch missing data from Polygon into the cache before running",
    )


class EngineTradeResponse(BaseModel):
    trade_number: int
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    # Per-trade indicator snapshot captured at the entry signal. Keys depend
    # on the strategy — e.g. SPY returns ``ema5``/``ema10``/``rsi``, SMA
    # crossover returns ``sma_10``/``sma_30``. The frontend renders these
    # dynamically rather than expecting a fixed shape.
    indicators: dict[str, float] = Field(default_factory=dict)
    pnl_pts: float
    pnl_pct: float
    result: str
    signal_reason: str = ""


class EngineBacktestResponse(BaseModel):
    success: bool
    strategy_name: str
    fill_mode: str
    initial_cash: float
    final_equity: float
    net_profit: float
    total_fees: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    # Extended statistics — computed from the trade log. See
    # app/engine/results/statistics.py for the full list of keys.
    statistics: dict[str, Any] = Field(default_factory=dict)
    # Full LEAN-parity statistics (portfolio + trade + runtime).
    lean_statistics: LeanStatisticsResponse | None = None
    trades: list[EngineTradeResponse] = []
    log_lines: list[str] = []
    equity_curve: list[dict] = Field(default_factory=list)
    # Consolidated OHLCV bars for the price chart (15-min or daily depending
    # on the strategy's consolidator). Much smaller than the full minute-bar
    # stream retained in BacktestResult.bars.
    chart_bars: list[dict] = Field(default_factory=list)
    # Phase 1: Insight tracking — per-prediction scoring and aggregate analytics.
    insights: list[dict] = Field(default_factory=list)
    insight_summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_fill_mode(raw: str) -> FillMode:
    key = raw.strip().lower()
    if key in ("signal_bar_close", "signalbarclose", "close"):
        return FillMode.SIGNAL_BAR_CLOSE
    if key in ("next_bar_open", "nextbaropen", "open"):
        return FillMode.NEXT_BAR_OPEN
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown fill_mode '{raw}'. Expected signal_bar_close or next_bar_open.",
    )


def _apply_overrides(strategy: Strategy, req: EngineBacktestRequest) -> None:
    """Apply request-level overrides on top of the strategy's own defaults.

    The strategy's ``initialize`` has already run by the time this is
    called, so any override here replaces the value set by the algorithm.
    """
    if req.start_date:
        d = datetime.strptime(req.start_date, "%Y-%m-%d")
        strategy.set_start_date(d.year, d.month, d.day)
    if req.end_date:
        d = datetime.strptime(req.end_date, "%Y-%m-%d")
        strategy.set_end_date(d.year, d.month, d.day)
    if req.initial_cash is not None:
        strategy.set_cash(req.initial_cash)


def _format_trade(index: int, trade: Any) -> EngineTradeResponse:
    # ``indicators`` is a dict[str, Decimal] on ``LoggedTrade``; convert to
    # plain floats for JSON serialization.
    raw_indicators = getattr(trade, "indicators", None) or {}
    indicators = {k: float(v) for k, v in raw_indicators.items()}
    return EngineTradeResponse(
        trade_number=index,
        entry_time=trade.entry_time.strftime("%Y-%m-%d %H:%M"),
        entry_price=float(trade.entry_price),
        exit_time=trade.exit_time.strftime("%Y-%m-%d %H:%M"),
        exit_price=float(trade.exit_price),
        indicators=indicators,
        pnl_pts=float(trade.pnl_pts),
        pnl_pct=float(trade.pnl_pct),
        result=trade.result,
        signal_reason=getattr(trade, "signal_reason", "") or "",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
class StrategyInfo(BaseModel):
    name: str
    display_name: str
    description: str
    # JSON Schema for the strategy's parameter model — the frontend renders
    # the parameter form dynamically from this.
    params_schema: dict[str, Any]
    # Data resolutions this strategy accepts. The Engine Lab uses this to
    # filter the strategy dropdown once the user picks a resolution.
    supported_resolutions: list[str] = Field(default_factory=list)
    # Short pseudocode snippet of the entry/exit rules, rendered in the
    # frontend strategy picker so users can see the rules at a glance.
    algorithm_pseudocode: str = ""
    # Parity-critical gotchas — implementation quirks, porting traps, or
    # known cross-system divergences. Rendered as a bullet list under the
    # strategy in the frontend so they're not rediscovered by trial and
    # error on the next ticker / strategy combination.
    gotchas: list[str] = Field(default_factory=list)


@router.get("/strategies", response_model=list[StrategyInfo])
def list_engine_strategies() -> list[StrategyInfo]:
    """List strategies registered with the LEAN-compatible engine.

    Each entry carries the JSON Schema of its parameter model so the frontend
    can render a parameter form without hardcoding strategy knowledge. Sorted
    alphabetically for deterministic UI ordering.
    """
    result: list[StrategyInfo] = []
    for name in sorted(_STRATEGY_REGISTRY.keys()):
        reg = _STRATEGY_REGISTRY[name]
        result.append(
            StrategyInfo(
                name=name,
                display_name=reg.display_name,
                description=reg.description,
                params_schema=reg.param_schema.model_json_schema(),
                supported_resolutions=sorted(reg.supported_resolutions),
                algorithm_pseudocode=reg.algorithm_pseudocode,
                gotchas=list(reg.gotchas),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Polygon → LEAN export endpoint
# ---------------------------------------------------------------------------
class LeanExportRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., description="YYYY-MM-DD (inclusive)")
    to_date: str = Field(..., description="YYYY-MM-DD (inclusive)")
    adjusted: bool = Field(True, description="Apply split/dividend adjustments")
    resolution: Literal["minute", "daily"] = Field(
        "minute",
        description="Resolution to fetch: 'minute' (per-day zips) or 'daily' (one zip per symbol)",
    )


class LeanExportResponse(BaseModel):
    success: bool
    symbol: str
    data_root: str
    days_written: int
    files: list[str] = []
    error: str | None = None


@router.post("/export-lean", response_model=LeanExportResponse)
def export_polygon_to_lean(request: LeanExportRequest) -> LeanExportResponse:
    """Fetch a Polygon minute-bar range and export it to LEAN zips.

    Writes one ``{YYYYMMDD}_trade.zip`` per trading day under
    ``{LEAN_DATA_CACHE}/equity/usa/minute/{symbol}/``. The read-only
    reference mount is never touched — all fetched data lives in the
    writable cache so the SPY fixture's bit-exact guarantee is preserved.
    """
    # Imported lazily — keeps this module importable in test contexts
    # that don't provide a Polygon API key.
    from app.engine.data.polygon_export import export_polygon_range_to_lean
    from app.services.polygon_client import PolygonClientService

    cache_root = _resolve_lean_cache_root()
    cache_root.mkdir(parents=True, exist_ok=True)

    try:
        polygon = PolygonClientService()
        files = export_polygon_range_to_lean(
            polygon=polygon,
            output_root=cache_root,
            symbol=request.symbol.upper(),
            from_date=request.from_date,
            to_date=request.to_date,
            adjusted=request.adjusted,
            resolution=request.resolution,
        )
    except Exception as exc:
        logger.exception("[ENGINE] LEAN export failed for %s", request.symbol)
        return LeanExportResponse(
            success=False,
            symbol=request.symbol.upper(),
            data_root=str(cache_root),
            days_written=0,
            error=str(exc),
        )

    return LeanExportResponse(
        success=True,
        symbol=request.symbol.upper(),
        data_root=str(cache_root),
        days_written=len(files),
        files=[str(p) for p in files],
    )


# ---------------------------------------------------------------------------
# Data availability endpoint
# ---------------------------------------------------------------------------
class AvailabilityResponse(BaseModel):
    symbol: str
    start: str
    end: str
    resolution: str
    expected_days: int
    available_days: int
    is_complete: bool
    missing_days: list[str] = []
    # Per-root breakdown (reference mount vs cache) so the UI can tell
    # the user where the data is coming from.
    sources: dict[str, list[str]] = Field(default_factory=dict)


def _parse_iso_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name}: expected YYYY-MM-DD, got {value!r}",
        ) from exc


@router.get("/data/availability", response_model=AvailabilityResponse)
def get_data_availability(
    symbol: str = Query(..., min_length=1, max_length=20),
    start: str = Query(..., description="YYYY-MM-DD (inclusive)"),
    end: str = Query(..., description="YYYY-MM-DD (inclusive)"),
    resolution: Literal["minute", "daily"] = Query(
        "minute",
        description="Resolution to check: 'minute' (per-day zips) or 'daily'",
    ),
) -> AvailabilityResponse:
    """Report how many trading days are already on disk for a symbol.

    Checks the reference mount first, then the writable cache, and
    returns both the aggregate coverage and a per-root breakdown so the
    Angular UI can show the user whether SPY is hitting the bit-exact
    reference data or an arbitrary ticker has been fetched into the
    Polygon cache.
    """
    start_date = _parse_iso_date(start, "start")
    end_date = _parse_iso_date(end, "end")
    if end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"end ({end}) must not precede start ({start})",
        )

    roots = _resolve_lean_data_roots()
    report: AvailabilityReport = check_availability(
        roots=roots,
        symbol=symbol,
        start=start_date,
        end=end_date,
        resolution=resolution,
    )
    data = report.to_dict()
    return AvailabilityResponse(**data)


@router.post("/backtest", response_model=EngineBacktestResponse)
def run_engine_backtest(
    request: EngineBacktestRequest,
    background_tasks: BackgroundTasks,
) -> EngineBacktestResponse:
    """Run a strategy through the LEAN-compatible backtest engine.

    The engine reads LEAN-format minute zips from the configured data root
    and produces trades that reproduce LEAN's reference log bit-exactly
    when the same strategy is run against the same data.
    """
    _run_start = time.time()
    registration = _STRATEGY_REGISTRY.get(request.strategy_name)
    if registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"Unknown strategy '{request.strategy_name}'. Registered: {sorted(_STRATEGY_REGISTRY)}"),
        )

    # Strategies declare which resolutions they accept. Reject up-front so
    # the user gets a clear 400 instead of a cryptic mismatch deep inside
    # the engine when a daily-only strategy is run against minute data (or
    # vice versa).
    if request.resolution not in registration.supported_resolutions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Strategy '{request.strategy_name}' does not support "
                f"resolution '{request.resolution}'. Supported: "
                f"{sorted(registration.supported_resolutions)}"
            ),
        )

    # Validate ``request.params`` against the strategy's own schema. We do this
    # explicitly rather than making ``params`` a typed field on the request,
    # because different strategies have different parameter shapes and the
    # request has to accept all of them.
    try:
        validated_params = registration.param_schema.model_validate(request.params)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "strategy": request.strategy_name,
                "params_errors": exc.errors(),
            },
        )

    fill_mode = _parse_fill_mode(request.fill_mode)

    data_roots = _resolve_lean_data_roots()
    if not data_roots:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No LEAN data roots configured (set LEAN_DATA_ROOT or LEAN_DATA_CACHE)",
        )

    strategy = registration.build(validated_params)

    # Optional on-demand fetch: if the caller asked for auto_fetch and we
    # know the symbol + date range, make sure the cache has everything the
    # engine will try to read. The SPY fixture never needs this because it
    # lives in the read-only reference mount and is already complete.
    if request.auto_fetch:
        symbol = getattr(validated_params, "symbol", None)
        start_override = request.start_date
        end_override = request.end_date
        if symbol and start_override and end_override:
            try:
                from app.services.polygon_client import PolygonClientService

                polygon = PolygonClientService()
                ensure_range(
                    reference_roots=data_roots[:-1],
                    cache_root=data_roots[-1],
                    symbol=symbol,
                    start=_parse_iso_date(start_override, "start_date"),
                    end=_parse_iso_date(end_override, "end_date"),
                    polygon=polygon,
                    resolution=request.resolution,
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception(
                    "[ENGINE] auto_fetch failed for %s %s..%s",
                    symbol,
                    start_override,
                    end_override,
                )
                return EngineBacktestResponse(
                    success=False,
                    strategy_name=request.strategy_name,
                    fill_mode=request.fill_mode,
                    initial_cash=0.0,
                    final_equity=0.0,
                    net_profit=0.0,
                    total_fees=0.0,
                    total_trades=0,
                    winning_trades=0,
                    losing_trades=0,
                    win_rate=0.0,
                    error=f"auto_fetch failed: {exc}",
                )

    # Pick the reader class to match the requested resolution. Both
    # readers share the same ``iter_bars(symbol, start, end)`` contract so
    # the engine loop is unchanged — only the bar cadence differs.
    reader: LeanMinuteDataReader | LeanDailyDataReader
    if request.resolution == "daily":
        reader = LeanDailyDataReader(data_roots)
    else:
        reader = LeanMinuteDataReader(data_roots)
    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(
            mode=fill_mode,
            commission_per_order=Decimal(str(request.commission_per_order)),
        ),
    )

    # The strategy's initialize() runs inside engine.run(). We need to
    # apply overrides *after* initialize but *before* the main loop, so we
    # wrap initialize to interleave the override step.
    original_initialize = strategy.initialize

    def _wrapped_initialize() -> None:
        original_initialize()
        _apply_overrides(strategy, request)

    strategy.initialize = _wrapped_initialize  # type: ignore[assignment]

    try:
        result = engine.run(strategy)
    except Exception as exc:
        logger.exception("[ENGINE] Backtest failed for %s", request.strategy_name)
        return EngineBacktestResponse(
            success=False,
            strategy_name=request.strategy_name,
            fill_mode=request.fill_mode,
            initial_cash=0.0,
            final_equity=0.0,
            net_profit=0.0,
            total_fees=0.0,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=0.0,
            error=str(exc),
        )

    trades = getattr(strategy, "trade_log", []) or []
    formatted = [_format_trade(i + 1, t) for i, t in enumerate(trades)]
    wins = sum(1 for t in trades if t.result == "WIN")
    losses = sum(1 for t in trades if t.result == "LOSS")
    total = len(trades)
    win_rate = (wins / total) if total else 0.0

    # Approximate calendar span (in trading days) for annualized metrics.
    # Uses the strategy's declared date range when available.
    trading_days: int | None = None
    if strategy.start_date and strategy.end_date:
        delta = (strategy.end_date.date() - strategy.start_date.date()).days
        if delta > 0:
            # Rough: 252 trading days per 365 calendar days.
            trading_days = max(1, round(delta * 252 / 365))

    from app.engine.results.statistics import EquityPoint

    equity_points = (
        [EquityPoint(timestamp=s.timestamp, equity=float(s.equity)) for s in result.equity_curve]
        if result.equity_curve
        else None
    )

    stats = summarize(
        initial_cash=float(result.initial_cash),
        final_equity=float(result.final_equity),
        trades=trades,
        trading_days=trading_days,
        equity_curve=equity_points,
    )

    # ── LEAN-parity statistics ──────────────────────────────────────
    lean_stats_resp: LeanStatisticsResponse | None = None
    if result.bars and trades:
        try:
            # Convert retained TradeBar objects → DataFrame with timestamp + close
            bar_records = [
                {
                    "timestamp": b.time,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": int(b.volume),
                }
                for b in result.bars
            ]
            df = pd.DataFrame(bar_records)

            # Convert LoggedTrade → TradeRecord
            cum_pnl = 0.0
            trade_records: list[TradeRecord] = []
            for i, t in enumerate(trades):
                cum_pnl += float(t.pnl_pct)
                trade_records.append(
                    TradeRecord(
                        trade_number=i + 1,
                        trade_type="Buy",  # engine strategies are long-only for now
                        entry_timestamp=t.entry_time.strftime("%Y-%m-%d %H:%M"),
                        exit_timestamp=t.exit_time.strftime("%Y-%m-%d %H:%M"),
                        entry_price=float(t.entry_price),
                        exit_price=float(t.exit_price),
                        pnl=float(t.pnl_pts),
                        pnl_pct=float(t.pnl_pct),
                        cumulative_pnl_pct=cum_pnl,
                        signal_reason=getattr(t, "signal_reason", "") or "",
                        indicator_snapshot={k: float(v) for k, v in (getattr(t, "indicators", None) or {}).items()},
                    )
                )

            lean_stats = compute_lean_statistics(
                df=df,
                trades=trade_records,
                start_capital=float(result.initial_cash),
                risk_free_rate=0.0,
                benchmark_returns=None,
            )

            from dataclasses import asdict as _dc_asdict

            lean_stats_resp = LeanStatisticsResponse(
                portfolio=LeanPortfolioStatsResponse(**_dc_asdict(lean_stats.portfolio)),
                trade=LeanTradeStatsResponse(**_dc_asdict(lean_stats.trade)),
                runtime=LeanRuntimeStatsResponse(
                    equity=lean_stats.equity,
                    fees=lean_stats.fees,
                    net_profit=lean_stats.net_profit,
                    total_return=lean_stats.total_return,
                    total_orders=lean_stats.total_orders,
                ),
            )
        except Exception:
            logger.exception("[ENGINE] LEAN statistics computation failed — returning without")

    equity_curve_dicts = [
        {
            "timestamp": s.timestamp.isoformat(),
            "equity": float(s.equity),
            "cash": float(s.cash),
            "holdings_value": float(s.holdings_value),
        }
        for s in result.equity_curve
    ]

    # ── Serialize consolidated bars for charting ──
    chart_bars_dicts = [
        {
            "t": int(b.time.timestamp() * 1000),
            "o": float(b.open),
            "h": float(b.high),
            "l": float(b.low),
            "c": float(b.close),
            "v": int(b.volume),
        }
        for b in (strategy.ctx.consolidated_bars if strategy.ctx else [])
    ]

    # ── Serialize insights ──
    insights_dicts = [i.to_dict() for i in result.insights]

    response = EngineBacktestResponse(
        success=True,
        strategy_name=request.strategy_name,
        fill_mode=request.fill_mode,
        initial_cash=float(result.initial_cash),
        final_equity=float(result.final_equity),
        net_profit=float(result.net_profit),
        total_fees=float(result.total_fees),
        total_trades=total,
        winning_trades=wins,
        losing_trades=losses,
        win_rate=win_rate,
        statistics=stats,
        lean_statistics=lean_stats_resp,
        trades=formatted,
        log_lines=result.log_lines,
        equity_curve=equity_curve_dicts,
        chart_bars=chart_bars_dicts,
        insights=insights_dicts,
        insight_summary=result.insight_summary,
    )

    # ── Auto-save to .NET backend (fire-and-forget) ──────────────
    background_tasks.add_task(
        _save_study_sync,
        response=response,
        symbol=strategy.ctx.symbols[0] if strategy.ctx.symbols else "SPY",
        start_date=request.start_date or "",
        end_date=request.end_date or "",
        resolution=request.resolution or "minute",
        params_json=json.dumps(request.params) if request.params else "{}",
        duration_ms=int((time.time() - _run_start) * 1000),
    )

    return response


# ---------------------------------------------------------------------------
# Study auto-save (fire-and-forget background task)
# ---------------------------------------------------------------------------
def _save_study_sync(
    *,
    response: EngineBacktestResponse,
    symbol: str,
    start_date: str,
    end_date: str,
    resolution: str,
    params_json: str,
    duration_ms: int,
) -> None:
    """POST the backtest result to the .NET backend for persistence."""
    from app.config import settings

    backend_url = getattr(settings, "BACKEND_URL", "http://localhost:5000")
    url = f"{backend_url}/api/studies"

    # Extract LEAN portfolio stats for the top-level columns
    lp = response.lean_statistics.portfolio if response.lean_statistics else None
    lt = response.lean_statistics.trade if response.lean_statistics else None

    body: dict[str, Any] = {
        "symbol": symbol,
        "strategyName": response.strategy_name,
        "parameters": params_json,
        "startDate": start_date,
        "endDate": end_date,
        "timespan": resolution,
        "fillMode": response.fill_mode,
        "source": "engine",
        "totalTrades": response.total_trades,
        "winningTrades": response.winning_trades,
        "losingTrades": response.losing_trades,
        "totalPnL": response.net_profit,
        "maxDrawdown": lp.drawdown if lp else response.statistics.get("max_drawdown_pct", 0),
        "sharpeRatio": lp.sharpe_ratio if lp else response.statistics.get("sharpe_ratio", 0),
        "initialCash": response.initial_cash,
        "finalEquity": response.final_equity,
        "totalFees": response.total_fees,
        "winRate": response.win_rate,
        "compoundingAnnualReturn": lp.compounding_annual_return if lp else 0,
        "sortinoRatio": lp.sortino_ratio if lp else response.statistics.get("sortino_ratio", 0),
        "probabilisticSharpeRatio": lp.probabilistic_sharpe_ratio if lp else 0,
        "profitFactor": lt.profit_factor if lt else response.statistics.get("profit_factor", 0),
        "alpha": lp.alpha if lp else 0,
        "beta": lp.beta if lp else 0,
        "informationRatio": lp.information_ratio if lp else 0,
        "trackingError": lp.tracking_error if lp else 0,
        "treynorRatio": lp.treynor_ratio if lp else 0,
        "valueAtRisk95": lp.value_at_risk_95 if lp else 0,
        "valueAtRisk99": lp.value_at_risk_99 if lp else 0,
        "annualStandardDeviation": lp.annual_standard_deviation if lp else 0,
        "drawdownRecoveryDays": lp.drawdown_recovery if lp else 0,
        "leanStatisticsJson": response.lean_statistics.model_dump_json() if response.lean_statistics else None,
        "durationMs": duration_ms,
        "trades": [
            {
                "tradeType": "Buy",
                "entryTimestamp": t.entry_time,
                "exitTimestamp": t.exit_time,
                "entryPrice": t.entry_price,
                "exitPrice": t.exit_price,
                "pnL": t.pnl_pts,
                "cumulativePnL": 0,  # not tracked per-trade in engine format
                "signalReason": t.signal_reason,
            }
            for t in response.trades
        ],
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=body)
            if resp.status_code < 300:
                logger.info("[ENGINE] Study saved (id=%s)", resp.json().get("id"))
            else:
                logger.warning("[ENGINE] Study save failed: %s %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.exception("[ENGINE] Study save request failed — study not persisted")
