"""The ``ema_previous_crossover`` Signal Program: its parameters and its wiring.

One file per program (issue #1735), so the program's executable
closure -- the artifact set its qualification receipt hashes --
names the code that wires these parameters to that math, and
nothing else.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from app.engine.strategy.algorithms.ema_previous_crossover import (
    DEFAULT_CROSS_MAX_AGE_BARS,
    DEFAULT_FAST_PERIOD,
    DEFAULT_HOLD_BARS,
    DEFAULT_SLOW_PERIOD,
    EmaPreviousCrossoverAlgorithm,
)
from app.engine.strategy.params import EmaCrossoverParams, StrategyParamsBase
from app.engine.strategy.signal_program import SignalProgram


class EmaPreviousCrossoverParams(EmaCrossoverParams):
    """EMA previous-crossover gates, lengths and cross age limit.

    Every field is dumped always: this program has no earlier identities for a
    default to stay neutral against.
    """

    # Versions this schema's own legal type/unit/range contract; sealed as
    # ``ConfiguredSignalProgramSeal.parameter_schema_version``.
    # v2 (#2902, revised 2026-10-05): adds the cross age limit.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "ema-previous-crossover-params/v2"

    gap: float = Field(
        0.20,
        ge=0.0,
        allow_inf_nan=False,
        title="Crossover gap",
        description="Minimum fast EMA − slow EMA gap, in absolute price, required on the entry bar. 0 imposes no absolute floor.",
    )
    gap_bps: float = Field(
        0.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="Crossover gap (bps)",
        description="Minimum 10,000 × (fast EMA − slow EMA) / slow EMA, in basis points, required on the entry bar. 0 imposes no normalized floor; both this and `gap` are minimums and both apply.",
    )
    rsi_min: float = Field(
        50.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="RSI lower gate",
        description="Inclusive lower RSI(14) value allowed on the entry bar.",
    )
    rsi_max: float = Field(
        70.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="RSI upper gate",
        description="Inclusive upper RSI(14) value allowed on the entry bar.",
    )
    fast_period: int = Field(
        DEFAULT_FAST_PERIOD,
        ge=2,
        le=30,
        title="Fast EMA length",
        description="EMA length, in 15-minute decision bars, of the fast line.",
    )
    slow_period: int = Field(
        DEFAULT_SLOW_PERIOD,
        ge=3,
        le=40,
        title="Slow EMA length",
        description="EMA length, in 15-minute decision bars, of the slow line. Must be longer than the fast line.",
    )
    hold_bars: int = Field(
        DEFAULT_HOLD_BARS,
        ge=1,
        le=26,
        title="Hold time",
        description="Decision bars (15-minute) a position is held after entry; counts only session decision bars, never wall-clock time.",
    )
    cross_max_age_bars: int = Field(
        DEFAULT_CROSS_MAX_AGE_BARS,
        ge=0,
        le=8,
        title="Cross age limit",
        description="Longest time since the cross allowed on the entry bar, in 15-minute bars of clock time. 0 allows the cross bar only; 1 also allows a bar that closes 15 minutes after it. A cross from an earlier session never counts.",
    )

    @field_validator("fast_period", "slow_period", "hold_bars", "cross_max_age_bars", mode="before")
    @classmethod
    def _refuse_boolean_lengths(cls, value: object) -> object:
        # Lax int parsing turns ``true`` into 1 -- a one-bar hold nobody chose.
        if isinstance(value, bool):
            raise ValueError("must be an integer, not a boolean")
        return value

    @model_validator(mode="after")
    def _validate_rsi_band(self) -> EmaPreviousCrossoverParams:
        if self.rsi_min >= self.rsi_max:
            raise ValueError("rsi_min must be less than rsi_max")
        return self

    @model_validator(mode="after")
    def _validate_ema_lengths(self) -> EmaPreviousCrossoverParams:
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be less than slow_period")
        return self


EMA_PREVIOUS_CROSSOVER_PROGRAM_KEY = "ema_previous_crossover"
EMA_PREVIOUS_CROSSOVER_PROGRAM_VERSION = "ema-previous-crossover/v2"


def build_ema_previous_crossover_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral EMA previous-crossover Signal Program from registry params."""
    assert isinstance(params, EmaPreviousCrossoverParams)
    strategy = EmaPreviousCrossoverAlgorithm(
        symbol=params.symbol,
        gap=params.gap,
        rsi_min=params.rsi_min,
        rsi_max=params.rsi_max,
        gap_bps=params.gap_bps,
        fast_period=params.fast_period,
        slow_period=params.slow_period,
        hold_bars=params.hold_bars,
        cross_max_age_bars=params.cross_max_age_bars,
    )
    program = SignalProgram.create(
        strategy,
        program_key=EMA_PREVIOUS_CROSSOVER_PROGRAM_KEY,
        program_version=EMA_PREVIOUS_CROSSOVER_PROGRAM_VERSION,
        # Fixed cadence: this program exposes no resolution parameter, and
        # its registration declares StrategyBarCadence("minute", 15).
        timeframe_ms=15 * 60_000,
    )
    strategy.signal_program = program
    return program
