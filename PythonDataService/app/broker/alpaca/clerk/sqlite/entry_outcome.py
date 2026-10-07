"""What an option entry came to, read from the books, and how the strategy is settled on it (ADR 0076).

An option entry is a limit order that waits at the broker. Its ENTER stays
``in_progress`` whether the order is working, filled or filled in part, so
the effect's state cannot tell those apart. The books can: the order row
holds the broker's last state for it, the fills hold what was bought, and
the acknowledgements hold the broker's own cumulative count.

``entry_outcome`` is that one read. It writes nothing and asks no broker.
``settlement_for`` is the rule a strategy is settled by, for the live
runner and for a replay alike. ``entry_settlement`` is the answer for anyone
who asks after the fact: the Clerk's recorded answer when it gave one
(``recorded_settlement``), else the rule applied to the books as they stand.

Formula: units filled = min over the entry's legs of
  floor(contracts filled of the leg / the leg's ratio): the whole structures
  that filled. For one leg of ratio one that is the contracts filled.
  Settlement: entered when units filled >= 1; discarded when the entry is
  proven to have ended with nothing filled, or was never sent; entered
  otherwise, because an exit will then come and close whatever turns out
  to be held.
Reference: ADR 0076; #2913 ("Held always means the confirmed filled
  quantity"; "an accepted option entry is settled when its outcome is
  proven").
Canonical implementation: this file. The filled quantity is
  ``reads.effective_fill_totals_for_order`` and the shortfall is
  ``reads.order_fills_short_of_broker_cumulative``.
Validated against: tests/broker/alpaca/clerk/sqlite/test_entry_outcome.py.

**Proven** means the books need nothing more from the broker to say what
the entry holds. Four standings are proven: filled, filled in part and
ended, ended unfilled, and never sent. Three are not: working, cancellation
requested, and outcome unknown. A terminal order whose recorded fills fall
short of the broker's own cumulative is unknown, never filled: a fill the
broker counted is not on the books yet. So is an entry whose books hold
more contracts than it asked for: something was counted that the order
cannot explain.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.execution_coverage import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.facts import EntrySettledFacts
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts, option_entry_of_order
from app.broker.alpaca.clerk.sqlite.order_projection import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
    UNFILLED_TERMINAL_STATES,
)
from app.broker.alpaca.clerk.sqlite.reads import FAILED_EFFECT_STATES

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


class EntryStanding(StrEnum):
    """Where one option entry stands. A closed set."""

    # Sent, or about to be, and the broker may still fill it.
    WORKING = "working"
    # The Clerk asked the broker to cancel it, and no lookup has shown it ended.
    CANCELLATION_REQUESTED = "cancellation_requested"
    # Every contract asked for filled.
    FILLED = "filled"
    # It ended with some of it filled. The bot holds what filled.
    PART_FILLED = "part_filled"
    # It ended with nothing filled.
    UNFILLED = "unfilled"
    # It was refused at or before the send, or proven never to have reached the broker.
    NOT_SENT = "not_sent"
    # What the broker did with it is not known yet.
    OUTCOME_UNKNOWN = "outcome_unknown"


PROVEN_STANDINGS: frozenset[EntryStanding] = frozenset(
    {EntryStanding.FILLED, EntryStanding.PART_FILLED, EntryStanding.UNFILLED, EntryStanding.NOT_SENT}
)


class EntrySettlement(StrEnum):
    """How a strategy is settled on an option entry: it is in, or the decision is dropped."""

    ENTERED = "entered"
    DISCARDED = "discarded"


@dataclass(frozen=True, slots=True, kw_only=True)
class EntryOutcome:
    """What one option entry came to, as the books show it."""

    standing: EntryStanding
    # The entry's orders, the parent first. One order while an entry is one leg.
    order_refs: tuple[str, ...]
    # Whole structures asked for, and the whole structures that filled.
    units_asked: int
    units_filled: int
    # Contracts filled of each contract, by its OCC symbol.
    contracts_filled: Mapping[str, int]
    # Whether the legs filled to different numbers of whole structures. Never, for one leg.
    legs_out_of_step: bool
    # When the Clerk cancels what has not filled; None for an entry from before it could rest.
    cancel_at_ms: int | None
    # When the Clerk recorded that it was asking the broker to cancel; None when it has not.
    cancel_requested_at_ms: int | None
    # When the books last changed for a proven entry; None while it is not proven.
    proven_at_ms: int | None

    @property
    def proven(self) -> bool:
        return self.standing in PROVEN_STANDINGS


@dataclass(frozen=True, slots=True, kw_only=True)
class SettledEntry:
    """The Clerk's answer to "what did this entry come to": how the strategy is settled, and on what."""

    settlement: EntrySettlement
    # Where the entry stood when the Clerk answered, and the whole structures asked for and filled then.
    standing: EntryStanding
    units_asked: int
    units_filled: int
    # The entry's order: the parent, once an entry has several.
    order_ref: str
    until_ms: int
    settled_at_ms: int
    # Whether the answer is on the books as ``ENTRY_SETTLED``. It is not when the wait ended
    # only because the bot's run had stopped: a later replay then settles from the books.
    recorded: bool

    @classmethod
    def of(cls, facts: EntrySettledFacts, *, order_ref: str, recorded: bool) -> SettledEntry:
        return cls(
            settlement=EntrySettlement(facts.settlement),
            standing=EntryStanding(facts.standing),
            units_asked=facts.units_asked,
            units_filled=facts.units_filled,
            order_ref=order_ref,
            until_ms=facts.until_ms,
            settled_at_ms=facts.settled_at_ms,
            recorded=recorded,
        )


