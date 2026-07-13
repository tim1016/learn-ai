"""Project account triage rows into bot daily lifecycle conditions."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from app.engine.live.account_artifacts import AccountArtifactError, AccountFreezeEvidence
from app.engine.live.account_identity import InvalidAccountIdError
from app.operator.notices.schema import OperatorNotice
from app.schemas.account_condition_actions import AccountCureAction
from app.schemas.account_reconciliation import AccountConditionRow
from app.schemas.live_runs import BotLifecycleCondition
from app.services.account_reconciliation import AccountReconciliationService

logger = logging.getLogger(__name__)

LIFECYCLE_CONDITION_CURE_LABELS: dict[AccountCureAction, str] = {
    "resolve_exposure": "Resolve exposure",
    "clear_freeze": "Clear account freeze",
    "reconcile_now": "Run account reconcile",
    "prove_evidence": "Prove broker evidence",
    "retire_replace": "Retire & Replace",
}

_INCIDENT_CONDITION_CODES = {
    "order.rejected",
    "submit.uncertain",
    "submit.halted",
    "submit.launch_failed",
    "submit.unmapped_diagnostic",
    "safety_halt.poisoned",
}


def lifecycle_conditions_for_instance(
    root: Path,
    *,
    account_id: str | None,
    sid: str,
    account_freeze: AccountFreezeEvidence | None,
    incident_headline_notice: OperatorNotice | None = None,
    now_ms: int,
) -> list[BotLifecycleCondition]:
    """Return renderable lifecycle conditions for one strategy instance."""

    incident_condition = _incident_condition(sid=sid, notice=incident_headline_notice)
    if account_id is None:
        return [*_fallback_freeze_condition(account_freeze), *incident_condition]
    try:
        triage = AccountReconciliationService(artifacts_root=root.parent).triage(
            account_id=account_id,
            now_ms=now_ms,
        )
    except (
        AccountArtifactError,
        InvalidAccountIdError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
    ) as exc:
        logger.warning(
            "failed to project account triage conditions for lifecycle status",
            extra={
                "strategy_instance_id": sid,
                "account_id": account_id,
                "error": str(exc),
            },
        )
        return [*_fallback_freeze_condition(account_freeze), *incident_condition]

    conditions = [condition for condition in triage.conditions if _condition_applies_to_instance(condition, sid)]
    conditions.sort(key=_lifecycle_condition_sort_key)
    return [*_to_lifecycle_conditions(conditions), *incident_condition]


def _incident_condition(
    *,
    sid: str,
    notice: OperatorNotice | None,
) -> list[BotLifecycleCondition]:
    if notice is None or notice.code not in _INCIDENT_CONDITION_CODES:
        return []
    cure_action: AccountCureAction = "retire_replace"
    if notice.code in {"order.rejected", "submit.uncertain", "submit.unmapped_diagnostic"}:
        cure_action = "prove_evidence"
    return [
        BotLifecycleCondition(
            scope="bot",
            severity="critical" if notice.tier == "critical" else "warning",
            title=notice.title,
            detail=notice.message,
            owner_label=f"Bot {sid}",
            cure_action=cure_action,
            cure_label=LIFECYCLE_CONDITION_CURE_LABELS[cure_action],
        )
    ]


def _fallback_freeze_condition(
    account_freeze: AccountFreezeEvidence | None,
) -> list[BotLifecycleCondition]:
    if account_freeze is None:
        return []
    cure_action: AccountCureAction = "resolve_exposure" if account_freeze.freeze_kind == "exposure" else "reconcile_now"
    return [
        BotLifecycleCondition(
            scope="account",
            severity="critical",
            title="Account freeze active",
            detail=(
                f"{account_freeze.reason} Account recovery details could not be "
                "loaded; run account reconcile to refresh the cure path."
            ),
            owner_label=f"Account {account_freeze.account_id}",
            cure_action=cure_action,
            cure_label=LIFECYCLE_CONDITION_CURE_LABELS[cure_action],
        )
    ]


def _condition_applies_to_instance(condition: AccountConditionRow, sid: str) -> bool:
    return (
        condition.scope == "account"
        or condition.owner.strategy_instance_id == sid
        or sid in condition.affected_strategy_instance_ids
    )


def _lifecycle_condition_sort_key(condition: AccountConditionRow) -> tuple[int, int, int, str]:
    severity_rank = 0 if condition.severity == "critical" else 1
    scope_rank = 0 if condition.scope == "bot" else 1
    return (severity_rank, scope_rank, -condition.evidence_at_ms, condition.condition_type)


def _to_lifecycle_conditions(conditions: list[AccountConditionRow]) -> list[BotLifecycleCondition]:
    return [
        BotLifecycleCondition(
            scope=condition.scope,
            severity=condition.severity,
            title=condition.title,
            detail=condition.detail,
            owner_label=condition.owner.label,
            cure_action=condition.cure_action,
            cure_label=LIFECYCLE_CONDITION_CURE_LABELS[condition.cure_action],
        )
        for condition in conditions
    ]


__all__ = ["lifecycle_conditions_for_instance"]
