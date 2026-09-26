"""Per-instance execution terms, separate from signal configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExitTermsValues(BaseModel):
    """Explicit operator values for a new deployment or profile default."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    band_multiple: float = Field(ge=1, le=10, allow_inf_nan=False)
    spread_cap_bps: float = Field(ge=1, le=1000, allow_inf_nan=False)

class ExitTermsInput(ExitTermsValues):
    """Explicit complete terms required for a new deployment."""

    exit_allowance_bps: float = Field(ge=0, lt=10_000, allow_inf_nan=False)

    def seal(self) -> ExitTerms:
        return ExitTerms(**self.model_dump(), provenance="deployed")


class ExitTerms(ExitTermsValues):
    """Immutable stored terms; an upgraded registration may retain an unset allowance."""

    exit_allowance_bps: float | None = Field(ge=0, lt=10_000, allow_inf_nan=False)
    provenance: Literal["deployed", "backfilled"]
