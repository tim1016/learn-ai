"""A simulation's own settlement of the option contracts it holds past their last session (ADR 0076 decision 22).

A real account's broker ends an expiring option position, and reports how
as an account activity. A Dry Run has no broker to do that, so its
simulation does it, as one explicit step: ``settle_expired_options``. The
Clerk's poll of option events calls the step on a simulated account's trade
port, and books what the step hands back through the same batch function
that books a real broker's activities.

**The step is a write, and it is not hidden in a read.** It changes the
simulation's own order ledger, so it lives on the trade port. The read port's
``read_option_event_activities`` stays a read: a simulation answers it with
none, and whole.

**What the step hands back** is everything the Clerk needs, read at one
moment under the ledger's own lock: each contract it settled, with the
simulation's record of how; each contract it still holds past its last
session and could not settle, with why; and each it is still waiting for
the closing price of.

**The rule is the simulation's, never a broker's.** Alpaca says that when
an account cannot pay to exercise an expiring position it sells the
position itself, within the hour before expiry. The simulation books that
sale, in cash, at the close of the contract's last session, at the
contract's exercise value. It never exercises into shares: a Dry Run's
budget could not pay for them, and a put would leave it short. A real
account would have got the market's bid, which is not this figure.

**The price** is the close of one bar the simulation kept for the
underlying. A bar that ends exactly at the close of the contract's last
session settles at once. Otherwise the simulation waits
``SETTLEMENT_GRACE_MS`` after the close, for the closing bar to arrive, and
then takes the newest bar of that session, provided it ended no more than
``SETTLEMENT_BAR_MAX_AGE_MS`` before the close. An older bar is no price,
and neither is a bar of another session, a bar the simulation made itself as
the evidence of a fill, or an option quote. With no price nothing is
settled.

**The owner's ending.** A contract with no price at the close would be held
for ever: no price comes by itself. So the owner can end it at their word.
The step is handed that word, asks for the price once more, and only when
there is still none lets go of the contract, at nothing, under the same
ledger record a settlement would have used. A price that turns up later
then settles nothing a second time. Nobody knows whether such a contract
ended in the money, so it is its own outcome and never "expired".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.alpaca.clerk.money import (
    ZERO,
    MoneyInputError,
    cents_spendable,
    dollars,
    money_context,
    normalize_money,
    notional,
)
from app.broker.contract.option_events import BrokerOptionEventActivity
from app.options.contract import OptionContract
from app.utils.session_anchors import MAX_TIMESTAMP_MS

# How long after the close of a contract's last session the simulation waits for that session's closing bar.
SETTLEMENT_GRACE_MS = 2 * 60_000
# The longest before the close a bar may have ended and still stand for the closing price, once the wait is over.
SETTLEMENT_BAR_MAX_AGE_MS = 15 * 60_000

# What a simulated settlement reports itself as, where a broker's activity carries the broker's own code.
SIMULATED_EXPIRY_ACTIVITY = "SIMULATED_EXPIRY"
SIMULATED_CASH_SETTLEMENT_ACTIVITY = "SIMULATED_CASH_SETTLEMENT"

SIMULATED_SETTLEMENT_RULE = (
    "A simulation's rule, not a broker's. Alpaca says it sells an expiring position itself, within the hour "
    "before expiry, when the account cannot pay to exercise it. This simulation books that sale at the close of "
    "the contract's last session, at the contract's exercise value, rounded down to the cent. A value under a "
    "cent is an expiry at nothing. A real account would have got the market's bid."
)
SIMULATED_FEES_RULE = "Charged by the option fee model, as for every simulated option sale."
SIMULATED_OWNER_ENDING_ACTIVITY = "SIMULATED_ENDED_AT_OWNERS_WORD"
SIMULATED_OWNER_ENDING_RULE = (
    "A simulation's rule, not a broker's. This simulation kept no price to settle the contract from at the close "
    "of its last session, and none comes by itself. The owner ended it at their word, and it was counted as "
    "worth nothing. Whether it ended in the money is not known."
)


class SimulatedOutcome(StrEnum):
    """How a simulation ended a contract it held past the contract's last session. A closed set."""

    # Its exercise value came to less than a cent: it expired, and nothing was sold.
    EXPIRED = "expired"
    # Sold, in cash, at its exercise value.
    SETTLED_IN_CASH = "settled_in_cash"
    # No price at the close: let go at nothing, at the owner's word.
    ENDED_BY_OWNER = "ended_by_owner"


