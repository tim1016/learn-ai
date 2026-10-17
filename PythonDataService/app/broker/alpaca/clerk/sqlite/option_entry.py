"""An option entry as the books keep it, and as its exit reads it back (ADR 0076).

An entry in an option contract is accepted under the same ``ENTER_ACCEPTED``
fact as an entry in shares, with one more key, ``option``, which this module
writes and reads. It holds what the entry was admitted on and what its exit
needs: the order as it was asked, the contract each leg resolved to with its
multiplier, the quote each leg was priced from, and the money in exact cents.

"Exit closes what entry opened" is read from here. The exit never asks the
plan's rules again: it reads the contract from the entry's own facts.

An entry is one of two types. ``OptionEntryFacts`` is one order of one
contract, written as it always was. ``MultiLegOptionEntryFacts`` is one
order of two to four legs: a new type beside it, so no one-leg entry's
facts, which are hashed into the custody chain, gain a key. Both answer the
same reads (``legs``, ``leg_terms``, ``units``, ``money``, ``structure``,
``order_line``), so a caller that takes an ``OptionEntry`` never asks which
it has. ``the_leg`` and ``contract_terms`` are the one-leg type's alone: code
that still assumes one leg fails loudly on a structure, and never takes its
first leg for the whole.

``held_of_entry`` reads what a bot holds of one entry: each leg's contracts
from the books, signed, and the whole structures they come to
(``app.options.whole_units``). ``held_option_legs`` reads what a bot holds of
every contract it has, whatever entry opened it: the legs an exit estimate is
worked from (#3100).

Formula: every figure here is ``app/options/entry.py``'s (``prepare_entry``)
  and ``app/broker/alpaca/option_fees.py``'s (the fee provision). This module
  copies them into the entry's facts and computes nothing of its own.
Reference: ADR 0076 decisions 2, 5 and 7.
Canonical implementation: ``app/options/requirement.py`` and
  ``app/broker/alpaca/option_fees.py``.
Validated against: tests/broker/alpaca/clerk/sqlite/test_option_entry_and_exit.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.alpaca.clerk.option_effects import OptionExitEvidence, RecordedQuote
from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms, contract_terms, order_contract_terms
from app.broker.alpaca.clerk.sqlite.facts import EnterAcceptedFacts, ExitAcceptedFacts
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.option_order_legs import multi_leg_parent_line
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import (
    MultiLegOptionOrderRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.options.contract import OptionContract
from app.options.deadlines import CloseTerms
from app.options.entry import PreparedEntry
from app.options.quotes import HeldQuote
from app.options.requirement import ContractAtMultiplier
from app.options.structure import MAX_OPTION_LEGS, LegPosition, ResolvedLeg, ResolvedStructure
from app.options.whole_units import LegContracts, WholeUnits, in_leg_direction, whole_units
from app.utils.session_anchors import MAX_TIMESTAMP_MS

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

_Cents = Annotated[int, Field(strict=True)]
_Milliseconds = Annotated[int, Field(strict=True, ge=0)]


class OptionEntryLegFacts(BaseModel):
    """One leg of an accepted option entry: the contract its rule came to, and what it was priced from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg_id: str = Field(min_length=1)
    contract: OptionContract
    position: LegPosition
    # Contracts a structure.
    ratio: int = Field(ge=1)
    # Shares one contract's price stands for, as its listing states it.
    multiplier: int = Field(ge=1)
    quote: RecordedQuote


