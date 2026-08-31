"""Cohort-scoped archive: N finished bots behind one affordance (ADR 0052).

#1911's motivating case is a profiling session that ended with 142 stopped,
flat bots and no sanctioned way to remove them. The per-bot ``archive``
action is that way; this is what stops it costing 142 clicks.

Orchestration only, exactly as ADR 0051 established for flatten: every leg
runs the unchanged per-bot pipeline through the shared batch executor, so
guards, tokens, idempotency, commit-time custody re-proof and per-bot
receipts are inherited rather than re-proven. Membership is explicit in the
request and never inferred at execution time.

Two deliberate differences from the flatten cohort, both because archive is
a roster-hygiene affordance rather than a response to a lockstep failure:

* **Single-member groups are presented.** A flatten cohort of one is just
  the per-bot action, so ADR 0051 hides it. Here the surface's job is to
  clear out the roster, and hiding a lone finished bot would leave it
  unreachable from the only screen built to remove it.
* **Retired rows are excluded, and so is every running bot.** They can never
  be archive legs, and the prefilter is what keeps this read from building a
  panel projection per roster row when only a handful are candidates.
"""

from __future__ import annotations

from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotPanelView,
    CohortActionResult,
    CohortArchiveCohort,
    CohortArchiveLeg,
    CohortArchiveRequest,
    CohortArchiveView,
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

#: The one action this surface composes. Fixed here rather than accepted from
#: the client so the endpoint cannot be steered to a different mutation.
ARCHIVE_ACTION_ID = "archive"


def _presented_archive(actions: list[PanelAction]) -> PanelAction | None:
    return next(
        (action for action in actions if action.action_id == ARCHIVE_ACTION_ID),
        None,
    )


def _leg_from_panel(panel: BotPanelView, action: PanelAction | None) -> CohortArchiveLeg:
    blocker_headline: str | None = None
    if action is not None and not action.enabled and action.blockers:
        blocker_headline = action.blockers[0].headline
    return CohortArchiveLeg(
        strategy_instance_id=panel.strategy_instance_id,
        enabled=action is not None and action.enabled,
        revision=None if action is None else action.revision,
        concurrency_token=None if action is None else action.concurrency_token,
        blocker_headline=blocker_headline,
    )


def _is_archive_candidate(row: BotCatalogView) -> bool:
    """Cheap catalog-level prefilter before the per-member panel reads.

    A running bot and an already-retired one can never be archive legs, and
    the guard would refuse them anyway. Skipping them here is what keeps this
    read proportional to the candidates rather than to the roster -- which
    matters most on exactly the oversized roster the affordance exists for.
    """
    return row.phase != "RETIRED" and not row.running


async def get_cohort_archive_view(broker: str, account_id: str) -> CohortArchiveView:
    """Group archivable members by (strategy_key, symbol); present leg facts."""
    resolved = await validate_account(broker, account_id)
    rows = await panel_data_source.get_catalog(broker, resolved)
    groups: dict[tuple[str, str], list[BotCatalogView]] = {}
    for row in rows:
        if not _is_archive_candidate(row):
            continue
        groups.setdefault((row.strategy_key, row.symbol), []).append(row)

    cohorts: list[CohortArchiveCohort] = []
    for (strategy_key, symbol), members in sorted(groups.items()):
        legs: list[CohortArchiveLeg] = []
        for row in sorted(members, key=lambda member: member.strategy_instance_id):
            panel = await panel_data_source.get_panel(
                broker, resolved, row.strategy_instance_id
            )
            legs.append(_leg_from_panel(panel, _presented_archive(panel.actions)))
        cohorts.append(
            CohortArchiveCohort(
                strategy_key=strategy_key,
                strategy_label=members[0].strategy_label,
                symbol=symbol,
                legs=legs,
                enabled_count=sum(1 for leg in legs if leg.enabled),
            )
        )
    return CohortArchiveView(
        account_id=resolved,
        cohorts=cohorts,
        observed_at_ms=now_ms_utc(),
    )


async def run_cohort_archive(
    broker: str,
    account_id: str,
    request: CohortArchiveRequest,
    *,
    operator_identity: str,
) -> CohortActionResult:
    """Execute the named archive legs under the shared cohort batch contract."""
    resolved = await validate_account(broker, account_id)
    legs = await execute_cohort_legs(
        broker,
        resolved,
        legs=[
            CohortLegCommand(
                strategy_instance_id=leg.strategy_instance_id,
                action_id=ARCHIVE_ACTION_ID,
                revision=leg.revision,
                concurrency_token=leg.concurrency_token,
            )
            for leg in request.legs
        ],
        idempotency_key=request.idempotency_key,
        reason=request.reason,
        operator_identity=operator_identity,
        telemetry_kind="archive",
    )
    return CohortActionResult(
        account_id=resolved,
        receipt_id=request.idempotency_key,
        recorded_at_ms=now_ms_utc(),
        legs=legs,
        **count_outcomes(legs),
    )
