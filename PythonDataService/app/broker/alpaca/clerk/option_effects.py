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

Which option plans the Clerk takes is one rule, ``option_plan_taken``. Today
it takes a single bought call or put and structures of two to four legs,
including bounded structures with sold legs (ADR 0076 decision 28). A real
account also needs the deployment's setting for orders of several legs
(``app.broker.alpaca.multi_leg_option_orders``). Every other plan is refused
by name here, before anything is written.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.alpaca.multi_leg_option_orders import SETTING as _SEVERAL_LEGS_SETTING
from app.broker.alpaca.multi_leg_option_orders import multi_leg_option_orders_enabled
from app.options.contract import OptionContract
from app.options.deadlines import CloseTerms
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.schemas.action_plan import ActionPlan, OptionEntryLeg, StockEntryLeg, is_option_plan
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class OptionEffectRefusal(StrEnum):
    """Why the Clerk took no option effect. A quote's own fault and a structure's own refusal keep their codes."""

    # The account is a Live one: only a Dry Run, a Live Shadow and a Paper account take an option plan.
    ACCOUNT_NOT_OPEN = "OPTION_PLAN_ACCOUNT_NOT_OPEN"
    # The broker's listing of the picked contract could not be read, so the contract was not confirmed.
    CONTRACT_REFERENCE_UNREAD = "OPTION_CONTRACT_REFERENCE_UNREAD"
    # The plan is not one the Clerk takes: a stock leg beside option legs, legs on two underlyings,
    # a lone sold leg, a lone leg of more than one contract a structure, or a structure a gate
    # still keeps shut (``option_plan_taken``).
    STRUCTURE_NOT_SUPPORTED = "OPTION_STRUCTURE_NOT_SUPPORTED"
    # The plan has several legs and the account is a real one, whose deployment has not turned on
    # option orders of several legs. A Dry Run takes it.
    SEVERAL_LEGS_SETTING_OFF = "OPTION_SEVERAL_LEGS_SETTING_OFF"
    # The structure's net price came to exactly zero. An order of several legs pays or receives;
    # the broker's reference describes nothing between.
    ENTRY_NET_PRICE_ZERO = "OPTION_ENTRY_NET_PRICE_ZERO"
    # A leg would sell a contract the account already holds bought for another bot or outside every
    # bot, or buy one it holds sold. At the broker the two would be one position of nothing.
    CONTRACT_HELD_ON_THE_OTHER_SIDE = "OPTION_CONTRACT_HELD_ON_THE_OTHER_SIDE"
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


@dataclass(frozen=True, slots=True)
class TakenOptionPlan:
    """An option plan the Clerk takes, and what its callers need of it."""

    # The plan's option legs, in its order: one to four.
    legs: tuple[OptionEntryLeg, ...]
    # What the bot is registered under, and what stream health is asked of.
    underlying: str


def option_plan_taken(action_plan: ActionPlan, *, real_account: bool, live_shadow: bool = False) -> TakenOptionPlan:
    """The option plan as the Clerk takes it; refuses every plan it does not take by what it is.

    The one rule, asked at registration, at the entry, at the contract
    reference and by the preview's verdict. ``real_account`` is True for a
    Paper and a Live account and False for a Dry Run and a Live Shadow.
    Only a real account, and only for a plan of several legs, reads the
    deployment's rollout setting. Simulations take every supported structure.

    Whether an account takes option plans at all is asked before this, by
    the account. Raises ``OptionPlanRefused``.
    """
    legs = tuple(leg for leg in action_plan.on_enter if isinstance(leg, OptionEntryLeg))
    if len(legs) != len(action_plan.on_enter) or any(isinstance(leg, StockEntryLeg) for leg in action_plan.on_enter):
        raise OptionPlanRefused(
            OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED,
            "This plan mixes option legs with a stock leg. A bot trades one or the other.",
        )
    if not legs:
        raise OptionPlanRefused(
            OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED, "This plan opens no option leg. It is not an option plan."
        )
    underlyings = sorted({leg.instrument.underlying for leg in legs})
    if len(underlyings) != 1:
        raise OptionPlanRefused(
            OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED,
            f"This plan's legs are on {', '.join(underlyings)}. Every leg of a structure is on one underlying.",
        )
    sold = [leg for leg in legs if leg.position != "long"]
    if len(legs) == 1:
        (leg,) = legs
        if sold:
            raise OptionPlanRefused(
                OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED,
                f"Leg {leg.leg_id!r} sells an option to open. The account Clerk takes one bought call or put for "
                "a structure; a sold leg needs a bounded multi-leg structure.",
            )
        if leg.qty_ratio != 1:
            raise OptionPlanRefused(
                OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED,
                f"Leg {leg.leg_id!r} holds {leg.qty_ratio} contracts a structure. A single option is one contract "
                "a structure; more of it is more structures.",
            )
        return TakenOptionPlan(legs=legs, underlying=underlyings[0])
    if real_account and not multi_leg_option_orders_enabled():
        raise OptionPlanRefused(
            OptionEffectRefusal.SEVERAL_LEGS_SETTING_OFF,
            f"This plan opens {len(legs)} option legs as one order. A Paper or a Live account takes such an order "
            f"only once its deployment turns on {_SEVERAL_LEGS_SETTING}, which is off: no order of several legs "
            "has been checked against Alpaca yet. A Dry Run takes this plan.",
        )
    return TakenOptionPlan(legs=legs, underlying=underlyings[0])


__all__ = [
    "STANDARD_CONTRACT_MULTIPLIER",
    "OptionEffectRefusal",
    "OptionEntryEvidence",
    "OptionExitEvidence",
    "OptionPlanRefused",
    "RecordedQuote",
    "TakenOptionPlan",
    "is_option_plan",
    "option_plan_taken",
]
