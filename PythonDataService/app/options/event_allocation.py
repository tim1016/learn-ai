"""Which lots an assignment, an exercise or an expiry takes, and what it does to each owner's books.

Three things end an option position without an order of ours: a sold option
is assigned, a bought option is exercised, or an option expires worthless.
The broker reports each against the account, by contract and by count. This
says whose lots it took, and what each owner's books then show (ADR 0076).

Formula: with the event's ``n`` contracts, the multiplier ``m``, the strike
  ``K``, and the lots it may take sorted oldest first, each holding ``q_j``
  contracts (bought positive, sold negative):
  taken_j  = min(|q_j|, n - Σ taken_i for i < j),  refused unless Σ taken_j = n
  and for ``t`` contracts taken from one lot:
  option position change = -t for a bought lot, +t for a sold one, at a price of zero
  shares = +t·m when a call is exercised or a put is assigned (shares bought)
           -t·m when a put is exercised or a call is assigned (shares sold)
            0   when the option expires
  cash   = -shares · K   (negative is paid, positive is received)
Reference: Alpaca, "Non-Trade Activities for Option Events",
  https://docs.alpaca.markets/docs/non-trade-activities-for-option-events
  (page dated 2025-09-24, read 2026-10-06): two calls exercised buy 200
  shares "at a per-share amount of $150 (strike price)" for -30000, two
  calls assigned sell 200 for 30000, and an option that expires out of the
  money is flattened with "no further action". The put directions are from
  Alpaca's Broker API page, "Options Trading Overview",
  https://docs.alpaca.markets/docs/options-trading-overview (page dated
  2026-09-16, read 2026-10-06): "A long put exercise results in selling the
  underlying stock at the strike price", and a put's seller has "the
  obligation to buy".
Canonical implementation: this file.
Validated against: tests/options/test_event_allocation.py, which holds the
  shares and cash to every pair of activities Alpaca publishes
  (tests/fixtures/alpaca_documented/option_activities) and to hand-derived
  cases, bit-exact.

What is Alpaca's documented behaviour, and what is not:

- **Documented:** the option position is removed with no cash of its own (the
  event's ``net_amount`` is "0"), and the shares trade at the strike, 100 a
  contract in every example. So the lot closes at a price of zero. The
  premium paid or received when it opened stays in the lot's cost; nothing
  here moves it into the shares' cost.
- **The contract's standard meaning, not a page:** that a contract moves
  ``m`` shares, for a multiplier other than 100. The multiplier here is both
  the shares a contract delivers and what its strike is multiplied by. That
  is true of a standard contract, the only kind a bot trades.
- **This repo's rule (#2913):** the oldest lot is taken first when more than
  one holds the contract, and a lot opened on a later New York date than the
  event is passed over, because it did not exist when the event happened.
  The broker reports an event against the account and names no lot.

Shares sold are sold whether or not the owner held them. A call assigned to
a bot with no shares leaves that bot short the stock, and its books must show
the short position: ``share_quantity_change`` is negative and nothing here
nets it against anything.

Every figure is a whole number of contracts, shares or cents, and no step
rounds. Cash that does not come to whole cents is refused.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.options.contract import OptionContract
from app.utils.session_anchors import et_date_at_ms

# A strike is in thousandths of a dollar; ten of them make a cent.
_THOUSANDTHS_PER_CENT = 10

# What an event closes a lot at, in dollars a share. The premium stays in the lot's cost.
EVENT_CLOSE_PRICE = Decimal(0)


class OptionEventKind(StrEnum):
    """What ended the position."""

    # A sold option was assigned: its seller must deliver on it.
    ASSIGNED = "assigned"
    # A bought option was exercised, by its holder or by the broker at expiry.
    EXERCISED = "exercised"
    # The option expired with nothing exercised.
    EXPIRED = "expired"


class OptionEventRefusal(StrEnum):
    """Why an event cannot be matched to the lots on the books."""

    # No lot of the contract is on any bot's books.
    CONTRACT_NOT_HELD = "OPTION_EVENT_CONTRACT_NOT_HELD"
    # Lots of the contract are held, and none on the side this kind of event ends.
    WRONG_SIDE = "OPTION_EVENT_WRONG_SIDE"
    # The event is for more contracts than the lots it may take hold.
    MORE_CONTRACTS_THAN_HELD = "OPTION_EVENT_MORE_CONTRACTS_THAN_HELD"
    # The broker did not report the event as executed. Raised where a broker's activity is allocated.
    NOT_EXECUTED = "OPTION_EVENT_NOT_EXECUTED"


class OptionEventRefused(ValueError):
    """A typed refusal to match an event to lots. Nothing is allocated: an event is matched whole or not at all."""

    def __init__(self, reason: OptionEventRefusal, detail: str, *, unmatched_contracts: int) -> None:
        super().__init__(detail)
        self.reason = reason
        # How many of the event's contracts found no lot.
        self.unmatched_contracts = unmatched_contracts


def _require_whole(name: str, value: object, *, at_least: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < at_least:
        raise ValueError(f"{name} must be a whole number, {at_least} or more; got {value!r}")


@dataclass(frozen=True, slots=True)
class HeldOptionLot:
    """One open lot of one contract on one owner's books."""

    # Whose books hold it: the bot.
    owner: str
    contract: OptionContract
    # Contracts still open: bought is positive, sold is negative, never zero.
    quantity: int
    # When the lot was opened, ``int64`` ms UTC.
    opened_at_ms: int
    # The lot's place in the books' own record, lower is earlier. It breaks a
    # tie between lots opened in the same millisecond, and with the owner it
    # names the lot.
    sequence: int

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not self.owner.strip():
            raise ValueError(f"a lot belongs to a named owner; got {self.owner!r}")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity == 0:
            raise ValueError(f"an open lot holds a whole number of contracts, never zero; got {self.quantity!r}")
        _require_whole("a lot's opening time", self.opened_at_ms, at_least=0)
        _require_whole("a lot's sequence", self.sequence, at_least=0)


