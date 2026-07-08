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
  CrashRecoveryOverrideResponse,
  FleetAccountSummary,
  LifecycleTimelineResponse,
  LiveInstanceStatus,
  SetInstanceDesiredStateResponse,
} from '../../../api/live-instances.types';
import type {
  CommandWriteResponse,
  HostRunnerActionResponse,
  HostRunnerHealth,
  ReconcileAckResponse,
} from '../../../api/live-runs.types';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerBannerComponent } from '../../../shell/broker-banner.component';
import { ActivityTabComponent } from './tabs/activity-tab.component';
import { BotEventStreamComponent } from './reused/bot-event-stream/bot-event-stream.component';
import { BotControlPageComponent } from './bot-control-page.component';
import { BotControlSidePanelComponent } from './bot-control-side-panel.component';
import { WorkbenchAuditPanelComponent } from './workbench-audit-panel.component';
import {
  makeAccountSummary,
  makeCommandWriteResponse,
  makeDesiredStateResponse,
  makeHostRunnerHealth,
  makeLifecycleTimeline,
  makeReconcileAckResponse,
  makeStatus,
} from './bot-control-page.fixtures';

@Component({
  selector: 'app-activity-tab',
  template: '<div data-testid="activity-tab-stub"></div>',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
class ActivityTabStubComponent {
  readonly status = input.required<LiveInstanceStatus>();
}

@Component({
  selector: 'app-bot-event-stream',
  template: '<div data-testid="bot-event-stream-stub">{{ runId() }}</div>',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
class BotEventStreamStubComponent {
  readonly runId = input.required<string>();
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
  recordCrashRecoveryOverride = vi.fn<LiveRunsService['recordCrashRecoveryOverride']>();
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

export function allowCrashRecoveryOverrideCall(
  liveRuns: FakeLiveRunsService,
  response: CrashRecoveryOverrideResponse,
): void {
  liveRuns.recordCrashRecoveryOverride.mockResolvedValue(response);
}

export function rejectReconcileInstanceCall(liveRuns: FakeLiveRunsService, error: unknown): void {
  liveRuns.reconcileInstance.mockRejectedValue(error);
}

function unexpectedMutation(method: string): Error {
  return new Error(`${method} was invoked without an explicit Bot Control harness mutation override.`);
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
  TestBed.overrideComponent(BotControlSidePanelComponent, {
    remove: { imports: [BotEventStreamComponent] },
    add: { imports: [BotEventStreamStubComponent] },
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
  recordCrashRecoveryOverride?: CrashRecoveryOverrideResponse;
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
  if (responses.recordCrashRecoveryOverride) {
    allowCrashRecoveryOverrideCall(liveRuns, responses.recordCrashRecoveryOverride);
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
  liveRuns.recordCrashRecoveryOverride.mockRejectedValue(unexpectedMutation('recordCrashRecoveryOverride'));
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
