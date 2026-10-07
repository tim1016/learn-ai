"""What a Clerk asks the coordinator about the rate and the dividends an option is priced with, and its answer (#2913).

A Clerk holds no Polygon key and no FRED key (ADR 0062), so it asks the
coordinator, which holds both. Both sides read and write these models and
nothing else, so the two cannot drift apart.

- Every moment is ``int64 ms UTC``. A date is one too: an expiry is its 16:00
  New York anchor, and a dividend's ex-date is the open of that date's session.
- A number that is not a whole number is text, ``"0.0512"``: the shortest
  decimal that reads back to the same float. A JSON number is refused, because
  a reader may round one.
- Each of the two facts is known or says why it is not. Dividends that are not
  known have another shape than dividends that are, so "not known" can never be
  read as an empty list, which would mean none is declared.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.options.expiry import OptionExpiryMs, option_expiry_anchor_ms
from app.schemas.broker_bots import normalized_symbol
from app.schemas.fleet_history_batch import MAX_HISTORY_BATCH_AS_OF_MS, MIN_HISTORY_BATCH_AS_OF_MS
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms, et_midnight_ms

if TYPE_CHECKING:
    from datetime import date

    from app.services.option_model_inputs import ModelInputsRead

_WIRE = ConfigDict(strict=True, extra="forbid", frozen=True)

# A number as its decimal text, never a JSON number.
DecimalText = Annotated[str, StringConstraints(pattern=r"^-?\d+(\.\d+)?$", max_length=64)]
# A moment the calendar can hold, in the years the history read also answers for.
_Moment = Annotated[int, Field(ge=MIN_HISTORY_BATCH_AS_OF_MS, le=MAX_HISTORY_BATCH_AS_OF_MS)]


def decimal_text(number: float) -> str:
    """``number`` as the shortest decimal text that reads back, with ``float``, to the same float."""
    return format(Decimal(repr(number)), "f")


class WhyNotKnown(StrEnum):
    """Why the rate is only the default, or why the dividends are not known. The code beside the sentence."""

    FRED_GAVE_NO_RATE = "fred_gave_no_rate"
    NO_POLYGON_KEY = "no_polygon_key"
    POLYGON_DID_NOT_ANSWER = "polygon_did_not_answer"
    POLYGON_ANSWER_UNREADABLE = "polygon_answer_unreadable"
    # What a Clerk says of its own ask. The coordinator never answers with these.
    COORDINATOR_NOT_CONFIGURED = "coordinator_not_configured"
    COORDINATOR_NOT_ASKED = "coordinator_not_asked"
    COORDINATOR_UNREACHABLE = "coordinator_unreachable"
    COORDINATOR_TIMED_OUT = "coordinator_timed_out"
    COORDINATOR_REFUSED = "coordinator_refused"
    COORDINATOR_FAILED = "coordinator_failed"
    COORDINATOR_ANSWER_UNREADABLE = "coordinator_answer_unreadable"
    # A lookup that failed and gave no code of its own.
    LOOKUP_FAILED = "lookup_failed"


class OptionModelInputsQuery(BaseModel):
    """One ask: the rate and the declared dividends for options on ``underlying`` at ``at_ms``, up to the latest expiry."""

    model_config = _WIRE

    underlying: str = Field(min_length=1, max_length=12)
    at_ms: _Moment
    latest_expiry_ms: OptionExpiryMs
    # False: answer at once that a lookup is running, in place of waiting for it. A page asks every second.
    wait: bool

    @field_validator("underlying")
    @classmethod
    def _normalize_underlying(cls, value: str) -> str:
        return normalized_symbol(value)

    @classmethod
    def asking(cls, underlying: str, *, at_ms: int, latest_expiry: date, wait: bool) -> OptionModelInputsQuery:
        return cls(underlying=underlying, at_ms=at_ms, latest_expiry_ms=option_expiry_anchor_ms(latest_expiry), wait=wait)


class OptionModelInputsRequest(OptionModelInputsQuery):
    """The ask as it crosses from a Clerk to the coordinator, with the Clerk it is authenticated as."""

    clerk_id: str = Field(min_length=1)


class ServedRate(BaseModel):
    """The rate to price with. ``source`` "default" is the rate not being known: the fixed default stands in."""

    model_config = _WIRE

    # A year's rate as a fraction: "0.0512" is 5.12%.
    value: DecimalText
    source: Literal["FRED", "default"]
    why_default: WhyNotKnown | None
    # The New York date the rate is the latest published by, as New York midnight beginning that date.
    published_by_ms: int = Field(gt=0, le=MAX_TIMESTAMP_MS)
    sentence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _a_default_says_why_and_only_a_default_does(self) -> ServedRate:
        if (self.source == "default") != (self.why_default is not None):
            raise ValueError("a default rate says why it is the default, and a rate from FRED gives no such reason")
        return self


class ServedDividend(BaseModel):
    """One declared cash dividend still to go ex."""

    model_config = _WIRE

    # The open of the session the stock first trades without it.
    ex_ms: int = Field(gt=0, le=MAX_TIMESTAMP_MS)
    # Dollars a share.
    cash: DecimalText
    currency: Literal["USD"]

    @field_validator("cash")
    @classmethod
    def _above_zero(cls, value: str) -> str:
        if not Decimal(value) > 0:
            raise ValueError("a cash dividend is an amount above zero")
        return value


class DividendsKnown(BaseModel):
    """The cash dividends declared and still to go ex. An empty list is known: none is declared."""

    model_config = _WIRE

    status: Literal["known"]
    declared: list[ServedDividend]
    # When the coordinator looked them up. It may have kept them up to an hour since.
    read_ms: _Moment
    sentence: str = Field(min_length=1)


class DividendsNotKnown(BaseModel):
    """The dividends could not be looked up. It carries no list: not known is not none declared."""

    model_config = _WIRE

    status: Literal["unknown"]
    why: WhyNotKnown
    sentence: str = Field(min_length=1)


class ModelInputsAnswer(BaseModel):
    """The coordinator's answer to one ask, naming the ask it answers."""

    model_config = _WIRE

    status: Literal["answered"]
    underlying: str
    at_ms: _Moment
    latest_expiry_ms: OptionExpiryMs
    rate: ServedRate
    dividends: Annotated[DividendsKnown | DividendsNotKnown, Field(discriminator="status")]

    @model_validator(mode="after")
    def _is_an_answer_for_its_own_moment(self) -> ModelInputsAnswer:
        if self.rate.published_by_ms != et_midnight_ms(et_date_at_ms(self.at_ms)):
            raise ValueError("the rate is not the one published by the New York date of at_ms")
        if isinstance(self.dividends, DividendsKnown):
            # Two may share an ex-date: a special dividend can go ex with a regular one.
            went_ex = [dividend.ex_ms for dividend in self.dividends.declared]
            if went_ex != sorted(went_ex) or any(ex_ms <= self.at_ms for ex_ms in went_ex):
                raise ValueError("the dividends are the ones still to go ex after at_ms, in the order they go ex")
        return self

    @classmethod
    def from_read(cls, query: OptionModelInputsQuery, read: ModelInputsRead) -> ModelInputsAnswer:
        """What this process read for ``query``, as the answer a Clerk is given."""
        if read.dividends is None:
            dividends: DividendsKnown | DividendsNotKnown = DividendsNotKnown(
                status="unknown", why=read.dividends_unknown_why or WhyNotKnown.LOOKUP_FAILED, sentence=read.dividends_source
            )
        else:
            dividends = DividendsKnown(
                status="known",
                declared=[
                    ServedDividend(ex_ms=paid.ex_ms, cash=decimal_text(paid.cash), currency="USD")
                    for paid in read.dividends
                ],
                read_ms=read.dividends_read_ms,
                sentence=read.dividends_source,
            )
        why_default = (read.rate_default_why or WhyNotKnown.LOOKUP_FAILED) if read.rate_is_default else None
        return cls(
            status="answered",
            underlying=query.underlying,
            at_ms=query.at_ms,
            latest_expiry_ms=query.latest_expiry_ms,
            rate=ServedRate(
                value=decimal_text(read.rate),
                source="default" if read.rate_is_default else "FRED",
                why_default=why_default,
                published_by_ms=et_midnight_ms(et_date_at_ms(query.at_ms)),
                sentence=read.rate_source,
            ),
            dividends=dividends,
        )


class ModelInputsBeingLookedUp(BaseModel):
    """Another ask's lookup is running, and this ask said it would not wait. Nothing is known from it."""

    model_config = _WIRE

    status: Literal["being_looked_up"]


OptionModelInputsResponse = Annotated[ModelInputsAnswer | ModelInputsBeingLookedUp, Field(discriminator="status")]

__all__ = [
    "DecimalText",
    "DividendsKnown",
    "DividendsNotKnown",
    "ModelInputsAnswer",
    "ModelInputsBeingLookedUp",
    "OptionModelInputsQuery",
    "OptionModelInputsRequest",
    "OptionModelInputsResponse",
    "ServedDividend",
    "ServedRate",
    "WhyNotKnown",
    "decimal_text",
]
