"""Narrow retire eligibility (#1778 WP7, finding S5).

A legacy bot registered against symbol "APPL" (a typo for AAPL) can never
run: no such security exists. It cannot be retired either, so it re-issues
doomed IBKR subscriptions forever -- 1 550 "no security definition" errors
in 45 hours of the fleet run, one every 105 seconds.

Retire here means exactly one thing: clean up a registration that can never
run again. It is deliberately NOT "end this bot's life" -- a healthy stopped
bot is not retire-eligible, because that is a destructive lifecycle action
with its own safety story. Above all, retire must never strand exposure.
"""

from __future__ import annotations

from app.broker.v2panel.action_policy import ACTION_REGISTRY, ActionGuardContext
from app.services.broker_v2_panel.presented_actions import strategy_runtime_missing


def _ctx(
    *,
    running: bool = False,
    phase: str = "OFF_DUTY",
    strategy_runtime_missing: bool = True,
    has_exposure: bool = False,
    working_order_count: int = 0,
) -> ActionGuardContext:
    return ActionGuardContext(
        running=running,
        phase=phase,
        desired_state="STOPPED",
        hold_active=False,
        freeze_active=False,
        reconciliation_verdict="clean",
        outstanding_intents=0,
        has_exposure=has_exposure,
        resume_admission=None,
        flatten_supported=True,
        account_id="PA3KWXU1C4C3",
        strategy_instance_id="Aug11",
        exposure={"APPL": 1.0} if has_exposure else {},
        working_order_count=working_order_count,
        strategy_runtime_missing=strategy_runtime_missing,
    )


def _retire(ctx: ActionGuardContext) -> tuple[bool, list]:
    return ACTION_REGISTRY["retire"].guard(ctx)


def test_a_registration_that_can_never_run_again_is_retire_eligible() -> None:
    enabled, blockers = _retire(_ctx())

    assert enabled is True
    assert blockers == []


def test_retire_refuses_while_the_bot_still_holds_exposure() -> None:
    """The one thing retire must never do is strand exposure."""
    enabled, blockers = _retire(_ctx(has_exposure=True))

    assert enabled is False
    assert blockers


def test_retire_refuses_while_an_order_is_still_working() -> None:
    enabled, _ = _retire(_ctx(working_order_count=1))

    assert enabled is False


def test_a_healthy_stopped_bot_is_not_retire_eligible() -> None:
    """Narrow, not broad: a runnable strategy is not cleanup."""
    enabled, blockers = _retire(_ctx(strategy_runtime_missing=False))

    assert enabled is False
    assert blockers


def test_retire_refuses_while_the_bot_is_running() -> None:
    enabled, _ = _retire(_ctx(running=True, phase="ON_DUTY"))

    assert enabled is False


def test_an_already_retired_bot_offers_no_second_retire() -> None:
    enabled, _ = _retire(_ctx(phase="RETIRED"))

    assert enabled is False


def test_runtime_missing_is_resolved_against_the_real_strategy_registry() -> None:
    """The predicate the guard depends on, checked against live vocabulary."""
    assert strategy_runtime_missing("deployment_validation") is False
    assert strategy_runtime_missing("strategy_that_no_longer_exists") is True
