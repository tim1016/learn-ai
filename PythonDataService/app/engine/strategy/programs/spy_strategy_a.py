"""The ``spy_strategy_a`` Signal Program: its parameters and its wiring.

One file per program (issue #1735), so the program's executable
closure -- the artifact set its qualification receipt hashes --
names the code that wires these parameters to that math, and
nothing else. Held in the registry, an edit here moved no digest.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, model_validator

from app.engine.strategy.algorithms.spy_strategy_a import SpyStrategyAAlgorithm
from app.engine.strategy.params import StrategyParamsBase, decision_timeframe_ms_for
from app.engine.strategy.signal_program import SignalProgram


class RsiRangeStrategyAParams(StrategyParamsBase):
    """Strategy A — EMA-gap + MACD + RSI-range, ADX-exit.

    All thresholds and indicator periods are configurable. Entry requires
    RSI to sit inside the ``[rsi_low_gate, rsi_high_gate]`` range AND the
    EMA gap to exceed ``ema_gap_threshold`` AND MACD line > 0, all at the
    same bar while flat. Pyramiding=1 prevents re-entry while holding.
    """

    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to the ``ge``/``le`` bounds below is a provable identity
    # change without duplicating every bound into the seal itself. Mirrors
    # ``SmaCrossoverParams.PARAMETER_SCHEMA_VERSION``'s pattern.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "rsi-range-strategy-a-params/v1"

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

    @model_validator(mode="after")
    def _validate_rsi_gate_order(self) -> RsiRangeStrategyAParams:
        # Mirrors RsiRangeStrategy.__init__'s own guard (the shared A/B/C
        # base class): a schema-valid payload that violates this would
        # otherwise pass admission and persist immutably before crashing
        # at construction.
        if self.rsi_low_gate >= self.rsi_high_gate:
            raise ValueError("rsi_low_gate must be strictly less than rsi_high_gate")
        return self


_SPY_STRATEGY_A_SIGNAL_PROGRAM_KEY = "spy_strategy_a"
_SPY_STRATEGY_A_SIGNAL_PROGRAM_VERSION = "spy-strategy-a/v1"


def _build_spy_strategy_a_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral Strategy A Signal Program from registry params."""
    typed = params
    assert isinstance(typed, RsiRangeStrategyAParams)
    strategy = SpyStrategyAAlgorithm(
        symbol=typed.symbol,
        ema_fast_period=typed.ema_fast_period,
        ema_slow_period=typed.ema_slow_period,
        ema_gap_threshold=typed.ema_gap_threshold,
        macd_fast=typed.macd_fast,
        macd_slow=typed.macd_slow,
        macd_signal=typed.macd_signal,
        rsi_period=typed.rsi_period,
        rsi_low_gate=typed.rsi_low_gate,
        rsi_high_gate=typed.rsi_high_gate,
        adx_period=typed.adx_period,
        adx_exit_threshold=typed.adx_exit_threshold,
        resolution_minutes=typed.resolution_minutes,
    )
    program = SignalProgram.create(
        strategy,
        program_key=_SPY_STRATEGY_A_SIGNAL_PROGRAM_KEY,
        program_version=_SPY_STRATEGY_A_SIGNAL_PROGRAM_VERSION,
        timeframe_ms=decision_timeframe_ms_for(typed, qualified_ms=15 * 60_000),
    )
    strategy.signal_program = program
    return program
