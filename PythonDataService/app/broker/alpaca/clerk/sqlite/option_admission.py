"""An option entry worked out by the Clerk from the facts it was handed, or the reason it was not (ADR 0076).

The caller hands over which contract each leg's rule came to, its listed
multiplier and the quote it read just before. It hands over no price and no
money. Here the Clerk asks, in order, and the first "no" refuses the entry:

1. Is the plan one the Clerk takes today: one bought call or put?
2. Did the facts come, do they cover the plan's leg, and is each of them
   what it must be: two limits that are percents, a generation and a read
   time that are whole numbers?
3. Is the picked contract a standard one, of a hundred shares?
4. Has the picked contract's last session ended, and is each quote usable at
   the Clerk's own clock: one generation, both sides, live, not crossed, and
   no side older than the decision-data budget? Is each side priced in whole
   cents, as an order's price must be?
5. What would its fees be? The provision is the entry's own fills settled as
   a day of their own.
6. Is it a structure the plan may open, is each leg's market inside the
   owner's gap limit, what does it cost, and is its round-trip cost inside
   the owner's limit? (``app.options.entry.prepare_entry``)

What comes back is the entry as the books keep it: the order, the contract,
the quotes and the money. Admission against the budget is asked next, of the
commitment worked out here (``envelope_admission``).

Every refusal here names what the caller handed over. Anything else that goes
wrong while the money is worked out is a fault of this code, and is raised as
one: it is never told as evidence that was incomplete.

Formula: fee provision = ``option_fee_provision_cents`` of one buy of
  units x ratio contracts at the leg's ask; every other figure is
  ``prepare_entry``'s.
Reference: ADR 0076 decisions 2, 5 and 7; ADR 0067 for the quote budget.
Canonical implementation: ``app/options/entry.py``,
  ``app/options/requirement.py``, ``app/broker/alpaca/option_fees.py``. This
  file composes them and computes nothing of its own.
Validated against: tests/broker/alpaca/clerk/sqlite/test_option_entry_and_exit.py
  and tests/broker/alpaca/clerk/sqlite/test_option_entry_fences.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

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
from app.options.deadlines import past_last_session
from app.options.entry import prepare_entry
from app.options.quotes import quote_fault
from app.options.requirement import StructureMoneyRefused
from app.options.structure_check import StructureRefused
from app.schemas.action_plan import ActionPlan, OptionEntryLeg
from app.services.market_liveness import QUOTE_MAX_AGE_MS
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

_CENT = Decimal("0.01")


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
    return None


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
            "This entry came with no picked contract and no quotes. A simulation fills only from the quotes kept "
            "with an entry, so nothing was filled.",
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
    fault = quote_fault(
        quote, at_ms=now_ms, generation=evidence.generation, max_age_ms=QUOTE_MAX_AGE_MS,
        needs_a_bid=leg.position == "short",
    )
    if fault is not None:
        return OptionEntryRefused(
            fault.value,
            f"The quote for {contract.occ_symbol} could not be used when the entry reached the account Clerk "
            f"({fault.name.lower().replace('_', ' ')}). A quote is used only when both sides arrived live in the "
            f"current connection, the bid is not above the ask, and neither side is older than "
            f"{QUOTE_MAX_AGE_MS:,} ms.",
        )
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
    )


__all__ = ["OptionEntryRefused", "work_out_option_entry"]
