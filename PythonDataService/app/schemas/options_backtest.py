"""Wire shapes for the Options Backtest page's setup routes (#3030).

The setup itself is ``app.options_backtest.setup.OptionsBacktestSetup``,
served as it is. Names are snake_case. Every temporal value is ``int64 ms
UTC``; a session's open and close are minutes past New York midnight, a
distance that holds all year, named for it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from app.options_backtest.describe import SetupDescription
from app.options_backtest.saved_setups import SavedSetup
from app.options_backtest.session_clock import SessionClock
from app.options_backtest.setup import OptionsBacktestSetup
from app.utils.session_anchors import MAX_TIMESTAMP_MS

MINUTES_PER_DAY = 24 * 60


def _plain_numbers(value: Any) -> Any:
    if isinstance(value, Decimal):
        return Decimal(format(value, "f"))
    if isinstance(value, dict):
        return {key: _plain_numbers(each) for key, each in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_plain_numbers(each) for each in value)
    return value


def served_setup(setup: OptionsBacktestSetup) -> OptionsBacktestSetup:
    """The same setup with every number in plain digits, so the text served names it the way an owner writes it.

    A setup spelled ``1e5`` holds ``Decimal("1E+5")``, which a model writes as ``"1E+5"``. A setup's numbers are held to a
    few places (``setup.py``), so only a positive exponent can show, and writing the digits out never changes a value.
    """
    return OptionsBacktestSetup.model_validate(_plain_numbers(setup.model_dump()))


class SessionClockResponse(BaseModel):
    """A full session's open and close, from the calendar, as minutes past New York midnight."""

    open_minute_et: int = Field(ge=0, lt=MINUTES_PER_DAY)
    close_minute_et: int = Field(ge=0, le=MINUTES_PER_DAY)

    @classmethod
    def from_clock(cls, clock: SessionClock) -> Self:
        return cls(open_minute_et=clock.open_minute_et, close_minute_et=clock.close_minute_et)


class LegDescriptionResponse(BaseModel):
    leg_id: str
    sentences: list[str]


class SetupDescriptionResponse(BaseModel):
    """A setup in plain sentences, and the setup as the server read it.

    Each block is a list of whole sentences, one to an item, built as a list: the page shows them as they are and never
    splits or rebuilds them. ``setup`` is the setup normalised, with every default filled, as a run would be given it.
    ``window`` is what the setup sets; ``fills`` is how the engine fills an order, which no setting changes.
    """

    setup: OptionsBacktestSetup
    window: list[str]
    fills: list[str]
    legs: list[LegDescriptionResponse]
    square_off: list[str]
    costs: list[str]
    # What the owner should read first; empty for a setup that looks sound.
    notices: list[str]

    @classmethod
    def from_description(cls, setup: OptionsBacktestSetup, description: SetupDescription) -> Self:
        return cls(
            setup=served_setup(setup),
            window=list(description.window),
            fills=list(description.fills),
            legs=[LegDescriptionResponse(leg_id=leg.leg_id, sentences=list(leg.sentences)) for leg in description.legs],
            square_off=list(description.square_off),
            costs=list(description.costs),
            notices=list(description.notices),
        )


class OptionsBacktestSetupNameRuleResponse(BaseModel):
    """What a setup name may be: the pattern the server checks, and the rule in a sentence for the owner."""

    pattern: str
    sentence: str


class OptionsBacktestLimitsResponse(BaseModel):
    """The limits the server holds a setup and a request to, so the page copies none of them."""

    max_legs: int
    max_contracts_per_leg: int
    max_reentries_per_leg: int
    max_strike_steps: int
    max_expiry_days: int
    # The most trading days one run, or one check, may name.
    max_range_sessions: int
    max_strikes_each_side: int = Field(
        description=(
            "The most strikes each side of the money one read of the past chain takes (a window of this many above "
            "and below the money strike, for each right). A request for more is refused."
        )
    )
    max_contracts_per_download: int = Field(
        description=(
            "The most distinct contracts one download of the past chain takes. A request naming more is refused "
            "before anything is fetched."
        )
    )
    setup_name: OptionsBacktestSetupNameRuleResponse


class SavedSetupResponse(BaseModel):
    """One listed setup. Exactly one of ``setup`` and ``cannot_open_reason`` is set; an example has no ``saved_at_ms``."""

    name: str
    example: bool
    saved_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    setup: OptionsBacktestSetup | None = None
    cannot_open_reason: str | None = None

    @model_validator(mode="after")
    def _either_the_setup_or_why_not(self) -> Self:
        if (self.setup is None) == (self.cannot_open_reason is None):
            raise ValueError("a listed setup carries its setup, or the reason it cannot be opened, and not both")
        return self

    @classmethod
    def from_saved(cls, saved: SavedSetup) -> Self:
        return cls(
            name=saved.name,
            example=saved.example,
            saved_at_ms=saved.saved_at_ms,
            setup=None if saved.setup is None else served_setup(saved.setup),
            cannot_open_reason=saved.cannot_open_reason,
        )


class OptionsBacktestRefusal(BaseModel):
    code: str
    message: str


class OptionsBacktestRefusalBody(BaseModel):
    """The 404 and 409 body: FastAPI wraps an ``HTTPException`` detail as ``{"detail": ...}``."""

    detail: OptionsBacktestRefusal
