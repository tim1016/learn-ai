"""Admission for a sealed session leg against its bot's confirmed portfolio.

Formula: reserve = max(0, collateral_after - collateral_before)
  + max(0, premium_paid) + fee provision. Unobserved sale proceeds are not spent.
  The quote-cost denominator is the held portfolio's basis plus collateral
  after this entry, including this entry's signed premium and provision.
Reference: ADR 0076 (cash custody and portfolio requirement), ADR 0078 (cover).
Canonical implementation: ``options/requirement.py`` owns collateral,
  natural prices and costs; ``options_backtest/cover_book.py`` owns cover.
Validated against: tests/broker/alpaca/clerk/test_options_leg_custody.py.
"""

from __future__ import annotations

from decimal import Decimal

from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionEntryEvidence
from app.broker.alpaca.clerk.options_legs import OptionsLegInstruction, allocation_book
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.option_admission import (
    OptionEntryRefused,
    _evidence_fault,
    _leg_refusal,
    _written_to_the_cent,
    entry_past_cutoff,
)
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts, OptionsAttemptFacts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.option_fees import OptionRateNotPinnedError, entry_fee_provision_cents
from app.broker.contract.options import opening_order
from app.options.entry import LegGapOverLimit, PreparedEntry, bid_ask_gap_percent_of_mid
from app.options.exact import whole_units
from app.options.requirement import (
    EntryMoney,
    EntrySize,
    LegQuote,
    StructureMoneyRefusal,
    natural_net_price,
    round_trip_cost,
    round_trip_cost_is_over_limit,
)
from app.options.structure import ResolvedLeg, ResolvedStructure
from app.options_backtest.cover_book import Candidate, admit_opening, collateral


def work_out_session_leg(
    repo: ClerkSqliteRepository, strategy_instance_id: str, instruction: OptionsLegInstruction,
    evidence: OptionEntryEvidence | None, *, now_ms: int,
) -> OptionEntryFacts | OptionEntryRefused:
    """Keep existing quote, fee, price-step and time gates for a portfolio leg."""
    legs = instruction.on_enter
    if evidence is None:
        return OptionEntryRefused(OptionEffectRefusal.ENTRY_EVIDENCE_MISSING.value, "No option quote evidence was supplied.")
    malformed = _evidence_fault(legs, evidence)
    if malformed is not None:
        return OptionEntryRefused(OptionEffectRefusal.ENTRY_EVIDENCE_INCOMPLETE.value, malformed)
    leg = legs[0]
    refusal = _leg_refusal(leg, evidence, now_ms=now_ms)
    if refusal is not None:
        return refusal
    contract = evidence.picked[leg.leg_id]
    if contract.root != instruction.setup.underlying or contract.right != instruction.leg.right:
        return OptionEntryRefused("OPTIONS_LEG_CONTRACT_MISMATCH", "The picked contract is not this setup leg's kind and underlying.")
    quote = evidence.quotes[leg.leg_id]
    assert quote.bid is not None and quote.ask is not None
    if bid_ask_gap_percent_of_mid(quote.bid.price, quote.ask.price) > evidence.leg_gap_limit_percent:
        refused = LegGapOverLimit(
            leg_id=leg.leg_id, contract=contract, bid=quote.bid.price, ask=quote.ask.price,
            limit_percent=evidence.leg_gap_limit_percent,
        )
        return OptionEntryRefused(refused.reason.value, str(refused))
    structure = ResolvedStructure(legs=(ResolvedLeg(
        leg_id=leg.leg_id, contract=contract, position=leg.position, ratio=1,
    ),))
    markets = {leg.leg_id: LegQuote(quote.bid.price, quote.ask.price)}
    size = EntrySize(multiplier=100, units=instruction.leg.contracts)
    net = natural_net_price(structure, markets)
    try:
        fees = entry_fee_provision_cents(structure, markets, size, trade_date_ms=now_ms)
    except OptionRateNotPinnedError as exc:
        return OptionEntryRefused(OptionEffectRefusal.FEE_RATE_NOT_PINNED.value, str(exc))
    premium = whole_units(net, units_per_one=size.units * size.multiplier * 100, what="entry premium", unit="cents")
    held, closing = allocation_book(repo, strategy_instance_id, instruction.setup)
    if any(holding.leg_id == leg.leg_id for holding in held.holdings):
        return OptionEntryRefused("OPTIONS_LEG_ALREADY_HELD", "This leg's earlier attempt still holds contracts.")
    before = collateral(held)
    candidate = Candidate(
        leg.leg_id, contract, instruction.leg.side, size.units, size.multiplier,
        cash_effect=-Decimal(premium + fees).scaleb(-2),
    )
    # Cash is admitted by the existing budget authority below. The cover book
    # is asked only for its independent identity/expiry/coverage policy here.
    judged = admit_opening(held, (candidate,), cash=None, closing=closing)
    if judged.refused:
        return OptionEntryRefused("OPTIONS_LEG_" + judged.refused[0].reason.value.upper(), "The confirmed portfolio does not permit this opening: " + judged.refused[0].reason.value)
    after = collateral(type(held)(held.listed, (*held.holdings, candidate)))
    added = whole_units(max(Decimal(0), after - before), units_per_one=100, what="added collateral", unit="cents")
    reserved = added + max(0, premium)
    cost = round_trip_cost(structure, markets, size)
    basis = _held_basis_cents(repo, strategy_instance_id)
    portfolio_commitment = basis + whole_units(after, units_per_one=100, what="collateral", unit="cents") + premium + fees
    if round_trip_cost_is_over_limit(
        cost_cents=cost, commitment_cents=max(1, portfolio_commitment), limit_percent=evidence.cost_limit_percent,
    ):
        return OptionEntryRefused(StructureMoneyRefusal.COST_OVER_LIMIT.value, "Opening and closing this leg exceeds the sealed cost limit of its portfolio commitment.")
    prepared = PreparedEntry(
        structure, size, _written_to_the_cent(net),
        EntryMoney(premium, added, reserved + fees, None), cost, evidence.quotes,
    )
    too_late = entry_past_cutoff(structure, now_ms=now_ms, terms=evidence.terms)
    if too_late is not None:
        return too_late
    order = opening_order(structure, units=size.units, net_limit_price=prepared.net_limit_price)
    entry = OptionEntryFacts.of(
        prepared, order=order, fee_provision_cents=fees,
        leg_gap_limit_percent=evidence.leg_gap_limit_percent, cost_limit_percent=evidence.cost_limit_percent,
        quote_generation=evidence.generation, rechecked_at_ms=evidence.rechecked_at_ms,
        admitted_at_ms=now_ms, terms=evidence.terms,
    )
    return entry.model_copy(update={"options_attempt": OptionsAttemptFacts(
        leg_id=leg.leg_id, attempt_id=instruction.attempt_id,
        setup_sha256=instruction.session_plan.setup_sha256, reservation_before_fees_cents=reserved,
    )})


def _held_basis_cents(repo: ClerkSqliteRepository, strategy_instance_id: str) -> int:
    """Read current portfolio basis from the canonical FIFO money projection."""
    from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
    from app.broker.alpaca.clerk.option_position_money import option_position_money

    records = effective_fill_records(repo._conn, account_id=repo.account_id, strategy_instance_ids=(strategy_instance_id,))
    fifo = compute_fifo_pnl(records)
    cost, collateral_dollars = option_position_money([lot for lot in fifo.open_lots if lot.multiplier is not None])
    return whole_units(cost - collateral_dollars, units_per_one=100, what="held basis", unit="cents")
