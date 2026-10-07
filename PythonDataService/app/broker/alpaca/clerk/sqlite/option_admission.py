"""An option entry worked out by the Clerk from the facts it was handed, or the reason it was not (ADR 0076).

The caller hands over which contract each leg's rule came to, its listed
multiplier and the quote it read just before. It hands over no price and no
money. Here the Clerk asks, in order, and the first "no" refuses the entry:

1. Is the plan one the Clerk takes today (``option_plan_taken``)?
2. Did the facts come, do they cover exactly the plan's legs, and is each of
   them what it must be: two limits that are percents, a generation and a
   read time that are whole numbers, and the owner's order terms, each
   inside its bounds (an entry wait of a second to five minutes)?
3. Each leg in turn, in the plan's order. Is its picked contract a standard
   one, of a hundred shares? Has its last session ended? Is its quote usable
   at the Clerk's own clock: one generation, both sides, live, not crossed,
   and no side older than the decision-data budget? A sold leg is entered
   at its bid, so it needs one. Is each side priced in whole cents, as an
   order's price must be? The refusal names the contract. Each quote is
   tested once more just before the order is sent, at the clock then
   (``entry_quote_refusal``).
4. Are the picks the plan's legs, on the plan's underlying
   (``resolved_structure``)? And for a structure of several legs: is its
   net price, each leg at its natural price, exactly zero? One order of
   several legs pays or receives, and the broker's reference describes
   nothing between, so it is refused by name before any order is made.
5. What would its fees be? The provision is every leg's own fill, a bought
   leg at its ask and a sold leg at its bid, settled as a day of their own
   (``entry_fee_provision_cents``).
6. Is it a structure the plan may open, is each leg's market inside the
   owner's gap limit, what does it cost, and is its round-trip cost inside
   the owner's limit? (``app.options.entry.prepare_entry``) A structure
   whose legs do not share an expiry and that has a sold leg is refused
   here, under the money rule's own name.
7. Could the entry still be seen through? Its wait for a fill, the time to
   prove a cancel and a margin must all end before the close its position
   would owe today (``entry_past_cutoff``). The runner's recheck asks this
   at its own moment; the Clerk asks again at its own clock, here and once
   more just before the order is sent.

What comes back is the entry as the books keep it: the order, each leg's
contract and quote, the money, and when the Clerk cancels what has not
filled (its own clock now, plus the owner's entry wait). One leg makes one
order of one contract (``OptionEntryFacts``). Two to four make one order of
several legs at one signed net price, positive when it pays and negative
when it receives (``MultiLegOptionEntryFacts``). Admission against the
budget is asked next, of the commitment worked out here
(``envelope_admission``).

The order's limit price is written to the cent, with two decimals, whatever
way the quote wrote it: ``1.9`` and ``1.90`` are one price, and must make one
order and one identity hash. Nothing is rounded: a quote finer than a cent
was refused before. No price step is applied: none is in the broker's
documents, and this is the one place a step would go.

One more refusal needs the books and is asked by the caller, of the entry
worked out here: ``held_on_the_other_side``.

Every refusal here names what the caller handed over. Anything else that goes
wrong while the money is worked out is a fault of this code, and is raised as
one: it is never told as evidence that was incomplete.

Formula: fee provision = ``entry_fee_provision_cents`` of the structure: one
  fill a leg of units x ratio contracts, a bought leg at its ask and a sold
  leg at its bid; every other figure is ``prepare_entry``'s.
Reference: ADR 0076 decisions 2, 5, 7 and 16; ADR 0067 for the quote budget.
Canonical implementation: ``app/options/entry.py``,
  ``app/options/requirement.py``, ``app/broker/alpaca/option_fees.py``,
  ``app/options/deadlines.py`` for the entry cutoff. This file composes them
  and computes nothing of its own.
Validated against: tests/broker/alpaca/clerk/sqlite/test_option_entry_and_exit.py,
  tests/broker/alpaca/clerk/sqlite/test_option_entry_fences.py and, for
  several legs, tests/broker/alpaca/clerk/sqlite/test_several_legs_admission.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.option_effects import (
    STANDARD_CONTRACT_MULTIPLIER,
    OptionEffectRefusal,
    OptionEntryEvidence,
    OptionPlanRefused,
    option_plan_taken,
)
from app.broker.alpaca.clerk.sqlite.option_entry import MultiLegOptionEntryFacts, OptionEntry, OptionEntryFacts
from app.broker.alpaca.option_fees import OptionRateNotPinnedError, entry_fee_provision_cents
from app.broker.contract.options import MultiLegOptionOrderRequest, opening_order
from app.options.deadlines import CloseTerms, entry_cutoff_on, past_last_session
from app.options.entry import prepare_entry
from app.options.exact import whole_units
from app.options.order_terms import MAX_ENTRY_WAIT_MS, MAX_ORDER_TERM_MS, MIN_ENTRY_WAIT_MS
from app.options.quotes import quote_fault
from app.options.requirement import EntrySize, LegQuote, StructureMoneyRefused, natural_net_price
from app.options.structure import ResolvedStructure
from app.options.structure_check import StructureRefused, resolved_structure
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

# What the owner's order terms may be is asked here, where the Clerk takes them (ADR 0076). The bounds are
# the Clerk's own safety limits, and are stated once, beside the narrower ranges an owner is offered
# (``app.options.order_terms``): ``MIN_ENTRY_WAIT_MS``, ``MAX_ENTRY_WAIT_MS`` and ``MAX_ORDER_TERM_MS``.


@dataclass(frozen=True, slots=True)
class OptionEntryRefused:
    """Why an option entry was not worked out, as its own reason code and in plain words."""

    reason_code: str
    explanation: str


def _is_a_percent_limit(limit: object) -> bool:
    """A limit the owner set: an exact decimal, finite, zero or more."""
    return isinstance(limit, Decimal) and limit.is_finite() and limit >= 0


def _evidence_fault(legs: Sequence[OptionEntryLeg], evidence: OptionEntryEvidence) -> str | None:
    """What is missing from, or malformed in, the facts handed over, in plain words; ``None`` when nothing is.

    Asked before anything is computed from them, so that a fault of the
    caller's is told as one and a fault found later is not mistaken for it.
    What came must name exactly the plan's legs: a leg handed over with no
    quote is a leg the entry cannot be priced from, and is told here by name.
    """
    handed = (evidence.picked, evidence.multipliers, evidence.quotes)
    leg_ids = [leg.leg_id for leg in legs]
    if any(set(by_leg) != set(leg_ids) for by_leg in handed):
        opens = f"leg {leg_ids[0]!r}" if len(leg_ids) == 1 else f"legs {', '.join(repr(leg_id) for leg_id in leg_ids)}"
        exactly = "exactly that leg" if len(leg_ids) == 1 else "exactly those legs, each of them"
        return (
            f"The plan opens {opens}. A contract, a multiplier and a quote are needed for {exactly}; "
            f"what came names {sorted({name for by_leg in handed for name in by_leg})}."
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


def _leg_refusal(
    leg: OptionEntryLeg, evidence: OptionEntryEvidence, *, now_ms: int
) -> OptionEntryRefused | None:
    """Why one leg's picked contract or its quote cannot be entered on at the Clerk's clock, or ``None``.

    The four questions a leg is asked, in order. Each refusal names the
    contract, so an entry of several legs says which leg it was.
    """
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
    return None


def work_out_option_entry(
    action_plan: ActionPlan, *, evidence: OptionEntryEvidence | None, units: int, now_ms: int, real_account: bool = True
) -> OptionEntry | OptionEntryRefused:
    """The entry ``action_plan`` makes of the facts handed over, at the Clerk's clock ``now_ms``, or why it makes none.

    ``real_account`` is the account's own word to the one rule
    (``option_plan_taken``): a Paper or a Live account, which a plan of
    several legs needs the deployment's setting for.
    """
    try:
        taken = option_plan_taken(action_plan, real_account=real_account)
    except OptionPlanRefused as exc:
        return OptionEntryRefused(exc.reason.value, exc.detail)
    if evidence is None:
        return OptionEntryRefused(
            OptionEffectRefusal.ENTRY_EVIDENCE_MISSING.value,
            "This entry came with no picked contract and no quotes. An option entry is priced from the quotes "
            "handed over with it, so no order was made.",
        )
    malformed = _evidence_fault(taken.legs, evidence)
    if malformed is not None:
        return OptionEntryRefused(OptionEffectRefusal.ENTRY_EVIDENCE_INCOMPLETE.value, malformed)
    for leg in taken.legs:
        refused = _leg_refusal(leg, evidence, now_ms=now_ms)
        if refused is not None:
            return refused
    markets = {
        leg.leg_id: LegQuote(bid=evidence.quotes[leg.leg_id].bid.price, ask=evidence.quotes[leg.leg_id].ask.price)  # type: ignore[union-attr]
        for leg in taken.legs
    }
    try:
        structure = resolved_structure(action_plan, evidence.picked)
        if len(taken.legs) > 1 and natural_net_price(structure, markets) == 0:
            contracts = ", ".join(leg.contract.occ_symbol for leg in structure.legs)
            return OptionEntryRefused(
                OptionEffectRefusal.ENTRY_NET_PRICE_ZERO.value,
                f"Bought at their asks and sold at their bids, the legs of this structure ({contracts}) come to a "
                "net price of exactly zero. One order of several legs pays or receives its net price; an order "
                "at nothing is not one the broker describes. No order was made.",
            )
        fee_provision_cents = entry_fee_provision_cents(
            structure,
            markets,
            EntrySize(multiplier=STANDARD_CONTRACT_MULTIPLIER, units=units),
            trade_date=et_date_at_ms(now_ms),
        )
    except StructureRefused as exc:
        return OptionEntryRefused(exc.reason.value, str(exc))
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
    kept_with_it = {
        "fee_provision_cents": fee_provision_cents,
        "leg_gap_limit_percent": evidence.leg_gap_limit_percent,
        "cost_limit_percent": evidence.cost_limit_percent,
        "quote_generation": evidence.generation,
        "rechecked_at_ms": evidence.rechecked_at_ms,
        "admitted_at_ms": now_ms,
        "terms": evidence.terms,
    }
    if isinstance(order, MultiLegOptionOrderRequest):
        return MultiLegOptionEntryFacts.of(prepared, order=order, **kept_with_it)
    return OptionEntryFacts.of(prepared, order=order, **kept_with_it)


@dataclass(frozen=True, slots=True)
class HeldByAnother:
    """One contract of an entry that the account already holds for someone else, and how."""

    # The contract's OCC symbol.
    symbol: str
    # The other bot's id, or ``None`` for a holding no bot of the account has: the owner's own orders.
    strategy_instance_id: str | None
    # Signed: bought above zero, sold below.
    quantity: float


def held_on_the_other_side(entry: OptionEntry, others: Sequence[HeldByAnother]) -> OptionEntryRefused | None:
    """Why ``entry`` would trade against what the account already holds for someone else, or ``None``.

    ``others`` are the account's holdings, outside this bot, in the
    entry's contracts (``reads.option_contracts_held_by_others``). A leg
    that sells a contract another bot or the owner holds bought, or buys one
    they hold sold, is refused: at the broker the two are one position of
    nothing. No expiry and no assignment would then ever be reported for
    either, and the books could not say whose it was (ADR 0076).

    A leg on the same side as another holding is taken: two bots may hold
    one contract the same way.
    """
    opening = {leg.contract.occ_symbol: 1 if leg.position == "long" else -1 for leg in entry.legs}
    against = [held for held in others if held.quantity * opening.get(held.symbol, 0) < 0]
    if not against:
        return None
    first = against[0]
    holder = "The owner's own orders hold" if first.strategy_instance_id is None else f"Bot {first.strategy_instance_id} holds"
    would, held_as = ("sell", "bought") if first.quantity > 0 else ("buy", "sold")
    return OptionEntryRefused(
        OptionEffectRefusal.CONTRACT_HELD_ON_THE_OTHER_SIDE.value,
        f"This entry would {would} {first.symbol}. {holder} {abs(first.quantity):g} of that contract {held_as} on "
        "this account. At the broker the two would cancel into one position of nothing, which the books could "
        "not keep apart. Nothing was opened.",
    )


__all__ = [
    "ENTRY_CLOSE_TERMS",
    "MAX_ENTRY_WAIT_MS",
    "MAX_ORDER_TERM_MS",
    "MIN_ENTRY_WAIT_MS",
    "HeldByAnother",
    "OptionEntryRefused",
    "entry_past_cutoff",
    "entry_quote_refusal",
    "held_on_the_other_side",
    "work_out_option_entry",
]
