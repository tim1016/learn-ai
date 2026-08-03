"""build_actions_from_registry: the Stop token depends ONLY on ``running``.

Defect #10 regression pin. The 2026-07-30 8-bot run hit a 409-storm on Stop.
The documented root cause — "the whole-panel display revision advances faster
than the operator's refetch" — is architecturally impossible, because the Stop
action's optimistic-concurrency token keys ONLY on ``ctx.running``
(``action_policy.py``: ``revision_inputs=lambda ctx: (ctx.running,)``). These
tests lock that invariant at the registry level so no future change can let
Clerk-journal churn or any other panel field manufacture a Stop-409.
"""

from __future__ import annotations

from app.broker.v2panel.action_policy import (
    ActionGuardContext,
    build_actions_from_registry,
)


def _ctx(*, running: bool, variant: int) -> ActionGuardContext:
    """A context whose EVERY field except ``running`` varies with ``variant``."""
    return ActionGuardContext(
        running=running,
        phase=f"phase-{variant}",
        desired_state=f"state-{variant}",
        hold_active=bool(variant % 2),
        freeze_active=bool(variant % 2),
        reconciliation_verdict=f"verdict-{variant}",
        outstanding_intents=variant,
        channel_fresh=bool(variant % 2),
        has_exposure=bool(variant % 2),
        resume_admission=None,
        flatten_supported=bool(variant % 2),
        account_id=f"acct-{variant}",
        strategy_instance_id=f"sid-{variant}",
        exposure={f"SYM{variant}": float(variant)},
        working_order_count=variant,
        account_working_order_count=variant,
        inventory_recovery_needed=bool(variant % 2),
    )


def _stop_token(ctx: ActionGuardContext, *, revision: int) -> str:
    actions = build_actions_from_registry(ctx, revision=revision, broker="alpaca")
    [stop] = [action for action in actions if action.action_id == "stop"]
    return stop.concurrency_token


def test_stop_token_depends_only_on_running() -> None:
    # Two contexts that agree on `running` but disagree on EVERY other field,
    # built at wildly different panel revisions, must yield the SAME Stop token.
    token_a = _stop_token(_ctx(running=True, variant=1), revision=1)
    token_b = _stop_token(_ctx(running=True, variant=999), revision=987_654)

    assert token_a == token_b


def test_stop_token_changes_when_running_changes() -> None:
    # Control: the token IS sensitive to `running`, so the invariant above is
    # not vacuously true (a constant token would also pass the first test).
    running_token = _stop_token(_ctx(running=True, variant=1), revision=1)
    stopped_token = _stop_token(_ctx(running=False, variant=1), revision=1)

    assert running_token != stopped_token
