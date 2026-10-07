"""A resolved option structure: the contracts an entry's rules picked, leg by leg.

An Action Plan's option legs are rules ("the call nearest 0.30 delta"). A
resolved structure is what those rules came to at one entry: one contract per
leg, bought or sold, so many per structure. The exit closes these contracts
and never asks the rules again (ADR 0012 §3).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.options.contract import OptionContract

LegPosition = Literal["long", "short"]

# The most option legs one order carries at the broker.
MAX_OPTION_LEGS = 4


class ResolvedLeg(BaseModel):
    """One leg of a resolved structure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The Action Plan leg this contract was picked for.
    leg_id: Annotated[str, Field(min_length=1)]
    contract: OptionContract
    # ``long`` is bought to open, ``short`` is sold to open.
    position: LegPosition
    # Contracts per structure.
    ratio: Annotated[int, Field(ge=1)]


class ResolvedStructure(BaseModel):
    """Every leg of one structure, all on one underlying."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    legs: Annotated[tuple[ResolvedLeg, ...], Field(min_length=1, max_length=MAX_OPTION_LEGS)]

    @model_validator(mode="after")
    def _one_underlying_and_distinct_legs(self) -> ResolvedStructure:
        leg_ids = [leg.leg_id for leg in self.legs]
        if len(set(leg_ids)) != len(leg_ids):
            raise ValueError(f"a structure names each leg once; these repeat: {sorted(leg_ids)}")
        symbols = [leg.contract.occ_symbol for leg in self.legs]
        if len(set(symbols)) != len(symbols):
            # Two legs on one contract are one leg at another ratio, or nothing; one order carries a contract once.
            raise ValueError(f"a structure holds each contract in one leg; these repeat: {sorted(symbols)}")
        roots = sorted({leg.contract.root for leg in self.legs})
        if len(roots) != 1:
            # Its money is worked out against one underlying price.
            raise ValueError(f"every leg of a structure is on one underlying; these are on {roots}")
        return self

    @property
    def has_one_expiry(self) -> bool:
        return len({leg.contract.expiry_ms for leg in self.legs}) == 1

    @property
    def has_sold_leg(self) -> bool:
        return any(leg.position == "short" for leg in self.legs)
