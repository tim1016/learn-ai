import { provideZonelessChangeDetection } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { EngineValidationAnalytics } from '../engine-validation-analytics.types';
import { ValidationAtlasComponent } from './validation-atlas.component';

const RUN_END_MS = Date.UTC(2026, 6, 10, 20, 0, 0);

function analytics(): EngineValidationAnalytics {
  return {
    horizons: [
      {
        key: '2w',
        label: '2 weeks',
        start_ms_utc: RUN_END_MS - 14 * 86_400_000,
        end_ms_utc: RUN_END_MS,
        has_full_coverage: true,
        net_return: 0.02,
        trade_count: 12,
        win_rate: 0.58,
        profit_factor: 1.42,
      },
      {
        key: '1m',
        label: '1 month',
        start_ms_utc: RUN_END_MS - 30 * 86_400_000,
        end_ms_utc: RUN_END_MS,
        has_full_coverage: false,
        net_return: null,
        trade_count: 12,
        win_rate: 0.58,
        profit_factor: 1.42,
      },
    ],
    timing_cells: [
      {
        weekday: 0,
        weekday_label: 'Mon',
        hour_et: 9,
        trade_count: 3,
        win_rate: 0.67,
        average_return: 0.004,
      },
      {
        weekday: 1,
        weekday_label: 'Tue',
        hour_et: 10,
        trade_count: 2,
        win_rate: 0.5,
        average_return: -0.002,
      },
    ],
    seasonality: [
      { month: 1, month_label: 'Jan', observation_count: 2, median_compounded_return: 0.031 },
      { month: 2, month_label: 'Feb', observation_count: 1, median_compounded_return: -0.012 },
    ],
    rolling_trade_stability: [
      {
        trade_number: 20,
        end_ms_utc: RUN_END_MS,
        window_size: 20,
        average_return: 0.003,
        win_rate: 0.6,
      },
    ],
  };
}

describe('ValidationAtlasComponent', () => {
  it('renders Python-owned validation analytics for horizons, timing, seasonality, and stability', async () => {
    await render(ValidationAtlasComponent, {
      inputs: { analytics: analytics() },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByRole('heading', { name: 'Performance memory' })).toBeTruthy();
    expect(screen.getByText('Calculated by Python')).toBeTruthy();
    expect(screen.getByText('2 weeks')).toBeTruthy();
    expect(screen.getByText('+2.00%')).toBeTruthy();
    expect(screen.getByText('Needs more data')).toBeTruthy();
    expect(screen.getByText('Entry expectancy by weekday and hour')).toBeTruthy();
    expect(screen.getByText('n=3')).toBeTruthy();
    expect(screen.getByText('Calendar-month seasonality')).toBeTruthy();
    expect(screen.getByText('Jan')).toBeTruthy();
    expect(screen.getByText('Rolling 20-trade stability')).toBeTruthy();
  });
});
