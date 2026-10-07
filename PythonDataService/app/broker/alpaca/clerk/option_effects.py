"""What a caller hands the Clerk with an option decision, and why the Clerk may refuse it (ADR 0076).

An option entry cannot be rebuilt from the Action Plan inside the Clerk: its
contract was picked by the resolver from quotes. So the caller hands over the
facts the pick stands on, and the Clerk works out the money from them itself:

- with an ENTER, ``OptionEntryEvidence``: the contract picked for each leg,
  its listed multiplier, the quote read just before the hand-over, the
  bot's two limits, and the owner's order terms;
- with an EXIT, ``OptionExitEvidence``: the quotes read at the exit decision
  for what the bot holds, each named by its contract. An EXIT may come
  without it.

Neither carries a price, a commitment or a fee. Those are the Clerk's to
compute (``app.options.entry.prepare_entry``, ``app.broker.alpaca.option_fees``).

Today the Clerk takes one shape of option plan: a single bought call or put,
in a Dry Run or on a Paper account. Every other plan is refused by name
here, before anything is written.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.options.contract import OptionContract
from app.options.deadlines import CloseTerms
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.schemas.action_plan import ActionPlan, OptionEntryLeg, StockEntryLeg, is_option_plan
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class OptionEffectRefusal(StrEnum):
    """Why the Clerk took no option effect. A quote's own fault and a structure's own refusal keep their codes."""

    # The account is a Live Shadow's or a Live one: only a Dry Run and a Paper account take an option plan.
    ACCOUNT_NOT_OPEN = "OPTION_PLAN_ACCOUNT_NOT_OPEN"
    # The broker's listing of the picked contract could not be read, so the contract was not confirmed.
    CONTRACT_REFERENCE_UNREAD = "OPTION_CONTRACT_REFERENCE_UNREAD"
    # The plan has a sold leg, several legs, or a leg of more than one contract a structure.
    STRUCTURE_NOT_SUPPORTED = "OPTION_STRUCTURE_NOT_SUPPORTED"
    # An ENTER came with no picked contracts and no quotes.
    ENTRY_EVIDENCE_MISSING = "OPTION_ENTRY_QUOTES_NOT_KEPT"
    # What came does not cover the plan's legs, or a part of it is not what it must be:
    # a limit that is no percent, a price finer than a cent.
    ENTRY_EVIDENCE_INCOMPLETE = "OPTION_ENTRY_EVIDENCE_INCOMPLETE"
    # The picked contract's multiplier is not the standard one. A bot trades standard contracts only.
    CONTRACT_NOT_STANDARD = "OPTION_CONTRACT_NOT_STANDARD"
    # The books already hold this contract with another multiplier than the one handed over.
    CONTRACT_TERMS_CONFLICT = "OPTION_CONTRACT_TERMS_CONFLICT"
    # The contract's last session has ended: nothing is bought or sold in it.
    CONTRACT_EXPIRED = "OPTION_CONTRACT_EXPIRED"
    # The entry reached the Clerk too close to the close its position would owe that day to be seen
    # through. The resolver's recheck refuses the same at its own moment, under the same code.
    PAST_ENTRY_CUTOFF = "OPTION_CONTRACT_PAST_ENTRY_CUTOFF"
    # The fee model has no rate for the day, so the entry's fees cannot be set aside.
    FEE_RATE_NOT_PINNED = "OPTION_FEE_RATE_NOT_PINNED"
    # A simulated close has no quote kept to price it from.
    EXIT_UNPRICED = "OPTION_EXIT_UNPRICED"
    # The bot holds more than one contract, and one EXIT closes one.
    SEVERAL_CONTRACTS_HELD = "OPTION_EXIT_SEVERAL_CONTRACTS_HELD"
    # An EXIT came with a quote of a contract the bot does not hold.
    EXIT_QUOTES_FOR_ANOTHER_CONTRACT = "OPTION_EXIT_QUOTES_NOT_THE_HELD_CONTRACT"
    # The plan's watched window can need more quote lines than this Clerk may open, so its
    # contracts could never all be quoted. Refused when the run starts, not at every entry.
    WINDOW_OVER_LINE_BUDGET = "OPTION_WINDOW_OVER_LINE_BUDGET"
    # This Clerk reads its market status from a shared source and makes no IBKR pass of its own,
    # so no option contract is ever quoted on it. Refused when the run starts, not at every entry.
    NO_OPTION_QUOTES_HERE = "OPTION_CLERK_HAS_NO_OPTION_QUOTES"
    # The bot's entry wait, with the time its runner keeps to prove a cancel and for the Clerk's read
    # of the listing, leaves nothing of the runner's wait for what an entry came to. No entry could be
    # taken and still be cancelled and proven ended in time. Refused when the run starts, not at every
    # entry.
    ENTRY_WAIT_OVER_OUTCOME_WAIT = "OPTION_ENTRY_WAIT_OVER_OUTCOME_WAIT"
    # The bot was started with no order terms sealed with it. They are fixed at Deploy for the bot's
    # life, and nothing a decision says supplies them. Refused when the run starts.
    ORDER_TERMS_NOT_SEALED = "OPTION_ORDER_TERMS_NOT_SEALED"


# Shares one standard contract's price stands for. A listing with any other
# multiplier is an adjusted or a non-standard contract, which the resolver
# never picks and the Clerk never takes on a caller's word.
STANDARD_CONTRACT_MULTIPLIER = 100


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
    # The owner's order terms: how long the entry waits for its fill before the Clerk
    # cancels what is left, and how long before a close its position is closed.
    terms: CloseTerms = field(default_factory=CloseTerms)


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionExitEvidence:
    """The quotes read at an exit decision for the contracts the bot holds. A contract with none is left out.

    Each quote is named by its contract's OCC symbol, as the quotes kept are
    (``kept_option_quotes``). A leg id would say which rule of the plan the
    quote was read for, never which contract it is of; the Clerk closes a
    contract, and takes a quote only for the one it closes.
    """

    # By the contract's OCC symbol.
    quotes: Mapping[str, HeldQuote]
    generation: int
    read_at_ms: int

    def for_other_contracts_than(self, symbol: str) -> tuple[str, ...]:
        """The contracts quoted here that are not ``symbol``, in order."""
        return tuple(sorted(quoted for quoted in self.quotes if quoted != symbol))


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
    "STANDARD_CONTRACT_MULTIPLIER",
    "OptionEffectRefusal",
    "OptionEntryEvidence",
    "OptionExitEvidence",
    "OptionPlanRefused",
    "RecordedQuote",
    "is_option_plan",
    "the_one_bought_option_leg",
]
