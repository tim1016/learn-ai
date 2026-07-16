"""Contract tests for server-owned Account desk guidance routing."""

from __future__ import annotations

import pytest

from app.schemas.account_condition_actions import AccountCureAction
from app.schemas.account_reconciliation import AccountConditionOwner, AccountConditionRow
from app.services.account_desk_guidance import author_account_desk_blockers


@pytest.mark.parametrize(
    ("cure_action", "anchor", "audience", "disposition", "action_kind"),
    [
        ("reconcile_now", "reconciliation", "operator", "fix_elsewhere", "navigate"),
        ("prove_evidence", "lease", "operator", "wait", None),
        ("clear_freeze", "cure_tools", "both", "fix_elsewhere", "navigate"),
        ("resolve_exposure", "cure_tools", "both", "fix_elsewhere", "navigate"),
        ("retire_replace", "reconciliation", "operator", "fix_elsewhere", "navigate"),
    ],
)
def test_author_account_desk_blockers_preserves_condition_copy_and_declared_guidance(
    cure_action: AccountCureAction,
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

    [blocker] = author_account_desk_blockers([condition])

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