class OptionEntryMoneyFacts(BaseModel):
    """What the entry was admitted on, in whole cents (``app.options.requirement.EntryMoney``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    net_entry_premium_cents: _Cents
    broker_requirement_cents: _Cents
    # What the bot set aside: requirement plus premium plus the fee provision.
    entry_commitment_cents: _Cents
    loss_at_expiry_cents: _Cents | None
    fee_provision_cents: _Cents = Field(ge=0)
    round_trip_cost_cents: _Cents = Field(ge=0)


class OptionOrderTermsFacts(BaseModel):
    """The owner's order terms as an entry keeps them: whole milliseconds (``app.options.deadlines.CloseTerms``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # How long the entry waits for its fill before what is left of it is cancelled.
    entry_wait_ms: _Milliseconds
    # For a cancel to be proven ended by a lookup of the order.
    cancel_allowance_ms: _Milliseconds
    entry_margin_ms: _Milliseconds
    # Before the session close on a held contract's expiry date.
    expiry_close_lead_ms: _Milliseconds
    # Before the close of every session a sold leg is held in.
    sold_leg_close_lead_ms: _Milliseconds

    @classmethod
    def of(cls, terms: CloseTerms) -> OptionOrderTermsFacts:
        return cls(
            entry_wait_ms=terms.entry_wait_ms,
            cancel_allowance_ms=terms.cancel_allowance_ms,
            entry_margin_ms=terms.entry_margin_ms,
            expiry_close_lead_ms=terms.expiry_close_lead_ms,
            sold_leg_close_lead_ms=terms.sold_leg_close_lead_ms,
        )

    @property
    def close_terms(self) -> CloseTerms:
        return CloseTerms(**self.model_dump())


class OptionsAttemptFacts(BaseModel):
    """A session leg allocation: immutable attribution beside the accepted order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg_id: str = Field(min_length=1, max_length=32)
    attempt_id: str = Field(min_length=1, max_length=128)
    setup_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # A separate portfolio admission reserves incremental collateral and paid
    # premium; sale proceeds remain unavailable until the account observes them.
    reservation_before_fees_cents: int = Field(ge=0)


def _leg_facts(prepared: PreparedEntry) -> tuple[OptionEntryLegFacts, ...]:
    """Each leg of a prepared entry as its facts keep it, in the structure's own leg order."""
    return tuple(
        OptionEntryLegFacts(
            leg_id=leg.leg_id,
            contract=leg.contract,
            position=leg.position,
            ratio=leg.ratio,
            multiplier=prepared.size.multiplier,
            quote=RecordedQuote.of(prepared.quotes[leg.leg_id]),
        )
        for leg in prepared.structure.legs
    )


def _money_facts(prepared: PreparedEntry, *, fee_provision_cents: int) -> OptionEntryMoneyFacts:
    return OptionEntryMoneyFacts(
        net_entry_premium_cents=prepared.money.net_entry_premium_cents,
        broker_requirement_cents=prepared.money.broker_requirement_cents,
        entry_commitment_cents=prepared.money.entry_commitment_cents,
        loss_at_expiry_cents=prepared.money.loss_at_expiry_cents,
        fee_provision_cents=fee_provision_cents,
        round_trip_cost_cents=prepared.round_trip_cost_cents,
    )


def _structure_of(legs: tuple[OptionEntryLegFacts, ...]) -> ResolvedStructure:
    return ResolvedStructure(
        legs=tuple(
            ResolvedLeg(leg_id=leg.leg_id, contract=leg.contract, position=leg.position, ratio=leg.ratio)
            for leg in legs
        )
    )


def _leg_terms_of(legs: tuple[OptionEntryLegFacts, ...]) -> tuple[ContractTerms, ...]:
    return tuple(ContractTerms(symbol=leg.contract.occ_symbol, multiplier=leg.multiplier) for leg in legs)


