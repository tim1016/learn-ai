"""How far a broker's option buying power fell across one option entry, against what the bot set aside (ADR 0076).

Around an option entry on a real account the Clerk reads the account's
option buying power before the order is sent and again once its outcome is
proven. This module is the arithmetic of that comparison and nothing else:
it reads no clock, no books and no broker. Whether two readings may be
compared at all is the Clerk's to say (``entry_buying_power``); what a
comparable pair comes to is said here.

Formula: every figure is whole cents and nothing is rounded but one share.
  fall = first reading - second reading.
  cost of what filled = requirement for the units filled
                        + premium paid for the contracts filled.
  set aside for what filled = the entry's commitment when every contract
    filled; else ceil(commitment * contracts filled / contracts asked).
  An entry that filled whole (``judge_fall``):
    consistent                  when fall == cost of what filled;
    under_reserved_unexplained  when fall >  set aside for what filled,
                                which is then the entry's whole commitment;
    over_reserved               otherwise.
    The first row that holds decides.
  An entry that filled in part and whose rest ended (``judge_part_fill``):
    under_reserved_unexplained  when fall >  the entry's whole commitment;
    unknown                     otherwise.
    The broker held the whole order's cost while it rested, and whether it
    had given back the unfilled part by the second reading is not verified.
    So the share for what filled is recorded and nothing is judged by it.
  The tolerance is zero: every input is whole cents, and a difference inside
  the set-aside is over_reserved or unknown, neither of which holds anything.
  A record the Clerk wrote with judgment switched off, whose pair was clean
  (``rejudge``): the same two rows, applied to the record's own readings and
  figures after the fact. Nothing is added to them.
Reference: #2913, "Comparing with Alpaca", check 2 ("the fall in
  options_buying_power ... against requirement plus premium for the units
  filled") and its four results; ADR 0076 decision 26; #3102 for the offline
  judgment. Alpaca's documents do not say when the figure moves for an order
  that rests or is cancelled, which is why the Clerk judges a pair only once
  its setting says the account has shown it, and never judges a part fill
  inside its whole set-aside.
Canonical implementation: this file.
Validated against: tests/options/test_buying_power_check.py (hand-derived
  cases, among them the PRD's $1.94 call, the screens' $193.00 fall, and two
  calls of which one filled).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Protocol

# The reason code the Clerk records for a clean pair it did not judge because the account's setting is off
# (``entry_buying_power.NotComparedReason.JUDGMENT_NOT_SWITCHED_ON`` is this value). The Clerk writes it only
# for a pair that passed every other test.
JUDGMENT_NOT_SWITCHED_ON_REASON: Final = "JUDGMENT_NOT_SWITCHED_ON"


class BuyingPowerCheckResult(StrEnum):
    """What the comparison of one entry with the broker's option buying power came to. A closed set."""

    # The fall is the cost of what filled, to the cent.
    CONSISTENT = "consistent"
    # No comparison can be made, or none has been judged. Always with a reason.
    UNKNOWN = "unknown"
    # The broker took something other than the cost, and no more than the bot set aside. Harmless.
    OVER_RESERVED = "over_reserved"
    # The broker took more than the bot set aside, across a clean pair of readings.
    UNDER_RESERVED_UNEXPLAINED = "under_reserved_unexplained"


def whole_cents(dollars: Decimal) -> int | None:
    """``dollars`` as whole cents, or ``None`` when it is not a whole number of cents or not a number.

    A figure with a fraction of a cent is never rounded into a comparison
    that is exact everywhere else.
    """
    if not isinstance(dollars, Decimal) or not dollars.is_finite():
        return None
    cents = dollars * 100
    if cents != cents.to_integral_value():
        return None
    return int(cents)


