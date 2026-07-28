"""Durable end-of-day intent with best-effort live clock-out.

The operator's safe choice must survive a control-plane outage.  This service
therefore writes PAUSED intent before it reads the host daemon, and treats a
failed read or command enqueue as a pending effect rather than a failed
operator action.  It does not own mutation-attempt receipts or lifecycle
state outside the canonical :class:`BotLifecycleEvaluator`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.engine.live import host_daemon_client
from app.engine.live.bot_lifecycle_evaluator import BotLifecycleEvaluator
from app.engine.live.command_channel import CommandChannel, CommandVerb
from app.engine.live.desired_state import DesiredState
from app.schemas.live_runs import (
    DesiredStateRecordResponse,
    EndDayIntentResponse,
    InstanceProcessView,
    IntentActuation,
    LiveBinding,
    MutationRungReceipt,
)
from app.services.mutation_attempt import MutationAttemptScope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EndDayIntentDispatch:
    """Durable intent and the separately observed (or pending) clock-out."""

    durable: DesiredStateRecordResponse
    actuation: IntentActuation
    process: InstanceProcessView
    daemon_process: dict[str, Any] | None
    command_id: str | None
    stop_outcome: str


async def persist_end_day_intent(
    *,
    artifacts_root: Path,
    strategy_instance_id: str,
    idempotency_key: str | None,
    now_ms: Callable[[], int],
    daemon_url: str,
    interpret_process: Callable[[dict[str, Any] | None], tuple[InstanceProcessView, LiveBinding | None]],
    visible_live_run_dir: Callable[[LiveBinding], Path | None],
    clock_out_is_in_progress: Callable[[Path], bool],
) -> EndDayIntentDispatch:
    """Persist PAUSED and queue CLOCK_OUT only while the runner is observable."""

    disposition = BotLifecycleEvaluator(artifacts_root, strategy_instance_id).set_desired_state(
        DesiredState.PAUSED,
        updated_by="operator",
        reason="end-day-now",
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
            "durable end-day intent could not read the host daemon",
            extra={"strategy_instance_id": strategy_instance_id, "exception": repr(exc)},
        )
        return EndDayIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=False,
                detail="durable end-day intent accepted; host daemon observation is pending",
            ),
            process=InstanceProcessView(state="unreachable"),
            daemon_process=None,
            command_id=None,
            stop_outcome="durable_intent_pending",
        )

    process, live_binding = interpret_process(daemon_process)
    if disposition.replayed:
        return EndDayIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=False,
                run_id=None if live_binding is None else live_binding.run_id,
                detail="durable end-day intent already accepted; runtime effect remains subject to reconciliation",
            ),
            process=process,
            daemon_process=daemon_process,
            command_id=None,
            stop_outcome="durable_intent_replayed",
        )
    if live_binding is None:
        return EndDayIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=False,
                detail="durable end-day intent accepted; host daemon has no live binding",
            ),
            process=process,
            daemon_process=daemon_process,
            command_id=None,
            stop_outcome="durable_intent_pending",
        )
    live_run_dir = visible_live_run_dir(live_binding)
    if live_run_dir is None:
        return EndDayIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=False,
                run_id=live_binding.run_id,
                detail="durable end-day intent accepted; the bound run is not locally visible for clock-out",
            ),
            process=process,
            daemon_process=daemon_process,
            command_id=None,
            stop_outcome="durable_intent_pending",
        )
    if clock_out_is_in_progress(live_run_dir):
        return EndDayIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=True,
                run_id=live_binding.run_id,
                detail="clock-out was already queued; awaiting runtime acknowledgement",
            ),
            process=process,
            daemon_process=daemon_process,
            command_id=f"clock-out-{live_binding.run_id}-in-progress",
            stop_outcome="clock_out_already_queued",
        )
    try:
        command = CommandChannel(live_run_dir / "commands").write_from_operator(CommandVerb.CLOCK_OUT)
    except OSError as exc:
        logger.warning(
            "durable end-day intent could not enqueue a live clock-out command",
            extra={
                "strategy_instance_id": strategy_instance_id,
                "run_id": live_binding.run_id,
                "exception": repr(exc),
            },
        )
        return EndDayIntentDispatch(
            durable=durable,
            actuation=IntentActuation(
                actuated=False,
                run_id=live_binding.run_id,
                detail="durable end-day intent accepted; live clock-out enqueue is pending reconciliation",
            ),
            process=process,
            daemon_process=daemon_process,
            command_id=None,
            stop_outcome="durable_intent_pending",
        )
    return EndDayIntentDispatch(
        durable=durable,
        actuation=IntentActuation(
            actuated=True,
            run_id=live_binding.run_id,
            command_seq=command.seq,
            detail=f"CLOCK_OUT queued on {live_binding.run_id}; awaiting acknowledgement",
        ),
        process=process,
        daemon_process=daemon_process,
        command_id=f"clock-out-{live_binding.run_id}-{command.seq}",
        stop_outcome="clock_out_queued",
    )


async def persist_end_day_intent_response(
    *,
    mutation_scope: MutationAttemptScope,
    rung_receipts: Callable[
        [dict[str, Any] | None], Awaitable[tuple[MutationRungReceipt, list[MutationRungReceipt]]]
    ],
    artifacts_root: Path,
    strategy_instance_id: str,
    idempotency_key: str | None,
    now_ms: Callable[[], int],
    daemon_url: str,
    interpret_process: Callable[[dict[str, Any] | None], tuple[InstanceProcessView, LiveBinding | None]],
    visible_live_run_dir: Callable[[LiveBinding], Path | None],
    clock_out_is_in_progress: Callable[[Path], bool],
) -> EndDayIntentResponse:
    """Wrap end-day intent and its observed effect in one mutation receipt."""

    with mutation_scope:
        mutation_scope.stage = "durable_end_day_intent"
        dispatch = await persist_end_day_intent(
            artifacts_root=artifacts_root,
            strategy_instance_id=strategy_instance_id,
            idempotency_key=idempotency_key,
            now_ms=now_ms,
            daemon_url=daemon_url,
            interpret_process=interpret_process,
            visible_live_run_dir=visible_live_run_dir,
            clock_out_is_in_progress=clock_out_is_in_progress,
        )
        mutation_scope.stage = "end_day_response_assembly"
        receipt, warnings = await rung_receipts(dispatch.daemon_process)
        confirmed = mutation_scope.confirm(
            outcome={
                "durable_state": dispatch.durable.state,
                "actuated": dispatch.actuation.actuated,
                "run_id": dispatch.actuation.run_id,
                "command_seq": dispatch.actuation.command_seq,
            },
        )
        return EndDayIntentResponse(
            durable=dispatch.durable,
            actuation=dispatch.actuation,
            process=dispatch.process,
            command_id=dispatch.command_id,
            stop_outcome=dispatch.stop_outcome,
            rung_receipt=receipt,
            rung_receipt_warnings=warnings,
            mutation_attempt_id=confirmed.mutation_attempt_id,
            mutation_dispatch_state=confirmed.dispatch_state,
        )
