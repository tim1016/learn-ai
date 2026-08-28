"""The ``ema_crossover_signal`` Signal Program: its parameters and its wiring.

One file per program (issue #1735), so the program's executable
closure -- the artifact set its qualification receipt hashes --
names the code that wires these parameters to that math, and
nothing else. Held in the registry, an edit here moved no digest.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field, model_validator

from app.engine.strategy.algorithms.ema_crossover_signal import EmaCrossoverSignalAlgorithm
from app.engine.strategy.params import EmaCrossoverParams, StrategyParamsBase
from app.engine.strategy.signal_program import SignalProgram


class EmaCrossoverSignalParams(EmaCrossoverParams):
    """EMA-crossover *signal* strategy gates, exposed as parameters.

    Defaults preserve the validated LEAN-parity point exactly (absolute gap
    0.20, RSI band 50–70); the Recency Chart sweeps them. The former
    ``ema_crossover_2_bps`` strategy is now this schema with
    ``gap_mode='bps', gap_bps=2`` — see the ENG-007 golden fixture.
    """

    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to the ``ge``/``le`` bounds below is a provable identity
    # change without duplicating every bound into the seal itself. A
    # ``ClassVar`` is invisible to Pydantic's field machinery, so it never
    # becomes part of the JSON schema or a constructor argument.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "ema-crossover-signal-params/v2"

    gap: float = Field(
        0.20,
        ge=0.0,
        allow_inf_nan=False,
        title="Crossover gap",
        description="Minimum EMA(5) − EMA(10) gap, in absolute price, required for entry. Consulted only when gap_mode is 'absolute'.",
    )
    gap_mode: Literal["absolute", "bps"] = Field(
        "absolute",
        title="Crossover gap unit",
        description=(
            "Unit the entry gap is measured in: 'absolute' price (the validated "
            "LEAN-parity default) or 'bps', the same spread normalized against "
            "EMA(10) so the gate scales with price level."
        ),
    )
    gap_bps: float = Field(
        2.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="Crossover gap (bps)",
        description="Minimum 10,000 × (EMA(5) − EMA(10)) / EMA(10), in basis points, required for entry. Consulted only when gap_mode is 'bps'.",
    )
    rsi_min: float = Field(
        50.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="RSI lower gate",
        description="Inclusive lower RSI(14) value allowed for entry.",
    )
    rsi_max: float = Field(
        70.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="RSI upper gate",
        description="Inclusive upper RSI(14) value allowed for entry.",
    )

    @model_validator(mode="after")
    def _validate_rsi_band(self) -> EmaCrossoverSignalParams:
        if self.rsi_min >= self.rsi_max:
            raise ValueError("rsi_min must be less than rsi_max")
        return self


EMA_SIGNAL_PROGRAM_KEY = "ema_crossover_signal"
EMA_SIGNAL_PROGRAM_VERSION = "ema-crossover-signal/v1"


def build_ema_crossover_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral EMA Signal Program from registry params."""
    assert isinstance(params, EmaCrossoverSignalParams)
    strategy = EmaCrossoverSignalAlgorithm(
        symbol=params.symbol,
        gap=params.gap,
        rsi_min=params.rsi_min,
        rsi_max=params.rsi_max,
        gap_mode=params.gap_mode,
        gap_bps=params.gap_bps,
    )
    program = SignalProgram.create(
        strategy,
        program_key=EMA_SIGNAL_PROGRAM_KEY,
        program_version=EMA_SIGNAL_PROGRAM_VERSION,
        # Fixed cadence: this program exposes no resolution parameter, and
        # its registration declares StrategyBarCadence("minute", 15).
        timeframe_ms=15 * 60_000,
    )
    strategy.signal_program = program
    return program
