import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BrokerService } from '../../../services/broker.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerDeployFormComponent } from './broker-deploy-form.component';

let activeFixture: { destroy(): void } | null = null;

const DEPLOYMENT_VALIDATION_AUDIT_COPY = 'references/qc-shadow/DeploymentValidationAlgorithm.py';
const DEPLOYMENT_VALIDATION_SPEC_PATH =
  'PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json';

function setup(
  opts: {
    daemonDown?: boolean;
    fleetBlocks?: boolean;
    qcEntries?: string[];
    instances?: { strategy_instance_id: string; process_state: string }[];
    parityGate?: {
      verdict: 'proven_match' | 'proven_mismatch' | 'cannot_prove';
      detail: string;
      expected_rule: unknown;
      actual_rule: unknown;
    };
    positions?: { symbol: string; quantity: number }[];
    strategies?: {
      name: string;
      display_name: string;
      description: string;
      sizing_surface: 'policy' | 'explicit';
    }[];
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
          name: 'ema_crossover_options',
          display_name: 'EMA Crossover Options',
          description: '',
          sizing_surface: 'explicit',
        },
      ],
    ),
    getSpecStrategyFixtures: vi.fn().mockResolvedValue([
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
    getQcAuditCopies: vi
      .fn()
      .mockResolvedValue({
        scope_root: 'references/qc-shadow',
        entries: opts.qcEntries ?? ['references/qc-shadow/A.py'],
      }),
    getInstances: vi.fn().mockResolvedValue(opts.instances ?? []),
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
    account: vi.fn().mockResolvedValue({ account_id: 'DU123' }),
    positions: vi
      .fn()
      .mockResolvedValue({ positions: opts.positions ?? [] }),
  };
  const connectivity = {
    links: () => [],
    blockers: () => [],
    daemonState: () => (opts.daemonDown ? 'down' : 'ok'),
    brokerState: () => 'ok',
    fleetState: () => (opts.fleetBlocks ? 'warn' : 'ok'),
    nothingDeployed: () => false,
    daemonDown: () => opts.daemonDown ?? false,
    fleetBlocksStarts: () => opts.fleetBlocks ?? false,
    daemonFreshness: () => ({ state: 'unknown', sha: null, commitsBehind: null }),
    reload: vi.fn(),
  };
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: LiveRunsService, useValue: svc },
      { provide: BrokerService, useValue: broker },
      { provide: BrokerConnectivityService, useValue: connectivity },
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

function fillRequired(component: BrokerDeployFormComponent) {
  component.strategyKey.set('spy_ema_crossover');
  component.specPath.set('PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json');
  component.accountId.set('DU123');
  component.qcBacktestId.set('bt-1');
  component.qcAuditCopyPath.set('references/qc-shadow/A.py');
  component.instanceId.set('spy-ema-paper-1');
}

function deployButton(fixture: { nativeElement: HTMLElement }): HTMLButtonElement {
  const el = fixture.nativeElement.querySelector('button[type="submit"]');
  if (!(el instanceof HTMLButtonElement)) throw new Error('no submit button');
  return el;
}