class OptionEntryFacts(BaseModel):
    """Everything kept with an accepted option entry. Never edited.

    ``cancel_at_ms`` and ``terms`` are kept with every entry accepted since
    an entry could rest at the broker. An entry accepted before that has
    neither, and is written back without them, so it hashes as it did.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The order as it was asked: one option order (ADR 0076). Read back from JSON, never from a decoded dict.
    order: SingleOptionOrderRequest
    legs: tuple[OptionEntryLegFacts, ...] = Field(min_length=1)
    # Structures opened.
    units: int = Field(ge=1)
    # Dollars a share for one structure: positive is paid.
    net_limit_price: Decimal
    money: OptionEntryMoneyFacts
    # The two limits the entry was held to.
    leg_gap_limit_percent: Decimal
    cost_limit_percent: Decimal
    # The subscription generation the quotes were read in, and when the caller read them.
    quote_generation: int
    rechecked_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    # The Clerk's own clock when it tested the quotes and worked out the money.
    admitted_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    # When the Clerk asks the broker to cancel what has not filled: the admission
    # time plus the owner's entry wait. The deadline is the entry's, never a leg's.
    cancel_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    terms: OptionOrderTermsFacts | None = None
    options_attempt: OptionsAttemptFacts | None = None

    @model_validator(mode="after")
    def _the_order_is_for_the_one_leg(self) -> OptionEntryFacts:
        if len(self.legs) != 1:
            # One order row trades one contract; several legs are several orders the books cannot yet tell apart.
            raise ValueError(f"an option entry of one order holds one leg; this one has {len(self.legs)}")
        if self.order.contract != self.legs[0].contract:
            raise ValueError("the order and the leg it opens name different contracts")
        return self

    @model_validator(mode="after")
    def _the_deadline_is_the_admission_plus_the_owners_wait(self) -> OptionEntryFacts:
        if (self.cancel_at_ms is None) != (self.terms is None):
            raise ValueError("an entry keeps its cancel time and the terms it came from together, or neither")
        if self.terms is not None and self.cancel_at_ms != self.admitted_at_ms + self.terms.entry_wait_ms:
            raise ValueError("an entry's cancel time is its admission time plus the owner's entry wait")
        return self

    @classmethod
    def of(
        cls,
        prepared: PreparedEntry,
        *,
        order: SingleOptionOrderRequest,
        fee_provision_cents: int,
        leg_gap_limit_percent: Decimal,
        cost_limit_percent: Decimal,
        quote_generation: int,
        rechecked_at_ms: int,
        admitted_at_ms: int,
        terms: CloseTerms,
    ) -> OptionEntryFacts:
        """The facts of one prepared entry and the order made of it.

        The entry's wait is counted from ``admitted_at_ms``, the Clerk's own
        clock: it is on the books before the order is sent, and the broker's
        acceptance time is not known then.
        """
        return cls(
            order=order,
            legs=_leg_facts(prepared),
            units=prepared.size.units,
            net_limit_price=prepared.net_limit_price,
            money=_money_facts(prepared, fee_provision_cents=fee_provision_cents),
            leg_gap_limit_percent=leg_gap_limit_percent,
            cost_limit_percent=cost_limit_percent,
            quote_generation=quote_generation,
            rechecked_at_ms=rechecked_at_ms,
            admitted_at_ms=admitted_at_ms,
            cancel_at_ms=admitted_at_ms + terms.entry_wait_ms,
            terms=OptionOrderTermsFacts.of(terms),
        )

    def to_facts(self) -> dict[str, Any]:
        """As the ``option`` key of the entry's facts: JSON's own values, every decimal a string.

        The cancel time and the terms are left out when the entry has none,
        so an entry accepted before it could rest is written as it was.
        """
        facts = self.model_dump(mode="json")
        if self.options_attempt is None:
            facts.pop("options_attempt")
        if self.terms is None:
            del facts["cancel_at_ms"], facts["terms"]
        return facts

    @classmethod
    def from_facts(cls, option: Mapping[str, Any]) -> OptionEntryFacts:
        # Through JSON: the order and its contract are strict and take no decoded dict.
        return cls.model_validate_json(canonicalize(option))

    @property
    def the_leg(self) -> OptionEntryLegFacts:
        return self.legs[0]

    @property
    def contract_terms(self) -> ContractTerms:
        """The entry's contract as custody counts it."""
        return ContractTerms(symbol=self.the_leg.contract.occ_symbol, multiplier=self.the_leg.multiplier)

    @property
    def leg_terms(self) -> tuple[ContractTerms, ...]:
        """Each leg's contract as custody counts it, in leg order: one, for this entry."""
        return _leg_terms_of(self.legs)

    @property
    def structure(self) -> ResolvedStructure:
        """The resolved structure the entry opened, as its exit closes it."""
        return _structure_of(self.legs)

    def order_line(self) -> dict[str, Any]:
        """The order in the keys a stock leg is written in, for every reader of an order's symbol and quantity.

        The contract's own symbol, the side, contracts, and the limit a
        share. The exact order is ``order``; this is its line in the books.
        """
        return {
            "symbol": self.order.contract.occ_symbol,
            "side": self.order.side.value,
            "quantity": float(self.order.quantity),
            "order_type": self.order.order_type.value,
            "limit_price": None if self.order.limit_price is None else float(self.order.limit_price),
            "time_in_force": "day",
            "extended_hours": False,
        }


