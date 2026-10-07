"""An option entry as the books keep it, and as its exit reads it back (ADR 0076).

An entry in an option contract is accepted under the same ``ENTER_ACCEPTED``
fact as an entry in shares, with one more key, ``option``, which this module
writes and reads. It holds what the entry was admitted on and what its exit
needs: the order as it was asked, the contract each leg resolved to with its
multiplier, the quote each leg was priced from, and the money in exact cents.

"Exit closes what entry opened" is read from here. The exit never asks the
plan's rules again: it reads the contract from the entry's own facts.

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
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.alpaca.clerk.option_effects import OptionExitEvidence, RecordedQuote
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms, keeps_option_contracts, order_contract_terms
from app.broker.alpaca.clerk.sqlite.facts import EnterAcceptedFacts, ExitAcceptedFacts
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.contract.models import OrderType
from app.broker.contract.options import SingleOptionOrderRequest, closing_order
from app.options.contract import OptionContract
from app.options.entry import PreparedEntry
from app.options.quotes import HeldQuote
from app.options.structure import LegPosition, ResolvedLeg, ResolvedStructure
from app.utils.session_anchors import MAX_TIMESTAMP_MS

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

_Cents = Annotated[int, Field(strict=True)]


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


class OptionEntryFacts(BaseModel):
    """Everything kept with an accepted option entry. Never edited."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The order as it was asked (slice 8's request). Read back from JSON, never from a decoded dict.
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

    @model_validator(mode="after")
    def _the_order_is_for_the_one_leg(self) -> OptionEntryFacts:
        if len(self.legs) != 1:
            # One order row trades one contract; several legs are several orders the books cannot yet tell apart.
            raise ValueError(f"an option entry of one order holds one leg; this one has {len(self.legs)}")
        if self.order.contract != self.legs[0].contract:
            raise ValueError("the order and the leg it opens name different contracts")
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
    ) -> OptionEntryFacts:
        """The facts of one prepared entry and the order made of it."""
        return cls(
            order=order,
            legs=tuple(
                OptionEntryLegFacts(
                    leg_id=leg.leg_id,
                    contract=leg.contract,
                    position=leg.position,
                    ratio=leg.ratio,
                    multiplier=prepared.size.multiplier,
                    quote=RecordedQuote.of(prepared.quotes[leg.leg_id]),
                )
                for leg in prepared.structure.legs
            ),
            units=prepared.size.units,
            net_limit_price=prepared.net_limit_price,
            money=OptionEntryMoneyFacts(
                net_entry_premium_cents=prepared.money.net_entry_premium_cents,
                broker_requirement_cents=prepared.money.broker_requirement_cents,
                entry_commitment_cents=prepared.money.entry_commitment_cents,
                loss_at_expiry_cents=prepared.money.loss_at_expiry_cents,
                fee_provision_cents=fee_provision_cents,
                round_trip_cost_cents=prepared.round_trip_cost_cents,
            ),
            leg_gap_limit_percent=leg_gap_limit_percent,
            cost_limit_percent=cost_limit_percent,
            quote_generation=quote_generation,
            rechecked_at_ms=rechecked_at_ms,
            admitted_at_ms=admitted_at_ms,
        )

    def to_facts(self) -> dict[str, Any]:
        """As the ``option`` key of the entry's facts: JSON's own values, every decimal a string."""
        return self.model_dump(mode="json")

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
    def structure(self) -> ResolvedStructure:
        """The resolved structure the entry opened, as its exit closes it."""
        return ResolvedStructure(
            legs=tuple(
                ResolvedLeg(leg_id=leg.leg_id, contract=leg.contract, position=leg.position, ratio=leg.ratio)
                for leg in self.legs
            )
        )

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

    def close_request(self, *, contracts: float) -> SingleOptionOrderRequest:
        """The one market order that closes ``contracts`` of what this entry opened.

        ``contracts`` is the position confirmed when the close was made. It
        is a whole number of contracts or the books are wrong, and the close
        is refused rather than rounded.
        """
        units, remainder = divmod(contracts, self.the_leg.ratio)
        if remainder or units < 1:
            raise ValueError(f"a close is for a whole number of structures, one or more; the books hold {contracts}")
        order = closing_order(self.structure, units=int(units))
        assert isinstance(order, SingleOptionOrderRequest) and order.order_type is OrderType.MARKET
        return order


class OptionExitFacts(BaseModel):
    """The quotes read at an exit decision for the contracts the bot holds, by leg id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quotes: dict[str, RecordedQuote]
    generation: int
    read_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)

    @classmethod
    def of(cls, evidence: OptionExitEvidence) -> OptionExitFacts:
        return cls(
            quotes={leg_id: RecordedQuote.of(quote) for leg_id, quote in evidence.quotes.items()},
            generation=evidence.generation,
            read_at_ms=evidence.read_at_ms,
        )

    def to_facts(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_facts(cls, option_exit: Mapping[str, Any]) -> OptionExitFacts:
        return cls.model_validate_json(canonicalize(option_exit))

    def quote_for(self, leg_id: str) -> HeldQuote | None:
        recorded = self.quotes.get(leg_id)
        return None if recorded is None else recorded.held


def option_entry_facts(facts: EnterAcceptedFacts) -> OptionEntryFacts | None:
    """The option part of an entry's acceptance, or ``None`` for an entry in shares."""
    return None if facts.option is None else OptionEntryFacts.from_facts(facts.option)


def option_exit_facts(facts: ExitAcceptedFacts) -> OptionExitFacts | None:
    """The quotes an EXIT's decision was handed, or ``None`` when it was handed none."""
    return None if facts.option_exit is None else OptionExitFacts.from_facts(facts.option_exit)


def option_entry_of_order(repo: ClerkSqliteRepository, entry_order_ref: str) -> OptionEntryFacts | None:
    """The option entry an ENTRY order was accepted as, or ``None`` when the order is in shares."""
    transition = repo.first_order_transition(order_ref=entry_order_ref, transition_kind="ENTER_ACCEPTED")
    if transition is None:
        return None
    return option_entry_facts(EnterAcceptedFacts.from_facts_json(transition["facts_json"]))


def order_contract(repo: ClerkSqliteRepository, order_ref: str) -> ContractTerms | None:
    """The option contract an order trades, or ``None`` for an order in shares."""
    with repo._write_lock:
        # A custody file from before the contract tables holds no option order.
        return order_contract_terms(repo._conn, order_ref) if keeps_option_contracts(repo._conn) else None


def option_entry_contracts(repo: ClerkSqliteRepository, strategy_instance_id: str) -> tuple[str, ...]:
    """The contracts one bot's own entries opened, newest entry first, by OCC symbol.

    What an EXIT of an option bot is looked for among: the bot's books,
    never its plan's rules.
    """
    symbols: list[str] = []
    for order in reversed(repo.entry_orders_for_strategy(strategy_instance_id)):
        entry = option_entry_of_order(repo, order.order_ref)
        if entry is not None and entry.contract_terms.symbol not in symbols:
            symbols.append(entry.contract_terms.symbol)
    return tuple(symbols)


__all__ = [
    "OptionEntryFacts",
    "OptionEntryLegFacts",
    "OptionEntryMoneyFacts",
    "OptionExitFacts",
    "option_entry_contracts",
    "option_entry_facts",
    "option_entry_of_order",
    "option_exit_facts",
    "order_contract",
]