afterEach(() => {
  activeFixture?.destroy();
  activeFixture = null;
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('BrokerDeployFormComponent', () => {
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
      strategy_spec_path: 'PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json',
      qc_audit_copy_path: 'references/qc-shadow/A.py',
      qc_cloud_backtest_id: 'bt-1',
      account_id: 'DU123',
      strategy_instance_id: 'spy-ema-paper-1',
      strategy_key: 'spy_ema_crossover',
      start: false,
    });
    expect(typeof req.start_date_ms).toBe('number');
    // Deploy-only: launch knobs are omitted so they aren't validated.
    expect(req.start_options).toBeUndefined();
    expect(fixture.nativeElement.textContent).toContain('Deployment created');
    expect(fixture.nativeElement.textContent).toContain('run-new');
  });

  it('defaults the sizing preset to Safe canary and submits FixedShares(1)', async () => {
    const { svc, component } = setup();
    await flush();
    fillRequired(component);
    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config).toEqual({ sizing: { kind: 'FixedShares', value: 1 } });
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
    const { svc, component, fixture } = setup();
    await flush();
    component.strategyKey.set('ema_crossover_options');
    component.specPath.set('PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json');
    component.accountId.set('DU123');
    component.qcBacktestId.set('bt-1');
    component.qcAuditCopyPath.set('references/qc-shadow/A.py');
    component.instanceId.set('opt-paper-1');
    fixture.detectChanges();
    await flush();

    expect(component.sizingSurfaceIsExplicit()).toBe(true);
    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.live_config).toEqual({ sizing: { kind: 'StrategyExplicit' } });
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
    expect(req.live_config).toEqual({ sizing: { kind: 'FixedShares', value: 25 } });
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
    expect(req.live_config).toEqual({ sizing: { kind: 'FixedNotional', value: '1500.50' } });
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
    const { svc, component } = setup();
    await flush();
    fillRequired(component);
    component.startNow.set(true);

    await component.submit();

    const req = svc.deployInstance.mock.calls[0][0];
    expect(req.start).toBe(true);
    expect(req.start_options.strategy).toBe('spy_ema_crossover');
    expect(req.start_options.max_orders_per_day).toBe(50_000);
  });

  it('keeps the deployment validation audit copy selectable when the daemon listing is empty', async () => {
    const { fixture, component } = setup({ qcEntries: [] });
    await flush();
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    const options = Array.from(host.querySelectorAll<HTMLOptionElement>('option')).map(
      (option) => option.textContent ?? '',
    );
    expect(options).toContain(DEPLOYMENT_VALIDATION_AUDIT_COPY);

    component.strategyKey.set('deployment_validation');
    await flush();

    expect(component.qcAuditCopyPath()).toBe(DEPLOYMENT_VALIDATION_AUDIT_COPY);
    expect(component.specPath()).toBe(DEPLOYMENT_VALIDATION_SPEC_PATH);
    // Default is now 50_000 (orphan-fill recovery + predictive cap-check
    // removed the need for the per-strategy auto-bump from 4→100).
    expect(component.maxOrdersPerDay()).toBe(50_000);

    component.strategyKey.set('spy_ema_crossover');
    await flush();

    expect(component.qcAuditCopyPath()).toBe('');
  });

  it('lists the missing required fields in the blocked reason', async () => {
    const { fixture, component } = setup({ qcEntries: [] });
    await flush();
    component.strategyKey.set('deployment_validation');
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain(
      'Missing: Backtest ID, Deployment name.',
    );
  });

  it('asks for confirmation before starting in Live mode', async () => {
    const { fixture, svc, component } = setup();
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    component.readonlyFlag.set(false);
    fixture.detectChanges();

    await component.submit();
    fixture.detectChanges();

    expect(svc.deployInstance).not.toHaveBeenCalled();
    expect(fixture.nativeElement.textContent).toContain('Start live trading?');

    await component.confirmLiveAndSubmit();

    expect(svc.deployInstance).toHaveBeenCalledTimes(1);
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
      deployInstance: vi.fn(),
    };
    const broker = { account: vi.fn().mockResolvedValue(null) };
    const connectivity = {
      links: () => [],
      blockers: () => [],
      daemonState: () => 'ok',
      brokerState: () => 'ok',
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
      ],
    });
    const harness = await RouterTestingHarness.create();
    const component = await harness.navigateByUrl(
      '/broker/deploy?strategy_key=spy_ema_crossover&spec_path=spec%2Fpath.json' +
        '&account_id=DU777&qc_backtest_id=bt-redeploy' +
        '&qc_audit_copy_path=audit%2Fcopy.py&instance_id=recovered_inst',
      BrokerDeployFormComponent,
    );
    activeFixture = harness.fixture;

    expect(component.strategyKey()).toBe('spy_ema_crossover');
    expect(component.specPath()).toBe('spec/path.json');
    expect(component.qcBacktestId()).toBe('bt-redeploy');
    expect(component.qcAuditCopyPath()).toBe('audit/copy.py');
    expect(component.instanceId()).toBe('recovered_inst');
    // Seeded account survives even with the broker prefill returning null.
    expect(component.accountId()).toBe('DU777');
  });

  it('allows "Deploy & start" when the instance exists but is not live', async () => {
    const { fixture, component } = setup({ instances: [{ strategy_instance_id: 'Minute', process_state: 'exited' }] });
    await flush();
    fillRequired(component);
    component.instanceId.set('Minute');
    component.startNow.set(true);
    fixture.detectChanges();

    expect(deployButton(fixture).disabled).toBe(false);
  });
});
