"""How an option position ended with no order of the bot's, as a screen reads it (ADR 0076 decision 22).

History reads it on the run that held the contract, and Activity on the
record of the event. Both are handed the same closed set and the same
sentence, written by the server.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

#: The closed set. ``sold_outside`` is an order the Clerk did not place: the broker's own sale, or the owner's by hand.
#: ``settled_in_cash`` is a simulation's own rule and never a broker's: a Dry Run sold the contract at its
#: exercise value at the close of its last session.
OptionEndingKind = Literal["expired", "exercised", "assigned", "sold_outside", "settled_in_cash"]


class OptionEndingView(BaseModel):
    """One option position that ended with no order of the bot's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: OptionEndingKind
    #: The contract's OCC symbol.
    contract: str = Field(min_length=1, max_length=32)
    #: Contracts the bot lost: one or more.
    contracts: int = Field(ge=1)
    #: Shares the bot's books gained (positive) or gave up (negative). ``None`` when it moved none.
    shares: int | None = None
    #: True when a simulation settled it by its own rule.
    simulated: bool
    #: What happened, in one sentence.
    headline: str = Field(min_length=1)
    #: When the books recorded it.
    recorded_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
