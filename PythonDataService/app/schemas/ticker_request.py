"""Canonical request schemas for ticker-bar endpoints.

Every route whose primary input is "bars for symbol X over date range
[from_date, to_date] at (timespan × multiplier) granularity" inherits
``TickerRequest``. Routes for a *universe* of symbols inherit
``MultiTickerRequest``. The route's date+sampling+session block lives
in ``_BarRange``, which both bases extend.

`extra="forbid"` is required: Pydantic v2's default `extra="ignore"`
silently drops unknown fields, which would hide the rename bug after
PR (iii) removes the transitional aliases.

Transitional aliases — to be REMOVED in PR (iii):
    ticker     → symbol
    tickers    → symbols
    start_date → from_date
    end_date   → to_date

These aliases let PR (ii) ship before PR (iii)'s frontend payload
renames, so the merge order has tolerance. Once PR (iii) lands and
every consumer sends canonical names, the aliases are removed and
legacy names produce a clear ``extra_forbidden`` 422.

Per-route default preservation: routes whose pre-migration default for
``multiplier`` / ``timespan`` / ``session`` differs from this base
**must override the inherited field explicitly** to preserve current
behavior. See e.g. ``SignalEngineJobRequest`` (multiplier=15) or
``IndicatorTableRequest`` (session="extended").
"""

from __future__ import annotations

from datetime import date as Date
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

# Per-symbol shape used by both ``TickerRequest.symbol`` and each element of
# ``MultiTickerRequest.symbols`` so the cross-sectional batch path enforces
# the same length bounds as the single-symbol path.
_SymbolStr = Annotated[str, StringConstraints(min_length=1, max_length=20)]

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

Timespan = Literal["minute", "hour", "day"]
Session = Literal["rth", "extended"]


class _BarRange(BaseModel):
    """Common shape for any request that pulls bars over a date range."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_date: str = Field(
        ...,
        pattern=DATE_PATTERN,
        validation_alias=AliasChoices("from_date", "start_date"),
    )
    to_date: str = Field(
        ...,
        pattern=DATE_PATTERN,
        validation_alias=AliasChoices("to_date", "end_date"),
    )
    timespan: Timespan = "minute"
    multiplier: int = Field(1, ge=1)
    session: Session = "rth"

    @model_validator(mode="after")
    def _validate_dates(self) -> _BarRange:
        # The pattern above only checks shape — "2025-13-99" passes the
        # regex but isn't a real date. Parse with date.fromisoformat to
        # verify calendar validity, then confirm from_date <= to_date.
        try:
            f = Date.fromisoformat(self.from_date)
            t = Date.fromisoformat(self.to_date)
        except ValueError as e:
            raise ValueError(f"invalid calendar date: {e}") from e
        if t < f:
            raise ValueError(
                f"to_date ({self.to_date}) must be >= from_date ({self.from_date})"
            )
        return self


class TickerRequest(_BarRange):
    """Single-symbol bar request."""

    symbol: _SymbolStr = Field(
        ...,
        validation_alias=AliasChoices("symbol", "ticker"),
    )


class MultiTickerRequest(_BarRange):
    """Universe-of-symbols bar request — used by cross-sectional research.

    Each element of ``symbols`` is constrained the same way as
    ``TickerRequest.symbol`` (1-20 chars) so empty strings or oversized
    tickers in the universe fail at validation rather than slipping
    through to the runners.
    """

    symbols: list[_SymbolStr] = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("symbols", "tickers"),
    )
