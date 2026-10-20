import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MonteCarloService } from '../../../../services/monte-carlo.service';
import type { MonteCarloResponse } from '../../../../services/monte-carlo.types';
import { MonteCarloDetailPageComponent } from './monte-carlo-detail-page.component';

function makeMcResponse(): MonteCarloResponse {
  return {
    config: {
      monte_carlo_id: 'b'.repeat(32),
      parent_run_id: 'a'.repeat(32),
      parent_trade_log_hash: 't'.repeat(64),
      method: 'reshuffle',
      simulation_count: 1000,
      projection_trade_count: 0,
      initial_equity: 100_000,
      random_seed: 42,
      breach_thresholds: [0.05, 0.10],
      created_at_ms: 1736000000000,
    },
    result: {
      monte_carlo_id: 'b'.repeat(32),
      parent_run_id: 'a'.repeat(32),
      method: 'reshuffle',
      simulation_count: 1000,
      realised_trade_count: 25,
      equity_bands: [
        { trade_index: 0, p5: 100_000, p50: 100_000, p95: 100_000 },
        { trade_index: 1, p5: 99_000, p50: 100_500, p95: 102_000 },
        { trade_index: 2, p5: 98_500, p50: 101_200, p95: 104_500 },
      ],
      drawdown_quantiles: { p5: 0.0123, p50: 0.0567, p95: 0.1234 },
      terminal_pnl_quantiles: { p5: -2500, p50: 1500, p95: 6800 },
      max_losing_streak_quantiles: { p5: 1, p50: 2, p95: 5 },
      breach_probabilities: [
        { threshold: 0.05, probability: 0.78 },
        { threshold: 0.10, probability: 0.32 },
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
  getMonteCarlo: ReturnType<typeof vi.fn>;
  listMonteCarlos: ReturnType<typeof vi.fn>;
  createMonteCarlo: ReturnType<typeof vi.fn>;
  runReshuffleFromRun: ReturnType<typeof vi.fn>;
}

describe('MonteCarloDetailPageComponent', () => {
  let fixture: ComponentFixture<MonteCarloDetailPageComponent>;
  let component: MonteCarloDetailPageComponent;
  let service: ServiceMock;

  beforeEach(async () => {
    service = {
      getMonteCarlo: vi.fn().mockResolvedValue(makeMcResponse()),
      listMonteCarlos: vi.fn(),
      createMonteCarlo: vi.fn(),
      runReshuffleFromRun: vi.fn(),
    };

    await TestBed.configureTestingModule({
      imports: [MonteCarloDetailPageComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: MonteCarloService, useValue: service },
        {
          provide: ActivatedRoute,
          useValue: {
            paramMap: of(convertToParamMap({ mc_id: 'b'.repeat(32) })),
          },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(MonteCarloDetailPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('loads the MC named by the route and shows its quantile cards, breach-probability table and parent-run link', () => {
    expect.soft(service.getMonteCarlo, 'fetches the MC named by the route').toHaveBeenCalledWith('b'.repeat(32));
    expect.soft(component.mcId(), 'reads mc_id from the route').toBe('b'.repeat(32));
    expect.soft(component.monteCarlo()?.config.method, 'holds the fetched MC').toBe('reshuffle');

    const text: string = fixture.nativeElement.textContent ?? '';
    expect.soft(text, 'quantile cards show the max drawdown card').toContain('Max drawdown');
    expect.soft(text, 'quantile cards show the terminal P&L card').toContain('Terminal P&L');
    expect.soft(text, 'quantile cards show the max losing streak card').toContain('Max losing streak');
    // 0.0567 → '5.67%'
    expect.soft(text, 'quantile cards show the drawdown median').toContain('5.67%');
    // 1500 (terminal P50) → '$1,500'
    expect.soft(text, 'quantile cards show the terminal P&L median').toContain('$1,500');
    expect.soft(text, 'parent-run link is labelled').toContain('Parent run');
    expect.soft(text, 'parent-run link shows the parent run id').toContain('aaaaaaaaaaaaaaaa');

    const rows = fixture.nativeElement.querySelectorAll('.mcd-breach-table tbody tr');
    expect(rows.length, 'breach-probability table has a row per threshold').toBe(2);
    const first = (rows[0].textContent ?? '').trim();
    // 0.05 threshold → '5%' and 0.78 probability → '78.0%' (PercentPipe '1.1-1')
    expect.soft(first, 'breach-probability row shows the threshold').toContain('5%');
    expect.soft(first, 'breach-probability row shows the probability').toContain('78.0%');
  });

  it('surfaces service errors', async () => {
    service.getMonteCarlo.mockRejectedValueOnce(new Error('not found'));
    await component.load('e'.repeat(32));
    fixture.detectChanges();
    expect(component.error()).toBe('not found');
  });

  it('clears stale payload before a new load — failed load does not show old data', async () => {
    expect(component.monteCarlo()).not.toBeNull();
    service.getMonteCarlo.mockRejectedValueOnce(new Error('not found'));
    await component.load('e'.repeat(32));
    expect(component.monteCarlo()).toBeNull();
    expect(component.error()).toBe('not found');
  });

  it('discards out-of-order responses when a newer load is in flight', async () => {
    // Resolve the *first* call slowly, *second* call quickly. Verify
    // the first's response is dropped because the second changed the
    // load token.
    let resolveFirst: (value: MonteCarloResponse) => void = () => {
      throw new Error('First Monte Carlo request did not expose a resolver');
    };
    const firstPromise = new Promise<MonteCarloResponse>((res) => {
      resolveFirst = res;
    });
    const stalePayload: MonteCarloResponse = {
      ...makeMcResponse(),
      config: { ...makeMcResponse().config, monte_carlo_id: 'd'.repeat(32) },
    };
    const newerPayload: MonteCarloResponse = {
      ...makeMcResponse(),
      config: { ...makeMcResponse().config, monte_carlo_id: 'e'.repeat(32) },
    };
    service.getMonteCarlo.mockReset();
    service.getMonteCarlo.mockReturnValueOnce(firstPromise);
    service.getMonteCarlo.mockResolvedValueOnce(newerPayload);

    const firstLoad = component.load('d'.repeat(32));
    const secondLoad = component.load('e'.repeat(32));
    await secondLoad;
    expect(component.monteCarlo()?.config.monte_carlo_id).toBe('e'.repeat(32));

    // Now the first call resolves — late. Its payload must NOT
    // overwrite the newer one, and ``loading`` must NOT be flipped
    // back on by the late call (the second load's finally already
    // set it false).
    resolveFirst(stalePayload);
    await firstLoad;
    expect(component.monteCarlo()?.config.monte_carlo_id).toBe('e'.repeat(32));
    expect(component.loading()).toBe(false);
  });
});
