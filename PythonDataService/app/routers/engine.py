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
from datetime import UTC, date, datetime
from datetime import time as time_of_day
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.engine.data.availability import (
    AvailabilityReport,
    check_availability,
    ensure_range,
)
from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.engine.engine import BacktestEngine
from app.engine.execution.execution_config import ExecutionConfig
from app.engine.execution.order import FillMode
from app.engine.pine_generators import (
    generate_strategy_a_pine,
    generate_strategy_b_pine,
    generate_strategy_c_pine,
)
from app.engine.results.statistics import summarize
from app.engine.strategy.algorithms.deployment_validation import (
    DeploymentValidationConsecutiveGreen,
)
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
from app.engine.strategy.algorithms.spy_strategy_a import SpyStrategyAAlgorithm
from app.engine.strategy.algorithms.spy_strategy_b import SpyStrategyBAlgorithm
from app.engine.strategy.algorithms.spy_strategy_c import SpyStrategyCAlgorithm
from app.engine.strategy.base import Strategy
from app.models.responses import (
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


class DeploymentValidationParams(StrategyParamsBase):
    """Fixed deployment-validation strategy with configurable ticker."""

    symbol: str = Field("SPY", min_length=1, max_length=20)


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


class RsiRangeStrategyAParams(StrategyParamsBase):
    """Strategy A — EMA-gap + MACD + RSI-range, ADX-exit.

    All thresholds and indicator periods are configurable. Entry requires
    RSI to sit inside the ``[rsi_low_gate, rsi_high_gate]`` range AND the
    EMA gap to exceed ``ema_gap_threshold`` AND MACD line > 0, all at the
    same bar while flat. Pyramiding=1 prevents re-entry while holding.
    """

    symbol: str = Field("SPY", min_length=1, max_length=20, description="Underlying ticker.")
    ema_fast_period: int = Field(20, ge=2, le=500, description="Fast EMA period.")
    ema_slow_period: int = Field(50, ge=3, le=1000, description="Slow EMA period.")
    ema_gap_threshold: float = Field(
        0.5,
        ge=0,
        description=(
            "Minimum absolute gap between fast and slow EMAs "
            "(EMA_fast − EMA_slow > threshold). Default 0.5 is a reasonable "
            "SPY 15-minute trend-confirmation threshold. Other tickers scale "
            "with price — tune accordingly."
        ),
    )
    macd_fast: int = Field(12, ge=2, le=200, description="MACD fast EMA period.")
    macd_slow: int = Field(26, ge=3, le=500, description="MACD slow EMA period.")
    macd_signal: int = Field(9, ge=2, le=200, description="MACD signal-line EMA period.")
    rsi_period: int = Field(14, ge=2, le=200, description="RSI period (Wilders smoothing).")
    rsi_low_gate: float = Field(
        38.0,
        ge=0,
        lt=100,
        description="Lower bound of the RSI entry range — RSI must be ≥ this to enter.",
    )
    rsi_high_gate: float = Field(
        70.0,
        gt=0,
        le=100,
        description="Upper bound of the RSI entry range — RSI must be ≤ this to enter.",
    )
    adx_period: int = Field(14, ge=2, le=200, description="ADX period (Wilders smoothing).")
    adx_exit_threshold: float = Field(
        15.0,
        ge=0,
        le=100,
        description="Exit when ADX drops below this threshold. Default 15 for Strategy A.",
    )
    resolution_minutes: int = Field(15, ge=1, le=1440, description="Bar resolution. Default 15 minutes.")


class RsiRangeStrategyBParams(StrategyParamsBase):
    """Strategy B — Supertrend + ADX-entry + MACD + RSI-range, ADX-exit."""

    symbol: str = Field("SPY", min_length=1, max_length=20, description="Underlying ticker.")
    supertrend_atr_period: int = Field(
        10, ge=2, le=200, description="ATR period for Supertrend. Default 10 (Pine default)."
    )
    supertrend_multiplier: float = Field(
        3.0,
        gt=0,
        description="Supertrend ATR multiplier. Default 3 (Pine default).",
    )
    adx_entry_threshold: float = Field(
        20.0,
        ge=0,
        le=100,
        description="Require ADX > this threshold at entry. Default 20.",
    )
    macd_fast: int = Field(12, ge=2, le=200, description="MACD fast EMA period.")
    macd_slow: int = Field(26, ge=3, le=500, description="MACD slow EMA period.")
    macd_signal: int = Field(9, ge=2, le=200, description="MACD signal-line EMA period.")
    rsi_period: int = Field(14, ge=2, le=200, description="RSI period (Wilders smoothing).")
    rsi_low_gate: float = Field(38.0, ge=0, lt=100, description="Lower bound of RSI entry range.")
    rsi_high_gate: float = Field(70.0, gt=0, le=100, description="Upper bound of RSI entry range.")
    adx_period: int = Field(14, ge=2, le=200, description="ADX period.")
    adx_exit_threshold: float = Field(
        20.0,
        ge=0,
        le=100,
        description="Exit when ADX drops below this threshold. Default 20 for Strategy B.",
    )
    resolution_minutes: int = Field(15, ge=1, le=1440, description="Bar resolution.")


class RsiRangeStrategyCParams(StrategyParamsBase):
    """Strategy C — ADX-entry + ADX-rising + RSI-range, ADX-exit.

    No MACD, no Supertrend — the simplest of the three. Entry requires
    RSI inside the range filter, ADX above a threshold, AND ADX rising
    bar-over-bar.
    """

    symbol: str = Field("SPY", min_length=1, max_length=20, description="Underlying ticker.")
    adx_entry_threshold: float = Field(
        20.0,
        ge=0,
        le=100,
        description="Require ADX > this threshold at entry. Default 20.",
    )
    rsi_period: int = Field(14, ge=2, le=200, description="RSI period (Wilders smoothing).")
    rsi_low_gate: float = Field(38.0, ge=0, lt=100, description="Lower bound of RSI entry range.")
    rsi_high_gate: float = Field(70.0, gt=0, le=100, description="Upper bound of RSI entry range.")
    adx_period: int = Field(14, ge=2, le=200, description="ADX period.")
    adx_exit_threshold: float = Field(
        15.0,
        ge=0,
        le=100,
        description="Exit when ADX drops below this threshold. Default 15 (same as Strategy A).",
    )
    resolution_minutes: int = Field(15, ge=1, le=1440, description="Bar resolution.")


@dataclass
class StrategyRegistration:
    display_name: str
    description: str
    param_schema: type[StrategyParamsBase]
    build: Callable[[StrategyParamsBase], Strategy]
    # VCR-0004 / Phase 2 — the algorithm class the runner constructs. The
    # registry key is the module name (``app.engine.strategy.algorithms.{key}``);
    # ``class_name`` names the class inside that module. Together they retire
    # the ``<PascalKey>Algorithm`` convention so a future class rename
    # (``DeploymentValidationAlgorithm = DeploymentValidationConsecutiveGreen``
    # was the smoking gun) cannot silently break the runner's class lookup.
    class_name: str = ""
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
    # Optional Pine v6 generator — takes validated params, returns a
    # complete Pine script. When present, the frontend can download the
    # script via ``GET /api/engine/strategies/{name}/pine``.
    pine_generator: Callable[[StrategyParamsBase], str] | None = None
    # ADR 0009 § 6 — which boundary sizes this strategy.
    # ``"policy"`` (default) — the strategy targets via ``set_holdings``;
    # the deploy-form sizing selector is enabled, and
    # ``live_config.sizing`` ∈ {FixedShares, FixedNotional, SetHoldings}
    # governs the magnitude.
    # ``"explicit"`` — the strategy supplies its own quantity/contracts
    # (``market_order``, ``contracts_per_trade``, internal accounting); the
    # deploy-form sizing selector is disabled + labelled "self-sized" and
    # the required ``live_config.sizing`` is ``StrategyExplicit``.
    sizing_surface: Literal["policy", "explicit"] = "policy"
    # ADR 0012 / PRD #593 Slice 1A — which boundary chooses the
    # *instrument* this strategy trades. ``"explicit"`` (default in
    # Slices 1–3 — every current strategy) means the strategy code
    # itself names the instrument (e.g. ``set_holdings("SPY", 1.0)``),
    # and ``live_config.action`` is informational only. ``"policy"``
    # is forward-compatible with Slice 4: future strategies that emit a
    # generic ``SignalIntent`` and let the deploy-time action plan
    # resolve to concrete instruments. The deploy boundary STORES this
    # field in the run ledger but does not refuse deploys based on it
    # in Slices 1–3 — enforcement lands with consumption (Slice 4).
    instrument_surface: Literal["policy", "explicit"] = "explicit"


_STRATEGY_REGISTRY: dict[str, StrategyRegistration] = {
    "spy_ema_crossover": StrategyRegistration(
        display_name="EMA Crossover",
        class_name="SpyEmaCrossoverAlgorithm",
        description=(
            "Long-only intraday trend strategy. Bit-exact against the LEAN "
            "C# reference when run with the default SPY symbol; any ticker "
            "in the cache (or auto-fetched from Polygon) can be substituted "
            "without touching code.\n"
            "\n"
            "The strategy reads minute SPY bars, consolidates them into "
            "15-minute bars, tracks EMA(5), EMA(10) and Wilders RSI(14), "
            "and goes long for exactly five 15-minute bars (75 min) every "
            "time a fresh fast-over-slow crossover lines up with a 0.20 "
            "minimum gap and an RSI in the 50–70 trend-confirmation band. "
            "There is no stop, no target, no scaling, and at most one "
            "position open at a time."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = 15-minute bars consolidated from minute data\n"
            "\n"
            "Indicators (Alpha — updated each 15m bar close at bar.end_time)\n"
            "    EMA_fast = ExponentialMovingAverage(5)\n"
            "    EMA_slow = ExponentialMovingAverage(10)\n"
            "    RSI      = RelativeStrengthIndex(14, Wilders smoothing)\n"
            "\n"
            "Warmup\n"
            "    no signals fire until EMA_fast.is_ready and EMA_slow.is_ready\n"
            "    and RSI.is_ready (at least 14 closes for RSI; EMAs are\n"
            "    SMA-seeded over their period). The crossover-state flag is\n"
            "    primed during warmup so the first eligible bar is not a\n"
            "    spurious 'fresh' cross.\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    fresh_cross = EMA_fast > EMA_slow\n"
            "                  AND EMA_fast[-1] <= EMA_slow[-1]\n"
            "    gap_ok      = (EMA_fast - EMA_slow) >= 0.20\n"
            "    rsi_ok      = 50 <= RSI <= 70\n"
            "    ⇒ if all three: emit Insight(UP, period=5 bars); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    bars_held += 1 each new bar\n"
            "    ⇒ when bars_held == 5: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    none — no stop-loss, no take-profit, no signal-flip exit.\n"
            "    A losing trade is held to the time-stop. A reversed crossover\n"
            "    inside the 5-bar window is ignored.\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close → fills at the signal bar's close\n"
            "                                   (matches LEAN parity fixture)\n"
            "              | next_bar_open    → fills at the next bar's open\n"
            "                                   (matches TradingView trade-list)\n"
            "\n"
            "End of algorithm\n"
            "    if still in position at the last bar: Liquidate(symbol)"
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
        class_name="SmaCrossoverAlgorithm",
        description=(
            "Classic golden-cross / death-cross. Enters long when the short "
            "SMA crosses above the long SMA, exits on the opposite cross. "
            "Configurable symbol, window sizes (in bars), and bar resolution."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = consolidated to resolution_minutes (default 15m)\n"
            "\n"
            "Indicators (Alpha — updated each bar at bar.end_time)\n"
            "    SMA_short = SimpleMovingAverage(short_window bars)\n"
            "    SMA_long  = SimpleMovingAverage(long_window bars)\n"
            "\n"
            "Warmup\n"
            "    no signals until both SMAs have window-size samples.\n"
            "    SMA has no recursion (just a rolling mean), so there is\n"
            "    no warmup parity drift between LEAN and Pine.\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    fresh_cross = SMA_short > SMA_long\n"
            "                  AND SMA_short[-1] <= SMA_long[-1]\n"
            "    ⇒ if fresh_cross: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    death_cross = SMA_short < SMA_long\n"
            "    ⇒ if death_cross: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    none — exit is signal-driven only. Re-entry requires a\n"
            "    fresh crossover, which prevents the same-bar re-entry\n"
            "    that would otherwise produce many spurious trades.\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close (LEAN default) | next_bar_open"
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
        class_name="SmaCrossoverAlgorithm",
        description=(
            "Long-term golden-cross / death-cross run against LEAN daily "
            "bars (one zip per symbol under equity/usa/daily/). Defaults "
            "to the classic 50/200 on AAPL. Same SmaCrossoverAlgorithm as "
            "the intraday variant — only the bar cadence differs."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default AAPL)\n"
            "    resolution = daily (read from equity/usa/daily/{symbol}.zip)\n"
            "\n"
            "Indicators (Alpha — updated each daily bar close)\n"
            "    SMA_short = SimpleMovingAverage(short_window days, default 50)\n"
            "    SMA_long  = SimpleMovingAverage(long_window  days, default 200)\n"
            "\n"
            "Warmup\n"
            "    no signals until both SMAs have window-size samples\n"
            "    (typically ~200 trading days for the 50/200 default).\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    golden_cross = SMA_short > SMA_long\n"
            "                   AND SMA_short[-1] <= SMA_long[-1]\n"
            "    ⇒ if golden_cross: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    death_cross = SMA_short < SMA_long\n"
            "    ⇒ if death_cross: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    none — overnight gap risk is not modeled by the fill model;\n"
            "    holding through earnings, splits, and dividends is the\n"
            "    intended behavior of the long-term cross.\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close (end-of-day fill at the\n"
            "    cross bar's close)"
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
        class_name="RsiMeanReversionAlgorithm",
        description=(
            "Long-only RSI threshold strategy. Buys oversold (RSI < oversold), "
            "sells overbought (RSI > overbought). Configurable symbol, window, "
            "thresholds, and resolution. No time-based exit — position is held "
            "as long as RSI stays inside the band."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = consolidated to resolution_minutes (default 15m)\n"
            "\n"
            "Indicators (Alpha — updated each bar at bar.end_time)\n"
            "    RSI = RelativeStrengthIndex(window, Wilders smoothing)\n"
            "\n"
            "Warmup\n"
            "    no signals until RSI is_ready (sample ≥ window+1).\n"
            "    First avg gain/loss is a plain SMA at sample window+1,\n"
            "    Wilders recursion thereafter.\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    oversold = RSI < oversold_threshold\n"
            "    ⇒ if oversold: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    overbought = RSI > overbought_threshold\n"
            "    ⇒ if overbought: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    none — no stop, no time-stop, no max drawdown guard.\n"
            "    Re-entry IS allowed on the same day if RSI dips back\n"
            "    below oversold after an exit (intentional).\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close | next_bar_open"
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
    "spy_orb": StrategyRegistration(
        display_name="Opening Range Breakout",
        class_name="SpyOpeningRangeBreakout",
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
            "Universe\n"
            "    symbol     = configurable (default SPY; SPY/QQQ/IWM trade cleanly)\n"
            "    resolution = 15-minute bars consolidated from minute data\n"
            "    session    = RTH only (09:30–16:00 America/New_York)\n"
            "\n"
            "Indicators\n"
            "    none — pure price action, zero recursive state across days\n"
            "\n"
            "Warmup\n"
            "    none — every trading day starts with no carried state, which\n"
            "    is what makes ORB ideal as a cross-system validation primitive\n"
            "    (no chance of indicator-state drift between implementations).\n"
            "\n"
            "Alpha — opening-range construction (first orb_bars of each RTH day)\n"
            "    bar_of_day in [1..orb_bars]:\n"
            "        orb_high = max(orb_high, bar.high)\n"
            "        orb_low  = min(orb_low,  bar.low)\n"
            "    on the orb_bars-th bar:\n"
            "        range_pct = (orb_high - orb_low) / orb_low * 100\n"
            "        orb_valid = min_range_pct <= range_pct <= max_range_pct\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat, after ORB completes)\n"
            "    breakout = bar.close > orb_high\n"
            "    ⇒ if orb_valid AND breakout AND NOT traded_today:\n"
            "        emit Insight(UP); enter long; traded_today = True\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    bars_held += 1 each new bar\n"
            "    ⇒ when bars_held == hold_bars: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    one trade per day — traded_today flag prevents re-entry\n"
            "    after the time-stop exit (regression test:\n"
            "    test_spy_orb_one_trade_per_day.py).\n"
            "    On days where the hold window crosses 16:00 ET, the exit\n"
            "    bar is the next session's first RTH bar (overnight hold).\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close (matches LEAN-style entries)"
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
    "deployment_validation": StrategyRegistration(
        display_name="Deployment Validation",
        class_name="DeploymentValidationConsecutiveGreen",
        description=(
            "Minute-bar lifecycle validation strategy. Starting with the "
            "09:45 ET minute close, it watches for two consecutive green "
            "minute bars (close > open). After the second green bar it "
            "queues a long entry intended for next-bar-open execution, holds "
            "through the third, fourth, and fifth bars, submits a liquidation "
            "on the fifth bar, resets the detector, and repeats. At 15:45 ET "
            "it stops detecting new entries and flattens any open position. "
            "This is a deployment-validation primitive, not an alpha model."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = 1-minute bars\n"
            "    session    = regular session data; strategy starts at 09:45 ET\n"
            "\n"
            "Indicators\n"
            "    none — only bar.open and bar.close are inspected\n"
            "\n"
            "Entry pattern\n"
            "    green = bar.close > bar.open\n"
            "    if two consecutive eligible green bars occur while flat:\n"
            "        SetHoldings(symbol, 1.0)\n"
            "\n"
            "Hold / exit\n"
            "    intended fill mode is next_bar_open, so the entry fills on\n"
            "    the third bar's open after the two green confirmation bars.\n"
            "    Count bar closes while in position, including the entry bar.\n"
            "    On the fifth bar: Liquidate(symbol)\n"
            "\n"
            "Reset\n"
            "    after each exit fill, reset the green-bar detector. Bars from\n"
            "    the open trade cannot seed the next entry pattern.\n"
            "\n"
            "Session barrier\n"
            "    at 15:45 ET: stop detecting entries and liquidate any exposure\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0) — one open position at a time\n"
            "\n"
            "Execution\n"
            "    intended fill_mode = next_bar_open"
        ),
        gotchas=[
            "Run this strategy with fill_mode=next_bar_open. The global "
            "Engine Lab default is signal_bar_close for legacy LEAN parity, "
            "but this validation strategy's timing spec is next-bar-open.",
            "A green bar means close > open, not close > previous close.",
            "Detection begins on the 09:45 ET bar close and stops at 15:45 ET. "
            "The 15:45 barrier also flattens any open position.",
            "The detector resets after exit, so bars that occurred during an "
            "open position cannot contribute to the next entry pattern.",
        ],
        param_schema=DeploymentValidationParams,
        build=lambda p: DeploymentValidationConsecutiveGreen(
            symbol=p.symbol,  # type: ignore[attr-defined]
        ),
    ),
    "spy_ema_crossover_options": StrategyRegistration(
        display_name="EMA Crossover Options",
        class_name="SpyEmaCrossoverOptionsAlgorithm",
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
            "Universe\n"
            "    underlying     = configurable (default SPY)\n"
            "    derivative set = same-underlying option chain, calls & puts\n"
            "    resolution     = 15-min bars consolidated from minute data\n"
            "\n"
            "Indicators (Alpha — identical to ema_crossover, on the underlying)\n"
            "    EMA_fast = ExponentialMovingAverage(ema_fast_period)\n"
            "    EMA_slow = ExponentialMovingAverage(ema_slow_period)\n"
            "    RSI      = RelativeStrengthIndex(rsi_period, Wilders smoothing)\n"
            "\n"
            "Warmup\n"
            "    same as ema_crossover — wait for all three to be is_ready,\n"
            "    prime the prev-cross flag during warmup bars.\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    fresh_cross = EMA_fast > EMA_slow\n"
            "                  AND EMA_fast[-1] <= EMA_slow[-1]\n"
            "    gap_ok      = (EMA_fast - EMA_slow) >= ema_gap_min\n"
            "    rsi_ok      = rsi_min <= RSI <= rsi_max\n"
            "    ⇒ if all three: emit Insight(UP); proceed to spread selection\n"
            "\n"
            "Universe Selection (per signal — narrow the chain)\n"
            "    candidates = chain.filter(min_dte <= DTE <= max_dte,\n"
            "                              open_interest >= min_open_interest,\n"
            "                              volume >= min_volume,\n"
            "                              bid_ask_spread_pct <= max_bid_ask_spread_pct)\n"
            "    if no liquid contract: skip signal (logged)\n"
            "\n"
            "Portfolio Construction (per spread_type)\n"
            "    BULL_CALL: long  call ≈ long_call_delta_target  (0.60)\n"
            "               short call ≈ short_call_delta_target (0.30)\n"
            "               net debit; max profit = width − debit\n"
            "    BULL_PUT:  short put  ≈ short_put_delta_target  (-0.30)\n"
            "               long  put  ≈ long_put_delta_target   (-0.15)\n"
            "               net credit; max profit = credit\n"
            "    sizing = contracts_per_trade (default 1) × contract_multiplier (100)\n"
            "    cap    = max_positions concurrently open\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    bars_held += 1 each new 15-min bar\n"
            "    ⇒ when bars_held == bars_to_hold: close BOTH legs\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    none — defined-risk by construction (max loss = debit\n"
            "    paid for bull-call, width − credit for bull-put). No early\n"
            "    assignment handling; assumes European-style settlement for\n"
            "    the backtest window (acceptable for 7–30 DTE SPY options).\n"
            "\n"
            "Execution / Pricing\n"
            "    pricing_mode = quantlib_only      (synthetic BS via QuantLib)\n"
            "                 | market_preferred   (Polygon snapshot if available)\n"
            "                 | market_required    (skip if no market data)\n"
            "    fills land at the spread's mid ± half_spread_pct"
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
        # ADR 0009 § 6 — this strategy sizes itself via
        # ``contracts_per_trade`` (an internal options-accounting surface);
        # ``live_config.sizing`` cannot meaningfully override it. The deploy
        # form disables the sizing control + labels it "self-sized".
        sizing_surface="explicit",
    ),
    "spy_strategy_a": StrategyRegistration(
        display_name="Strategy A — EMA-gap + MACD + RSI-range",
        class_name="SpyStrategyAAlgorithm",
        description=(
            "Long-only 15-minute trend-follower. On each bar while flat, enters "
            "long if RSI is inside the [rsi_low_gate, rsi_high_gate] range AND "
            "(EMA_fast − EMA_slow) exceeds a threshold AND MACD line > 0. Exits "
            "when ADX drops below 15. Pyramiding=1 prevents re-entry while "
            "holding. Validated against TradingView via trade-by-trade CSV "
            "reconciliation."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = consolidated to resolution_minutes (default 15m)\n"
            "\n"
            "Indicators (Alpha — updated each bar at bar.end_time)\n"
            "    EMA_fast = ExponentialMovingAverage(ema_fast_period, default 20)\n"
            "    EMA_slow = ExponentialMovingAverage(ema_slow_period, default 50)\n"
            "    MACD     = MovingAverageConvergenceDivergence(\n"
            "                   macd_fast, macd_slow, macd_signal)\n"
            "    RSI      = RelativeStrengthIndex(rsi_period, Wilders smoothing)\n"
            "    ADX      = AverageDirectionalIndex(adx_period, Wilders)\n"
            "\n"
            "Warmup\n"
            "    ~200 bars by default (longest indicator chain is\n"
            "    EMA_slow=50 plus a buffer for steady state).\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    rsi_in_range = rsi_low_gate <= RSI <= rsi_high_gate\n"
            "    gap_ok       = (EMA_fast - EMA_slow) > ema_gap_threshold\n"
            "    macd_bull    = MACD.line > 0     (line, NOT histogram, NOT signal)\n"
            "    ⇒ if all three: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    trend_weak = ADX < adx_exit_threshold (default 15)\n"
            "    ⇒ if trend_weak: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    pyramiding=1 — no scale-in / re-entry while holding.\n"
            "    no SL / no TP — drawdown is unbounded until ADX collapses.\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = next_bar_open  (TradingView-parity setting;\n"
            "    signals fire at bar close, fill price = next bar's open)"
        ),
        gotchas=[
            "EMA gap threshold is ticker-scaled. Default 0.5 is a reasonable "
            "SPY 15-min starting point; other tickers scale with price.",
            "MACD gate is the MACD line itself (fast_EMA − slow_EMA), NOT the histogram and NOT the signal line.",
            "Fills land on NEXT_BAR_OPEN — the strategy signals at the bar "
            "close, the fill price is the next bar's open. Set the engine's "
            "fill mode accordingly for TV parity.",
            "Warmup is 200 bars by default — longest indicator chain is EMA_slow(50) plus buffer.",
            "No SL/TP — TV defaults. Worst-case drawdown per trade is unbounded until ADX drops below 15.",
        ],
        param_schema=RsiRangeStrategyAParams,
        pine_generator=generate_strategy_a_pine,
        build=lambda p: SpyStrategyAAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            ema_fast_period=p.ema_fast_period,  # type: ignore[attr-defined]
            ema_slow_period=p.ema_slow_period,  # type: ignore[attr-defined]
            ema_gap_threshold=p.ema_gap_threshold,  # type: ignore[attr-defined]
            macd_fast=p.macd_fast,  # type: ignore[attr-defined]
            macd_slow=p.macd_slow,  # type: ignore[attr-defined]
            macd_signal=p.macd_signal,  # type: ignore[attr-defined]
            rsi_period=p.rsi_period,  # type: ignore[attr-defined]
            rsi_low_gate=p.rsi_low_gate,  # type: ignore[attr-defined]
            rsi_high_gate=p.rsi_high_gate,  # type: ignore[attr-defined]
            adx_period=p.adx_period,  # type: ignore[attr-defined]
            adx_exit_threshold=p.adx_exit_threshold,  # type: ignore[attr-defined]
            resolution_minutes=p.resolution_minutes,  # type: ignore[attr-defined]
        ),
    ),
    "spy_strategy_b": StrategyRegistration(
        display_name="Strategy B — Supertrend + ADX + MACD + RSI-range",
        class_name="SpyStrategyBAlgorithm",
        description=(
            "Long-only 15-minute momentum strategy. Same RSI-range filter as "
            "Strategy A. On each bar while flat, enters long if RSI is in "
            "range, Supertrend is long, ADX > 20 (strong trend), and MACD "
            "line > 0. Exits when ADX drops below 20."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = consolidated to resolution_minutes (default 15m)\n"
            "\n"
            "Indicators (Alpha — updated each bar at bar.end_time)\n"
            "    Supertrend = Supertrend(supertrend_atr_period,\n"
            "                            supertrend_multiplier)   default (10, 3)\n"
            "    MACD       = MACD(macd_fast, macd_slow, macd_signal)\n"
            "    RSI        = RSI(rsi_period, Wilders)\n"
            "    ADX        = ADX(adx_period, Wilders)\n"
            "\n"
            "Warmup\n"
            "    ~50 bars minimum (Supertrend = atr_period bars,\n"
            "    MACD ≈ 34 bars to converge, RSI ≈ 15, plus buffer).\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    rsi_in_range  = rsi_low_gate <= RSI <= rsi_high_gate\n"
            "    trend_long    = Supertrend.is_long\n"
            "    trend_strong  = ADX > adx_entry_threshold (default 20)\n"
            "    macd_bull     = MACD.line > 0\n"
            "    ⇒ if all four: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    trend_weak = ADX < adx_exit_threshold (default 20)\n"
            "    ⇒ if trend_weak: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    pyramiding=1 — no scale-in while holding.\n"
            "    no SL / no TP — exit is purely trend-strength-driven.\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close (default) | next_bar_open"
        ),
        gotchas=[
            "Supertrend here uses the pandas-ta direction convention "
            "(1 = uptrend / bullish, −1 = downtrend / bearish). Pine's "
            "ta.supertrend() returns the *opposite* sign. The `is_long` "
            "property abstracts this away — read that, not the raw "
            "direction integer.",
            "Supertrend default (ATR=10, multiplier=3) matches the Pine defaults. pandas-ta's default is ATR=7.",
            "ADX entry threshold and ADX exit threshold are independent "
            "parameters — the default pair (20 in / 20 out) means the "
            "strategy exits as soon as trend strength weakens below its "
            "own entry condition.",
            "Supertrend needs at least `atr_period` bars of warmup before "
            "emitting a direction. Combined with MACD's 34-bar warmup and "
            "RSI's 15-bar warmup, plan for ~50 bars minimum before the "
            "first possible trade.",
        ],
        param_schema=RsiRangeStrategyBParams,
        pine_generator=generate_strategy_b_pine,
        build=lambda p: SpyStrategyBAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            supertrend_atr_period=p.supertrend_atr_period,  # type: ignore[attr-defined]
            supertrend_multiplier=p.supertrend_multiplier,  # type: ignore[attr-defined]
            adx_entry_threshold=p.adx_entry_threshold,  # type: ignore[attr-defined]
            macd_fast=p.macd_fast,  # type: ignore[attr-defined]
            macd_slow=p.macd_slow,  # type: ignore[attr-defined]
            macd_signal=p.macd_signal,  # type: ignore[attr-defined]
            rsi_period=p.rsi_period,  # type: ignore[attr-defined]
            rsi_low_gate=p.rsi_low_gate,  # type: ignore[attr-defined]
            rsi_high_gate=p.rsi_high_gate,  # type: ignore[attr-defined]
            adx_period=p.adx_period,  # type: ignore[attr-defined]
            adx_exit_threshold=p.adx_exit_threshold,  # type: ignore[attr-defined]
            resolution_minutes=p.resolution_minutes,  # type: ignore[attr-defined]
        ),
    ),
    "spy_strategy_c": StrategyRegistration(
        display_name="Strategy C — ADX-rising + RSI-range",
        class_name="SpyStrategyCAlgorithm",
        description=(
            "Long-only 15-minute strategy with the simplest gate: on each "
            "bar while flat, enter long if RSI is in range, ADX > 20 AND "
            "ADX is rising bar-over-bar (ADX[i] > ADX[i-1]). Exits when "
            "ADX drops below 15 (same exit rule as Strategy A)."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = consolidated to resolution_minutes (default 15m)\n"
            "\n"
            "Indicators (Alpha — updated each bar at bar.end_time)\n"
            "    RSI = RelativeStrengthIndex(rsi_period, Wilders)\n"
            "    ADX = AverageDirectionalIndex(adx_period, Wilders)\n"
            "\n"
            "Warmup\n"
            "    ~30 bars (longest dependency is ADX, Wilders-smoothed).\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    rsi_in_range = rsi_low_gate <= RSI <= rsi_high_gate\n"
            "    trend_strong = ADX > adx_entry_threshold (default 20)\n"
            "    adx_rising   = ADX > ADX[-1]    (strict; flat ADX does NOT count)\n"
            "    ⇒ if all three: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    trend_weak = ADX < adx_exit_threshold (default 15)\n"
            "    ⇒ if trend_weak: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    pyramiding=1 — no scale-in while holding.\n"
            "    no SL / no TP. Cleanest test of the RSI-range + ADX-strength\n"
            "    combination — no MACD or Supertrend confounds.\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close (default) | next_bar_open"
        ),
        gotchas=[
            "'ADX rising' is strictly greater than the prior bar. Flat ADX "
            "(exactly equal to prior) does NOT count as rising.",
            "No MACD, no Supertrend — this is the cleanest test of the RSI-range + ADX-strength combination.",
            "Same exit threshold as Strategy A by default (15). The user's "
            "TV screenshot had C's exit rule cropped; we inherit A's.",
        ],
        param_schema=RsiRangeStrategyCParams,
        pine_generator=generate_strategy_c_pine,
        build=lambda p: SpyStrategyCAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            adx_entry_threshold=p.adx_entry_threshold,  # type: ignore[attr-defined]
            rsi_period=p.rsi_period,  # type: ignore[attr-defined]
            rsi_low_gate=p.rsi_low_gate,  # type: ignore[attr-defined]
            rsi_high_gate=p.rsi_high_gate,  # type: ignore[attr-defined]
            adx_period=p.adx_period,  # type: ignore[attr-defined]
            adx_exit_threshold=p.adx_exit_threshold,  # type: ignore[attr-defined]
            resolution_minutes=p.resolution_minutes,  # type: ignore[attr-defined]
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
    """Backtest request for the in-process LEAN-compatible engine.

    Does NOT inherit ``TickerRequest`` because:
      - Dates are *optional* overrides (``None`` lets the strategy use
        its own defaults), not required.
      - The engine uses ``resolution: Literal["minute", "daily"]``
        instead of the base's ``timespan: Literal["minute","hour","day"]``
        (no "hour", and the field name differs).
      - Symbol is strategy-owned (set by the strategy's Initialize-equivalent),
        not a top-level form field.
      - No ``multiplier`` / ``session`` concepts at this layer.

    What does change in this PR: ``start_date`` / ``end_date`` are
    renamed to ``from_date`` / ``to_date`` to align with the canonical
    naming. The legacy names continue to be accepted via Pydantic
    ``AliasChoices`` during the PR (ii) → (iii) transition window;
    they're removed in PR (iii).
    """

    model_config = ConfigDict(populate_by_name=True)

    strategy_name: str = Field(..., description="Registered strategy identifier")
    fill_mode: str = Field(
        "signal_bar_close",
        description="Fill mode: signal_bar_close or next_bar_open",
    )
    commission_per_order: float = Field(1.0, ge=0)
    slippage_per_share: float = Field(
        0.0,
        ge=0,
        description=(
            "Per-share slippage applied against the trade direction at fill. "
            "Defaults to 0 to preserve LEAN-parity for bit-exact runs; pass a "
            "non-zero value (e.g. 0.02 = 2 ticks for US equities) to model a "
            "more realistic execution cost."
        ),
    )
    session_entry_cutoff: time_of_day | None = Field(
        None,
        description=(
            "After this time-of-day, entry orders (those that would grow "
            "|position|) are dropped. Exits still fill. Interpreted in the "
            "timezone of the bar data. Example: '15:55:00' for ET data."
        ),
    )
    force_flat_at: time_of_day | None = Field(
        None,
        description=(
            "At the first minute bar whose wall-clock time reaches this "
            "value, the engine cancels all queued / deferred orders, clears "
            "active TP/SL brackets, closes every open position at that "
            "minute's close, and calls strategy.on_force_flat(). Once per "
            "calendar day. Example: '15:58:00' for ET data."
        ),
    )
    limit_penetration: float = Field(
        0.0,
        ge=0,
        description=(
            "Dollar amount the bar must penetrate past a resting limit's "
            "price before the fill is recognized. Measured against the "
            "adverse extreme — low for buy limits, high for sell limits. "
            "Default 0 = TradingView-style touch fill; 0.02 for US "
            "equities is a realistic 2-tick queue-position model."
        ),
    )
    # Optional overrides — when omitted, the strategy's own defaults (set in
    # its Initialize equivalent) are used.
    from_date: str | None = Field(
        None,
        description="YYYY-MM-DD override (legacy: start_date)",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("from_date", "start_date"),
    )
    to_date: str | None = Field(
        None,
        description="YYYY-MM-DD override (legacy: end_date)",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("to_date", "end_date"),
    )
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

    # PR B (2026-05-19) — canonical DataPolicy block. Optional on the wire
    # so legacy callers (any pre-PR-B UI build) still work; when omitted, a
    # default block is synthesized from ``params.symbol`` + ``resolution``.
    # The shape mirrors ``app.lean_sidecar.data_policy.DataPolicy`` so the
    # GraphQL/engine compare-view sees an identical schema on both engines.
    data_policy: _EngineDataPolicyModel | None = Field(
        None,
        description=(
            "Canonical DataPolicy block (PR B). When omitted, synthesized "
            "from ``params.symbol`` + ``resolution`` with ``adjusted=true`` "
            "and ``session='regular'``. Required when ``params.symbol`` is "
            "absent (no source of truth for the synthesizer)."
        ),
    )

    @model_validator(mode="after")
    def _synthesize_legacy_data_policy(self) -> EngineBacktestRequest:
        """Synthesize a default ``DataPolicy`` when the caller omits it.

        One-deprecation-cycle compat. The pre-PR-B engine wire shape
        carried ``symbol`` inside ``params`` and the resolution in the
        top-level field; we synthesize a canonical block from those two
        signals so the row written to ``StrategyExecution`` always has a
        ``DataPolicyJson``. Synthesis defaults match the engine's actual
        runtime behavior today (Polygon-sourced, pre-adjusted, regular
        session, m/1 → m/1; the strategy's own consolidator handles any
        further intra-strategy timeframe).

        When BOTH ``data_policy`` and ``params.symbol`` are absent we
        leave ``data_policy=None`` rather than raising. Legacy clients
        that POST ``params={}`` rely on the strategy registry's default
        symbol (e.g., SPY) being resolved downstream; failing
        validation here would short-circuit one-cycle compat. Downstream
        consumers (``_save_study_sync``, response serialization) already
        treat ``data_policy is None`` as "policy unknown at request
        time" and emit a null ``dataPolicyJson``; the .NET persistence
        layer then synthesizes a legacy block from ``Symbol`` in that
        case (see ``BacktestRunPersistenceService.SynthesizeLegacyDataPolicy``).
        """
        if self.data_policy is not None:
            return self
        symbol = self.params.get("symbol") if isinstance(self.params, dict) else None
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            # Legacy compat: defer synthesis to downstream code once the
            # strategy registry has resolved its default symbol.
            return self
        timespan = "day" if self.resolution == "daily" else "minute"
        self.data_policy = _EngineDataPolicyModel(
            source="polygon",
            symbol=symbol.strip().upper(),
            adjusted=True,
            session="regular",
            input_bars=_EngineBarsSpecModel(timespan=timespan, multiplier=1),
            strategy_bars=_EngineBarsSpecModel(timespan=timespan, multiplier=1),
        )
        return self


# ---------------------------------------------------------------------------
# DataPolicy + BarsSpec pydantic shapes (engine-side mirror)
# ---------------------------------------------------------------------------
# PR B (2026-05-19) — the engine surface accepts the canonical DataPolicy
# block on its inbound request. Mirrors ``app.lean_sidecar.data_policy.DataPolicy``
# and the leading-underscore models in ``app.routers.lean_sidecar`` (kept
# local here to avoid importing a leading-underscore name across modules).
class _EngineBarsSpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timespan: Literal["minute", "hour", "day"]
    multiplier: int = Field(..., ge=1)


class _EngineDataPolicyModel(BaseModel):
    """Pydantic shape for the canonical ``DataPolicy`` block on the engine surface.

    Identical to ``_DataPolicyModel`` in ``app.routers.lean_sidecar`` (and
    to ``app.lean_sidecar.data_policy.DataPolicy``); duplicated here only
    because the lean_sidecar module is leading-underscore private. A
    future PR can extract a shared neutral module.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["synthetic", "polygon"]
    symbol: str
    adjusted: bool = True
    session: Literal["regular", "extended"]
    input_bars: _EngineBarsSpecModel
    strategy_bars: _EngineBarsSpecModel
    timestamp_policy: Literal["bar_close_ms_utc"] = "bar_close_ms_utc"
    timezone: Literal["America/New_York"] = "America/New_York"
    provider_kind: Literal["live", "fixture"] = "live"
    fixture_id: str | None = None
    fixture_sha256: str | None = None


EngineBacktestRequest.model_rebuild()


class EngineTradeResponse(BaseModel):
    trade_number: int
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    # Filled share/contract count from the engine's fill model. Required for
    # downstream dollar-PnL persistence — without it, ``BacktestTrade.Quantity``
    # defaults to 1 on the .NET side and the persisted PnL silently diverges
    # from the actual run by a factor of ``quantity``. See
    # ``.claude/rules/numerical-rigor.md`` → ``QUANTITY_MISMATCH``.
    quantity: int
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
    # Auto-save study id, populated synchronously before returning so the
    # Engine Lab can immediately enable the Replay tab without polling
    # /api/studies for the latest row. Null when the save call failed
    # (the run still succeeded — the persistence is best-effort).
    study_id: int | None = None
    error: str | None = None
    # PR B (2026-05-19) — echo of the post-normalization DataPolicy so the
    # frontend can render the policy that was actually used by the engine
    # (which may differ from the request when the legacy synthesizer kicked
    # in). Never null on a successful run.
    data_policy: _EngineDataPolicyModel | None = None


# ---------------------------------------------------------------------------
# Phase callbacks
#
# Both the synchronous /backtest endpoint and the Jobs-system worker call
# the same ``_execute_engine_backtest_core`` to do the actual run. They
# differ only in how progress is reported: the sync path passes no-op
# callbacks (the response is the only signal); the Jobs worker forwards
# every phase/log into a ProgressEmitter that writes Redis events the
# .NET SSE layer streams to the browser. Keeping this as a callback pair
# avoids importing ProgressEmitter into the hot path.
# ---------------------------------------------------------------------------
PhaseCallback = Callable[[str], None]
LogCallback = Callable[[str], None]


def _noop_phase(_: str) -> None:
    pass


def _noop_log(_: str) -> None:
    pass


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
    if req.from_date:
        d = datetime.strptime(req.from_date, "%Y-%m-%d")
        strategy.set_start_date(d.year, d.month, d.day)
    if req.to_date:
        d = datetime.strptime(req.to_date, "%Y-%m-%d")
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
        # ISO-8601 with Z designator: this string is the wire format the
        # .NET ``StudiesApi.ParseUtc`` requires when persisting trades, and
        # per ``.claude/rules/numerical-rigor.md`` §"Timestamp rigor" naive
        # ISO strings are banned at boundaries. Earlier `"%Y-%m-%d %H:%M"`
        # encoding caused every Python engine save to silently 500 against
        # `/api/studies` since the parser was hardened.
        entry_time=_to_utc_iso(trade.entry_time),
        entry_price=float(trade.entry_price),
        exit_time=_to_utc_iso(trade.exit_time),
        exit_price=float(trade.exit_price),
        quantity=int(trade.quantity),
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
    # True when a Pine v6 generator is registered for this strategy.
    # The frontend uses this to show/hide the Pine-download button.
    pine_available: bool = False
    # ADR 0009 § 6 — the boundary that sizes this strategy. ``"policy"`` =
    # set_holdings via live_config.sizing; ``"explicit"`` = strategy supplies
    # its own quantity/contracts and the deploy form's sizing control is
    # disabled + labelled "self-sized".
    sizing_surface: Literal["policy", "explicit"] = "policy"


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
                pine_available=reg.pine_generator is not None,
                sizing_surface=reg.sizing_surface,
            )
        )
    return result


@router.post("/strategies/{name}/pine", response_class=PlainTextResponse)
def generate_pine_script(name: str, params: dict[str, Any]) -> PlainTextResponse:
    """Generate a Pine v6 script for ``name`` using the given params.

    The request body is the same ``{params: {...}}`` shape used by
    ``/backtest`` — the same Pydantic schema validates it. Response is
    the Pine source as ``text/plain`` so the browser can offer it as a
    direct download.
    """
    reg = _STRATEGY_REGISTRY.get(name)
    if reg is None:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {name}")
    if reg.pine_generator is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Pine script template available for strategy '{name}'",
        )
    try:
        validated = reg.param_schema(**params)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    pine_source = reg.pine_generator(validated)
    return PlainTextResponse(
        content=pine_source,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.pine"',
        },
    )


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
) -> EngineBacktestResponse:
    """Run a strategy through the LEAN-compatible backtest engine (synchronous).

    Used by tests, curl, and any caller that doesn't need streamed
    progress. The Engine Lab UI uses the Jobs system instead — see
    ``POST /api/jobs/engine_backtest`` (defined in the .NET layer) which
    forwards to ``/api/jobs-internal/engine-backtest`` here.

    The engine reads LEAN-format minute zips from the configured data
    root and produces trades that reproduce LEAN's reference log
    bit-exactly when the same strategy is run against the same data.
    """
    return execute_engine_backtest(
        request=request,
        on_phase=_noop_phase,
        on_log=_noop_log,
    )


def execute_engine_backtest(
    *,
    request: EngineBacktestRequest,
    on_phase: PhaseCallback,
    on_log: LogCallback,
) -> EngineBacktestResponse:
    """Core backtest workflow shared by the sync POST and the Jobs worker.

    Both call paths converge here. ``on_phase`` and ``on_log`` are
    callbacks the worker uses to forward to a ProgressEmitter; the sync
    path passes no-ops. Raises HTTPException for client errors; returns
    an EngineBacktestResponse with ``success=False`` for engine
    failures.
    """
    _run_start = time.time()
    registration = _STRATEGY_REGISTRY.get(request.strategy_name)
    if registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"Unknown strategy '{request.strategy_name}'. Registered: {sorted(_STRATEGY_REGISTRY)}"),
        )

    if request.resolution not in registration.supported_resolutions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Strategy '{request.strategy_name}' does not support "
                f"resolution '{request.resolution}'. Supported: "
                f"{sorted(registration.supported_resolutions)}"
            ),
        )

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

    if request.auto_fetch:
        symbol = getattr(validated_params, "symbol", None)
        start_override = request.from_date
        end_override = request.to_date
        if symbol and start_override and end_override:
            on_phase("fetching_data")
            on_log(f"Ensuring {symbol} {request.resolution} bars {start_override} → {end_override}")
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

    reader: LeanMinuteDataReader | LeanDailyDataReader
    if request.resolution == "daily":
        reader = LeanDailyDataReader(data_roots)
    else:
        # Honor the request's ``data_policy.session`` so the reader drops
        # extended-hours bars when the operator asked for the regular session.
        # Before this was wired, the policy value round-tripped through the
        # response but never reached the reader, and Polygon-sourced caches
        # (which retain pre/post-market by design) silently fed 04:00-20:00 ET
        # bars to the consolidator. See ``.claude/rules/numerical-rigor.md``
        # → ``DECISION_MISMATCH`` and the divergence trace at
        # ``StrategyExecutions`` rows 41/42 (run on 2026-05-21).
        session_mode = "regular"
        if request.data_policy is not None:
            session_mode = request.data_policy.session
        reader = LeanMinuteDataReader(data_roots, session=session_mode)
    execution_config = ExecutionConfig(
        fill_mode=fill_mode,
        commission_per_order=Decimal(str(request.commission_per_order)),
        slippage_per_share=Decimal(str(request.slippage_per_share)),
        session_entry_cutoff=request.session_entry_cutoff,
        force_flat_at=request.force_flat_at,
        limit_penetration=Decimal(str(request.limit_penetration)),
    )
    engine = BacktestEngine(
        data_source=reader,
        execution_config=execution_config,
    )

    original_initialize = strategy.initialize

    def _wrapped_initialize() -> None:
        original_initialize()
        _apply_overrides(strategy, request)

    strategy.initialize = _wrapped_initialize  # type: ignore[assignment]

    # Decompose the old monolithic "simulating" phase into the two stages
    # the engine walks through during ``engine.run``. The engine itself
    # is a single call from our side, so both phases fire back-to-back
    # immediately before invocation — they're contractually-ordered
    # markers, not progress checkpoints inside the engine loop.
    on_phase("consolidating_bars")
    on_log("Consolidating raw bars to strategy resolution")
    on_phase("running_indicators")
    on_log(f"Running {request.strategy_name} on {getattr(validated_params, 'symbol', '?')} ({request.resolution})")

    try:
        result = engine.run(strategy)
    except Exception as exc:
        logger.exception("[ENGINE] Backtest failed for %s", request.strategy_name)
        on_log(f"Engine error: {exc}")
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

    on_phase("aggregating_results")
    on_log(f"Engine produced {len(getattr(strategy, 'trade_log', []) or [])} trades; aggregating results and statistics")

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
        data_policy=request.data_policy,  # PR B — echo the normalized policy
    )

    # ── Auto-save to .NET backend (synchronous so we can return the id) ──
    # Used by the Engine Lab to enable the Replay tab right after a run
    # without an extra round-trip to /api/studies?latest=true. The save
    # itself is best-effort — a backend hiccup leaves study_id=None and
    # logs the failure but does not fail the backtest response.
    on_phase("persisting")
    on_log("Persisting run to history")
    response.study_id = _save_study_sync(
        response=response,
        symbol=strategy.ctx.symbols[0] if strategy.ctx.symbols else "SPY",
        start_date=request.from_date or "",
        end_date=request.to_date or "",
        resolution=request.resolution or "minute",
        params_json=json.dumps(request.params) if request.params else "{}",
        duration_ms=int((time.time() - _run_start) * 1000),
        commission_per_order=float(request.commission_per_order),
    )

    on_log(f"Saved study {response.study_id}")

    return response


# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------
def _to_utc_iso(dt: datetime) -> str:
    """Format a datetime as ISO-8601 UTC with Z designator.

    Required by .NET ``StudiesApi.ParseUtc`` which only accepts
    ``yyyy-MM-ddTHH:mm:ss'Z'``. Naive inputs are treated as UTC for
    defensive backwards-compat — the engine's bar pipeline normally
    yields tz-aware ET datetimes, but a strategy that bypasses the
    standard pipeline must not silently fail the save.

    See ``.claude/rules/numerical-rigor.md`` §"Timestamp rigor": naive
    ISO strings are banned as wire format; this is the canonical
    encoding for engine→backend trade timestamps until they move to
    int64 ms UTC.
    """
    if dt.tzinfo is None:
        dt_utc = dt.replace(tzinfo=UTC)
    else:
        dt_utc = dt.astimezone(UTC)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


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
    commission_per_order: float = 0.0,
) -> int | None:
    """POST the backtest result to the .NET backend for persistence.

    Returns the saved study id so the Engine Lab can immediately enable
    the Replay tab. Returns None when the save fails — the run itself
    is unaffected; persistence is best-effort.
    """
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
        # PR B (2026-05-19) — DataPolicy / Commission / Brokerage. Always
        # populated because the request synthesizer guarantees ``data_policy``
        # is non-null by the time we reach response construction. The .NET
        # ``SaveStudyAsync`` endpoint writes these into the new columns.
        "dataPolicyJson": response.data_policy.model_dump_json() if response.data_policy else None,
        "commissionPerOrder": commission_per_order,
        # Python engine doesn't model brokerage — record the LEAN-side
        # convention so the compare-view's soft-match treats it correctly.
        "brokeragePolicy": "algorithm_default",
        # Dollar PnL net of commission, matching LEAN's persisted
        # ``t.pnL`` semantics. The engine charges ``commission_per_order``
        # on both entry and exit fills, so each round-trip incurs
        # ``2 × commission_per_order``. Without this scaling the
        # persisted ``BacktestTrade.PnL`` column silently disagreed with
        # the engine's own ``net_profit`` by a factor of ``quantity``
        # and the per-trade commission. See
        # ``.claude/rules/numerical-rigor.md`` → ``PNL_DRIFT``.
        "trades": [
            {
                "tradeType": "Buy",
                "entryTimestamp": t.entry_time,
                "exitTimestamp": t.exit_time,
                "entryPrice": t.entry_price,
                "exitPrice": t.exit_price,
                "quantity": t.quantity,
                "pnL": t.pnl_pts * t.quantity - 2 * commission_per_order,
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
                payload = resp.json()
                study_id = payload.get("id")
                logger.info("[ENGINE] Study saved (id=%s)", study_id)
                return int(study_id) if study_id is not None else None
            logger.warning("[ENGINE] Study save failed: %s %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.exception("[ENGINE] Study save request failed — study not persisted")
    return None
