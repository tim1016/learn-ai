import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { LiveRunsService } from '../../../services/live-runs.service';
import { CohortLaunchMonitorComponent } from './cohort-launch-monitor.component';

class FakeLiveRunsService {
  getLatestCohortBatchLaunch = vi.fn();
  getInstanceStatus = vi.fn();
  getStatus = vi.fn();
}

describe('CohortLaunchMonitorComponent', () => {
  it('renders the durable blocker and live per-bot success evidence', async () => {
    const liveRuns = new FakeLiveRunsService();
    liveRuns.getLatestCohortBatchLaunch.mockResolvedValue({
      schema_version: 1,
      account_id: 'DU1234567',
      cohort_id: 'paper-validation-1',
      member_strategy_instance_ids: ['spy-a', 'spy-b'],
      window_start_ms: 1_780_000_000_000,
      window_end_ms: 1_780_000_030_000,
      authorized_by: 'operator.alice',
      authorized_recorded_at_ms: 1_780_000_000_000,
      outcomes_state: 'recorded',
      outcomes: [
        {
          strategy_instance_id: 'spy-a',
          state: 'accepted',
          reason: 'start.request.accepted',
          next_safe_action: 'Monitor receipt state.',
        },
        {
          strategy_instance_id: 'spy-b',
          state: 'blocked',
          reason: 'ACCOUNT_FROZEN',
          next_safe_action: 'Clear the account freeze.',
        },
      ],
      outcomes_recorded_at_ms: 1_780_000_001_000,
      outcomes_error: null,
    });
    liveRuns.getInstanceStatus.mockImplementation(async (instanceId: string) => ({
      strategy_instance_id: instanceId,
      process: {
        state: instanceId === 'spy-a' ? 'running' : 'idle',
        ibkr_client_id: 17,
        started_at_ms: 1_779_999_990_000,
      },
      live_binding: instanceId === 'spy-a' ? { run_id: 'run-spy-a' } : null,
      evidence_binding: instanceId === 'spy-b' ? { run_id: 'run-spy-b' } : null,
      readiness: { orders_cap: 4, orders_used: 2 },
      start_defaults: { max_orders_per_day: 4 },
      broker: { bot_order_namespace: `learn-ai/${instanceId}/v1`, owned_positions: { SPY: 0 } },
    }));
    liveRuns.getStatus.mockResolvedValue({
      executions: { row_count: 2, last_fills: [{ symbol: 'SPY', fill_quantity: 1, fill_price: 500, ts_ms: 1_780_000_001_000 }] },
      trades: { row_count: 1 },
    });
    await TestBed.configureTestingModule({
      imports: [CohortLaunchMonitorComponent],
      providers: [provideZonelessChangeDetection(), { provide: LiveRunsService, useValue: liveRuns }],
    }).compileComponents();
    const fixture = TestBed.createComponent(CohortLaunchMonitorComponent);
    fixture.componentRef.setInput('accountId', 'DU1234567');
    fixture.detectChanges();
    await fixture.componentInstance.refresh();
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('Target count');
    expect(root.textContent).toContain('Account Frozen');
    expect(root.textContent).toContain('Clear the account freeze.');
    expect(root.textContent).toContain('run-spy-a');
    expect(root.textContent).toContain('learn-ai/spy-a/v1: SPY 0');
    expect(root.querySelector('table caption')?.textContent).toContain('Per-bot durable receipt state');
    expect(root.querySelectorAll('th[scope="col"]').length).toBe(7);
    expect(root.querySelector<HTMLButtonElement>('[aria-label="Refresh cohort monitor"]')).toBeTruthy();
  });
});
