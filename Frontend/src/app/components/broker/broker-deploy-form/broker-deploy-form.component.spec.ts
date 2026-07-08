import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AccountTruthResponse } from '../../../api/broker-models';
import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import { BrokerService } from '../../../services/broker.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { StrategyValidationService } from '../../../services/strategy-validation.service';
import type { StrategyValidationCatalog } from '../../../services/strategy-validation.types';
import type { ActionPlan } from '../../../api/action-plan.types';
import { BrokerDeployFormComponent } from './broker-deploy-form.component';

let activeFixture: { destroy(): void; detectChanges(): void } | null = null;

const DEPLOYMENT_VALIDATION_AUDIT_COPY = 'references/qc-shadow/DeploymentValidationAlgorithm.py';
const DEPLOYMENT_VALIDATION_SPEC_PATH =
  'PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json';
const DEPLOYMENT_VALIDATION_QC_BACKTEST_ID = 'd2fe45a7142e88575f6fbd75229f8681';
type AccountTruthFixture = Pick<AccountTruthResponse, 'final_verdict' | 'status_label' | 'status_detail'> &
  Partial<Pick<AccountTruthResponse, 'blockers' | 'evidence_gaps' | 'invariants' | 'source_freshness'>>;
const DEFAULT_STRATEGY_VALIDATION_CATALOG: StrategyValidationCatalog = {
  strategies: [
    {
      strategy_key: 'deployment_validation',
      display_name: 'Deployment Validation',
      description: '',
      validation_state: 'validated',
      deployable: true,
      settings_file_ref: DEPLOYMENT_VALIDATION_SPEC_PATH,
      settings_file_sha256: 'spec-sha',
      qc_cloud_backtest_id: DEPLOYMENT_VALIDATION_QC_BACKTEST_ID,
      audit_copy_ref: DEPLOYMENT_VALIDATION_AUDIT_COPY,
      audit_copy_sha256: 'audit-sha',
      reconciliation_ref: 'references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md',
      validation_case_symbol: 'SPY',
      reconciliation_status: 'passed',
      diagnostics: {
        verdict: 'passed',
        trades_matched: 56,
        trades_validated: 56,
        pnl_max_abs_diff: '0.00',
        divergence_counts: {},
        notes: [],
      },
      behavioral_equivalence: {
        verdict: 'accepted_for_deploy',
        detail: 'Human validation accepted the current engine evidence for deployment.',
      },
      current_flag_event: null,
      flag_events: [],
    },
    {
      strategy_key: 'spy_orb',
      display_name: 'Opening Range Breakout',
      description: '',
      validation_state: 'needs_validation',
      deployable: false,
      settings_file_ref: null,
      settings_file_sha256: null,
      qc_cloud_backtest_id: null,
      audit_copy_ref: null,
      audit_copy_sha256: null,
      reconciliation_ref: null,
      validation_case_symbol: null,
      reconciliation_status: null,
      diagnostics: null,
      behavioral_equivalence: null,
      current_flag_event: null,
      flag_events: [],
    },
  ],
};

function setup(
  opts: {
    daemonDown?: boolean;
    fleetBlocks?: boolean;
    qcEntries?: string[];
    instances?: { strategy_instance_id: string; process_state: string }[];
    instancesPromise?: Promise<{ strategy_instance_id: string; process_state: string }[]>;
    parityGate?: {
      verdict: 'proven_match' | 'proven_mismatch' | 'cannot_prove';
      detail: string;
      expected_rule: unknown;
      actual_rule: unknown;
    };
    positions?: { symbol: string; quantity: number }[];
    queryParams?: Record<string, string>;
    strategies?: {
      name: string;
      display_name: string;
      description: string;
      sizing_surface: 'policy' | 'explicit';
    }[];
    specFixtures?: {
      name: string;
      spec_name: string;
      path: string;
      symbols: string[];
      description: string | null;
    }[];
    accountPromise?: Promise<{ account_id: string } | null>;
    accountTruth?: AccountTruthFixture;
    instanceStatus?: LiveInstanceStatus | null;
    instanceStatusPromise?: Promise<LiveInstanceStatus | null>;
    instanceStatusError?: Error;
    instanceStatusResolver?: (instanceId: string) => Promise<LiveInstanceStatus | null>;
    strategyValidationCatalog?: StrategyValidationCatalog;
  } = {},
) {
  const svc = {
    getEngineStrategies: vi.fn().mockResolvedValue(
      opts.strategies ?? [
        {
          name: 'spy_ema_crossover',
          display_name: 'SPY EMA Crossover',
          description: '',
          sizing_surface: 'policy',
        },
        {
          name: 'deployment_validation',
          display_name: 'Deployment Validation',
          description: '',
          sizing_surface: 'policy',
        },
        {
          name: 'spy_ema_crossover_options',
          display_name: 'EMA Crossover Options',
          description: '',
          sizing_surface: 'explicit',
        },
      ],
    ),
    getSpecStrategyFixtures: vi.fn().mockResolvedValue([
      ...(opts.specFixtures ?? [
        {
          name: 'spy_ema_crossover',
          spec_name: 'SPY EMA Crossover',
          path: 'PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json',
          symbols: ['SPY'],
          description: null,
        },
        {
          name: 'deployment_validation',
          spec_name: 'Deployment Validation',
          path: DEPLOYMENT_VALIDATION_SPEC_PATH,
          symbols: ['SPY'],
          description: null,
        },
      ]),
    ]),
    getQcAuditCopies: vi
      .fn()
      .mockResolvedValue({
        scope_root: 'references/qc-shadow',
        entries: opts.qcEntries ?? ['references/qc-shadow/A.py'],
      }),
    getInstances: vi.fn().mockImplementation(() => opts.instancesPromise ?? Promise.resolve(opts.instances ?? [])),
    getInstanceStatus: vi.fn().mockImplementation((instanceId: string) => {
      if (opts.instanceStatusResolver !== undefined) return opts.instanceStatusResolver(instanceId);
      if (opts.instanceStatusError !== undefined) return Promise.reject(opts.instanceStatusError);
      if (opts.instanceStatusPromise !== undefined) return opts.instanceStatusPromise;
      if (opts.instanceStatus !== undefined) return Promise.resolve(opts.instanceStatus);
      return Promise.resolve(clearStartStatus());
    }),
    deployInstance: vi
      .fn()
      .mockResolvedValue({ run_id: 'run-new', run_dir: '/runs/run-new', created: true, start: null }),
    getAuditCopySizingLookup: vi.fn().mockResolvedValue(
      opts.parityGate ?? {
        verdict: 'cannot_prove',
        detail: 'allow-list unavailable in tests',
        expected_rule: null,
        actual_rule: null,
      },
    ),
  };
  const broker = {
    account: vi.fn().mockReturnValue(opts.accountPromise ?? Promise.resolve({ account_id: 'DU123' })),
    accountTruth: vi.fn().mockResolvedValue(
      opts.accountTruth ?? {
        final_verdict: 'clean',
        status_label: 'Clean',
        status_detail: 'Broker/account evidence is fresh enough to start.',
      },
    ),
    positions: vi
      .fn()
      .mockResolvedValue({ positions: opts.positions ?? [] }),
  };
  const connectivity = {
    links: () => [],
    blockers: () => [],
    daemonState: () => (opts.daemonDown ? 'down' : 'ok'),
    brokerState: () => 'ok',
    brokerDetail: () => 'Connected',
    fleetState: () => (opts.fleetBlocks ? 'warn' : 'ok'),
    nothingDeployed: () => false,
    daemonDown: () => opts.daemonDown ?? false,
    fleetBlocksStarts: () => opts.fleetBlocks ?? false,
    daemonFreshness: () => ({ state: 'unknown', sha: null, commitsBehind: null }),
    reload: vi.fn(),
  };
  const strategyValidation = {
    getCatalog: vi.fn().mockResolvedValue(
      opts.strategyValidationCatalog ?? DEFAULT_STRATEGY_VALIDATION_CATALOG,
    ),
  };
  const queryParamMap = convertToParamMap(opts.queryParams ?? {});
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: LiveRunsService, useValue: svc },
      { provide: BrokerService, useValue: broker },
      { provide: BrokerConnectivityService, useValue: connectivity },
      { provide: StrategyValidationService, useValue: strategyValidation },
      {
        provide: ActivatedRoute,
        useValue: { snapshot: { queryParamMap } },
      },
    ],
  });
  const fixture = TestBed.createComponent(BrokerDeployFormComponent);
  activeFixture = fixture;
  fixture.detectChanges();
  return { fixture, svc, component: fixture.componentInstance };
}

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  TestBed.flushEffects();
  await Promise.resolve();
}

