# Live-control-plane domain map

**Baseline:** `6ebc34d3c` (2026-07-19 local checkout) contained these 171 top-level functions. The line values are the current working locations after the diagnostics pilot; the baseline inventory deliberately remains stable when a pilot moves implementation details. `Services / modules` records direct imported call boundaries only, not transitive calls. `Shared state` records the five mutable module singletons from the issue.

Run this check after any map update:

```sh
python3 - <<'PY'
import ast
from pathlib import Path
tree = ast.parse(Path('PythonDataService/app/routers/live_instances.py').read_text())
print(sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in tree.body))
PY
```

The checked-in baseline must yield `171`. Every row below belongs to exactly one domain; cross-domain callers are noted through the direct dependency column rather than duplicating ownership.

| Current line | Function | Domain | Services / modules touched | Shared state | Planned owner |
|---:|---|---|---|---|---|
| 357 | `_cohort_launch_lock` | deployment / launch | asyncio | _cohort_launch_locks | future LiveInstanceDeploymentService |
| 389 | `_validate_instance_id` | instance status | routers.live_runs._validate_path_segment<br>fastapi.HTTPException | — | future LiveInstanceSurfaceSources |
| 410 | `_run_is_soft_deleted` | instance status | services.bot_deletion.bot_run_is_soft_deleted | — | future LiveInstanceSurfaceSources |
| 421 | `_sid_has_soft_deletion` | instance status | services.bot_deletion.bot_has_soft_deletion | — | future LiveInstanceSurfaceSources |
| 432 | `_sid_has_soft_deletion_from_directory` | instance status | os | — | future LiveInstanceSurfaceSources |
| 461 | `_resolve_bot_lifecycle_state` | instance status | engine.live.bot_lifecycle_state.stable_bot_lifecycle_state_path<br>engine.live.bot_lifecycle_state.BotLifecycleStateRepo | — | future LiveInstanceSurfaceSources |
| 476 | `_bot_lifecycle_state_repo` | instance status | engine.live.bot_lifecycle_state.BotLifecycleStateRepo<br>engine.live.bot_lifecycle_state.stable_bot_lifecycle_state_path<br>fastapi.HTTPException | — | future LiveInstanceSurfaceSources |
| 484 | `_active_roll_call_offer` | instance status | services.bot_roll_call.active_roll_call_offer | — | future LiveInstanceSurfaceSources |
| 495 | `_daily_lifecycle_mutation_response` | lifecycle mutation | schemas.live_runs.BotLifecycleMutationResponse<br>engine.live.host_daemon_client | — | future LiveInstanceLifecycleService |
| 514 | `_visible_runs_by_instance` | instance status | services.fleet_contamination.scan_runs_by_instance | — | future LiveInstanceSurfaceSources |
| 526 | `_interpret_daemon_process` | instance status | schemas.live_runs.LiveBinding<br>schemas.live_runs.InstanceProcessView | — | future LiveInstanceSurfaceSources |
| 565 | `_visible_live_run_dir` | instance status | routers.live_runs._validate_path_segment<br>routers.live_runs._confine<br>pathlib.Path | — | future LiveInstanceSurfaceSources |
| 589 | `_resolve_readiness` | instance status | schemas.live_runs.ReadinessVector<br>pathlib.Path<br>engine.live.readiness.build_start_readiness<br>engine.live.readiness_sidecar.read_readiness | — | future LiveInstanceSurfaceSources |
| 623 | `_resolve_account_freeze` | instance status | engine.live.account_artifacts.read_account_freeze<br>routers.live_runs._read_ledger<br>engine.live.account_identity.normalize_account_id<br>pathlib.Path | — | future LiveInstanceSurfaceSources |
| 649 | `_run_dir_account_id` | instance status | routers.live_runs._read_ledger<br>engine.live.account_identity.normalize_account_id | — | future LiveInstanceSurfaceSources |
| 663 | `_raise_if_crash_recovery_blocks_start` | lifecycle mutation | services.account_crash_recovery.crash_recovery_blocking_binding<br>fastapi.HTTPException<br>services.account_crash_recovery.crash_recovery_block_detail | — | future LiveInstanceLifecycleService |
| 682 | `_resolve_account_clerk_surface` | instance status | schemas.live_runs.OperatorSurfaceAccountClerk<br>engine.live.account_artifacts.read_account_clerk_generation<br>engine.live.account_artifacts.read_active_accepting_account_clerk_generation | — | future LiveInstanceSurfaceSources |
| 724 | `_resolve_account_observation_surface` | instance status | engine.live.account_observation_lease.assess_account_observation_lease<br>schemas.live_runs.OperatorSurfaceAccountObservation | — | future LiveInstanceSurfaceSources |
| 746 | `_project_instance_account_lifecycle_events` | instance status | services.bot_lifecycle_projection.project_account_events<br>engine.live.account_artifacts.read_account_events<br>services.bot_lifecycle_projection.account_event_to_lifecycle_event | — | future LiveInstanceSurfaceSources |
| 783 | `_account_event_matches_instance` | instance status | internal helper / stdlib only | — | future LiveInstanceSurfaceSources |
| 815 | `_nonempty_str` | instance status | internal helper / stdlib only | — | future LiveInstanceSurfaceSources |
| 819 | `_strategy_state` | instance status | pathlib.Path<br>engine.live.live_artifact_io.artifact_exists<br>engine.live.live_artifact_io.read_parquet_tail<br>routers.live_runs._read_ledger | — | future LiveInstanceSurfaceSources |
| 852 | `_resolve_readonly_default` | deployment / launch | internal helper / stdlib only | — | future LiveInstanceDeploymentService |
| 871 | `_resolve_evidence_run_dir` | instance status | pathlib.Path | — | future LiveInstanceSurfaceSources |
| 884 | `_mutation_attempt_root` | reconciliation surfaces | internal helper / stdlib only | — | future LiveInstanceReconciliationService |
| 893 | `_operator_mutation_scope` | reconciliation surfaces | services.mutation_attempt.MutationAttemptRepo<br>services.mutation_attempt.MutationAttemptScope<br>routers.live_runs._now_ms | — | future LiveInstanceReconciliationService |
| 911 | `_mutation_error_detail` | reconciliation surfaces | internal helper / stdlib only | — | future LiveInstanceReconciliationService |
| 920 | `_resolve_reconciliation_inputs` | reconciliation surfaces | engine.live.intent_wal.IntentWal | — | future LiveInstanceReconciliationService |
| 957 | `_session_started_at_ms` | instance status | internal helper / stdlib only | — | future LiveInstanceSurfaceSources |
| 968 | `_resolve_live_run_dir` | instance status | pathlib.Path | — | future LiveInstanceSurfaceSources |
| 975 | `_resolve_durable_control_write_failure` | reconciliation surfaces | routers.live_runs.build_command_timeline<br>routers.live_runs._confine | — | future LiveInstanceReconciliationService |
| 991 | `_resolve_durable_control_write_failure_for_status` | reconciliation surfaces | internal helper / stdlib only | — | future LiveInstanceReconciliationService |
| 999 | `_resolve_incident_headline` | instance status | operator.incidents.store.IncidentStore | — | future LiveInstanceSurfaceSources |
| 1014 | `_resolve_latest_mutation` | reconciliation surfaces | services.mutation_attempt.MutationAttemptRepo | — | future LiveInstanceReconciliationService |
| 1027 | `_resolve_runtime_freshness` | instance status | internal helper / stdlib only | — | future LiveInstanceSurfaceSources |
| 1048 | `_resolve_engine_runtime_snapshot_and_freshness` | instance status | engine.live.engine_runtime.read_engine_runtime_snapshot<br>services.runtime_freshness.evaluate_runtime_freshness<br>services.runtime_freshness.unavailable_runtime_freshness<br>lean_sidecar.trading_calendar.session_state_at_ms | — | future LiveInstanceSurfaceSources |
| 1073 | `_safety_verdict_final_from_engine_runtime` | instance status | internal helper / stdlib only | — | future LiveInstanceSurfaceSources |
| 1086 | `_broker_connection_state_from_engine_runtime` | instance status | internal helper / stdlib only | — | future LiveInstanceSurfaceSources |
| 1129 | `_resolve_broker_observation_consistency` | instance status | broker.runtime_snapshot.snapshot_data_plane_broker | — | future LiveInstanceSurfaceSources |
| 1161 | `_resolve_start_run_id` | instance status | internal helper / stdlib only | — | future LiveInstanceSurfaceSources |
| 1175 | `_start_defaults` | instance status | schemas.live_runs.InstanceStartDefaults<br>routers.live_runs._read_ledger | — | future LiveInstanceSurfaceSources |
| 1225 | `_cohort_start_request_for_run` | deployment / launch | schemas.live_runs.HostRunnerStartRequest | — | future LiveInstanceDeploymentService |
| 1255 | `_cohort_live_config_for_run` | deployment / launch | routers.live_runs._read_ledger | — | future LiveInstanceDeploymentService |
| 1265 | `_resolve_symbol_resolution` | deployment / launch | services.deploy_admission.resolve_symbol_from_ledger<br>routers.live_runs._read_ledger | — | future LiveInstanceDeploymentService |
| 1280 | `_resolve_symbol` | deployment / launch | internal helper / stdlib only | — | future LiveInstanceDeploymentService |
| 1312 | `_resolve_session_capability_for_symbol` | deployment / launch | services.broker_capability_service.get_broker_capability_service | — | future LiveInstanceDeploymentService |
| 1323 | `_container_resolve_repo_path` | deployment / launch | pathlib.Path | — | future LiveInstanceDeploymentService |
| 1349 | `_sizing_audit_rows` | deployment / launch | broker.ibkr.config.get_settings<br>pathlib.Path<br>engine.live.live_state_sidecar.stable_live_state_path<br>engine.live.live_state_sidecar.LiveStateSidecarRepo | — | future LiveInstanceDeploymentService |
| 1387 | `_fold_wal_sizing_audit` | deployment / launch | json<br>engine.live.intent_wal.IntentWal | — | future LiveInstanceDeploymentService |
| 1512 | `_sizing` | deployment / launch | schemas.live_runs.InstanceSizing<br>routers.live_runs._read_ledger<br>schemas.live_runs.SizingAuditRow | — | future LiveInstanceDeploymentService |
| 1566 | `_resolve_action_plan` | deployment / launch | routers.live_runs._read_ledger | — | future LiveInstanceDeploymentService |
| 1593 | `_resolve_lineage` | deployment / launch | routers.live_runs._read_ledger | — | future LiveInstanceDeploymentService |
| 1613 | `_resolve_instrument_surface` | deployment / launch | routers.live_runs._read_ledger | — | future LiveInstanceDeploymentService |
| 1647 | `_provenance` | deployment / launch | schemas.live_runs.InstanceProvenance<br>routers.live_runs._read_ledger | — | future LiveInstanceDeploymentService |
| 1680 | `_build_live_instance_summaries` | fleet roster | routers.live_runs._resolve_desired_state<br>engine.live.host_daemon_client<br>schemas.live_runs.LiveBinding<br>schemas.live_runs.LiveInstanceSummary | — | future FleetRosterService |
| 1748 | `_fleet_roster_blockers` | fleet roster | schemas.operator_blocker.OperatorBlocker<br>schemas.operator_blocker.OperatorMove<br>schemas.operator_blocker.NavigateAction | — | future FleetRosterService |
| 1810 | `list_live_instances` | fleet roster | broker.ibkr.config.get_settings<br>pathlib.Path | _FLEET_ROSTER_HUB | future FleetRosterService |
| 1820 | `_daemon_process_from_instance` | fleet roster | internal helper / stdlib only | — | future FleetRosterService |
| 1829 | `_resolve_activity_publisher_for_status` | SSE / surface hubs | services.broker_activity_publisher_registry.get_publisher_registry | — | lifespan-owned LiveInstanceSurfaceRuntime |
| 1840 | `_resolve_daemon_diagnostic_condition_for_status` | SSE / surface hubs | internal helper / stdlib only | — | lifespan-owned LiveInstanceSurfaceRuntime |
| 1853 | `_resolve_fleet_blocks_starts_for_status` | SSE / surface hubs | internal helper / stdlib only | — | lifespan-owned LiveInstanceSurfaceRuntime |
| 1865 | `_surface_visible_runs_by_instance` | SSE / surface hubs | internal helper / stdlib only | _SURFACE_RUNS_CACHE | lifespan-owned LiveInstanceSurfaceRuntime |
| 1869 | `_fetch_surface_instance_process` | SSE / surface hubs | engine.live.host_daemon_client | _FLEET_DAEMON_PROVIDER | lifespan-owned LiveInstanceSurfaceRuntime |
| 1890 | `_get_surface_assembler` | SSE / surface hubs | services.live_instance_surface_assembler.LiveInstanceSurfaceAssembler<br>services.live_instance_surface_assembler.LiveInstanceSurfaceDependencies | — | lifespan-owned LiveInstanceSurfaceRuntime |
| 1953 | `_resolve_instance_status_from_process` | SSE / surface hubs | internal helper / stdlib only | — | lifespan-owned LiveInstanceSurfaceRuntime |
| 1972 | `_assemble_instance_surface` | SSE / surface hubs | internal helper / stdlib only | — | lifespan-owned LiveInstanceSurfaceRuntime |
| 1976 | `_reconcile_surface_activity_publisher` | SSE / surface hubs | services.broker_activity_publisher_registry.get_publisher_registry<br>asyncio | — | lifespan-owned LiveInstanceSurfaceRuntime |
| 2019 | `_surface_hub_for` | SSE / surface hubs | internal helper / stdlib only | _SURFACE_HUBS | lifespan-owned LiveInstanceSurfaceRuntime |
| 2027 | `_assemble_fleet_roster_snapshot` | SSE / surface hubs | broker.ibkr.config.get_settings<br>pathlib.Path<br>schemas.live_runs.FleetRosterSnapshot<br>routers.live_runs._now_ms | _FLEET_DAEMON_PROVIDER | lifespan-owned LiveInstanceSurfaceRuntime |
| 2045 | `_fleet_roster_hub_for` | SSE / surface hubs | services.surface_hub.SurfaceHub | _FLEET_ROSTER_HUB | lifespan-owned LiveInstanceSurfaceRuntime |
| 2056 | `start_surface_hubs` | SSE / surface hubs | broker.ibkr.config.get_settings<br>pathlib.Path<br>services.fleet_daemon_snapshot_provider.FleetDaemonSnapshotProvider<br>services.mutation_attempt.MutationAttemptRepo | _FLEET_DAEMON_PROVIDER<br>_SURFACE_HUBS | lifespan-owned LiveInstanceSurfaceRuntime |
| 2087 | `stop_surface_hubs` | SSE / surface hubs | internal helper / stdlib only | _FLEET_DAEMON_PROVIDER<br>_FLEET_ROSTER_HUB<br>_SURFACE_HUBS<br>_SURFACE_RUNS_CACHE | lifespan-owned LiveInstanceSurfaceRuntime |
| 2104 | `_ensure_surface_hub_started` | SSE / surface hubs | pathlib.Path<br>broker.ibkr.config.get_settings | _FLEET_DAEMON_PROVIDER<br>_FLEET_ROSTER_HUB<br>_SURFACE_RUNS_CACHE | lifespan-owned LiveInstanceSurfaceRuntime |
| 2131 | `list_bot_catalog` | fleet roster | broker.ibkr.config.get_settings<br>pathlib.Path<br>services.bot_catalog_projection.trading_mode_from_configured_mode<br>schemas.live_runs.BotCatalogResponse | — | future FleetRosterService |
| 2174 | `_bot_catalog_row_for_sid` | fleet roster | services.bot_catalog_projection.compose_bot_catalog_row<br>services.bot_roll_call.attendance_for_instance | — | future FleetRosterService |
| 2201 | `_resolve_instance_status_for_fleet_sid` | fleet roster | engine.live.host_daemon_client | — | future FleetRosterService |
| 2225 | `run_roll_call` | fleet roster | broker.ibkr.config.get_settings<br>pathlib.Path<br>routers.live_runs._now_ms<br>services.bot_catalog_projection.trading_mode_from_configured_mode | — | future FleetRosterService |
| 2334 | `_live_config_for_run_dir` | fleet roster | routers.live_runs._read_ledger | — | future FleetRosterService |
| 2343 | `_is_retired_bot` | fleet roster | internal helper / stdlib only | — | future FleetRosterService |
| 2349 | `delete_instance` | fleet roster | broker.ibkr.config.get_settings<br>pathlib.Path<br>schemas.live_runs.BotDeleteRequest<br>engine.live.host_daemon_client | _SURFACE_HUBS<br>_SURFACE_RUNS_CACHE | future FleetRosterService |
| 2434 | `_read_bot_deletion_for_endpoint` | fleet roster | services.bot_deletion.read_bot_deletion<br>fastapi.HTTPException | — | future FleetRosterService |
| 2448 | `_bot_delete_response` | fleet roster | schemas.live_runs.BotDeleteResponse<br>services.bot_deletion.stable_bot_deletion_path | — | future FleetRosterService |
| 2459 | `_raise_if_deploy_admission_blocks_start` | deployment / launch | services.deploy_admission.evaluate_deploy_start_admission<br>services.fleet_contamination.instance_broker<br>fastapi.HTTPException | — | future LiveInstanceDeploymentService |
| 2480 | `_host_deploy_request_from_public` | deployment / launch | schemas.live_runs.HostRunnerDeployRequest<br>fastapi.HTTPException | — | future LiveInstanceDeploymentService |
| 2516 | `deploy_preflight` | deployment / launch | services.deploy_preflight<br>schemas.operator_blocker.DeployPreflightResponse<br>fastapi.HTTPException | — | future LiveInstanceDeploymentService |
| 2543 | `_raise_if_deploy_preflight_blocks_start` | deployment / launch | fastapi.HTTPException<br>services.deploy_preflight | — | future LiveInstanceDeploymentService |
| 2578 | `deploy_instance` | deployment / launch | broker.ibkr.config.get_settings<br>pathlib.Path<br>engine.live.account_artifacts.read_account_freeze<br>fastapi.HTTPException | — | future LiveInstanceDeploymentService |
| 2693 | `_parse_action_response` | deployment / launch | schemas.live_runs.HostRunnerActionResponse<br>fastapi.HTTPException | — | future LiveInstanceDeploymentService |
| 2705 | `_mutation_rung_receipts_from_process` | deployment / launch | services.mutation_rung_receipts.mutation_rung_receipts | — | future LiveInstanceDeploymentService |
| 2723 | `_mutation_rung_receipts_for_instance` | deployment / launch | engine.live.host_daemon_client | — | future LiveInstanceDeploymentService |
| 2740 | `_strategy_instance_id_for_run` | deployment / launch | services.fleet_contamination.scan_runs_by_instance | — | future LiveInstanceDeploymentService |
| 2748 | `preview_action_plan` | deployment / launch | schemas.action_plan.ActionPlanPreviewResponse<br>engine.action_plan.parity.parity_diagnostics | — | future LiveInstanceDeploymentService |
| 2770 | `_bot_soft_deleted_detail` | instance status | internal helper / stdlib only | — | future LiveInstanceSurfaceSources |
| 2781 | `_raise_if_start_boundary_blocks` | lifecycle mutation | pathlib.Path<br>services.daily_session_schedule.start_boundary_verdict<br>fastapi.HTTPException | — | future LiveInstanceLifecycleService |
| 2802 | `_ensure_account_observation_lease_allows_start` | lifecycle mutation | engine.live.account_observation_lease.assess_account_observation_lease<br>services.account_reconciliation.AccountReconciliationService<br>engine.live.account_observation_lease.account_observation_lease_gate_result<br>fastapi.HTTPException | — | future LiveInstanceLifecycleService |
| 2874 | `_assert_roll_call_offer_allows_start` | lifecycle mutation | fastapi.HTTPException | — | future LiveInstanceLifecycleService |
| 2932 | `_start_request_with_ledger_strategy_default` | lifecycle mutation | routers.live_runs._read_ledger<br>schemas.live_runs.HostRunnerStartRequest<br>fastapi.HTTPException | — | future LiveInstanceLifecycleService |
| 2971 | `_raise_if_lifecycle_retired` | lifecycle mutation | fastapi.HTTPException | — | future LiveInstanceLifecycleService |
| 2996 | `_start_intent_repo` | lifecycle mutation | routers.live_runs._desired_state_root<br>engine.live.desired_state.DesiredStateRepo<br>engine.live.desired_state.stable_desired_state_path<br>fastapi.HTTPException | — | future LiveInstanceLifecycleService |
| 3005 | `_persist_start_intent` | lifecycle mutation | fastapi.HTTPException<br>routers.live_runs._now_ms | — | future LiveInstanceLifecycleService |
| 3040 | `_restore_start_intent` | lifecycle mutation | internal helper / stdlib only | — | future LiveInstanceLifecycleService |
| 3061 | `_assert_start_allowed` | lifecycle mutation | pathlib.Path<br>routers.live_runs._now_ms<br>fastapi.HTTPException<br>engine.live.host_daemon_client | — | future LiveInstanceLifecycleService |
| 3177 | `start_run` | lifecycle mutation | broker.ibkr.config.get_settings<br>pathlib.Path<br>routers.live_runs._validate_path_segment<br>fastapi.HTTPException | — | future LiveInstanceLifecycleService |
| 3284 | `launch_cohort` | deployment / launch | engine.live.account_identity.normalize_account_id<br>broker.ibkr.config.get_settings<br>services.cohort_launch.CohortLaunchCoordinator<br>fastapi.HTTPException | — | future LiveInstanceDeploymentService |
| 3334 | `_maybe_start_broker_activity_publisher` | deployment / launch | internal helper / stdlib only | — | future LiveInstanceDeploymentService |
| 3380 | `stop_run` | lifecycle mutation | broker.ibkr.config.get_settings<br>pathlib.Path<br>routers.live_runs._validate_path_segment<br>fastapi.HTTPException | — | future LiveInstanceLifecycleService |
| 3451 | `end_day_now` | lifecycle mutation | broker.ibkr.config.get_settings<br>pathlib.Path<br>engine.live.host_daemon_client<br>fastapi.HTTPException | — | future LiveInstanceLifecycleService |
| 3533 | `set_lifecycle_roster` | lifecycle mutation | broker.ibkr.config.get_settings<br>pathlib.Path<br>routers.live_runs._now_ms | — | future LiveInstanceLifecycleService |
| 3552 | `retire_and_replace` | lifecycle mutation | broker.ibkr.config.get_settings<br>pathlib.Path<br>fastapi.HTTPException<br>engine.live.host_daemon_client | — | future LiveInstanceLifecycleService |
| 3597 | `_instance_last_exit` | instance status | pathlib.Path<br>routers.live_runs._read_sidecar<br>schemas.live_runs.InstanceLastExit<br>engine.live.halt.read_poisoned_flag | — | future LiveInstanceSurfaceSources |
| 3680 | `get_audit_copy_sizing_lookup` | deployment / launch | broker.ibkr.config.get_settings<br>engine.live.host_daemon_client<br>schemas.live_runs.AuditCopySizingLookup<br>fastapi.HTTPException | — | future LiveInstanceDeploymentService |
| 3734 | `get_qc_audit_copies` | deployment / launch | broker.ibkr.config.get_settings<br>engine.live.host_daemon_client<br>schemas.live_runs.QcAuditCopyListing<br>fastapi.HTTPException | — | future LiveInstanceDeploymentService |
| 3760 | `get_daemon_diagnostics` | diagnostics | services.daemon_diagnostics.get_daemon_diagnostics_service | — | DaemonDiagnosticsService |
| 3772 | `get_instance_daemon_diagnostics` | diagnostics | services.daemon_diagnostics.project_daemon_diagnostic_report<br>services.daemon_diagnostics.get_daemon_diagnostics_service | — | DaemonDiagnosticsService |
| 3781 | `get_daemon_health` | diagnostics | fastapi.HTTPException<br>services.daemon_diagnostics.get_daemon_diagnostics_service | — | DaemonDiagnosticsService |
| 3821 | `renew_daemon_lease` | diagnostics | fastapi.HTTPException<br>services.daemon_diagnostics.get_daemon_diagnostics_service | — | DaemonDiagnosticsService |
| 3842 | `_instance_ledger_account_id` | fleet roster | services.fleet_contamination.scan_runs_by_instance<br>routers.live_runs._read_ledger<br>engine.live.account_identity.normalize_account_id<br>pathlib.Path | — | future FleetRosterService |
| 3869 | `_fetch_broker_connected_account` | fleet roster | broker.runtime_snapshot.snapshot_data_plane_broker | — | future FleetRosterService |
| 3894 | `_cohort_target_account_posture` | fleet roster | broker.runtime_snapshot.snapshot_data_plane_broker<br>engine.live.account_identity.normalize_account_id | — | future FleetRosterService |
| 3922 | `_compute_account_fleet_contamination` | fleet roster | services.fleet_contamination | — | future FleetRosterService |
| 3934 | `_raise_if_fleet_contamination_blocks_start` | fleet roster | fastapi.HTTPException<br>engine.live.account_identity.normalize_account_id<br>services.deploy_preflight | — | future FleetRosterService |
| 4030 | `get_account_fleet` | fleet roster | broker.ibkr.config.get_settings<br>pathlib.Path | — | future FleetRosterService |
| 4044 | `get_account_summary` | fleet roster | fastapi.Query<br>broker.ibkr.config.get_settings<br>pathlib.Path<br>services.fleet_contamination.scan_runs_by_instance | — | future FleetRosterService |
| 4086 | `_surface_snapshot_unavailable` | SSE / surface hubs | fastapi.HTTPException | — | lifespan-owned LiveInstanceSurfaceRuntime |
| 4101 | `stream_fleet_roster` | SSE / surface hubs | fastapi.responses.StreamingResponse<br>fastapi.HTTPException<br>fastapi.Header | _FLEET_ROSTER_HUB | lifespan-owned LiveInstanceSurfaceRuntime |
| 4141 | `stream_instance_operator_surface` | SSE / surface hubs | broker.ibkr.config.get_settings<br>pathlib.Path<br>fastapi.responses.StreamingResponse<br>fastapi.HTTPException | _SURFACE_HUBS | lifespan-owned LiveInstanceSurfaceRuntime |
| 4184 | `get_instance_status` | SSE / surface hubs | fastapi.Query<br>broker.ibkr.config.get_settings<br>pathlib.Path<br>fastapi.HTTPException | _SURFACE_HUBS | lifespan-owned LiveInstanceSurfaceRuntime |
| 4215 | `record_crash_recovery_override` | lifecycle mutation | broker.ibkr.config.get_settings<br>pathlib.Path<br>fastapi.HTTPException<br>services.account_crash_recovery.record_crash_recovery_override_evidence | — | future LiveInstanceLifecycleService |
| 4287 | `_resolve_safety_verdict_final` | instance status | broker.runtime_snapshot.snapshot_data_plane_broker | — | future LiveInstanceSurfaceSources |
| 4323 | `_resolve_resume_guard_state_for` | instance status | services.resume_guard_state.resolve_guard_state_from_paths<br>services.resume_guard_state.empty_guard_state | — | future LiveInstanceSurfaceSources |
| 4347 | `_load_instance_context_for_router` | instance status | broker.ibkr.config.get_settings<br>pathlib.Path<br>services.instance_context.load_instance_context<br>engine.live.host_daemon_client | — | future LiveInstanceSurfaceSources |
| 4424 | `_raise_outcome_unknown` | instance status | schemas.live_runs.MutationOutcomeUnknownResponse<br>fastapi.HTTPException<br>routers.live_runs._now_ms | — | future LiveInstanceSurfaceSources |
| 4461 | `_broker_connection_state_from_readiness` | instance status | internal helper / stdlib only | — | future LiveInstanceSurfaceSources |
| 4485 | `_read_parquet_rows` | activity / events projection | engine.live.live_artifact_io.read_parquet_rows | — | future LiveInstanceActivityService |
| 4499 | `_filter_rows_to_utc_day` | activity / events projection | datetime.datetime | — | future LiveInstanceActivityService |
| 4510 | `_filter_rows_to_window` | activity / events projection | internal helper / stdlib only | — | future LiveInstanceActivityService |
| 4515 | `_runs_active_on` | activity / events projection | pathlib.Path<br>routers.live_runs._read_sidecar<br>datetime.datetime | — | future LiveInstanceActivityService |
| 4541 | `_runs_active_in_window` | activity / events projection | pathlib.Path<br>routers.live_runs._read_sidecar | — | future LiveInstanceActivityService |
| 4576 | `_today_ny` | activity / events projection | datetime.datetime | — | future LiveInstanceActivityService |
| 4587 | `_ny_session_bounds_ms` | activity / events projection | services.activity_lifecycle_consistency.ny_session_bounds_ms | — | future LiveInstanceActivityService |
| 4597 | `_activity_evidence_refs_for_session` | activity / events projection | broker.ibkr.api_evidence.get_ibkr_api_evidence_recorder<br>services.activity_evidence_matching.activity_evidence_ref_from_event | — | future LiveInstanceActivityService |
| 4620 | `_activity_row_time_ms` | activity / events projection | internal helper / stdlib only | — | future LiveInstanceActivityService |
| 4624 | `_activity_order_key` | activity / events projection | internal helper / stdlib only | — | future LiveInstanceActivityService |
| 4634 | `_activity_fill_key` | activity / events projection | internal helper / stdlib only | — | future LiveInstanceActivityService |
| 4640 | `_position_effect_for_fill` | activity / events projection | internal helper / stdlib only | — | future LiveInstanceActivityService |
| 4665 | `_read_activity_wal_rows` | activity / events projection | services.broker_activity_wal.BrokerActivityWal<br>services.broker_activity_wal.instance_broker_activity_wal_path | — | future LiveInstanceActivityService |
| 4674 | `_latest_activity_wal_day` | activity / events projection | services.broker_activity_wal.BrokerActivityWal<br>services.broker_activity_wal.instance_broker_activity_wal_path<br>datetime.datetime | — | future LiveInstanceActivityService |
| 4686 | `_build_activity_projection` | activity / events projection | services.activity_projection_contract.fold_activity_event_rows<br>schemas.live_runs.LiveInstanceActivityProjection<br>schemas.live_runs.ActivityFillMarker<br>services.activity_projection_contract.activity_evidence_narrative | — | future LiveInstanceActivityService |
| 4960 | `_activity_lifecycle_consistency_warnings` | activity / events projection | services.activity_lifecycle_consistency.activity_order_refs_for_session<br>services.activity_lifecycle_consistency.activity_lifecycle_consistency_warnings | — | future LiveInstanceActivityService |
| 4988 | `_lifecycle_order_refs_for_activity` | activity / events projection | services.activity_lifecycle_consistency.runs_active_in_window | — | future LiveInstanceActivityService |
| 5033 | `_read_intent_events_for_activity` | activity / events projection | engine.live.intent_wal.IntentWal | — | future LiveInstanceActivityService |
| 5047 | `_run_account_id` | activity / events projection | routers.live_runs._read_ledger<br>engine.live.account_identity.normalize_account_id | — | future LiveInstanceActivityService |
| 5061 | `_ts_in_window` | activity / events projection | internal helper / stdlib only | — | future LiveInstanceActivityService |
| 5065 | `_order_ref_from_lifecycle_payload` | activity / events projection | internal helper / stdlib only | — | future LiveInstanceActivityService |
| 5075 | `_group_rows_by_order_key` | activity / events projection | internal helper / stdlib only | — | future LiveInstanceActivityService |
| 5090 | `_broker_activity_summary` | activity / events projection | schemas.live_runs.ActivityBrokerCategorySummary | — | future LiveInstanceActivityService |
| 5132 | `_broker_activity_category` | activity / events projection | re | — | future LiveInstanceActivityService |
| 5152 | `get_chart_snapshot` | activity / events projection | broker.ibkr.config.get_settings<br>pathlib.Path<br>routers.live_runs._now_ms<br>schemas.live_runs.ChartSnapshotResponse | — | future LiveInstanceActivityService |
| 5300 | `get_instance_activity` | activity / events projection | broker.ibkr.config.get_settings<br>pathlib.Path<br>services.activity_repair_projection.load_activity_repair_projection<br>services.activity_projection_contract.fold_activity_event_rows | — | future LiveInstanceActivityService |
| 5401 | `get_active_dates` | activity / events projection | broker.ibkr.config.get_settings<br>pathlib.Path<br>routers.live_runs._now_ms<br>fastapi.HTTPException | — | future LiveInstanceActivityService |
| 5473 | `set_instance_desired_state` | lifecycle mutation | broker.ibkr.config.get_settings<br>pathlib.Path<br>routers.live_runs._desired_state_root<br>services.operator_capability.evaluate_action | — | future LiveInstanceLifecycleService |
| 5620 | `flatten_and_pause_instance` | lifecycle mutation | broker.ibkr.config.get_settings<br>pathlib.Path<br>routers.live_runs._desired_state_root<br>services.operator_capability.evaluate_action | — | future LiveInstanceLifecycleService |
| 5774 | `reconcile_instance` | reconciliation surfaces | broker.ibkr.config.get_settings<br>pathlib.Path<br>schemas.live_runs.ReconcileAckResponse<br>engine.live.host_daemon_client | — | future LiveInstanceReconciliationService |
| 5850 | `reconcile_instance_mutation` | reconciliation surfaces | broker.ibkr.config.get_settings<br>pathlib.Path<br>services.mutation_attempt.MutationAttemptRepo<br>services.mutation_attempt.reconcile_mutation_effect | — | future LiveInstanceReconciliationService |
| 5929 | `_assemble_reconciliation_evidence` | reconciliation surfaces | routers.live_runs._now_ms<br>services.mutation_attempt.ReconciliationEvidence<br>engine.live.host_daemon_client | — | future LiveInstanceReconciliationService |
| 5964 | `_read_desired_state_literal` | reconciliation surfaces | routers.live_runs._resolve_desired_state | — | future LiveInstanceReconciliationService |
| 5975 | `_read_engine_runtime_state` | reconciliation surfaces | pathlib.Path<br>engine.live.engine_runtime.read_engine_runtime_snapshot<br>services.fleet_contamination.scan_runs_by_instance | — | future LiveInstanceReconciliationService |
| 5989 | `_read_owned_positions_empty` | reconciliation surfaces | services.fleet_contamination.instance_broker | — | future LiveInstanceReconciliationService |
| 6001 | `get_instance_commands` | reconciliation surfaces | broker.ibkr.config.get_settings<br>pathlib.Path<br>schemas.live_runs.CommandsTimeline<br>engine.live.host_daemon_client | — | future LiveInstanceReconciliationService |
| 6021 | `issue_instance_command` | reconciliation surfaces | broker.ibkr.config.get_settings<br>pathlib.Path<br>engine.live.command_channel.CommandVerb<br>fastapi.HTTPException | — | future LiveInstanceReconciliationService |

