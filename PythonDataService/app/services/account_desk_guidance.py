"""Backend-authored Account desk guidance from existing triage conditions."""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.account_condition_actions import AccountCureAction
from app.schemas.account_reconciliation import AccountConditionRow
from app.schemas.operator_blocker import (
    Disposition,
    NavigateAction,
    OperatorBlocker,
    OperatorBlockerAnchor,
    OperatorBlockerAudience,
    OperatorMove,
)


@dataclass(frozen=True)
class _GuidanceSpec:
    anchor: OperatorBlockerAnchor
    audience: OperatorBlockerAudience
    disposition: Disposition
    move_label: str | None = None
    move_action: NavigateAction | None = None


_CURE_ACTION_GUIDANCE: dict[AccountCureAction, _GuidanceSpec] = {
    "reconcile_now": _GuidanceSpec(
        anchor=OperatorBlockerAnchor(kind="reconciliation", subject_key=None),
        audience="operator",
        disposition="fix_elsewhere",
        move_label="Open account reconciliation",
        move_action=NavigateAction(
            kind="navigate",
            route="/broker/account-monitor",
            fragment="account-reconciliation-action",
        ),
    ),
    "prove_evidence": _GuidanceSpec(
        anchor=OperatorBlockerAnchor(kind="lease", subject_key=None),
        audience="operator",
        disposition="fix_elsewhere",
        move_label="Open account reconciliation",
        move_action=NavigateAction(
            kind="navigate",
            route="/broker/account-monitor",
            fragment="account-reconciliation-action",
        ),
    ),
    "clear_freeze": _GuidanceSpec(
        anchor=OperatorBlockerAnchor(kind="cure_tools", subject_key=None),
        audience="both",
        disposition="fix_elsewhere",
        move_label="Open account recovery controls",
        move_action=NavigateAction(
            kind="navigate",
            route="/broker/account-monitor",
            fragment="account-clear-freeze-action",
        ),
    ),
    "resolve_exposure": _GuidanceSpec(
        anchor=OperatorBlockerAnchor(kind="cure_tools", subject_key=None),
        audience="both",
        disposition="fix_elsewhere",
        move_label="Open account recovery controls",
        move_action=NavigateAction(
            kind="navigate",
            route="/broker/account-monitor",
            fragment="account-primary-action",
        ),
    ),
    "retire_replace": _GuidanceSpec(
        anchor=OperatorBlockerAnchor(kind="reconciliation", subject_key=None),
        audience="operator",
        disposition="fix_elsewhere",
        move_label="Open bot controls",
    ),
}


def author_account_desk_blockers(
    conditions: list[AccountConditionRow],
) -> list[OperatorBlocker]:
    """Attach declared guidance to triage conditions without client inference."""

    blockers: list[OperatorBlocker] = []
    for condition in conditions:
        spec = _CURE_ACTION_GUIDANCE[condition.cure_action]
        primary_move = None
        if spec.move_label is not None:
            action = spec.move_action
            if condition.cure_action == "retire_replace":
                action = NavigateAction(
                    kind="navigate",
                    route=f"/broker/bots/{condition.owner.owner_id}",
                )
            if action is not None:
                primary_move = OperatorMove(label=spec.move_label, action=action)
        blockers.append(
            OperatorBlocker.for_host(
                condition_id=f"account-condition:{condition.condition_type}:{condition.owner.owner_id}",
                scope="account" if condition.scope == "account" else "bot",
                host="account_desk",
                anchor=spec.anchor,
                audience=spec.audience,
                disposition=spec.disposition,
                headline=condition.title,
                detail=condition.detail,
                applies_to="both",
                primary_move=primary_move,
                severity="blocking" if condition.severity == "critical" else "warning",
                evidence={
                    "source": condition.source,
                    "evidence_at_ms": condition.evidence_at_ms,
                },
            )
        )
    return blockers
