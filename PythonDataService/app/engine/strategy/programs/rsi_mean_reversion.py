"""The ``rsi_mean_reversion`` Signal Program: its parameters and its wiring.

One file per program (issue #1735), so the program's executable
closure -- the artifact set its qualification receipt hashes --
names the code that wires these parameters to that math, and
nothing else. Held in the registry, an edit here moved no digest.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, model_validator

from app.engine.strategy.algorithms.rsi_mean_reversion import RsiMeanReversionAlgorithm
from app.engine.strategy.params import StrategyParamsBase, decision_timeframe_ms_for
from app.engine.strategy.signal_program import SignalProgram


class RsiMeanReversionParams(StrategyParamsBase):
    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to the ``ge``/``le`` bounds below is a provable identity
    # change without duplicating every bound into the seal itself. Mirrors
    # ``SmaCrossoverParams.PARAMETER_SCHEMA_VERSION``'s pattern.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "rsi-mean-reversion-params/v1"

    symbol: str = Field("SPY", min_length=1, max_length=20)
    window: int = Field(14, ge=2, le=500)
    oversold: float = Field(30.0, gt=0, lt=100)
    overbought: float = Field(70.0, gt=0, lt=100)
    resolution_minutes: int = Field(15, ge=1, le=1440)

    @model_validator(mode="after")
    def _validate_threshold_order(self) -> RsiMeanReversionParams:
        # Mirrors RsiMeanReversionAlgorithm.__init__'s own guard: a
        # schema-valid payload that violates this would otherwise pass
        # admission and persist immutably before crashing at construction.
        if not self.oversold < self.overbought:
            raise ValueError("oversold must be strictly less than overbought")
        return self


RSI_MEAN_REVERSION_SIGNAL_PROGRAM_KEY = "rsi_mean_reversion"
RSI_MEAN_REVERSION_SIGNAL_PROGRAM_VERSION = "rsi-mean-reversion/v1"


def build_rsi_mean_reversion_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral RSI Signal Program from registry params."""
    assert isinstance(params, RsiMeanReversionParams)
    strategy = RsiMeanReversionAlgorithm(
        symbol=params.symbol,
        window=params.window,
        oversold=params.oversold,
        overbought=params.overbought,
        resolution_minutes=params.resolution_minutes,
    )
    program = SignalProgram.create(
        strategy,
        program_key=RSI_MEAN_REVERSION_SIGNAL_PROGRAM_KEY,
        program_version=RSI_MEAN_REVERSION_SIGNAL_PROGRAM_VERSION,
        timeframe_ms=decision_timeframe_ms_for(params, qualified_ms=15 * 60_000),
    )
    strategy.signal_program = program
    return program