@dataclass(frozen=True, slots=True)
class ShareTrade:
    """The trade in the underlying that an exercise or an assignment creates."""

    # The contract's root, as its symbol writes it. A share-class ticker is not matched to it here.
    root: str
    # Shares bought (positive) or sold (negative).
    shares: int
    # Dollars a share: the strike.
    price: Decimal
    # Whole cents paid (negative) or received (positive).
    cash_cents: int


@dataclass(frozen=True, slots=True)
class LotAllocation:
    """What an event takes from one lot, and what that does to its owner's books."""

    owner: str
    sequence: int
    opened_at_ms: int
    # Contracts taken from the lot: one or more, never more than it held.
    contracts: int
    # What the lot still holds, signed as the lot is. Zero when it was taken whole.
    remaining_quantity: int
    # The change to the owner's position in the contract, closed at ``EVENT_CLOSE_PRICE``.
    option_quantity_change: int
    # Shares the owner's books gain (positive) or give up (negative). Zero for an expiry.
    share_quantity_change: int
    # Whole cents paid (negative) or received (positive) for those shares. Zero for an expiry.
    cash_change_cents: int


@dataclass(frozen=True, slots=True)
class OptionEventAllocation:
    """One event matched to lots, oldest first. The lots' contracts sum to the event's."""

    kind: OptionEventKind
    contract: OptionContract
    contracts: int
    multiplier: int
    lots: tuple[LotAllocation, ...]

    @property
    def share_price(self) -> Decimal | None:
        """Dollars a share the resulting shares trade at: the strike. ``None`` for an expiry, which trades none."""
        return None if self.kind is OptionEventKind.EXPIRED else self.contract.strike

    @property
    def share_quantity_change(self) -> int:
        return sum(lot.share_quantity_change for lot in self.lots)

    @property
    def cash_change_cents(self) -> int:
        return sum(lot.cash_change_cents for lot in self.lots)


def _shares_and_cash_cents(
    kind: OptionEventKind, contract: OptionContract, *, contracts: int, multiplier: int
) -> tuple[int, int]:
    """Signed shares and signed whole cents for ``contracts`` of an event; an expiry moves neither."""
    if kind is OptionEventKind.EXPIRED:
        return 0, 0
    # A call's holder buys and a put's holder sells; the one assigned takes the other side.
    holder_buys = contract.right == "call"
    buys = holder_buys if kind is OptionEventKind.EXERCISED else not holder_buys
    shares = contracts * multiplier
    cents, part_of_a_cent = divmod(contract.strike_thousandths * shares, _THOUSANDTHS_PER_CENT)
    if part_of_a_cent:
        raise ValueError(
            f"{shares} shares at a strike of {contract.strike} do not come to whole cents; "
            "the amount is refused, never rounded"
        )
    return (shares, -cents) if buys else (-shares, cents)


def expected_share_trade(
    kind: OptionEventKind, contract: OptionContract, *, contracts: int, multiplier: int
) -> ShareTrade | None:
    """The share trade ``contracts`` of an event create in all, or ``None`` for an expiry.

    What a broker reports as the trade must agree with this: the same shares,
    at the strike, for the same cash.
    """
    _require_whole("an event's contracts", contracts, at_least=1)
    _require_whole("a contract's multiplier", multiplier, at_least=1)
    if kind is OptionEventKind.EXPIRED:
        return None
    shares, cash_cents = _shares_and_cash_cents(kind, contract, contracts=contracts, multiplier=multiplier)
    return ShareTrade(root=contract.root, shares=shares, price=contract.strike, cash_cents=cash_cents)


