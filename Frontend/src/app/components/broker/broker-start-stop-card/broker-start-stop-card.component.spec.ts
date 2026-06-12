import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerStartStopCardComponent } from './broker-start-stop-card.component';

function makeStatus(overrides: Partial<LiveInstanceStatus> = {}): LiveInstanceStatus {
  return {
    strategy_instance_id: 'spy_ema_paper',
    process: { state: 'idle' },
    live_binding: null,
    evidence_binding: { run_id: 'run-old', state: 'latest_run_by_ledger', is_live: false },
    desired_state: null,
    readiness: null,
    latest_decision: null,
    decision_columns: [],
    broker: null,
    start_defaults: {
      strategy: 'rsi_mean_reversion',
      readonly: true,
      hydrate_policy: 'optional',
      max_orders_per_day: 7,
      ibkr_host: '10.0.0.5',
    },
    provenance: null,
    last_exit: null,
    symbol: null,
    fetched_at_ms: 1,
    ...overrides,
  };
}

let activeFixture: { destroy(): void } | null = null;

function render(status: LiveInstanceStatus, daemonDown = false, fleetBlocks = false) {
  const svc = {
    startHostRunner: vi.fn().mockResolvedValue({ accepted: true, process: { state: 'running' } }),
    stopHostRunner: vi.fn().mockResolvedValue({ accepted: true, process: { state: 'stopping' } }),
  };
  const connectivity = { daemonDown: () => daemonDown, fleetBlocksStarts: () => fleetBlocks };
  TestBed.configureTestingModule({
    providers: [
      { provide: LiveRunsService, useValue: svc },
      { provide: BrokerConnectivityService, useValue: connectivity },
    ],
  });
  const fixture = TestBed.createComponent(BrokerStartStopCardComponent);
  activeFixture = fixture;
  fixture.componentRef.setInput('status', status);
  fixture.detectChanges();
  return { fixture, svc, component: fixture.componentInstance };
}

function button(fixture: { nativeElement: HTMLElement }, label: string): HTMLButtonElement {
  const el = Array.from(fixture.nativeElement.querySelectorAll('button')).find(
    (b) => b.textContent?.trim() === label,
  );
  if (!(el instanceof HTMLButtonElement)) throw new Error(`no "${label}" button`);
  return el;
}

afterEach(() => {
  activeFixture?.destroy();
  activeFixture = null;
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('BrokerStartStopCardComponent', () => {
  it('seeds the five fields from the server-authored start_defaults', () => {
    const { fixture } = render(makeStatus());
    const el = fixture.nativeElement as HTMLElement;
    const strategy = el.querySelector<HTMLInputElement>('input[type="text"]');
    const hydrate = el.querySelector<HTMLSelectElement>('select');
    const maxOrders = el.querySelector<HTMLInputElement>('input[type="number"]');

    expect(strategy?.value).toBe('rsi_mean_reversion');
    expect(hydrate?.value).toBe('optional');
    expect(maxOrders?.value).toBe('7');
  });

  it('falls back to the historical strategy when the ledger has no key', () => {
    const { fixture } = render(makeStatus({ start_defaults: null }));
    const strategy = (fixture.nativeElement as HTMLElement).querySelector<HTMLInputElement>(
      'input[type="text"]',
    );
    expect(strategy?.value).toBe('spy_ema_crossover');
  });

  it('shows shadow-mode ON (no orders) when the server default is readonly', () => {
    const { fixture } = render(makeStatus()); // start_defaults.readonly === true
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('no orders placed');
  });

  it('reflects a readonly=false server default as "orders will be placed" and starts non-readonly', async () => {
    const { fixture, svc } = render(
      makeStatus({
        start_defaults: {
          strategy: 'rsi_mean_reversion',
          readonly: false,
          hydrate_policy: 'optional',
          max_orders_per_day: 7,
          ibkr_host: '10.0.0.5',
        },
      }),
    );
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('orders will be placed');
    expect(text).not.toContain('no orders placed');

    button(fixture, 'Start Trading').click();
    await Promise.resolve();

    expect(svc.startHostRunner).toHaveBeenCalledWith('run-old', {
      readonly: false,
      hydrate_policy: 'optional',
      strategy: 'rsi_mean_reversion',
      max_orders_per_day: 7,
      ibkr_host: '10.0.0.5',
    });
  });

  it('starts the evidence run with the seeded request and emits changed', async () => {
    const { fixture, svc, component } = render(makeStatus());
    const changed = vi.fn();
    component.changed.subscribe(changed);

    button(fixture, 'Start Trading').click();
    await Promise.resolve();

    expect(svc.startHostRunner).toHaveBeenCalledWith('run-old', {
      readonly: true,
      hydrate_policy: 'optional',
      strategy: 'rsi_mean_reversion',
      max_orders_per_day: 7,
      ibkr_host: '10.0.0.5',
    });
    expect(changed).toHaveBeenCalled();
  });

  it('stops the bound run when the process is live', async () => {
    const { fixture, svc } = render(
      makeStatus({
        process: { state: 'running', bound_run_id: 'run-live' },
        live_binding: { run_id: 'run-live', run_dir: null, source: 'registry' },
      }),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    button(fixture, 'Stop Bot').click();
    await Promise.resolve();

    expect(svc.stopHostRunner).toHaveBeenCalledWith('run-live', { force: false });
  });

  it('disables Start with a visible reason when the daemon is down', () => {
    const { fixture } = render(makeStatus(), /* daemonDown */ true);
    const el = fixture.nativeElement as HTMLElement;

    expect(button(fixture, 'Start Trading').disabled).toBe(true);
    expect(el.querySelector('.disabled-reason')?.textContent).toContain('Host daemon unreachable');
  });

  it('disables Start with a reason when fleet policy blocks new starts', () => {
    const { fixture } = render(makeStatus(), /* daemonDown */ false, /* fleetBlocks */ true);
    const el = fixture.nativeElement as HTMLElement;

    expect(button(fixture, 'Start Trading').disabled).toBe(true);
    expect(el.querySelector('.disabled-reason')?.textContent).toContain('Fleet policy blocks new starts');
  });

  it('disables Start with a reason when there is no run to start', () => {
    const { fixture } = render(makeStatus({ evidence_binding: null, live_binding: null }));
    const el = fixture.nativeElement as HTMLElement;

    expect(button(fixture, 'Start Trading').disabled).toBe(true);
    expect(el.querySelector('.disabled-reason')?.textContent).toContain('No run to start');
  });

  it('renders an inline operation result when start fails (503)', async () => {
    const { fixture, svc, component } = render(makeStatus());
    svc.startHostRunner.mockRejectedValue(
      new HttpErrorResponse({ status: 503, error: { detail: 'host daemon unreachable' } }),
    );

    await component.start();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('host daemon unreachable');
    expect(el.textContent).toContain('Start — service unavailable');
  });
});
