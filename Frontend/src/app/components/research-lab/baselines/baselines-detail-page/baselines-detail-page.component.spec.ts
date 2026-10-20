import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BaselinesService } from '../../../../services/baselines.service';
import type { BaselineResponse } from '../../../../services/baselines.types';
import { BaselinesDetailPageComponent } from './baselines-detail-page.component';

function makeBaselineResponse(): BaselineResponse {
  return {
    config: {
      baseline_id: 'b'.repeat(32),
      parent_run_id: 'a'.repeat(32),
      parent_trade_log_hash: 't'.repeat(64),
      method: 'random_ema_windows',
      sample_count: 30,
      random_seed: 7,
      method_params: { fast_range: [3, 12], slow_range: [10, 30] },
      target_metrics: ['sharpe_ratio', 'total_return_pct'],
      created_at_ms: 1736000000000,
    },
    result: {
      baseline_id: 'b'.repeat(32),
      parent_run_id: 'a'.repeat(32),
      method: 'random_ema_windows',
      sample_count: 30,
      baselines: [
        {
          baseline_run_id: 'r'.repeat(32),
          method: 'random_ema_windows',
          parameters: { fast: 5, slow: 20 },
          test_metrics: {
            total_trades: 22,
            winning_trades: 12,
            losing_trades: 10,
            win_rate: 0.55,
            total_return_pct: 0.0825,
            max_drawdown_pct: 0.0312,
            sharpe_ratio: 1.42,
            sortino_ratio: null,
            profit_factor: null,
            expectancy_pct: null,
            payoff_ratio: null,
            exposure_pct: null,
            avg_trade_bars: null,
          },
          test_trade_count: 22,
          status: 'completed',
          failure_reason: null,
        },
      ],
      null_distributions: [
        {
          metric_name: 'sharpe_ratio',
          parent_value: 1.85,
          null_values: [0.5, 0.9, 1.1, 1.3, 1.5, 1.7, 1.9, 2.0],
          empirical_percentile: 0.857,
          empirical_p_value: 0.222,
        },
      ],
      warnings: [],
      created_at_ms: 1736000000000,
      completed_at_ms: 1736000005000,
      status: 'completed',
      failure_reason: null,
    },
  };
}

interface ServiceMock {
  getBaseline: ReturnType<typeof vi.fn>;
  listBaselines: ReturnType<typeof vi.fn>;
  createBaseline: ReturnType<typeof vi.fn>;
  runFromRun: ReturnType<typeof vi.fn>;
}

describe('BaselinesDetailPageComponent', () => {
  let fixture: ComponentFixture<BaselinesDetailPageComponent>;
  let component: BaselinesDetailPageComponent;
  let service: ServiceMock;

  beforeEach(async () => {
    service = {
      getBaseline: vi.fn().mockResolvedValue(makeBaselineResponse()),
      listBaselines: vi.fn(),
      createBaseline: vi.fn(),
      runFromRun: vi.fn(),
    };

    await TestBed.configureTestingModule({
      imports: [BaselinesDetailPageComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: BaselinesService, useValue: service },
        {
          provide: ActivatedRoute,
          useValue: {
            paramMap: of(convertToParamMap({ baseline_id: 'b'.repeat(32) })),
          },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(BaselinesDetailPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('loads the baseline named by the route and shows its null-distribution card, null-array P5/P50/P95, sample baseline row and parent-run link', () => {
    expect.soft(service.getBaseline, 'fetches the baseline named by the route').toHaveBeenCalledWith('b'.repeat(32));
    expect.soft(component.baselineId(), 'reads baseline_id from the route').toBe('b'.repeat(32));
    expect.soft(component.baseline()?.config.method, 'holds the fetched baseline').toBe('random_ema_windows');

    const text: string = fixture.nativeElement.textContent ?? '';
    expect.soft(text, 'null-distribution card names the metric').toContain('sharpe_ratio');
    // parent value 1.85 → '1.8500'
    expect.soft(text, 'null-distribution card shows the parent value').toContain('1.8500');
    // empirical_percentile 0.857 → '85.7%'
    expect.soft(text, 'null-distribution card shows the percentile').toContain('85.7%');
    // p-value 0.222 → '0.2220'
    expect.soft(text, 'null-distribution card shows the p-value').toContain('0.2220');
    expect.soft(text, 'parent-run link is labelled').toContain('Parent run');
    expect.soft(text, 'parent-run link shows the parent run id').toContain('aaaaaaaaaaaaaaaa');

    const summaries = component.nullSummaries();
    expect(summaries.length, 'computes one null-array summary').toBe(1);
    const s = summaries[0];
    // 8 values: [0.5,0.9,1.1,1.3,1.5,1.7,1.9,2.0]; linear-interp
    // P50 between idx 3 (1.3) and 4 (1.5) at 0.5 → 1.4
    expect.soft(s.p50, 'null-array P50 is linearly interpolated from null_values').toBeCloseTo(1.4, 6);
    // P5 at 0.05*(8-1)=0.35 between 0.5 and 0.9 → 0.5 + 0.4*0.35 = 0.64
    expect.soft(s.p5, 'null-array P5 is linearly interpolated from null_values').toBeCloseTo(0.64, 6);
    // P95 at 0.95*(8-1)=6.65 between 1.9 and 2.0 → 1.9 + 0.1*0.65 = 1.965
    expect.soft(s.p95, 'null-array P95 is linearly interpolated from null_values').toBeCloseTo(1.965, 6);
    expect.soft(s.null_count, 'null-array summary counts the null values').toBe(8);

    const rows = fixture.nativeElement.querySelectorAll(
      '.bld-runs-table tbody tr',
    );
    expect(rows.length, 'renders a row per sample baseline').toBe(1);
    const row = (rows[0].textContent ?? '').trim();
    expect.soft(row, 'sample baseline row shows the fast parameter').toContain('fast=5');
    expect.soft(row, 'sample baseline row shows the slow parameter').toContain('slow=20');
    expect.soft(row, 'sample baseline row shows the Sharpe test metric').toContain('1.42');
    expect.soft(row, 'sample baseline row shows the test trade count').toContain('22');
  });

  it('surfaces service errors', async () => {
    service.getBaseline.mockRejectedValueOnce(new Error('not found'));
    await component.load('e'.repeat(32));
    fixture.detectChanges();
    expect(component.error()).toBe('not found');
  });
});