_SIDE_AN_EVENT_ENDS: dict[OptionEventKind, str] = {
    OptionEventKind.ASSIGNED: "sold",
    OptionEventKind.EXERCISED: "bought",
}


def _on_the_side_ended(kind: OptionEventKind, lot: HeldOptionLot) -> bool:
    if kind is OptionEventKind.ASSIGNED:
        return lot.quantity < 0
    if kind is OptionEventKind.EXERCISED:
        return lot.quantity > 0
    return True


def _require_one_contract_and_distinct_lots(contract: OptionContract, lots: Sequence[HeldOptionLot]) -> None:
    elsewhere = sorted({lot.contract.occ_symbol for lot in lots if lot.contract != contract})
    if elsewhere:
        # Taking a lot of another contract would close a position the event never touched.
        raise ValueError(f"every lot offered must be in {contract.occ_symbol}; these are in {elsewhere}")
    names = [(lot.owner, lot.sequence) for lot in lots]
    if len(set(names)) != len(names):
        raise ValueError("an owner and a sequence name one lot; a lot was offered twice")


def allocate_option_event(
    *,
    kind: OptionEventKind,
    contract: OptionContract,
    contracts: int,
    event_date_ms: int,
    multiplier: int,
    lots: Sequence[HeldOptionLot],
) -> OptionEventAllocation:
    """Match one event to the open lots of its contract, oldest first, with each owner's book effects.

    ``lots`` are every open lot of ``contract``, across owners. An assignment
    takes sold lots only and an exercise takes bought lots only; an expiry
    takes either. A lot is taken in part when the event ends before it does.
    ``event_date_ms`` is any instant on the event's New York date; only the
    date is read, and a lot opened on a later date is passed over.

    The lots taken hold exactly ``contracts``, or the event is refused with
    the reason and the count that found no lot: a contract nobody holds, lots
    on the wrong side only, or more contracts than the lots hold.
    """
    _require_whole("an event's contracts", contracts, at_least=1)
    _require_whole("a contract's multiplier", multiplier, at_least=1)
    _require_one_contract_and_distinct_lots(contract, lots)
    symbol = contract.occ_symbol
    if not lots:
        raise OptionEventRefused(
            OptionEventRefusal.CONTRACT_NOT_HELD,
            f"{contracts} of {symbol} were {kind.value}, and no bot holds that contract.",
            unmatched_contracts=contracts,
        )
    on_side = [lot for lot in lots if _on_the_side_ended(kind, lot)]
    if not on_side:
        raise OptionEventRefused(
            OptionEventRefusal.WRONG_SIDE,
            f"{contracts} of {symbol} were {kind.value}, which ends a {_SIDE_AN_EVENT_ENDS[kind]} option; "
            f"every lot of it on the books is {'bought' if kind is OptionEventKind.ASSIGNED else 'sold'}.",
            unmatched_contracts=contracts,
        )
    event_date = et_date_at_ms(event_date_ms)
    open_then = sorted(
        (lot for lot in on_side if et_date_at_ms(lot.opened_at_ms) <= event_date),
        key=lambda lot: (lot.opened_at_ms, lot.sequence, lot.owner),
    )
    held = sum(abs(lot.quantity) for lot in open_then)
    if held < contracts:
        opened_later = sum(abs(lot.quantity) for lot in on_side) - held
        raise OptionEventRefused(
            OptionEventRefusal.MORE_CONTRACTS_THAN_HELD,
            f"{contracts} of {symbol} were {kind.value} on {event_date.isoformat()}, and the lots open then hold {held}."
            + (f" Lots opened after that date hold {opened_later} more." if opened_later else ""),
            unmatched_contracts=contracts - held,
        )
    taken: list[LotAllocation] = []
    left = contracts
    for lot in open_then:
        if not left:
            break
        take = min(abs(lot.quantity), left)
        left -= take
        bought = lot.quantity > 0
        shares, cash_cents = _shares_and_cash_cents(kind, contract, contracts=take, multiplier=multiplier)
        taken.append(
            LotAllocation(
                owner=lot.owner,
                sequence=lot.sequence,
                opened_at_ms=lot.opened_at_ms,
                contracts=take,
                remaining_quantity=lot.quantity - take if bought else lot.quantity + take,
                option_quantity_change=-take if bought else take,
                share_quantity_change=shares,
                cash_change_cents=cash_cents,
            )
        )
    return OptionEventAllocation(
        kind=kind, contract=contract, contracts=contracts, multiplier=multiplier, lots=tuple(taken)
    )
