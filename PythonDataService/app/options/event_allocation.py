"""Which lots an assignment, an exercise or an expiry takes, and what it does to each owner's books.

Three things end an option position without an order of ours: a sold option
is assigned, a bought option is exercised, or an option expires worthless.
The broker reports each against the account, by contract and by count. This
says whose lots it took, and what each owner's books then show (ADR 0076).

Formula: with the change ``c`` the broker reports to the account's position
  (negative ends a bought position, positive a sold one), ``n = |c|``
  contracts, the multiplier ``m``, the strike ``K``, and the lots it may
  take in the order it takes them (oldest first for an assignment or an
  exercise, newest first for an expiry), each holding ``q_j`` contracts
  (bought positive, sold negative):
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
Validated against: tests/fixtures/test_option_event_allocation_fixture.py, on
  golden fixture OEV-001 (tests/fixtures/golden/option-events/OEV-001): the
  shares, the price and the cash of every exercise, assignment and expiry
  Alpaca's two pages document, bit-exact. Which lots are taken is this
  repo's rule and is on no page; hand-derived cases hold it in
  tests/options/test_event_allocation.py.

What is Alpaca's documented behaviour, and what is not:

- **Documented for the Trading API, which this repo trades through:** an
  exercised call buys the shares at the strike, an assigned call sells them,
  and an option that expires is removed and nothing more. The event's own
  ``net_amount`` is "0" and the shares are 100 a contract in every example.
- **Documented only for the Broker API, another product:** that an exercised
  put sells the shares and an assigned put buys them. It is also what a put
  means, and it is used here on that footing.
- **Where the Trading API page contradicts itself, an accepted divergence:**
  its example of the exercised calls prints a share price of 90 on a strike
  of 150, against the sentence under it and against its own net amount
  (-30000 for 200 shares). The strike is the price here. OEV-001 records
  what the page prints beside what its sentence and net amount give, and
  the fixture's test names the 90 as the one printed value not matched.
- **The contract's standard meaning, on no page:** that a contract moves
  ``m`` shares when its multiplier is not 100. The multiplier here is both
  the shares a contract delivers and what its strike is multiplied by, which
  is true of a standard contract, the only kind a bot trades.
- **This repo's bookkeeping (ADR 0076):** the lot closes at a price of zero,
  so the premium paid or received when it opened stays in the lot's cost and
  is not moved into the shares' cost. Alpaca's zero net amount on the event
  is consistent with that and does not prescribe it.
- **This repo's rule (#2913, ADR 0076):** an assignment or an exercise takes
  the oldest lot first when more than one holds the contract. An expiry takes
  the newest first: it is what is left once the oldest were assigned or
  exercised, so which bot is assigned is the same whichever row is matched
  first. A lot opened on a later New York date than the event is passed
  over, because it did not exist when the event happened. The broker
  reports an event against the account and names no lot.
- **This repo's rule (ADR 0076):** an expiry of a contract held on both
  sides is refused. The broker's row is taken to be the account's net,
  bought less sold (no published example shows one), and no count of it
  says how many contracts each side lost.

Shares sold are sold whether or not the owner held them. A call assigned to
a bot with no shares leaves that bot short the stock, and its books must show
the short position: ``share_quantity_change`` is negative and nothing here
nets it against anything. No page read says what Alpaca itself then does
with such an account; that the shares are sold at the strike is the
contract's meaning.

Every figure is a whole number of contracts, shares or cents, and no step
rounds. Cash that does not come to whole cents is refused by name: the
event's total first, before any lot is looked at, and then any lot's part
of a whole total that is not whole itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.options.contract import OptionContract
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

# A strike is in thousandths of a dollar; ten of them make a cent.
_THOUSANDTHS_PER_CENT = 10


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
    # An expiry of a contract held bought and sold at once. The broker's row is the account's net.
    HELD_ON_BOTH_SIDES = "OPTION_EVENT_HELD_ON_BOTH_SIDES"
    # The shares at the strike do not come to whole cents.
    CASH_NOT_WHOLE_CENTS = "OPTION_EVENT_CASH_NOT_WHOLE_CENTS"
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


def _require_an_instant(name: str, value: object) -> None:
    """Refuse a time that is not ``int64`` ms UTC inside the range the domain admits."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TIMESTAMP_MS:
        raise ValueError(
            f"{name} must be a whole number of milliseconds from 0 to {MAX_TIMESTAMP_MS}; got {value!r}"
        )


def _require_a_change(position_change: object) -> None:
    if isinstance(position_change, bool) or not isinstance(position_change, int) or position_change == 0:
        raise ValueError(
            f"an event's position change is a whole number of contracts, never zero; got {position_change!r}"
        )


