"""An option entry worked out by the Clerk from the facts it was handed, or the reason it was not (ADR 0076).

The caller hands over which contract each leg's rule came to, its listed
multiplier and the quote it read just before. It hands over no price and no
money. Here the Clerk asks, in order, and the first "no" refuses the entry:

1. Is the plan one the Clerk takes today: one bought call or put?
2. Did the facts come, do they cover the plan's leg, and is each of them
   what it must be: two limits that are percents, a generation and a read
   time that are whole numbers, and the owner's order terms, each inside
   its bounds (an entry wait of a second to five minutes)?
3. Is the picked contract a standard one, of a hundred shares?
4. Has the picked contract's last session ended, and is each quote usable at
   the Clerk's own clock: one generation, both sides, live, not crossed, and
   no side older than the decision-data budget? Is each side priced in whole
   cents, as an order's price must be? The quote is tested once more just
   before the order is sent, at the clock then (``entry_quote_refusal``).
5. What would its fees be? The provision is the entry's own fills settled as
   a day of their own.
6. Is it a structure the plan may open, is each leg's market inside the
   owner's gap limit, what does it cost, and is its round-trip cost inside
   the owner's limit? (``app.options.entry.prepare_entry``)
7. Could the entry still be seen through? Its wait for a fill, the time to
   prove a cancel and a margin must all end before the close its position
   would owe today (``entry_past_cutoff``). The runner's recheck asks this
   at its own moment; the Clerk asks again at its own clock, here and once
   more just before the order is sent.

What comes back is the entry as the books keep it: the order, the contract,
the quotes, the money, and when the Clerk cancels what has not filled (its
own clock now, plus the owner's entry wait). Admission against the budget is
asked next, of the commitment worked out here (``envelope_admission``).

The order's limit price is written to the cent, with two decimals, whatever
way the quote wrote it: ``1.9`` and ``1.90`` are one price, and must make one
order and one identity hash. Nothing is rounded: a quote finer than a cent
was refused before.

Every refusal here names what the caller handed over. Anything else that goes
wrong while the money is worked out is a fault of this code, and is raised as
one: it is never told as evidence that was incomplete.

Formula: fee provision = ``option_fee_provision_cents`` of one buy of
  units x ratio contracts at the leg's ask; every other figure is
  ``prepare_entry``'s.
Reference: ADR 0076 decisions 2, 5, 7 and 16; ADR 0067 for the quote budget.
Canonical implementation: ``app/options/entry.py``,
  ``app/options/requirement.py``, ``app/broker/alpaca/option_fees.py``,
  ``app/options/deadlines.py`` for the entry cutoff. This file composes them
  and computes nothing of its own.
Validated against: tests/broker/alpaca/clerk/sqlite/test_option_entry_and_exit.py
  and tests/broker/alpaca/clerk/sqlite/test_option_entry_fences.py.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.option_effects import (
    STANDARD_CONTRACT_MULTIPLIER,
    OptionEffectRefusal,
    OptionEntryEvidence,
    OptionPlanRefused,
    the_one_bought_option_leg,
)
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts
from app.broker.alpaca.option_fees import OptionRateNotPinnedError, fees_for_option_fill, option_fee_provision_cents
from app.broker.contract.models import OrderSide
from app.broker.contract.options import SingleOptionOrderRequest, opening_order
from app.options.deadlines import CloseTerms, entry_cutoff_on, past_last_session
from app.options.entry import prepare_entry
from app.options.exact import whole_units
from app.options.quotes import quote_fault
from app.options.requirement import StructureMoneyRefused
from app.options.structure import ResolvedStructure
from app.options.structure_check import StructureRefused
from app.schemas.action_plan import ActionPlan, OptionEntryLeg
from app.services.market_liveness import QUOTE_MAX_AGE_MS
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

if TYPE_CHECKING:
    from app.options.contract import OptionContract
    from app.options.quotes import HeldQuote
    from app.options.structure import LegPosition

_CENT = Decimal("0.01")

# The terms an entry accepted before entries kept their own is held to (#2913).
ENTRY_CLOSE_TERMS = CloseTerms()

# What the owner's order terms may be, asked where the Clerk takes them (ADR 0076).
# An entry wait: a second at least, since a wait of nothing cancels the order as it is sent;
# five minutes at most, since the quotes it was priced from are seconds old.
MIN_ENTRY_WAIT_MS = 1_000
MAX_ENTRY_WAIT_MS = 5 * 60_000
# Every other term: no longer than a whole regular session, which nothing could be counted back from.
MAX_ORDER_TERM_MS = 390 * 60_000


@dataclass(frozen=True, slots=True)
class OptionEntryRefused:
    """Why an option entry was not worked out, as its own reason code and in plain words."""

    reason_code: str
    explanation: str


def _is_a_percent_limit(limit: object) -> bool:
    """A limit the owner set: an exact decimal, finite, zero or more."""
    return isinstance(limit, Decimal) and limit.is_finite() and limit >= 0


def _evidence_fault(leg: OptionEntryLeg, evidence: OptionEntryEvidence) -> str | None:
    """What is missing from, or malformed in, the facts handed over, in plain words; ``None`` when nothing is.

    Asked before anything is computed from them, so that a fault of the
    caller's is told as one and a fault found later is not mistaken for it.
    """
    handed = (evidence.picked, evidence.multipliers, evidence.quotes)
    if any(set(by_leg) != {leg.leg_id} for by_leg in handed):
        return (
            f"The plan opens leg {leg.leg_id!r}. A contract, a multiplier and a quote are needed for exactly that "
            f"leg; what came names {sorted({name for by_leg in handed for name in by_leg})}."
        )
    for name, limit in (
        ("leg gap limit", evidence.leg_gap_limit_percent), ("structure cost limit", evidence.cost_limit_percent)
    ):
        if not _is_a_percent_limit(limit):
            return f"The {name} handed over is {limit!r}. A limit is a percent, zero or more."
    if type(evidence.generation) is not int:
        return f"The quotes' generation handed over is {evidence.generation!r}. It is a whole number."
    if type(evidence.rechecked_at_ms) is not int or not 0 <= evidence.rechecked_at_ms <= MAX_TIMESTAMP_MS:
        return f"The time the quotes were read is {evidence.rechecked_at_ms!r}. It is a time in milliseconds."
    if not isinstance(evidence.terms, CloseTerms):
        return f"The order terms handed over are {evidence.terms!r}. They are the owner's entry wait and close leads."
    return _terms_fault(evidence.terms)


def _terms_fault(terms: CloseTerms) -> str | None:
    """What is out of bounds in the owner's order terms, in plain words; ``None`` when nothing is.

    Asked here so that a wait of nothing, of hours, or too large to be a
    time at all is a refusal by name, and never an error out of the entry's
    own record further on.
    """
    if not MIN_ENTRY_WAIT_MS <= terms.entry_wait_ms <= MAX_ENTRY_WAIT_MS:
        return (
            f"The entry wait handed over is {terms.entry_wait_ms:,} ms. An entry waits for its fill between "
            f"{MIN_ENTRY_WAIT_MS:,} and {MAX_ENTRY_WAIT_MS:,} ms."
        )
    for name, value in (
        ("time allowed to prove a cancel", terms.cancel_allowance_ms),
        ("entry margin", terms.entry_margin_ms),
        ("lead of the close on an expiry day", terms.expiry_close_lead_ms),
        ("lead of a sold leg's close", terms.sold_leg_close_lead_ms),
    ):
        if value > MAX_ORDER_TERM_MS:
            return (
                f"The {name} handed over is {value:,} ms. No order term is longer than a regular session, "
                f"{MAX_ORDER_TERM_MS:,} ms."
            )
    return None


def _written_to_the_cent(price: Decimal) -> Decimal:
    """``price`` in its one written form: whole cents, two decimals. Refuses a price finer than a cent."""
    cents = whole_units(price, units_per_one=100, what="an option order's limit price", unit="cents")
    return Decimal(cents).scaleb(-2)


def entry_past_cutoff(structure: ResolvedStructure, *, now_ms: int, terms: CloseTerms) -> OptionEntryRefused | None:
    """Why no entry on ``structure`` is opened at the Clerk's clock ``now_ms``, or ``None`` when one still may be.

    ``terms`` are the entry's own: the owner's wait, cancel allowance, margin
    and close leads that came with it and are kept with it.

    An entry admitted seconds before its contract's last close could never
    be closed again: nothing is sold after a contract's last session. So an
    entry is refused from the cutoff on: the close its position would owe
    today, less the entry's own wait for a fill, the time to prove a cancel
    and a margin (``app.options.deadlines.entry_cutoff_on``). The close is
    counted back from the calendar's session close, so a half day's cutoff
    is a half day's. A structure that would owe no close today has none.

    Asked only of a contract that still trades in a session that is open:
    the expiry and the session are each asked before this.
    """
    cutoff_ms = entry_cutoff_on(structure, et_date_at_ms(now_ms), terms)
    if cutoff_ms is None or now_ms < cutoff_ms:
        return None
    contracts = ", ".join(leg.contract.occ_symbol for leg in structure.legs)
    return OptionEntryRefused(
        OptionEffectRefusal.PAST_ENTRY_CUTOFF.value,
        f"An entry on {contracts} reached the account Clerk too close to the time its position must be closed "
        "by today for the entry to be seen through: its wait for a fill and for a cancel to be proven would "
        "run past that time. Nothing was opened.",
    )


def entry_quote_refusal(
    contract: OptionContract,
    quote: HeldQuote,
    *,
    position: LegPosition,
    generation: int,
    now_ms: int,
    at_the_send: bool = False,
) -> OptionEntryRefused | None:
    """Why the quote an entry's leg is priced from cannot be used at the Clerk's clock ``now_ms``, or ``None``.

    Asked as the entry is worked out and again just before its order is sent
    (``at_the_send``): by the same tests, each side's age counted at the
    clock then. The entry is worked out under the intake lock and sent after
    it, so a quote that passed can be older than the decision-data budget by
    the time its order goes out. A simulation fills from the quote it was
    handed whatever its age, and a real account's limit order rests at the
    price that quote gave. The refusal is the quote's own fault code both
    times. ``generation`` is the one the quotes were read in.
    """
    fault = quote_fault(
        quote, at_ms=now_ms, generation=generation, max_age_ms=QUOTE_MAX_AGE_MS, needs_a_bid=position == "short"
    )
    if fault is None:
        return None
    when = "when its order was about to be sent" if at_the_send else "when the entry reached the account Clerk"
    return OptionEntryRefused(
        fault.value,
        f"The quote for {contract.occ_symbol} could not be used {when} "
        f"({fault.name.lower().replace('_', ' ')}). A quote is used only when both sides arrived live in the "
        f"current connection, the bid is not above the ask, and neither side is older than "
        f"{QUOTE_MAX_AGE_MS:,} ms.",
    )


def work_out_option_entry(
    action_plan: ActionPlan, *, evidence: OptionEntryEvidence | None, units: int, now_ms: int
) -> OptionEntryFacts | OptionEntryRefused:
    """The entry ``action_plan`` makes of the facts handed over, at the Clerk's clock ``now_ms``, or why it makes none."""
    try:
        leg = the_one_bought_option_leg(action_plan)
    except OptionPlanRefused as exc:
        return OptionEntryRefused(exc.reason.value, exc.detail)
    if evidence is None:
        return OptionEntryRefused(
            OptionEffectRefusal.ENTRY_EVIDENCE_MISSING.value,
            "This entry came with no picked contract and no quotes. An option entry is priced from the quotes "
            "handed over with it, so no order was made.",
        )
    malformed = _evidence_fault(leg, evidence)
    if malformed is not None:
        return OptionEntryRefused(OptionEffectRefusal.ENTRY_EVIDENCE_INCOMPLETE.value, malformed)
    contract, quote = evidence.picked[leg.leg_id], evidence.quotes[leg.leg_id]
    multiplier = evidence.multipliers[leg.leg_id]
    if type(multiplier) is not int or multiplier != STANDARD_CONTRACT_MULTIPLIER:
        # The multiplier is the caller's word until the contract reference is read. Only the
        # standard one is taken on it: any other would count every dollar of the entry wrongly.
        return OptionEntryRefused(
            OptionEffectRefusal.CONTRACT_NOT_STANDARD.value,
            f"{contract.occ_symbol} was handed over with a multiplier of {multiplier!r}. A bot trades standard "
            f"contracts only, of {STANDARD_CONTRACT_MULTIPLIER} shares each.",
        )
    if past_last_session(contract.expiry_date, now_ms):
        # How long before its last trade an entry is still taken is the
        # runner's recheck to say; a contract that has stopped trading never is.
        return OptionEntryRefused(
            OptionEffectRefusal.CONTRACT_EXPIRED.value,
            f"{contract.occ_symbol} expired on {contract.expiry_date.isoformat()} and its last session is over; "
            "nothing can be bought in it.",
        )
    unusable = entry_quote_refusal(
        contract, quote, position=leg.position, generation=evidence.generation, now_ms=now_ms
    )
    if unusable is not None:
        return unusable
    assert quote.bid is not None and quote.ask is not None
    finer_than_a_cent = [side.price for side in (quote.bid, quote.ask) if side.price % _CENT]
    if finer_than_a_cent:
        return OptionEntryRefused(
            OptionEffectRefusal.ENTRY_EVIDENCE_INCOMPLETE.value,
            f"The quote for {contract.occ_symbol} is bid {quote.bid.price} and offered at {quote.ask.price}. "
            "An order's price is in whole cents, so no order can be made of it.",
        )
    try:
        fee_provision_cents = option_fee_provision_cents([
            fees_for_option_fill(
                trade_date=et_date_at_ms(now_ms), side=OrderSide.BUY, contracts=Decimal(units * leg.qty_ratio),
                premium=quote.ask.price, multiplier=multiplier,
            )
        ])
    except OptionRateNotPinnedError as exc:
        return OptionEntryRefused(
            OptionEffectRefusal.FEE_RATE_NOT_PINNED.value,
            f"The option fee model has no rate for today for: {', '.join(exc.components)}. The entry's fees "
            "cannot be set aside, so it was not admitted.",
        )
    try:
        prepared = prepare_entry(
            action_plan,
            picked=evidence.picked,
            multipliers=evidence.multipliers,
            quotes=evidence.quotes,
            units=units,
            fee_provision_cents=fee_provision_cents,
            leg_gap_limit_percent=evidence.leg_gap_limit_percent,
            cost_limit_percent=evidence.cost_limit_percent,
        )
    except (StructureRefused, StructureMoneyRefused) as exc:
        return OptionEntryRefused(exc.reason.value, str(exc))
    too_late = entry_past_cutoff(prepared.structure, now_ms=now_ms, terms=evidence.terms)
    if too_late is not None:
        return too_late
    prepared = replace(prepared, net_limit_price=_written_to_the_cent(prepared.net_limit_price))
    order = opening_order(prepared.structure, units=units, net_limit_price=prepared.net_limit_price)
    assert isinstance(order, SingleOptionOrderRequest)
    return OptionEntryFacts.of(
        prepared,
        order=order,
        fee_provision_cents=fee_provision_cents,
        leg_gap_limit_percent=evidence.leg_gap_limit_percent,
        cost_limit_percent=evidence.cost_limit_percent,
        quote_generation=evidence.generation,
        rechecked_at_ms=evidence.rechecked_at_ms,
        admitted_at_ms=now_ms,
        terms=evidence.terms,
    )


__all__ = [
    "ENTRY_CLOSE_TERMS",
    "MAX_ENTRY_WAIT_MS",
    "MAX_ORDER_TERM_MS",
    "MIN_ENTRY_WAIT_MS",
    "OptionEntryRefused",
    "entry_past_cutoff",
    "entry_quote_refusal",
    "work_out_option_entry",
]
