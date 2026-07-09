from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.operator_blocker import (
    NavigateAction,
    OperatorBlocker,
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
        OperatorBlocker(
            id="broker_disconnected",
            severity="blocking",
            disposition="fix_elsewhere",
            headline="Broker disconnected",
            detail=None,
            primary_move=None,
            secondary_moves=[],
            applies_to="both",
        )


def test_wait_must_not_carry_a_move() -> None:
    with pytest.raises(ValidationError, match="must not carry a primary_move"):
        OperatorBlocker(
            id="broker_reconnecting",
            severity="blocking",
            disposition="wait",
            headline="Waiting for broker to reconnect",
            detail=None,
            primary_move=_nav_move(),
            secondary_moves=[],
            applies_to="both",
        )


def test_terminal_requires_at_least_one_move() -> None:
    with pytest.raises(ValidationError, match="requires at least one move"):
        OperatorBlocker(
            id="run_poisoned",
            severity="blocking",
            disposition="terminal",
            headline="Can't recover",
            detail=None,
            primary_move=None,
            secondary_moves=[],
            applies_to="run",
        )


def test_valid_fix_elsewhere_blocker_constructs() -> None:
    blocker = OperatorBlocker(
        id="broker_disconnected",
        severity="blocking",
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
    blocker = OperatorBlocker(
        id="run_poisoned",
        severity="blocking",
        disposition="terminal",
        headline="Can't recover",
        detail="This run is poisoned and cannot be restarted safely.",
        primary_move=OperatorMove(
            label="Replace",
            action=RetireReplaceAction(kind="retire_replace"),
            target="retire_replace",
        ),
        secondary_moves=[
            OperatorMove(
                label="Remove",
                action=RemoveAction(kind="remove"),
                target="delete",
            )
        ],
        applies_to="run",
    )

    assert blocker.primary_move is not None
    assert blocker.primary_move.action.kind == "retire_replace"
    assert blocker.secondary_moves[0].action.kind == "remove"
