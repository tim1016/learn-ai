import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { LiveRunsService } from '../../../services/live-runs.service';
import { CohortLaunchMonitorComponent } from './cohort-launch-monitor.component';

class FakeLiveRunsService {
  getLatestCohortBatchLaunch = vi.fn();
  getCohortValidationCertificate = vi.fn();
}

describe('CohortLaunchMonitorComponent', () => {
  it('renders only the durable server-authored cohort evidence', async () => {
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
      evidence: {
        sample_count: 2,
        cadence_ms: 5_000,
        healthy_overlap_ms: 10_000,
        verdict: 'failed',
        reason: 'COHORT_MEMBER_HALTED',
        source: 'account_event.cohort_evidence_sample',
        members: [
          {
            strategy_instance_id: 'spy-a',
            run_id: 'run-spy-a',
            verdict: 'healthy',
            reason: null,
            orders_used: 2,
            orders_cap: 4,
          },
          {
            strategy_instance_id: 'spy-b',
            run_id: 'run-spy-b',
            verdict: 'failed',
            reason: 'COHORT_MEMBER_HALTED',
            orders_used: 1,
            orders_cap: 4,
          },
        ],
      },
    });
    liveRuns.getCohortValidationCertificate.mockResolvedValue({
      schema_version: 1,
      account_id: 'DU1234567',
      cohort_id: 'paper-validation-1',
      healthy_overlap_ms: 10_000,
      evidence_verdict: 'failed',
      evidence_reason: 'COHORT_MEMBER_HALTED',
      incidents: [],
      verdict: 'failed',
      reasons: ['FAILED_NAMESPACE_EXPOSURE_NONZERO'],
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
    expect(root.textContent).toContain('Evidence verdict');
    expect(root.textContent).toContain('Account Frozen');
    expect(root.textContent).toContain('Clear the account freeze.');
    expect(root.textContent).toContain('run-spy-a');
    expect(root.textContent).toContain('Cohort Member Halted');
    expect(root.textContent).toContain('Certificate overlap');
    expect(root.textContent).toContain('Failed Namespace Exposure Nonzero');
    expect(root.querySelector('table caption')?.textContent).toContain('latest server observation');
    expect(root.querySelectorAll('th[scope="col"]').length).toBe(5);
    expect(root.querySelector<HTMLButtonElement>('[aria-label="Refresh cohort monitor"]')).toBeTruthy();
  });
});
