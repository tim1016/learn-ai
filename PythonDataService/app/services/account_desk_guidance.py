"""Backend-authored Account desk guidance from existing triage conditions."""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.schemas.account_condition_actions import AccountCureAction
from app.schemas.account_reconciliation import AccountConditionRow
from app.schemas.journal_cures import AccountRecoveryFlattenCandidate
from app.schemas.operator_blocker import (
    ConfirmInFormAction,
    Disposition,
    NavigateAction,
    OperatorBlocker,
    OperatorBlockerAnchor,
    OperatorBlockerAudience,
    OperatorConfirmationCopy,
    OperatorMove,
)


@dataclass(frozen=True)
class _GuidanceSpec:
    anchor: OperatorBlockerAnchor
    audience: OperatorBlockerAudience
    disposition: Disposition
    move_label: str | None = None
    move_action: ConfirmInFormAction | NavigateAction | None = None
    confirmation: OperatorConfirmationCopy | None = None


_CURE_ACTION_GUIDANCE: dict[AccountCureAction, _GuidanceSpec] = {
    "reconcile_now": _GuidanceSpec(
        anchor=OperatorBlockerAnchor(kind="reconciliation", subject_key=None),
        audience="operator",
        disposition="fix_here",
        move_label="Run account reconcile",
        move_action=ConfirmInFormAction(
            kind="confirm_in_form",
            anchor="account-reconciliation-action",
        ),
        confirmation=OperatorConfirmationCopy(
            title="Run account reconciliation",
            body="Request a fresh account reconciliation for this account.",
            consequence="The returned reconciliation receipt will replace the current proof on this desk.",
            confirm_label="Run account reconcile",
        ),
    ),
    "prove_evidence": _GuidanceSpec(
        anchor=OperatorBlockerAnchor(kind="lease", subject_key=None),
        audience="operator",
        disposition="wait",
    ),
    "clear_freeze": _GuidanceSpec(
        anchor=OperatorBlockerAnchor(kind="cure_tools", subject_key=None),
        audience="operator",
        disposition="fix_here",
        move_label="Clear account freeze",
        move_action=ConfirmInFormAction(
            kind="confirm_in_form",
            anchor="account-clear-freeze-action",
        ),
        confirmation=OperatorConfirmationCopy(
            title="Clear account freeze",
            body="Clear the active account freeze using the currently proven reconciliation receipt.",
            consequence="New account starts may become eligible after the server accepts the recovery proof.",
            confirm_label="Clear account freeze",
        ),
    ),
    "resolve_exposure": _GuidanceSpec(
        anchor=OperatorBlockerAnchor(kind="cure_tools", subject_key=None),
        audience="operator",
        disposition="fix_here",
        move_label="Accept account exposure",
        move_action=ConfirmInFormAction(
            kind="confirm_in_form",
            anchor="account-exposure-override-action",
        ),
        confirmation=OperatorConfirmationCopy(
            title="Accept account exposure",
            body="Record an audited acceptance of the server-projected account exposure.",
            consequence="The server will re-evaluate the account freeze using the submitted operator reason.",
            confirm_label="Accept exposure",
        ),
    ),
    "retire_replace": _GuidanceSpec(
        anchor=OperatorBlockerAnchor(kind="reconciliation", subject_key=None),
        audience="operator",
        disposition="fix_elsewhere",
        move_label="Open bot controls",
        move_action=NavigateAction(kind="navigate", route="/broker/bots"),
    ),
}


def author_account_desk_blockers(
    conditions: list[AccountConditionRow],
    *,
    clear_freeze_actionable: bool,
    recovery_flatten_candidates: list[AccountRecoveryFlattenCandidate] | None = None,
) -> list[OperatorBlocker]:
    """Attach declared guidance to triage conditions without client inference."""

    blockers: list[OperatorBlocker] = []
    for condition in conditions:
        spec = _guidance_spec_for_condition(
            condition,
            clear_freeze_actionable=clear_freeze_actionable,
        )
        primary_move = None
        if spec.move_label is not None and spec.move_action is not None:
            primary_move = OperatorMove(
                label=spec.move_label,
                action=spec.move_action,
                confirmation=spec.confirmation,
            )
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
    for candidate in recovery_flatten_candidates or []:
        blockers.append(
            OperatorBlocker.for_host(
                condition_id=f"account-recovery-flatten:{candidate.intent.intent_id}",
                scope="bot",
                host="account_desk",
                anchor=OperatorBlockerAnchor(kind="cure_tools", subject_key=None),
                audience="operator",
                disposition="fix_here",
                headline="Clerk recovery flatten is ready",
                detail="The server has prepared one exact recovery request for a retired namespace.",
                applies_to="both",
                primary_move=OperatorMove(
                    label="Review recovery flatten",
                    target=candidate.intent.intent_id,
                    action=ConfirmInFormAction(
                        kind="confirm_in_form",
                        anchor="account-recovery-flatten-action",
                    ),
                    confirmation=candidate.confirmation,
                ),
                severity="blocking",
                evidence={
                    "source": "account_clerk_journal",
                    "evidence_at_ms": candidate.intent.created_at_ms,
                },
            )
        )
    return blockers


def _guidance_spec_for_condition(
    condition: AccountConditionRow,
    *,
    clear_freeze_actionable: bool,
) -> _GuidanceSpec:
    spec = _CURE_ACTION_GUIDANCE[condition.cure_action]
    if condition.cure_action == "clear_freeze" and not clear_freeze_actionable:
        return replace(spec, disposition="wait", move_label=None, move_action=None, confirmation=None)
    return spec
