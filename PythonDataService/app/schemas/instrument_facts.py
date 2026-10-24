"""Wire shapes for the facts an instrument header and a Markets list row carry (PRD #3490 R08, #3502).

Every fact names where it came from. A price names its basis and the instant it
was observed; a move names the close it is measured from; an archive status
says whether some days or every required day is stored. A fact that no source
answered is absent or ``unavailable`` with the reason, never a stand-in value.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.data_lake.types import PriceAdjustmentMode
from app.utils.session_anchors import MAX_TIMESTAMP_MS

TimestampMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]

#: The most symbols one read answers. A list page asks for one page of rows, never the whole list.
MAX_FACT_SYMBOLS = 25

#: Where a price came from, from the freshest source down: the live IBKR feed, the vendor's delayed
#: snapshot, or the close of the last completed session.
QuoteBasis = Literal["live_quote", "delayed_snapshot", "last_close"]


class BasisNotUsed(BaseModel):
    """A source that was asked first and did not supply the price, with why."""

    basis: QuoteBasis
    reason: str


class ComparisonClose(BaseModel):
    """The close a day move is measured from: one completed session's regular-hours close."""

    price: float
    session_close_ms: TimestampMs = Field(description="That session's scheduled close, from the canonical calendar.")


class DayMove(BaseModel):
    """``change = price - comparison close``; ``change_percent = 100 * change / comparison close``."""

    change: float
    change_percent: float
    comparison_close: ComparisonClose


class AvailableQuote(BaseModel):
    status: Literal["available"] = "available"
    price: float
    basis: QuoteBasis
    as_of_ms: TimestampMs = Field(description="When the price was observed, not when it was read.")
    move: DayMove | None = None
    move_unavailable_reason: str | None = Field(
        default=None, description="Why there is no move; set exactly when ``move`` is absent."
    )
    not_used: list[BasisNotUsed] = Field(
        default_factory=list, description="Fresher sources that did not answer, freshest first."
    )


class UnavailableQuote(BaseModel):
    status: Literal["unavailable"] = "unavailable"
    reason: str
    not_used: list[BasisNotUsed] = Field(default_factory=list)


InstrumentQuote = Annotated[AvailableQuote | UnavailableQuote, Field(discriminator="status")]

#: ``none_stored``: the archive tracks this underlying and holds no session of it.
#: ``some_days``: at least one session is stored and at least one required session is not.
#: ``complete``: every required session since the archive began is stored. This says nothing of a
#: narrower window a page asks about; the archive's own span is on the fact.
#: ``unavailable``: the archive's receipts could not be read.
OptionsArchiveKind = Literal["none_stored", "some_days", "complete", "unavailable"]


class OptionsArchiveFact(BaseModel):
    kind: OptionsArchiveKind
    earliest_required_session_ms: TimestampMs | None = None
    last_archived_session_ms: TimestampMs | None = None
    complete_through_session_ms: TimestampMs | None = None
    missing_sessions: int | None = Field(default=None, ge=0, le=1_000_000)
    capturing: bool | None = Field(default=None, description="Whether nightly capture is switched on.")
    reason: str | None = None


class InstrumentFacts(BaseModel):
    symbol: str
    quote: InstrumentQuote
    options_archive: OptionsArchiveFact | None = Field(
        default=None, description="Absent for an underlying the archive does not track."
    )


class InstrumentFactsResponse(BaseModel):
    generated_ms: TimestampMs
    closes_adjustment: PriceAdjustmentMode = Field(
        description="The lake tree every close (last close, comparison close) is read from."
    )
    facts: list[InstrumentFacts]
