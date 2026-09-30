"""Archive eligibility: the one exit for a bot you are finished with (ADR 0052).

#1911 measured that a bot you are done with could be stopped but never
removed, so catalog rows only accumulated, and #1801 measured both read and
deploy cost as linear in exactly that number. Archive -- Clear on Home's
Finished fold -- is that exit, and since #2578 the only one: Retire and its
provably-dead proofs are gone.

Its enabling proof is custody: the registration is stopped, settled, flat,
and holds no working orders. These tests pin that proof -- above all that it
must be *believable* before it is believed, which is why a frozen account
refuses even though it reports no exposure.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.broker.v2panel.action_policy import ArchiveVerdict, archive_action, evaluate_archive
from app.schemas.broker_v2_panel import PanelAction


def _action(
    *,
    running: bool = False,
    phase: str = "OFF_DUTY",
    has_exposure: bool = False,
    working_order_count: int = 0,
    freeze_active: bool = False,
) -> PanelAction:
    return archive_action(
        running=running,
        phase=phase,
        freeze_active=freeze_active,
        exposure={"SPY": 1.0} if has_exposure else {},
        working_order_count=working_order_count,
        account_id="PA3KWXU1C4C3",
        strategy_instance_id="Aug11",
        revision=1,
    )


def _archive(**facts: Any) -> tuple[bool, list]:
    action = _action(**facts)
    return action.enabled, action.blockers


def test_a_stopped_flat_bot_is_archive_eligible() -> None:
    """The case #1911 asked for."""
    enabled, blockers = _archive()

    assert enabled is True
    assert blockers == []


def test_archive_refuses_a_running_bot() -> None:
    enabled, blockers = _archive(running=True, phase="ON_DUTY")

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["BOT_STILL_RUNNING"]


def test_archive_refuses_a_dead_process_whose_run_never_settled() -> None:
    """`running` and `phase` are different facts, and both are required.

    A task that dies before its stop transition commits reads
    ``running=False`` while the authority still holds an ACTIVE run.
    Archiving there would stamp `retired_at_ms` on a registration whose run
    never ended -- and the fold that writes it states there is no active run.
    """
    enabled, blockers = _archive(running=False, phase="ON_DUTY")

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["BOT_DUTY_NOT_SETTLED"]


@pytest.mark.parametrize("phase", ["OFF_DUTY", "ON_DUTY"])
def test_archive_refuses_a_bot_sealed_on_an_account_the_clerk_does_not_hold(phase: str) -> None:
    """A live account's shadow rehearsal after graduation (#2589): nothing here
    can prove it holds nothing, and no wait changes that -- so it is refused
    for its account, and ahead of a not-yet-settled run, whose words would
    promise a clear that never comes."""
    verdict = evaluate_archive(
        running=False,
        phase=phase,
        custody_account_foreign=True,
        has_exposure=False,
        working_order_count=0,
        outstanding_effect_count=0,
        custody_provable=False,
    )

    assert verdict == ArchiveVerdict(eligible=False, cause="ARCHIVE_SEALED_ACCOUNT_CUSTODY")


def test_archive_refuses_while_an_effect_is_still_unresolved() -> None:
    """An accepted effect can create broker custody after the archive lands.

    Only the commit sees this count -- the panel has no bot-scoped effect
    view -- which is why the rule takes it as an argument rather than reading
    it, and why an armed button can still be refused on click.
    """
    verdict = evaluate_archive(
        running=False,
        phase="OFF_DUTY",
        custody_account_foreign=False,
        has_exposure=False,
        working_order_count=0,
        outstanding_effect_count=1,
        custody_provable=True,
    )

    assert verdict.eligible is False
    assert verdict.cause == "ARCHIVE_WOULD_STRAND_CUSTODY"


def test_archive_refuses_while_the_bot_still_holds_exposure() -> None:
    enabled, blockers = _archive(has_exposure=True)

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["ARCHIVE_WOULD_STRAND_CUSTODY"]


