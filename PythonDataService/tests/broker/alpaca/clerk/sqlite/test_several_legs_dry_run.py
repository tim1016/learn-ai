"""An option structure enters through the real Dry Run Clerk and its retained simulation (#2913)."""

from collections.abc import Mapping
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.schemas.action_plan import ActionPlan
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import (
    PAID_PICKS,
    PAID_SPREAD,
    RECEIVED_PICKS,
    RECEIVED_SPREAD,
    STRADDLE,
    STRADDLE_PICKS,
    Pick,
    evidence,
    several_legs_taken,  # noqa: F401 -- pytest fixture
)
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import SID, _DryRun
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import dry_run as _dry_run_fixture

dry_run = _dry_run_fixture
pytestmark = pytest.mark.usefixtures("several_legs_taken")


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
