"""An option structure enters through the real Dry Run Clerk and its retained simulation (#2913)."""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.options.named_structures import NamedStructure
from app.options.structure_catalogue import starting_plan
from app.schemas.action_plan import ActionPlan
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import (
    PAID_PICKS,
    PAID_SPREAD,
    RECEIVED_PICKS,
    RECEIVED_SPREAD,
    STRADDLE,
    STRADDLE_PICKS,
    Pick,
    evidence,
)
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import SID, _DryRun
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import dry_run as _dry_run_fixture
from tests.options.test_structure_check import _AS_NAMED, _picked

dry_run = _dry_run_fixture


@pytest.mark.parametrize(("plan", "picks", "reference"), [
    (PAID_SPREAD, PAID_PICKS, "194.00"), (RECEIVED_SPREAD, RECEIVED_PICKS, "193.00"),
    (STRADDLE, STRADDLE_PICKS, "600.00"),
])
async def test_every_contract_fills_under_one_parent_and_settles_one_whole_structure(
    dry_run: _DryRun, plan: ActionPlan, picks: Mapping[str, Pick], reference: str,
) -> None:
    receipt = await dry_run.enter(evidence(dry_run.clock(), picks), decision="structure-entry", plan=plan)
    assert receipt.state is EffectOperationState.SUBMITTED, receipt.explanation
    (parent,) = receipt.child_order_refs
    assert dry_run.held() == {
        picks[leg.leg_id][0].occ_symbol: 1.0 if leg.position == "long" else -1.0 for leg in plan.on_enter
    }
    assert len(dry_run.fills()) == 2 and dry_run.repo.fills_for_order(parent) == []
    reserved_units, reserved_reference, _fees = dry_run.reservations()[0]
    assert (reserved_units, Decimal(reserved_reference)) == (1.0, Decimal(reference))
    settled = await dry_run.clerk.await_entry_outcome(
        strategy_instance_id=SID, decision_id="structure-entry", until_ms=dry_run.clock(),
    )
    assert settled.standing is EntryStanding.FILLED and settled.units_filled == 1
    fills = dry_run.clerk.option_fills_of_decision(SID, "structure-entry")
    assert len(fills) == 2 and all(fill.quote_age_ms == 300 and fill.fill_model == "natural_price" for fill in fills)


def _catalogue_picks(name: NamedStructure) -> Mapping[str, Pick]:
    contracts, _shape_price = _AS_NAMED[name]
    bids = {"C580": "6.20", "C585": "3.10", "C590": "1.20", "C595": "0.50", "C600": "0.18",
            "P570": "0.18", "P575": "0.50", "P580": "1.20", "P585": "2.82", "P590": "5.50"}
    return {
        leg_id: (contract, bids[contracts[leg_id]], str(Decimal(bids[contracts[leg_id]]) + Decimal("0.01")))
        for leg_id, contract in _picked(**contracts).items()
    }


@pytest.mark.parametrize("name", list(NamedStructure))
async def test_every_catalogue_structure_enters_and_counts_its_exact_leg_ratios(dry_run: _DryRun, name: NamedStructure) -> None:
    plan, picks = starting_plan(name, "SPY"), _catalogue_picks(name)
    receipt = await dry_run.enter(evidence(dry_run.clock(), picks), decision="catalogue-entry", plan=plan)
    assert receipt.state is EffectOperationState.SUBMITTED, receipt.explanation
    (parent,) = receipt.child_order_refs
    settled = await dry_run.clerk.await_entry_outcome(
        strategy_instance_id=SID, decision_id="catalogue-entry", until_ms=dry_run.clock(),
    )
    assert settled.standing is EntryStanding.FILLED and settled.units_filled == 1
    assert dry_run.held() == {
        picks[leg.leg_id][0].occ_symbol: float(leg.qty_ratio) * (1 if leg.position == "long" else -1)
        for leg in plan.on_enter
    }
    fills = dry_run.clerk.option_fills_of_decision(SID, "catalogue-entry")
    assert {each.contract: each.quantity for each in fills} == {
        picks[leg.leg_id][0].occ_symbol: leg.qty_ratio for leg in plan.on_enter
    }
    assert {each.contract: each.price for each in fills} == {
        picks[leg.leg_id][0].occ_symbol: float(picks[leg.leg_id][2 if leg.position == "long" else 1])
        for leg in plan.on_enter
    }
    assert all(each.quote_age_ms == 300 and each.fill_model == "natural_price" for each in fills)
    if len(plan.on_enter) > 1:
        assert dry_run.repo.fills_for_order(parent) == []


@pytest.mark.parametrize("name", list(NamedStructure))
async def test_every_catalogue_structure_is_refused_whole_when_one_leg_has_no_quote(dry_run: _DryRun, name: NamedStructure) -> None:
    plan, picks = starting_plan(name, "SPY"), _catalogue_picks(name)
    missing = plan.on_enter[-1].leg_id
    handed = evidence(dry_run.clock(), picks)
    receipt = await dry_run.enter(
        evidence(dry_run.clock(), picks, quotes={key: value for key, value in handed.quotes.items() if key != missing}),
        decision="catalogue-refused", plan=plan,
    )
    assert receipt.state is EffectOperationState.REJECTED and missing in receipt.explanation
    assert dry_run.held() == {} and dry_run.fills() == []


async def test_a_structure_with_a_sold_leg_handed_over_on_a_day_with_no_session_is_refused_as_the_market_closed(
    dry_run: _DryRun,
) -> None:
    """The Saturday after the fixture's Tuesday. A day with no session has no close, so no entry cutoff to be
    past (ADR 0076 decision 20), and the Clerk's session check refuses the entry."""
    dry_run.wait(et_minute_of_day_ms(date(2026, 9, 12), 12 * 60) - dry_run.clock())

    receipt = await dry_run.enter(evidence(dry_run.clock(), PAID_PICKS), decision="no-session-entry", plan=PAID_SPREAD)

    assert receipt.state is EffectOperationState.REJECTED
    assert receipt.explanation.startswith("MARKET_CLOSED: "), receipt.explanation
    assert dry_run.orders() == 0 and dry_run.held() == {}
