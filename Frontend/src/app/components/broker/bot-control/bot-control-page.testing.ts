import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  provideZonelessChangeDetection,
  signal,
} from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, type ParamMap } from '@angular/router';
import { of, type Observable } from 'rxjs';
import { vi } from 'vitest';

import type {
  FleetAccountSummary,
  HostProcessState,
  LifecycleTimelineResponse,
  LiveInstanceStatus,
  OperatorNotice,
  OperatorSurfaceRuntimeFreshness,
  SetInstanceDesiredStateResponse,
} from '../../../api/live-instances.types';
import type {
  CommandVerb,
  CommandWriteResponse,
  HostRunnerActionResponse,
  HostRunnerHealth,
  HostRunnerStartRequest,
  ReconcileAckResponse,
} from '../../../api/live-runs.types';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerBannerComponent } from '../../../shell/broker-banner.component';
import { makeLifecycleChartFixture } from '../../../testing/live-instance-status-fixtures';
import { ActivityTabComponent } from './tabs/activity-tab.component';
import { BotControlPageComponent } from './bot-control-page.component';
import { WorkbenchAuditPanelComponent } from './workbench-audit-panel.component';

@Component({
  selector: 'app-activity-tab',
  template: '<div data-testid="activity-tab-stub"></div>',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
class ActivityTabStubComponent {
  readonly status = input.required<LiveInstanceStatus>();
}

@Component({
  selector: 'app-workbench-audit-panel',
  template: `
    <div data-testid="workbench-audit-panel">
      @for (line of proofLines(); track line.id) {
        <div
          data-testid="locked-evidence-field"
          [class.tone-neutral]="line.tone === 'neutral'"
          [class.tone-ok]="line.tone === 'ok'"
          [class.tone-attention]="line.tone === 'attention'"
          [attr.title]="line.detail"
        >
          <span>{{ line.label }}</span>
          <strong>{{ line.message }}</strong>
        </div>
      }
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
class WorkbenchAuditPanelStubComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly proofLines = computed(() => this.status().operator_surface.trader_guidance.proof_lines);
}

@Component({
  imports: [BotControlPageComponent, BrokerBannerComponent],
  template: `
    <app-bot-control-page />
    <app-broker-banner />
  `,
})
export class BotControlWithSidebarHostComponent {}

class FakeBrokerHealthService {
  readonly health = signal(null);
  readonly bannerState = signal(null);
  readonly lifecycleAction = signal(null);
  connect = vi.fn().mockResolvedValue(undefined);
  disconnect = vi.fn().mockResolvedValue(undefined);
}

export class FakeLiveRunsService {
  getInstanceStatus = vi.fn<LiveRunsService['getInstanceStatus']>();
  getAccountSummary = vi.fn<LiveRunsService['getAccountSummary']>();
  getLifecycleTimeline = vi.fn<LiveRunsService['getLifecycleTimeline']>();
  renewControlPlaneLease = vi.fn<LiveRunsService['renewControlPlaneLease']>();
  startHostRunner = vi.fn<LiveRunsService['startHostRunner']>();
  setInstanceDesiredState = vi.fn<LiveRunsService['setInstanceDesiredState']>();
  flattenAndPause = vi.fn<LiveRunsService['flattenAndPause']>();
  issueInstanceCommand = vi.fn<LiveRunsService['issueInstanceCommand']>();
  reconcileInstance = vi.fn<LiveRunsService['reconcileInstance']>();
}

export function makeStatus(options: {
  id?: string;
  hostState?: HostProcessState;
  hostNotice?: string;
  startCapabilityEnabled?: boolean;
  startRunId?: string;
  startRequest?: HostRunnerStartRequest;
  markPoisonedEnabled?: boolean;
} = {}): LiveInstanceStatus {
  const hostState = options.hostState ?? 'UNREACHABLE';
  const startRequest: HostRunnerStartRequest = options.startRequest ?? {
    readonly: false,
    hydrate_policy: 'require',
    strategy: 'deployment_validation',
    max_orders_per_day: 2,
    ibkr_host: '127.0.0.1',
  };
  const processState: LiveInstanceStatus['process']['state'] = hostState === 'WAITING_FOR_HOST'
    ? 'idle'
    : hostState === 'RUNNING'
      ? 'running'
      : 'exited';
  return {
    strategy_instance_id: options.id ?? 'sid-x',
    process: { state: processState, pid: null, bound_run_id: null, started_at_ms: null },
    live_binding: null,
    evidence_binding: null,
    desired_state: {
      state: 'RUNNING',
      path_status: 'ok',
      updated_at_ms: 0,
      updated_by: 'op',
      reason: null,
      version: 1,
    },
    readiness: null,
    latest_decision: null,
    latest_signal_tone: 'neutral',
    decision_columns: [],
    broker: null,
    start_defaults: null,
    provenance: null,
    sizing: null,
    last_exit: null,
    symbol: 'SPY',
    action_plan: null,
    instrument_surface: null,
    lineage: null,
    operator_surface: {
      schema_version: 1,
      host_process: {
        state: hostState,
        notice: options.hostNotice ?? 'Start the host runner before trading this bot.',
        copyable_command: hostState === 'UNREACHABLE' ? 'make broker-runner' : null,
        start_capability: options.startCapabilityEnabled
          ? {
              enabled: true,
              run_id: options.startRunId ?? 'run-x',
              request: startRequest,
              disabled_reason_code: null,
              gate_results: [],
            }
          : {
              enabled: false,
              run_id: null,
              request: null,
              disabled_reason_code: 'HOST_SERVICE_OFFLINE',
              gate_results: [],
            },
      },
      prior_run: { classification: 'UNKNOWN' },
      broker: { safety_verdict: 'UNKNOWN', connection: 'DISCONNECTED' },
      configuration: { verdict: 'UNKNOWN', reason_codes: [] },
      current_risk: {
        posture: 'UNKNOWN',
        pending_order_count: null,
        verdict: 'UNKNOWN',
        unrealized_pnl: null,
      },
      daily_order_cap: { used: null, limit: null },
      action_plan: { consumption: 'UNKNOWN', anomaly_verdict: 'UNKNOWN' },
      account_owner: null,
      submit_readiness: {
        code: 'broker_state_unproven',
        label: 'Broker state unproven',
        explanation: 'The backend cannot prove the broker/session/reconciliation evidence required for a safe submit.',
        can_submit: false,
        blocking_reason_codes: ['BROKER_CONNECTION_DISCONNECTED'],
        template_id: 'operator_surface.submit_readiness.broker_state_unproven',
        template_version: 1,
      },
      trader_guidance: {
        situation_code: 'broker_state_unproven',
        headline: 'Broker state is not proven enough to submit.',
        explanation: 'The backend cannot prove the broker/session/reconciliation facts needed before a submit.',
        risk_headline: 'Do not treat stale or missing broker evidence as live truth',
        risk_explanation: 'Reconnect or reconcile until the broker evidence is fresh and explicit.',
        primary_remediation: {
          kind: 'invoke_endpoint',
          endpoint: 'reconcile_instance',
          method: 'POST',
          path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
        },
        additional_attention_groups: [
          {
            code: 'broker_connection',
            severity: 'warning',
            headline: 'Broker session is disconnected',
            explanation: 'The broker connection evidence is not connected.',
            operator_next_step: 'Reconnect the broker session, then refresh broker evidence.',
            remediation: { kind: 'open_runbook', slug: 'broker-reconnect' },
          },
        ],
        proof_lines: [
          {
            id: 'broker-proof',
            label: 'Broker',
            message: 'Broker session is disconnected.',
            detail: 'Account safety proof is not recorded. Broker session is disconnected.',
            tone: 'attention',
          },
          {
            id: 'submit-readiness',
            label: 'Trade submit',
            message: 'Broker state unproven',
            detail:
              'The backend cannot prove the broker/session/reconciliation evidence required for a safe submit. 1 blocking proof still needs attention.',
            tone: 'attention',
          },
          {
            id: 'account-owner',
            label: 'Account owner',
            message: 'Waiting for AccountOwner proof.',
            detail: 'No AccountOwner artifact is available for this bot.',
            tone: 'attention',
          },
          {
            id: 'reconciliation',
            label: 'Reconciliation',
            message: 'Waiting for reconciliation proof.',
            detail: 'No reconciliation claim has been produced for this run.',
            tone: 'attention',
          },
          {
            id: 'runtime-freshness',
            label: 'Runtime',
            message: 'No live runtime is bound yet.',
            detail: 'No child runtime is currently bound to this instance.',
            tone: 'attention',
          },
        ],
        advanced_evidence: [
          {
            label: 'broker.connection',
            value: 'DISCONNECTED',
            source: 'operator_surface',
            gate_id: null,
            ts_ms: null,
            ts_ms_resolved: false,
          },
        ],
        template_id: 'operator_surface.trader_guidance.broker_state_unproven',
        template_version: 1,
      },
      actions: {
        resume: {
          enabled: false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: 'NO_LIVE_BINDING',
          disabled_reasons: ['NO_LIVE_BINDING'],
          gate_results: [],
        },
        pause: {
          enabled: true,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: null,
          disabled_reasons: [],
          gate_results: [],
        },
        stop: {
          enabled: true,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: null,
          disabled_reasons: [],
          gate_results: [],
        },
        flatten_and_pause: {
          enabled: false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: 'NO_OWNED_POSITIONS',
          disabled_reasons: ['NO_OWNED_POSITIONS'],
          gate_results: [],
        },
        mark_poisoned: {
          enabled: options.markPoisonedEnabled ?? false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: options.markPoisonedEnabled ? null : 'NO_LIVE_BINDING',
          disabled_reasons: options.markPoisonedEnabled ? [] : ['NO_LIVE_BINDING'],
          gate_results: [],
        },
      },
      trading_session: {
        phase: 'UNKNOWN',
        permits_strategy_activity: false,
        next_transition_ms: null,
        timezone: 'America/New_York',
        as_of_ms: 0,
      },
      readiness_gates: [],
      runtime_freshness: null,
      control_plane: {
        state: 'UNREACHABLE',
        last_transition_ms: 0,
        last_success_ms: null,
        attempt: 0,
        daemon_boot_id: null,
        notice: 'Last command channel health check failed.',
        runbook_slug: 'control plane/runbook?',
      },
      broker_observation_consistency: null,
      reconciliation: null,
      broker_activity_health: null,
      incident_headline: null,
    },
    lifecycle_chart: makeLifecycleChartFixture(),
    fetched_at_ms: 0,
  };
}

export function makeAccountSummary(): FleetAccountSummary {
  return {
    account_id: 'DU1',
    account_identity: 'UNKNOWN',
    account_identity_reason_codes: [],
    contamination: {
      net_positions: null,
      explained_total: {},
      explained_by_instance: [],
      residual: {},
      verdict: 'unknown',
      policy_blocks_starts: false,
      summary: 'Broker evidence unavailable.',
    },
    notice: {
      code: 'activity.source_blind_to_bot_orders',
      tier: 'warning',
      title: 'Broker evidence is unavailable',
      message: 'The data plane could not fetch broker net positions.',
      source_codes: [],
      forensic_facts: {},
      action: {
        kind: 'external_manual_check',
        label: 'Check positions in IBKR',
        target: 'ibkr_positions',
      },
      runbook_slug: 'broker evidence/health?',
      occurred_at_ms: null,
    },
  };
}

export function makeIncidentHeadline(): OperatorNotice {
  return {
    code: 'watchdog.flatten_timed_out',
    tier: 'critical',
    title: 'Flatten timed out',
    message: 'The watchdog could not prove that the account is flat after the emergency flatten attempt.',
    source_codes: ['watchdog.flatten_timed_out'],
    forensic_facts: {
      run_id: 'run-x',
      attempt: 1,
    },
    action: { kind: 'none', label: null, target: null },
    runbook_slug: 'watchdog-halt',
    occurred_at_ms: 1_700_000_001_000,
  };
}

export function makeRuntimeFreshnessWithLeaseAction(): OperatorSurfaceRuntimeFreshness {
  const headline: OperatorNotice = {
    code: 'runtime.control_plane_lease_stale',
    tier: 'critical',
    title: 'Control-plane lease is stale',
    message: 'The engine has not observed a fresh daemon lease.',
    source_codes: ['CONTROL_PLANE_LEASE_STALE'],
    forensic_facts: {},
    action: {
      kind: 'renew_control_plane_lease',
      label: 'Renew control-plane lease',
      target: 'daemon_lease',
    },
    runbook_slug: 'runtime-freshness',
    occurred_at_ms: 1_700_000_001_000,
  };
  return {
    posture_demoted: true,
    stale_reason_codes: ['CONTROL_PLANE_LEASE_STALE'],
    command_loop: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
    broker: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
    bar_loop: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
    control_plane: {
      state: 'STALE',
      age_ms: 30_000,
      stale_reason_codes: ['CONTROL_PLANE_LEASE_STALE'],
    },
    headline,
    additional_reasons: [],
  };
}

export function makeLifecycleTimeline(): LifecycleTimelineResponse {
  return {
    projection_available: true,
    canonical_fallback_required: false,
    rows: [
      {
        id: 101,
        account_id: 'DU1',
        strategy_instance_id: 'sid-x',
        run_id: 'run-x',
        event_id: 'intent_wal:run-x:7:ACK_FAILED_UNCERTAIN',
        event_type: 'BrokerOrderUncertain',
        category: 'order',
        node_id: 'ack_or_reconcile',
        gate_id: null,
        status: 'blocked',
        severity: 'warning',
        ts_ms: 1_700_000_001_000,
        ts_ms_resolved: true,
        source_artifact: 'intent_events.jsonl',
        source_type: 'broker_ack',
        source_rank: 30,
        source_seq: 7,
        source_offset: null,
        source_hash: null,
        summary: 'Broker acknowledgment failed; submit outcome is uncertain.',
        why: 'Probe broker before retrying this intent.',
        operator_next_step: 'PROBE_BROKER_BEFORE_RETRY',
        receipt_payload: { intent_id: 'intent-7', order_ref: 'learn-ai/sid-x/v1:intent-7' },
        evidence_refs: [],
        rendered_headline: null,
        rendered_template_id: null,
        inserted_at_ms: 1_700_000_001_100,
        updated_at_ms: 1_700_000_001_100,
      },
    ],
  };
}

export function makeDesiredStateResponse(): SetInstanceDesiredStateResponse {
  return {
    durable: {
      state: 'PAUSED',
      updated_at_ms: 1_700_000_001_000,
      updated_by: 'operator',
      reason: 'test action accepted',
      version: 1,
    },
    actuation: {
      actuated: true,
      run_id: 'run-x',
      command_seq: 1,
      detail: 'Command accepted.',
    },
  };
}

export function makeCommandWriteResponse(verb: CommandVerb = 'MARK_POISONED'): CommandWriteResponse {
  return {
    accepted: true,
    command: {
      seq: 1,
      verb,
      status: 'queued',
      reason: null,
      issued_by: 'operator',
      queued_at_ms: 1_700_000_001_000,
      acked_at_ms: null,
      outcome: null,
      outcome_detail: null,
    },
  };
}

export function makeHostRunnerProcess(): HostRunnerHealth['process'] {
  return {
    state: 'running',
    run_id: 'run-x',
    pid: 42,
    started_at_ms: 1_700_000_001_000,
    ended_at_ms: null,
    exit_code: null,
    command: [],
    log_path: '/tmp/run-x.log',
    message: 'running',
  };
}

export function makeHostRunnerHealth(): HostRunnerHealth {
  return {
    ok: true,
    repo_root: '/repo',
    live_runs_root: '/runs',
    fetched_at_ms: 1_700_000_001_000,
    process: makeHostRunnerProcess(),
  };
}

export function makeReconcileAckResponse(): ReconcileAckResponse {
  return {
    request_id: 'reconcile-request-x',
    accepted_at_ms: 1_700_000_001_000,
  };
}

export function allowRenewControlPlaneLeaseCall(
  liveRuns: FakeLiveRunsService,
  response: HostRunnerHealth = makeHostRunnerHealth(),
): void {
  liveRuns.renewControlPlaneLease.mockResolvedValue(response);
}

export function allowStartHostRunnerCall(
  liveRuns: FakeLiveRunsService,
  response: HostRunnerActionResponse,
): void {
  liveRuns.startHostRunner.mockResolvedValue(response);
}

export function allowSetDesiredStateCall(
  liveRuns: FakeLiveRunsService,
  response: SetInstanceDesiredStateResponse = makeDesiredStateResponse(),
): void {
  liveRuns.setInstanceDesiredState.mockResolvedValue(response);
}

export function rejectSetDesiredStateCall(liveRuns: FakeLiveRunsService, error: unknown): void {
  liveRuns.setInstanceDesiredState.mockRejectedValue(error);
}

export function allowFlattenAndPauseCall(
  liveRuns: FakeLiveRunsService,
  response: SetInstanceDesiredStateResponse = makeDesiredStateResponse(),
): void {
  liveRuns.flattenAndPause.mockResolvedValue(response);
}

export function allowIssueInstanceCommandCall(
  liveRuns: FakeLiveRunsService,
  response: CommandWriteResponse = makeCommandWriteResponse(),
): void {
  liveRuns.issueInstanceCommand.mockResolvedValue(response);
}

export function allowReconcileInstanceCall(
  liveRuns: FakeLiveRunsService,
  response: ReconcileAckResponse = makeReconcileAckResponse(),
): void {
  liveRuns.reconcileInstance.mockResolvedValue(response);
}

export function rejectReconcileInstanceCall(liveRuns: FakeLiveRunsService, error: unknown): void {
  liveRuns.reconcileInstance.mockRejectedValue(error);
}

function unexpectedMutation(method: string): Error {
  return new Error(`${method} was invoked without an explicit Bot Control harness mutation override.`);
}

export function allowFlattenAndPause(status: LiveInstanceStatus): void {
  status.operator_surface.actions.flatten_and_pause = {
    enabled: true,
    effect: 'LIVE_ACTUATION',
    disabled_reason_code: null,
    disabled_reasons: [],
    gate_results: [],
  };
}

export function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

export async function flush(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }): Promise<void> {
  await fixture.whenStable();
  await Promise.resolve();
  fixture.detectChanges();
}

function installLocalStorageStub(): void {
  const store = new Map<string, string>();
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => store.set(key, value),
      removeItem: (key: string) => store.delete(key),
      clear: () => store.clear(),
    },
  });
}

export function installBotControlPageTestStubs(): void {
  installLocalStorageStub();
  TestBed.overrideComponent(BotControlPageComponent, {
    remove: { imports: [ActivityTabComponent, WorkbenchAuditPanelComponent] },
    add: { imports: [ActivityTabStubComponent, WorkbenchAuditPanelStubComponent] },
  });
}

type AsyncMockValue<T> = T | Promise<T>;

interface BotControlMutationResponses {
  renewControlPlaneLease?: HostRunnerHealth;
  startHostRunner?: HostRunnerActionResponse;
  setInstanceDesiredState?: SetInstanceDesiredStateResponse;
  flattenAndPause?: SetInstanceDesiredStateResponse;
  issueInstanceCommand?: CommandWriteResponse;
  reconcileInstance?: ReconcileAckResponse;
}

interface BotControlMutationFailures {
  setInstanceDesiredState?: unknown;
  reconcileInstance?: unknown;
}

// Harness convention: use this for ordinary Bot Control page wiring tests so
// route, status, account-summary, and lifecycle-timeline setup stays shared.
// Keep direct TestBed setup for sidebar-host integration, route-race subjects,
// or intentionally bespoke service sequencing. Prefer the typed read/mutation
// options below before using configureLiveRuns; mutations fail closed by
// default, and action tests must explicitly opt into the command they exercise.
export interface BotControlLiveRunsOptions {
  routeId?: string;
  status?: LiveInstanceStatus;
  statusSequence?: readonly AsyncMockValue<LiveInstanceStatus>[];
  statusResolver?: LiveRunsService['getInstanceStatus'];
  accountSummary?: FleetAccountSummary;
  accountSummarySequence?: readonly AsyncMockValue<FleetAccountSummary>[];
  lifecycleTimeline?: LifecycleTimelineResponse;
  lifecycleTimelineSequence?: readonly AsyncMockValue<LifecycleTimelineResponse>[];
  lifecycleTimelineFailure?: unknown;
  mutationResponses?: BotControlMutationResponses;
  mutationFailures?: BotControlMutationFailures;
  configureLiveRuns?: (liveRuns: FakeLiveRunsService) => void;
}

export interface BotControlPageSetupOptions extends BotControlLiveRunsOptions {
  routeParamMap$?: Observable<ParamMap>;
}

export interface BotControlPageHarness {
  fixture: ComponentFixture<BotControlPageComponent>;
  component: BotControlPageComponent;
  element: HTMLElement;
  liveRuns: FakeLiveRunsService;
}

export interface BotControlSidebarHostHarness {
  fixture: ComponentFixture<BotControlWithSidebarHostComponent>;
  element: HTMLElement;
  liveRuns: FakeLiveRunsService;
}

function applyReadSequence<T>(
  mock: {
    mockResolvedValue(value: AsyncMockValue<T>): unknown;
    mockResolvedValueOnce(value: AsyncMockValue<T>): unknown;
  },
  sequence: readonly AsyncMockValue<T>[] | undefined,
  fallback: T,
): void {
  if (!sequence?.length) {
    mock.mockResolvedValue(fallback);
    return;
  }
  for (const value of sequence) {
    mock.mockResolvedValueOnce(value);
  }
  mock.mockResolvedValue(sequence[sequence.length - 1]);
}

function applyMutationResponses(
  liveRuns: FakeLiveRunsService,
  responses: BotControlMutationResponses | undefined,
): void {
  if (!responses) return;
  if (responses.renewControlPlaneLease) {
    allowRenewControlPlaneLeaseCall(liveRuns, responses.renewControlPlaneLease);
  }
  if (responses.startHostRunner) {
    allowStartHostRunnerCall(liveRuns, responses.startHostRunner);
  }
  if (responses.setInstanceDesiredState) {
    allowSetDesiredStateCall(liveRuns, responses.setInstanceDesiredState);
  }
  if (responses.flattenAndPause) {
    allowFlattenAndPauseCall(liveRuns, responses.flattenAndPause);
  }
  if (responses.issueInstanceCommand) {
    allowIssueInstanceCommandCall(liveRuns, responses.issueInstanceCommand);
  }
  if (responses.reconcileInstance) {
    allowReconcileInstanceCall(liveRuns, responses.reconcileInstance);
  }
}

function hasOwn(object: object, property: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(object, property);
}

function applyMutationFailures(
  liveRuns: FakeLiveRunsService,
  failures: BotControlMutationFailures | undefined,
): void {
  if (!failures) return;
  if (hasOwn(failures, 'setInstanceDesiredState')) {
    rejectSetDesiredStateCall(liveRuns, failures.setInstanceDesiredState);
  }
  if (hasOwn(failures, 'reconcileInstance')) {
    rejectReconcileInstanceCall(liveRuns, failures.reconcileInstance);
  }
}

export function makeFailClosedLiveRuns(options: BotControlLiveRunsOptions = {}): FakeLiveRunsService {
  const routeId = options.routeId ?? 'sid-x';
  const liveRuns = new FakeLiveRunsService();
  if (options.statusResolver) {
    liveRuns.getInstanceStatus.mockImplementation(options.statusResolver);
  } else {
    applyReadSequence(
      liveRuns.getInstanceStatus,
      options.statusSequence,
      options.status ?? makeStatus({ id: routeId }),
    );
  }
  applyReadSequence(
    liveRuns.getAccountSummary,
    options.accountSummarySequence,
    options.accountSummary ?? makeAccountSummary(),
  );
  if (options.lifecycleTimelineFailure) {
    liveRuns.getLifecycleTimeline.mockRejectedValue(options.lifecycleTimelineFailure);
  } else {
    applyReadSequence(
      liveRuns.getLifecycleTimeline,
      options.lifecycleTimelineSequence,
      options.lifecycleTimeline ?? makeLifecycleTimeline(),
    );
  }
  liveRuns.renewControlPlaneLease.mockRejectedValue(unexpectedMutation('renewControlPlaneLease'));
  liveRuns.startHostRunner.mockRejectedValue(unexpectedMutation('startHostRunner'));
  liveRuns.setInstanceDesiredState.mockRejectedValue(unexpectedMutation('setInstanceDesiredState'));
  liveRuns.flattenAndPause.mockRejectedValue(unexpectedMutation('flattenAndPause'));
  liveRuns.issueInstanceCommand.mockRejectedValue(unexpectedMutation('issueInstanceCommand'));
  liveRuns.reconcileInstance.mockRejectedValue(unexpectedMutation('reconcileInstance'));
  applyMutationResponses(liveRuns, options.mutationResponses);
  applyMutationFailures(liveRuns, options.mutationFailures);
  options.configureLiveRuns?.(liveRuns);
  return liveRuns;
}

export async function setupBotControlPage(
  options: BotControlPageSetupOptions = {},
): Promise<BotControlPageHarness> {
  const routeId = options.routeId ?? 'sid-x';
  const liveRuns = makeFailClosedLiveRuns(options);

  TestBed.configureTestingModule({
    providers: [
      provideZonelessChangeDetection(),
      provideRouter([]),
      provideHttpClient(),
      provideHttpClientTesting(),
      {
        provide: ActivatedRoute,
        useValue: {
          paramMap: options.routeParamMap$ ?? of(convertToParamMap({ id: routeId })),
        },
      },
      { provide: LiveRunsService, useValue: liveRuns },
    ],
  });

  const fixture = TestBed.createComponent(BotControlPageComponent);
  fixture.detectChanges();
  await flush(fixture);
  return {
    fixture,
    component: fixture.componentInstance,
    element: fixture.nativeElement as HTMLElement,
    liveRuns,
  };
}

export async function setupBotControlSidebarHost(
  options: BotControlPageSetupOptions = {},
): Promise<BotControlSidebarHostHarness> {
  const routeId = options.routeId ?? 'sid-x';
  const liveRuns = makeFailClosedLiveRuns(options);

  TestBed.configureTestingModule({
    imports: [BotControlWithSidebarHostComponent],
    providers: [
      provideZonelessChangeDetection(),
      provideRouter([]),
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
      {
        provide: ActivatedRoute,
        useValue: {
          paramMap: options.routeParamMap$ ?? of(convertToParamMap({ id: routeId })),
        },
      },
      { provide: LiveRunsService, useValue: liveRuns },
    ],
  });

  const fixture = TestBed.createComponent(BotControlWithSidebarHostComponent);
  fixture.detectChanges();
  await flush(fixture);
  return {
    fixture,
    element: fixture.nativeElement as HTMLElement,
    liveRuns,
  };
}