## Domain-level characterization net

| Domain | Primary router tests / service tests | Extraction safety net |
|---|---|---|
| instance status | `test_live_instances.py` status, provenance, start-default, and surface payload cases; `test_live_instances_operator_surface.py` | `tests/fixtures/surface_hub/status_payload_parity.json`, `tests/services/test_surface_hub.py` |
| SSE / surface hubs | `test_live_instances.py` surface-hub and stream cases | `tests/services/test_surface_hub.py` lifecycle, generation, and cache tests |
| fleet roster | catalog, roll-call, deletion, account-summary, and fleet-stream cases in `test_live_instances.py` | surface roster and daemon-provider tests |
| deployment / launch | deploy preflight, deploy/start, rolling one-bot admission, and retired cohort-route cases in `test_live_instances.py` | deploy-preflight service tests plus host-daemon contract tests |
| lifecycle mutation | start/stop/end-day, roster, retire/replace, desired-state, and flatten cases in `test_live_instances.py` | desired-state, lifecycle, and mutation-attempt service tests |
| diagnostics | daemon-health, daemon-diagnose, and lease-renewal cases in `test_live_instances.py` | `tests/services/test_daemon_diagnostics.py` |
| activity / events projection | chart, activity, active-date, evidence, repair, and DST cases in `test_live_instances.py` | activity projection, repair, and lifecycle-consistency service tests |
| reconciliation surfaces | reconcile, reconcile-mutation, command, and emergency-flatten cases in `test_live_instances.py` | mutation-attempt and command-channel service/engine tests |

## Reading the map

The `LiveInstanceSurfaceAssembler` extraction is real but incomplete: its dependency dataclass still accepts 56 router-owned callables. The highest-value seam is therefore to replace those adapters with a domain-owned source object, while keeping the router as HTTP transport and keeping the runtime lifecycle in the FastAPI lifespan.