class OwnersWord(BaseModel):
    """The owner's word that an expired contract nothing will settle is to be ended, counted as worth nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Who said it: the installation's operator, never a request's own field.
    operator: str = Field(min_length=1)
    # The owner's own reason, when one was given.
    reason: str | None = None
    # When it was said, on the Clerk's clock.
    said_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)


def settlement_activity_id(symbol: str) -> str:
    """The id a simulation hands the settlement of one contract over under. One for each contract, for good."""
    return f"sim-option-event:{symbol}"


def plain(amount: Decimal) -> str:
    """An exact decimal written out in full, with no exponent and no trailing zeros: ``585``, ``590.25``."""
    with money_context():
        return format(amount.normalize(), "f")


@dataclass(frozen=True, slots=True)
class ExerciseValue:
    """What a contract held to the close of its last session is worth there, by the simulation's rule."""

    # Dollars a share, exact, as worked out. Never below zero.
    worked_out: Decimal
    # Dollars a share as booked: ``worked_out`` rounded down to the cent.
    booked: Decimal
    # What the contracts settled come to at the booked figure, in whole cents.
    cash_cents: int


def exercise_value(
    contract: OptionContract, *, underlying_price: Decimal, contracts: int, multiplier: int
) -> ExerciseValue:
    """What a simulation books an expired contract at, from the underlying's closing price.

    Formula: with the underlying's price ``S`` at the close of the contract's
      last session, the strike ``K``, ``n`` contracts and the multiplier ``m``:
        value a share   v = max(S - K, 0) for a call, max(K - S, 0) for a put
        booked a share  b = floor(100 · v) / 100     (down to the cent: against the holder)
        cash, in cents    = 100 · b · n · m          (whole: b is whole cents, n and m whole numbers)
      ``b = 0`` is an expiry at nothing: no sale, no cash and no fee. So a value
      that rounds down to nothing and one that is nothing are one outcome.
    Reference: the exercise payoff of one bought leg, as ``app/options/requirement.py``
      states it after Alpaca's "Options Level 3 Trading" page. That it is
      booked as a sale at the close, and rounded down, is this simulation's
      own rule (ADR 0076 decision 22): options trade in whole cents, and
      Alpaca's Trading API page ("Options Trading", read 2026-10-06) says a
      position an account cannot pay to exercise is sold before expiry.
    Canonical implementation: this function; the cash is ``clerk/money.py::notional``'s, an option's
      contracts x price a share x multiplier.
    Validated against: tests/broker/alpaca/clerk/test_simulated_option_settlement.py
      (hand-derived cases, exact; and the payoff of ``app/options/requirement.py``
      for one bought leg, exact).
    """
    if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts < 1:
        raise ValueError(f"a settlement is of one contract or more; got {contracts!r}")
    if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier < 1:
        raise ValueError(f"a contract's multiplier is a whole number, one or more; got {multiplier!r}")
    price = normalize_money(underlying_price)
    if price <= 0:
        raise ValueError(f"an underlying's price is above zero; got {underlying_price}")
    with money_context():
        value = price - contract.strike if contract.right == "call" else contract.strike - price
        worked_out = max(value, ZERO)
        booked = Decimal(cents_spendable(worked_out)).scaleb(-2)
        # Whole cents: the booked price is whole cents, and the contracts and the multiplier whole numbers.
        cash_cents = cents_spendable(notional(contracts, booked, multiplier=multiplier))
        return ExerciseValue(worked_out=worked_out, booked=booked, cash_cents=cash_cents)


