"""HTTP contract for the strategy registry's own account of itself (#3508).

One read lists every registered strategy, the ones the Strategy Lab picker
hides included, with what the registry allows for it and why not when it does
not. It carries no proof, approval or running-bot state: those have their own
reads, and a registered strategy is never thereby deployable or approved.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.strategy_validation import StrategyCategory


class CapabilityAnswer(BaseModel):
    """Whether the registry allows one action for a strategy; ``reason`` says why not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    supported: bool
    reason: str | None = Field(default=None, description="Set exactly when the action is not supported.")

    @model_validator(mode="after")
    def _reason_exactly_when_unsupported(self) -> CapabilityAnswer:
        if self.supported == (self.reason is not None):
            raise ValueError("an unsupported action carries its reason, and a supported one carries none")
        return self


class StrategyCapabilities(BaseModel):
    """What the registry allows. Allowed is not approved: proof, qualification and admission are judged elsewhere."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sweep: CapabilityAnswer = Field(description="Sweep, Walk-forward and Recency Chart, which share one eligibility rule.")
    golden: CapabilityAnswer = Field(description="A Golden Search study.")
    live: CapabilityAnswer = Field(description="A bot of this strategy on a Live account.")


class StrategyRegistryEntry(BaseModel):
    """One registered strategy, as its registry entry declares it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_key: str
    display_name: str
    #: The entry's own prose. Empty when the entry has none; a reader says so rather than writing one.
    description: str
    strategy_category: StrategyCategory
    experimental_notice: str | None
    #: ``False`` for a compatibility registration the Strategy Lab picker does not offer.
    catalog_visible: bool
    #: ``None`` for a strategy with no governed Signal Program.
    program_version: str | None
    #: The program's own statement of its rules (its numerical provenance formula); ``None`` without a program.
    rule_statement: str | None
    #: The corpus point the program was validated at. Empty when the program declares none.
    validated_settings: dict[str, str | int | float | bool]
    validated_symbols: list[str]
    #: Each parameter's unit as the program declares it.
    parameter_units: dict[str, str]
    capabilities: StrategyCapabilities
