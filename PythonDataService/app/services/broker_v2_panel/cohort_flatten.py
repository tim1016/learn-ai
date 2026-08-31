"""Cohort-scoped flatten: N attributed per-bot legs behind one affordance.

ADR 0051 (#1802, finding T3): same-symbol cohorts strand in lockstep on a
stop wave, and the proven per-bot remedy costs N×3 clicks. This module adds
orchestration only — every leg runs the existing, unchanged per-bot action
pipeline (`panel_data_source.run_action`), so attribution, guards, tokens,
idempotency, and per-bot receipts are inherited rather than re-proven.

The presentation (`get_cohort_flatten_view`) is a pure read: it projects the
same per-bot panel actions the single-bot surface presents, grouped by
``(strategy_key, symbol)``. It is fetched on demand, not polled — it builds a
panel projection per cohort member, which is deliberate (a presented leg *is*
the member's presented action) and priced for an operator opening a surface,
not a poll loop.
"""

from __future__ import annotations

import logging
from typing import Literal, cast

from app.schemas.broker_v2_panel import (
    BotCatalogView,
    CohortFlattenActionId,
    CohortFlattenCohort,
    CohortFlattenLeg,
    CohortFlattenLegRequest,
    CohortFlattenLegResult,
    CohortFlattenRequest,
    CohortFlattenResult,
    CohortFlattenView,
    PanelAction,
    PanelActionErrorResponse,
    PanelActionRequest,
)
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.action_execution_service import (
    ActionExecutionError,
    ActionOutcomeUnknownError,
    AuthorityPoisonedError,
    ExecutionAuthorityLostError,
    StaleRevisionError,
)
from app.services.broker_v2_panel.panel_errors import PanelDataError
from app.services.broker_v2_panel.panel_scope import validate_account
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: Presentation preference order: the running bot's ``flatten_stop`` first,
#: the stranded stopped bot's recovery-ladder ``execute_safe_flatten`` second.
_FLATTEN_ACTION_IDS: tuple[CohortFlattenActionId, ...] = (
    "flatten_stop",
    "execute_safe_flatten",
)


def _presented_flatten(actions: list[PanelAction]) -> PanelAction | None:
    """The member's presented flatten-class action, preferring an armed one."""
    by_id = {
        action.action_id: action
        for action in actions
        if action.action_id in _FLATTEN_ACTION_IDS
    }
    for action_id in _FLATTEN_ACTION_IDS:
        action = by_id.get(action_id)
        if action is not None and action.enabled:
            return action
    for action_id in _FLATTEN_ACTION_IDS:
        action = by_id.get(action_id)
        if action is not None:
            return action
    return None


def _leg_from_panel_action(row: BotCatalogView, action: PanelAction | None) -> CohortFlattenLeg:
    blocker_headline: str | None = None
    if action is not None and not action.enabled and action.blockers:
        blocker_headline = action.blockers[0].headline
    return CohortFlattenLeg(
        strategy_instance_id=row.strategy_instance_id,
        # Narrowing is proven by construction: ``_presented_flatten`` only
        # returns actions whose id is in the closed flatten pair.
        action_id=(
            None if action is None else cast(CohortFlattenActionId, action.action_id)
        ),
        enabled=action is not None and action.enabled,
        revision=None if action is None else action.revision,
        concurrency_token=None if action is None else action.concurrency_token,
        blocker_headline=blocker_headline,
        exposure=row.exposure,
    )


async def get_cohort_flatten_view(broker: str, account_id: str) -> CohortFlattenView:
    """Group the roster by (strategy_key, symbol); present per-leg facts."""
    resolved = await validate_account(broker, account_id)
    rows = await panel_data_source.get_catalog(broker, resolved)
    groups: dict[tuple[str, str], list[BotCatalogView]] = {}
    for row in rows:
        if row.phase == "RETIRED":
            continue
        groups.setdefault((row.strategy_key, row.symbol), []).append(row)

    cohorts: list[CohortFlattenCohort] = []
    for (strategy_key, symbol), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        legs: list[CohortFlattenLeg] = []
        for row in sorted(members, key=lambda member: member.strategy_instance_id):
            panel = await panel_data_source.get_panel(
                broker, resolved, row.strategy_instance_id
            )
            legs.append(_leg_from_panel_action(row, _presented_flatten(panel.actions)))
        cohorts.append(
            CohortFlattenCohort(
                strategy_key=strategy_key,
                strategy_label=members[0].strategy_label,
                symbol=symbol,
                legs=legs,
                enabled_count=sum(1 for leg in legs if leg.enabled),
            )
        )
    return CohortFlattenView(
        account_id=resolved,
        cohorts=cohorts,
        observed_at_ms=now_ms_utc(),
    )