async function settleResource(fixture: { detectChanges(): void }) {
  fixture.detectChanges();
  await flush();
  fixture.detectChanges();
  await flush();
  fixture.detectChanges();
}

function validDeploymentValidationActionPlan(): ActionPlan {
  return {
    on_enter: [
      {
        leg_id: 'spy_long',
        instrument: { kind: 'stock', underlying: 'SPY' },
        position: 'long',
        qty_ratio: 1,
      },
    ],
    on_exit: [{ kind: 'close_leg', entry_leg_id: 'spy_long' }],
  };
}

function fillRequired(component: BrokerDeployFormComponent) {
  component.strategyKey.set('deployment_validation');
  component.specPath.set(DEPLOYMENT_VALIDATION_SPEC_PATH);
  component.signalStream.set('SPY');
  component.accountId.set('DU123');
  component.qcBacktestId.set(DEPLOYMENT_VALIDATION_QC_BACKTEST_ID);
  component.qcAuditCopyPath.set(DEPLOYMENT_VALIDATION_AUDIT_COPY);
  component.instanceId.set('deployment-validation-paper');
  component.actionPlan.set(validDeploymentValidationActionPlan());
  component.startNow.set(false);
  activeFixture?.detectChanges();
}

function stoppedLatchStatus(): LiveInstanceStatus {
  return {
    desired_state: {
      state: 'STOPPED',
      updated_at_ms: 1_781_000_000_000,
      updated_by: 'operator',
      reason: 'stop_command',
      version: 3,
      path_status: 'ok',
    },
    operator_surface: {
      host_process: {
        start_capability: {
          enabled: false,
          run_id: null,
          request: null,
          disabled_reason_code: 'STOPPED_REQUIRES_RESUME',
          gate_results: [
            {
              gate_id: 'desired_state.start',
              status: 'block',
              source: 'desired_state',
              operator_reason: 'STOPPED',
              operator_next_step: 'STOPPED_REQUIRES_RESUME',
              evidence_at_ms: 1_781_000_000_000,
            },
          ],
        },
      },
    },
  } as unknown as LiveInstanceStatus;
}

