"""Source gathering and semantic composition for one Bot Cockpit surface.

The router wires existing source readers into this service. ``SurfaceHub`` owns
cadence, versioning, and lifecycle; this service owns what one status document
means. Keeping those responsibilities here prevents HTTP reads from becoming
the composition boundary again.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from app.schemas.live_runs import EvidenceBinding, LiveInstanceStatus
from app.services.account_fleet_read_context import (
    AccountFleetReadContext,
    build_account_fleet_read_contexts,
)
from app.services.bot_daily_lifecycle import BotDailyLifecycleEvidence
from app.services.bot_lifecycle_receipts import LifecycleReceiptContext

VisibleRuns = dict[str, list[dict]]


class VisibleRunsSnapshotCache:
    """Coalesce fleet-wide run scans shared by every per-bot assembler."""

    def __init__(self, *, ttl_seconds: float = 0.9) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[Path, tuple[float, VisibleRuns]] = {}
        self._lock = asyncio.Lock()

    async def get(
        self,
        root: Path,
        loader: Callable[[Path], VisibleRuns],
    ) -> VisibleRuns:
        key = root.resolve(strict=False)
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        async with self._lock:
            now = time.monotonic()
            cached = self._entries.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
            snapshot = await asyncio.to_thread(loader, root)
            completed_at = time.monotonic()
            self._entries = {
                entry_root: entry for entry_root, entry in self._entries.items() if entry[0] > completed_at
            }
            self._entries[key] = (
                completed_at + self._ttl_seconds,
                snapshot,
            )
            return snapshot

    async def invalidate(self, root: Path | None = None) -> None:
        async with self._lock:
            if root is None:
                self._entries.clear()
            else:
                self._entries.pop(root.resolve(strict=False), None)


@dataclass(frozen=True, slots=True)
class LiveInstanceSurfaceDependencies:
    """Explicit boundary around the legacy source readers being composed."""

    get_settings: Callable[..., Any]
    visible_runs_by_instance: Callable[..., Any]
    sid_has_soft_deletion: Callable[..., Any]
    bot_soft_deleted_detail: Callable[..., Any]
    fetch_instance_process: Callable[..., Any]
    interpret_daemon_process: Callable[..., Any]
    scan_runs_by_instance: Callable[..., Any]
    resolve_account_freeze: Callable[..., Any]
    resolve_desired_state: Callable[..., Any]
    strategy_state: Callable[..., Any]
    instance_last_exit: Callable[..., Any]
    resolve_readiness: Callable[..., Any]
    resolve_readonly_default: Callable[..., Any]
    instance_broker: Callable[..., Any]
    start_defaults: Callable[..., Any]
    sizing: Callable[..., Any]
    resolve_action_plan: Callable[..., Any]
    get_daemon_connectivity_monitor: Callable[..., Any]
    resolve_resume_guard_state_for: Callable[..., Any]
    now_ms: Callable[..., Any]
    resolve_engine_runtime_snapshot_and_freshness: Callable[..., Any]
    safety_verdict_final_from_engine_runtime: Callable[..., Any]
    resolve_safety_verdict_final: Callable[..., Any]
    broker_connection_state_from_engine_runtime: Callable[..., Any]
    broker_connection_state_from_readiness: Callable[..., Any]
    instance_ledger_account_id: Callable[..., Any]
    crash_recovery_gate_for_instance: Callable[..., Any]
    resolve_account_clerk_surface: Callable[..., Any]
    resolve_account_observation_surface: Callable[..., Any]
    get_account_truth_snapshot_provider: Callable[..., Any]
    resolve_latest_mutation: Callable[..., Any]
    resolve_broker_observation_consistency: Callable[..., Any]
    resolve_reconciliation_inputs: Callable[..., Any]
    resolve_activity_publisher_for_status: Callable[..., Any]
    resolve_daemon_diagnostic_condition_for_status: Callable[..., Any]
    resolve_incident_headline: Callable[..., Any]
    resolve_bot_lifecycle_state: Callable[..., Any]
    resolve_live_run_dir: Callable[..., Any]
    compute_operator_surface: Callable[..., Any]
    resolve_durable_control_write_failure_for_status: Callable[..., Any]
    resolve_start_run_id: Callable[..., Any]
    active_roll_call_offer: Callable[..., Any]
    lifecycle_conditions_for_instance: Callable[..., Any]
    project_bot_daily_lifecycle: Callable[..., Any]
    provenance: Callable[..., Any]
    resolve_symbol: Callable[..., Any]
    resolve_session_capability_for_symbol: Callable[..., Any]
    resolve_instrument_surface: Callable[..., Any]
    resolve_lineage: Callable[..., Any]
    read_instance_live_state: Callable[..., Any]
    session_started_at_ms: Callable[..., Any]
    project_intent_events: Callable[..., Any]
    project_instance_account_lifecycle_events: Callable[..., Any]
    sort_lifecycle_events: Callable[..., Any]
    compose_bot_lifecycle_chart: Callable[..., Any]


class LiveInstanceSurfaceAssembler:
    """Gather every source and compose one semantic status document."""

    def __init__(self, dependencies: LiveInstanceSurfaceDependencies) -> None:
        self._d = dependencies

    async def assemble(self, strategy_instance_id: str) -> LiveInstanceStatus:
        d = self._d
        settings = d.get_settings()
        root = Path(settings.live_runs_root)
        runs_by_instance = await d.visible_runs_by_instance(root)
        if strategy_instance_id not in runs_by_instance and d.sid_has_soft_deletion(
            root.parent,
            strategy_instance_id,
        ):
            raise HTTPException(
                status.HTTP_410_GONE,
                detail=d.bot_soft_deleted_detail(strategy_instance_id),
            )
        _result, daemon = await d.fetch_instance_process(
            settings.live_runner_daemon_url,
            strategy_instance_id,
        )
        return await self.assemble_from_process(
            strategy_instance_id,
            root,
            settings,
            daemon,
            runs_by_instance=runs_by_instance,
        )

    async def assemble_from_process(
        self,
        sid: str,
        root: Path,
        settings: Any,
        daemon_process: dict | None,
        *,
        runs_by_instance: dict[str, list[dict]] | None = None,
        account_fleet_read_context: AccountFleetReadContext | None = None,
    ) -> LiveInstanceStatus:
        d = self._d
        process, live_binding = d.interpret_daemon_process(daemon_process, root)

        if runs_by_instance is None:
            runs_by_instance = d.scan_runs_by_instance(root)
        runs = runs_by_instance.get(sid, [])
        account_freeze = d.resolve_account_freeze(root.parent, runs)
        evidence = EvidenceBinding(run_id=runs[0]["run_id"]) if runs else None
        desired = d.resolve_desired_state(root, sid)
        latest_decision, latest_signal_tone, decision_columns = d.strategy_state(
            root,
            live_binding,
            runs,
        )
        last_exit = d.instance_last_exit(runs)
        readiness = d.resolve_readiness(root, live_binding, runs, desired.state)
        raw_mode = getattr(settings, "mode", None)
        configured_mode = raw_mode if raw_mode in ("paper", "live") else None
        broker_view = d.instance_broker(root, sid)
        start_defaults = d.start_defaults(
            root,
            live_binding,
            runs,
            readonly_default=d.resolve_readonly_default(settings),
        )
        sizing = d.sizing(root, live_binding, runs, sid)
        action_plan = d.resolve_action_plan(root, live_binding, runs)
        poisoned = bool(last_exit and last_exit.halt_trigger is not None)
        daemon_monitor = d.get_daemon_connectivity_monitor()
        control_plane_state = daemon_monitor.state if daemon_monitor is not None else None
        guard_state = d.resolve_resume_guard_state_for(root, live_binding, runs)
        observed_at_ms = d.now_ms()
        runtime_snapshot, runtime_freshness = d.resolve_engine_runtime_snapshot_and_freshness(
            root,
            live_binding,
            now_ms=observed_at_ms,
        )
        fresh_runtime_snapshot = (
            runtime_snapshot if runtime_freshness is not None and runtime_freshness.broker.state == "FRESH" else None
        )
        safety_verdict_final = d.safety_verdict_final_from_engine_runtime(
            fresh_runtime_snapshot
        ) or d.resolve_safety_verdict_final(configured_mode)
        broker_connection_state = d.broker_connection_state_from_engine_runtime(
            fresh_runtime_snapshot
        ) or d.broker_connection_state_from_readiness(readiness)
        instance_account_id = d.instance_ledger_account_id(
            root,
            sid,
            runs_by_instance=runs_by_instance,
        )
        crash_recovery_gate = d.crash_recovery_gate_for_instance(
            root.parent,
            account_id=instance_account_id,
            strategy_instance_id=sid,
        )
        account_clerk = d.resolve_account_clerk_surface(
            root.parent,
            instance_account_id,
            now_ms=observed_at_ms,
        )
        account_observation = d.resolve_account_observation_surface(
            root.parent,
            instance_account_id,
            now_ms=observed_at_ms,
        )
        if account_fleet_read_context is None:
            account_fleet_read_context = (
                await asyncio.to_thread(
                    build_account_fleet_read_contexts,
                    root,
                    [instance_account_id],
                    snapshot_provider=d.get_account_truth_snapshot_provider(),
                    observed_at_ms=observed_at_ms,
                )
            ).get(instance_account_id)
        account_truth_snapshot = (
            account_fleet_read_context.account_truth_evidence
            if account_fleet_read_context is not None
            else None
        )
        latest_mutation = d.resolve_latest_mutation(root, sid)
        broker_observation_consistency = d.resolve_broker_observation_consistency(
            live_binding,
            runtime_snapshot=runtime_snapshot,
            configured_mode=configured_mode,
            now_ms=observed_at_ms,
        )
        (
            reconciliation_receipt,
            current_wal_seq,
            current_run_id,
            current_namespace,
            intent_wal_events,
        ) = d.resolve_reconciliation_inputs(root, live_binding)
        activity_publisher, activity_publisher_registered_at_ms = await d.resolve_activity_publisher_for_status(
            sid, live_binding
        )
        if account_fleet_read_context is not None:
            fleet_blocks_starts = account_fleet_read_context.fleet_blocks_starts
        else:
            fleet_blocks_starts = True
        daemon_diagnostic_condition = await d.resolve_daemon_diagnostic_condition_for_status(sid)
        incident_headline = d.resolve_incident_headline(root, live_binding, runs)
        lifecycle_state = d.resolve_bot_lifecycle_state(root, sid)
        live_run_dir = d.resolve_live_run_dir(root, live_binding)
        symbol = d.resolve_symbol(root, live_binding, runs)
        session_capability = d.resolve_session_capability_for_symbol(symbol)
        operator_surface = d.compute_operator_surface(
            process=process,
            last_exit=last_exit,
            safety_verdict_final=safety_verdict_final,
            broker_connection_state=broker_connection_state,
            broker=broker_view,
            readiness=readiness,
            action_plan=action_plan,
            start_defaults=start_defaults,
            sizing=sizing,
            instance_broker_self_consistent=None,
            live_binding=live_binding,
            poisoned=poisoned,
            bot_lifecycle_phase=(lifecycle_state.phase if lifecycle_state is not None else None),
            desired_state=desired,
            guard_state=guard_state,
            runtime_freshness=runtime_freshness,
            control_plane_state=control_plane_state,
            latest_mutation=latest_mutation,
            broker_observation_consistency=broker_observation_consistency,
            account_truth_snapshot=account_truth_snapshot,
            precomputed_account_truth_assessment=(
                account_fleet_read_context.account_truth_assessment
                if account_fleet_read_context is not None
                else None
            ),
            fleet_blocks_starts=fleet_blocks_starts,
            daemon_diagnostic_condition=daemon_diagnostic_condition,
            durable_control_write_failure=d.resolve_durable_control_write_failure_for_status(
                root,
                live_binding,
                runs,
            ),
            host_start_command=settings.live_runner_host_start_command,
            start_run_id=d.resolve_start_run_id(root, live_binding, runs),
            account_freeze=account_freeze,
            crash_recovery_gate=crash_recovery_gate,
            account_clerk=account_clerk,
            account_observation=account_observation,
            account_gate_authority=getattr(settings, "account_gate_authority", "account_truth"),
            reconciliation_receipt=reconciliation_receipt,
            current_wal_seq=current_wal_seq,
            current_run_id=current_run_id,
            current_namespace=current_namespace,
            latest_broker_event_ms=None,
            latest_mutation_ms=(latest_mutation.last_transition_at_ms if latest_mutation is not None else None),
            reconciliation_ttl_ms=getattr(settings, "reconciliation_receipt_ttl_ms", None),
            activity_publisher=activity_publisher,
            activity_publisher_registered_at_ms=activity_publisher_registered_at_ms,
            incident_headline_notice=incident_headline,
            session_capability=session_capability,
            now_ms=observed_at_ms,
        )
        roll_call_offer = d.active_roll_call_offer(root, sid, now_ms=observed_at_ms)
        lifecycle_conditions = d.lifecycle_conditions_for_instance(
            root,
            account_id=instance_account_id,
            sid=sid,
            account_freeze=account_freeze,
            incident_headline_notice=incident_headline,
            now_ms=observed_at_ms,
        )
        daily_lifecycle = d.project_bot_daily_lifecycle(
            BotDailyLifecycleEvidence(
                strategy_instance_id=sid,
                process=process,
                start_capability=operator_surface.host_process.start_capability,
                latest_run_id=runs[0]["run_id"] if runs else None,
                active_run_id=live_binding.run_id if live_binding is not None else None,
                persisted_state=lifecycle_state,
                roll_call_offer=roll_call_offer,
                conditions=tuple(lifecycle_conditions),
                now_ms=observed_at_ms,
            )
        )
        redeploy_available = bool(
            start_defaults is not None
            and start_defaults.strategy_spec_path
            and start_defaults.qc_audit_copy_path
            and start_defaults.qc_cloud_backtest_id
        )
        provenance = d.provenance(root, live_binding, runs)
        instrument_surface = d.resolve_instrument_surface(root, live_binding, runs)
        lineage = d.resolve_lineage(root, live_binding, runs)
        receipt_context = LifecycleReceiptContext(
            symbol=symbol,
            action_plan=action_plan,
            instrument_surface=instrument_surface,
            start_defaults=start_defaults,
            provenance=provenance,
            sizing=sizing,
            last_exit=last_exit,
        )
        live_state = d.read_instance_live_state(root, sid)
        intent_projection_since_ms = d.session_started_at_ms(process, live_binding)
        lifecycle_events = d.project_intent_events(
            intent_wal_events,
            bot_id=sid,
            account_id=instance_account_id,
            run_id=current_run_id,
            wal_path=(live_run_dir / "intent_events.jsonl" if live_run_dir is not None else None),
            since_ms=intent_projection_since_ms,
            live_state_last_intent_wal_seq=(live_state.last_intent_wal_seq if live_state is not None else None),
        )
        lifecycle_events = d.sort_lifecycle_events(
            [
                *lifecycle_events,
                *d.project_instance_account_lifecycle_events(
                    root.parent,
                    account_id=instance_account_id,
                    sid=sid,
                    run_id=current_run_id,
                    bot_order_namespace=current_namespace,
                ),
            ]
        )

        return LiveInstanceStatus(
            strategy_instance_id=sid,
            process=process,
            live_binding=live_binding,
            evidence_binding=evidence,
            latest_mutation=(latest_mutation.model_dump(mode="json") if latest_mutation is not None else None),
            desired_state=desired,
            readiness=readiness,
            latest_decision=latest_decision,
            latest_signal_tone=latest_signal_tone,
            decision_columns=decision_columns,
            broker=broker_view,
            start_defaults=start_defaults,
            provenance=provenance,
            sizing=sizing,
            last_exit=last_exit,
            symbol=symbol,
            action_plan=action_plan,
            instrument_surface=instrument_surface,
            lineage=lineage,
            operator_surface=operator_surface,
            lifecycle_chart=d.compose_bot_lifecycle_chart(
                sid,
                operator_surface,
                desired_state=desired,
                redeploy_available=redeploy_available,
                lifecycle_events=lifecycle_events,
                receipt_context=receipt_context,
            ),
            daily_lifecycle=daily_lifecycle,
            fetched_at_ms=observed_at_ms,
        )


__all__ = [
    "LiveInstanceSurfaceAssembler",
    "LiveInstanceSurfaceDependencies",
    "VisibleRunsSnapshotCache",
]
