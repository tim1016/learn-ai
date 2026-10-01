from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.operator_blocker import (
    NavigateAction,
    OperatorBlocker,
    OperatorBlockerAnchor,
    OperatorCondition,
    OperatorConfirmationCopy,
    OperatorMove,
    RemoveAction,
)


def _surface_anchor() -> OperatorBlockerAnchor:
    return OperatorBlockerAnchor(kind="surface", subject_key=None)


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
            anchor=_surface_anchor(),
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
            anchor=_surface_anchor(),
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
            anchor=_surface_anchor(),
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
        anchor=_surface_anchor(),
        disposition="fix_elsewhere",
        headline="Broker disconnected",
        detail="Connect the IBKR session before deploying.",
        primary_move=_nav_move(),
        secondary_moves=[],
        applies_to="both",
    )

    assert blocker.primary_move is not None
    assert blocker.primary_move.action.kind == "navigate"


def test_terminal_blocker_accepts_a_primary_and_a_secondary_move() -> None:
    blocker = OperatorBlocker.for_host(
        condition_id="run_poisoned",
        scope="bot",
        host="bot_cockpit",
        anchor=_surface_anchor(),
        disposition="terminal",
        headline="Can't recover",
        detail="This run is poisoned and cannot be restarted safely.",
        primary_move=_nav_move(),
        secondary_moves=[
            OperatorMove(
                label="Remove",
                action=RemoveAction(kind="remove"),
            )
        ],
        applies_to="run",
    )

    assert blocker.primary_move is not None
    assert blocker.primary_move.action.kind == "navigate"
    assert blocker.secondary_moves[0].action.kind == "remove"


def test_operator_move_serializes_backend_confirmation_copy() -> None:
    move = OperatorMove(
        label="Remove",
        action=RemoveAction(kind="remove"),
        confirmation=OperatorConfirmationCopy(
            title="Remove this bot",
            body="Remove this bot from the catalog.",
            consequence="Only confirm after broker exposure is flat.",
            confirm_label="Remove",
        ),
    )

    assert move.model_dump()["confirmation"] == {
        "title": "Remove this bot",
        "body": "Remove this bot from the catalog.",
        "consequence": "Only confirm after broker exposure is flat.",
        "confirm_label": "Remove",
        "required_token": "",
    }


def test_same_condition_can_project_to_different_host_dispositions() -> None:
    condition = OperatorCondition(id="fleet_contaminated", severity="blocking", scope="fleet")

    cockpit = OperatorBlocker(
        condition=condition,
        host="bot_cockpit",
        anchor=_surface_anchor(),
        disposition="fix_elsewhere",
        headline="Fleet state blocks starts",
        detail="Clear the account fleet state before starting another bot.",
        primary_move=OperatorMove(
            label="Open Accounts",
            action=NavigateAction(
                kind="navigate",
                route="/broker/accounts",
                fragment=None,
            ),
        ),
        secondary_moves=[],
        applies_to="both",
    )
    monitor = OperatorBlocker(
        condition=condition,
        host="account_monitor",
        anchor=_surface_anchor(),
        disposition="fix_here",
        headline="Fleet state blocks starts",
        detail="Clear or reconcile the fleet state on this account.",
        primary_move=OperatorMove(
            label="Reconcile account",
            action=NavigateAction(
                kind="navigate",
                route="/broker/accounts/DU123456",
                fragment="account-desk-recovery-controls",
            ),
        ),
        secondary_moves=[],
        applies_to="both",
    )

    assert cockpit.condition.id == monitor.condition.id
    assert cockpit.disposition == "fix_elsewhere"
    assert monitor.disposition == "fix_here"
    assert cockpit.host == "bot_cockpit"
    assert monitor.host == "account_monitor"


@pytest.mark.parametrize(
    ("kind", "subject_key", "error"),
    [
        ("holdings_row", None, "requires a subject_key"),
        ("event", "", "requires a subject_key"),
        ("verdict", "DU123", "must not carry a subject_key"),
        ("surface", "account:DU123", "must not carry a subject_key"),
    ],
)
def test_anchor_rejects_invalid_kind_and_subject_key_pairing(
    kind: str,
    subject_key: str | None,
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        OperatorBlockerAnchor(kind=kind, subject_key=subject_key)


def test_anchor_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        OperatorBlockerAnchor(kind="unknown", subject_key=None)


def test_anchor_requires_subject_key_field() -> None:
    with pytest.raises(ValidationError, match="subject_key"):
        OperatorBlockerAnchor.model_validate({"kind": "surface"})


def test_operator_blocker_requires_its_anchor() -> None:
    blocker = OperatorBlocker.for_host(
        condition_id="broker_disconnected",
        scope="broker",
        host="account_monitor",
        anchor=_surface_anchor(),
        disposition="fix_elsewhere",
        headline="Broker disconnected",
        detail="Connect the IBKR session before deploying.",
        primary_move=_nav_move(),
        applies_to="both",
    )
    payload = blocker.model_dump()
    payload.pop("anchor")

    with pytest.raises(ValidationError, match="anchor"):
        OperatorBlocker.model_validate(payload)


def test_anchor_preserves_opaque_subject_key() -> None:
    anchor = OperatorBlockerAnchor(
        kind="holdings_row",
        subject_key="DU123|con_id:265598|SPY  260620C00500000",
    )

    assert anchor.subject_key == "DU123|con_id:265598|SPY  260620C00500000"


def test_operator_blocker_wire_contract_pins_its_fields() -> None:
    blocker = OperatorBlocker.for_host(
        condition_id="fleet_contaminated",
        scope="fleet",
        host="account_monitor",
        anchor=OperatorBlockerAnchor(kind="reconciliation", subject_key=None),
        disposition="fix_elsewhere",
        headline="Fleet state blocks starts",
        detail="Clear the account fleet state before starting another bot.",
        primary_move=_nav_move(),
        applies_to="both",
    )

    assert blocker.model_dump() == {
        "condition": {
            "id": "fleet_contaminated",
            "severity": "blocking",
            "scope": "fleet",
            "evidence": {},
        },
        "host": "account_monitor",
        "anchor": {"kind": "reconciliation", "subject_key": None},
        "disposition": "fix_elsewhere",
        "headline": "Fleet state blocks starts",
        "detail": "Clear the account fleet state before starting another bot.",
        "primary_move": {
            "label": "Connect the broker",
            "action": {"kind": "navigate", "route": "/broker", "fragment": None},
            "target": None,
            "confirmation": None,
        },
        "secondary_moves": [],
        "applies_to": "both",
    }
