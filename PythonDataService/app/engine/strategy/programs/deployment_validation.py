"""The ``deployment_validation`` Signal Program: its parameters and its wiring.

One file per program (issue #1735), so the program's executable
closure -- the artifact set its qualification receipt hashes --
names the code that wires these parameters to that math, and
nothing else. Held in the registry, an edit here moved no digest.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from app.engine.strategy.algorithms.deployment_validation import DeploymentValidationConsecutiveGreen
from app.engine.strategy.params import StrategyParamsBase
from app.engine.strategy.signal_program import SignalProgram


class DeploymentValidationParams(StrategyParamsBase):
    """Deployment-validation strategy: its symbol is its only parameter."""

    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to this schema's fields is a provable identity change
    # without duplicating every bound into the seal itself. Mirrors
    # ``SmaCrossoverParams.PARAMETER_SCHEMA_VERSION``'s pattern.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "deployment-validation-params/v2"

    symbol: str = Field("SPY", min_length=1, max_length=20)


DEPLOYMENT_VALIDATION_SIGNAL_PROGRAM_KEY = "deployment_validation"
DEPLOYMENT_VALIDATION_SIGNAL_PROGRAM_VERSION = "deployment-validation/v1"


def build_deployment_validation_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral Deployment Validation Signal Program."""
    assert isinstance(params, DeploymentValidationParams)
    strategy = DeploymentValidationConsecutiveGreen(symbol=params.symbol)
    program = SignalProgram.create(
        strategy,
        program_key=DEPLOYMENT_VALIDATION_SIGNAL_PROGRAM_KEY,
        program_version=DEPLOYMENT_VALIDATION_SIGNAL_PROGRAM_VERSION,
        # This program's decision clock IS the raw minute bar -- there is no
        # consolidator/resolution parameter to derive it from (unlike
        # sma_crossover's own configurable timeframe_ms above).
        timeframe_ms=60_000,
    )
    strategy.signal_program = program
    return program
