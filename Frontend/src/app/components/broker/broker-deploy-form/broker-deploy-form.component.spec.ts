import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DeployPreflightResponse } from '../../../api/operator-blocker.types';
import { ActionPlanPreviewService } from '../../../api/action-plan-preview.service';
import { BrokerService } from '../../../services/broker.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { StrategyValidationService } from '../../../services/strategy-validation.service';
import type { StrategyValidationCatalog } from '../../../services/strategy-validation.types';
import type { ActionPlan } from '../../../api/action-plan.types';
import { BrokerDeployFormComponent } from './broker-deploy-form.component';
import { operatorBlockerFixture } from '../../../testing/operator-blocker-fixtures';

let activeFixture: { destroy(): void; detectChanges(): void } | null = null;

const DEPLOYMENT_VALIDATION_AUDIT_COPY = 'references/qc-shadow/DeploymentValidationAlgorithm.py';
const DEPLOYMENT_VALIDATION_SPEC_PATH =
  'PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json';
const DEPLOYMENT_VALIDATION_QC_BACKTEST_ID = 'd2fe45a7142e88575f6fbd75229f8681';

function identityCoherenceCard(fixture: { nativeElement: HTMLElement }): HTMLElement | null {
  return fixture.nativeElement.querySelector('[aria-label="Run identity confirmation"]');
}

function exposureCoherenceCard(fixture: { nativeElement: HTMLElement }): HTMLElement | null {
  return fixture.nativeElement.querySelector('[aria-label="Exposure confirmation"]');
}

function exposureLaunchDecision(fixture: { nativeElement: HTMLElement }): HTMLElement | null {
  return fixture.nativeElement.querySelector('.launch-decision');
}

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

function deferred<T>(): {
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

function setup(
  opts: {
    qcEntries?: string[];
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
    strategyValidationCatalog?: StrategyValidationCatalog;
    deployPreflight?: DeployPreflightResponse | Promise<DeployPreflightResponse>;
  } = {},
) {
  const deployPreflight = opts.deployPreflight ?? { ready: true, blockers: [] };
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
    getInstances: vi.fn().mockResolvedValue([]),
    getInstanceStatus: vi.fn().mockRejectedValue(new Error('instance status is not used by deploy preflight')),
    deployPreflight: vi.fn().mockImplementation(() => Promise.resolve(deployPreflight)),
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
    positions: vi
      .fn()
      .mockResolvedValue({ positions: opts.positions ?? [] }),
  };
  const connectivity = {
    links: () => [],
    blockers: () => [],
    rosterBlockers: () => [],
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
  const strategyValidation = {
    getCatalog: vi.fn().mockResolvedValue(
      opts.strategyValidationCatalog ?? DEFAULT_STRATEGY_VALIDATION_CATALOG,
    ),
  };
  const actionPlanPreview = {
    preview: vi.fn().mockResolvedValue({ warnings: [] }),
  };
  const queryParamMap = convertToParamMap(opts.queryParams ?? {});
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: LiveRunsService, useValue: svc },
      { provide: BrokerService, useValue: broker },
      { provide: BrokerConnectivityService, useValue: connectivity },
      { provide: StrategyValidationService, useValue: strategyValidation },
      { provide: ActionPlanPreviewService, useValue: actionPlanPreview },
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
  component.updateTicket({
    strategyKey: 'deployment_validation',
    signalStream: 'SPY',
    instanceId: 'deployment-validation-paper',
  });
  component.specPath.set(DEPLOYMENT_VALIDATION_SPEC_PATH);
  component.accountId.set('DU123');
  component.qcBacktestId.set(DEPLOYMENT_VALIDATION_QC_BACKTEST_ID);
  component.qcAuditCopyPath.set(DEPLOYMENT_VALIDATION_AUDIT_COPY);
  component.actionPlan.set(validDeploymentValidationActionPlan());
  activeFixture?.detectChanges();
}

function deployButton(fixture: { nativeElement: HTMLElement }): HTMLButtonElement {
  const el = fixture.nativeElement.querySelector('button.primary');
  if (!(el instanceof HTMLButtonElement)) throw new Error('no submit button');
  return el;
}

function blockerText(component: BrokerDeployFormComponent): string | null {
  const blocker = component.topBlocker();
  if (blocker === null) return null;
  return [blocker.headline, blocker.detail].filter((part): part is string => !!part).join(' ');
}

