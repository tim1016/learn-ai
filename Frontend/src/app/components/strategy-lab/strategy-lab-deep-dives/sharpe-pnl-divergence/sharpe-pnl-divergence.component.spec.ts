import { provideZonelessChangeDetection } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { SharpePnlDivergence } from '../../../lean-engine/engine-results/engine-validation-analytics.types';
import {
  SHARPE_PNL_DIVERGENCE_CHART_FACTORY,
  SharpePnlDivergenceComponent,
} from './sharpe-pnl-divergence.component';

function study(): SharpePnlDivergence {
  return {
    error: null,
    return_window_sessions: 20,
    trend_window_sessions: 20,
    annualization_sessions: 252,
    minimum_observation_count: 40,
    daily_observation_count: 60,
    eligible_observation_count: 21,
    divergence_observation_count: 6,
    divergence_ratio: 6 / 21,
    longest_divergence_streak: 3,
    latest_rolling_sharpe: 0.42,
    latest_cumulative_pnl: 8_200.5,
    currently_diverging: true,
    points: [
      {
        timestamp_ms_utc: Date.UTC(2026, 5, 1, 20),
        cumulative_pnl: 7_900,
        rolling_sharpe: 0.58,
        is_divergence: false,
        is_trend_eligible: true,
      },
      {
        timestamp_ms_utc: Date.UTC(2026, 5, 2, 20),
        cumulative_pnl: 8_200.5,
        rolling_sharpe: 0.42,
        is_divergence: true,
        is_trend_eligible: true,
      },
    ],
  };
}

describe('SharpePnlDivergenceComponent', () => {
  const pnlSeries = { setData: vi.fn() };
  const sharpeSeries = { setData: vi.fn() };
  const divergenceSeries = { setData: vi.fn() };
  const chart = {
    addSeries: vi
      .fn()
      .mockReturnValueOnce(pnlSeries)
      .mockReturnValueOnce(sharpeSeries)
      .mockReturnValueOnce(divergenceSeries),
    priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
    timeScale: vi.fn(() => ({ fitContent: vi.fn() })),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    chart.addSeries
      .mockReset()
      .mockReturnValueOnce(pnlSeries)
      .mockReturnValueOnce(sharpeSeries)
      .mockReturnValueOnce(divergenceSeries);
    const theme: Record<string, string> = {
      '--bg-surface': '#131722',
      '--text-subtle': '#9598a1',
      '--border': '#1e222d',
      '--border-light': '#2a2e39',
      '--text-secondary': '#b2b5be',
      '--bg-elevated': '#1b1f2e',
      '--accent-text': '#7aa9ff',
      '--warn': '#ff9800',
      '--bear-soft': 'rgba(239, 83, 80, 0.15)',
    };
    vi.stubGlobal('getComputedStyle', () => ({
      getPropertyValue: (token: string) => theme[token] ?? '',
    }));
  });

  afterEach(() => vi.unstubAllGlobals());

  it('renders Python-authored divergence evidence and maps all three chart series', async () => {
    await render(SharpePnlDivergenceComponent, {
      inputs: { study: study() },
      providers: [
        provideZonelessChangeDetection(),
        { provide: SHARPE_PNL_DIVERGENCE_CHART_FACTORY, useValue: vi.fn(() => chart) },
      ],
    });

    expect(screen.getByRole('heading', { name: 'Rolling Sharpe vs cumulative P&L' })).toBeTruthy();
    expect(screen.getByText('Calculated by Python')).toBeTruthy();
    expect(screen.getByText('Diverging now')).toBeTruthy();
    expect(screen.getByText('28.6%')).toBeTruthy();
    expect(screen.getByText(/exploratory decay diagnostic/i)).toBeTruthy();
    expect(pnlSeries.setData).toHaveBeenCalledOnce();
    expect(sharpeSeries.setData).toHaveBeenCalledOnce();
    expect(divergenceSeries.setData).toHaveBeenCalledOnce();
    expect(divergenceSeries.setData.mock.calls[0][0][1]).toMatchObject({ value: 1 });
  });

  it('states when the run is too short for the two rolling windows', async () => {
    await render(SharpePnlDivergenceComponent, {
      inputs: {
        study: {
          ...study(),
          eligible_observation_count: 0,
          divergence_observation_count: 0,
          divergence_ratio: null,
          latest_rolling_sharpe: null,
          currently_diverging: null,
          points: [],
        },
      },
      providers: [
        provideZonelessChangeDetection(),
        { provide: SHARPE_PNL_DIVERGENCE_CHART_FACTORY, useValue: vi.fn(() => chart) },
      ],
    });

    expect(screen.getByText(/at least 40 daily observations are required/i)).toBeTruthy();
  });
});