class SettlementPrice(BaseModel):
    """The price a simulation settled a contract from: the close of one bar it kept for the underlying."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Dollars a share, exact.
    underlying_price: Decimal
    # The retained bar that price is the close of: its reference, who delivered it, and when it ended.
    bar_ref: str = Field(min_length=1)
    bar_provider: str = Field(min_length=1)
    bar_end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    # How long before the close of the contract's last session that bar ended. Nothing for the closing bar itself.
    bar_before_close_ms: int = Field(ge=0)


class SynthesizedOptionSettlement(BaseModel):
    """How a simulation ended a contract it held past the contract's last session (ADR 0076 decision 22).

    One record in the simulation's order ledger: it takes the option
    position out, at the booked price. It moves no shares. This is the
    record of the rule, the price the rule read and every figure worked
    out from it. It is a simulation's settlement, never a broker's.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: OptionContract
    outcome: SimulatedOutcome
    # Contracts settled: one or more. A simulation settles only what it bought.
    contracts: int = Field(ge=1, strict=True)
    multiplier: int = Field(ge=1, strict=True)
    # When the contract stopped trading, and when the simulation settled it.
    last_session_close_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    settled_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    # The rule applied, in plain words.
    rule: str = Field(min_length=1)
    # The price the rule read. None only for a contract ended at the owner's word, which had none.
    price: SettlementPrice | None = None
    # Dollars a share, exact: the exercise value as worked out, and as booked (rounded down to the cent).
    # No value was worked out for a contract ended at the owner's word. It is booked at nothing.
    exercise_value: Decimal | None = None
    booked_price: Decimal
    # What the settlement paid the account, in whole cents: the booked price for every contract settled.
    cash_cents: int = Field(ge=0, strict=True)
    # What the option fee model charges this sale taken as a day of its own, in whole cents. None when
    # nothing was sold, or when a fee rate is not pinned for the day.
    fee_cents: int | None = Field(default=None, ge=0, strict=True)
    # For a contract ended at the owner's word: whose word, and why the simulation's own rule could not settle it.
    owner: OwnersWord | None = None
    why_no_settlement: str | None = None

    @model_validator(mode="after")
    def _figures_that_are_one_settlement(self) -> Self:
        if self.outcome is SimulatedOutcome.ENDED_BY_OWNER:
            if self.owner is None or not (self.why_no_settlement or "").strip():
                raise ValueError("a contract ended at the owner's word names who said so and why nothing settled it")
            if self.price is not None or self.exercise_value is not None:
                raise ValueError("a contract ended at the owner's word had no price to settle from")
            if self.booked_price != 0 or self.cash_cents != 0 or self.fee_cents is not None:
                raise ValueError("a contract ended at the owner's word is counted as worth nothing: no cash, no fee")
            return self
        if self.owner is not None or self.why_no_settlement is not None:
            raise ValueError(f"a contract that was {self.outcome.value} was settled by the rule, at nobody's word")
        if self.price is None or self.exercise_value is None:
            raise ValueError(f"a contract that was {self.outcome.value} states the price it was settled from")
        worked_out = exercise_value(
            self.contract, underlying_price=self.price.underlying_price, contracts=self.contracts,
            multiplier=self.multiplier,
        )
        if (self.exercise_value, self.booked_price, self.cash_cents) != (
            worked_out.worked_out, worked_out.booked, worked_out.cash_cents
        ):
            raise ValueError(
                f"the settlement of {self.contract.occ_symbol} states {self.exercise_value} a share, booked at "
                f"{self.booked_price}, for {self.cash_cents} cents; its own price gives {worked_out}"
            )
        sold = self.booked_price > 0
        if sold is not (self.outcome is SimulatedOutcome.SETTLED_IN_CASH):
            raise ValueError(f"a contract booked at {self.booked_price} a share is not {self.outcome.value}")
        if not sold and self.fee_cents is not None:
            raise ValueError("an expiry at nothing is no sale and has no fee")
        return self

    def owner_word(self) -> dict[str, Any] | None:
        """Whose word ended the contract and why nothing settled it, as plain values. None when the rule settled it."""
        if self.owner is None:
            return None
        return {
            "operator": self.owner.operator, "reason": self.owner.reason, "said_at_ms": self.owner.said_at_ms,
            "why_no_settlement": self.why_no_settlement,
        }

    def note(self) -> dict[str, Any]:
        """The settlement as plain values, for the books' own record of the event."""
        if self.price is None or self.exercise_value is None:
            return {
                "rule": self.rule,
                "outcome": self.outcome.value,
                "underlying": self.contract.root,
                "strike": plain(self.contract.strike),
                "right": self.contract.right,
                "booked_price": plain(self.booked_price),
                "contracts": self.contracts,
                "multiplier": self.multiplier,
                "cash_cents": self.cash_cents,
                "last_session_close_ms": self.last_session_close_ms,
                "settled_at_ms": self.settled_at_ms,
                "why_no_settlement": self.why_no_settlement,
                "fees": "None: nothing was sold.",
                "fee_cents": None,
            }
        return {
            "rule": self.rule,
            "outcome": self.outcome.value,
            "underlying": self.contract.root,
            "underlying_price": plain(self.price.underlying_price),
            "strike": plain(self.contract.strike),
            "right": self.contract.right,
            "exercise_value": plain(self.exercise_value),
            "booked_price": plain(self.booked_price),
            "contracts": self.contracts,
            "multiplier": self.multiplier,
            "cash_cents": self.cash_cents,
            "bar_ref": self.price.bar_ref,
            "bar_provider": self.price.bar_provider,
            "bar_end_ms": self.price.bar_end_ms,
            "last_session_close_ms": self.last_session_close_ms,
            "bar_before_close_ms": self.price.bar_before_close_ms,
            "settled_at_ms": self.settled_at_ms,
            "fees": SIMULATED_FEES_RULE if self.outcome is SimulatedOutcome.SETTLED_IN_CASH else "None: nothing was sold.",
            "fee_cents": self.fee_cents,
        }