def _leg_error(
    leg: CohortFlattenLegRequest,
    error: Exception,
    *,
    outcome: Literal["refused", "failed", "unknown"],
    receipt_id: str | None,
    reason_code: str | None,
) -> CohortFlattenLegResult:
    detail = getattr(error, "detail", None)
    return CohortFlattenLegResult(
        strategy_instance_id=leg.strategy_instance_id,
        outcome=outcome,
        result=None,
        error=PanelActionErrorResponse(
            action_id=leg.action_id,
            outcome=(
                "unknown"
                if outcome == "unknown"
                else ("conflict" if outcome == "refused" else "failure")
            ),
            receipt_id=receipt_id,
            recorded_at_ms=now_ms_utc(),
            message=str(error),
            why=detail,
            reason_code=reason_code,
        ),
    )


async def run_cohort_flatten(
    broker: str,
    account_id: str,
    request: CohortFlattenRequest,
    *,
    operator_identity: str,
) -> CohortFlattenResult:
    """Execute the named legs in order; a leg's own trouble never aborts its siblings.

    ADR 0051: sequential on purpose (the account authority is a single
    writer, so parallelism buys no wall time and would make receipt order
    nondeterministic), explicit membership (exactly the named legs run), and
    derived per-leg idempotency identity (``{idempotency_key}:{sid}``) so a
    re-POST replays applied legs as no-ops and retries only released ones.
    The one early exit is account-scoped authority loss — a fact about the
    account, not any leg — where the batch ends with the attempted legs'
    outcomes and the rest absent (safe to re-POST under the same key).
    """
    resolved = await validate_account(broker, account_id)
    legs: list[CohortFlattenLegResult] = []
    for leg in request.legs:
        leg_request = PanelActionRequest(
            action_id=leg.action_id,
            revision=leg.revision,
            concurrency_token=leg.concurrency_token,
            idempotency_key=f"{request.idempotency_key}:{leg.strategy_instance_id}",
            reason=request.reason,
        )
        try:
            result = await panel_data_source.run_action(
                broker,
                resolved,
                leg.strategy_instance_id,
                leg_request,
                operator_identity=operator_identity,
            )
        except ActionOutcomeUnknownError as error:
            legs.append(
                _leg_error(
                    leg,
                    error,
                    outcome="unknown",
                    receipt_id=leg_request.idempotency_key,
                    reason_code=error.reason_code,
                )
            )
        except (ExecutionAuthorityLostError, AuthorityPoisonedError) as error:
            # Account-scoped authority loss: no later leg can succeed. Record
            # this leg's typed failure and end the batch early — unattempted
            # legs are absent from the response and safe to re-POST under the
            # same cohort key once the account-scoped cure lands (ADR 0051).
            legs.append(
                _leg_error(
                    leg,
                    error,
                    outcome="failed",
                    receipt_id=None,
                    reason_code=error.reason_code,
                )
            )
            break
        except ActionExecutionError as error:
            refused = isinstance(error, StaleRevisionError) or error.http_status == 409
            legs.append(
                _leg_error(
                    leg,
                    error,
                    outcome="refused" if refused else "failed",
                    receipt_id=None,
                    reason_code=error.reason_code,
                )
            )
        except PanelDataError as error:
            legs.append(
                _leg_error(
                    leg,
                    error,
                    outcome="failed",
                    receipt_id=None,
                    reason_code=None,
                )
            )
        else:
            legs.append(
                CohortFlattenLegResult(
                    strategy_instance_id=leg.strategy_instance_id,
                    outcome="applied" if result.applied else "replayed",
                    result=result,
                    error=None,
                )
            )
    logger.info(
        "cohort flatten executed",
        extra={
            "action": "cohort_flatten_executed",
            "account_id": resolved,
            "receipt_id": request.idempotency_key,
            "leg_count": len(legs),
            "applied": sum(1 for leg in legs if leg.outcome == "applied"),
            "refused": sum(1 for leg in legs if leg.outcome == "refused"),
            "failed": sum(1 for leg in legs if leg.outcome in ("failed", "unknown")),
        },
    )
    return CohortFlattenResult(
        account_id=resolved,
        receipt_id=request.idempotency_key,
        recorded_at_ms=now_ms_utc(),
        legs=legs,
        applied_count=sum(1 for leg in legs if leg.outcome == "applied"),
        replayed_count=sum(1 for leg in legs if leg.outcome == "replayed"),
        refused_count=sum(1 for leg in legs if leg.outcome == "refused"),
        failed_count=sum(1 for leg in legs if leg.outcome in ("failed", "unknown")),
    )