function clearStartStatus(): LiveInstanceStatus {
  return {
    desired_state: {
      state: 'RUNNING',
      updated_at_ms: 1_781_000_000_000,
      updated_by: 'operator',
      reason: null,
      version: 4,
      path_status: 'ok',
    },
    operator_surface: {
      host_process: {
        start_capability: {
          enabled: true,
          run_id: 'run-minute',
          request: null,
          disabled_reason_code: null,
          gate_results: [],
        },
      },
    },
  } as unknown as LiveInstanceStatus;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function deployButton(fixture: { nativeElement: HTMLElement }): HTMLButtonElement {
  const el = fixture.nativeElement.querySelector('button.primary');
  if (!(el instanceof HTMLButtonElement)) throw new Error('no submit button');
  return el;
}

function fieldControl(
  fixture: { nativeElement: HTMLElement },
  labelText: string,
): HTMLInputElement | HTMLSelectElement {
  const labels = Array.from(fixture.nativeElement.querySelectorAll<HTMLLabelElement>('label.field'));
  const label = labels.find((candidate) =>
    candidate.querySelector('span')?.textContent?.includes(labelText),
  );
  const control = label?.querySelector('input, select');
  if (control instanceof HTMLInputElement || control instanceof HTMLSelectElement) {
    return control;
  }
  throw new Error(`missing field control: ${labelText}`);
}

function changeSelect(
  fixture: { nativeElement: HTMLElement },
  labelText: string,
  value: string,
): void {
  const control = fieldControl(fixture, labelText);
  if (!(control instanceof HTMLSelectElement)) throw new Error(`${labelText} is not a select`);
  control.value = value;
  control.dispatchEvent(new Event('change'));
}

function typeText(
  fixture: { nativeElement: HTMLElement },
  labelText: string,
  value: string,
): void {
  const control = fieldControl(fixture, labelText);
  if (!(control instanceof HTMLInputElement)) throw new Error(`${labelText} is not an input`);
  control.value = value;
  control.dispatchEvent(new Event('input'));
}

function chooseExecutionCapability(
  fixture: { nativeElement: HTMLElement },
  value: 'read_only' | 'paper_orders' | 'live',
): void {
  const control = fixture.nativeElement.querySelector<HTMLInputElement>(
    `input[name="launch-mode"][value="${value}"]`,
  );
  if (!(control instanceof HTMLInputElement)) throw new Error(`missing execution capability: ${value}`);
  control.checked = true;
  control.dispatchEvent(new Event('change'));
}

afterEach(() => {
  activeFixture?.destroy();
  activeFixture = null;
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('BrokerDeployFormComponent', () => {
  it('lists only validated strategies and auto-populates provenance read-only', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    const strategyOptions = Array.from(
      fieldControl(fixture, 'Strategy').querySelectorAll('option'),
    ).map((option) => option.textContent ?? '');
    expect(strategyOptions).toContain('Deployment Validation');
    expect(strategyOptions).not.toContain('Opening Range Breakout');

    changeSelect(fixture, 'Strategy', 'deployment_validation');
    await flush();
    fixture.detectChanges();

    expect(component.specPath()).toBe(DEPLOYMENT_VALIDATION_SPEC_PATH);
    expect(component.qcBacktestId()).toBe('d2fe45a7142e88575f6fbd75229f8681');
    expect(component.qcAuditCopyPath()).toBe(DEPLOYMENT_VALIDATION_AUDIT_COPY);
    expect(component.signalStream()).toBe('SPY');
    expect(fieldControl(fixture, 'Backtest ID')).toHaveProperty('readOnly', true);
    expect(fieldControl(fixture, 'Algorithm audit copy')).toHaveProperty('readOnly', true);
    expect(fixture.nativeElement.textContent).toContain('View full validation');
  });

  it('defaults launch to paper orders with the 2000-order guardrail', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    expect(component.startNow()).toBe(true);
    expect(component.executionCapability()).toBe('paper_orders');
    expect(component.readonlyFlag()).toBe(false);
    expect(component.maxOrdersPerDay()).toBe(2000);
    expect(fixture.nativeElement.textContent).toContain('PAPER_ORDERS_ENABLED');
    expect(fixture.nativeElement.textContent).toContain('Guardrail limit: 2000 orders/day');
    expect(deployButton(fixture).textContent).toContain('Deploy & start');
  });

  it('renders the deploy top strip, free-navigation tabs, and named readiness facts', async () => {
    const { fixture } = setup();
    await flush();
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    const topStrip = host.querySelector('.deploy-top-strip');
    expect(topStrip?.textContent).toContain('Deployment name');
    expect(topStrip?.textContent).toContain('Connected broker account');

    const tabs = Array.from(
      host.querySelectorAll<HTMLElement>('.form-tabs [role="tab"]'),
    ).map((tab) => tab.textContent ?? '');
    expect(tabs).toEqual(
      expect.arrayContaining([
        expect.stringContaining('Strategy'),
        expect.stringContaining('Signal stream'),
        expect.stringContaining('Sizing'),
        expect.stringContaining('Legs'),
        expect.stringContaining('Launch'),
      ]),
    );

    const readiness = host.querySelector('.deploy-readiness-strip');
    expect(readiness?.textContent).toContain('Engine');
    expect(readiness?.textContent).toContain('Broker');
    expect(readiness?.textContent).toContain('Account');
    expect(readiness?.textContent).toContain('Fleet');
  });

  it('reveals sizing controls when the Sizing step is selected', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    const sizingTab = host.querySelector<HTMLButtonElement>(
      '.form-tabs button[aria-controls="sizing-section"]',
    );
    expect(sizingTab).toBeTruthy();
    expect(sizingTab?.getAttribute('href')).toBeNull();

    sizingTab?.click();
    fixture.detectChanges();

    const sizingGroup = host.querySelector('#sizing-section')?.closest('.group');
    const strategyGroup = host.querySelector('#strategy-section')?.closest('.group');
    expect(component.activeDeployTab()).toBe('sizing');
    expect(sizingGroup?.classList.contains('step-hidden')).toBe(false);
    expect(strategyGroup?.classList.contains('step-hidden')).toBe(true);
    expect(sizingGroup?.querySelector('.sizing-presets')).toBeTruthy();
  });

  it('submits a deploy request with the collected fields and reports success', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    fixture.detectChanges();

    await component.submit();
    fixture.detectChanges();

    expect(svc.deployInstance).toHaveBeenCalledTimes(1);
    const req = svc.deployInstance.mock.calls[0][0];
    expect(req).toMatchObject({
      strategy_spec_path: DEPLOYMENT_VALIDATION_SPEC_PATH,
      qc_audit_copy_path: DEPLOYMENT_VALIDATION_AUDIT_COPY,
      qc_cloud_backtest_id: DEPLOYMENT_VALIDATION_QC_BACKTEST_ID,
      strategy_instance_id: 'deployment-validation-paper',
      strategy_key: 'deployment_validation',
      start: false,
    });
    expect(req).not.toHaveProperty('account_id');
    expect(typeof req.start_date_ms).toBe('number');
    // Deploy-only: launch knobs are omitted so they aren't validated.
    expect(req.start_options).toBeUndefined();
    expect(fixture.nativeElement.textContent).toContain('Deployment created');
    expect(fixture.nativeElement.textContent).toContain('run-new');
    expect(fixture.nativeElement.querySelector('.back')?.getAttribute('href')).toBe('/broker/bots');
    expect(fixture.nativeElement.querySelector('.goto')?.getAttribute('href')).toBe(
      '/broker/bots/deployment-validation-paper',
    );
  });

  it('shows a coherent accepted-start state after the submitted instance becomes running', async () => {
    const { fixture, svc, component } = setup();
    svc.deployInstance.mockResolvedValueOnce({
      run_id: 'run-started',
      run_dir: '/runs/run-started',
      created: true,
      start: {
        accepted: true,
        process: { state: 'running', message: 'Host runner process is active.' },
      },
    });
    svc.getInstances.mockResolvedValueOnce([
      { strategy_instance_id: 'deployment-validation-paper', process_state: 'running' },
    ]);
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    fixture.detectChanges();

    await component.submit();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Deployment created');
    expect(text).toContain('Start accepted: Host runner process is active.');
    expect(text).toContain('Start accepted for run run-started. View deployment to monitor the live run.');
    expect(text).not.toContain('"deployment-validation-paper" is already running');
    expect(component.commandState().kind).toBe('accepted');
    expect(component.blockedReason()).toBeNull();
    expect(deployButton(fixture).disabled).toBe(true);

    component.startNow.set(false);
    fixture.detectChanges();

    expect(component.commandState().kind).toBe('ready');
    expect(deployButton(fixture).disabled).toBe(false);

    await component.submit();

    const deployOnlyRetry = svc.deployInstance.mock.calls[1][0];
    expect(deployOnlyRetry.start).toBe(false);
    expect(deployOnlyRetry.start_options).toBeUndefined();
  });

  // PRD #593 Slice 1E (#598) — query-param-deep-linked redeploy carries
  // the parent_run_id at the top level of the submit payload (NOT
  // inside live_config — lineage is unhashed).
  it('flows parent_run_id from the route query param into the submit payload at the top level', async () => {
    const { svc, component } = setup({ queryParams: { parent_run_id: 'run-parent-abc' } });
    await flush();
    fillRequired(component);

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.parent_run_id).toBe('run-parent-abc');
    // Belt-and-braces: lineage MUST NOT sneak into live_config — it's
    // unhashed (ADR 0012 §7 / Slice 1E load-bearing invariant).
    expect(Object.keys(req.live_config ?? {})).not.toContain('parent_run_id');
  });

  // PRD #593 Slice 1B (#595) — the deploy form carries the operator-
  // declared action plan into ``live_config.action``.
  it('blocks deployment-validation submit when the operator declared no legs', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    component.actionPlan.set({ on_enter: [], on_exit: [] });
    fixture.detectChanges();

    await component.submit();

    expect(component.blockedReason()).toContain('ON ENTER and ON EXIT are both empty');
    expect(component.deployTabs().find((tab) => tab.key === 'legs')?.complete).toBe(false);
    expect(deployButton(fixture).disabled).toBe(true);
    expect(svc.deployInstance).not.toHaveBeenCalled();
  });

  it('submits the operator-built stock plan via live_config.action', async () => {
    const { svc, component } = setup();
    await flush();
    fillRequired(component);

    component.actionPlan.set({
      on_enter: [
        {
          leg_id: 'spy_long',
          instrument: { kind: 'stock', underlying: 'SPY' },
          position: 'long',
          qty_ratio: 1,
        },
      ],
      on_exit: [{ kind: 'close_leg', entry_leg_id: 'spy_long' }],
    });

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config?.action).toEqual({
      on_enter: [
        {
          leg_id: 'spy_long',
          instrument: { kind: 'stock', underlying: 'SPY' },
          position: 'long',
          qty_ratio: 1,
        },
      ],
      on_exit: [{ kind: 'close_leg', entry_leg_id: 'spy_long' }],
    });
  });

  it('defaults the sizing preset to Safe canary and submits FixedShares(1)', async () => {
    const { svc, component } = setup();
    await flush();
    fillRequired(component);
    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config).toEqual({
      symbol: 'SPY',
      sizing: { kind: 'FixedShares', value: 1 },
      action: validDeploymentValidationActionPlan(),
    });
    expect(component.sizingPreset()).toBe('safe_canary');
  });

  it('enables Reference parity only when the gate returns proven_match', async () => {
    const { component } = setup({
      parityGate: {
        verdict: 'proven_match',
        detail: 'audit copy proves SetHoldings(1.0) — Reference parity available',
        expected_rule: { kind: 'SetHoldings', fraction: '1.0' },
        actual_rule: null,
      },
    });
    component.qcAuditCopyPath.set('references/qc-shadow/A.py');
    await flush();
    await flush();
    await flush();

    expect(component.referenceParityAvailable()).toBe(true);
  });

  it('queries the parity gate with the Reference-parity policy so the registered rule is compared against the preset', async () => {
    const { component, svc } = setup();
    component.qcAuditCopyPath.set('references/qc-shadow/A.py');
    await flush();
    await flush();
    await flush();

    expect(svc.getAuditCopySizingLookup).toHaveBeenCalledWith(
      'references/qc-shadow/A.py',
      { kind: 'SetHoldings', fraction: '1.0' },
    );
  });

  it('submits StrategyExplicit and disables the sizing selector for an explicit-surface strategy', async () => {
    const { svc, component, fixture } = setup({
      strategyValidationCatalog: {
        strategies: [
          {
            ...DEFAULT_STRATEGY_VALIDATION_CATALOG.strategies[0],
            strategy_key: 'spy_ema_crossover_options',
            display_name: 'EMA Crossover Options',
            settings_file_ref: 'PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json',
          },
        ],
      },
    });
    await flush();
    component.strategyKey.set('spy_ema_crossover_options');
    component.specPath.set('PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json');
    component.signalStream.set('SPY');
    component.accountId.set('DU123');
    component.qcBacktestId.set('bt-1');
    component.qcAuditCopyPath.set('references/qc-shadow/A.py');
    component.instanceId.set('opt-paper-1');
    component.startNow.set(false);
    fixture.detectChanges();
    await flush();

    expect(component.sizingSurfaceIsExplicit()).toBe(true);
    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config).toEqual({
      symbol: 'SPY',
      sizing: { kind: 'StrategyExplicit' },
      action: { on_enter: [], on_exit: [] },
    });
  });

  it('emits a Custom FixedShares policy from the kind/value inputs', async () => {
    const { svc, component } = setup();
    await flush();
    fillRequired(component);
    component.sizingPreset.set('custom');
    component.customKind.set('FixedShares');
    component.customValue.set('25');

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config).toEqual({
      symbol: 'SPY',
      sizing: { kind: 'FixedShares', value: 25 },
      action: validDeploymentValidationActionPlan(),
    });
  });

  it('emits a Custom FixedNotional policy with the value as a decimal string', async () => {
    const { svc, component } = setup();
    await flush();
    fillRequired(component);
    component.sizingPreset.set('custom');
    component.customKind.set('FixedNotional');
    component.customValue.set('1500.50');

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config).toEqual({
      symbol: 'SPY',
      sizing: { kind: 'FixedNotional', value: '1500.50' },
      action: validDeploymentValidationActionPlan(),
    });
  });

  it('rejects Custom FixedShares values that parseInt would silently truncate', async () => {
    const { component } = setup();
    await flush();
    fillRequired(component);
    component.sizingPreset.set('custom');
    component.customKind.set('FixedShares');

    for (const bad of ['1.9', '25abc', '-3', '0', ' 5 5', '']) {
      component.customValue.set(bad);
      expect(component.blockedReason()).not.toBeNull();
      expect(component.canSubmit()).toBe(false);
    }
    component.customValue.set('25');
    expect(component.customSizingError()).toBeNull();
  });

  it('keeps the submit path safe when Custom sizing is invalid (no busy wedge)', async () => {
    const { svc, component } = setup();
    await flush();
    fillRequired(component);
    component.sizingPreset.set('custom');
    component.customKind.set('FixedNotional');
    component.customValue.set('not-a-number');

    // submit() short-circuits via canSubmit(); it never throws or sets busy.
    await component.submit();
    expect(svc.deployInstance).not.toHaveBeenCalled();
    expect(component.busy()).toBe(false);
  });

  it('blocks Reference parity when the strategy symbol already has exposure', async () => {
    const { fixture, component } = setup({
      parityGate: {
        verdict: 'proven_match',
        detail: 'audit copy proves SetHoldings(1.0)',
        expected_rule: { kind: 'SetHoldings', fraction: '1.0' },
        actual_rule: null,
      },
      positions: [{ symbol: 'SPY', quantity: 37 }],
    });
    component.qcAuditCopyPath.set('references/qc-shadow/A.py');
    fillRequired(component);
    await flush();
    await flush();
    await flush();
    component.sizingPreset.set('reference_parity');
    fixture.detectChanges();

    expect(component.blockedReason()).toMatch(/already holds 37/);
  });

  it('refuses to switch to Reference parity when the gate is cannot_prove', async () => {
    const { component } = setup({
      parityGate: {
        verdict: 'cannot_prove',
        detail: 'audit copy is not registered in the allow-list',
        expected_rule: null,
        actual_rule: null,
      },
    });
    await flush();
    component.qcAuditCopyPath.set('references/qc-shadow/Unregistered.py');
    await flush();

    // Simulate a stray check on the disabled radio.
    const evt = { target: { value: 'reference_parity' } } as unknown as Event;
    Object.setPrototypeOf(evt.target as object, HTMLInputElement.prototype);
    component.setSizingPreset(evt);

    expect(component.sizingPreset()).toBe('safe_canary');
  });

  it('attaches start_options only when "Start now" is checked', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    await settleResource(fixture);

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.start).toBe(true);
    expect(req.start_options.strategy).toBe('deployment_validation');
    expect(req.start_options.max_orders_per_day).toBe(2000);
  });

  it('locks deployment validation provenance when the daemon listing is empty', async () => {
    const { fixture, component } = setup({ qcEntries: [] });
    await flush();
    fixture.detectChanges();

    component.strategyKey.set('deployment_validation');
    await flush();
    fixture.detectChanges();

    expect(component.qcAuditCopyPath()).toBe(DEPLOYMENT_VALIDATION_AUDIT_COPY);
    expect(component.specPath()).toBe(DEPLOYMENT_VALIDATION_SPEC_PATH);
    expect(component.maxOrdersPerDay()).toBe(2000);
    expect(fieldControl(fixture, 'Algorithm audit copy')).toHaveProperty('readOnly', true);
  });

  it('auto-selects the deployment validation spec after the operator previously used manual spec mode', async () => {
    const { fixture, component } = setup({ qcEntries: [] });
    await flush();
    component.useManualSpecPath();
    component.specPath.set('custom/manual.spec.json');
    fixture.detectChanges();

    changeSelect(fixture, 'Strategy', 'deployment_validation');
    await flush();
    fixture.detectChanges();

    expect(component.manualSpecPath()).toBe(false);
    expect(component.specPath()).toBe(DEPLOYMENT_VALIDATION_SPEC_PATH);
    expect(component.qcAuditCopyPath()).toBe(DEPLOYMENT_VALIDATION_AUDIT_COPY);
  });

  it('clears the missing-fields message when required fields are filled through the rendered controls', async () => {
    const { fixture, component } = setup({ qcEntries: [] });
    await flush();
    fixture.detectChanges();

    changeSelect(fixture, 'Strategy', 'deployment_validation');
    await flush();
    fixture.detectChanges();
    typeText(fixture, 'Signal stream', 'SPY');
    typeText(fixture, 'Deployment name', 'deployment-validation-paper');
    component.actionPlan.set(validDeploymentValidationActionPlan());
    await settleResource(fixture);

    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Ready to deploy.',
    );
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('syncs visibly filled controls even when no individual field event reached the signal setters', async () => {
    const { fixture, component } = setup({ qcEntries: [] });
    await flush();
    fixture.detectChanges();

    fieldControl(fixture, 'Strategy').value = 'deployment_validation';
    fieldControl(fixture, 'Connected broker account').value = 'DU123';
    fieldControl(fixture, 'Signal stream').value = 'SPY';
    fieldControl(fixture, 'Backtest ID').value = DEPLOYMENT_VALIDATION_QC_BACKTEST_ID;
    fieldControl(fixture, 'Algorithm audit copy').value = DEPLOYMENT_VALIDATION_AUDIT_COPY;
    fieldControl(fixture, 'Deployment name').value = 'deployment-validation-paper';

    component.syncRenderedFieldValues();
    component.actionPlan.set(validDeploymentValidationActionPlan());
    await settleResource(fixture);

    expect(component.strategyKey()).toBe('deployment_validation');
    expect(component.specPath()).toBe(DEPLOYMENT_VALIDATION_SPEC_PATH);
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Ready to deploy.',
    );
  });

  it('imports visibly restored fields even when one signal was already prefilled', async () => {
    const { fixture, component } = setup({
      accountPromise: Promise.resolve({ account_id: 'DUM284968' }),
      qcEntries: [],
    });
    await flush();
    fixture.detectChanges();

    fieldControl(fixture, 'Strategy').value = 'deployment_validation';
    fieldControl(fixture, 'Signal stream').value = 'SPY';
    fieldControl(fixture, 'Backtest ID').value = DEPLOYMENT_VALIDATION_QC_BACKTEST_ID;
    fieldControl(fixture, 'Algorithm audit copy').value = DEPLOYMENT_VALIDATION_AUDIT_COPY;
    fieldControl(fixture, 'Deployment name').value = 'june25';

    component.syncRenderedFieldValues({ includeEmpty: false, onlyEmptySignals: true });
    component.actionPlan.set(validDeploymentValidationActionPlan());
    await settleResource(fixture);

    expect(component.strategyKey()).toBe('deployment_validation');
    expect(component.specPath()).toBe(DEPLOYMENT_VALIDATION_SPEC_PATH);
    expect(component.accountId()).toBe('DUM284968');
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Ready to deploy.',
    );
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('fails closed when the connected broker account is unavailable', async () => {
    const { fixture } = setup({
      accountPromise: Promise.reject(new Error('broker account unavailable')),
      qcEntries: [],
    });
    await flush();
    fixture.detectChanges();

    changeSelect(fixture, 'Strategy', 'deployment_validation');
    await flush();
    fixture.detectChanges();
    typeText(fixture, 'Signal stream', 'SPY');
    typeText(fixture, 'Deployment name', 'june25');
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Connected broker account unavailable.',
    );
    expect(deployButton(fixture).disabled).toBe(true);
  });

  it('syncs visibly cleared controls so stale values cannot be submitted', async () => {
    const { fixture, component } = setup();
    await flush();
    fillRequired(component);

    fieldControl(fixture, 'Strategy').value = '';
    fieldControl(fixture, 'Strategy settings file').value = '';
    fieldControl(fixture, 'Backtest ID').value = '';
    fieldControl(fixture, 'Algorithm audit copy').value = '';
    fieldControl(fixture, 'Deployment name').value = '';

    component.syncRenderedFieldValues();
    await flush();
    fixture.detectChanges();

    expect(component.strategyKey()).toBe('');
    expect(component.specPath()).toBe('');
    expect(component.accountId()).toBe('DU123');
    expect(component.qcBacktestId()).toBe('');
    expect(component.qcAuditCopyPath()).toBe('');
    expect(component.instanceId()).toBe('');
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Missing: Strategy, Strategy settings file, Backtest ID, Algorithm audit copy, Deployment name.',
    );
    expect(deployButton(fixture).disabled).toBe(true);
  });

  it('ignores manual account edits and uses the connected broker account when it resolves', async () => {
    let resolveAccount: (value: { account_id: string }) => void = () => undefined;
    const accountPromise = new Promise<{ account_id: string }>((resolve) => {
      resolveAccount = resolve;
    });
    const { fixture, component } = setup({ accountPromise });
    fixture.detectChanges();

    typeText(fixture, 'Connected broker account', 'DU999');
    resolveAccount({ account_id: 'DU123' });
    await flush();

    expect(component.accountId()).toBe('DU123');
  });

  it('lists the missing required fields in the blocked reason', async () => {
    const { fixture, component } = setup({ qcEntries: [] });
    await flush();
    component.strategyKey.set('deployment_validation');
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Missing: Deployment name.',
    );
  });

  it('submits the selected signal stream separately from the traded action-plan instrument', async () => {
    const { svc, component, fixture } = setup();
    await flush();
    fillRequired(component);
    typeText(fixture, 'Signal stream', 'SPY');
    component.actionPlan.set({
      on_enter: [
        {
          leg_id: 'aapl_long',
          instrument: { kind: 'stock', underlying: 'AAPL' },
          position: 'long',
          qty_ratio: 1,
        },
      ],
      on_exit: [{ kind: 'close_leg', entry_leg_id: 'aapl_long' }],
    });

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config?.symbol).toBe('SPY');
    expect(req.live_config?.action).toEqual({
      on_enter: [
        {
          leg_id: 'aapl_long',
          instrument: { kind: 'stock', underlying: 'AAPL' },
          position: 'long',
          qty_ratio: 1,
        },
      ],
      on_exit: [{ kind: 'close_leg', entry_leg_id: 'aapl_long' }],
    });
  });

  it('requires an explicit signal stream for multi-symbol fixtures instead of picking the first one', async () => {
    const { fixture, component } = setup({
      strategies: [
        {
          name: 'multi_signal',
          display_name: 'Multi Signal',
          description: '',
          sizing_surface: 'policy',
        },
      ],
      specFixtures: [
        {
          name: 'multi_signal',
          spec_name: 'Multi Signal',
          path: 'PythonDataService/app/engine/strategy/spec/fixtures/multi_signal.spec.json',
          symbols: ['spy', 'SPY', 'QQQ'],
          description: null,
        },
      ],
      strategyValidationCatalog: {
        strategies: [
          {
            ...DEFAULT_STRATEGY_VALIDATION_CATALOG.strategies[0],
            strategy_key: 'multi_signal',
            display_name: 'Multi Signal',
            settings_file_ref: 'PythonDataService/app/engine/strategy/spec/fixtures/multi_signal.spec.json',
            validation_case_symbol: null,
          },
        ],
      },
    });
    await flush();

    changeSelect(fixture, 'Strategy', 'multi_signal');
    await flush();
    fixture.detectChanges();

    expect(component.fixtureSymbols()).toEqual(['SPY', 'QQQ']);
    expect(component.resolvedSignalStream()).toBe('');
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain('Signal stream');

    typeText(fixture, 'Signal stream', 'QQQ');
    fixture.detectChanges();

    expect(component.resolvedSignalStream()).toBe('QQQ');
  });

  it('starts with paper order submission enabled without an extra modal', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    await settleResource(fixture);
    chooseExecutionCapability(fixture, 'paper_orders');
    fixture.detectChanges();

    await component.submit();
    fixture.detectChanges();

    expect(svc.deployInstance).toHaveBeenCalledTimes(1);
    expect(fixture.nativeElement.textContent).not.toContain('Enable paper order submission?');
    expect(fixture.nativeElement.textContent).toContain('PAPER_ORDERS_ENABLED');
    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.start_options.readonly).toBe(false);
    expect(req.start_options.max_orders_per_day).toBe(2000);
  });

  it('shows live execution as an option but blocks submit before the request boundary', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    fixture.detectChanges();

    chooseExecutionCapability(fixture, 'live');
    fixture.detectChanges();

    expect(component.executionCapability()).toBe('live');
    expect(component.readonlyFlag()).toBe(false);
    expect(component.blockedReason()).toContain('Live execution is not available');
    expect(deployButton(fixture).disabled).toBe(true);

    await component.submit();

    expect(svc.deployInstance).not.toHaveBeenCalled();
  });

  it('reuses a stable start_date_ms across retries (idempotency)', async () => {
    const { svc, component } = setup();
    await flush();
    fillRequired(component);

    await component.submit();
    await component.submit();

    const first = svc.deployInstance.mock.calls[0][0].start_date_ms;
    const second = svc.deployInstance.mock.calls[1][0].start_date_ms;
    expect(second).toBe(first);
  });

  it('conveys an idempotent no-op as success, not an error', async () => {
    const { fixture, svc, component } = setup();
    svc.deployInstance.mockResolvedValue({
      run_id: 'run-existing',
      run_dir: '/runs/run-existing',
      created: false,
      start: null,
    });
    await flush();
    fillRequired(component);

    await component.submit();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[role="alert"]')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('already exists');
  });

  it('renders the dirty-tree precondition (409) with remediation', async () => {
    const { fixture, svc, component } = setup();
    svc.deployInstance.mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: { detail: 'working tree dirty under PythonDataService, references/qc-shadow' },
      }),
    );
    await flush();
    fillRequired(component);

    await component.submit();
    fixture.detectChanges();

    const alert = fixture.nativeElement.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain('working tree dirty under PythonDataService');
    expect(alert?.textContent?.toLowerCase()).toContain('commit'); // remediation from (deploy, 409)
  });

  it('disables Deploy with a visible reason when the daemon is down', async () => {
    const { fixture, component } = setup({ daemonDown: true });
    await flush();
    fillRequired(component);
    fixture.detectChanges();

    expect(deployButton(fixture).disabled).toBe(true);
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain('Live engine unavailable');
  });

  it('blocks "Deploy & start" when fleet policy blocks starts, but allows deploy-only', async () => {
    const { fixture, component } = setup({ fleetBlocks: true });
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    fixture.detectChanges();
    expect(deployButton(fixture).disabled).toBe(true);
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain('Fleet state blocks new starts');

    component.startNow.set(false);
    fixture.detectChanges();
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('blocks start when account truth is not proven but allows deploy-only staging', async () => {
    const { fixture, component } = setup({
      accountTruth: {
        final_verdict: 'not_proven',
        status_label: 'Not proven',
        status_detail: 'Run account reconcile before starting.',
        source_freshness: [
          {
            source: 'positions',
            label: 'Positions',
            status: 'missing',
            severity: 'critical',
            fetched_at_ms: null,
            age_ms: null,
            hard_ttl_ms: 60_000,
            reason_code: 'ACCOUNT_TRUTH_SOURCE_MISSING',
            message: 'Positions source is missing.',
          },
          {
            source: 'open_orders',
            label: 'Open orders',
            status: 'stale',
            severity: 'critical',
            fetched_at_ms: 1700000000000,
            age_ms: 120_000,
            hard_ttl_ms: 60_000,
            reason_code: 'ACCOUNT_TRUTH_SOURCE_STALE',
            message: 'Open orders source is stale.',
          },
        ],
      },
    });
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    fixture.detectChanges();

    expect(deployButton(fixture).disabled).toBe(true);
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Account proof is not proven',
    );
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Missing evidence: Positions source is missing. Open orders source is stale.',
    );
    const reconcileLink = fixture.nativeElement.querySelector('.blocked a');
    expect(reconcileLink?.textContent).toContain('Run account reconcile');
    expect(reconcileLink?.getAttribute('href')).toBe(
      '/broker/account-monitor#account-reconciliation-action',
    );

    component.startNow.set(false);
    fixture.detectChanges();

    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('switches a durable STOPPED start-now request to deploy-only before submit', async () => {
    const { fixture, svc, component } = setup({
      instances: [{ strategy_instance_id: 'deployment-validation-paper', process_state: 'exited' }],
      instanceStatus: stoppedLatchStatus(),
    });
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    await settleResource(fixture);

    expect(svc.getInstanceStatus).toHaveBeenCalledWith('deployment-validation-paper');
    expect(fixture.nativeElement.textContent).toContain('Deploy only');
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Durable STOPPED latch is set',
    );
    expect(deployButton(fixture).disabled).toBe(false);

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.start).toBe(false);
    expect(req.start_options).toBeUndefined();
    expect(fieldControl(fixture, 'Daily order limit').disabled).toBe(true);
    expect(fieldControl(fixture, 'Restore previous state').disabled).toBe(true);
  });

  it('does not submit start-now while the durable STOPPED lookup is still loading', async () => {
    const pending = deferred<LiveInstanceStatus | null>();
    const { fixture, svc, component } = setup({
      instances: [{ strategy_instance_id: 'deployment-validation-paper', process_state: 'exited' }],
      instanceStatusPromise: pending.promise,
    });
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Checking durable desired state',
    );
    expect(deployButton(fixture).disabled).toBe(true);

    await component.submit();

    expect(svc.deployInstance).not.toHaveBeenCalled();

    pending.resolve(stoppedLatchStatus());
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Durable STOPPED latch is set',
    );
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('checks durable STOPPED state even when the fleet list has not exposed the instance', async () => {
    const pendingInstances = deferred<{ strategy_instance_id: string; process_state: string }[]>();
    const { fixture, svc, component } = setup({
      instancesPromise: pendingInstances.promise,
      instanceStatus: clearStartStatus(),
    });
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    await settleResource(fixture);

    expect(svc.getInstanceStatus).toHaveBeenCalledWith('deployment-validation-paper');
    expect(deployButton(fixture).disabled).toBe(false);

    pendingInstances.resolve([]);
    await settleResource(fixture);

    expect(svc.getInstanceStatus).toHaveBeenCalledTimes(1);
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('does not carry a prior STOPPED latch onto a new instance name', async () => {
    const { fixture, svc, component } = setup({
      instances: [{ strategy_instance_id: 'deployment-validation-paper', process_state: 'exited' }],
      instanceStatusResolver: (instanceId) =>
        Promise.resolve(
          instanceId === 'deployment-validation-paper' ? stoppedLatchStatus() : clearStartStatus(),
        ),
    });
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    await settleResource(fixture);

    expect(component.stoppedStartLatch()).toBe(true);

    component.instanceId.set('fresh-deployment-name');
    await settleResource(fixture);

    expect(svc.getInstanceStatus).toHaveBeenCalledTimes(2);
    expect(svc.getInstanceStatus).toHaveBeenLastCalledWith('fresh-deployment-name');
    expect(component.stoppedStartLatch()).toBe(false);
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('fails closed when the durable STOPPED status lookup errors', async () => {
    const { fixture, svc, component } = setup({
      instances: [{ strategy_instance_id: 'deployment-validation-paper', process_state: 'exited' }],
      instanceStatusError: new Error('status lookup failed'),
    });
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    await settleResource(fixture);

    expect(svc.getInstanceStatus).toHaveBeenCalledWith('deployment-validation-paper');
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Could not verify durable desired state',
    );
    expect(deployButton(fixture).disabled).toBe(true);

    await component.submit();

    expect(svc.deployInstance).not.toHaveBeenCalled();
  });

  it('shows the real deploy blocker when STOPPED latch is present but deploy-only is unavailable', async () => {
    const { fixture, component } = setup({
      instances: [{ strategy_instance_id: 'deployment-validation-paper', process_state: 'exited' }],
      instanceStatus: stoppedLatchStatus(),
      accountPromise: Promise.reject(new Error('broker account unavailable')),
    });
    await flush();
    fillRequired(component);
    component.accountId.set('');
    component.startNow.set(true);
    await settleResource(fixture);

    expect(component.stoppedStartLatch()).toBe(true);
    expect(deployButton(fixture).disabled).toBe(true);
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Connected broker account unavailable',
    );
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).not.toContain(
      'This submit will deploy only',
    );
  });

  it('includes failing account truth invariants in the account-proof action detail', async () => {
    const { fixture, component } = setup({
      accountTruth: {
        final_verdict: 'not_proven',
        status_label: 'Not proven',
        status_detail: 'Run account reconcile before starting.',
        invariants: [
          {
            key: 'all_open_orders_owned',
            label: 'All open orders owned',
            status: 'fail',
            severity: 'critical',
            headline: 'Open order ownership is not proven.',
            narrative: 'At least one broker order lacks a bot or manual owner receipt.',
            checked_at_ms: 1_780_000_001_000,
            evidence_count: 0,
          },
        ],
      },
    });
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    fixture.detectChanges();

    expect(deployButton(fixture).disabled).toBe(true);
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Missing evidence: Open order ownership is not proven.',
    );
  });

  it('keeps higher-priority blockers ahead of account-proof action links', async () => {
    const { fixture, component } = setup({
      daemonDown: true,
      accountTruth: {
        final_verdict: 'not_proven',
        status_label: 'Not proven',
        status_detail: 'Run account reconcile before starting.',
        source_freshness: [
          {
            source: 'positions',
            label: 'Positions',
            status: 'missing',
            severity: 'critical',
            fetched_at_ms: null,
            age_ms: null,
            hard_ttl_ms: 60_000,
            reason_code: 'ACCOUNT_TRUTH_SOURCE_MISSING',
            message: 'Positions source is missing.',
          },
        ],
      },
    });
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Live engine unavailable',
    );
    expect(fixture.nativeElement.querySelector('.blocked a')).toBeNull();
  });

  it('blocks "Deploy & start" onto an already-running instance, but allows deploy-only', async () => {
    const { fixture, component } = setup({ instances: [{ strategy_instance_id: 'Minute', process_state: 'running' }] });
    await flush();
    fillRequired(component);
    component.instanceId.set('Minute');
    component.startNow.set(true);
    fixture.detectChanges();

    expect(deployButton(fixture).disabled).toBe(true);
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      '"Minute" is already running',
    );

    // Deploy-only is fine — only the immediate start would collide.
    component.startNow.set(false);
    fixture.detectChanges();
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('prefills the form from re-deploy deep-link query params', async () => {
    const svc = {
      getEngineStrategies: vi.fn().mockResolvedValue([]),
      getSpecStrategyFixtures: vi.fn().mockResolvedValue([]),
      getQcAuditCopies: vi.fn().mockResolvedValue({ scope_root: 'references/qc-shadow', entries: [] }),
      getInstances: vi.fn().mockResolvedValue([]),
      getInstanceStatus: vi.fn().mockRejectedValue(new Error('instance not found')),
      deployInstance: vi.fn(),
      getAuditCopySizingLookup: vi.fn().mockResolvedValue({
        verdict: 'cannot_prove',
        detail: 'allow-list unavailable in tests',
        expected_rule: null,
        actual_rule: null,
      }),
    };
    const broker = {
      account: vi.fn().mockResolvedValue(null),
      accountTruth: vi.fn().mockResolvedValue({
        final_verdict: 'not_proven',
        status_label: 'Not proven',
        status_detail: 'No connected account proof.',
      }),
      positions: vi.fn().mockResolvedValue({ positions: [] }),
    };
    const strategyValidation = {
      getCatalog: vi.fn().mockResolvedValue(DEFAULT_STRATEGY_VALIDATION_CATALOG),
    };
    const connectivity = {
      links: () => [],
      blockers: () => [],
      daemonState: () => 'ok',
      brokerState: () => 'ok',
      brokerDetail: () => 'Connected',
      fleetState: () => 'ok',
      nothingDeployed: () => false,
      daemonDown: () => false,
      fleetBlocksStarts: () => false,
      daemonFreshness: () => ({ state: 'unknown', sha: null, commitsBehind: null }),
      reload: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'broker/deploy', component: BrokerDeployFormComponent },
        ]),
        { provide: LiveRunsService, useValue: svc },
        { provide: BrokerService, useValue: broker },
        { provide: BrokerConnectivityService, useValue: connectivity },
        { provide: StrategyValidationService, useValue: strategyValidation },
      ],
    });
    const harness = await RouterTestingHarness.create();
    const component = await harness.navigateByUrl(
      '/broker/deploy?strategy_key=deployment_validation&spec_path=spec%2Fpath.json' +
        '&signal_stream=aapl&account_id=DU777&qc_backtest_id=bt-redeploy' +
        '&qc_audit_copy_path=audit%2Fcopy.py&instance_id=recovered_inst',
      BrokerDeployFormComponent,
    );
    activeFixture = harness.fixture;
    await flush();

    expect(component.strategyKey()).toBe('deployment_validation');
    expect(component.specPath()).toBe(DEPLOYMENT_VALIDATION_SPEC_PATH);
    expect(component.signalStream()).toBe('AAPL');
    expect(component.resolvedSignalStream()).toBe('AAPL');
    expect(component.qcBacktestId()).toBe(DEPLOYMENT_VALIDATION_QC_BACKTEST_ID);
    expect(component.qcAuditCopyPath()).toBe(DEPLOYMENT_VALIDATION_AUDIT_COPY);
    expect(component.instanceId()).toBe('recovered_inst');
    // Account is no longer sourced from the redeploy URL; Deploy requires the
    // currently connected broker session to provide it.
    expect(component.accountId()).toBe('');
  });

  it('allows "Deploy & start" when the instance exists but is not live', async () => {
    const { fixture, component } = setup({
      instances: [{ strategy_instance_id: 'Minute', process_state: 'exited' }],
      instanceStatus: clearStartStatus(),
    });
    await flush();
    fillRequired(component);
    component.instanceId.set('Minute');
    component.startNow.set(true);
    fixture.detectChanges();
    await fixture.whenStable();
    await flush();
    fixture.detectChanges();

    expect(component.blockedReason()).toBeNull();
    expect(deployButton(fixture).disabled).toBe(false);
  });
});