def check_settlement_note(
    note: Mapping[str, Any], contract: OptionContract, *, multiplier: int, outcome: str, close_price: Decimal
) -> None:
    """Refuse a simulation's record whose own figures do not give the price a booking states.

    The books' fold asks this of every simulated booking. It works the
    exercise value out again from the price the record names and the
    contract's own strike, and holds the record's booked price, its cash and
    the booking's close price to that.
    """
    stated = {key: note.get(key) for key in ("outcome", "underlying_price", "booked_price", "cash_cents", "contracts")}
    if outcome == SimulatedOutcome.ENDED_BY_OWNER.value:
        # No price was read, so there is no value to work out again: only that nothing was paid.
        if (stated["outcome"], stated["booked_price"], stated["cash_cents"]) != (outcome, "0", 0) or close_price != 0:
            raise ValueError(
                f"{contract.occ_symbol} ended at the owner's word is counted as worth nothing; the record states "
                f"{stated!r} and the booking {plain(close_price)}"
            )
        if stated["underlying_price"] is not None:
            raise ValueError("a contract ended at the owner's word had no price to settle from, and this record names one")
        return
    try:
        underlying_price, booked_price = (
            normalize_money(stated[key]) if isinstance(stated[key], str) else None
            for key in ("underlying_price", "booked_price")
        )
    except MoneyInputError:
        underlying_price = booked_price = None
    if underlying_price is None or booked_price is None:
        raise ValueError(f"a simulation's record states the price it read and the price it booked; got {stated!r}")
    contracts = stated["contracts"]
    if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts < 1:
        raise ValueError(f"a simulation's record states the contracts it settled; got {stated!r}")
    worked_out = exercise_value(contract, underlying_price=underlying_price, contracts=contracts, multiplier=multiplier)
    if (
        stated["outcome"] != outcome
        or booked_price != worked_out.booked
        or stated["cash_cents"] != worked_out.cash_cents
        or close_price != worked_out.booked
    ):
        raise ValueError(
            f"{contract.occ_symbol} settled from {stated['underlying_price']} is worth {plain(worked_out.booked)} a "
            f"share, {dollars(worked_out.cash_cents)} in all; the record states {stated!r} and the booking "
            f"{outcome} at {plain(close_price)}"
        )


