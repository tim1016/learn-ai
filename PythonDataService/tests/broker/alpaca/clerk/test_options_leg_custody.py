"""Real synthetic Clerk custody for independent options-session allocations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.option_effects import OptionEntryEvidence, OptionExitEvidence
from app.broker.alpaca.clerk.sqlite.option_entry import option_entry_of_order
from app.options.contract import OptionContract
from app.options_backtest.setup import OptionsBacktestSetup
from app.schemas.action_plan import OptionsSessionActionPlan
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.schemas.options_bot import OptionsBotConfiguration
from app.schemas.signal_program_seal import semantic_payload_hash
from app.services import market_liveness
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (
    RUN,
    SID,
    _binding,
    _DryRun,
    _open,
    _quote,
)

pytestmark = pytest.mark.usefixtures("budgeted_run_registration")

CALL = OptionContract.of(root="SPY", expiry_date=date(2026, 9, 18), right="call", strike=585)
OTHER = OptionContract.of(root="SPY", expiry_date=date(2026, 9, 18), right="call", strike=590)


def _setup(*, sold: bool = False, contracts: int = 1, count: int = 2) -> OptionsBacktestSetup:
    return OptionsBacktestSetup.model_validate({
        "version": 1, "underlying": "SPY", "entry": {"anchor": "open", "offset_ms": 60_000},
        "exit": {"anchor": "close", "offset_ms": 600_000}, "square_off": "partial",
        "legs": [
            {"leg_id": name, "side": "sell" if sold and name == "second" else "buy", "right": "call",
             "contracts": contracts, "expiry": {"kind": "this_week"}, "strike": {"kind": "atm"}}
            for name in (("first", "second") if count == 2 else tuple("leg_" + str(i) for i in range(count)))
        ], "fees": {"kind": "estimated"}, "starting_cash_usd": "10000",
    })


@pytest.fixture
async def session_clerk(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[_DryRun]:
    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=585.0)
    clock = _TestClock(NOON)
    setup = _setup(**getattr(request, "param", {}))
    binding = _session_binding(setup)
    try:
        account = await _open(tmp_path, clock, binding)
        await account.clerk.register_strategy_run(account.binding)
        clock.value += 1000
        yield account
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()


def _session_binding(setup: OptionsBacktestSetup) -> BrokerBotBinding:
    digest = semantic_payload_hash(setup.model_dump(mode="json"))
    return _binding(budget_cents=1_000_000).model_copy(update={
        "strategy_key": "options_rules",
        "action_plan": OptionsSessionActionPlan(kind="options_session", underlying="SPY", setup_sha256=digest),
        "options_bot": OptionsBotConfiguration(
            golden_setup_id="approved", setup_sha256=digest, rules_digest="a" * 64, setup=setup,
        ),
        "option_order_terms": SealedOptionOrderTerms(
            gap_limit_percent="8", entry_wait_seconds=5, cost_limit_percent="10", expiry_close_lead_minutes=15,
        ),
    })


def _entry(account: _DryRun, leg_id: str, contract: OptionContract) -> OptionEntryEvidence:
    return replace(account.evidence(), picked={leg_id: contract}, multipliers={leg_id: 100},
                   quotes={leg_id: _quote("1.90", "1.94", received_ms=account.clock() - 100)})


async def _enter(
    account: _DryRun, leg_id: str, contract: OptionContract, *, decision: str | None = None, run_id: str = RUN,
) -> EffectOperationReceipt:
    return await account.clerk.execute_options_leg(
        strategy_instance_id=SID, run_id=run_id, decision_id=decision or "open-" + leg_id,
        leg_id=leg_id, attempt_id="session-1", purpose=EffectPurpose.ENTER,
        option_entry=_entry(account, leg_id, contract),
    )


async def _close(
    account: _DryRun, leg_id: str, contract: OptionContract, *, decision: str | None = None, run_id: str = RUN,
) -> EffectOperationReceipt:
    return await account.clerk.execute_options_leg(
        strategy_instance_id=SID, run_id=run_id, decision_id=decision or "close-" + leg_id,
        leg_id=leg_id, attempt_id="session-1", purpose=EffectPurpose.EXIT,
        option_exit=OptionExitEvidence(
            quotes={contract.occ_symbol: _quote("2.00", "2.04", received_ms=account.clock() - 100)},
            generation=4, read_at_ms=account.clock(),
        ),
    )


async def test_independent_leg_exit_keeps_the_other_contract(session_clerk: _DryRun) -> None:
    account = session_clerk
    first = await _enter(account, "first", CALL)
    assert first.state is EffectOperationState.SUBMITTED, first
    await account.read_account()
    second = await _enter(account, "second", OTHER)
    assert second.state is EffectOperationState.SUBMITTED, second

    closed = await _close(account, "first", CALL)

    assert closed.state is EffectOperationState.FLAT, closed
    assert account.held() == {OTHER.occ_symbol: 1.0}
    snapshot = account.clerk.options_leg_snapshot(SID, "first", "session-1")
    assert snapshot.held_quantity == 0
    assert len(snapshot.entry_fills) == len(snapshot.close_fills) == 1


async def test_two_legs_on_same_contract_have_separate_custody(session_clerk: _DryRun) -> None:
    account = session_clerk
    assert (await _enter(account, "first", CALL)).state is EffectOperationState.SUBMITTED
    await account.read_account()
    assert (await _enter(account, "second", CALL)).state is EffectOperationState.SUBMITTED

    assert (await _close(account, "second", CALL)).state is EffectOperationState.FLAT

    assert account.held() == {CALL.occ_symbol: 1.0}
    assert account.clerk.options_leg_snapshot(SID, "first", "session-1").held_quantity == 1
    assert account.clerk.options_leg_snapshot(SID, "second", "session-1").held_quantity == 0


async def test_duplicate_request_and_attempt_never_send_a_second_open(session_clerk: _DryRun) -> None:
    account = session_clerk
    first = await _enter(account, "first", CALL)
    again = await _enter(account, "first", CALL)
    other_decision = await _enter(account, "first", CALL, decision="different-decision")

    assert first.child_order_refs == again.child_order_refs
    assert other_decision.state is EffectOperationState.REJECTED
    assert account.held() == {CALL.occ_symbol: 1.0}
    entry = option_entry_of_order(account.repo, first.child_order_refs[0])
    assert entry is not None and entry.options_attempt.leg_id == "first"


async def test_unsealed_leg_cannot_request_an_order(session_clerk: _DryRun) -> None:
    with pytest.raises(ValueError, match="no leg"):
        await _enter(session_clerk, "invented", CALL)
    assert session_clerk.orders() == 0


@pytest.mark.parametrize("purpose", [EffectPurpose.ENTER, EffectPurpose.EXIT])
async def test_sealed_options_bot_cannot_bypass_leg_authorization_with_a_stock_plan(
    session_clerk: _DryRun, purpose: EffectPurpose,
) -> None:
    with pytest.raises(ValueError, match="sealed leg"):
        await session_clerk.clerk.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN, decision_id="caller-invented-stock-plan",
            purpose=purpose, action_plan=alpaca_v1_action_plan("QQQ"), quantity=100,
        )
    assert session_clerk.orders() == 0


@pytest.mark.parametrize("session_clerk", [{"sold": True}], indirect=True)
async def test_sold_leg_uses_confirmed_cover_and_protection_cannot_leave(session_clerk: _DryRun) -> None:
    account = session_clerk
    naked = await _enter(account, "second", OTHER)
    assert naked.state is EffectOperationState.REJECTED
    assert account.orders() == 0
    assert (await _enter(account, "first", CALL)).state is EffectOperationState.SUBMITTED
    await account.read_account()
    evidence = replace(_entry(account, "second", OTHER), quotes={"second": _quote("1.20", "1.24", received_ms=account.clock() - 100)})
    sale = await account.clerk.execute_options_leg(
        strategy_instance_id=SID, run_id=RUN, decision_id="covered-sale", leg_id="second", attempt_id="session-1",
        purpose=EffectPurpose.ENTER, option_entry=evidence,
    )
    assert sale.state is EffectOperationState.SUBMITTED, sale
    assert account.clerk.options_leg_snapshot(SID, "second", "session-1").held_quantity == -1

    refused = await _close(account, "first", CALL)

    assert refused.state is EffectOperationState.REJECTED
    assert "PROTECTION" in refused.explanation
    assert (await _close(account, "second", OTHER)).state is EffectOperationState.FLAT
    assert (await _close(account, "first", CALL, decision="protection-can-leave")).state is EffectOperationState.FLAT
    assert account.held() == {}


@pytest.mark.parametrize("session_clerk", [{"count": 8}], indirect=True)
async def test_eight_leg_portfolio_keeps_each_broker_order_within_its_shape(session_clerk: _DryRun) -> None:
    account = session_clerk
    for number in range(8):
        answer = await _enter(account, "leg_" + str(number), CALL)
        assert answer.state is EffectOperationState.SUBMITTED, answer
        await account.read_account()

    assert account.held() == {CALL.occ_symbol: 8.0}
    assert account.orders() == 8
    for number in range(8):
        assert (await _close(account, "leg_" + str(number), CALL)).state is EffectOperationState.FLAT
    assert account.held() == {}
