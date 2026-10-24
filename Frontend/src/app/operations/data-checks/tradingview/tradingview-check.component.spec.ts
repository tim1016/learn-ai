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

/** The check as a link opens it: the symbol the link carried is its input. */
async function renderCheck(scope: { ticker?: string } = {}) {
  const result = await render(TradingviewCheckComponent, {
    inputs: { ticker: 'SPY', ...scope },
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

  it('leads to Build dataset for the instrument the picker holds', async () => {
    await renderCheck({ ticker: 'aapl' });

    expect(screen.getByText('Build dataset').getAttribute('href')).toBe('/markets/AAPL/data/build');
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
});
