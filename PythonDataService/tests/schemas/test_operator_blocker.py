from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.operator_blocker import (
    NavigateAction,
    OpenRunbookAction,
    OperatorBlocker,
    OperatorCondition,
    OperatorMove,
    RemoveAction,
    RetireReplaceAction,
)


def _nav_move() -> OperatorMove:
    return OperatorMove(
        label="Connect the broker",
        action=NavigateAction(kind="navigate", route="/broker", fragment=None),
    )


def test_fix_elsewhere_requires_primary_move() -> None:
    with pytest.raises(ValidationError, match="requires a primary_move"):
        OperatorBlocker.for_host(
            condition_id="broker_disconnected",
            scope="broker",
            host="bot_cockpit",
            disposition="fix_elsewhere",
            headline="Broker disconnected",
            detail=None,
            primary_move=None,
            secondary_moves=[],
            applies_to="both",
        )


def test_wait_must_not_carry_a_move() -> None:
    with pytest.raises(ValidationError, match="must not carry a primary_move"):
        OperatorBlocker.for_host(
            condition_id="broker_reconnecting",
            scope="broker",
            host="bot_cockpit",
            disposition="wait",
            headline="Waiting for broker to reconnect",
            detail=None,
            primary_move=_nav_move(),
            secondary_moves=[],
            applies_to="both",
        )


def test_terminal_requires_at_least_one_move() -> None:
    with pytest.raises(ValidationError, match="requires at least one move"):
        OperatorBlocker.for_host(
            condition_id="run_poisoned",
            scope="bot",
            host="bot_cockpit",
            disposition="terminal",
            headline="Can't recover",
            detail=None,
            primary_move=None,
            secondary_moves=[],
            applies_to="run",
        )


def test_valid_fix_elsewhere_blocker_constructs() -> None:
    blocker = OperatorBlocker.for_host(
        condition_id="broker_disconnected",
        scope="broker",
        host="bot_cockpit",
        disposition="fix_elsewhere",
        headline="Broker disconnected",
        detail="Connect the IBKR session before deploying.",
        primary_move=_nav_move(),
        secondary_moves=[],
        applies_to="both",
    )

    assert blocker.primary_move is not None
    assert blocker.primary_move.action.kind == "navigate"


def test_terminal_blocker_accepts_replace_and_remove_moves() -> None:
    blocker = OperatorBlocker.for_host(
        condition_id="run_poisoned",
        scope="bot",
        host="bot_cockpit",
        disposition="terminal",
        headline="Can't recover",
        detail="This run is poisoned and cannot be restarted safely.",
        primary_move=OperatorMove(
            label="Replace",
            action=RetireReplaceAction(kind="retire_replace"),
        ),
        secondary_moves=[
            OperatorMove(
                label="Remove",
                action=RemoveAction(kind="remove"),
            )
        ],
        applies_to="run",
    )

    assert blocker.primary_move is not None
    assert blocker.primary_move.action.kind == "retire_replace"
    assert blocker.secondary_moves[0].action.kind == "remove"


def test_fix_elsewhere_accepts_open_runbook_move() -> None:
    blocker = OperatorBlocker.for_host(
        condition_id="orphaned_socket",
        scope="broker",
        host="bot_cockpit",
        disposition="fix_elsewhere",
        headline="Bot socket is orphaned",
        detail="Review the broker session mirror before restarting.",
        primary_move=OperatorMove(
            label="Restart the launcher",
            action=OpenRunbookAction(kind="open_runbook", slug="broker-session-orphaned-socket"),
            target="broker-session-orphaned-socket",
        ),
        secondary_moves=[],
        applies_to="run",
    )

    assert blocker.primary_move is not None
    assert blocker.primary_move.action.kind == "open_runbook"


def test_same_condition_can_project_to_different_host_dispositions() -> None:
    condition = OperatorCondition(id="fleet_contaminated", severity="blocking", scope="fleet")

    cockpit = OperatorBlocker(
        condition=condition,
        host="bot_cockpit",
        disposition="fix_elsewhere",
        headline="Fleet state blocks starts",
        detail="Clear the account fleet state before starting another bot.",
        primary_move=OperatorMove(
            label="Open account monitor",
            action=NavigateAction(
                kind="navigate",
                route="/broker/account-monitor",
                fragment="account-reconciliation-action",
            ),
        ),
        secondary_moves=[],
        applies_to="both",
    )
    account_monitor = OperatorBlocker(
        condition=condition,
        host="account_monitor",
        disposition="fix_here",
        headline="Fleet state blocks starts",
        detail="Clear or reconcile the fleet state on this account.",
        primary_move=OperatorMove(
            label="Reconcile account",
            action=NavigateAction(
                kind="navigate",
                route="/broker/account-monitor",
                fragment="account-reconciliation-action",
            ),
        ),
        secondary_moves=[],
        applies_to="both",
    )

    assert cockpit.condition.id == account_monitor.condition.id
    assert cockpit.disposition == "fix_elsewhere"
    assert account_monitor.disposition == "fix_here"
    assert cockpit.host == "bot_cockpit"
    assert account_monitor.host == "account_monitor"