class MultiLegOptionEntryFacts(BaseModel):
    """Everything kept with an accepted option entry of two to four legs. Never edited.

    The sibling of ``OptionEntryFacts``, which is one order of one contract.
    This is one order of several legs, sent and filled as one: the order as
    it was asked, each leg in the order's own leg order with the quote it
    was priced from, and the money for all the units. The net limit is
    signed: positive is paid, negative is received.

    No entry of several legs predates the cancel time and the terms, so
    both are always kept.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The order as it was asked: one order of several legs (ADR 0076). Read back from JSON, never from a decoded dict.
    order: MultiLegOptionOrderRequest
    # In the order's own leg order.
    legs: tuple[OptionEntryLegFacts, ...] = Field(min_length=2, max_length=MAX_OPTION_LEGS)
    # Structures opened.
    units: int = Field(ge=1)
    # Dollars a share for one structure: positive is paid, negative is received.
    net_limit_price: Decimal
    # For all the units.
    money: OptionEntryMoneyFacts
    leg_gap_limit_percent: Decimal
    cost_limit_percent: Decimal
    quote_generation: int
    rechecked_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    admitted_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    # When the Clerk asks the broker to cancel what has not filled. The deadline is the entry's, never a leg's.
    cancel_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    terms: OptionOrderTermsFacts

    @model_validator(mode="after")
    def _the_order_opens_these_legs_at_this_price(self) -> MultiLegOptionEntryFacts:
        if self.order.order_type is not OrderType.LIMIT or self.order.net_limit_price != self.net_limit_price:
            raise ValueError("an entry of several legs is one limit order at the entry's own net price")
        if self.order.quantity != self.units:
            raise ValueError(f"the order is for {self.order.quantity} structures and the entry for {self.units}")
        if len(self.order.legs) != len(self.legs):
            raise ValueError(f"the order has {len(self.order.legs)} legs and the entry keeps {len(self.legs)}")
        for asked, kept in zip(self.order.legs, self.legs, strict=True):
            opens = OrderSide.BUY if kept.position == "long" else OrderSide.SELL
            if (asked.contract, asked.side, asked.ratio, asked.intent) != (
                kept.contract, opens, kept.ratio, OptionPositionIntent.OPEN
            ):
                raise ValueError(f"the order's leg in {asked.contract.occ_symbol} is not the entry's leg {kept.leg_id!r}")
        if len({leg.leg_id for leg in self.legs}) != len(self.legs):
            raise ValueError("each leg of an entry has an id of its own")
        return self

    @model_validator(mode="after")
    def _the_deadline_is_the_admission_plus_the_owners_wait(self) -> MultiLegOptionEntryFacts:
        if self.cancel_at_ms != self.admitted_at_ms + self.terms.entry_wait_ms:
            raise ValueError("an entry's cancel time is its admission time plus the owner's entry wait")
        return self

    @classmethod
    def of(
        cls,
        prepared: PreparedEntry,
        *,
        order: MultiLegOptionOrderRequest,
        fee_provision_cents: int,
        leg_gap_limit_percent: Decimal,
        cost_limit_percent: Decimal,
        quote_generation: int,
        rechecked_at_ms: int,
        admitted_at_ms: int,
        terms: CloseTerms,
    ) -> MultiLegOptionEntryFacts:
        """The facts of one prepared entry of several legs and the order made of it.

        As for one leg, the wait is counted from ``admitted_at_ms``, the
        Clerk's own clock.
        """
        return cls(
            order=order,
            legs=_leg_facts(prepared),
            units=prepared.size.units,
            net_limit_price=prepared.net_limit_price,
            money=_money_facts(prepared, fee_provision_cents=fee_provision_cents),
            leg_gap_limit_percent=leg_gap_limit_percent,
            cost_limit_percent=cost_limit_percent,
            quote_generation=quote_generation,
            rechecked_at_ms=rechecked_at_ms,
            admitted_at_ms=admitted_at_ms,
            cancel_at_ms=admitted_at_ms + terms.entry_wait_ms,
            terms=OptionOrderTermsFacts.of(terms),
        )

    def to_facts(self) -> dict[str, Any]:
        """As the ``option`` key of the entry's facts: JSON's own values, every decimal a string."""
        return self.model_dump(mode="json")

    @classmethod
    def from_facts(cls, option: Mapping[str, Any]) -> MultiLegOptionEntryFacts:
        # Through JSON: the order and its contracts are strict and take no decoded dict.
        return cls.model_validate_json(canonicalize(option))

    @property
    def leg_terms(self) -> tuple[ContractTerms, ...]:
        """Each leg's contract as custody counts it, in leg order."""
        return _leg_terms_of(self.legs)

    @property
    def structure(self) -> ResolvedStructure:
        """The resolved structure the entry opened, as its exit closes it."""
        return _structure_of(self.legs)

    @property
    def unit_set_aside(self) -> Decimal:
        """What one structure sets aside before fees, in dollars: requirement plus premium at the limit.

        Formula: (broker requirement + net entry premium) / units, in whole
          cents a unit, written as dollars. Above zero for every admitted
          entry: the money rule refuses a commitment at or below zero.
        Reference: ADR 0076 decisions 2 and 5.
        Canonical implementation: ``app/options/requirement.py`` (the two
          figures); this property only divides them by the units.
        Validated against: tests/broker/alpaca/clerk/sqlite/test_several_legs_admission.py.

        This is what the entry's reservation keeps where an entry in shares
        keeps a price a share. It is not a price: nothing multiplies it by a
        contract's multiplier.
        """
        cents, remainder = divmod(self.money.broker_requirement_cents + self.money.net_entry_premium_cents, self.units)
        if remainder or cents <= 0:
            raise ValueError(
                f"an entry of {self.units} structures sets aside a whole number of cents a structure, above zero; "
                f"its requirement is {self.money.broker_requirement_cents} cents and its premium "
                f"{self.money.net_entry_premium_cents}"
            )
        return Decimal(cents).scaleb(-2)

    def order_line(self) -> dict[str, Any]:
        """The parent's own order line: no symbol, no side, whole structures, no limit (``multi_leg_parent_line``)."""
        return multi_leg_parent_line(units=self.units, order_type=self.order.order_type.value)


