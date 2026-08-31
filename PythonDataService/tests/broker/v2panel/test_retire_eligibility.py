"""Retire eligibility: two proofs of permanent inadmissibility (#1778 S5, #1795).

A legacy bot registered against symbol "APPL" (a typo for AAPL) can never
run: no such security exists. It cannot be retired either, so it re-issues
doomed feed subscriptions forever -- 1 550 "no security definition" errors
in 45 hours of the fleet run, one every 105 seconds.

Retire here means exactly one thing: clean up a registration that can never
run again. It is deliberately NOT "end this bot's life" -- a healthy stopped
bot is not retire-eligible, because that is a destructive lifecycle action
with its own safety story. Above all, retire must never strand exposure.

Two independent proofs make a bot retire-eligible, and either suffices:

- ``strategy_runtime_missing`` -- the strategy key is gone from the runtime
  registry (#1778's original predicate).
- ``symbol_unresolvable`` -- the durable symbol-validity store holds a
  definitive broker answer that the bound symbol is not a listed asset
  (#1795's widening; produced by the reconciliation sweep's post-pass probe,
  see ``app.broker.alpaca.symbol_validity``). This is what finally covers the
  motivating "APPL" bot, whose strategy key (``deployment_validation``) is
  alive while its *symbol* is what is dead.

When neither proof holds, the refusal must claim no more than it knows: the
bot is not *provably* dead -- which covers both a genuinely healthy bot and a
dead-symbol bot the sweep has not yet observed.
"""

from __future__ import annotations

from app.broker.v2panel.action_policy import (
    ACTION_REGISTRY,
    ActionGuardContext,
    build_actions_from_registry,
)
from app.services.broker_v2_panel.presented_actions import strategy_runtime_missing


def _ctx(
    *,
    running: bool = False,
    phase: str = "OFF_DUTY",
    strategy_runtime_missing: bool = True,
    symbol_unresolvable: bool = False,
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
        symbol_unresolvable=symbol_unresolvable,
    )


def _retire(ctx: ActionGuardContext) -> tuple[bool, list]:
    return ACTION_REGISTRY["retire"].guard(ctx)


def test_a_registration_that_can_never_run_again_is_retire_eligible() -> None:
    enabled, blockers = _retire(_ctx())

    assert enabled is True
    assert blockers == []


def test_an_unresolvable_symbol_alone_makes_a_flat_bot_retire_eligible() -> None:
    """#1795: the motivating "APPL" bot -- live strategy key, dead symbol."""
    enabled, blockers = _retire(
        _ctx(strategy_runtime_missing=False, symbol_unresolvable=True)
    )

    assert enabled is True
    assert blockers == []


def test_an_unresolvable_symbol_bot_with_custody_is_still_refused() -> None:
    """Guard ordering preserved: custody stays the last word (#1795)."""
    enabled, blockers = _retire(
        _ctx(strategy_runtime_missing=False, symbol_unresolvable=True, has_exposure=True)
    )

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["RETIRE_WOULD_STRAND_CUSTODY"]

    enabled, blockers = _retire(
        _ctx(strategy_runtime_missing=False, symbol_unresolvable=True, working_order_count=1)
    )

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["RETIRE_WOULD_STRAND_CUSTODY"]


def test_retire_refuses_while_the_bot_still_holds_exposure() -> None:
    """The one thing retire must never do is strand exposure."""
    enabled, blockers = _retire(_ctx(has_exposure=True))

    assert enabled is False
    assert blockers


def test_retire_refuses_while_an_order_is_still_working() -> None:
    enabled, _ = _retire(_ctx(working_order_count=1))

    assert enabled is False


def test_a_healthy_stopped_bot_is_not_retire_eligible() -> None:
    """Narrow, not broad: a live program on a resolvable symbol is not cleanup."""
    enabled, blockers = _retire(
        _ctx(strategy_runtime_missing=False, symbol_unresolvable=False)
    )

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["STRATEGY_STILL_RUNNABLE"]


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


def test_retire_and_resume_never_contradict_on_a_permanently_dead_bot() -> None:
    """T1 (#1795): the reported defect was the panel contradicting itself.

    `Aug11` showed "Resume is blocked." permanently and, on the same panel,
    "This bot can still run." With the widening, a bot the broker has durably
    answered is unlisted gets an *enabled* Retire -- refusal and cure agree the
    bot is dead. And where retire still refuses (no proof yet), the blocker
    claims no runnability, so the pair can never assert both again.
    """
    enabled, blockers = _retire(
        _ctx(strategy_runtime_missing=False, symbol_unresolvable=True)
    )
    assert enabled is True, "the permanently dead bot must be offered its cure"
    assert blockers == []

    _enabled, blockers = _retire(_ctx(strategy_runtime_missing=False))
    assert blockers, "an unproven bot must still be refused retire"
    rendered = " ".join(f"{b.headline} {b.detail}" for b in blockers).lower()
    assert "can still run" not in rendered
    assert "no proof" in rendered


def _retire_confirmation(ctx: ActionGuardContext):
    actions = build_actions_from_registry(ctx, revision=1, broker="alpaca")
    retire = next(action for action in actions if action.action_id == "retire")
    assert retire.enabled, "confirmation copy only exists for an enabled action"
    return retire.confirmation


def test_retire_confirmation_names_the_proof_that_enabled_it() -> None:
    """An irreversible command must not misdescribe its own reason (#1904 review).

    Stating "its strategy is no longer registered" for a symbol-proved bot is
    simply false -- that bot's strategy is alive -- and would send the operator
    hunting a runtime problem that does not exist.
    """
    by_symbol = _retire_confirmation(
        _ctx(strategy_runtime_missing=False, symbol_unresolvable=True)
    )
    assert by_symbol is not None
    assert "not a listed asset" in by_symbol.body
    assert "no longer registered" not in by_symbol.body

    by_strategy = _retire_confirmation(
        _ctx(strategy_runtime_missing=True, symbol_unresolvable=False)
    )
    assert by_strategy is not None
    assert "no longer registered" in by_strategy.body

    # Both proofs: the missing program is the broader fact, so it leads.
    both = _retire_confirmation(
        _ctx(strategy_runtime_missing=True, symbol_unresolvable=True)
    )
    assert both is not None
    assert "no longer registered" in both.body