@dataclass(frozen=True, slots=True)
class HeldOptionLot:
    """One open lot of one contract on one owner's books."""

    # Whose books hold it: the bot.
    owner: str
    contract: OptionContract
    # Contracts still open: bought is positive, sold is negative, never zero.
    quantity: int
    # When the lot was opened, ``int64`` ms UTC, from 0 to ``MAX_TIMESTAMP_MS``.
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
        _require_an_instant("a lot's opening time", self.opened_at_ms)
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
    # The change to the owner's position in the contract, closed at a price of zero: the premium stays in the lot's cost.
    option_quantity_change: int
    # Shares the owner's books gain (positive) or give up (negative). Zero for an expiry.
    share_quantity_change: int
    # Whole cents paid (negative) or received (positive) for those shares. Zero for an expiry.
    cash_change_cents: int


@dataclass(frozen=True, slots=True)
class OptionEventAllocation:
    """One event matched to lots, in the order they were taken. The lots' contracts sum to the event's."""

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
    kind: OptionEventKind, contract: OptionContract, *, contracts: int, multiplier: int, event_contracts: int
) -> tuple[int, int]:
    """Signed shares and signed whole cents for ``contracts`` of an event; an expiry moves neither.

    ``event_contracts`` is the whole event's count, which a refusal names:
    cash that is not whole cents refuses the event, not a part of it.
    """
    if kind is OptionEventKind.EXPIRED:
        return 0, 0
    # A call's holder buys and a put's holder sells; the one assigned takes the other side.
    holder_buys = contract.right == "call"
    buys = holder_buys if kind is OptionEventKind.EXERCISED else not holder_buys
    shares = contracts * multiplier
    cents, part_of_a_cent = divmod(contract.strike_thousandths * shares, _THOUSANDTHS_PER_CENT)
    if part_of_a_cent:
        raise OptionEventRefused(
            OptionEventRefusal.CASH_NOT_WHOLE_CENTS,
            f"{shares} shares of {contract.occ_symbol} at a strike of {contract.strike} do not come to whole "
            "cents; the amount is refused, never rounded.",
            unmatched_contracts=event_contracts,
        )
    return (shares, -cents) if buys else (-shares, cents)


def expected_share_trade(
    kind: OptionEventKind, contract: OptionContract, *, contracts: int, multiplier: int
) -> ShareTrade | None:
    """The share trade ``contracts`` of an event create in all, or ``None`` for an expiry.

    What a broker reports as the trade must agree with this: the same shares,
    at the strike, for the same cash. Cash that does not come to whole cents
    is refused by name (``CASH_NOT_WHOLE_CENTS``).
    """
    # A plain string is read as the kind it spells, or refused: never taken for an assignment.
    kind = OptionEventKind(kind)
    _require_whole("an event's contracts", contracts, at_least=1)
    _require_whole("a contract's multiplier", multiplier, at_least=1)
    if kind is OptionEventKind.EXPIRED:
        return None
    shares, cash_cents = _shares_and_cash_cents(
        kind, contract, contracts=contracts, multiplier=multiplier, event_contracts=contracts
    )
    return ShareTrade(root=contract.root, shares=shares, price=contract.strike, cash_cents=cash_cents)


def assignment_cash_cents(contract: OptionContract, *, contracts: int, multiplier: int) -> int:
    """The whole cents the shares of ``contracts`` assigned contracts trade for, unsigned: strike × multiplier × contracts.

    What assignment of a sold contract obliges the account to pay (a put) or
    receive (a call) for the shares: the one figure the account's picture and
    the Options Backtest both state as the assignment obligation, each
    converting the unit at its own edge (#3101).

    Formula: cents = |cash of the trade ``expected_share_trade`` states for an
      assignment| = n · m · K / 10 for ``K`` in thousandths of a dollar.
    Reference: ADR 0076 decision 22; the cash of an assignment is
      ``cash = -shares · K`` above.
    Canonical implementation: this function over ``expected_share_trade``;
      it adds no arithmetic of its own.
    Validated against: tests/options/test_event_allocation.py, and
      tests/broker/alpaca/clerk/test_account_picture.py (the same holdings
      through the backtest's own figure).

    Refuses as ``OptionEventRefused`` an amount that is not whole cents.
    """
    trade = expected_share_trade(OptionEventKind.ASSIGNED, contract, contracts=contracts, multiplier=multiplier)
    assert trade is not None, "an assignment always trades shares"
    return abs(trade.cash_cents)


_BOUGHT = "bought"
_SOLD = "sold"
_THE_OTHER_SIDE = {_BOUGHT: _SOLD, _SOLD: _BOUGHT}

# The side of a position each kind of event can end. Only a sold option is
# assigned and only a bought one exercised; either expires.
_SIDES_A_KIND_ENDS: dict[OptionEventKind, tuple[str, ...]] = {
    OptionEventKind.ASSIGNED: (_SOLD,),
    OptionEventKind.EXERCISED: (_BOUGHT,),
    OptionEventKind.EXPIRED: (_BOUGHT, _SOLD),
}