OptionEntry = OptionEntryFacts | MultiLegOptionEntryFacts


class OptionExitFacts(BaseModel):
    """The quotes read at an exit decision for the contract the EXIT closes, by OCC symbol."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quotes: dict[str, RecordedQuote]
    generation: int
    read_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)

    @classmethod
    def of(cls, evidence: OptionExitEvidence, *, closing: str | tuple[str, ...]) -> OptionExitFacts:
        """What an EXIT of the contract ``closing`` keeps of the quotes handed with it.

        A quote of any other contract is the caller's mistake: it could
        never price this close, and kept under this EXIT it would look as
        though it had been read for it.
        """
        symbols = (closing,) if isinstance(closing, str) else closing
        others = tuple(sorted(set(evidence.quotes) - set(symbols)))
        if others:
            raise ValueError(
                f"this EXIT closes {closing}; a quote was handed for {', '.join(others)}, which it does not close"
            )
        return cls(
            quotes={symbol: RecordedQuote.of(quote) for symbol, quote in evidence.quotes.items()},
            generation=evidence.generation,
            read_at_ms=evidence.read_at_ms,
        )

    def to_facts(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_facts(cls, option_exit: Mapping[str, Any]) -> OptionExitFacts:
        return cls.model_validate_json(canonicalize(option_exit))

    def quote_for(self, symbol: str) -> HeldQuote | None:
        """The quote handed for the contract ``symbol``, or ``None`` when none was."""
        recorded = self.quotes.get(symbol)
        return None if recorded is None else recorded.held


def option_entry_facts(facts: EnterAcceptedFacts) -> OptionEntry | None:
    """The option part of an entry's acceptance, or ``None`` for an entry in shares.

    Which type it is is told by the order's own shape: an order of several
    legs has ``legs``, and an order of one contract has none.
    """
    if facts.option is None:
        return None
    if "legs" in facts.option["order"]:
        return MultiLegOptionEntryFacts.from_facts(facts.option)
    return OptionEntryFacts.from_facts(facts.option)


def option_exit_facts(facts: ExitAcceptedFacts) -> OptionExitFacts | None:
    """The quotes an EXIT's decision was handed, or ``None`` when it was handed none."""
    return None if facts.option_exit is None else OptionExitFacts.from_facts(facts.option_exit)