@dataclass(frozen=True, slots=True)
class SimulatedSettlement:
    """One contract a simulation settled, as the Clerk books it."""

    # The settlement in the shape an option event travels in, so the one rule that chooses lots chooses these.
    # It is an expiry there, whatever its outcome: it ends a bought position and moves no shares.
    event: BrokerOptionEventActivity
    # The simulation's own record of the settlement: its rule, the price it read and every figure from it.
    record: SynthesizedOptionSettlement


@dataclass(frozen=True, slots=True)
class SimulatedOptionSettlements:
    """What one run of a simulation's settlement step did, and what it could not do."""

    # Every settlement in the window asked for, the ones made by earlier runs of the step among them.
    settled: tuple[SimulatedSettlement, ...] = ()
    # Each contract still held past its last session that the step cannot settle, with why. The
    # simulation lets go of one at the owner's word only when the why is that it kept no price of
    # the close. For any other why (a sold or part contract, no store of bars) it does not let go.
    not_settled: Mapping[str, str] = field(default_factory=dict)
    # Each contract still held whose closing bar the step is waiting for, with the instant the wait ends.
    waiting: Mapping[str, int] = field(default_factory=dict)


@runtime_checkable
class OptionSettlementSimulation(Protocol):
    """A trade port that may be a simulation's, and can be asked to settle what the simulation holds past expiry.

    A simulation's own port always answers with its settlements. The
    Clerk's guarded trade port answers None when the port behind it is no
    simulation: that account's broker ends its positions, and reports them
    through the read port.
    """

    def settle_expired_options(
        self, *, after_ms: int, at_the_owners_word: Mapping[str, OwnersWord] | None = None
    ) -> SimulatedOptionSettlements | None:
        """Settle every contract held past its last session, and hand back the settlements dated in the window.

        ``after_ms`` is any instant on the window's first New York date, as
        for ``BrokerReadPort.read_option_event_activities``.
        ``at_the_owners_word`` names, by contract symbol, each contract the
        owner has said to end: one of those that still has no price at the
        close is let go of, at nothing. One that has a price is settled by
        the rule, as every other is.
        """
        ...


def option_settlement_simulation(trade: object) -> OptionSettlementSimulation | None:
    """The settlement step a trade port declares, or None for a port that declares none."""
    return trade if isinstance(trade, OptionSettlementSimulation) else None


__all__ = [
    "SETTLEMENT_BAR_MAX_AGE_MS",
    "SETTLEMENT_GRACE_MS",
    "SIMULATED_CASH_SETTLEMENT_ACTIVITY",
    "SIMULATED_EXPIRY_ACTIVITY",
    "SIMULATED_OWNER_ENDING_ACTIVITY",
    "SIMULATED_OWNER_ENDING_RULE",
    "SIMULATED_SETTLEMENT_RULE",
    "ExerciseValue",
    "OptionSettlementSimulation",
    "OwnersWord",
    "SettlementPrice",
    "SimulatedOptionSettlements",
    "SimulatedOutcome",
    "SimulatedSettlement",
    "SynthesizedOptionSettlement",
    "check_settlement_note",
    "exercise_value",
    "option_settlement_simulation",
    "plain",
    "settlement_activity_id",
]
