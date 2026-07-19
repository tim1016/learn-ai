"""Engine strategy registry.

The registry is engine-level authority: routers render it, live deploy gates
read it, and the runner uses it to resolve the algorithm class. Keeping it out
of ``app.routers.engine`` prevents live runtime code from importing private
router symbols just to answer deploy/start safety questions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from typing import Literal

from pydantic import BaseModel, Field

from app.engine.pine_generators import (
    generate_strategy_a_pine,
    generate_strategy_b_pine,
    generate_strategy_c_pine,
)
from app.engine.strategy.algorithms.deployment_validation import (
    DeploymentValidationConsecutiveGreen,
)
from app.engine.strategy.algorithms.ema_crossover_signal import (
    EmaCrossoverSignalAlgorithm,
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


class StrategyParamsBase(BaseModel):
    """Base for every strategy's parameter model.

    Subclasses declare the strategy's own fields. A strategy with no
    parameters can simply reuse this class directly.
    """

    model_config = {"extra": "forbid"}


class EmaCrossoverParams(StrategyParamsBase):
    """EMA crossover signal parameters.

    Shares the exact indicator / gap / RSI logic as the LEAN-parity SPY
    reference run, but lets the user pick the *signal stream* at request time.
    Defaults to SPY so the out-of-the-box run matches the bit-exact
    reference fixture; other symbols (QQQ, IWM, etc.) can be substituted
    without touching the strategy. A live Action Plan independently selects
    the stock to trade; Engine Lab backtests bind the one loaded price stream
    to both roles.
    """

    symbol: str = Field(
        "SPY",
        min_length=1,
        max_length=20,
        description="Signal-stream ticker. The live Action Plan selects the traded stock separately.",
    )


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
    """Deployment-validation strategy with configurable signal/trade tickers."""

    symbol: str = Field("SPY", min_length=1, max_length=20)
    trade_symbol: str | None = Field(None, min_length=1, max_length=20)


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
    # ``"policy"`` (default) — the execution boundary targets via
    # ``set_holdings``;
    # the deploy-form sizing selector is enabled, and
    # ``live_config.sizing`` ∈ {FixedShares, FixedNotional, SetHoldings}
    # governs the magnitude.
    # ``"explicit"`` — the strategy supplies its own quantity/contracts
    # (``market_order``, ``contracts_per_trade``, internal accounting); the
    # deploy-form sizing selector is disabled + labelled "self-sized" and
    # the required ``live_config.sizing`` is ``StrategyExplicit``.
    sizing_surface: Literal["policy", "explicit"] = "policy"
    # Fields accepted only by non-Engine-Lab construction paths. They remain in
    # ``param_schema`` so the live runner can validate its internal injection,
    # but are hidden from ``GET /strategies`` and rejected by normal backtests.
    hidden_params: set[str] = dc_field(default_factory=set)
    # ADR 0012 / PRD #593 Slice 1A — which boundary chooses the
    # *instrument* this strategy trades. ``"explicit"`` means the strategy code
    # itself names the instrument (e.g. ``set_holdings("SPY", 1.0)``),
    # and ``live_config.action`` is informational only. ``"policy"``
    # is active for strategies that emit a generic ``SignalIntent`` and let the
    # deploy-time Action Plan resolve concrete instruments. The deploy boundary
    # stores this field in the run ledger and rejects unsupported Action Plans
    # for policy strategies.
    instrument_surface: Literal["policy", "explicit"] = "explicit"
    # The live Action Plan shape this strategy requires to start. This is
    # intentionally independent from ``instrument_surface``: a legacy explicit
    # strategy may still receive its trade symbol from a stock Action Plan.
    # ``"single_long_stock"`` means exactly one long stock entry and a
    # matching close-leg exit; ``"none"`` means no deploy/start requirement.
    action_plan_contract: Literal["none", "single_long_stock"] = "none"
    # Compatibility registrations remain runnable for existing ledgers but
    # must not appear as duplicate choices in the Engine Lab strategy picker.
    catalog_visible: bool = True
    # Engine Lab parity — the LEAN trusted-sample template that implements
    # the same rules as this strategy, if one exists. When set, every raw
    # minute-resolution Python run auto-spawns a LEAN validating companion
    # sharing a parity_group_id (see app/services/parity_companion.py).
    # None → honest "parity unavailable — no LEAN counterpart".
    lean_twin: str | None = None


_STRATEGY_REGISTRY: dict[str, StrategyRegistration] = {
    "ema_crossover_signal": StrategyRegistration(
        display_name="EMA Crossover Signal",
        class_name="EmaCrossoverSignalAlgorithm",
        description=(
            "Long-only intraday EMA signal generator. Bit-exact against the "
            "LEAN C# reference when the default SPY signal stream is used; "
            "any cached ticker (or Polygon-fetched ticker) can supply signals "
            "without changing the strategy.\n"
            "\n"
            "The strategy reads minute signal bars, consolidates them into "
            "15-minute bars, tracks EMA(5), EMA(10) and Wilders RSI(14), "
            "and goes long for exactly five 15-minute bars (75 min) every "
            "time a fresh fast-over-slow crossover lines up with a 0.20 "
            "minimum gap and an RSI in the 50–70 trend-confirmation band. "
            "There is no stop, no target, no scaling, and at most one "
            "position lifecycle open at a time. In live runs, the Action Plan "
            "selects the stock to trade; the strategy does not."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    signal_symbol = configurable (default SPY)\n"
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
            "    ⇒ if all three: emit Insight(UP, period=5 bars); emit ENTER\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    bars_held += 1 each new bar\n"
            "    ⇒ when bars_held == 5: emit EXIT\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    none — no stop-loss, no take-profit, no signal-flip exit.\n"
            "    A losing trade is held to the time-stop. A reversed crossover\n"
            "    inside the 5-bar window is ignored.\n"
            "\n"
            "Action Plan / Portfolio Construction\n"
            "    live execution selects the stock from its Action Plan\n"
            "    Engine Lab binds intents to its single signal stream\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close → fills at the signal bar's close\n"
            "                                   (matches LEAN parity fixture)\n"
            "              | next_bar_open    → fills at the next bar's open\n"
            "                                   (matches TradingView trade-list)\n"
            "\n"
            "End of algorithm\n"
            "    if still in position at the last bar: emit EXIT"
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
            "The Action Plan may trade a different stock from the signal "
            "stream. That stock selection and sizing are execution concerns; "
            "this strategy emits only ENTER and EXIT intents.",
            "TV labels trades by bar START, Engine Lab by bar END — a fixed "
            "15-minute label offset that is cosmetic, not a fill-time bug.",
        ],
        param_schema=EmaCrossoverParams,
        build=lambda p: EmaCrossoverSignalAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
        ),
        instrument_surface="policy",
        action_plan_contract="single_long_stock",
        lean_twin="ema_crossover_signal",
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
            "bars (one daily zip per symbol). Defaults "
            "to the classic 50/200 on AAPL. Same SmaCrossoverAlgorithm as "
            "the intraday variant - only the bar cadence differs."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default AAPL)\n"
            "    resolution = daily (read from the LEAN daily equity zip for the symbol)\n"
            "\n"
            "Indicators (Alpha - updated each daily bar close)\n"
            "    SMA_short = SimpleMovingAverage(short_window days, default 50)\n"
            "    SMA_long  = SimpleMovingAverage(long_window  days, default 200)\n"
            "\n"
            "Warmup\n"
            "    no signals until both SMAs have window-size samples\n"
            "    (typically ~200 trading days for the 50/200 default).\n"
            "\n"
            "Alpha - bar entry conditions (evaluated while flat)\n"
            "    golden_cross = SMA_short > SMA_long\n"
            "                   AND SMA_short[-1] <= SMA_long[-1]\n"
            "    ⇒ if golden_cross: emit Insight(UP); enter long\n"
            "\n"
            "Alpha - bar exit conditions (evaluated while in trade)\n"
            "    death_cross = SMA_short < SMA_long\n"
            "    ⇒ if death_cross: Liquidate(symbol)\n"
            "\n"
            "Risk Management - position survival rules\n"
            "    none - overnight gap risk is not modeled by the fill model;\n"
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
            "one-per-day from the LEAN daily equity zip - make sure "
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
            "    signal symbol = configurable (default SPY)\n"
            "    trade symbol  = configurable (defaults to signal symbol)\n"
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
        lean_twin="deployment_validation",
        param_schema=DeploymentValidationParams,
        hidden_params={"trade_symbol"},
        action_plan_contract="single_long_stock",
        build=lambda p: DeploymentValidationConsecutiveGreen(
            symbol=p.symbol,  # type: ignore[attr-defined]
            trade_symbol=p.trade_symbol,  # type: ignore[attr-defined]
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

# Historical ledgers use this key and import path. Keep it executable but do
# not offer it as a second picker option beside the migrated signal strategy.
_STRATEGY_REGISTRY["spy_ema_crossover"] = replace(
    _STRATEGY_REGISTRY["ema_crossover_signal"],
    display_name="EMA Crossover (legacy compatibility)",
    class_name="SpyEmaCrossoverAlgorithm",
    build=lambda p: SpyEmaCrossoverAlgorithm(symbol=p.symbol),  # type: ignore[attr-defined]
    instrument_surface="explicit",
    action_plan_contract="none",
    catalog_visible=False,
    lean_twin="ema_crossover",
)



__all__ = [
    "_STRATEGY_REGISTRY",
    "StrategyParamsBase",
    "StrategyRegistration",
]
