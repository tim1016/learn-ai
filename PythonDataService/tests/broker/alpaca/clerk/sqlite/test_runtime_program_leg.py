"""The Clerk shapes every program leg from the run's session and its decision bar."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    active_program_leg_policy,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import OrderType, TimeInForce
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.source_bar_ledger import RetainedSourceBar
from app.utils.timestamps import to_ms_utc
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, _FakeTradePort
from tests.broker.alpaca.clerk.sqlite.test_exit import _make_entry

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
RUN_ID = "run-1"

_ET = ZoneInfo("America/New_York")
_DAY = date(2026, 9, 2)
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_ALLOWANCES = ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20"))
_EXTENDED_POLICY = ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES)


def _bar(hour: int, minute: int, *, phase: str, close: str = "100.00") -> RetainedSourceBar:
    end = to_ms_utc(datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=_ET))
    return RetainedSourceBar(
        seq=1,
        account_id=ACCOUNT_ID,
        provider="ibkr",
        symbol="SPY",
        bar_identity=f"ibkr:SPY:{end - 60_000}:{end}",
        bar_ref="bar-1",
        start_ms=end - 60_000,
        end_ms=end,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
        fetched_at_ms=end,
        session_phase=phase,
    )


def _binding(*, use_rth: bool) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=use_rth,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        sealed_account_id=ACCOUNT_ID,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id=RUN_ID,
        created_at_ms=1,
    )


async def _enter(
    tmp_path: Path,
    *,
    use_rth: bool,
    policy: ProgramLegPolicy,
    retained_source_bar: RetainedSourceBar | None,
) -> tuple[_FakeTradePort, EffectOperationState, str]:
    """Drive one ENTER through the facade and report the port and the receipt."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    trade = _FakeTradePort()
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=trade,
        account_mode="paper",
        program_leg_policy=policy,
    )
    binding = _binding(use_rth=use_rth)
    await facade.register_strategy_run(binding)
    try:
        receipt = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-1",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=use_rth,
            retained_source_bar=retained_source_bar,
        )
    finally:
        repo.close()
    return trade, receipt.state, receipt.explanation


async def _exit(
    tmp_path: Path,
    *,
    use_rth: bool,
    policy: ProgramLegPolicy,
    retained_source_bar: RetainedSourceBar | None,
) -> tuple[_FakeTradePort, EffectOperationState, str]:
    """Drive one EXIT through the facade, after a filled ENTER, and report the port and the receipt."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    trade = _FakeTradePort()
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=trade,
        account_mode="paper",
        program_leg_policy=policy,
    )
    binding = _binding(use_rth=use_rth)
    await facade.register_strategy_run(binding)
    # The filled ENTER is seeded directly in the repo (as test_exit_reducing_shape.py
    # does), not through this facade's own trade port: only the EXIT's reducing
    # submission below should land in `trade.submitted_legs`.
    await _make_entry(repo, quantity=10, filled_quantity=10, status="filled")
    try:
        receipt = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-exit-1",
            purpose=EffectPurpose.EXIT,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=use_rth,
            retained_source_bar=retained_source_bar,
        )
    finally:
        repo.close()
    return trade, receipt.state, receipt.explanation


async def test_an_extended_exit_decision_submits_a_marketable_day_limit_at_the_exit_allowance(
    tmp_path: Path,
) -> None:
    """A filled ENTER plus an extended EXIT decision shapes the reducing leg (review finding 3a)."""
    trade, state, _explanation = await _exit(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
    )

    assert state is not EffectOperationState.REJECTED
    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours, leg.side.value) == (
        OrderType.LIMIT,
        TimeInForce.DAY,
        99.80,
        True,
        "sell",
    )


async def test_an_exit_decision_at_session_close_is_rejected_before_broker_contact(
    tmp_path: Path,
) -> None:
    """No session accepts a program leg at 20:00 ET; the EXIT refuses, never submits (review finding 3b)."""
    trade, state, explanation = await _exit(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(20, 0, phase="CLOSED"),
    )

    assert state is EffectOperationState.REJECTED
    assert explanation.startswith("SESSION_CLOSED_AT_DECISION:")
    assert trade.submitted_legs == []


async def test_an_extended_decision_outside_the_regular_session_submits_a_marketable_day_limit(
    tmp_path: Path,
) -> None:
    trade, state, _explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
    )

    assert state is not EffectOperationState.REJECTED
    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.LIMIT,
        TimeInForce.DAY,
        100.10,
        True,
    )


async def test_an_extended_decision_inside_the_regular_session_submits_a_market_day_leg(
    tmp_path: Path,
) -> None:
    trade, _state, _explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(10, 0, phase="RTH"),
    )

    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.MARKET,
        TimeInForce.DAY,
        None,
        False,
    )


async def test_an_extended_decision_without_a_retained_bar_is_rejected_before_broker_contact(
    tmp_path: Path,
) -> None:
    trade, state, explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=None,
    )

    assert state is EffectOperationState.REJECTED
    assert explanation.startswith("EXTENDED_ANCHOR_UNAVAILABLE:")
    assert trade.submitted_legs == []


async def test_a_regular_hours_decision_keeps_the_market_day_leg(tmp_path: Path) -> None:
    """The RTH filter never yields an 18:30 bar in production; the runtime does not re-check."""
    trade, _state, _explanation = await _enter(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
    )

    (leg,) = trade.submitted_legs
    assert leg.order_type is OrderType.MARKET
    assert leg.extended_hours is False


async def test_a_regular_only_authority_refuses_an_extended_decision(tmp_path: Path) -> None:
    trade, state, explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=ProgramLegPolicy.regular_only(),
        retained_source_bar=_bar(18, 30, phase="POST"),
    )

    assert state is EffectOperationState.REJECTED
    assert explanation.startswith("EXTENDED_HOURS_UNSUPPORTED:")
    assert trade.submitted_legs == []


@pytest.mark.parametrize(
    ("policy", "expected_window"),
    [(_EXTENDED_POLICY, _WINDOW), (ProgramLegPolicy.regular_only(), None)],
)
def test_the_facade_publishes_its_leg_policy_and_window(
    tmp_path: Path,
    policy: ProgramLegPolicy,
    expected_window: ExtendedHoursWindow | None,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=_FakeTradePort(),
        account_mode="paper",
        program_leg_policy=policy,
    )
    try:
        assert facade.program_leg_policy is policy
        assert facade.extended_hours_window == expected_window
    finally:
        repo.close()


def test_a_facade_built_without_a_policy_is_regular_only(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(), account_mode="paper"
    )
    try:
        assert facade.program_leg_policy == ProgramLegPolicy.regular_only()
        assert facade.extended_hours_window is None
    finally:
        repo.close()


def test_the_process_accessor_reads_the_active_authoritys_policy(tmp_path: Path) -> None:
    assert active_program_leg_policy() == ProgramLegPolicy.regular_only()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=_FakeTradePort(),
        account_mode="paper",
        program_leg_policy=_EXTENDED_POLICY,
    )
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    try:
        assert active_program_leg_policy() is _EXTENDED_POLICY
    finally:
        set_active_clerk_runtime(None)
        repo.close()
