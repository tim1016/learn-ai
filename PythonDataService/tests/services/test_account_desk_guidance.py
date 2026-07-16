"""Contract tests for server-owned Account desk guidance routing."""

from __future__ import annotations

import pytest

from app.schemas.account_condition_actions import AccountCureAction
from app.schemas.account_reconciliation import AccountConditionOwner, AccountConditionRow
from app.services.account_desk_guidance import author_account_desk_blockers


@pytest.mark.parametrize(
    ("cure_action", "clear_freeze_actionable", "anchor", "audience", "disposition", "action_kind"),
    [
        ("reconcile_now", False, "reconciliation", "operator", "fix_here", "confirm_in_form"),
        ("prove_evidence", False, "reconciliation", "operator", "fix_here", "confirm_in_form"),
        ("clear_freeze", False, "cure_tools", "operator", "wait", None),
        ("clear_freeze", True, "cure_tools", "operator", "fix_here", "confirm_in_form"),
        ("resolve_exposure", False, "cure_tools", "operator", "fix_here", "confirm_in_form"),
        ("retire_replace", False, "reconciliation", "operator", "fix_elsewhere", "navigate"),
    ],
)
def test_author_account_desk_blockers_preserves_condition_copy_and_declared_guidance(
    cure_action: AccountCureAction,
    clear_freeze_actionable: bool,
    anchor: str,
    audience: str,
    disposition: str,
    action_kind: str | None,
) -> None:
    condition = AccountConditionRow(
        condition_type="evidence_stale",
        scope="account",
        owner=AccountConditionOwner(
            owner_type="account",
            owner_id="DU1234567",
            label="Account DU1234567",
        ),
        severity="warning",
        title="Backend-authored condition title",
        detail="Backend-authored condition detail.",
        source="account_reconciliation_receipt",
        evidence_at_ms=1_780_000_000_000,
        cure_action=cure_action,
    )

    [blocker] = author_account_desk_blockers(
        [condition],
        clear_freeze_actionable=clear_freeze_actionable,
    )

    assert blocker.condition.id == "account-condition:evidence_stale:DU1234567"
    assert blocker.headline == condition.title
    assert blocker.detail == condition.detail
    assert blocker.anchor.kind == anchor
    assert blocker.audience == audience
    assert blocker.disposition == disposition
    if action_kind is None:
        assert blocker.primary_move is None
    else:
        assert blocker.primary_move is not None
        assert blocker.primary_move.action.kind == action_kind
        if action_kind == "confirm_in_form":
            assert blocker.primary_move.confirmation is not None
