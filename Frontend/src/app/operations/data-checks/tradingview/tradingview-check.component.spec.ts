import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { TradingviewCheckComponent } from './tradingview-check.component';

/** The check as a link opens it: the scope the link carried is its inputs. */
async function renderCheck(scope: { ticker?: string; from?: string; to?: string } = {}) {
  const result = await render(TradingviewCheckComponent, {
    inputs: { ticker: 'SPY', from: '2026-04-15', to: '2026-05-15', ...scope },
    providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting(), ...fakePickerWorld().providers],
  });
  const http = TestBed.inject(HttpTestingController);
  return { ...result, http, component: result.fixture.componentInstance };
}

describe('TradingviewCheckComponent', () => {
  it('opens without a request: nothing is uploaded or analyzed until a control is pressed', async () => {
    const { http } = await renderCheck();

    http.expectNone(() => true);
    http.verify();
  });

  it('runs disabled until both CSVs are selected', async () => {
    await renderCheck();
    const run = screen.getByRole('button', { name: 'Run validation' }) as HTMLButtonElement;
    expect(run.disabled).toBe(true);
  });

  it('uploads both files with the picked ticker, renders the report, and offers download', async () => {
    const { http, component } = await renderCheck({ ticker: 'aapl' });
    component.ourCsvFile.set(new File(['t,close\n1,2\n'], 'ours.csv', { type: 'text/csv' }));
    component.tvCsvFile.set(new File(['time,close\n1,2\n'], 'theirs.csv', { type: 'text/csv' }));

    await userEvent.click(screen.getByRole('button', { name: 'Run validation' }));
    const req = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/dataset/validation-report`),
    );
    expect(req.request.method).toBe('POST');
    expect((req.request.body as FormData).get('ticker')).toBe('AAPL');
    req.flush({ success: true, report: '# Comparison\n\npandas-ta matches TradingView on 250/250 bars.' });

    expect(await screen.findByText(/pandas-ta matches TradingView/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Download report' })).toBeTruthy();
    http.verify();
  });

  it('surfaces a failed validation response as an alert', async () => {
    const { http, component } = await renderCheck();
    component.ourCsvFile.set(new File(['a'], 'ours.csv'));
    component.tvCsvFile.set(new File(['b'], 'theirs.csv'));

    await userEvent.click(screen.getByRole('button', { name: 'Run validation' }));
    const req = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/dataset/validation-report`),
    );
    req.flush({ success: false, report: '' }, { status: 200, statusText: 'OK' });

    expect(await screen.findByRole('alert')).toBeTruthy();
    http.verify();
  });

  it('runs quality analysis over the dates the link carried and renders server numbers', async () => {
    const { http } = await renderCheck();
    await userEvent.click(screen.getByText('Quality evidence'));
    await userEvent.click(screen.getByRole('button', { name: 'Run quality analysis' }));
    const req = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/data-quality/analyze`),
    );
    expect(req.request.body['symbol']).toBe('SPY');
    expect(req.request.body['from_date']).toBe('2026-04-15');
    expect(req.request.body['to_date']).toBe('2026-05-15');
    req.flush({
      success: true,
      ticker: 'SPY',
      from_date: '2026-04-15',
      to_date: '2026-05-15',
      raw_summary: {
        total_bars: 100, trading_days: 20, zero_volume_bars: 2, flat_bars_ohlc_equal: 0,
        fractional_volume_bars: 0, vwap_above_high: 0, vwap_below_low: 0, ohlc_violations: 0,
        duplicate_timestamps: 1, weekend_bars: 0, intraday_gaps: 3,
      },
      clean_summary: {
        total_bars: 99, trading_days: 20, zero_volume_bars: 0, flat_bars_ohlc_equal: 0,
        fractional_volume_bars: 0, vwap_above_high: 0, vwap_below_low: 0, ohlc_violations: 0,
        duplicate_timestamps: 0, weekend_bars: 0, intraday_gaps: 0,
      },
      steps: [],
    });

    expect(await screen.findByText('Total bars')).toBeTruthy();
    expect(await screen.findByText('99')).toBeTruthy();
    http.verify();
  });

  it.each([
    ['no dates', {}],
    ['a date that is not a calendar day', { from: '2026-02-30', to: '2026-05-15' }],
  ])('keeps quality analysis disabled when the link carried %s, rather than inventing a window', async (_name, scope) => {
    await renderCheck({ from: undefined, to: undefined, ...scope });
    await userEvent.click(screen.getByText('Quality evidence'));

    expect((screen.getByRole('button', { name: 'Run quality analysis' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Pick both dates above before running quality analysis.')).toBeTruthy();
  });
});
