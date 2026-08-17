import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ExhaustiveRunService } from '../../../../services/exhaustive-run.service';
import type { ExhaustiveRunResponse } from '../../../../services/exhaustive-run.types';
import { JobsService } from '../../../../services/jobs.service';
import { ExhaustiveRunPanelComponent } from './exhaustive-run-panel.component';

const WALK_FORWARD_ID = 'f'.repeat(32);

function response(): ExhaustiveRunResponse {
  return {
    config: {
      exhaustive_run_id: 'e'.repeat(32),
      source_walk_forward_id: WALK_FORWARD_ID,
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
      exhaustive_run_id: 'e'.repeat(32),
      source_walk_forward_id: WALK_FORWARD_ID,
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
    jobs: ReturnType<typeof signal>;
    job: ReturnType<typeof vi.fn>;
    startJob: ReturnType<typeof vi.fn>;
    cancelJob: ReturnType<typeof vi.fn>;
    fetchResult: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    exhaustiveRuns = { getLatestForWalkForward: vi.fn().mockResolvedValue(response()) };
    jobs = {
      jobs: signal([]),
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
});
