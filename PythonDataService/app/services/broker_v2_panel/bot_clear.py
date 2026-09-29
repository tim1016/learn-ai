"""Clear finished bots from Home: N unchanged per-bot archive legs.

Owner decision 2026-09-28 (PRD #2560, #2567): the Finished fold clears the
bots the owner ticks. Clearing is ADR 0052's ``archive`` and nothing more:
the bot leaves Home and the catalog poll, and every record it wrote -- its
runs, fills, fees and budget rows -- stays in custody, readable by id.

Orchestration only, in ADR 0051's shape (ADR 0052 §4):

* **Explicit membership.** Exactly the named bots; nothing is inferred.
* **Each leg is the unchanged per-bot archive**, run through the shared batch
  executor (``cohort_execution``) under ``{idempotency_key}:{sid}``, so a
  resend replays cleared legs as no-ops. Its concurrency token is the one its
  own panel presents, read when the leg is prepared; ``bot_runner.archive``
  then re-answers the guard against fresh custody under the bot's lock before
  it writes ``STRATEGY_INSTANCE_RETIRED`` (ADR 0052 §3). A running or holding
  bot, or one whose fill landed after the owner looked, is refused with the
  guard's own reason and code; its siblings still clear.
* **One account reconciliation per batch.** Fresh custody is the batch's one
  pass, not one per leg: per leg, clearing 150 bots read Alpaca 600 times
  against its 200-a-minute limit. Each leg's guard is still answered under
  its lock, against that pass wherever it still proves the bot's custody
  (``bot_runner._archive_custody`` says when), and against a pass of its own
  otherwise.

There is no presentation read. The Finished rows the owner chose from are the
catalog's, and what each leg may do is decided per leg at execution, where the
guard's answer is fresh -- a presentation fetched first would only be older.
"""

from __future__ import annotations

import logging

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.models import ReconciliationCut
from app.schemas.broker_v2_panel import (
    BotClearRequest,
    CohortActionResult,
    CohortLegResult,
    PanelActionErrorResponse,
)
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.cohort_execution import (
    CohortLegCommand,
    count_outcomes,
    execute_cohort_legs,
)
from app.services.broker_v2_panel.panel_errors import PanelDataError
from app.services.broker_v2_panel.panel_scope import validate_account
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: The one per-bot action a clear leg runs. Fixed here, never taken from the
#: request, so this endpoint cannot be steered to a different mutation.
CLEAR_ACTION_ID = "archive"


def _refused(sid: str, *, message: str, why: str | None, reason_code: str | None) -> CohortLegResult:
    """A leg refused before it ran: nothing was attempted, so a resend may retry it."""
    return CohortLegResult(
        strategy_instance_id=sid,
        outcome="refused",
        result=None,
        error=PanelActionErrorResponse(
            action_id=CLEAR_ACTION_ID,
            outcome="conflict",
            receipt_id=None,
            recorded_at_ms=now_ms_utc(),
            message=message,
            why=why,
            reason_code=reason_code,
        ),
    )


async def _prepare(broker: str, account_id: str, sid: str) -> CohortLegCommand | CohortLegResult:
    """The leg's command from its own presented archive action, or its refusal."""
    try:
        panel = await panel_data_source.get_panel(broker, account_id, sid)
    except PanelDataError as error:
        return _refused(sid, message=str(error), why=error.detail, reason_code=None)
    action = next((candidate for candidate in panel.actions if candidate.action_id == CLEAR_ACTION_ID), None)
    if action is None:
        return _refused(
            sid,
            message="This bot cannot be cleared.",
            why="Its page offers no way to take it off Home.",
            reason_code="CLEAR_NOT_OFFERED",
        )
    # A disabled leg still runs through the per-bot pipeline: a resend of a
    # leg this key already cleared must replay as a no-op, and the pipeline
    # checks its receipt ledger before the guard's refusal.
    return CohortLegCommand(
        strategy_instance_id=sid,
        action_id=CLEAR_ACTION_ID,
        revision=action.revision,
        concurrency_token=action.concurrency_token,
    )


async def _reconcile_once() -> ReconciliationCut | None:
    """The batch's one account pass, or none -- then every leg reconciles for itself."""
    clerk = get_alpaca_clerk()
    if clerk is None:
        return None
    try:
        return await clerk.reconcile_through()
    except Exception:
        # Whatever stopped this pass stops each leg's own the same way, and
        # the batch owes every leg its own typed answer to it
        # (``cohort_execution``) -- never one error for the whole batch.
        logger.warning(
            "clear batch could not reconcile once; each leg reconciles for itself",
            exc_info=True,
            extra={"action": "bots_clear_batch_reconcile_failed"},
        )
        return None


async def clear_bots(
    broker: str,
    account_id: str,
    request: BotClearRequest,
    *,
    operator_identity: str,
) -> CohortActionResult:
    """Clear the named bots, one archive leg each, answering every leg in request order.

    A leg the account-scoped early exit never reached (``cohort_execution``)
    is absent from the answer and safe to resend under the same key.
    """
    resolved = await validate_account(broker, account_id)
    # Before the legs are prepared, so each presents from the fresh verdict.
    reconciled = await _reconcile_once()
    prepared = [await _prepare(broker, resolved, sid) for sid in request.strategy_instance_ids]
    commands = [leg for leg in prepared if isinstance(leg, CohortLegCommand)]
    executed = {
        leg.strategy_instance_id: leg
        for leg in (
            await execute_cohort_legs(
                broker,
                resolved,
                legs=commands,
                idempotency_key=request.idempotency_key,
                # The audit line on each retirement: why this registration ended.
                reason="Cleared from Home",
                operator_identity=operator_identity,
                telemetry_kind="clear",
                reconciled=reconciled,
            )
            if commands
            else []
        )
    }
    legs = [
        leg if isinstance(leg, CohortLegResult) else executed[leg.strategy_instance_id]
        for leg in prepared
        if isinstance(leg, CohortLegResult) or leg.strategy_instance_id in executed
    ]
    return CohortActionResult(
        account_id=resolved,
        receipt_id=request.idempotency_key,
        recorded_at_ms=now_ms_utc(),
        legs=legs,
        **count_outcomes(legs),
    )
