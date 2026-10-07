"""Option orders, the option contract reference and the account's option fields (broker-neutral).

These are new types beside the stock ones in ``models.py``, which this module
leaves alone: a stock leg, and every hash taken over one, is what it was
(ADR 0076).

Three things differ from the stock models, on purpose:

- **A contract is an ``OptionContract``**, never a symbol this module checks
  itself. The symbol an order carries is that contract's ``occ_symbol``.
- **A quantity is a whole number and a price is an exact ``Decimal``.** The
  stock models hold both as ``float`` because they began as a display
  surface. An option order's price is compared with what was set aside for
  it, in exact cents, so no float sits between the two.
- **An option order is for the day and for regular hours.** A request has no
  field for either, so neither can be asked for.

Time is ``int64`` ms UTC, as everywhere. An expiry is its date's 16:00 New
York anchor (ADR 0022 (i)).

Formula: a multi-leg order's limit price = the structure's net price a share,
  Σ s_i · r_i · price_i with s_i = +1 for a bought leg and -1 for a sold one:
  positive is paid, negative is received. A one-leg order's limit price is
  that net price's size, above zero; its side says which way the money goes.
  Leg ratios r_i must have a greatest common divisor of one.
Reference: Alpaca's Trading API documents, read 2026-10-06.
  Orders, and the sign of a multi-leg ``limit_price``:
    https://docs.alpaca.markets/reference/postorder
  Multi-leg orders, and the divisor rule:
    https://docs.alpaca.markets/docs/options-level-3-trading
  Option orders: https://docs.alpaca.markets/docs/options-trading
  Contracts: https://docs.alpaca.markets/reference/get-option-contract-symbol_or_id
  Account: https://docs.alpaca.markets/reference/getaccount-1
Canonical implementation: the net price is ``app/options/requirement.py``
  (``natural_net_price``); this file carries it into an order and changes
  nothing but a lone sold leg's sign.
Validated against: tests/broker/contract/test_options.py, and on the wire
  tests/broker/alpaca/test_adapter_option_orders.py, held to Alpaca's
  published examples. No order has been sent to Alpaca: the sign of a credit
  is its reference's sentence, not yet a recording.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from math import gcd
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.contract.models import OrderSide, OrderType
from app.options.contract import OptionContract, OptionRight
from app.options.exact import require_everyday
from app.options.expiry import OptionExpiryMs
from app.options.structure import MAX_OPTION_LEGS, LegPosition, ResolvedStructure

# What a standard stock or ETF option delivers, and its price multiplier.
STANDARD_SHARES_PER_CONTRACT = 100


class OptionPositionIntent(StrEnum):
    """Whether a leg opens a position or closes one.

    A closing leg says the order is meant only to reduce what is held, so
    that a repeated exit does not open the reverse position. That the broker
    refuses a closing leg beyond what is held is not in its documents and is
    still to be shown on the account (#2913).
    """

    OPEN = "open"
    CLOSE = "close"


# ── Requests ────────────────────────────────────────────────────────────────


class _OptionRequestModel(BaseModel):
    """Base for a request: frozen, and strict, so no number or side is coerced into an order.

    A quantity is an ``int``: ``True``, ``1.0`` and ``"1"`` are refused. A
    price is a ``Decimal``: a float, an int and a string are refused. A side
    and an intent are their enums, never the bare word, and the legs are a
    tuple. Strict mode still takes two things as given: the order type's own
    word (``"limit"`` equals its enum member, and is stored as the member),
    and a mapping where a contract goes, which is built into an
    ``OptionContract`` by that type's own rules.

    Read a stored request back with ``model_validate_json``, which reads
    JSON's own strings as the enums and decimals they name.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _require_price_matches_order_type(order_type: OrderType, price: Decimal | None, *, what: str) -> None:
    if order_type is OrderType.LIMIT and price is None:
        raise ValueError(f"a limit order requires {what}")
    if order_type is OrderType.MARKET and price is not None:
        raise ValueError(f"a market order has no price limit and must not carry {what}")
    if price is not None:
        require_everyday(price, what=what)


class SingleOptionOrderRequest(_OptionRequestModel):
    """One option contract, bought or sold: a plain option order.

    A structure of one leg is sent as this, never as a multi-leg order of one.
    """

    contract: OptionContract
    side: OrderSide
    intent: OptionPositionIntent
    # Whole contracts.
    quantity: Annotated[int, Field(ge=1)]
    order_type: Literal[OrderType.MARKET, OrderType.LIMIT]
    # Dollars a share, above zero. The side says whether it is paid or received.
    limit_price: Decimal | None = None

    @model_validator(mode="after")
    def _a_limit_has_a_price_above_zero(self) -> SingleOptionOrderRequest:
        _require_price_matches_order_type(self.order_type, self.limit_price, what="a limit price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError(f"an option's limit price is above zero; got {self.limit_price}")
        return self


class OptionLegRequest(_OptionRequestModel):
    """One leg of a multi-leg order."""

    contract: OptionContract
    side: OrderSide
    intent: OptionPositionIntent
    # Contracts of this leg in one structure.
    ratio: Annotated[int, Field(ge=1)]


class MultiLegOptionOrderRequest(_OptionRequestModel):
    """Two to four option legs on one underlying, sent and filled as one order.

    ``net_limit_price`` is signed as Alpaca signs it: **positive is a debit,
    what the order may pay; negative is a credit, what it must receive**
    (orders reference, ``limit_price``: "A positive value indicates a debit
    ... A negative value signifies a credit"). It is dollars a share for one
    structure, the sign ``app.options.requirement.natural_net_price`` returns,
    so a price goes from there to the order with no change of sign.

    The price step the broker accepts is not stated in the documents read, so
    none is applied here: the order carries the exact decimal it is given.
    """

    legs: Annotated[tuple[OptionLegRequest, ...], Field(min_length=2, max_length=MAX_OPTION_LEGS)]
    # Whole structures. A leg's contracts are this times its ratio.
    quantity: Annotated[int, Field(ge=1)]
    order_type: Literal[OrderType.MARKET, OrderType.LIMIT]
    net_limit_price: Decimal | None = None

    @model_validator(mode="after")
    def _one_order_the_broker_takes(self) -> MultiLegOptionOrderRequest:
        symbols = [leg.contract.occ_symbol for leg in self.legs]
        if len(set(symbols)) != len(symbols):
            raise ValueError(f"a multi-leg order names each contract in one leg; these repeat: {sorted(symbols)}")
        roots = sorted({leg.contract.root for leg in self.legs})
        if len(roots) != 1:
            raise ValueError(f"every leg of a multi-leg order is on one underlying; these are on {roots}")
        ratios = [leg.ratio for leg in self.legs]
        if gcd(*ratios) != 1:
            # Alpaca refuses ratios that share a divisor: 2:4 is 1:2 at twice the quantity.
            raise ValueError(f"leg ratios must share no divisor above one; got {ratios}")
        _require_price_matches_order_type(self.order_type, self.net_limit_price, what="a net limit price")
        if self.net_limit_price is not None:
            self._require_a_sign_the_legs_can_have(self.net_limit_price)
        return self

    def _require_a_sign_the_legs_can_have(self, price: Decimal) -> None:
        if price == 0:
            # The reference describes a debit and a credit, and nothing between.
            raise ValueError("a net limit price is a debit above zero or a credit below it, never zero")
        sides = {leg.side for leg in self.legs}
        if sides == {OrderSide.BUY} and price < 0:
            raise ValueError(f"an order that only buys pays; a net limit price of {price} says it receives")
        if sides == {OrderSide.SELL} and price > 0:
            raise ValueError(f"an order that only sells receives; a net limit price of {price} says it pays")


OptionOrderRequest = SingleOptionOrderRequest | MultiLegOptionOrderRequest

_OPENING_SIDE: dict[LegPosition, OrderSide] = {"long": OrderSide.BUY, "short": OrderSide.SELL}
_CLOSING_SIDE: dict[LegPosition, OrderSide] = {"long": OrderSide.SELL, "short": OrderSide.BUY}


def _require_units(units: int) -> int:
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError(f"an order is for a whole number of structures, one or more; got {units!r}")
    return units


def _the_one_leg_contract_and_position(structure: ResolvedStructure) -> tuple[OptionContract, LegPosition]:
    (leg,) = structure.legs
    if leg.ratio != 1:
        # More contracts of a lone leg are more structures, not a ratio.
        raise ValueError(f"a structure of one leg holds one contract a structure; leg {leg.leg_id!r} has {leg.ratio}")
    return leg.contract, leg.position


def opening_order(structure: ResolvedStructure, *, units: int, net_limit_price: Decimal) -> OptionOrderRequest:
    """The limit order that opens ``units`` of a resolved structure at one net price.

    ``net_limit_price`` is dollars a share for one structure, positive when
    the entry pays and negative when it receives. Each leg's contract, side
    and ratio are read from the structure, so the order cannot differ from it.
    """
    _require_units(units)
    require_everyday(net_limit_price, what="a net limit price")
    if len(structure.legs) > 1:
        return MultiLegOptionOrderRequest(
            legs=tuple(
                OptionLegRequest(
                    contract=leg.contract,
                    side=_OPENING_SIDE[leg.position],
                    intent=OptionPositionIntent.OPEN,
                    ratio=leg.ratio,
                )
                for leg in structure.legs
            ),
            quantity=units,
            order_type=OrderType.LIMIT,
            net_limit_price=net_limit_price,
        )
    contract, position = _the_one_leg_contract_and_position(structure)
    # A bought option is paid for and a sold one paid to us; the side carries that, so the price is unsigned.
    # ``copy_negate`` and not unary minus, which rounds to the ambient decimal context's precision.
    paid = net_limit_price if position == "long" else net_limit_price.copy_negate()
    if paid <= 0:
        raise ValueError(
            f"a {position} option {'pays' if position == 'long' else 'receives'} its premium; "
            f"a net limit price of {net_limit_price} says otherwise"
        )
    return SingleOptionOrderRequest(
        contract=contract,
        side=_OPENING_SIDE[position],
        intent=OptionPositionIntent.OPEN,
        quantity=units,
        order_type=OrderType.LIMIT,
        limit_price=paid,
    )


def closing_order(structure: ResolvedStructure, *, units: int) -> OptionOrderRequest:
    """The market order that closes ``units`` of a resolved structure: every side reversed, every leg closing.

    A market order has no price limit. All the legs go in one order; a
    structure is never closed a leg at a time.
    """
    _require_units(units)
    if len(structure.legs) > 1:
        return MultiLegOptionOrderRequest(
            legs=tuple(
                OptionLegRequest(
                    contract=leg.contract,
                    side=_CLOSING_SIDE[leg.position],
                    intent=OptionPositionIntent.CLOSE,
                    ratio=leg.ratio,
                )
                for leg in structure.legs
            ),
            quantity=units,
            order_type=OrderType.MARKET,
        )
    contract, position = _the_one_leg_contract_and_position(structure)
    return SingleOptionOrderRequest(
        contract=contract,
        side=_CLOSING_SIDE[position],
        intent=OptionPositionIntent.CLOSE,
        quantity=units,
        order_type=OrderType.MARKET,
    )


# ── What the broker says back ───────────────────────────────────────────────


class _OptionEvidenceModel(BaseModel):
    """Base for what a broker reported: frozen, strict, and unknown fields are an adapter's typo.

    Strict as a request is: an adapter hands over the ``int`` or ``Decimal``
    it read, and ``True``, ``"2"`` or a float is refused, never read as a
    quantity or a price.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _require_filled_within_quantity(filled: int, quantity: int | None, *, what: str) -> None:
    """Refuse a fill larger than its order: untrue whatever the two numbers count."""
    if quantity is not None and filled > quantity:
        raise ValueError(f"{what} cannot have filled {filled} of a quantity of {quantity}")


class BrokerOptionOrderLeg(_OptionEvidenceModel):
    """One leg of a multi-leg order as the broker reports it: an order of its own, under the parent."""

    # The leg's own broker order id, never the parent's.
    order_id: str = Field(min_length=1)
    client_order_id: str | None
    contract: OptionContract
    side: OrderSide
    # ``None`` when the broker's answer does not say.
    intent: OptionPositionIntent | None
    ratio: int | None = Field(ge=1)
    # ``quantity`` is ``None`` when the answer does not say.
    quantity: int | None = Field(ge=0)
    filled_quantity: int = Field(ge=0)
    # Dollars a share, as the broker reports this leg's fills.
    filled_avg_price: Decimal | None
    status: str = Field(min_length=1)
    submitted_at_ms: int | None
    created_at_ms: int | None
    updated_at_ms: int | None
    filled_at_ms: int | None
    canceled_at_ms: int | None
    expired_at_ms: int | None

    @model_validator(mode="after")
    def _no_more_filled_than_ordered(self) -> BrokerOptionOrderLeg:
        _require_filled_within_quantity(self.filled_quantity, self.quantity, what="an option order leg")
        return self


class BrokerOptionOrder(_OptionEvidenceModel):
    """An option order and its status, with the legs nested under a multi-leg parent.

    A single-leg order carries its own ``contract`` and ``side``, its
    ``intent`` when the broker's answer states one (``None`` when it does
    not), and no ``legs``. A multi-leg parent has none of the three and two
    to four ``legs``, each on a contract of its own. Its ``limit_price`` and
    ``filled_avg_price`` are net, a share, signed as the broker signs them.

    A parent's ``quantity`` and ``filled_quantity`` count structures. A
    leg's are the broker's own numbers for that leg: in Alpaca's one
    published answer they are contracts, for an order of one structure, and
    what it writes for more is not yet recorded (#2913). So no leg is held
    to its ratio times the parent's quantity. What is held is true whatever
    the units: nothing fills beyond its quantity, and a single order is for
    one contract or more.
    """

    broker: str
    # The parent's broker order id: what a cancel names.
    order_id: str = Field(min_length=1)
    client_order_id: str | None
    multi_leg: bool
    contract: OptionContract | None
    side: OrderSide | None
    intent: OptionPositionIntent | None
    order_type: str = Field(min_length=1)
    time_in_force: str = Field(min_length=1)
    quantity: int = Field(ge=0)
    filled_quantity: int = Field(ge=0)
    limit_price: Decimal | None
    filled_avg_price: Decimal | None
    status: str = Field(min_length=1)
    submitted_at_ms: int | None
    created_at_ms: int | None
    updated_at_ms: int | None
    filled_at_ms: int | None
    canceled_at_ms: int | None
    expired_at_ms: int | None
    legs: tuple[BrokerOptionOrderLeg, ...] = ()
    observed_at_ms: int

    @model_validator(mode="after")
    def _a_parent_with_legs_or_one_contract(self) -> BrokerOptionOrder:
        if self.multi_leg:
            if not 2 <= len(self.legs) <= MAX_OPTION_LEGS:
                raise ValueError(f"a multi-leg order has two to {MAX_OPTION_LEGS} legs; this one has {len(self.legs)}")
            if self.contract is not None or self.side is not None or self.intent is not None:
                raise ValueError("a multi-leg parent names no contract, side or intent of its own")
            leg_ids = [leg.order_id for leg in self.legs]
            if len({self.order_id, *leg_ids}) != len(leg_ids) + 1:
                raise ValueError("a multi-leg parent and each of its legs has an order id of its own")
            symbols = [leg.contract.occ_symbol for leg in self.legs]
            if len(set(symbols)) != len(symbols):
                raise ValueError(f"a multi-leg order has each contract in one leg; these repeat: {sorted(symbols)}")
        elif self.legs or self.contract is None or self.side is None:
            raise ValueError("a single option order names its contract and side and has no legs")
        elif self.quantity < 1:
            raise ValueError("a single option order is for one contract or more")
        _require_filled_within_quantity(self.filled_quantity, self.quantity, what="an option order")
        return self


# ── The contract reference ──────────────────────────────────────────────────


class BrokerOptionDeliverable(_OptionEvidenceModel):
    """One thing a contract delivers when it is exercised or assigned."""

    # ``equity`` or ``cash``, in the broker's word, lowercase as its reference lists them.
    kind: str
    symbol: str
    # Shares, or dollars for cash. ``None`` while the broker has not settled the amount.
    amount: Decimal | None


class BrokerOptionContract(_OptionEvidenceModel):
    """One option contract as the broker lists it.

    ``root_symbol`` and ``underlying_symbol`` are carried as the broker
    writes them. They differ for a ticker with a share class, whose contract
    root drops the dot; nothing here matches one to the other.
    """

    broker: str
    contract_id: str
    symbol: str
    name: str | None
    # ``active`` or ``inactive``, in the broker's word, lowercase as its reference lists them.
    status: str
    tradable: bool
    # The expiry date's 16:00 New York anchor.
    expiry_ms: OptionExpiryMs
    root_symbol: str | None
    underlying_symbol: str
    underlying_asset_id: str | None
    right: OptionRight
    # ``american`` or ``european``, in the broker's word, lowercase as its reference lists them.
    style: str
    strike_price: Decimal
    # What a price is multiplied by to give dollars a contract.
    multiplier: Decimal
    # Shares delivered a contract.
    shares_per_contract: Decimal
    # Whether the contract trades in penny steps. ``None`` when the answer does not say.
    penny_program: bool | None
    # ``None`` when the answer carries no list of deliverables.
    deliverables: tuple[BrokerOptionDeliverable, ...] | None
    observed_at_ms: int


class OptionContractRefusal(StrEnum):
    """Why a bot may not trade an option contract at the broker."""

    # The broker lists no such contract.
    NOT_LISTED = "OPTION_CONTRACT_NOT_LISTED"
    # The broker's answer describes a different contract from the one asked about.
    NOT_THE_CONTRACT_ASKED_FOR = "OPTION_CONTRACT_NOT_THE_CONTRACT_ASKED_FOR"
    NOT_ACTIVE = "OPTION_CONTRACT_NOT_ACTIVE"
    NOT_TRADABLE = "OPTION_CONTRACT_NOT_TRADABLE"
    NOT_AMERICAN = "OPTION_CONTRACT_NOT_AMERICAN"
    # It does not deliver 100 shares of its underlying, or its multiplier is not 100.
    NOT_STANDARD = "OPTION_CONTRACT_NOT_STANDARD"


class OptionContractRefused(ValueError):
    """A typed refusal of one contract, by its symbol. Spelled as the structure's refusals are."""

    def __init__(self, reason: OptionContractRefusal, symbol: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.symbol = symbol


def _is_standard_deliverable(listing: BrokerOptionContract) -> bool:
    if listing.multiplier != STANDARD_SHARES_PER_CONTRACT or listing.shares_per_contract != STANDARD_SHARES_PER_CONTRACT:
        return False
    if listing.deliverables is None:
        return True
    if len(listing.deliverables) != 1:
        return False
    (deliverable,) = listing.deliverables
    return (
        deliverable.kind == "equity"
        and deliverable.symbol == listing.underlying_symbol
        and deliverable.amount == STANDARD_SHARES_PER_CONTRACT
    )


def option_contract_refusal(
    contract: OptionContract, listing: BrokerOptionContract | None
) -> OptionContractRefused | None:
    """Why a bot may not trade ``contract`` at the broker, or ``None`` when it may.

    ``listing`` is the broker's answer about that contract; ``None`` is the
    broker not listing it. The one rule for every place that asks before an
    order can exist: the contract is listed, is the one asked about, is
    active and tradable, is American, and delivers 100 shares of its
    underlying at a multiplier of 100. The first failure is the reason given.

    The broker's words are compared as its reference writes them:
    ``active``, ``american`` and ``equity``, lowercase. Any other spelling is
    a word the reference does not list, and is refused as not being that one.

    The caller raises the refusal or reads it. A share-class ticker is not
    matched to its root here: the listing's root must be the symbol's own.
    """
    symbol = contract.occ_symbol
    if listing is None:
        return OptionContractRefused(
            OptionContractRefusal.NOT_LISTED, symbol, f"The broker lists no option contract {symbol}."
        )
    same_contract = (
        listing.symbol == symbol
        and listing.expiry_ms == contract.expiry_ms
        and listing.right == contract.right
        and listing.strike_price == contract.strike
        and listing.root_symbol in (None, contract.root)
    )
    if not same_contract:
        return OptionContractRefused(
            OptionContractRefusal.NOT_THE_CONTRACT_ASKED_FOR,
            symbol,
            f"Asked about {symbol}, the broker described {listing.symbol or 'a contract with no symbol'}: "
            f"a {listing.right} struck at {listing.strike_price} on root {listing.root_symbol!r}.",
        )
    if listing.status != "active":
        return OptionContractRefused(
            OptionContractRefusal.NOT_ACTIVE, symbol, f"The broker lists {symbol} as {listing.status}, not active."
        )
    if not listing.tradable:
        return OptionContractRefused(
            OptionContractRefusal.NOT_TRADABLE, symbol, f"The broker lists {symbol} as not tradable."
        )
    if listing.style != "american":
        return OptionContractRefused(
            OptionContractRefusal.NOT_AMERICAN, symbol, f"{symbol} is {listing.style} style, not American."
        )
    if not _is_standard_deliverable(listing):
        return OptionContractRefused(
            OptionContractRefusal.NOT_STANDARD,
            symbol,
            f"{symbol} is not a standard contract: multiplier {listing.multiplier}, "
            f"{listing.shares_per_contract} shares a contract"
            + ("." if listing.deliverables is None else f", delivering {_deliverables_in_words(listing)}."),
        )
    return None


def _deliverables_in_words(listing: BrokerOptionContract) -> str:
    assert listing.deliverables is not None
    if not listing.deliverables:
        return "nothing"
    return " and ".join(
        f"{'an unsettled amount' if item.amount is None else item.amount} of {item.symbol} ({item.kind})"
        for item in listing.deliverables
    )


# ── The account's option fields ─────────────────────────────────────────────

# The option approval levels Alpaca documents: 0 disabled, 1 covered calls and
# cash-secured puts, 2 bought calls and puts, 3 spreads and straddles.
_OptionLevel = Annotated[int, Field(ge=0, le=3)]


class BrokerOptionAccountSnapshot(_OptionEvidenceModel):
    """What the account may do in options and has to spend on them, as the broker reports it.

    Each of the three is ``None`` when the broker's answer does not carry it.
    Absent is unknown, never zero: a level of zero is the broker saying
    options are off.

    These are read apart from ``BrokerAccountSnapshot`` so that model, which a
    route serves, is unchanged.
    """

    broker: str
    account_id: str
    account_mode: Literal["paper", "live"]
    # Dollars, exact as the broker wrote them.
    options_buying_power: Decimal | None
    # The level the broker approved the account for.
    options_approved_level: _OptionLevel | None
    # The level in force: the approved level, or a lower one the owner set.
    options_trading_level: _OptionLevel | None
    observed_at_ms: int