def set_aside_for_filled_cents(*, commitment_cents: int, contracts_asked: int, contracts_filled: int) -> int:
    """What the bot set aside for the contracts that filled, in whole cents.

    The whole commitment when every contract filled. For a part fill, its
    share by contracts, rounded up to the cent: the fee provision is the one
    figure in a commitment that does not divide evenly, and rounding up
    never calls a broker's take "more than was set aside" over a fraction
    of a cent.
    """
    _require_whole(commitment_cents, "commitment_cents", least=1)
    _require_whole(contracts_asked, "contracts_asked", least=1)
    _require_whole(contracts_filled, "contracts_filled", least=0)
    if contracts_filled > contracts_asked:
        raise ValueError(f"{contracts_filled} contracts filled of {contracts_asked} asked for")
    return -((-commitment_cents * contracts_filled) // contracts_asked)


def requirement_for_filled_cents(*, requirement_cents: int, units_asked: int, units_filled: int) -> int:
    """The broker's requirement for the whole structures that filled, in whole cents.

    A requirement is so much a structure, so its share divides evenly. One
    that does not is a figure this module was not handed correctly.
    """
    _require_whole(requirement_cents, "requirement_cents", least=0)
    _require_whole(units_asked, "units_asked", least=1)
    _require_whole(units_filled, "units_filled", least=0)
    if units_filled > units_asked:
        raise ValueError(f"{units_filled} structures filled of {units_asked} asked for")
    share, left_over = divmod(requirement_cents * units_filled, units_asked)
    if left_over:
        raise ValueError(
            f"a requirement of {requirement_cents} cents for {units_asked} structures is not a whole number of cents each"
        )
    return share


def judge_fall(
    *, first_cents: int, second_cents: int, cost_of_filled_cents: int, set_aside_for_filled_cents: int
) -> BuyingPowerCheckResult:
    """What a comparable pair of readings comes to for an entry that filled whole.

    Never ``UNKNOWN``: that is said before this is asked. An entry that
    filled in part is ``judge_part_fill``'s.
    """
    for name, value in (("first_cents", first_cents), ("second_cents", second_cents)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} is whole cents; got {value!r}")
    _require_whole(cost_of_filled_cents, "cost_of_filled_cents", least=0)
    _require_whole(set_aside_for_filled_cents, "set_aside_for_filled_cents", least=0)
    fall_cents = first_cents - second_cents
    if fall_cents == cost_of_filled_cents:
        return BuyingPowerCheckResult.CONSISTENT
    if fall_cents > set_aside_for_filled_cents:
        return BuyingPowerCheckResult.UNDER_RESERVED_UNEXPLAINED
    return BuyingPowerCheckResult.OVER_RESERVED


def judge_part_fill(*, first_cents: int, second_cents: int, commitment_cents: int) -> BuyingPowerCheckResult:
    """What a comparable pair of readings comes to for an entry that filled in part and whose rest then ended.

    Held to everything the entry set aside, and to nothing finer. A fall
    of more than that is ``UNDER_RESERVED_UNEXPLAINED``: nothing of the
    entry explains it, however much of the unfilled part the broker still
    holds. Any other fall is ``UNKNOWN``. Never ``CONSISTENT`` or
    ``OVER_RESERVED``: either would be a guess at how much of the fall is
    the part that filled.
    """
    for name, value in (("first_cents", first_cents), ("second_cents", second_cents)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} is whole cents; got {value!r}")
    _require_whole(commitment_cents, "commitment_cents", least=1)
    if first_cents - second_cents > commitment_cents:
        return BuyingPowerCheckResult.UNDER_RESERVED_UNEXPLAINED
    return BuyingPowerCheckResult.UNKNOWN


class ComparisonRecord(Protocol):
    """What offline judgment reads of a recorded comparison. The Clerk's ``EntryBuyingPowerComparedFacts`` has all of it."""

    @property
    def reason_code(self) -> str | None: ...

    @property
    def contracts_asked(self) -> int: ...

    @property
    def contracts_filled(self) -> int: ...

    @property
    def commitment_cents(self) -> int: ...

    @property
    def set_aside_for_filled_cents(self) -> int | None: ...

    @property
    def cost_of_filled_cents(self) -> int | None: ...

    @property
    def first_buying_power(self) -> str | None: ...

    @property
    def second_buying_power(self) -> str | None: ...

    @property
    def fall_cents(self) -> int | None: ...


def rejudge(record: ComparisonRecord) -> BuyingPowerCheckResult | None:
    """What a recorded comparison comes to when it is judged now, or ``None`` when it is not this function's to judge.

    Only a record whose stored reason is "judgment not switched on" is
    judged. The Clerk writes that reason for a pair that passed every other
    test and was held back by the account's setting alone, so the record's
    readings and figures are a clean pair. The arithmetic is the Clerk's own,
    over the record's own fields: ``judge_part_fill`` when contracts were left
    unfilled, ``judge_fall`` otherwise. Every other record is ``None``, one
    already judged included: it was never a clean pair, or it has its result.

    It reads no books and writes none, and it raises no notice and holds no
    bot. A record that says it was a clean pair and does not hold what that
    needs (a reading that is missing or is not whole cents, a fall that is not
    its readings' difference, contracts filled outside what was asked) is
    damaged, and raises ``ValueError``: it is never read as "not applicable".
    """
    if record.reason_code != JUDGMENT_NOT_SWITCHED_ON_REASON:
        return None
    first_cents = _stated_cents(record.first_buying_power, "first_buying_power")
    second_cents = _stated_cents(record.second_buying_power, "second_buying_power")
    if record.fall_cents != first_cents - second_cents:
        raise ValueError(
            f"the record's fall is {record.fall_cents!r} cents but its readings differ by {first_cents - second_cents}"
        )
    asked, filled = record.contracts_asked, record.contracts_filled
    if not 1 <= filled <= asked:
        raise ValueError(f"{filled} contracts filled of {asked} asked for is not a clean pair judgment was held back on")
    if filled < asked:
        return judge_part_fill(first_cents=first_cents, second_cents=second_cents, commitment_cents=record.commitment_cents)
    if record.cost_of_filled_cents is None or record.set_aside_for_filled_cents is None:
        raise ValueError("the record states no cost of what filled, or no set-aside for it")
    return judge_fall(
        first_cents=first_cents,
        second_cents=second_cents,
        cost_of_filled_cents=record.cost_of_filled_cents,
        set_aside_for_filled_cents=record.set_aside_for_filled_cents,
    )


def _stated_cents(stated: str | None, name: str) -> int:
    """A reading as the Clerk recorded it (dollars, as Alpaca wrote it) in whole cents; refuses what is not."""
    if stated is None:
        raise ValueError(f"{name} is missing")
    try:
        cents = whole_cents(Decimal(stated))
    except InvalidOperation as exc:
        raise ValueError(f"{name} {stated!r} is not a number") from exc
    if cents is None:
        raise ValueError(f"{name} {stated!r} is not a whole number of cents")
    return cents


def _require_whole(value: int, name: str, *, least: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < least:
        raise ValueError(f"{name} is a whole number, {least} or more; got {value!r}")


__all__ = [
    "JUDGMENT_NOT_SWITCHED_ON_REASON",
    "BuyingPowerCheckResult",
    "ComparisonRecord",
    "judge_fall",
    "judge_part_fill",
    "rejudge",
    "requirement_for_filled_cents",
    "set_aside_for_filled_cents",
    "whole_cents",
]