def test_archive_refuses_while_an_order_is_still_working() -> None:
    enabled, blockers = _archive(working_order_count=1)

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["ARCHIVE_WOULD_STRAND_CUSTODY"]


def test_archive_refuses_when_the_clerk_cannot_prove_flatness() -> None:
    """The load-bearing guard ordering.

    Under an account freeze the Clerk cannot observe the broker, so
    ``has_exposure=False`` reports its ignorance rather than the bot's
    flatness. Archive's *enabling* proof is that reading, so it must refuse
    rather than treat an unproven fact as an enabling one.
    """
    enabled, blockers = _archive(freeze_active=True)

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["ARCHIVE_CUSTODY_UNPROVABLE"]
    assert blockers[0].condition.scope == "account"


def test_a_frozen_account_refuses_archive_before_reporting_exposure() -> None:
    """Ordering: unprovable custody outranks the exposure it cannot prove."""
    enabled, blockers = _archive(freeze_active=True, has_exposure=True)

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["ARCHIVE_CUSTODY_UNPROVABLE"]


def test_an_already_retired_registration_cannot_be_archived_again() -> None:
    enabled, blockers = _archive(phase="RETIRED")

    assert enabled is False
    assert [b.condition.id for b in blockers] == ["BOT_ALREADY_RETIRED"]


def test_archive_carries_a_typed_confirmation_naming_the_custody_it_rests_on() -> None:
    """An irreversible command states the proof the operator is acting on."""
    archive = _action()

    assert archive.enabled is True
    assert archive.confirmation is not None
    assert archive.confirmation.required_token == "ARCHIVE"
    # The blast radius is quoted from the same facts the guard used.
    assert "0 working orders" in archive.confirmation.body
    assert "Aug11" in archive.confirmation.body
    assert "cannot be undone" in archive.confirmation.consequence


def test_a_disabled_archive_offers_no_confirmation() -> None:
    """Confirmation copy describes a command the operator can actually run."""
    archive = _action(has_exposure=True)

    assert archive.enabled is False
    assert archive.confirmation is None


def test_the_archive_token_keys_only_on_the_facts_its_rule_reads() -> None:
    """Archive owns its compare-and-set domain: the account, the bot's name and
    the panel revision cannot make a presented Clear stale (#2635 kept the
    payload the registry hashed, so the served token is unchanged)."""
    baseline = _action()
    elsewhere = archive_action(
        running=False,
        phase="OFF_DUTY",
        freeze_active=False,
        exposure={},
        working_order_count=0,
        account_id="318420190",
        strategy_instance_id="another-bot",
        revision=99,
    )

    # The token every lane served for a stopped, flat, unfrozen bot on
    # 2026-09-30 (paper, live and Dry Run panels alike).
    assert baseline.concurrency_token == "9a8f9b9530fb687e13bc07cfd1c5b9aa"
    assert elsewhere.concurrency_token == baseline.concurrency_token
    for changed in (
        _action(has_exposure=True),
        _action(working_order_count=1),
        _action(freeze_active=True),
        _action(phase="RETIRED"),
    ):
        assert changed.concurrency_token != baseline.concurrency_token


@pytest.mark.parametrize(
    "facts",
    [
        {},
        {"running": True, "phase": "ON_DUTY"},
        {"has_exposure": True},
        {"working_order_count": 2},
        {"freeze_active": True},
        {"phase": "RETIRED"},
    ],
)
def test_the_shared_rule_is_what_the_action_renders(facts: dict[str, Any]) -> None:
    """The action must not restate the rule -- commit-time answers the same one."""
    resolved = {
        "running": False,
        "phase": "OFF_DUTY",
        "has_exposure": False,
        "working_order_count": 0,
        "freeze_active": False,
        **facts,
    }
    verdict = evaluate_archive(
        running=resolved["running"],
        phase=resolved["phase"],
        custody_account_foreign=False,
        has_exposure=resolved["has_exposure"],
        working_order_count=resolved["working_order_count"],
        outstanding_effect_count=0,
        custody_provable=not resolved["freeze_active"],
    )

    assert _action(**resolved).enabled is verdict.eligible
