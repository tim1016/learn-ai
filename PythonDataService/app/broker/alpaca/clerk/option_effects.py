"""What a caller hands the Clerk with an option decision, and why the Clerk may refuse it (ADR 0076).

An option entry cannot be rebuilt from the Action Plan inside the Clerk: its
contract was picked by the resolver from quotes. So the caller hands over the
facts the pick stands on, and the Clerk works out the money from them itself:

- with an ENTER, ``OptionEntryEvidence``: the contract picked for each leg,
  its listed multiplier, the quote read just before the hand-over, and the
  bot's two limits;
- with an EXIT, ``OptionExitEvidence``: the quotes read at the exit decision
  for what the bot holds. An EXIT may come without it.

Neither carries a price, a commitment or a fee. Those are the Clerk's to
compute (``app.options.entry.prepare_entry``, ``app.broker.alpaca.option_fees``).

Today the Clerk takes one shape of option plan, on one kind of account: a
single bought call or put, in a Dry Run. Every other plan is refused by name
here, before anything is written.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.schemas.action_plan import ActionPlan, OptionEntryLeg, StockEntryLeg
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class OptionEffectRefusal(StrEnum):
    """Why the Clerk took no option effect. A quote's own fault and a structure's own refusal keep their codes."""

    # The account is not a Dry Run's: nothing about option orders has been shown on a real account.
    ACCOUNT_NOT_SIMULATED = "OPTION_PLAN_NEEDS_A_DRY_RUN"
    # The plan has a sold leg, several legs, or a leg of more than one contract a structure.
    STRUCTURE_NOT_SUPPORTED = "OPTION_STRUCTURE_NOT_SUPPORTED"
    # An ENTER came with no picked contracts and no quotes.
    ENTRY_EVIDENCE_MISSING = "OPTION_ENTRY_QUOTES_NOT_KEPT"
    # What came does not cover the plan's legs.
    ENTRY_EVIDENCE_INCOMPLETE = "OPTION_ENTRY_EVIDENCE_INCOMPLETE"
    # The picked contract's expiry date is already past.
    CONTRACT_EXPIRED = "OPTION_CONTRACT_EXPIRED"
    # The fee model has no rate for the day, so the entry's fees cannot be set aside.
    FEE_RATE_NOT_PINNED = "OPTION_FEE_RATE_NOT_PINNED"
    # A simulated close has no quote kept to price it from.
    EXIT_UNPRICED = "OPTION_EXIT_UNPRICED"


class OptionPlanRefused(ValueError):
    """A typed refusal of an option plan, raised where no receipt can carry it."""

    def __init__(self, reason: OptionEffectRefusal, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


class RecordedQuote(BaseModel):
    """A quote as it is written down: with an entry's facts, with a simulated fill, and in the quotes kept.

    The price of each side that had arrived and when the provider's callback
    delivered it, the kind of data it was, and the subscription generation.
    A side that had not arrived is absent, never zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bid: Decimal | None
    bid_received_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    ask: Decimal | None
    ask_received_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    data_type: QuoteDataType
    generation: int

    @model_validator(mode="after")
    def _each_side_has_a_price_and_a_time_or_neither(self) -> RecordedQuote:
        for name, price, received in (("bid", self.bid, self.bid_received_ms), ("ask", self.ask, self.ask_received_ms)):
            if (price is None) != (received is None):
                raise ValueError(f"a quote's {name} has a price and the time it was received, or neither")
        return self

    @classmethod
    def of(cls, quote: HeldQuote) -> RecordedQuote:
        return cls(
            bid=None if quote.bid is None else quote.bid.price,
            bid_received_ms=None if quote.bid is None else quote.bid.received_ms,
            ask=None if quote.ask is None else quote.ask.price,
            ask_received_ms=None if quote.ask is None else quote.ask.received_ms,
            data_type=quote.data_type,
            generation=quote.generation,
        )

    @property
    def held(self) -> HeldQuote:
        return HeldQuote(
            bid=None if self.bid is None or self.bid_received_ms is None else QuoteSide(self.bid, self.bid_received_ms),
            ask=None if self.ask is None or self.ask_received_ms is None else QuoteSide(self.ask, self.ask_received_ms),
            data_type=self.data_type,
            generation=self.generation,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionEntryEvidence:
    """The facts one option entry stands on, as the caller read them just before handing it over."""

    # The contract picked for each option leg, by the plan's leg id.
    picked: Mapping[str, OptionContract]
    # The multiplier each pick is listed with.
    multipliers: Mapping[str, int]
    # The quote each leg's price is to be read from. Each side carries its own receipt time.
    quotes: Mapping[str, HeldQuote]
    # The subscription generation those quotes were read in.
    generation: int
    # When the caller read them (int64 ms UTC).
    rechecked_at_ms: int
    # The widest bid-ask gap the owner allows any leg, as a percent of its mid.
    leg_gap_limit_percent: Decimal
    # The most opening and closing at once may cost, as a percent of the entry commitment.
    cost_limit_percent: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionExitEvidence:
    """The quotes read at an exit decision for the legs the bot holds. A leg with none is left out."""

    quotes: Mapping[str, HeldQuote]
    generation: int
    read_at_ms: int


def is_option_plan(action_plan: ActionPlan) -> bool:
    """Whether the plan's entry is in option contracts. A plan cannot mix them with a stock leg."""
    return any(isinstance(leg, OptionEntryLeg) for leg in action_plan.on_enter)


def the_one_bought_option_leg(action_plan: ActionPlan) -> OptionEntryLeg:
    """The plan's single bought option leg; refuses every other option plan by what it is.

    A sold leg has a requirement the books cannot yet reserve, and several
    legs fill as several orders the books cannot yet tell apart.
    """
    legs = [leg for leg in action_plan.on_enter if isinstance(leg, OptionEntryLeg)]
    if len(legs) != len(action_plan.on_enter) or any(isinstance(leg, StockEntryLeg) for leg in action_plan.on_enter):
        raise OptionPlanRefused(
            OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED,
            "This plan mixes option legs with a stock leg. A bot trades one or the other.",
        )
    if len(legs) != 1:
        raise OptionPlanRefused(
            OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED,
            f"This plan opens {len(legs)} option legs. The account Clerk takes one bought call or put for now; "
            "structures of several legs are not built yet.",
        )
    (leg,) = legs
    if leg.position != "long":
        raise OptionPlanRefused(
            OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED,
            f"Leg {leg.leg_id!r} sells an option to open. The account Clerk takes one bought call or put for now; "
            "a sold leg is not built yet.",
        )
    if leg.qty_ratio != 1:
        raise OptionPlanRefused(
            OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED,
            f"Leg {leg.leg_id!r} holds {leg.qty_ratio} contracts a structure. A single option is one contract a "
            "structure; more of it is more structures.",
        )
    return leg


__all__ = [
    "OptionEffectRefusal",
    "OptionEntryEvidence",
    "OptionExitEvidence",
    "OptionPlanRefused",
    "RecordedQuote",
    "is_option_plan",
    "the_one_bought_option_leg",
]