def _side_held(lot: HeldOptionLot) -> str:
    return _BOUGHT if lot.quantity > 0 else _SOLD


def _side_a_change_ends(position_change: int) -> str:
    """The side a broker's signed change ends: it takes from a bought position or adds back to a sold one."""
    return _BOUGHT if position_change < 0 else _SOLD


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
    position_change: int,
    event_date_ms: int,
    multiplier: int,
    lots: Sequence[HeldOptionLot],
) -> OptionEventAllocation:
    """Match one event to the open lots of its contract, with each owner's book effects.

    ``position_change`` is the change the broker reports to the account's
    position, in whole contracts and signed as the broker signs it: negative
    ends a bought position, positive a sold one. ``lots`` are every open lot
    of ``contract``, across owners. ``event_date_ms`` is any instant on the
    event's New York date; only the date is read, and a lot opened on a
    later date is passed over. A lot is taken in part when the event ends
    before it does.

    **Which side.** An assignment takes sold lots only and an exercise
    bought lots only; a change signed for the other side is not that kind of
    event and raises ``ValueError``. An expiry takes the side its sign
    names. When lots are held on one side only, an expiry signed for the
    other side still closes them: every expiry Alpaca's pages show is signed
    negative, so the sign of a sold one is an inference, and the books hold
    nothing else the row could mean. When lots are held on both sides an
    expiry is refused (``HELD_ON_BOTH_SIDES``): the broker's row is taken to
    be the account's net, and no count of it can be matched to lots.

    **Which lots.** An assignment or an exercise takes the oldest lots
    first. An expiry takes the newest first, because it is what is left once
    the oldest were assigned or exercised. So the lots that are assigned are
    the same whether the expiry or the assignment is matched first.

    The rule chooses among the lots it is given and no others. A holding of
    the contract that is on nobody's books here, such as one the owner
    opened by hand, must be offered as a lot of its own, or an event that
    took it is matched to a bot's lot.

    The lots taken hold exactly the event's contracts, or the event is
    refused with the reason and the count that found no lot: cash that is
    not whole cents, a contract nobody holds, lots on the wrong side only,
    an expiry of a contract held on both sides, or more contracts than the
    lots hold. Nothing is allocated when an event is refused.
    """
    # A plain string is read as the kind it spells, or refused: never taken for an assignment.
    kind = OptionEventKind(kind)
    _require_a_change(position_change)
    _require_an_instant("an event's date", event_date_ms)
    _require_whole("a contract's multiplier", multiplier, at_least=1)
    _require_one_contract_and_distinct_lots(contract, lots)
    contracts = abs(position_change)
    side = _side_a_change_ends(position_change)
    if side not in _SIDES_A_KIND_ENDS[kind]:
        raise ValueError(
            f"an option that is {kind.value} ends a {_THE_OTHER_SIDE[side]} position; "
            f"a change of {position_change:+d} would end a {side} one"
        )
    # The cash is the event's own, whatever the books hold: refused before any lot is looked at.
    _shares_and_cash_cents(kind, contract, contracts=contracts, multiplier=multiplier, event_contracts=contracts)
    symbol = contract.occ_symbol
    if not lots:
        raise OptionEventRefused(
            OptionEventRefusal.CONTRACT_NOT_HELD,
            f"{contracts} of {symbol} were {kind.value}, and no bot holds that contract.",
            unmatched_contracts=contracts,
        )
    on_side = [lot for lot in lots if _side_held(lot) == side]
    on_the_other_side = [lot for lot in lots if _side_held(lot) != side]
    expired = kind is OptionEventKind.EXPIRED
    if expired and on_side and on_the_other_side:
        raise OptionEventRefused(
            OptionEventRefusal.HELD_ON_BOTH_SIDES,
            f"{contracts} of {symbol} expired, and the books hold it both bought and sold. The broker reports "
            "the account's net, so no count of it can be matched to lots.",
            unmatched_contracts=contracts,
        )
    if expired and not on_side:
        # Held on one side only: the row can mean nothing else, whatever its sign.
        on_side = on_the_other_side
    if not on_side:
        raise OptionEventRefused(
            OptionEventRefusal.WRONG_SIDE,
            f"{contracts} of {symbol} were {kind.value}, which ends a {side} option; "
            f"every lot of it on the books is {_THE_OTHER_SIDE[side]}.",
            unmatched_contracts=contracts,
        )
    event_date = et_date_at_ms(event_date_ms)
    # An assignment or an exercise takes the oldest lots. An expiry is what they leave, so it takes the newest.
    open_then = sorted(
        (lot for lot in on_side if et_date_at_ms(lot.opened_at_ms) <= event_date),
        key=lambda lot: (lot.opened_at_ms, lot.sequence, lot.owner),
        reverse=expired,
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
        shares, cash_cents = _shares_and_cash_cents(
            kind, contract, contracts=take, multiplier=multiplier, event_contracts=contracts
        )
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
