"""The ``spy_strategy_c`` Signal Program: its parameters and its wiring.

One file per program (issue #1735), so the program's executable
closure -- the artifact set its qualification receipt hashes --
names the code that wires these parameters to that math, and
nothing else. Held in the registry, an edit here moved no digest.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, model_validator

from app.engine.strategy.algorithms.spy_strategy_c import SpyStrategyCAlgorithm
from app.engine.strategy.params import StrategyParamsBase, decision_timeframe_ms_for
from app.engine.strategy.signal_program import SignalProgram


class RsiRangeStrategyCParams(StrategyParamsBase):
    """Strategy C — ADX-entry + ADX-rising + RSI-range, ADX-exit.

    No MACD, no Supertrend — the simplest of the three. Entry requires
    RSI inside the range filter, ADX above a threshold, AND ADX rising
    bar-over-bar.
    """

    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to the ``ge``/``le`` bounds below is a provable identity
    # change without duplicating every bound into the seal itself. Mirrors
    # ``SmaCrossoverParams.PARAMETER_SCHEMA_VERSION``'s pattern.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "spy-strategy-c-params/v1"

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

    @model_validator(mode="after")
    def _validate_rsi_gate_order(self) -> RsiRangeStrategyCParams:
        if self.rsi_low_gate >= self.rsi_high_gate:
            raise ValueError("rsi_low_gate must be strictly less than rsi_high_gate")
        return self


_SPY_C_SIGNAL_PROGRAM_KEY = "spy_strategy_c"
_SPY_C_SIGNAL_PROGRAM_VERSION = "spy-strategy-c/v1"


def _build_spy_strategy_c_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral Strategy C Signal Program from registry params."""
    typed = params
    assert isinstance(typed, RsiRangeStrategyCParams)
    strategy = SpyStrategyCAlgorithm(
        symbol=typed.symbol,
        adx_entry_threshold=typed.adx_entry_threshold,
        rsi_period=typed.rsi_period,
        rsi_low_gate=typed.rsi_low_gate,
        rsi_high_gate=typed.rsi_high_gate,
        adx_period=typed.adx_period,
        adx_exit_threshold=typed.adx_exit_threshold,
        resolution_minutes=typed.resolution_minutes,
    )
    program = SignalProgram.create(
        strategy,
        program_key=_SPY_C_SIGNAL_PROGRAM_KEY,
        program_version=_SPY_C_SIGNAL_PROGRAM_VERSION,
        timeframe_ms=decision_timeframe_ms_for(typed, qualified_ms=15 * 60_000),
    )
    strategy.signal_program = program
    return program
