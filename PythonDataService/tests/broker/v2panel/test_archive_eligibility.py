"""Archive eligibility: the sanctioned exit for a bot you are finished with (ADR 0052).

``retire`` clears a registration that is *provably dead* (#1795) and refuses
every healthy stopped bot with ``STRATEGY_STILL_RUNNABLE`` -- correctly, for
what it covers. The consequence measured in #1911 is that a bot you are done
with can be stopped but never removed, so catalog rows only accumulate, and
#1801 measured both read and deploy cost as linear in exactly that number.

Archive is the destructive lifecycle action #1795 deferred. Its enabling
proof is custody rather than inadmissibility: the registration is stopped,
flat, and holds no working orders. That difference is what these tests pin --
above all that the proof must be *believable* before it is believed, which is
why a frozen account refuses even though it reports no exposure.
"""

from __future__ import annotations

from app.broker.v2panel.action_policy import (
    ACTION_REGISTRY,
    ActionGuardContext,
    evaluate_archive,
)


def _ctx(
    *,
    running: bool = False,
    phase: str = "OFF_DUTY",
    has_exposure: bool = False,
    working_order_count: int = 0,
    freeze_active: bool = False,
) -> ActionGuardContext:
    return ActionGuardContext(
        running=running,
        phase=phase,
        desired_state="RUNNING" if running else "STOPPED",
        hold_active=False,
        freeze_active=freeze_active,
        reconciliation_verdict="clean",
        outstanding_intents=0,
        has_exposure=has_exposure,
        resume_admission=None,
        flatten_supported=True,
        account_id="PA3KWXU1C4C3",
        strategy_instance_id="Aug11",
        exposure={"SPY": 1.0} if has_exposure else {},
        working_order_count=working_order_count,
    )


def _archive(ctx: ActionGuardContext) -> tuple[bool, list]:
    return ACTION_REGISTRY["archive"].guard(ctx)


def test_a_stopped_flat_bot_is_archive_eligible() -> None:
    """The case #1795 deliberately excluded and #1911 asked for."""
    enabled, blockers = _archive(_ctx())

    assert enabled is True
    assert blockers == []


def test_archive_refuses_a_running_bot() -> None:
    enabled, blockers = _archive(_ctx(running=True, phase="ON_DUTY"))

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["BOT_STILL_RUNNING"]


def test_archive_refuses_while_the_bot_still_holds_exposure() -> None:
    enabled, blockers = _archive(_ctx(has_exposure=True))

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["ARCHIVE_WOULD_STRAND_CUSTODY"]


def test_archive_refuses_while_an_order_is_still_working() -> None:
    enabled, blockers = _archive(_ctx(working_order_count=1))

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["ARCHIVE_WOULD_STRAND_CUSTODY"]


def test_archive_refuses_when_the_clerk_cannot_prove_flatness() -> None:
    """The load-bearing difference from retire's guard ordering.

    Under an account freeze the Clerk cannot observe the broker, so
    ``has_exposure=False`` reports its ignorance rather than the bot's
    flatness. Archive's *enabling* proof is that reading, so it must refuse
    rather than treat an unproven fact as an enabling one -- where retire's
    custody check is only a backstop behind an independent proof.
    """
    enabled, blockers = _archive(_ctx(freeze_active=True))

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["ARCHIVE_CUSTODY_UNPROVABLE"]
    assert blockers[0].condition.scope == "account"


def test_a_frozen_account_refuses_archive_before_reporting_exposure() -> None:
    """Ordering: unprovable custody outranks the exposure it cannot prove."""
    enabled, blockers = _archive(_ctx(freeze_active=True, has_exposure=True))

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["ARCHIVE_CUSTODY_UNPROVABLE"]


def test_an_already_retired_registration_cannot_be_archived_again() -> None:
    enabled, blockers = _archive(_ctx(phase="RETIRED"))

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["BOT_ALREADY_RETIRED"]


def test_archive_carries_a_typed_confirmation_naming_the_custody_it_rests_on() -> None:
    """An irreversible command states the proof the operator is acting on."""
    from app.broker.v2panel.action_policy import build_actions_from_registry

    actions = {
        action.action_id: action
        for action in build_actions_from_registry(_ctx(), revision=1, broker="alpaca")
    }
    archive = actions["archive"]

    assert archive.enabled is True
    assert archive.confirmation is not None
    assert archive.confirmation.required_token == "ARCHIVE"
    # The blast radius is quoted from the same facts the guard used.
    assert "0 working orders" in archive.confirmation.body
    assert "Aug11" in archive.confirmation.body
    assert "cannot be undone" in archive.confirmation.consequence


def test_a_disabled_archive_offers_no_confirmation() -> None:
    """Confirmation copy describes a command the operator can actually run."""
    from app.broker.v2panel.action_policy import build_actions_from_registry

    actions = {
        action.action_id: action
        for action in build_actions_from_registry(
            _ctx(has_exposure=True), revision=1, broker="alpaca"
        )
    }

    assert actions["archive"].enabled is False
    assert actions["archive"].confirmation is None


def test_archive_and_retire_answer_independently() -> None:
    """Neither rule is expressed in terms of the other (#1795 is untouched).

    A healthy stopped bot is archive-eligible and retire-blocked; a bot whose
    strategy is gone is retire-eligible and, being equally stopped and flat,
    archive-eligible too. Archive never widens what retire admits.
    """
    healthy = _ctx()
    assert _archive(healthy)[0] is True
    retire_enabled, retire_blockers = ACTION_REGISTRY["retire"].guard(healthy)
    assert retire_enabled is False
    assert [b.condition.id for b in retire_blockers] == ["STRATEGY_STILL_RUNNABLE"]


def test_the_shared_rule_is_what_the_guard_renders() -> None:
    """The guard must not restate the rule -- commit-time answers the same one."""
    for kwargs in (
        {},
        {"running": True, "phase": "ON_DUTY"},
        {"has_exposure": True},
        {"working_order_count": 2},
        {"freeze_active": True},
        {"phase": "RETIRED"},
    ):
        ctx = _ctx(**kwargs)
        verdict = evaluate_archive(
            running=ctx.running,
            phase=ctx.phase,
            has_exposure=ctx.has_exposure,
            working_order_count=ctx.working_order_count,
            custody_provable=not ctx.freeze_active,
        )
        enabled, _ = _archive(ctx)
        assert enabled is verdict.eligible
