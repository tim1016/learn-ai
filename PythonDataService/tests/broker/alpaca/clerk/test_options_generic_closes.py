"""Automatic and operator closes preserve each Options allocation's custody."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.sqlite.dry_run_close import close_exposure_of_ended_dry_runs
from app.broker.alpaca.clerk.sqlite.ended_run_close import CloseDeferred, bot_holdings, closes_owed_under, drive_close
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.alpaca.clerk.sqlite.projection_models import SafeFlattenPlan, SafeFlattenPlanLeg
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import execute_safe_flatten_plan
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.contract.errors import BrokerUnavailable
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import PaperOptions
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import _DryRun, _quote
from tests.broker.alpaca.clerk.test_options_leg_custody import (
    CALL,
    OTHER,
    RUN,
    SID,
    _enter,
    _entry,
    session_clerk,  # noqa: F401 -- pytest fixture
)
from tests.broker.alpaca.clerk.test_options_leg_paper import options_paper  # noqa: F401 -- pytest fixture

pytestmark = pytest.mark.usefixtures("budgeted_run_registration")


def _plan(account: _DryRun) -> SafeFlattenPlan:
    return SafeFlattenPlan(
        version_token="reviewed-two-allocations", account_id=account.repo.account_id,
        authority_generation=1, db_identity_token="test", control_revision=0,
        scope="CUSTODY_SUBJECT", strategy_instance_id=SID, reconciliation_id="test-read",
        account_check_kind="reconcile_now", account_check_observed_at_ms=account.clock(),
        prepared_at_ms=account.clock(), expires_at_ms=account.clock() + 30_000,
        legs=(SafeFlattenPlanLeg(SID, CALL.occ_symbol, "sell", account.repo.position(SID, CALL.occ_symbol), account.clock()),),
    )


async def _open_both(account: _DryRun) -> None:
    assert (await _enter(account, "first", CALL)).state is EffectOperationState.SUBMITTED
    await account.read_account()
    assert (await _enter(account, "second", CALL)).state is EffectOperationState.SUBMITTED
    assert account.held() == {CALL.occ_symbol: 2.0}
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="operator_stop")


async def test_ended_run_closes_both_allocations_on_one_contract(session_clerk: _DryRun) -> None:  # noqa: F811
    account = session_clerk
    await _open_both(account)
    assert len(bot_holdings(account.repo, SID, decision_prefix="test-ended-")) == 2

    for _ in range(2):
        await close_exposure_of_ended_dry_runs(
            account.repo, trade=account.clerk._trade, intake=account.clerk.intake,
            pricing=account.clerk.recovery_pricing,
        )

    assert account.held() == {}
    assert account.orders() == 4
    assert bot_holdings(account.repo, SID, decision_prefix="test-ended-") == []


@pytest.mark.parametrize("options_paper", [{"budget_cents": 100_000}], indirect=True)
async def test_safe_flatten_lost_answers_keep_one_order_per_actual_allocation(
    options_paper: PaperOptions, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    paper = options_paper
    for leg in ("first", "second"):
        opened = await _enter(paper, leg, CALL)
        assert opened.state is EffectOperationState.SUBMITTED, opened
        await paper.stream_fill(opened.child_order_refs[0], contracts=2, filled_in_all=2, price="1.94", execution_id="fill-" + leg)
        assert await paper.sync.tick() == "observed"
    await paper.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="operator_stop")
    plan = _plan(paper)
    paper.venue.option_answer_lost = True
    openings = set(paper.venue.option_submit_calls)
    original_lookup = paper.venue.get_option_order_by_client_order_id
    lost = True

    async def lookup(order_ref):
        if lost and order_ref not in openings:
            raise BrokerUnavailable("lost closing lookup", broker="alpaca")
        return await original_lookup(order_ref)

    monkeypatch.setattr(paper.venue, "get_option_order_by_client_order_id", lookup)

    async def flatten():
        return await execute_safe_flatten_plan(
            paper.repo, plan=plan, read=paper.account, trade=paper.venue,
            intake=paper.clerk.intake, account_id=paper.repo.account_id,
            pricing=paper.clerk.recovery_pricing,
        )

    # An unknown first close fences a second send. Retrying this same plan
    # retains both captured decisions without duplicating the lost order.
    with pytest.raises(AdmissionBlockedError, match="ORDER_OUTCOME_UNKNOWN"):
        await flatten()
    with pytest.raises(AdmissionBlockedError, match="ORDER_OUTCOME_UNKNOWN"):
        await flatten()
    closes = [order for order in paper.venue.submitted_option_orders if order.intent.value == "close"]
    assert len(closes) == 1
    assert closes[0].quantity == 2
    assert len(paper.venue.option_submit_calls) == 3
    lost = False
    paper.venue.option_answer_lost = False
    await paper.stream_fill(paper.venue.option_submit_calls[2], contracts=2, filled_in_all=2, price="2.00", execution_id="close-first")
    await paper.pass_()
    await flatten()
    assert len(paper.venue.option_submit_calls) == 4
    await paper.stream_fill(paper.venue.option_submit_calls[3], contracts=2, filled_in_all=2, price="2.00", execution_id="close-second")
    await flatten()
    assert paper.repo.position(SID, CALL.occ_symbol) == 0
    assert len(paper.venue.option_submit_calls) == 4


async def test_safe_flatten_closes_each_allocation_and_same_plan_retry_is_idempotent(session_clerk: _DryRun) -> None:  # noqa: F811
    account = session_clerk
    await _open_both(account)
    plan = _plan(account)

    async def flatten():
        return await execute_safe_flatten_plan(
            account.repo, plan=plan, read=account.clerk._read, trade=account.clerk._trade,
            intake=account.clerk.intake, account_id=account.repo.account_id,
            pricing=account.clerk.recovery_pricing,
        )

    first = await flatten()
    second = await flatten()

    assert len(first.accepted_effect_operation_ids) == 2
    assert first.accepted_effect_operation_ids == second.accepted_effect_operation_ids
    assert account.held() == {}
    assert account.orders() == 4
    assert account.clerk.options_leg_snapshot(SID, "first", "session-1").held_quantity == 0
    assert account.clerk.options_leg_snapshot(SID, "second", "session-1").held_quantity == 0


@pytest.mark.parametrize("session_clerk", [{"sold": True}], indirect=True)
async def test_automatic_close_buys_back_sold_allocation_before_releasing_cover(session_clerk: _DryRun) -> None:  # noqa: F811
    account = session_clerk
    assert (await _enter(account, "first", CALL)).state is EffectOperationState.SUBMITTED
    await account.read_account()
    evidence = replace(_entry(account, "second", OTHER), quotes={"second": _quote("1.20", "1.24", received_ms=account.clock() - 100)})
    sale = await account.clerk.execute_options_leg(
        strategy_instance_id=SID, run_id=RUN, decision_id="covered-sale", leg_id="second", attempt_id="session-1",
        purpose=EffectPurpose.ENTER, option_entry=evidence,
    )
    assert sale.state is EffectOperationState.SUBMITTED, sale
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="operator_stop")
    closes = closes_owed_under(account.repo, SID, decision_prefix="test-auto-")
    assert [close.symbol for close in closes] == [OTHER.occ_symbol, CALL.occ_symbol]

    async def drive(close):
        return await drive_close(
            account.repo, close, trade=account.clerk._trade, intake=account.clerk.intake,
            pricing=account.clerk.recovery_pricing, run=run_inline,
        )

    assert await drive(closes[1]) is CloseDeferred.BUSY
    await drive(closes[0])
    await drive(closes[1])
    assert account.held() == {}
    assert account.orders() == 4
