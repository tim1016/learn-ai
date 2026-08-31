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
from typing import cast

from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotPanelView,
    CohortActionResult,
    CohortFlattenActionId,
    CohortFlattenCohort,
    CohortFlattenLeg,
    CohortFlattenRequest,
    CohortFlattenView,
    PanelAction,
)
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.cohort_execution import (
    CohortLegCommand,
    count_outcomes,
    execute_cohort_legs,
)
from app.services.broker_v2_panel.panel_scope import validate_account
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: Presentation preference order. Where a surface presents both, the
#: lifecycle ``flatten_stop`` wins; under the active SQLite authority only
#: the recovery ladder's ``execute_safe_flatten`` reaches the panel, and a
#: running member presents it disabled — the ladder is stop first, then
#: flatten, which is exactly T3's stop-wave-then-flatten sequence.
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


def _leg_from_panel(panel: BotPanelView, action: PanelAction | None) -> CohortFlattenLeg:
    blocker_headline: str | None = None
    if action is not None and not action.enabled and action.blockers:
        blocker_headline = action.blockers[0].headline
    return CohortFlattenLeg(
        strategy_instance_id=panel.strategy_instance_id,
        # Narrowing is proven by construction: ``_presented_flatten`` only
        # returns actions whose id is in the closed flatten pair.
        action_id=(
            None if action is None else cast(CohortFlattenActionId, action.action_id)
        ),
        enabled=action is not None and action.enabled,
        revision=None if action is None else action.revision,
        concurrency_token=None if action is None else action.concurrency_token,
        blocker_headline=blocker_headline,
        # From the same panel cut as the presented action, never the earlier
        # catalog cut: the blast-radius quantity the operator confirms must
        # be the one the accepted token will flatten.
        exposure=panel.exposure,
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
            legs.append(_leg_from_panel(panel, _presented_flatten(panel.actions)))
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


async def run_cohort_flatten(
    broker: str,
    account_id: str,
    request: CohortFlattenRequest,
    *,
    operator_identity: str,
) -> CohortActionResult:
    """Execute the named flatten legs under the shared cohort batch contract."""
    resolved = await validate_account(broker, account_id)
    legs = await execute_cohort_legs(
        broker,
        resolved,
        legs=[
            CohortLegCommand(
                strategy_instance_id=leg.strategy_instance_id,
                action_id=leg.action_id,
                revision=leg.revision,
                concurrency_token=leg.concurrency_token,
            )
            for leg in request.legs
        ],
        idempotency_key=request.idempotency_key,
        reason=request.reason,
        operator_identity=operator_identity,
        telemetry_kind="flatten",
    )
    return CohortActionResult(
        account_id=resolved,
        receipt_id=request.idempotency_key,
        recorded_at_ms=now_ms_utc(),
        legs=legs,
        **count_outcomes(legs),
    )
