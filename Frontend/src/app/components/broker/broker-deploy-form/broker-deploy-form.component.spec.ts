import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BrokerService } from '../../../services/broker.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerDeployFormComponent } from './broker-deploy-form.component';

let activeFixture: { destroy(): void } | null = null;

function setup(opts: { daemonDown?: boolean; fleetBlocks?: boolean } = {}) {
  const svc = {
    getEngineStrategies: vi
      .fn()
      .mockResolvedValue([
        { name: 'spy_ema_crossover', display_name: 'SPY EMA Crossover', description: '' },
      ]),
    getQcAuditCopies: vi
      .fn()
      .mockResolvedValue({ scope_root: 'references/qc-shadow', entries: ['references/qc-shadow/A.py'] }),
    deployInstance: vi
      .fn()
      .mockResolvedValue({ run_id: 'run-new', run_dir: '/runs/run-new', created: true, start: null }),
  };
  const broker = { account: vi.fn().mockResolvedValue({ account_id: 'DU123' }) };
  const connectivity = {
    links: () => [],
    blockers: () => [],
    daemonDown: () => opts.daemonDown ?? false,
    fleetBlocksStarts: () => opts.fleetBlocks ?? false,
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
    expect(req.start_options.strategy).toBe('spy_ema_crossover');
    expect(fixture.nativeElement.textContent).toContain('Deployed run');
    expect(fixture.nativeElement.textContent).toContain('run-new');
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
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain('Host daemon unreachable');
  });

  it('blocks "Deploy & start" when fleet policy blocks starts, but allows deploy-only', async () => {
    const { fixture, component } = setup({ fleetBlocks: true });
    await flush();
    fillRequired(component);
    component.startNow.set(true);
    fixture.detectChanges();
    expect(deployButton(fixture).disabled).toBe(true);
    expect(fixture.nativeElement.querySelector('.blocked')?.textContent).toContain('Fleet policy blocks new starts');

    component.startNow.set(false);
    fixture.detectChanges();
    expect(deployButton(fixture).disabled).toBe(false);
  });
});
