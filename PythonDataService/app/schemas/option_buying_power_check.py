"""What a bot's page is served about each option entry's comparison with Alpaca's option buying power (ADR 0076 decision 26).

Every word and every dollar string here is authored by the backend. Amounts
are whole cents beside the dollars a page shows. Every time is ``int64 ms
UTC``. A figure that could not be had is ``null``, never zero, and a
comparison that was not made says why: silence is never shown as agreement.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

OptionBuyingPowerResult = Literal["consistent", "unknown", "over_reserved", "under_reserved_unexplained"]
# ``entry_buying_power.NotComparedReason``, spelled out so the generated contract carries the closed set.
OptionBuyingPowerNotComparedReason = Literal[
    "JUDGMENT_NOT_SWITCHED_ON",
    "FIRST_READING_FAILED",
    "FIRST_READING_NOT_READY",
    "FIRST_READING_WITHOUT_FIGURE",
    "FIRST_READING_STALE",
    "SECOND_READING_FAILED",
    "SECOND_READING_WITHOUT_FIGURE",
    "READING_NOT_WHOLE_CENTS",
    "PREMIUM_NOT_WHOLE_CENTS",
    "OTHER_ORDER_MOVED",
    "CASH_MOVED",
    "CASH_MOVEMENT_UNREAD",
    "ACCOUNT_NOT_CHECKED_AFTER",
    "ACCOUNT_NOT_CLEAN_AFTER",
    "ENTRY_BOUGHT_NOTHING",
    "UNFILLED_PART_RELEASE_NOT_VERIFIED",
    "OUTCOME_NOT_PROVEN",
    "CLERK_RESTARTED",
    "COMPARISON_FAILED",
    "SIMULATION",
    "NOT_COMPARED_YET",
]
_Ms = Field(ge=0, le=MAX_TIMESTAMP_MS)


class OptionBuyingPowerAmount(BaseModel):
    """One amount: whole cents, and the dollars a page shows for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cents: int
    dollars: str


class OptionBuyingPowerReading(BaseModel):
    """One read of the account's option buying power, with the Clerk's clock as the read began and ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    began_ms: int = _Ms
    ended_ms: int = _Ms
    # What Alpaca stated, when it stated a figure in whole cents.
    buying_power: OptionBuyingPowerAmount | None


class OptionBuyingPowerCheck(BaseModel):
    """One option entry's comparison, as a page shows it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_ref: str
    # The contract's OCC symbol.
    contract: str
    result: OptionBuyingPowerResult
    result_label: str
    # Set exactly when the result is ``unknown``.
    reason_code: OptionBuyingPowerNotComparedReason | None
    # One sentence saying what was found, or why nothing was compared.
    sentence: str
    contracts_asked: int = Field(ge=1)
    contracts_filled: int | None = Field(ge=0)
    # What the entry set aside when it was accepted, and its share for the contracts that filled.
    set_aside: OptionBuyingPowerAmount
    set_aside_for_filled: OptionBuyingPowerAmount | None
    # From the executions on the books.
    premium_paid: OptionBuyingPowerAmount | None
    first_reading: OptionBuyingPowerReading | None
    second_reading: OptionBuyingPowerReading | None
    # The Clerk's clock as the order was handed to Alpaca.
    sent_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    # First reading less second. Negative when the figure rose.
    fall: OptionBuyingPowerAmount | None
    compared_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    # Whether the bot's open notice names this entry.
    holds: bool
    # The account's own option requirement over every contract the books attribute to a bot, as the
    # first reading began and as the second began once the entry's outcome was proven. Evidence: it
    # changes no result and holds nothing. Alpaca values the whole account together, so the change
    # can be less than what the bot set aside. Absent, not zero, when it was not recorded.
    account_requirement_before: OptionBuyingPowerAmount | None
    account_requirement_after: OptionBuyingPowerAmount | None
    # After less before. Negative when the entry released requirement by netting.
    account_requirement_change: OptionBuyingPowerAmount | None
    # One sentence saying what the account's requirement did, or that it was not recorded.
    account_requirement_sentence: str


class OptionBuyingPowerHoldEntry(BaseModel):
    """One entry a bot's notice names, with the notice's two figures."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_ref: str
    fall: OptionBuyingPowerAmount
    set_aside: OptionBuyingPowerAmount


class OptionBuyingPowerHold(BaseModel):
    """The notice that holds a bot's new entries until the owner has reviewed it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    headline: str
    explanation: str
    impact: str
    next_step: str
    # What the owner's review does, said before it is confirmed.
    review_consequence: str
    raised_at_ms: int = _Ms
    entries: list[OptionBuyingPowerHoldEntry]


class OptionBuyingPowerReasonCount(BaseModel):
    """How many of a bot's option entries were not compared for one reason, with the reason in words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason_code: OptionBuyingPowerNotComparedReason
    sentence: str
    count: int = Field(ge=1)


class OptionBuyingPowerCounts(BaseModel):
    """How many of a bot's option entries were compared with Alpaca's option buying power, and why the others were not.

    Over every option entry of the bot, not only the newest ones listed.
    An entry nobody compared is counted as that, with its reason: silence is
    never shown as agreement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: int = Field(ge=0)
    # Entries whose pair of readings came to a result: consistent, over-reserved or under-reserved.
    compared: int = Field(ge=0)
    not_compared: int = Field(ge=0)
    # The reasons of the entries not compared, most first.
    not_compared_by_reason: list[OptionBuyingPowerReasonCount]
    sentence: str


class OptionBuyingPowerChecksResponse(BaseModel):
    """A bot's option entries, newest first, each with its comparison, and the notice that holds the bot if one does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    strategy_instance_id: str
    # Whether this bot's account is a simulation, which makes no comparison.
    simulation: bool
    # Whether a clean pair of readings is judged on this account (the account's setting).
    judged: bool
    hold: OptionBuyingPowerHold | None
    counts: OptionBuyingPowerCounts
    # The newest entries only; ``counts`` is over all of them.
    checks: list[OptionBuyingPowerCheck]


class OptionBuyingPowerHoldReviewResponse(BaseModel):
    """What a review did, and the bot's comparisons as they now stand."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # False when the bot had no notice open: nothing was changed.
    reviewed: bool
    state: OptionBuyingPowerChecksResponse
