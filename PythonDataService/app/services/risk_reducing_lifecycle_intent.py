"""Outage-safe durable dispatch for Pause and Stop.

Safe controls must be able to record their durable intent while the daemon is
unreachable.  The canonical lifecycle evaluator owns the write; this service
only makes live command enqueue a best-effort, separately reported effect.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.engine.live import host_daemon_client
from app.engine.live.bot_lifecycle_evaluator import (
    BotLifecycleEvaluator,
    LifecycleTransitionRefusedError,
)
from app.engine.live.command_channel import CommandChannel, CommandVerb
from app.engine.live.desired_state import DesiredState
from app.schemas.live_runs import (
    DesiredStateRecordResponse,
    IntentActuation,
    LiveBinding,
    MutationRungReceipt,
    SetInstanceDesiredStateResponse,
)
from app.services.mutation_attempt import MutationAttemptScope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskReducingIntentDispatch:
    """One durable safe intent and its independently observable effect."""

    durable: DesiredStateRecordResponse
    actuation: IntentActuation
    daemon_process: dict[str, Any] | None
    replayed: bool


class RiskReducingIntentRefusedError(RuntimeError):
    """A lifecycle refusal known to have happened before live actuation.

    This differs from an unavailable daemon response: the evaluator refused
    locally and deterministically, so the mutation receipt must record a
    confirmed non-effect rather than incorrectly forcing the operator into an
    outcome-unknown reconciliation path.
    """

    def __init__(self, *, refusal_code: str, mutation_attempt_id: str, dispatch_state: str) -> None:
        super().__init__(f"durable lifecycle intent refused: {refusal_code}")
        self.refusal_code = refusal_code
        self.mutation_attempt_id = mutation_attempt_id
        self.dispatch_state = dispatch_state


async def persist_risk_reducing_intent(
    *,
    artifacts_root: Path,
    strategy_instance_id: str,
    desired_state: DesiredState,
    command_verb: CommandVerb,
    updated_by: str,
    reason: str | None,
    idempotency_key: str | None,
    now_ms: Callable[[], int],
    daemon_url: str,
    live_binding_from_process: Callable[[dict[str, Any] | None], LiveBinding | None],
    visible_live_run_dir: Callable[[LiveBinding], Path | None],
) -> RiskReducingIntentDispatch:
    """Write first, then enqueue only if this request can see a live runner."""

    disposition = BotLifecycleEvaluator(artifacts_root, strategy_instance_id).set_desired_state(
        desired_state,
        updated_by=updated_by,
        reason=reason,
        now_ms=now_ms(),
        idempotency_key=idempotency_key,
    )
    record = disposition.desired_state
    if record is None:
        raise OSError("lifecycle evaluator did not return a desired-state record")
    durable = DesiredStateRecordResponse(
        state=record.desired_state.value,
        updated_at_ms=record.updated_at_ms,
        updated_by=record.updated_by,
        reason=record.reason,
        version=record.version,
    )

    try:
        _result, daemon_process = await host_daemon_client.fetch_instance_process(
            daemon_url,
            strategy_instance_id,
        )
    except (host_daemon_client.HostDaemonError, host_daemon_client.HostDaemonOutcomeUnknownError) as exc:
        logger.warning(
            "durable lifecycle intent could not read the host daemon",
            extra={"strategy_instance_id": strategy_instance_id, "exception": repr(exc)},
        )
        return RiskReducingIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=False,
                detail="durable intent accepted; host daemon observation is pending",
            ),
            daemon_process=None,
            replayed=disposition.replayed,
        )

    live_binding = live_binding_from_process(daemon_process)
    if disposition.replayed:
        return RiskReducingIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=False,
                run_id=None if live_binding is None else live_binding.run_id,
                detail="durable intent already accepted; runtime effect remains subject to reconciliation",
            ),
            daemon_process=daemon_process,
            replayed=True,
        )
    if live_binding is None:
        return RiskReducingIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=False,
                detail="durable only; will gate the next start",
            ),
            daemon_process=daemon_process,
            replayed=False,
        )
    live_run_dir = visible_live_run_dir(live_binding)
    if live_run_dir is None:
        return RiskReducingIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=False,
                run_id=live_binding.run_id,
                detail=f"durable only; bound run {live_binding.run_id} is not visible locally",
            ),
            daemon_process=daemon_process,
            replayed=False,
        )
    try:
        command = CommandChannel(live_run_dir / "commands").write_from_operator(command_verb)
    except OSError as exc:
        logger.warning(
            "durable lifecycle intent could not enqueue a live command",
            extra={
                "strategy_instance_id": strategy_instance_id,
                "run_id": live_binding.run_id,
                "command_verb": command_verb.value,
                "exception": repr(exc),
            },
        )
        return RiskReducingIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=False,
                run_id=live_binding.run_id,
                detail="durable intent accepted; live command enqueue is pending reconciliation",
            ),
            daemon_process=daemon_process,
            replayed=False,
        )
    return RiskReducingIntentDispatch(
        durable=durable,
        actuation=IntentActuation(
            actuated=True,
            run_id=live_binding.run_id,
            command_seq=command.seq,
            detail=f"{command_verb.value} queued on {live_binding.run_id}; awaiting ack",
        ),
        daemon_process=daemon_process,
        replayed=False,
    )


async def persist_risk_reducing_intent_response(
    *,
    mutation_scope: MutationAttemptScope,
    rung_receipts: Callable[
        [dict[str, Any] | None], Awaitable[tuple[MutationRungReceipt, list[MutationRungReceipt]]]
    ],
    artifacts_root: Path,
    strategy_instance_id: str,
    desired_state: DesiredState,
    command_verb: CommandVerb,
    updated_by: str,
    reason: str | None,
    idempotency_key: str | None,
    now_ms: Callable[[], int],
    daemon_url: str,
    live_binding_from_process: Callable[[dict[str, Any] | None], LiveBinding | None],
    visible_live_run_dir: Callable[[LiveBinding], Path | None],
) -> SetInstanceDesiredStateResponse:
    """Wrap one safe intent in its one durable mutation-attempt receipt."""

    with mutation_scope:
        mutation_scope.stage = "durable_intent_write"
        try:
            dispatch = await persist_risk_reducing_intent(
                artifacts_root=artifacts_root,
                strategy_instance_id=strategy_instance_id,
                desired_state=desired_state,
                command_verb=command_verb,
                updated_by=updated_by,
                reason=reason,
                idempotency_key=idempotency_key,
                now_ms=now_ms,
                daemon_url=daemon_url,
                live_binding_from_process=live_binding_from_process,
                visible_live_run_dir=visible_live_run_dir,
            )
        except LifecycleTransitionRefusedError as exc:
            refused = mutation_scope.reject_not_observed(
                outcome={
                    "reason_code": "LIFECYCLE_TRANSITION_REFUSED",
                    "refusal_code": str(exc),
                },
            )
            raise RiskReducingIntentRefusedError(
                refusal_code=str(exc),
                mutation_attempt_id=refused.mutation_attempt_id,
                dispatch_state=refused.dispatch_state,
            ) from exc
        mutation_scope.stage = "receipt_assembly"
        receipt, warnings = await rung_receipts(dispatch.daemon_process)
        confirmed = mutation_scope.confirm(
            outcome={
                "durable_state": dispatch.durable.state,
                "actuated": dispatch.actuation.actuated,
                "run_id": dispatch.actuation.run_id,
                "command_seq": dispatch.actuation.command_seq,
            },
        )
        return SetInstanceDesiredStateResponse(
            durable=dispatch.durable,
            actuation=dispatch.actuation,
            rung_receipt=receipt,
            rung_receipt_warnings=warnings,
            mutation_attempt_id=confirmed.mutation_attempt_id,
            mutation_dispatch_state=confirmed.dispatch_state,
        )