def option_entry_of_order(repo: ClerkSqliteRepository, entry_order_ref: str) -> OptionEntry | None:
    """The option entry an ENTRY order was accepted as, or ``None`` when the order is in shares.

    For an entry of several legs the order is the parent. A leg's own row
    was accepted as nothing: it answers ``None``.
    """
    transition = repo.first_order_transition(order_ref=entry_order_ref, transition_kind="ENTER_ACCEPTED")
    if transition is None:
        return None
    return option_entry_facts(EnterAcceptedFacts.from_facts_json(transition["facts_json"]))


def order_contract(repo: ClerkSqliteRepository, order_ref: str) -> ContractTerms | None:
    """The option contract an order trades, or ``None`` for an order in shares."""
    with repo._write_lock:
        return order_contract_terms(repo._conn, order_ref)


def option_entry_contracts(repo: ClerkSqliteRepository, strategy_instance_id: str) -> tuple[str, ...]:
    """The contracts one bot's own entries opened, newest entry first, by OCC symbol.

    Every leg's contract, an entry's legs in their order. What an EXIT of
    an option bot is looked for among: the bot's books, never its plan's
    rules.
    """
    symbols: list[str] = []
    for order in reversed(repo.entry_orders_for_strategy(strategy_instance_id)):
        entry = option_entry_of_order(repo, order.order_ref)
        for terms in () if entry is None else entry.leg_terms:
            if terms.symbol not in symbols:
                symbols.append(terms.symbol)
    return tuple(symbols)


@dataclass(frozen=True, slots=True)
class HeldLeg:
    """What a bot holds of one leg of an entry, from the books."""

    leg_id: str
    # The contract's OCC symbol.
    symbol: str
    position: LegPosition
    ratio: int
    multiplier: int
    # Signed, from the books: bought above zero, sold below.
    held: int


@dataclass(frozen=True, slots=True)
class HeldOfEntry:
    """What a bot holds of one entry: each leg's contracts, and the whole structures they come to."""

    # The entry's own order: the parent of a structure.
    entry_order_ref: str
    # In the entry's leg order; one for a one-leg entry.
    legs: tuple[HeldLeg, ...]
    whole: WholeUnits


