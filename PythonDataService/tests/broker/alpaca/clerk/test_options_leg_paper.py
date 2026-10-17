"""Paper Clerk execution with a local venue: partial fills and lost answers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.contract.errors import BrokerUnavailable
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services import market_liveness
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _FakeTradePort
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import PaperAccountRead, PaperOptions, _compose
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _new_budget_repo
from tests.broker.alpaca.clerk.test_options_leg_custody import (
    CALL,
    RUN,
    SID,
    _close,
    _enter,
    _session_binding,
    _setup,
)

pytestmark = pytest.mark.usefixtures("budgeted_run_registration")


@pytest.fixture
async def options_paper(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[PaperOptions]:
    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=585.0)
    repo = _new_budget_repo(tmp_path)
    venue = _FakeTradePort()
    venue.venue_clock = repo.clock
    account = PaperAccountRead(venue)
    sync, keeper, clerk = _compose(repo, repo.clock, venue, account)
    assert await sync.tick() == "observed"
    binding = _session_binding(_setup(contracts=2)).model_copy(update={
        "mode": "trade", "sealed_account_id": repo.account_id,
        "budget_consent": DeployBudgetConsent(
            committed_cents=getattr(request, "param", {}).get("budget_cents", 50_000),
            risk_revision=1, actor="owner", request_fingerprint="reviewed",
            world="real_paper",
        ),
    })
    await clerk.register_strategy_run(binding)
    repo.clock.advance(1000)
    opened = PaperOptions(repo, repo.clock, clerk, venue, account, keeper, sync, tmp_path)
    try:
        yield opened
    finally:
        await keeper.stop()
        await sync.stop()
        await clerk.drain_effects()
        repo.close()
        market_liveness.reset_market_liveness_store_for_testing()


async def test_partial_open_cancel_preserves_actual_quantity_and_closes_only_that(options_paper: PaperOptions) -> None:
    paper = options_paper
    opened = await _enter(paper, "first", CALL)
    assert opened.state is EffectOperationState.SUBMITTED, opened
    [order_ref] = opened.child_order_refs
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.93", execution_id="first-half")

    canceled = await paper.clerk.cancel_options_leg_entry(SID, RUN, "first", "session-1")

    assert canceled.standing is EntryStanding.PART_FILLED
    assert canceled.held_quantity == 1
    assert [(fill.quantity, fill.price) for fill in canceled.entry_fills] == [(1.0, 1.93)]
    closing = await _close(paper, "first", CALL)
    assert closing.state is EffectOperationState.EXIT_PENDING, closing
    reducing = closing.child_order_refs[-1]
    assert paper.venue.option_orders[reducing]["qty"] == "1"
    await paper.stream_fill(reducing, contracts=1, filled_in_all=1, price="2.00", execution_id="close-half")
    snapshot = paper.clerk.options_leg_snapshot(SID, "first", "session-1")
    assert snapshot.held_quantity == 0
    assert sum(fill.quantity for fill in snapshot.close_fills) == 1


async def test_uncertain_submit_reuses_captured_identity_and_never_duplicates(options_paper: PaperOptions) -> None:
    paper = options_paper
    paper.venue.option_answer_lost = True
    paper.venue.option_lookup_error = BrokerUnavailable("lookup temporarily unavailable", broker="alpaca")

    first = await _enter(paper, "first", CALL)
    again = await _enter(paper, "first", CALL)

    assert first.state is EffectOperationState.UNCERTAIN
    assert first.child_order_refs == again.child_order_refs
    assert len(paper.venue.option_submit_calls) == 1
    assert paper.clerk.options_leg_snapshot(SID, "first", "session-1").standing is EntryStanding.OUTCOME_UNKNOWN
    changed = CALL.model_copy(update={"strike_thousandths": CALL.strike_thousandths + 5000})
    with pytest.raises(DurableConflictError):
        await _enter(paper, "first", changed)
    assert len(paper.venue.option_submit_calls) == 1


async def test_close_captures_a_late_opening_fill_before_sizing_the_reduction(options_paper: PaperOptions) -> None:
    paper = options_paper
    opened = await _enter(paper, "first", CALL)
    [order_ref] = opened.child_order_refs
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.93", execution_id="first-fill")
    paper.venue.cancel_is_slow = True

    pending = await _close(paper, "first", CALL)

    assert pending.state in (EffectOperationState.ACCEPTED, EffectOperationState.UNCERTAIN, EffectOperationState.EXIT_PENDING)
    assert len(paper.venue.option_submit_calls) == 1
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=2, price="1.93", execution_id="late-fill")
    paper.venue.cancel_is_slow = False
    await paper.pass_()
    reductions = [order for order in paper.venue.submitted_option_orders if order.intent.value == "close"]
    assert len(reductions) == 1
    assert reductions[0].quantity == 2
