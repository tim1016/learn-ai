"""The ``deployment_validation`` Signal Program: its parameters and its wiring.

One file per program (issue #1735), so the program's executable
closure -- the artifact set its qualification receipt hashes --
names the code that wires these parameters to that math, and
nothing else. Held in the registry, an edit here moved no digest.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, model_validator

from app.engine.strategy.algorithms.deployment_validation import DeploymentValidationConsecutiveGreen
from app.engine.strategy.params import StrategyParamsBase
from app.engine.strategy.signal_program import SignalProgram


class DeploymentValidationParams(StrategyParamsBase):
    """Deployment-validation strategy with configurable signal/trade tickers."""

    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to this schema's fields is a provable identity change
    # without duplicating every bound into the seal itself. Mirrors
    # ``SmaCrossoverParams.PARAMETER_SCHEMA_VERSION``'s pattern.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "deployment-validation-params/v1"

    symbol: str = Field("SPY", min_length=1, max_length=20)
    trade_symbol: str | None = Field(None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def _default_trade_symbol_to_signal_symbol(self) -> DeploymentValidationParams:
        # Mirrors DeploymentValidationConsecutiveGreen.__init__'s own
        # ``(trade_symbol or symbol)`` fallback. Signal Program admission
        # (app/services/signal_program_admission.py::build_start_program_seal)
        # resolves and seals every field of this model, including a hidden
        # one left at its default -- ResolvedSignalParameter.value has no
        # legal ``None`` variant (str | int | float | bool only), so an
        # unresolved ``None`` here would crash seal construction with a
        # Pydantic ValidationError the instant this became a registered
        # Signal Program. Resolving the real fallback value here, once,
        # keeps the sealed identity honest instead of sealing a sentinel.
        if self.trade_symbol is None:
            self.trade_symbol = self.symbol
        return self


_DEPLOYMENT_VALIDATION_SIGNAL_PROGRAM_KEY = "deployment_validation"
_DEPLOYMENT_VALIDATION_SIGNAL_PROGRAM_VERSION = "deployment-validation/v1"


def _build_deployment_validation_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral Deployment Validation Signal Program."""
    typed = params
    assert isinstance(typed, DeploymentValidationParams)
    strategy = DeploymentValidationConsecutiveGreen(
        symbol=typed.symbol,
        trade_symbol=typed.trade_symbol,
    )
    program = SignalProgram.create(
        strategy,
        program_key=_DEPLOYMENT_VALIDATION_SIGNAL_PROGRAM_KEY,
        program_version=_DEPLOYMENT_VALIDATION_SIGNAL_PROGRAM_VERSION,
        # This program's decision clock IS the raw minute bar -- there is no
        # consolidator/resolution parameter to derive it from (unlike
        # sma_crossover's own configurable timeframe_ms above).
        timeframe_ms=60_000,
    )
    strategy.signal_program = program
    return program