def _whole_contracts(quantity: float, *, what: str) -> int:
    """``quantity`` as a whole number of contracts. Refuses a fraction: the books are then wrong, and nothing is rounded."""
    if not float(quantity).is_integer():
        raise ValueError(f"{what} is {quantity!r}: an option contract is held and filled in whole contracts")
    return int(quantity)


def held_of_entry(repo: ClerkSqliteRepository, entry_order_ref: str) -> HeldOfEntry:
    """What the bot that made the option entry ``entry_order_ref`` holds of its contracts now. Raises for any other order.

    Read from the bot's own positions under the write fence, so every leg
    is of one moment. A position is by bot and contract: two entries of one
    bot in one contract are one holding, and are read as one here.
    """
    with repo.write_fence():
        order = repo.order(entry_order_ref)
        entry = None if order is None else option_entry_of_order(repo, entry_order_ref)
        if order is None or entry is None:
            raise ValueError(f"{entry_order_ref!r} is not an entry in option contracts")
        effect = repo.effect_operation(order.effect_operation_id)
        assert effect is not None and effect.strategy_instance_id is not None
        legs = tuple(
            HeldLeg(
                leg_id=leg.leg_id,
                symbol=leg.contract.occ_symbol,
                position=leg.position,
                ratio=leg.ratio,
                multiplier=leg.multiplier,
                held=_whole_contracts(
                    repo.position(effect.strategy_instance_id, leg.contract.occ_symbol),
                    what=f"the position in {leg.contract.occ_symbol}",
                ),
            )
            for leg in entry.legs
        )
    return HeldOfEntry(
        entry_order_ref=entry_order_ref,
        legs=legs,
        whole=whole_units([
            LegContracts(leg_id=leg.leg_id, ratio=leg.ratio, contracts=in_leg_direction(leg.held, leg.position))
            for leg in legs
        ]),
    )


def held_option_legs(repo: ClerkSqliteRepository, strategy_instance_id: str) -> dict[ContractAtMultiplier, int]:
    """What one bot holds of option contracts now: signed whole contracts by ``(OCC symbol, multiplier)``.

    Bought above zero and sold below, each contract at the multiplier the
    books recorded for it, which is the shape ``held_requirement_cents``
    takes. Read from the positions that are not zero under the write fence, so
    every contract is of one moment, and the bot's own entries are not read:
    a bot holds a contract only through an entry of its own. A bot holding
    none answers an empty mapping. Raises ``ValueError`` for a quantity that is
    not whole contracts, as ``held_of_entry`` does: the books are then wrong,
    and nothing is rounded.
    """
    with repo.write_fence() as conn:
        held = [
            position
            for position in reads.held_option_contracts(conn)
            if position.strategy_instance_id == strategy_instance_id and position_quantity_is_nonzero(position.quantity)
        ]
        recorded = {position.symbol: contract_terms(conn, position.symbol) for position in held}
    legs: dict[ContractAtMultiplier, int] = {}
    for position in held:
        terms = recorded[position.symbol]
        # ``held_option_contracts`` lists only contracts the books have recorded terms for.
        assert terms is not None, f"{position.symbol} is held with no recorded terms"
        legs[position.symbol, terms.multiplier] = _whole_contracts(
            position.quantity, what=f"the position in {position.symbol}"
        )
    return legs


__all__ = [
    "HeldLeg",
    "HeldOfEntry",
    "MultiLegOptionEntryFacts",
    "OptionEntry",
    "OptionEntryFacts",
    "OptionEntryLegFacts",
    "OptionEntryMoneyFacts",
    "OptionExitFacts",
    "OptionOrderTermsFacts",
    "held_of_entry",
    "held_option_legs",
    "option_entry_contracts",
    "option_entry_facts",
    "option_entry_of_order",
    "option_exit_facts",
    "order_contract",
]