def entry_outcome(repo: ClerkSqliteRepository, entry_order_ref: str) -> EntryOutcome:
    """What the option entry accepted as ``entry_order_ref`` has come to. Raises for any other order.

    Read under the repository's write fence, so the order row, its fills
    and its ENTER's state are of one moment.
    """
    with repo.write_fence():
        order = repo.order(entry_order_ref)
        entry = None if order is None or order.role != "ENTRY" else option_entry_of_order(repo, entry_order_ref)
        if order is None or entry is None:
            raise ValueError(f"{entry_order_ref!r} is not an entry in an option contract")
        effect = repo.effect_operation(order.effect_operation_id)
        assert effect is not None
        filled, _cost = repo.effective_fill_totals_for_order(entry_order_ref)
        fills_short = repo.order_fills_short_of_broker_cumulative(entry_order_ref)
        cancel_request = repo.last_order_transition(
            order_ref=entry_order_ref, transition_kind="ORDER_CANCEL_REQUESTED"
        )
    contracts = int(filled + FILL_QTY_EPSILON)
    standing = _standing(
        entry,
        broker_state=order.broker_state,
        effect_state=effect.state,
        contracts_filled=contracts,
        fills_short=fills_short,
        cancel_requested=cancel_request is not None,
    )
    return EntryOutcome(
        standing=standing,
        order_refs=(entry_order_ref,),
        units_asked=entry.units,
        units_filled=contracts // entry.the_leg.ratio,
        contracts_filled={entry.contract_terms.symbol: contracts},
        legs_out_of_step=False,
        cancel_at_ms=entry.cancel_at_ms,
        cancel_requested_at_ms=None if cancel_request is None else cancel_request["recorded_at_ms"],
        proven_at_ms=max(order.updated_at_ms, effect.updated_at_ms) if standing in PROVEN_STANDINGS else None,
    )


def _standing(
    entry: OptionEntryFacts,
    *,
    broker_state: str | None,
    effect_state: str,
    contracts_filled: int,
    fills_short: bool,
    cancel_requested: bool,
) -> EntryStanding:
    """The standing those facts come to. The first rule that applies decides."""
    asked = entry.order.quantity
    state = (broker_state or "").lower()
    if fills_short or effect_state == "unknown":
        # The broker counted a fill the books do not hold, or its last answer was lost.
        return EntryStanding.OUTCOME_UNKNOWN
    if contracts_filled > asked:
        # More is on the books than the order asked for: not an outcome an order can have.
        return EntryStanding.OUTCOME_UNKNOWN
    if contracts_filled == asked:
        return EntryStanding.FILLED
    if effect_state in FAILED_EFFECT_STATES and not state:
        # The ENTER ended and the broker never reported the order: it was refused before the
        # send, refused by the broker, or proven absent. A fill on it says the broker has it after all.
        return EntryStanding.OUTCOME_UNKNOWN if contracts_filled else EntryStanding.NOT_SENT
    if state in UNFILLED_TERMINAL_STATES:
        return EntryStanding.PART_FILLED if contracts_filled else EntryStanding.UNFILLED
    if state in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES:
        # The broker says filled, or replaced, and the books hold less than was asked for.
        return EntryStanding.OUTCOME_UNKNOWN
    if cancel_requested or state == "pending_cancel":
        return EntryStanding.CANCELLATION_REQUESTED
    return EntryStanding.WORKING