function buttonContaining(
  fixture: { nativeElement: HTMLElement },
  text: string,
): HTMLButtonElement {
  const buttons = Array.from(fixture.nativeElement.querySelectorAll('button'));
  const button = buttons.find((candidate) => candidate.textContent?.includes(text));
  if (button instanceof HTMLButtonElement) return button;
  throw new Error(`missing button containing: ${text}`);
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
  it('lists only validated strategies and binds their deploy receipt without rendering receipt inputs', async () => {
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
    expect(fixture.nativeElement.textContent).not.toContain('Backtest verification');
    expect(fixture.nativeElement.textContent).not.toContain('Validated deploy binding');
    expect(fixture.nativeElement.textContent).not.toContain('Connected broker account');
  });

  it('defaults launch to paper orders with the 2000-order guardrail', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Launch strategy');
    expect(component.executionCapability()).toBe('paper_orders');
    expect(component.readonlyFlag()).toBe(false);
    expect(component.maxOrdersPerDay()).toBe(2000);
    expect(fixture.nativeElement.textContent).toContain('Paper orders');
    expect(fixture.nativeElement.textContent).toContain('Daily order limit');
    expect(deployButton(fixture).textContent).toContain('Deploy & run');
  });

  it('renders a single trade ticket with all required trading decisions visible', async () => {
    const { fixture } = setup();
    await flush();
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('#ticket-identity')).toBeTruthy();
    expect(host.querySelector('#ticket-signal')).toBeTruthy();
    expect(host.querySelector('#ticket-sizing')).toBeTruthy();
    expect(host.querySelector('#ticket-legs')).toBeTruthy();
    expect(host.querySelector('#ticket-launch-settings')).toBeTruthy();
    expect(host.querySelector('.ticket-review .clerk-route')).toBeTruthy();
    expect(host.querySelector('.ticket-main .clerk-route')).toBeNull();
    expect(host.querySelector('.ticket-blockers')).toBeNull();
    expect(host.querySelector('.form-tabs')).toBeNull();
    expect(host.querySelector('header .eyebrow')?.textContent).toContain('Deploy strategy');
    expect(host.textContent).not.toContain('Broker deploy');
    expect(host.textContent).not.toContain('Ticket identity');
    expect(host.textContent).not.toContain('Market input');
    expect(host.textContent).not.toContain('Order scale');
    expect(host.textContent).not.toContain('Trade instructions');
    expect(host.textContent).not.toContain('Launch review');
  });

  it('shows a client validation error only below a touched form field', async () => {
    const { fixture } = setup();
    await flush();
    fixture.detectChanges();

    const deploymentName = fieldControl(fixture, 'Deployment name');
    deploymentName.dispatchEvent(new Event('blur'));
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    expect(host.textContent).toContain('Enter a deployment name.');
    expect(host.querySelector('.ticket-blockers')).toBeNull();
  });

  it('keeps launch disabled and never calls deploy for a whitespace-padded deployment name', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    typeText(fixture, 'Deployment name', 'deployment-validation-paper ');
    fieldControl(fixture, 'Deployment name').dispatchEvent(new Event('blur'));
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain(
      'Use letters, numbers, periods, underscores, or hyphens.',
    );
    expect(deployButton(fixture).disabled).toBe(true);

    await component.submit();

    expect(svc.deployInstance).not.toHaveBeenCalled();
  });

  it('submits a deploy request with the collected fields and reports success', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);

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
      start: true,
    });
    expect(req).not.toHaveProperty('account_id');
    expect(typeof req.start_date_ms).toBe('number');
    expect(req.start_options).toMatchObject({
      readonly: false,
      hydrate_policy: 'require',
      strategy: 'deployment_validation',
      max_orders_per_day: 2000,
      ibkr_host: '127.0.0.1',
    });
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
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    await component.submit();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Deployment created');
    expect(text).toContain('Start accepted: Host runner process is active.');
    expect(text).toContain(
      'Launch request accepted for run run-started. Confirm On duty and fresh runtime evidence in Bot Operations before deploying another bot.',
    );
    expect(text).not.toContain('"deployment-validation-paper" is already running');
    expect(component.commandState().kind).toBe('accepted');
    expect(blockerText(component)).toBeNull();
    expect(deployButton(fixture).disabled).toBe(true);
  });

  it('offers separate deploy-only and deploy-and-run actions and reviews the trade asset', async () => {
    const { fixture, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Deploy & run');
    expect(text).toContain('Deploy only');
    expect(text).toContain('Trade asset');
    expect(text).toContain('SPY · Stock');
  });

  it('submits deploy-only requests without immediate start', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    await component.deployOnly();
    fixture.detectChanges();

    expect(svc.deployInstance).toHaveBeenCalledWith(expect.objectContaining({ start: false }));
    expect(fixture.nativeElement.textContent).toContain('registered but has not started');
  });

  it('formats a code-like start state when the server does not supply a message', async () => {
    const { fixture, svc, component } = setup();
    svc.deployInstance.mockResolvedValueOnce({
      run_id: 'run-started',
      run_dir: '/runs/run-started',
      created: true,
      start: {
        accepted: true,
        process: { state: 'running', message: null },
      },
    });
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    await component.submit();
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Start accepted: Running');
  });

  // PRD #593 Slice 1E (#598) — query-param-deep-linked redeploy carries
  // the parent_run_id at the top level of the submit payload (NOT
  // inside live_config — lineage is unhashed).
  it('flows parent_run_id from the route query param into the submit payload at the top level', async () => {
    const { fixture, svc, component } = setup({ queryParams: { parent_run_id: 'run-parent-abc' } });
    await flush();
    fillRequired(component);
    await settleResource(fixture);

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

    expect(blockerText(component)).toContain('ON ENTER and ON EXIT are both empty');
    expect(component.actionPlanReadiness().canDeploy).toBe(false);
    expect(deployButton(fixture).disabled).toBe(true);
    expect(svc.deployInstance).not.toHaveBeenCalled();
  });

  it('submits the operator-built stock plan via live_config.action', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);

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

  it('defaults to one share per signal and submits FixedShares(1)', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);
    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config).toEqual({
      symbol: 'SPY',
      sizing: { kind: 'FixedShares', value: 1 },
      action: validDeploymentValidationActionPlan(),
    });
    expect(component.sizingPreset()).toBe('safe_canary');
  });

  it('renders only the simple share-or-fixed-size sizing choices', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    const presets = host.querySelectorAll<HTMLInputElement>('input[name="sizing-preset"]');
    expect(presets).toHaveLength(2);
    expect(host.textContent).toContain('One share per signal');
    expect(host.textContent).toContain('Each entry signal uses one share for the stock leg declared below.');
    expect(host.textContent).toContain('Set order size');
    expect(host.textContent).not.toContain('Safe canary');
    expect(host.textContent).not.toContain('Reference parity');

    const fixedSize = host.querySelector<HTMLInputElement>('input[name="sizing-preset"][value="custom"]');
    if (!(fixedSize instanceof HTMLInputElement)) throw new Error('missing fixed-size choice');
    fixedSize.checked = true;
    fixedSize.dispatchEvent(new Event('change'));
    fixture.detectChanges();

    const fixedNotional = host.querySelector<HTMLInputElement>(
      'input[name="custom-sizing-kind"][value="FixedNotional"]',
    );
    if (!(fixedNotional instanceof HTMLInputElement)) throw new Error('missing fixed-notional choice');
    fixedNotional.checked = true;
    fixedNotional.dispatchEvent(new Event('change'));

    expect(component.sizingPreset()).toBe('custom');
    expect(component.customKind()).toBe('FixedNotional');
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
    component.updateTicket({
      strategyKey: 'spy_ema_crossover_options',
      signalStream: 'SPY',
      instanceId: 'opt-paper-1',
    });
    component.specPath.set('PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json');
    component.accountId.set('DU123');
    component.qcBacktestId.set('bt-1');
    component.qcAuditCopyPath.set('references/qc-shadow/A.py');
    fixture.detectChanges();
    await flush();

    expect(component.sizingSurfaceIsExplicit()).toBe(true);
    await settleResource(fixture);
    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config).toEqual({
      symbol: 'SPY',
      sizing: { kind: 'StrategyExplicit' },
      action: { on_enter: [], on_exit: [] },
    });
  });

  it('emits a Custom FixedShares policy from the kind/value inputs', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);
    component.sizingPreset.set('custom');
    component.updateTicket({ customKind: 'FixedShares', customValue: '25' });

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config).toEqual({
      symbol: 'SPY',
      sizing: { kind: 'FixedShares', value: 25 },
      action: validDeploymentValidationActionPlan(),
    });
  });

  it('emits a Custom FixedNotional policy with the value as a decimal string', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);
    component.sizingPreset.set('custom');
    component.updateTicket({ customKind: 'FixedNotional', customValue: '1500.50' });

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
    component.updateTicket({ customKind: 'FixedShares' });

    for (const bad of ['1.9', '25abc', '-3', '0', ' 5 5', '']) {
      component.updateTicket({ customValue: bad });
      expect(blockerText(component)).not.toBeNull();
      expect(component.canSubmit()).toBe(false);
    }
    component.updateTicket({ customValue: '25' });
    expect(component.customSizingError()).toBeNull();
  });

  it('keeps the submit path safe when Custom sizing is invalid (no busy wedge)', async () => {
    const { svc, component } = setup();
    await flush();
    fillRequired(component);
    component.sizingPreset.set('custom');
    component.updateTicket({ customKind: 'FixedNotional', customValue: 'not-a-number' });

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

    expect(blockerText(component)).toMatch(/already holds 37/);
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

  it('attaches start_options for every deploy-and-run request', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.start).toBe(true);
    expect(req.start_options.strategy).toBe('deployment_validation');
    expect(req.start_options.max_orders_per_day).toBe(2000);
  });

  it('threads the ticket daily order limit unchanged into the launch request', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    typeText(fixture, 'Daily order limit', '37');
    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.start_options.max_orders_per_day).toBe(37);
  });

  it('blocks an invalid daily order limit instead of replacing it with the default', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    typeText(fixture, 'Daily order limit', '1.5');
    fixture.detectChanges();

    expect(component.dailyOrderLimitError()).toContain('whole number');
    expect(blockerText(component)).toContain('Daily order limit is invalid');
    expect(deployButton(fixture).disabled).toBe(true);

    await component.submit();

    expect(svc.deployInstance).not.toHaveBeenCalled();
  });

  it('binds deployment provenance from validation even when the daemon listing is empty', async () => {
    const { fixture, component } = setup({ qcEntries: [] });
    await flush();
    fixture.detectChanges();

    component.updateTicket({ strategyKey: 'deployment_validation' });
    await flush();
    fixture.detectChanges();

    expect(component.qcAuditCopyPath()).toBe(DEPLOYMENT_VALIDATION_AUDIT_COPY);
    expect(component.specPath()).toBe(DEPLOYMENT_VALIDATION_SPEC_PATH);
    expect(component.maxOrdersPerDay()).toBe(2000);
    expect(fixture.nativeElement.textContent).not.toContain('Algorithm audit copy');
  });

  it('replaces stale provenance with the selected strategy validation receipt', async () => {
    const { fixture, component } = setup({ qcEntries: [] });
    await flush();
    component.specPath.set('custom/manual.spec.json');
    fixture.detectChanges();

    changeSelect(fixture, 'Strategy', 'deployment_validation');
    await flush();
    fixture.detectChanges();

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

    expect(component.commandStatus()).toBe('Ready to launch.');
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

    expect(blockerText(fixture.componentInstance)).toContain('Connected broker account');
    expect(deployButton(fixture).disabled).toBe(true);
  });

  it('blocks launch when an operator clears required ticket fields', async () => {
    const { fixture, component } = setup();
    await flush();
    fillRequired(component);

    changeSelect(fixture, 'Strategy', '');
    typeText(fixture, 'Deployment name', '');
    await flush();
    fixture.detectChanges();

    expect(component.strategyKey()).toBe('');
    expect(component.accountId()).toBe('DU123');
    expect(component.instanceId()).toBe('');
    expect(blockerText(component)).toContain(
      'Missing: Strategy, Deployment name.',
    );
    expect(deployButton(fixture).disabled).toBe(true);
  });

  it('uses the connected broker account once the Account Clerk resolves it', async () => {
    let resolveAccount: (value: { account_id: string }) => void = () => undefined;
    const accountPromise = new Promise<{ account_id: string }>((resolve) => {
      resolveAccount = resolve;
    });
    const { fixture, component } = setup({ accountPromise });
    fixture.detectChanges();

    resolveAccount({ account_id: 'DU123' });
    await flush();

    expect(component.accountId()).toBe('DU123');
  });

  it('lists the missing required fields in the blocked reason', async () => {
    const { fixture, component } = setup({ qcEntries: [] });
    await flush();
    component.updateTicket({ strategyKey: 'deployment_validation' });
    await flush();
    fixture.detectChanges();

    expect(blockerText(component)).toContain(
      'Missing: Deployment name.',
    );
  });

  it('submits the selected signal stream separately from the traded action-plan instrument', async () => {
    const { svc, component, fixture } = setup();
    await flush();
    fillRequired(component);
    typeText(fixture, 'Signal stream', 'SPY');
    await settleResource(fixture);
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

    expect(component.resolvedSignalStream()).toBe('');
    expect(blockerText(component)).toContain('Signal stream');

    typeText(fixture, 'Signal stream', 'QQQ');
    fixture.detectChanges();

    expect(component.resolvedSignalStream()).toBe('QQQ');
  });

  it('clears an auto-filled signal stream when the next approved strategy has no validation symbol', async () => {
    const noValidationSymbol = {
      ...DEFAULT_STRATEGY_VALIDATION_CATALOG.strategies[0],
      strategy_key: 'no_validation_symbol',
      display_name: 'No Validation Symbol',
      validation_case_symbol: null,
    };
    const { fixture } = setup({
      strategyValidationCatalog: {
        strategies: [DEFAULT_STRATEGY_VALIDATION_CATALOG.strategies[0], noValidationSymbol],
      },
    });
    await flush();

    changeSelect(fixture, 'Strategy', 'deployment_validation');
    await flush();
    fixture.detectChanges();
    expect(fieldControl(fixture, 'Signal stream').value).toBe('SPY');

    changeSelect(fixture, 'Strategy', 'no_validation_symbol');
    await flush();
    fixture.detectChanges();

    expect(fieldControl(fixture, 'Signal stream').value).toBe('');
    expect(deployButton(fixture).disabled).toBe(true);
  });

  it('starts with paper order submission enabled without an extra modal', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);
    chooseExecutionCapability(fixture, 'paper_orders');
    fixture.detectChanges();

    await component.submit();
    fixture.detectChanges();

    expect(svc.deployInstance).toHaveBeenCalledTimes(1);
    expect(fixture.nativeElement.textContent).not.toContain('Enable paper order submission?');
    expect(fixture.nativeElement.textContent).not.toContain('The run can submit paper orders through the connected Account Clerk.');
    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.start_options.readonly).toBe(false);
    expect(req.start_options.max_orders_per_day).toBe(2000);
  });

  it('shows live execution as an option but blocks submit before the request boundary', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    fixture.detectChanges();

    chooseExecutionCapability(fixture, 'live');
    fixture.detectChanges();

    expect(component.executionCapability()).toBe('live');
    expect(component.readonlyFlag()).toBe(false);
    expect(blockerText(component)).toContain('Live execution is unavailable');
    expect(deployButton(fixture).disabled).toBe(true);

    await component.submit();

    expect(svc.deployInstance).not.toHaveBeenCalled();
  });

  it('reuses a stable start_date_ms across retries (idempotency)', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);

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
    await settleResource(fixture);

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
    await settleResource(fixture);

    await component.submit();
    fixture.detectChanges();

    const alert = fixture.nativeElement.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain('working tree dirty under PythonDataService');
    expect(alert?.textContent?.toLowerCase()).toContain('commit'); // remediation from (deploy, 409)
    expect(fixture.nativeElement.querySelector('.ticket-review .server-launch-error')?.textContent).toContain(
      'Launch was rejected by the server',
    );
  });

  it('renders server-supplied recovery moves when launch preflight changes after the ticket check', async () => {
    const { fixture, svc, component } = setup();
    svc.deployInstance.mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            reason_code: 'DEPLOY_PREFLIGHT_BLOCKED',
            message: 'A launch gate changed after the ticket check.',
            gate_id: 'broker.connection',
            blockers: [
              operatorBlockerFixture({
                id: 'broker_disconnected',
                scope: 'broker',
                host: 'deploy_preflight',
                headline: 'Broker session needs reconnecting',
                detail: 'Reconnect through Account Clerk, then retry the launch.',
                primaryMove: {
                  label: 'Open Accounts',
                  action: { kind: 'navigate', route: '/broker/accounts', fragment: null },
                  target: null,
                },
              }),
            ],
          },
        },
      }),
    );
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    await component.submit();
    fixture.detectChanges();

    const serverError = fixture.nativeElement.querySelector('.ticket-review .server-launch-error');
    expect(serverError?.textContent).toContain('A launch gate changed after the ticket check.');
    expect(serverError?.textContent).toContain('Broker session needs reconnecting');
    expect(serverError?.textContent).toContain('Open Accounts');
  });

  it('disables Deploy & run and names the backend preflight blocker', async () => {
    const { fixture, svc, component } = setup({
      deployPreflight: {
        ready: false,
        blockers: [operatorBlockerFixture({ host: 'deploy_preflight' })],
      },
    });
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    expect(svc.deployPreflight).toHaveBeenCalledWith({
      strategyKey: 'deployment_validation',
      accountId: 'DU123',
      instanceId: 'deployment-validation-paper',
    });
    expect(deployButton(fixture).disabled).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('Broker disconnected');
    expect(fixture.nativeElement.textContent).toContain('Launch blocked: Broker disconnected.');
    expect(svc.deployInstance).not.toHaveBeenCalled();
  });

  it('renders the Clerk contamination cure and disables Deploy & run', async () => {
    const { fixture, svc, component } = setup({
      deployPreflight: {
        ready: false,
        blockers: [
          operatorBlockerFixture({
            id: 'fleet_contaminated',
            scope: 'fleet',
            host: 'deploy_preflight',
            headline: 'Fleet state blocks new deploys',
            detail: 'Clear the account fleet state before deploying or starting a bot.',
            primaryMove: {
              label: 'Open Accounts',
              action: { kind: 'navigate', route: '/broker/accounts', fragment: null },
              target: null,
            },
          }),
        ],
      },
    });
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    expect(deployButton(fixture).disabled).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('Fleet state blocks new deploys');
    expect(fixture.nativeElement.textContent).toContain('Open Accounts');
    expect(component.commandStatus()).toBe('Launch blocked: Fleet state blocks new deploys.');
    expect(svc.deployInstance).not.toHaveBeenCalled();
  });

  it('keeps Deploy & run disabled while backend preflight is loading', async () => {
    const pending = deferred<DeployPreflightResponse>();
    const { fixture, svc, component } = setup({ deployPreflight: pending.promise });
    await flush();
    fillRequired(component);
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(svc.deployPreflight).toHaveBeenCalledWith({
      strategyKey: 'deployment_validation',
      accountId: 'DU123',
      instanceId: 'deployment-validation-paper',
    });
    expect(component.commandStatus()).toBe('Checking server launch gates…');
    expect(deployButton(fixture).disabled).toBe(true);
    expect(component.canSubmit()).toBe(false);

    pending.resolve({ ready: true, blockers: [] });
    await settleResource(fixture);

    expect(component.commandStatus()).toBe('Ready to launch.');
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('keeps deploy command copy and validity stable while a deployment-name preflight reloads', async () => {
    const pendingReload = deferred<DeployPreflightResponse>();
    const { fixture, svc, component } = setup();
    svc.deployPreflight
      .mockResolvedValueOnce({ ready: true, blockers: [] })
      .mockImplementation(() => pendingReload.promise);
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    expect(component.commandStatus()).toBe('Ready to launch.');
    expect(deployButton(fixture).disabled).toBe(false);

    typeText(fixture, 'Deployment name', 'deployment-validation-paper-next');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(svc.deployPreflight).toHaveBeenLastCalledWith({
      strategyKey: 'deployment_validation',
      accountId: 'DU123',
      instanceId: 'deployment-validation-paper-next',
    });
    expect(component.deployPreflight.isLoading()).toBe(true);
    expect(component.commandStatus()).toBe('Ready to launch.');
    expect(deployButton(fixture).disabled).toBe(false);

    pendingReload.resolve({ ready: true, blockers: [] });
    await settleResource(fixture);

    expect(component.commandStatus()).toBe('Ready to launch.');
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('shows a custom-sizing error below the touched sizing field', async () => {
    const { fixture, component } = setup();
    await flush();
    fillRequired(component);
    component.sizingPreset.set('custom');
    fixture.detectChanges();

    typeText(fixture, 'Shares per signal', '1.5');
    fieldControl(fixture, 'Shares per signal').dispatchEvent(new Event('blur'));
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('whole number');
  });

  it('enables Deploy & run when preflight is ready and the form is complete', async () => {
    const { fixture, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    expect(deployButton(fixture).disabled).toBe(false);
    expect(component.commandStatus()).toBe('Ready to launch.');
  });

  it('blocks "Deploy & run" when inherited identity conflicts with the new signal or action plan until confirmed', async () => {
    const { fixture, svc, component } = setup({
      queryParams: {
        inherited_symbol: 'mu',
        inherited_symbol_source: 'run_ledger.live_config.action stock target',
        signal_stream: 'spy',
      },
    });
    await flush();
    fillRequired(component);
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
    await settleResource(fixture);

    expect(component.identityCoherenceEvidence()?.facts.map((fact) => fact.value)).toEqual([
      'MU',
      'SPY',
      'AAPL',
    ]);
    expect(blockerText(component)).toContain('Inherited bot symbol MU conflicts');
    expect(blockerText(component)).toContain('Signal stream SPY and Action plan AAPL');
    expect(identityCoherenceCard(fixture)?.textContent).toContain(
      'Confirm new run identity',
    );
    expect(deployButton(fixture).disabled).toBe(true);

    component.confirmIdentityCoherence();
    fixture.detectChanges();

    expect(component.identityCoherenceConfirmed()).toBe(true);
    expect(blockerText(component)).toBeNull();
    expect(identityCoherenceCard(fixture)?.textContent).toContain(
      'Confirmed for this Deploy & run.',
    );
    expect(deployButton(fixture).disabled).toBe(false);

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.inherited_symbol).toBe('MU');
    expect(req.inherited_symbol_source).toBe('run_ledger.live_config.action stock target');
    expect(req.identity_coherence_confirmation).toEqual({
      inherited_symbol: 'MU',
      signal_stream: 'SPY',
      action_plan_symbol: 'AAPL',
    });

    component.updateTicket({ signalStream: 'QQQ' });
    fixture.detectChanges();

    expect(component.identityCoherenceConfirmed()).toBe(false);
    expect(blockerText(component)).toContain('Signal stream QQQ');
    expect(deployButton(fixture).disabled).toBe(true);
  });

  it('lists only inherited-symbol disagreements in the identity confirmation', async () => {
    const { fixture, component } = setup({
      queryParams: {
        inherited_symbol: 'SPY',
        inherited_symbol_source: 'run_ledger.live_config.symbol signal stream',
        signal_stream: 'spy',
      },
    });
    await flush();
    fillRequired(component);
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
    await settleResource(fixture);

    expect(component.identityCoherenceEvidence()?.facts.map((fact) => fact.value)).toEqual([
      'SPY',
      'AAPL',
    ]);
    expect(blockerText(component)).toContain('Action plan AAPL');
    expect(blockerText(component)).not.toContain('Signal stream SPY');
    expect(identityCoherenceCard(fixture)?.textContent).not.toContain('Signal stream');
    expect(deployButton(fixture).disabled).toBe(true);
  });

  it('blocks "Deploy & run" when inherited exposure is not proven flat until confirmed', async () => {
    const { fixture, svc, component } = setup({
      queryParams: {
        inherited_exposure_posture: 'LONG',
        inherited_exposure_positions: '{"SPY":5}',
        inherited_exposure_pending_order_count: '2',
        inherited_exposure_source: 'operator_surface.current_risk',
      },
    });
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    expect(component.exposureCoherenceEvidence()?.posture).toBe('LONG');
    expect(component.exposureCoherenceEvidence()?.ownedPositions).toEqual({ SPY: 5 });
    expect(blockerText(component)).toContain('Inherited exposure is Long');
    expect(exposureCoherenceCard(fixture)?.textContent).toContain('SPY 5');
    expect(exposureCoherenceCard(fixture)?.textContent).toContain(
      'Confirm exposure state',
    );
    expect(deployButton(fixture).disabled).toBe(true);

    component.confirmExposureCoherence();
    fixture.detectChanges();

    expect(component.exposureCoherenceConfirmed()).toBe(true);
    expect(blockerText(component)).toBeNull();
    expect(deployButton(fixture).disabled).toBe(false);

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.inherited_exposure_posture).toBe('LONG');
    expect(req.inherited_exposure_positions).toEqual({ SPY: 5 });
    expect(req.inherited_exposure_pending_order_count).toBe(2);
    expect(req.inherited_exposure_source).toBe('operator_surface.current_risk');
    expect(req.exposure_coherence_confirmation).toEqual({
      posture: 'LONG',
      pending_order_count: 2,
      owned_positions: { SPY: 5 },
      strategy_instance_id: 'deployment-validation-paper',
      run_id: null,
    });
  });

  it('uses backend identity-coherence evidence after an unconfirmed submit is rejected', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);
    svc.deployInstance.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            reason_code: 'IDENTITY_COHERENCE_UNCONFIRMED',
            gate_id: 'deploy.identity_coherence',
            message: 'Confirm the new run identity.',
            evidence: [
              {
                label: 'inherited_symbol',
                value: 'MU',
                source: 'run_ledger.live_config.action stock target',
              },
              { label: 'signal_stream', value: 'SPY', source: 'live_config.symbol' },
            ],
            remediation: 'Confirm the current values, or turn off start.',
          },
        },
      }),
    );

    expect(component.identityCoherenceEvidence()).toBeNull();

    await component.submit();
    fixture.detectChanges();

    expect(component.inheritedSymbol()).toBe('MU');
    expect(component.inheritedSymbolSource()).toBe('run_ledger.live_config.action stock target');
    expect(component.identityCoherenceEvidence()?.facts.map((fact) => fact.value)).toEqual([
      'MU',
      'SPY',
      'SPY',
    ]);
    expect(identityCoherenceCard(fixture)?.textContent).toContain(
      'Confirm new run identity',
    );
    expect(deployButton(fixture).disabled).toBe(true);

    component.confirmIdentityCoherence();
    fixture.detectChanges();
    await component.submit();

    const retry = svc.deployInstance.mock.calls[1][0];
    expect(retry.identity_coherence_confirmation).toEqual({
      inherited_symbol: 'MU',
      signal_stream: 'SPY',
      action_plan_symbol: 'SPY',
    });
  });

  it('uses backend exposure-coherence evidence after an unconfirmed submit is rejected', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);
    svc.deployInstance.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            reason_code: 'EXPOSURE_COHERENCE_UNCONFIRMED',
            gate_id: 'deploy.exposure_coherence',
            message: 'Confirm the current exposure.',
            evidence: {
              posture: 'LONG',
              pending_order_count: 1,
              owned_positions: { spy: 5 },
              source: 'live_state.expected_position_by_symbol',
              strategy_instance_id: 'deployment-validation-paper',
              run_id: 'run-prev',
            },
            remediation: 'Confirm the current values, or turn off start.',
          },
        },
      }),
    );

    expect(component.exposureCoherenceEvidence()).toBeNull();

    await component.submit();
    fixture.detectChanges();

    expect(component.inheritedExposurePosture()).toBe('LONG');
    expect(component.inheritedExposurePendingOrderCount()).toBe(1);
    expect(component.inheritedExposurePositions()).toEqual({ SPY: 5 });
    expect(component.inheritedExposureSource()).toBe('live_state.expected_position_by_symbol');
    expect(component.parentRunId()).toBe('run-prev');
    expect(exposureCoherenceCard(fixture)?.textContent).toContain('SPY 5');
    expect(deployButton(fixture).disabled).toBe(true);

    component.confirmExposureCoherence();
    fixture.detectChanges();
    await component.submit();

    const retry = svc.deployInstance.mock.calls[1][0];
    expect(retry.parent_run_id).toBe('run-prev');
    expect(retry.exposure_coherence_confirmation).toEqual({
      posture: 'LONG',
      pending_order_count: 1,
      owned_positions: { SPY: 5 },
      strategy_instance_id: 'deployment-validation-paper',
      run_id: 'run-prev',
    });
  });

  it('surfaces UNKNOWN exposure blocks as launch recovery actions', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    await settleResource(fixture);
    svc.deployInstance.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            reason_code: 'EXPOSURE_COHERENCE_UNCONFIRMED',
            gate_id: 'deploy.exposure_coherence',
            message:
              'Deploy & run is blocked because existing exposure is not proven flat (posture=UNKNOWN, pending_order_count=None).',
            evidence: {
              posture: 'UNKNOWN',
              pending_order_count: null,
              owned_positions: {},
              source: 'operator_surface.current_risk',
              strategy_instance_id: 'deployment-validation-paper',
              run_id: null,
            },
            remediation:
              'Review the bot current risk and account reconciliation, then confirm before deploying and running.',
          },
        },
      }),
    );

    await component.submit();
    fixture.detectChanges();

    expect(component.error()?.detail).toContain('existing exposure is not proven flat');
    expect(exposureLaunchDecision(fixture)?.textContent).toContain('Exposure is not proven flat');
    expect(exposureLaunchDecision(fixture)?.textContent).toContain('Pending orders');
    expect(exposureLaunchDecision(fixture)?.textContent).toContain('unknown');

    buttonContaining(fixture, 'Confirm and deploy & run').click();
    await flush();
    fixture.detectChanges();

    const retry = svc.deployInstance.mock.calls[1][0];
    expect(component.error()).toBeNull();
    expect(retry.start).toBe(true);
    expect(retry.exposure_coherence_confirmation).toEqual({
      posture: 'UNKNOWN',
      pending_order_count: null,
      owned_positions: {},
      strategy_instance_id: 'deployment-validation-paper',
      run_id: null,
    });
  });

  it('does not ask for identity confirmation when the inherited symbol matches the new signal stream', async () => {
    const { fixture, component } = setup({
      queryParams: {
        inherited_symbol: 'SPY',
        inherited_symbol_source: 'run_ledger.live_config.symbol signal stream',
        signal_stream: 'SPY',
      },
    });
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    expect(component.identityCoherenceEvidence()).toBeNull();
    expect(blockerText(component)).toBeNull();
    expect(identityCoherenceCard(fixture)).toBeNull();
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('does not ask for exposure confirmation when inherited exposure is flat with no pending orders', async () => {
    const { fixture, component } = setup({
      queryParams: {
        inherited_exposure_posture: 'FLAT',
        inherited_exposure_positions: '{}',
        inherited_exposure_pending_order_count: '0',
        inherited_exposure_source: 'operator_surface.current_risk',
      },
    });
    await flush();
    fillRequired(component);
    await settleResource(fixture);

    expect(component.exposureCoherenceEvidence()).toBeNull();
    expect(blockerText(component)).toBeNull();
    expect(exposureCoherenceCard(fixture)).toBeNull();
    expect(deployButton(fixture).disabled).toBe(false);
  });

  it('prefills the form from re-deploy deep-link query params', async () => {
    const svc = {
      getEngineStrategies: vi.fn().mockResolvedValue([]),
      getSpecStrategyFixtures: vi.fn().mockResolvedValue([]),
      getQcAuditCopies: vi.fn().mockResolvedValue({ scope_root: 'references/qc-shadow', entries: [] }),
      getInstances: vi.fn().mockResolvedValue([]),
      getInstanceStatus: vi.fn().mockRejectedValue(new Error('instance not found')),
      deployPreflight: vi.fn().mockResolvedValue({ ready: true, blockers: [] }),
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
      positions: vi.fn().mockResolvedValue({ positions: [] }),
    };
    const strategyValidation = {
      getCatalog: vi.fn().mockResolvedValue(DEFAULT_STRATEGY_VALIDATION_CATALOG),
    };
    const actionPlanPreview = {
      preview: vi.fn().mockResolvedValue({ warnings: [] }),
    };
    const connectivity = {
      links: () => [],
      blockers: () => [],
      rosterBlockers: () => [],
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
        { provide: ActionPlanPreviewService, useValue: actionPlanPreview },
      ],
    });
    const harness = await RouterTestingHarness.create();
    const component = await harness.navigateByUrl(
      '/broker/deploy?strategy_key=deployment_validation&spec_path=spec%2Fpath.json' +
        '&signal_stream=aapl&account_id=DU777&qc_backtest_id=bt-redeploy' +
        '&qc_audit_copy_path=audit%2Fcopy.py&instance_id=recovered_inst' +
        '&inherited_symbol=mu&inherited_symbol_source=run_ledger.live_config.action%20stock%20target' +
        '&action_plan=%7B%22on_enter%22%3A%5B%7B%22leg_id%22%3A%22spy_long%22%2C%22instrument%22%3A%7B%22kind%22%3A%22stock%22%2C%22underlying%22%3A%22SPY%22%7D%2C%22position%22%3A%22long%22%2C%22qty_ratio%22%3A1%7D%5D%2C%22on_exit%22%3A%5B%7B%22kind%22%3A%22close_leg%22%2C%22entry_leg_id%22%3A%22spy_long%22%7D%5D%7D',
      BrokerDeployFormComponent,
    );
    activeFixture = harness.fixture;
    await flush();

    expect(component.strategyKey()).toBe('deployment_validation');
    expect(component.specPath()).toBe(DEPLOYMENT_VALIDATION_SPEC_PATH);
    expect(component.signalStream()).toBe('AAPL');
    expect(component.resolvedSignalStream()).toBe('AAPL');
    expect(component.inheritedSymbol()).toBe('MU');
    expect(component.inheritedSymbolSource()).toBe('run_ledger.live_config.action stock target');
    expect(component.qcBacktestId()).toBe(DEPLOYMENT_VALIDATION_QC_BACKTEST_ID);
    expect(component.qcAuditCopyPath()).toBe(DEPLOYMENT_VALIDATION_AUDIT_COPY);
    expect(component.instanceId()).toBe('recovered_inst');
    expect(component.actionPlan()).toEqual(validDeploymentValidationActionPlan());
    // Account is no longer sourced from the redeploy URL; Deploy requires the
    // currently connected broker session to provide it.
    expect(component.accountId()).toBe('');
  });

  it('allows "Deploy & run" when backend preflight is ready', async () => {
    const { fixture, component } = setup();
    await flush();
    fillRequired(component);
    component.updateTicket({ instanceId: 'Minute' });
    fixture.detectChanges();
    await fixture.whenStable();
    await flush();
    fixture.detectChanges();

    expect(blockerText(component)).toBeNull();
    expect(deployButton(fixture).disabled).toBe(false);
  });
});
