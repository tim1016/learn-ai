"""Per-instance execution terms, separate from signal configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExitTermsInput(BaseModel):
    """Explicit operator values for a new deployment or profile default."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    exit_allowance_bps: float = Field(ge=0, lt=10_000, allow_inf_nan=False)
    band_multiple: float = Field(ge=1, le=10, allow_inf_nan=False)
    spread_cap_bps: float = Field(ge=0, lt=10_000, allow_inf_nan=False)

    def seal(self) -> ExitTerms:
        return ExitTerms(**self.model_dump(), provenance="deployed")


class ExitTerms(ExitTermsInput):
    """A historical registration may be explicitly sealed with an unset allowance."""

    exit_allowance_bps: float | None = Field(ge=0, lt=10_000, allow_inf_nan=False)
    provenance: Literal["deployed", "backfilled"]
