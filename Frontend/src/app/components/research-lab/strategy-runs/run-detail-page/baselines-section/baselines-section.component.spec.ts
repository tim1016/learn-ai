import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Router } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BaselinesService } from '../../../../../services/baselines.service';
import type {
  BaselineConfig,
  BaselineListResponse,
  BaselineResponse,
} from '../../../../../services/baselines.types';
import { BaselinesSectionComponent } from './baselines-section.component';
import { makeRunLedger } from '../../../../../testing/strategy-run-fixtures';

function makeBaselineConfig(overrides: Partial<BaselineConfig> = {}): BaselineConfig {
  return {
    baseline_id: 'b'.repeat(32),
    parent_run_id: 'a'.repeat(32),
    parent_trade_log_hash: 't'.repeat(64),
    method: 'buy_and_hold',
    sample_count: 1,
    random_seed: 0,
    method_params: {},
    target_metrics: [],
    created_at_ms: 1736000000000,
    ...overrides,
  };
}

function makeBaselineResponse(): BaselineResponse {
  return {
    config: makeBaselineConfig(),
    result: {
      baseline_id: 'b'.repeat(32),
      parent_run_id: 'a'.repeat(32),
      method: 'buy_and_hold',
      sample_count: 1,
      baselines: [],
      null_distributions: [],
      warnings: [],
      created_at_ms: 1736000000000,
      completed_at_ms: 1736000005000,
      status: 'completed',
      failure_reason: null,
    },
  };
}

interface ServiceMock {
  listBaselines: ReturnType<typeof vi.fn>;
  runFromRun: ReturnType<typeof vi.fn>;
  createBaseline: ReturnType<typeof vi.fn>;
  getBaseline: ReturnType<typeof vi.fn>;
}

describe('BaselinesSectionComponent', () => {
  let fixture: ComponentFixture<BaselinesSectionComponent>;
  let component: BaselinesSectionComponent;
  let service: ServiceMock;
  let routerNavigate: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    const empty: BaselineListResponse = { baselines: [] };
    service = {
      listBaselines: vi.fn().mockResolvedValue(empty),
      runFromRun: vi.fn().mockResolvedValue(makeBaselineResponse()),
      createBaseline: vi.fn(),
      getBaseline: vi.fn(),
    };
    routerNavigate = vi.fn().mockResolvedValue(true);

    await TestBed.configureTestingModule({
      imports: [BaselinesSectionComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: BaselinesService, useValue: service },
        { provide: Router, useValue: { navigate: routerNavigate } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(BaselinesSectionComponent);
    fixture.componentRef.setInput('run', makeRunLedger());
    component = fixture.componentInstance;
    await fixture.whenStable();
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('loads baselines filtered by parent_run_id on input arrival', () => {
    expect(service.listBaselines).toHaveBeenCalledWith({
      parent_run_id: 'a'.repeat(32),
      limit: 50,
    });
  });

  it('runBuyAndHold calls runFromRun with method=buy_and_hold and refreshes', async () => {
    service.listBaselines.mockClear();
    await component.runBuyAndHold();
    expect(service.runFromRun).toHaveBeenCalledOnce();
    const [, method] = service.runFromRun.mock.calls[0];
    expect(method).toBe('buy_and_hold');
    expect(service.listBaselines).toHaveBeenCalledOnce();
    expect(component.lastBaselineId()).toBe('b'.repeat(32));
  });

  it('runRandomEmaWindows calls runFromRun with method=random_ema_windows', async () => {
    await component.runRandomEmaWindows();
    expect(service.runFromRun).toHaveBeenCalledOnce();
    const [, method] = service.runFromRun.mock.calls[0];
    expect(method).toBe('random_ema_windows');
  });

  it('open(b) navigates to the baseline detail route', () => {
    component.open(makeBaselineConfig({ baseline_id: 'c'.repeat(32) }));
    expect(routerNavigate).toHaveBeenCalledWith([
      '/research/history/baseline',
      'c'.repeat(32),
    ]);
  });

  it('renders existing baselines in the table', async () => {
    service.listBaselines.mockResolvedValueOnce({
      baselines: [
        makeBaselineConfig({
          baseline_id: 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
          method: 'random_ema_windows',
          sample_count: 30,
          random_seed: 42,
        }),
      ],
    });
    await component.refresh();
    fixture.detectChanges();
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('eeeeeeeeeeeeeeee');
    expect(text).toContain('random_ema_windows');
    expect(text).toContain('30');
    expect(text).toContain('42');
  });

  it('surfaces service errors', async () => {
    service.listBaselines.mockRejectedValueOnce(new Error('network down'));
    await component.refresh();
    expect(component.error()).toBe('network down');
  });

  it('disables CTAs while a method is running (anyRunning)', async () => {
    const promise = component.runBuyAndHold();
    expect(component.running()).toBe('buy_and_hold');
    expect(component.anyRunning()).toBe(true);
    await promise;
    expect(component.running()).toBeNull();
    expect(component.anyRunning()).toBe(false);
  });
});
