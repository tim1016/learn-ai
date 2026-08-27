"""The ``sma_crossover`` Signal Program: its parameters and its wiring.

One file per program (issue #1735), so the program's executable
closure -- the artifact set its qualification receipt hashes --
names the code that wires these parameters to that math, and
nothing else. Held in the registry, an edit here moved no digest.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, model_validator

from app.engine.strategy.algorithms.sma_crossover import SmaCrossoverAlgorithm
from app.engine.strategy.params import StrategyParamsBase, decision_timeframe_ms_for
from app.engine.strategy.signal_program import SignalProgram


class SmaCrossoverParams(StrategyParamsBase):
    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to the ``ge``/``le`` bounds below is a provable identity
    # change without duplicating every bound into the seal itself. Mirrors
    # ``EmaCrossoverSignalParams.PARAMETER_SCHEMA_VERSION``'s pattern.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "sma-crossover-params/v1"

    symbol: str = Field("SPY", min_length=1, max_length=20)
    short_window: int = Field(10, ge=2, le=500)
    long_window: int = Field(30, ge=3, le=1000)
    resolution_minutes: int = Field(15, ge=1, le=1440)

    @model_validator(mode="after")
    def _validate_window_order(self) -> SmaCrossoverParams:
        # Mirrors SmaCrossoverAlgorithm.__init__'s own guard: a schema-valid
        # payload that violates this would otherwise pass admission and
        # persist immutably before crashing at strategy construction.
        if self.long_window <= self.short_window:
            raise ValueError("long_window must be strictly greater than short_window")
        return self


_SMA_SIGNAL_PROGRAM_KEY = "sma_crossover"
_SMA_SIGNAL_PROGRAM_VERSION = "sma-crossover/v1"


def _build_sma_crossover_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral SMA Signal Program from registry params."""
    typed = params
    assert isinstance(typed, SmaCrossoverParams)
    strategy = SmaCrossoverAlgorithm(
        symbol=typed.symbol,
        short_window=typed.short_window,
        long_window=typed.long_window,
        resolution_minutes=typed.resolution_minutes,
    )
    program = SignalProgram.create(
        strategy,
        program_key=_SMA_SIGNAL_PROGRAM_KEY,
        program_version=_SMA_SIGNAL_PROGRAM_VERSION,
        timeframe_ms=decision_timeframe_ms_for(typed, qualified_ms=15 * 60_000),
    )
    strategy.signal_program = program
    return program
