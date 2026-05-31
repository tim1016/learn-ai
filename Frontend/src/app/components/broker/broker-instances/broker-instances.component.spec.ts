import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  LiveInstanceStatus,
  LiveInstanceSummary,
} from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerInstancesComponent } from './broker-instances.component';

const FLEET: LiveInstanceSummary[] = [
  {
    strategy_instance_id: 'spy_ema_paper',
    process_state: 'running',
    bound_run_id: 'run-live',
    latest_run_id: 'run-live',
  },
  {
    strategy_instance_id: 'spy_vwap_shadow',
    process_state: 'offline',
    bound_run_id: null,
    latest_run_id: 'run-old',
  },
];

function makeStatus(overrides: Partial<LiveInstanceStatus> = {}): LiveInstanceStatus {
  return {
    strategy_instance_id: 'spy_ema_paper',
    process: { state: 'running', pid: 99, bound_run_id: 'run-live', started_at_ms: 1 },
    live_binding: { run_id: 'run-live', run_dir: null, source: 'registry' },
    evidence_binding: { run_id: 'run-live', state: 'latest_run_by_ledger', is_live: false },
    desired_state: {
      state: 'RUNNING',
      updated_at_ms: 1,
      updated_by: 'operator',
      reason: null,
      version: 1,
      path_status: 'ok',
    },
    fetched_at_ms: 1,
    ...overrides,
  };
}

class FakeLiveRunsService {
  getInstances = vi.fn().mockResolvedValue(FLEET);
  getInstanceStatus = vi.fn().mockResolvedValue(makeStatus());
  setInstanceDesiredState = vi.fn().mockResolvedValue({
    durable: { state: 'PAUSED', updated_at_ms: 1, updated_by: 'operator', reason: null, version: 2 },
    actuation: {
      actuated: true,
      run_id: 'run-live',
      command_seq: 1,
      detail: 'PAUSE queued on run-live; awaiting ack',
    },
  });
}

/** Flush microtask queue and Angular effect queue (resource loads). */
async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  TestBed.flushEffects();
}

let activeFixture: { destroy(): void } | null = null;

function setup() {
  const svc = new FakeLiveRunsService();
  TestBed.configureTestingModule({
    providers: [{ provide: LiveRunsService, useValue: svc }],
  });
  const fixture = TestBed.createComponent(BrokerInstancesComponent);
  activeFixture = fixture;
  fixture.detectChanges();
  return { fixture, svc, component: fixture.componentInstance };
}

afterEach(() => {
  activeFixture?.destroy();
  activeFixture = null;
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('BrokerInstancesComponent', () => {
  it('lists every strategy instance from the fleet endpoint', async () => {
    const { fixture } = setup();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('spy_ema_paper');
    expect(text).toContain('spy_vwap_shadow');
  });

  it('shows the live binding when a running instance is selected', async () => {
    const { fixture, component, svc } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(svc.getInstanceStatus).toHaveBeenCalledWith('spy_ema_paper');
    expect(fixture.nativeElement.textContent).toContain('run-live (live)');
  });

  it('labels a dead instance as stale evidence with commands disabled', async () => {
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(
      makeStatus({
        process: { state: 'idle' },
        live_binding: null,
        evidence_binding: { run_id: 'run-old', state: 'latest_run_by_ledger', is_live: false },
      }),
    );
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('stale evidence');
    expect(text).toContain('gate the next start');
  });

  it('issues durable intent and surfaces the actuation result', async () => {
    const { fixture, component, svc } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    await component.setIntent('pause');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(svc.setInstanceDesiredState).toHaveBeenCalledWith('spy_ema_paper', { action: 'pause' });
    expect(fixture.nativeElement.textContent).toContain('PAUSE queued on run-live');
  });
});
