import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
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
      imports: [BaselinesDetailPageComponent, NoopAnimationsModule],
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

  it('reads baseline_id from the route and fetches the baseline', () => {
    expect(service.getBaseline).toHaveBeenCalledWith('b'.repeat(32));
    expect(component.baselineId()).toBe('b'.repeat(32));
    expect(component.baseline()?.config.method).toBe('random_ema_windows');
  });

  it('renders the null-distribution card with parent value, percentile, and p-value', () => {
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('sharpe_ratio');
    // parent value 1.85 → '1.8500'
    expect(text).toContain('1.8500');
    // empirical_percentile 0.857 → '85.7%'
    expect(text).toContain('85.7%');
    // p-value 0.222 → '0.2220'
    expect(text).toContain('0.2220');
  });

  it('computes null-array P5/P50/P95 from null_values', () => {
    const summaries = component.nullSummaries();
    expect(summaries.length).toBe(1);
    const s = summaries[0];
    // 8 values: [0.5,0.9,1.1,1.3,1.5,1.7,1.9,2.0]; linear-interp
    // P50 between idx 3 (1.3) and 4 (1.5) at 0.5 → 1.4
    expect(s.p50).toBeCloseTo(1.4, 6);
    // P5 at 0.05*(8-1)=0.35 between 0.5 and 0.9 → 0.5 + 0.4*0.35 = 0.64
    expect(s.p5).toBeCloseTo(0.64, 6);
    // P95 at 0.95*(8-1)=6.65 between 1.9 and 2.0 → 1.9 + 0.1*0.65 = 1.965
    expect(s.p95).toBeCloseTo(1.965, 6);
    expect(s.null_count).toBe(8);
  });

  it('renders a row per sample baseline with parameters and test metrics', () => {
    const rows = fixture.nativeElement.querySelectorAll(
      '.bld-runs-table tbody tr',
    );
    expect(rows.length).toBe(1);
    const row = (rows[0].textContent ?? '').trim();
    expect(row).toContain('fast=5');
    expect(row).toContain('slow=20');
    expect(row).toContain('1.42');
    expect(row).toContain('22');
  });

  it('renders the parent-run link', () => {
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Parent run');
    expect(text).toContain('aaaaaaaaaaaaaaaa');
  });

  it('surfaces service errors', async () => {
    service.getBaseline.mockRejectedValueOnce(new Error('not found'));
    await component.load('e'.repeat(32));
    fixture.detectChanges();
    expect(component.error()).toBe('not found');
  });
});
