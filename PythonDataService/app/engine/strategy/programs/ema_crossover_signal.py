"""The ``ema_crossover_signal`` Signal Program: its parameters and its wiring.

One file per program (issue #1735), so the program's executable
closure -- the artifact set its qualification receipt hashes --
names the code that wires these parameters to that math, and
nothing else. Held in the registry, an edit here moved no digest.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field, SerializerFunctionWrapHandler, field_validator, model_serializer, model_validator

from app.engine.strategy.algorithms.ema_crossover_signal import (
    DEFAULT_FAST_PERIOD,
    DEFAULT_HOLD_BARS,
    DEFAULT_SLOW_PERIOD,
    EmaCrossoverSignalAlgorithm,
)
from app.engine.strategy.params import EmaCrossoverParams, StrategyParamsBase
from app.engine.strategy.signal_program import SignalProgram

# The lengths a dump leaves out while they sit at their defaults; see
# ``EmaCrossoverSignalParams._omit_identity_neutral_defaults``.
_IDENTITY_NEUTRAL_FIELDS: tuple[str, ...] = ("fast_period", "slow_period", "hold_bars")


class EmaCrossoverSignalParams(EmaCrossoverParams):
    """EMA-crossover *signal* strategy gates and lengths, exposed as parameters.

    Defaults preserve the validated LEAN-parity point exactly (absolute gap
    0.20, RSI band 50–70, EMA 5/10, a five-bar hold); the Recency Chart and
    Golden Search sweep them. The former ``ema_crossover_2_bps`` strategy is
    now this schema with ``gap=0, gap_bps=2`` — see the ENG-007 golden
    fixture.
    """

    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to the ``ge``/``le`` bounds below is a provable identity
    # change without duplicating every bound into the seal itself. A
    # ``ClassVar`` is invisible to Pydantic's field machinery, so it never
    # becomes part of the JSON schema or a constructor argument.
    # v3 (#2696): adds the fast/slow EMA lengths and the hold. A parameter
    # set at the reference lengths dumps exactly as a v2 set did, so its seal
    # keeps v2's version: see ``at_reference_lengths``.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "ema-crossover-signal-params/v3"
    REFERENCE_PARAMETER_SCHEMA_VERSION: ClassVar[str] = "ema-crossover-signal-params/v2"

    gap: float = Field(
        0.20,
        ge=0.0,
        allow_inf_nan=False,
        title="Crossover gap",
        description="Minimum fast EMA − slow EMA gap, in absolute price, required for entry. 0 imposes no absolute floor.",
    )
    gap_bps: float = Field(
        0.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="Crossover gap (bps)",
        description="Minimum 10,000 × (fast EMA − slow EMA) / slow EMA, in basis points, required for entry. 0 imposes no normalized floor; both this and `gap` are minimums and both apply.",
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

    @field_validator(*_IDENTITY_NEUTRAL_FIELDS, mode="before")
    @classmethod
    def _refuse_boolean_lengths(cls, value: object) -> object:
        # Lax int parsing turns ``true`` into 1 -- a one-bar hold nobody chose.
        if isinstance(value, bool):
            raise ValueError("must be an integer, not a boolean")
        return value

    @model_validator(mode="after")
    def _validate_rsi_band(self) -> EmaCrossoverSignalParams:
        if self.rsi_min >= self.rsi_max:
            raise ValueError("rsi_min must be less than rsi_max")
        return self

    @model_validator(mode="after")
    def _validate_ema_lengths(self) -> EmaCrossoverSignalParams:
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be less than slow_period")
        return self

    @model_serializer(mode="wrap")
    def _omit_identity_neutral_defaults(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Leave each EMA length and the hold out of every dump while it is at its default.

        Every identity minted before these became parameters (#2696) was
        computed from a dump without them: Golden Validation case parameters
        matched as exact JSONB, seal parameter maps, budget review tokens,
        evaluation settings, and the trace corpus settings. Omitting a value
        that equals its default keeps all of those byte-identical at the 5/10/5
        point, while a non-default value is always present -- so the dump stays
        one canonical form per configuration and ``model_validate`` of it
        rebuilds the same model. ``model_json_schema()`` still declares all
        three properties with their defaults.
        """
        dumped = handler(self)
        fields = type(self).model_fields
        for name in _IDENTITY_NEUTRAL_FIELDS:
            if getattr(self, name) == fields[name].default:
                dumped.pop(name, None)
        return dumped

    def at_reference_lengths(self) -> bool:
        """Whether the EMA lengths and hold are the LEAN reference's 5/10/5, where every identity predates them."""
        fields = type(self).model_fields
        return all(getattr(self, name) == fields[name].default for name in _IDENTITY_NEUTRAL_FIELDS)


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
        gap_bps=params.gap_bps,
        fast_period=params.fast_period,
        slow_period=params.slow_period,
        hold_bars=params.hold_bars,
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