def settlement_for(outcome: EntryOutcome) -> EntrySettlement:
    """How the strategy is settled on ``outcome``: the one rule, for the live runner and for a replay.

    Anything filled: it is in. Proven to have ended with nothing filled, or
    never sent: the decision is dropped, as a refused entry is. Anything
    else is not proven yet, and the strategy is in: an exit will come and
    close whatever turns out to be held.
    """
    if outcome.units_filled >= 1:
        return EntrySettlement.ENTERED
    if outcome.standing in (EntryStanding.UNFILLED, EntryStanding.NOT_SENT):
        return EntrySettlement.DISCARDED
    return EntrySettlement.ENTERED


def settled_now(outcome: EntryOutcome, *, until_ms: int, now_ms: int) -> EntrySettledFacts:
    """How the strategy is settled on ``outcome`` at ``now_ms``, as the record of it."""
    return EntrySettledFacts(
        settlement=settlement_for(outcome).value,
        standing=outcome.standing.value,
        units_asked=outcome.units_asked,
        units_filled=outcome.units_filled,
        until_ms=until_ms,
        settled_at_ms=now_ms,
    )


def recorded_settlement(repo: ClerkSqliteRepository, entry_order_ref: str) -> SettledEntry | None:
    """The answer the Clerk recorded for the entry ``entry_order_ref`` (``ENTRY_SETTLED``), or ``None`` when it gave none."""
    with repo.write_fence():
        order = repo.order(entry_order_ref)
        if order is None:
            return None
        recorded = repo.first_effect_transition(
            effect_operation_id=order.effect_operation_id, transition_kind="ENTRY_SETTLED"
        )
    if recorded is None:
        return None
    return SettledEntry.of(
        EntrySettledFacts.from_facts_json(recorded["facts_json"]), order_ref=entry_order_ref, recorded=True
    )


def entry_settlement(repo: ClerkSqliteRepository, entry_order_ref: str) -> EntrySettlement:
    """How a strategy is settled on the option entry ``entry_order_ref``, asked after the fact: the record wins, else the books.

    The Clerk's recorded answer stands whatever the books say by now: it is
    what the strategy was told. With none recorded (the bot's run stopped
    before the Clerk answered) the rule is applied to the books as they
    stand, so two askers on either side of the moment an outcome is proven
    can be told differently. Raises for an order that is not an option entry.
    """
    recorded = recorded_settlement(repo, entry_order_ref)
    if recorded is not None:
        return recorded.settlement
    return settlement_for(entry_outcome(repo, entry_order_ref))


class EntryOutcomeWaitRefused(ValueError):
    """A wait for an entry's outcome was asked with a limit the Clerk does not take, and why by name."""

    def __init__(self, reason_code: str, explanation: str) -> None:
        super().__init__(f"{reason_code}: {explanation}")
        self.reason_code = reason_code
        self.explanation = explanation


# The furthest limit a wait for an outcome is taken with: a week. A limit is the close of the bot's next
# decision bar. For a daily bar that is three days on over a weekend and more over a holiday, and a bound
# of exactly one day refused a daily bar's own limit when the Clerk's clock was a millisecond behind the
# bar's close (ADR 0076 decision 19).
MAX_ENTRY_OUTCOME_WAIT_MS = 7 * 24 * 60 * 60 * 1000
ENTRY_OUTCOME_LIMIT_TOO_FAR = "ENTRY_OUTCOME_LIMIT_TOO_FAR"
ENTRY_OUTCOME_LIMIT_PAST = "ENTRY_OUTCOME_LIMIT_PAST"


__all__ = [
    "ENTRY_OUTCOME_LIMIT_PAST",
    "ENTRY_OUTCOME_LIMIT_TOO_FAR",
    "MAX_ENTRY_OUTCOME_WAIT_MS",
    "PROVEN_STANDINGS",
    "EntryOutcome",
    "EntryOutcomeWaitRefused",
    "EntrySettlement",
    "EntryStanding",
    "SettledEntry",
    "entry_outcome",
    "entry_settlement",
    "recorded_settlement",
    "settled_now",
    "settlement_for",
]
