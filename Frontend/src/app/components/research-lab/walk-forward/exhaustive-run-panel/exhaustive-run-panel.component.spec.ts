import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ExhaustiveRunService } from '../../../../services/exhaustive-run.service';
import type { ExhaustiveRunResponse } from '../../../../services/exhaustive-run.types';
import { JobsService, type JobState } from '../../../../services/jobs.service';
import { ExhaustiveRunPanelComponent } from './exhaustive-run-panel.component';

const WALK_FORWARD_ID = 'f'.repeat(32);

function response(
  walkForwardId = WALK_FORWARD_ID,
  exhaustiveRunId = 'e'.repeat(32),
): ExhaustiveRunResponse {
  return {
    config: {
      exhaustive_run_id: exhaustiveRunId,
      source_walk_forward_id: walkForwardId,
      protocol_id: 'spy-ema-exhaustive-run',
      protocol_version: '1.0',
      source_protocol_id: 'spy-ema-normalized-gap',
      source_protocol_version: '1.0',
      start_ms: 1,
      end_ms: 3,
      recent_window_start_ms: 2,
      max_candidates_per_fold: 5,
      ranking_method: 'equal_weight_train_sharpe_return_percentile',
      initial_cash: 100_000,
      fill_mode: 'next_bar_open',
      commission_per_order: 0,
      slippage_per_share: 0,
      random_seed: 0,
      data_root_revision: 'revision',
      created_at_ms: 1,
    },
    result: {
      exhaustive_run_id: exhaustiveRunId,
      source_walk_forward_id: walkForwardId,
      selections: [],
      candidates: [],
      warnings: [],
      status: 'completed',
      failure_reason: null,
      created_at_ms: 1,
      completed_at_ms: 2,
    },
  };
}

describe('ExhaustiveRunPanelComponent', () => {
  let fixture: ComponentFixture<ExhaustiveRunPanelComponent>;
  let exhaustiveRuns: { getLatestForWalkForward: ReturnType<typeof vi.fn> };
  let jobs: {
    jobs: ReturnType<typeof signal<JobState[]>>;
    job: ReturnType<typeof vi.fn>;
    startJob: ReturnType<typeof vi.fn>;
    cancelJob: ReturnType<typeof vi.fn>;
    fetchResult: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    exhaustiveRuns = { getLatestForWalkForward: vi.fn().mockResolvedValue(response()) };
    jobs = {
      jobs: signal<JobState[]>([]),
      job: vi.fn(),
      startJob: vi.fn().mockResolvedValue('job-id'),
      cancelJob: vi.fn().mockResolvedValue(undefined),
      fetchResult: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [ExhaustiveRunPanelComponent],
      providers: [
        provideRouter([]),
        { provide: ExhaustiveRunService, useValue: exhaustiveRuns },
        { provide: JobsService, useValue: jobs },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(ExhaustiveRunPanelComponent);
    fixture.componentRef.setInput('walkForwardId', WALK_FORWARD_ID);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  });

  it('separates descriptive full-data fit from OOS evidence', () => {
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(exhaustiveRuns.getLatestForWalkForward).toHaveBeenCalledWith(WALK_FORWARD_ID);
    expect(text).toContain('Full two-year fit');
    expect(text).toContain('selection/look-ahead bias');
    expect(text).toContain('18-fold forward stability');
    expect(text).toContain('Forward stability · 18 OOS folds');
  });

  it('starts the frozen job with the source receipt id', async () => {
    await fixture.componentInstance.runAnalysis();

    expect(jobs.startJob).toHaveBeenCalledWith('spy_ema_exhaustive', {
      walk_forward_id: WALK_FORWARD_ID,
    });
  });

  it('resumes only an active job scoped to the current receipt', async () => {
    jobs.jobs.set([
      queuedJob('unrelated-job', 'a'.repeat(32)),
      queuedJob('matching-job', WALK_FORWARD_ID),
    ]);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(fixture.componentInstance.jobId()).toBe('matching-job');
  });

  it('does not resume an unscoped job of the same type', async () => {
    jobs.jobs.set([queuedJob('unscoped-job')]);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(fixture.componentInstance.jobId()).toBeNull();
  });

  it('discards an older evidence response after the receipt changes', async () => {
    const firstId = '1'.repeat(32);
    const secondId = '2'.repeat(32);
    const first = deferred<ExhaustiveRunResponse>();
    const second = deferred<ExhaustiveRunResponse>();
    exhaustiveRuns.getLatestForWalkForward.mockImplementation((id: string) => {
      if (id === firstId) return first.promise;
      if (id === secondId) return second.promise;
      return Promise.resolve(response());
    });

    fixture.componentRef.setInput('walkForwardId', firstId);
    fixture.detectChanges();
    fixture.componentRef.setInput('walkForwardId', secondId);
    fixture.detectChanges();
    second.resolve(response(secondId, '2'.repeat(32)));
    await second.promise;
    fixture.detectChanges();
    first.resolve(response(firstId, '1'.repeat(32)));
    await first.promise;
    fixture.detectChanges();

    expect(fixture.componentInstance.response()?.config.source_walk_forward_id).toBe(secondId);
  });

  it('does not let the initial lookup overwrite a completed job result', async () => {
    const receiptId = '3'.repeat(32);
    const latest = deferred<ExhaustiveRunResponse>();
    exhaustiveRuns.getLatestForWalkForward.mockImplementation((id: string) =>
      id === receiptId ? latest.promise : Promise.resolve(response()));
    jobs.job.mockReturnValue({
      ...queuedJob('completed-job', receiptId),
      status: 'completed',
    });
    jobs.fetchResult.mockResolvedValue(response(receiptId, '4'.repeat(32)));

    fixture.componentRef.setInput('walkForwardId', receiptId);
    fixture.detectChanges();
    jobs.jobs.set([queuedJob('completed-job', receiptId)]);
    fixture.componentInstance.jobId.set('completed-job');
    fixture.detectChanges();
    await Promise.resolve();
    fixture.detectChanges();

    latest.resolve(response(receiptId, '5'.repeat(32)));
    await latest.promise;
    fixture.detectChanges();

    expect(fixture.componentInstance.response()?.config.exhaustive_run_id).toBe('4'.repeat(32));
    expect(fixture.componentInstance.loading()).toBe(false);
  });
});

function queuedJob(id: string, walkForwardId?: string): JobState {
  return {
    id,
    type: 'spy_ema_exhaustive',
    status: 'queued',
    parameters: walkForwardId === undefined ? undefined : { walk_forward_id: walkForwardId },
    recentLogs: [],
    logSeq: 0,
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolver) => {
    resolve = resolver;
  });
  return { promise, resolve };
}
