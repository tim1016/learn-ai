import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { StrategyPreflightComponent } from './strategy-preflight.component';

const PREFLIGHT_URL = `${environment.pythonServiceUrl}/research/data-divergence/preflight`;

async function renderPreflight() {
  const result = await render(StrategyPreflightComponent, {
    providers: [provideHttpClient(), provideHttpClientTesting(), ...fakePickerWorld().providers],
  });
  return { ...result, http: TestBed.inject(HttpTestingController), component: result.fixture.componentInstance };
}

describe('StrategyPreflightComponent', () => {
  it('opens without a request: nothing is checked until the button is pressed', async () => {
    const { http } = await renderPreflight();

    http.expectNone(() => true);
    expect(screen.getByRole('button', { name: 'Run pre-flight checks' })).toBeTruthy();
  });

  it('sends the strategy as configured and shows the verdict with each check and its fix', async () => {
    const { http } = await renderPreflight();

    await userEvent.click(screen.getByRole('button', { name: 'Run pre-flight checks' }));
    const request = http.expectOne(PREFLIGHT_URL);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toMatchObject({
      strategy_name: 'spy_ema_crossover',
      symbol: 'SPY',
      start_date: '2024-03-28',
      end_date: '2026-03-27',
      timeframe: '15m',
      session_filter: 'unspecified',
      warmup_days: 0,
      dividend_adjustment: false,
      indicators: [{ name: 'ema', length: 5 }, { name: 'ema', length: 10 }, { name: 'rsi', length: 14 }],
    });
    request.flush({
      overall: 'blocking',
      summary: '1 blocking issue',
      checks: [
        { id: 'warmup', label: 'Indicator warmup buffer', status: 'blocking', message: 'Warmup is far too short.', fix_hint: 'Increase warmup_days to at least 8.' },
        { id: 'dividend_adjustment', label: 'Dividend adjustment', status: 'ok', message: 'Unadjusted prices.', fix_hint: null },
      ],
    });

    expect(await screen.findByText('DO NOT run — blocking issues must be fixed first.')).toBeTruthy();
    expect(screen.getByText('1 blocking issue')).toBeTruthy();
    expect(screen.getByText('Warmup is far too short.')).toBeTruthy();
    expect(screen.getByText('Increase warmup_days to at least 8.')).toBeTruthy();
    expect(screen.getByText('Unadjusted prices.')).toBeTruthy();
    http.verify();
  });

  it('refuses a sampling the check does not take, sending nothing', async () => {
    const { http, component } = await renderPreflight();
    component.range.update((range) => ({ ...range, resolution: 'minute', multiplier: 60 }));

    await userEvent.click(screen.getByRole('button', { name: 'Run pre-flight checks' }));

    expect(await screen.findByText(/preflight only accepts 5m \/ 15m \/ 1h/)).toBeTruthy();
    http.expectNone(PREFLIGHT_URL);
  });

  it('says the call failed rather than showing a verdict', async () => {
    const { http } = await renderPreflight();

    await userEvent.click(screen.getByRole('button', { name: 'Run pre-flight checks' }));
    http.expectOne(PREFLIGHT_URL).flush({ detail: 'boom' }, { status: 500, statusText: 'Server Error' });

    expect(await screen.findByText(/Preflight call failed/)).toBeTruthy();
    await waitFor(() => expect(screen.queryByText(/Ready to run/)).toBeNull());
  });
});
